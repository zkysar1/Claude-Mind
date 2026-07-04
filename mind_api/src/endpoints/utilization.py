"""Utilization-stats read endpoints — parity with core/scripts/utilization-stats.py.

Daemonises all 5 read-only utilization reports (Batch 6). The module is pure
reporting — zero writes, no history/changelog/lock (CLI docstring line 49).

  GET /v1/utilization/guardrails/report               world
  GET /v1/utilization/guardrails/candidates?limit=N    world
  GET /v1/utilization/rb/report                        world
  GET /v1/utilization/rb/candidates?limit=N            world
  GET /v1/utilization/rules/audit                      mixed (HEADER REQ)

SCOPE: guardrails/rb report+candidates read WORLD only (no header). rules audit
reads PROJECT_ROOT (.claude/rules, CLAUDE.md, core/config/{*.md,conventions/*.md},
.claude/skills/*/SKILL.md), WORLD (guardrails/reasoning-bank/board), the bound
agent's journal/experience/temp, AND every SIBLING agent's journal+experience+
temp (via project_root/agents/*/local-paths.conf, skipping the bound agent) —
so it REQUIRES X-Mind-Agent (400 missing_agent_header otherwise).

BYTE-COMPATIBILITY:
  - 4 store reports + rules-audit MAIN path: json.dumps(out, indent=2,
    ensure_ascii=False) + "\n" (CLI lines 259/278/491). ensure_ascii=False is
    load-bearing — Response.text with the exact string, never Response.json.
  - rules-audit EARLY RETURN (no .claude/rules dir, CLI line 389): json.dumps(
    {"status":"no_rules_dir","path":...}) + "\n" with NO indent and DEFAULT
    ensure_ascii=True. The two serialisations differ on purpose — replicated.

The only sys.exit in the CLI is argparse's missing-subcommand (exit 2),
unreachable in the daemon. Pure functions (_evidence/_is_candidate/
_candidate_sort_key/_summarize/_parse_date) are lifted verbatim; all path
access routes through ctx.paths (WORLD->world, META->meta, AGENT->agent,
PROJECT_ROOT/agents_root->project_root).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# --- constants (verbatim from CLI lines 71-88) ---
DEFAULT_CANDIDATE_LIMIT = 15
MIN_EXPOSURE = 50
MIN_AGE_DAYS = 30
RULES_OVERLAP_JACCARD_MIN = 0.6
EVIDENCE_WEIGHTS = {
    "times_helpful": 1.0,
    "times_inferred_helpful": 0.5,
    "times_active": 0.25,
    "times_cited": 1.0,
}


# ---------------------------------------------------------------------------
# Pure helpers (verbatim from CLI — no path dependencies)
# ---------------------------------------------------------------------------

def _today():
    return datetime.now().date()


def _parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Fault-tolerant: missing file -> []; corrupt lines skipped (CLI line 115).

    The CLI prints a stderr WARN per corrupt line; the daemon drops it silently
    because byte-compat is measured on the JSON body (stdout), not stderr.
    """
    from storage_backend import get_backend
    p = Path(path)
    get_backend().ensure_local(p)  # own-cloud read-path fix: materialize S3-only file before local read; no-op on LocalBackend and out-of-root paths
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _evidence(util):
    if not isinstance(util, dict):
        return 0.0
    score = 0.0
    for key, weight in EVIDENCE_WEIGHTS.items():
        v = util.get(key, 0)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            score += weight * v
    return score


def _is_candidate(rec, today=None):
    if today is None:
        today = _today()
    if rec.get("status") != "active":
        return False
    util = rec.get("utilization", {}) or {}
    if rec.get("auto_flagged_for_review"):
        return True
    if _evidence(util) > 0:
        return False
    rc = util.get("retrieval_count", 0)
    if rc < MIN_EXPOSURE:
        return False
    created = _parse_date(rec.get("created"))
    if created is None:
        pass
    elif (today - created).days < MIN_AGE_DAYS:
        return False
    eligible_after = _parse_date(rec.get("next_review_eligible_at"))
    if eligible_after is not None and eligible_after > today:
        return False
    return True


def _candidate_sort_key(rec):
    util = rec.get("utilization", {}) or {}
    evidence = _evidence(util)
    created = _parse_date(rec.get("created")) or _today()
    age_days = (_today() - created).days
    rc = util.get("retrieval_count", 0)
    return (evidence, -age_days, -rc)


def _summarize(rec, kind):
    util = rec.get("utilization", {}) or {}
    created = _parse_date(rec.get("created"))
    age_days = (_today() - created).days if created else None
    out = {
        "id": rec.get("id"),
        "kind": kind,
        "status": rec.get("status"),
        "evidence": round(_evidence(util), 4),
        "retrieval_count": util.get("retrieval_count", 0),
        "times_helpful": util.get("times_helpful", 0),
        "times_inferred_helpful": util.get("times_inferred_helpful", 0),
        "times_active": util.get("times_active", 0),
        "times_cited": util.get("times_cited", 0),
        "times_skipped": util.get("times_skipped", 0),
        "times_inferred_unknown": util.get("times_inferred_unknown", 0),
        "age_days": age_days,
        "created": rec.get("created"),
        "next_review_eligible_at": rec.get("next_review_eligible_at"),
        "auto_flagged_for_review": bool(rec.get("auto_flagged_for_review", False)),
        "category": rec.get("category", ""),
    }
    if kind == "guardrail":
        out["rule"] = (rec.get("rule") or "")[:140]
    else:
        out["title"] = (rec.get("title") or "")[:140]
    return out


def _record_kind(kind):
    return "guardrail" if kind == "guardrails" else "reasoning_bank"


def _store_path(ctx, kind: str) -> Path:
    if kind == "guardrails":
        return ctx.paths.world / "guardrails.jsonl"
    return ctx.paths.world / "reasoning-bank.jsonl"


def _out(obj: Any) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# guardrails / rb : report + candidates
# ---------------------------------------------------------------------------

def _report(ctx, kind: str) -> "Response":  # type: ignore[name-defined]
    recs = [r for r in _read_jsonl(_store_path(ctx, kind)) if r.get("status") == "active"]
    summaries = [_summarize(r, _record_kind(kind)) for r in recs]
    summaries.sort(key=lambda s: (s["evidence"], -(s["age_days"] or 0)))
    return _out({"kind": kind, "active_count": len(summaries), "items": summaries})


def _candidates(ctx, kind: str) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    limit_raw = ctx.query.get("limit")
    if limit_raw is None or limit_raw == "":
        limit = DEFAULT_CANDIDATE_LIMIT
    else:
        try:
            limit = int(limit_raw)
        except ValueError:
            return Response.error(400, "invalid_param", "limit must be an integer")
    recs = _read_jsonl(_store_path(ctx, kind))
    today = _today()
    candidates = [r for r in recs if _is_candidate(r, today)]
    candidates.sort(key=_candidate_sort_key)
    if limit and limit > 0:
        candidates = candidates[: int(limit)]
    summaries = [_summarize(r, _record_kind(kind)) for r in candidates]
    return _out({
        "kind": kind,
        "candidate_count": len(summaries),
        "limit": limit,
        "items": summaries,
    })


def guardrails_report(ctx):
    return _report(ctx, "guardrails")


def guardrails_candidates(ctx):
    return _candidates(ctx, "guardrails")


def rb_report(ctx):
    return _report(ctx, "rb")


def rb_candidates(ctx):
    return _candidates(ctx, "rb")


# ---------------------------------------------------------------------------
# rules audit
# ---------------------------------------------------------------------------

_PATH_PATTERN = re.compile(
    r"(?<![\w.-])((?:core|world|meta|alpha|bravo|\.claude)/[\w./-]+\.(?:py|sh|md|yaml|yml|json|jsonl))\b"
)


def _resolve_referenced_path(ctx, ref):
    if not ref:
        return None
    if ref.startswith("world/") and ctx.paths.world is not None:
        return ctx.paths.world / ref[len("world/"):]
    if ref.startswith("meta/") and ctx.paths.meta is not None:
        return ctx.paths.meta / ref[len("meta/"):]
    return ctx.paths.project_root / ref


def _gather_citation_haystack(ctx) -> str:
    sources = []
    world = ctx.paths.world
    agent = ctx.paths.agent
    project_root = ctx.paths.project_root
    if world is not None:
        sources.append(world / "reasoning-bank.jsonl")
        sources.append(world / "guardrails.jsonl")
        board_dir = world / "board"
        if board_dir.exists():
            sources.extend(sorted(board_dir.glob("*.jsonl")))
    if agent is not None:
        sources.append(agent / "journal.jsonl")
        sources.append(agent / "experience.jsonl")
        for sub in ("experience", "temp"):
            sub_dir = agent / sub
            if sub_dir.exists():
                sources.extend(sorted(sub_dir.glob("*.md")))
        agents_root = project_root / "agents"
        for sib in sorted(agents_root.glob("*/local-paths.conf")):
            agent_d = sib.parent
            if agent_d == agent:
                continue
            sources.append(agent_d / "journal.jsonl")
            for sub in ("experience", "temp"):
                sub_dir = agent_d / sub
                if sub_dir.exists():
                    sources.extend(sorted(sub_dir.glob("*.md")))
    sources.append(project_root / "CLAUDE.md")
    config_dir = project_root / "core" / "config"
    if config_dir.exists():
        sources.extend(sorted(config_dir.glob("*.md")))
        conventions_dir = config_dir / "conventions"
        if conventions_dir.exists():
            sources.extend(sorted(conventions_dir.glob("*.md")))
    skills_dir = project_root / ".claude" / "skills"
    if skills_dir.exists():
        sources.extend(sorted(skills_dir.glob("*/SKILL.md")))
    parts = []
    for src in sources:
        p = Path(src)
        if not p.exists():
            continue
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def rules_audit(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for rules audit "
            "(citation haystack reads the bound agent's journal/experience/temp).")

    rules_dir = ctx.paths.project_root / ".claude" / "rules"
    if not rules_dir.exists():
        # EARLY RETURN trap: compact (no indent), default ensure_ascii=True.
        return Response.text(
            json.dumps({"status": "no_rules_dir", "path": str(rules_dir)}) + "\n",
            content_type="application/json")

    rule_files = sorted(p for p in rules_dir.glob("*.md") if p.is_file())
    haystack = _gather_citation_haystack(ctx)

    audit_items = []
    contents_lc = {}
    for rf in rule_files:
        try:
            text = rf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        prefixed_pat = re.compile(rf"\.claude/rules/{re.escape(rf.stem)}(?:\.md)?\b")
        bare_pat = re.compile(rf"(?<![\w-])(?<!\.claude/rules/){re.escape(rf.stem)}\.md\b")
        cite_count = len(prefixed_pat.findall(haystack)) + len(bare_pat.findall(haystack))

        stale_paths = []
        for ref in sorted(set(_PATH_PATTERN.findall(text))):
            resolved = _resolve_referenced_path(ctx, ref)
            if resolved is None:
                continue
            if not resolved.exists():
                stale_paths.append(ref)

        audit_items.append({
            "filename": rf.name,
            "size_bytes": rf.stat().st_size,
            "citation_count": cite_count,
            "stale_paths": stale_paths[:10],
            "stale_path_count": len(stale_paths),
        })
        contents_lc[rf.name] = set(re.findall(r"[a-z]{4,}", text.lower()))

    overlaps = []
    names = sorted(contents_lc.keys())
    for i, a in enumerate(names):
        ta = contents_lc.get(a) or set()
        if not ta:
            continue
        for b in names[i + 1:]:
            tb = contents_lc.get(b) or set()
            if not tb:
                continue
            union = len(ta | tb)
            if union == 0:
                continue
            jaccard = len(ta & tb) / union
            if jaccard >= RULES_OVERLAP_JACCARD_MIN:
                overlaps.append({"file_a": a, "file_b": b, "jaccard": round(jaccard, 3)})
    overlaps.sort(key=lambda o: -o["jaccard"])

    return _out({
        "kind": "rules_audit",
        "rule_count": len(rule_files),
        "items": audit_items,
        "overlap_pairs": overlaps[:20],
        "overlap_threshold": RULES_OVERLAP_JACCARD_MIN,
    })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/utilization/guardrails/report")] = guardrails_report
    routes[("GET", "/v1/utilization/guardrails/candidates")] = guardrails_candidates
    routes[("GET", "/v1/utilization/rb/report")] = rb_report
    routes[("GET", "/v1/utilization/rb/candidates")] = rb_candidates
    routes[("GET", "/v1/utilization/rules/audit")] = rules_audit
