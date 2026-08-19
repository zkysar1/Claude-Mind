#!/usr/bin/env python3
"""utilization-stats — read-only reporting on guardrail / rb / rules utilization.

Composite evidence score (single source of truth for retire-candidate ranking):

    evidence = times_helpful
             + 0.5  * times_inferred_helpful
             + 0.25 * times_active
             + 1.0  * times_cited

Candidate filter (B.1 of the curation plan):

    candidate iff
      status == 'active'
      AND (auto_flagged_for_review == true   OR   evidence == 0)
      AND retrieval_count >= 50
      AND age_days >= 30
      AND (next_review_eligible_at is absent OR <= today)

The exposure floor used to be `(retrieval_count + times_skipped) >= 50`, but
the 2026-05-09 audit found `times_skipped` was inflated 2-15x by
guardrail-check.py firing skip-increments on every non-matching active entry
per call. Skip is now incremented only by reflect-bookkeeping.py (LLM
deliberation marks items as skipped) and is no longer a reliable exposure
signal. The exposure floor is now `retrieval_count` only — a strictly tighter
gate that limits curation candidacy to entries that have actually been pulled
into context. New entries are protected by the age gate; mature low-utility
entries still surface.

`next_review_eligible_at` (top-level, optional, ISO date) is the curation-side
exemption window written by `aspirations-curate-memory` after a KEEP decision:
14 days for zero-evidence entries, 30 days otherwise. Absent → eligible.

`auto_flagged_for_review` (top-level, optional, bool) is set by C.3's
`times_inferred_unknown` accumulator when an entry hits the unknown threshold.
Forces inclusion in the candidate list regardless of evidence (because we
explicitly want to revisit something that has consistently failed --infer).

Subcommands:
    guardrails report                      — full ranked list (JSON)
    guardrails candidates --limit N        — top-N retire candidates (JSON)
    rb report
    rb candidates --limit N
    rules audit                            — staleness / overlap / citation report

Stdout is JSON in all modes; consumers: `aspirations-curate-memory` sub-skill,
the daily g-115-348 goal, and the weekly g-115-rules-audit goal.

Read-only: never writes to any store. Pure reporting.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR, AGENT_DIR, META_DIR, PROJECT_ROOT, agents_root as _agents_root  # type: ignore  # noqa: E402
from _utilization_store import load_counters, utilization_of  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CANDIDATE_LIMIT = 15

# Candidate filter tunables. Keep in sync with the docstring.
# 2026-05-09: MIN_EXPOSURE now applies to retrieval_count alone (skip dropped
# from the formula — see module docstring).
MIN_EXPOSURE = 50          # retrieval_count
MIN_AGE_DAYS = 30
RULES_OVERLAP_JACCARD_MIN = 0.6  # report-only; not a hard threshold

# DO NOT INLINE the evidence formula here without updating the parallel
# utilization_score_v2 in reasoning-bank.py / tree.py / utilization-feedback.py.
# They share the same weights by design — see C.1.
EVIDENCE_WEIGHTS = {
    "times_helpful": 1.0,
    "times_inferred_helpful": 0.5,
    "times_active": 0.25,
    "times_cited": 1.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today():
    return datetime.now().date()


def _parse_date(s):
    """Parse an ISO date or full ISO timestamp into a date. None on failure."""
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


def _read_jsonl(path):
    """Read a JSONL file. Empty list on missing file or unreadable lines."""
    p = Path(path)
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
                # Surface corruption to stderr but don't fail the read — a
                # single bad line shouldn't block reporting on the rest.
                print(f"[utilization-stats] WARN: corrupt line in {p}",
                      file=sys.stderr)
    return items


def _evidence(util):
    """Composite evidence score. See module docstring."""
    if not isinstance(util, dict):
        return 0.0
    score = 0.0
    for key, weight in EVIDENCE_WEIGHTS.items():
        v = util.get(key, 0)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            score += weight * v
    return score


def _is_candidate(rec, today=None, counters=None):
    """Apply the candidate filter. See module docstring."""
    if today is None:
        today = _today()
    if rec.get("status") != "active":
        return False

    util = utilization_of(rec, counters) or {}

    # Auto-flagged forces inclusion (C.3 escape hatch — explicit "we tried
    # --infer N times and couldn't classify, please review").
    if rec.get("auto_flagged_for_review"):
        return True

    if _evidence(util) > 0:
        return False

    # Exposure = retrieval_count alone (2026-05-09 audit). times_skipped was
    # historically included but is unreliable post-2026-05-09 — see module
    # docstring.
    rc = util.get("retrieval_count", 0)
    if rc < MIN_EXPOSURE:
        return False

    created = _parse_date(rec.get("created"))
    if created is None:
        # No created date — treat as old-enough so we can still review it
        pass
    elif (today - created).days < MIN_AGE_DAYS:
        return False

    eligible_after = _parse_date(rec.get("next_review_eligible_at"))
    if eligible_after is not None and eligible_after > today:
        return False

    return True


def _candidate_sort_key(rec, counters=None):
    """Sort: evidence asc, age desc, exposure desc.

    Ties go to the oldest, most-exposed entries — those have the strongest
    "we've shown this many times to no effect" signal. Exposure is
    retrieval_count alone since 2026-05-09 (times_skipped dropped — see
    module docstring on the inflation issue).
    """
    util = utilization_of(rec, counters) or {}
    evidence = _evidence(util)
    created = _parse_date(rec.get("created")) or _today()
    age_days = (_today() - created).days
    rc = util.get("retrieval_count", 0)
    return (evidence, -age_days, -rc)


def _summarize(rec, kind, counters=None):
    """Compact dict per record for JSON output."""
    util = utilization_of(rec, counters) or {}
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


# ---------------------------------------------------------------------------
# Subcommands: guardrails + rb
# ---------------------------------------------------------------------------

def _path_for(kind):
    if kind == "guardrails":
        return WORLD_DIR / "guardrails.jsonl"
    if kind == "rb":
        return WORLD_DIR / "reasoning-bank.jsonl"
    raise ValueError(f"unknown kind: {kind}")


def _record_kind(kind):
    return "guardrail" if kind == "guardrails" else "reasoning_bank"


def _store_kind(kind):
    """This script's CLI kind -> the sidecar store's kind (_utilization_store.KINDS).

    Three vocabularies for the same two stores already exist here: the CLI says
    `rb`, `_record_kind` says `reasoning_bank`, and the sidecar says
    `reasoning-bank`. Mapping explicitly beats reusing `_record_kind`, whose
    underscore form `_check_kind` would reject.
    """
    return "guardrails" if kind == "guardrails" else "reasoning-bank"


def cmd_report(kind):
    """Full ranked list, evidence ascending."""
    path = _path_for(kind)
    # Sidecar counters, loaded ONCE per command rather than per record — this is
    # a file read, and the per-record helpers take the map ( item 1).
    # Empty until the writer lands, at which point `utilization_of` falls through
    # to the embedded field and behaviour is byte-identical to before conversion.
    counters = load_counters(_store_kind(kind), WORLD_DIR)
    recs = [r for r in _read_jsonl(path) if r.get("status") == "active"]
    summaries = [_summarize(r, _record_kind(kind), counters) for r in recs]
    summaries.sort(key=lambda s: (s["evidence"], -(s["age_days"] or 0)))
    out = {
        "kind": kind,
        "active_count": len(summaries),
        "items": summaries,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_candidates(kind, limit):
    """Top-N retire candidates."""
    path = _path_for(kind)
    counters = load_counters(_store_kind(kind), WORLD_DIR)
    recs = _read_jsonl(path)
    today = _today()
    candidates = [r for r in recs if _is_candidate(r, today, counters)]
    candidates.sort(key=lambda r: _candidate_sort_key(r, counters))
    if limit and limit > 0:
        candidates = candidates[: int(limit)]
    summaries = [_summarize(r, _record_kind(kind), counters) for r in candidates]
    out = {
        "kind": kind,
        "candidate_count": len(summaries),
        "limit": limit,
        "items": summaries,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Subcommand: rules audit
# ---------------------------------------------------------------------------

# Path-like reference pattern. Captures things like:
#   core/scripts/foo.py
#   .claude/skills/bar/SKILL.md
#   world/conventions/baz.md
# but NOT bare words and NOT names that happen to start with a framework-dir
# stem (e.g. `world-cat.sh` is a script name, not the path `world/cat.sh` —
# the trailing `/` after the prefix is the discriminator).
#
# DO NOT switch the start anchor to `\b`. `\b` is a word/non-word transition,
# and `.claude` starts with a non-word char (`.`), so `\b\.claude` never
# matches when `.claude` is preceded by another non-word char (space, backtick,
# etc.) — every `.claude/...` reference becomes invisible to the audit.
# Negative lookbehind `(?<![\w.-])` is the correct anchor: it rejects when
# the prior char is word/dot/dash (preventing `myworld/...`, `precore/...`,
# `world-cat.sh` false positives) without depending on word-boundary semantics.
_PATH_PATTERN = re.compile(
    r"(?<![\w.-])((?:core|world|meta|alpha|bravo|\.claude)/[\w./-]+\.(?:py|sh|md|yaml|yml|json|jsonl))\b"
)


def _resolve_referenced_path(ref):
    """Return the absolute Path that a reference like 'world/foo.md' should
    resolve to under the configured external paths. None means we can't make
    sense of it and shouldn't flag it as stale (fail-open).
    """
    if not ref:
        return None
    if ref.startswith("world/") and WORLD_DIR is not None:
        return WORLD_DIR / ref[len("world/"):]
    if ref.startswith("meta/") and META_DIR is not None:
        return META_DIR / ref[len("meta/"):]
    # Project-root-relative (core/, .claude/, alpha/, bravo/).
    return PROJECT_ROOT / ref


def _gather_citation_haystack():
    """Read all stores where a rule filename might be cited. One concatenated
    string is enough — we only count occurrences."""
    sources = []
    if WORLD_DIR is not None:
        sources.append(WORLD_DIR / "reasoning-bank.jsonl")
        sources.append(WORLD_DIR / "guardrails.jsonl")
        board_dir = WORLD_DIR / "board"
        if board_dir.exists():
            sources.extend(sorted(board_dir.glob("*.jsonl")))
    if AGENT_DIR is not None:
        sources.append(AGENT_DIR / "journal.jsonl")
        sources.append(AGENT_DIR / "experience.jsonl")
        # Also include this agent's experience + temp markdown content.
        # experience.jsonl is metadata only; rule citations live in the
        # per-trace markdown content_path (<agent>/experience/<id>.md) and
        # in <agent>/temp/*.md. Without these sources, rules cited only
        # in trace bodies show false-zero citation_count (,
        # evidence from  follow-up: 9-file probe confirmed
        # code-review-protocol.md / implementation-discipline.md /
        # learning-philosophy.md / pre-completion-review.md citations all
        # land in agent experience+temp markdown; reports/ abolished 2026-06-02).
        for sub in ("experience", "temp"):
            sub_dir = AGENT_DIR / sub
            if sub_dir.exists():
                sources.extend(sorted(sub_dir.glob("*.md")))
        # Also include other agents' journals + experience/temp markdown
        # — cross-agent citations count. Gated on local-paths.conf so only
        # registered agent dirs are scanned (avoids picking up stray
        # top-level dirs that happen to contain experience/ or temp/).
        for sib in sorted(_agents_root().glob("*/local-paths.conf")):
            agent_dir = sib.parent
            if agent_dir == AGENT_DIR:
                continue
            sources.append(agent_dir / "journal.jsonl")
            for sub in ("experience", "temp"):
                sub_dir = agent_dir / sub
                if sub_dir.exists():
                    sources.extend(sorted(sub_dir.glob("*.md")))
    # Framework + skill markdown surfaces. Rule citations often live in
    # CLAUDE.md, conventions, verification checklists, and SKILL.md files;
    # without these sources 9/24 rules show false-zero citation_count
    # (, evidence from  investigation).
    sources.append(PROJECT_ROOT / "CLAUDE.md")
    config_dir = PROJECT_ROOT / "core" / "config"
    if config_dir.exists():
        sources.extend(sorted(config_dir.glob("*.md")))
        conventions_dir = config_dir / "conventions"
        if conventions_dir.exists():
            sources.extend(sorted(conventions_dir.glob("*.md")))
    skills_dir = PROJECT_ROOT / ".claude" / "skills"
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


def cmd_rules_audit():
    """Audit .claude/rules/*.md for staleness, overlap, and citation count."""
    rules_dir = PROJECT_ROOT / ".claude" / "rules"
    if not rules_dir.exists():
        print(json.dumps({"status": "no_rules_dir",
                          "path": str(rules_dir)}))
        return

    rule_files = sorted(p for p in rules_dir.glob("*.md") if p.is_file())
    haystack = _gather_citation_haystack()

    # Per-file analysis ------------------------------------------------------
    audit_items = []
    contents_lc = {}  # for overlap detection
    for rf in rule_files:
        try:
            text = rf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Citation count: each occurrence of `.claude/rules/<stem>` (with
        # optional trailing `.md`) OR a bare `<stem>.md` reference is one
        # citation. Two patterns evaluated, sums combined:
        #
        # 1. Prefixed:  `.claude/rules/<stem>` or `.claude/rules/<stem>.md`
        # 2. Bare:      `<stem>.md` (without the `.claude/rules/` prefix)
        #
        # The bare pattern uses two negative lookbehinds to prevent two
        # false-positive classes:
        #   - `(?<!\.claude/rules/)` excludes the prefixed form (already
        #     counted by pattern 1; prevents double-counting).
        #   - `(?<![\w-])` excludes partial matches inside a longer hyphen-
        #     joined name like `test-<stem>.md` — the char before the stem
        #     must NOT be a word char or hyphen.
        #
        #  audit: bare references dominate the natural writing
        # pattern in agent experience+reports (33 bare vs 14 prefixed
        # across 4 surveyed rule stems). Stem collisions across the entire
        # repo+world+meta tree: 0 (verified via glob). Path-A (force
        # prefix-style writing convention) fights the natural pattern;
        # Path-B (this change — accept bare with strict lookbehind) reflects
        # actual citations without false-positive risk under current
        # filename layout. New same-stem files outside `.claude/rules/`
        # would create FP risk; mitigation lives in `domain-leak-check.sh`
        # / future filename-collision audit (filed separately if needed).
        prefixed_pat = re.compile(
            rf"\.claude/rules/{re.escape(rf.stem)}(?:\.md)?\b"
        )
        bare_pat = re.compile(
            rf"(?<![\w-])(?<!\.claude/rules/){re.escape(rf.stem)}\.md\b"
        )
        cite_count = (
            len(prefixed_pat.findall(haystack))
            + len(bare_pat.findall(haystack))
        )

        # Stale references: extract path-like tokens, check existence.
        stale_paths = []
        for ref in sorted(set(_PATH_PATTERN.findall(text))):
            resolved = _resolve_referenced_path(ref)
            if resolved is None:
                continue
            if not resolved.exists():
                stale_paths.append(ref)

        audit_items.append({
            "filename": rf.name,
            "size_bytes": rf.stat().st_size,
            "citation_count": cite_count,
            "stale_paths": stale_paths[:10],  # cap output size
            "stale_path_count": len(stale_paths),
        })

        # Cache lowercased word-set for overlap detection
        contents_lc[rf.name] = set(re.findall(r"[a-z]{4,}", text.lower()))

    # Pairwise overlap (Jaccard on word-sets) --------------------------------
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
                overlaps.append({
                    "file_a": a,
                    "file_b": b,
                    "jaccard": round(jaccard, 3),
                })
    overlaps.sort(key=lambda o: -o["jaccard"])

    out = {
        "kind": "rules_audit",
        "rule_count": len(rule_files),
        "items": audit_items,
        "overlap_pairs": overlaps[:20],
        "overlap_threshold": RULES_OVERLAP_JACCARD_MIN,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        description="Read-only utilization reporting (guardrails / rb / rules).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  utilization-stats.py guardrails report\n"
            "  utilization-stats.py guardrails candidates --limit 15\n"
            "  utilization-stats.py rb candidates --limit 15\n"
            "  utilization-stats.py rules audit\n"
        ),
    )
    sub = parser.add_subparsers(dest="kind", required=True)

    g = sub.add_parser("guardrails", help="guardrail-store reporting")
    g_sub = g.add_subparsers(dest="action", required=True)
    g_sub.add_parser("report", help="full ranked list")
    g_cand = g_sub.add_parser("candidates", help="top-N retire candidates")
    g_cand.add_argument("--limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)

    r = sub.add_parser("rb", help="reasoning-bank reporting")
    r_sub = r.add_subparsers(dest="action", required=True)
    r_sub.add_parser("report", help="full ranked list")
    r_cand = r_sub.add_parser("candidates", help="top-N retire candidates")
    r_cand.add_argument("--limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)

    rules = sub.add_parser("rules", help="rules-directory audit")
    rules_sub = rules.add_subparsers(dest="action", required=True)
    rules_sub.add_parser("audit", help="staleness / overlap / citation report")

    return parser


def main():
    args = _build_parser().parse_args()
    if args.kind == "guardrails":
        if args.action == "report":
            cmd_report("guardrails")
        else:
            cmd_candidates("guardrails", args.limit)
    elif args.kind == "rb":
        if args.action == "report":
            cmd_report("rb")
        else:
            cmd_candidates("rb", args.limit)
    elif args.kind == "rules":
        cmd_rules_audit()


if __name__ == "__main__":
    main()
