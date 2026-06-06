#!/usr/bin/env python3
"""health-attribution.py — attribute a health regression to recent file changes.

Spec: core/config/conventions/health-ledger.md §9 (change attribution) + §11
(tiered revert by constitutional ring). Design: workflow wf_b82a26e9-958
(2026-06-03).

Given a regression window [after, before] and the degraded signal, this lists
the candidate causal files changed in the window (primary source: `git log`,
which is cross-machine because git history is shared), scores each
  attribution_score = temporal_proximity × correlation_weight × recency_decay
classifies each by constitutional ring (the single canonical `_classify_ring`),
and filters out (a) commits carrying a `Health-Revert` git trailer (so a revert
can never be re-attributed — breaks the feedback loop) and (b) per-agent
dead-end registry entries for the degraded signal.

PHASE 2 = REPORT ONLY. This engine is read-only / side-effect-free: it never
reverts. Phase 3's revert pipeline consumes `ring` + `authority` + `score` from
this output. The constitutional-ring classifier and the scoring live here so
both phases share one source of truth.

CLI:
  python3 health-attribution.py --after <iso> --before <iso> --signal <name> [--json]
Fail-open: git/parse errors yield an empty candidate list, never a crash.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

_RECENCY_HALF_LIFE_S = 7 * 24 * 3600   # 7-day half-life for recency_decay
_HEALTH_REVERT_TRAILER = "Health-Revert"


def _eprint(msg):
    print(f"[health-attribution] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Constitutional-ring classifier — the SINGLE canonical implementation.
# Spec §11; ring definitions: core/config/conventions/constitutional-rings.md.
# Returns one of: "0" | "1" | "1.5" | "2" | "3" | "unknown".
# Path matching is on a POSIX-style repo-relative path (forward slashes).
# ---------------------------------------------------------------------------
def _norm_relpath(path):
    """Normalize to a forward-slash repo-relative path (strip leading ./)."""
    p = str(path).replace("\\", "/").lstrip("/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _classify_ring(path):
    p = _norm_relpath(path)
    base = p.rsplit("/", 1)[-1]

    # --- Ring 0: the constitutional anchor — NEVER touch (skip + discard) ---
    if base == "settings.local.json" and p.endswith(".claude/settings.local.json"):
        return "0"
    if base in ("settings-structural-validator.py", "settings-structural-validator.sh"):
        return "0"

    # --- Ring 1: core mission — user owns intent (notify + participants:[user]) ---
    if p == "world/program.md":
        return "1"
    if p.startswith("agents/") and base == "self.md":
        return "1"
    if p.startswith("core/config/conventions/"):
        return "1"
    if p.startswith("core/config/modes/"):
        return "1"
    if p.startswith(".claude/rules/"):
        return "1"

    # --- Ring 3: protocols & parameters — fully autonomous (auto-revert eligible) ---
    # Checked BEFORE Ring 2's broad core/config catch so meta strategies and the
    # agent's mutable developmental-stage are correctly the auto tier.
    if p.startswith("meta/") and (p.endswith(".yaml") or p.endswith(".yml")):
        return "3"
    if p.startswith("agents/") and base == "developmental-stage.yaml":
        return "3"

    # --- Ring 1.5: critical framework infra — treat as Ring 2 (NEVER auto) ---
    # Executable framework code: a bad auto-revert here can brick the loop.
    if p.startswith("core/scripts/") and (p.endswith(".py") or p.endswith(".sh")):
        return "1.5"
    if p.startswith("mind_api/src/") and p.endswith(".py"):
        return "1.5"
    # Skill pseudocode is executable framework behavior (scripts run it) — a
    # bad auto-revert here silently changes how the loop reasons. Classify
    # explicitly rather than letting it fall to 'unknown' (functionally the
    # same authority — agent, never-auto — but semantically clear).
    if p.startswith(".claude/skills/"):
        return "1.5"

    # --- Ring 2: standards & metrics — agent-gated (revert → verify → keep/undo) ---
    if p in ("core/config/aspirations.yaml",
             "core/config/evolution-triggers.yaml",
             "core/config/developmental-stage.yaml",
             "core/config/curriculum.yaml"):
        return "2"
    if p in ("world/guardrails.jsonl", "world/reasoning-bank.jsonl"):
        return "2"
    if p.startswith("world/conventions/"):
        return "2"

    # Unmatched: conservative — never auto-revert something unclassified.
    return "unknown"


def _ring_authority(ring, score=0.0, confidence_gate=0.70):
    """Map a ring (+score for Ring 3) to a revert authority:
    'never'  — Ring 0; an attribution here is wrong, skip+discard.
    'user'   — Ring 1; pre-notify + participants:[user] Unblock.
    'agent'  — Ring 1.5/2/unknown; agent-gated revert→verify→keep/undo.
    'auto'   — Ring 3 with score ≥ confidence_gate (else demote to 'agent')."""
    if ring == "0":
        return "never"
    if ring == "1":
        return "user"
    if ring == "3":
        return "auto" if score >= confidence_gate else "agent"
    return "agent"  # 1.5, 2, unknown


# ---------------------------------------------------------------------------
# Scoring — pure functions (independently unit-tested).
# ---------------------------------------------------------------------------
def _temporal_proximity(commit_ts, after_ts, before_ts):
    """Position of the commit within the regression window, in [0,1]. Commits
    nearer `before_ts` (where the drop is observed) are more suspect → score
    higher. Degenerate window (after==before) → 1.0."""
    span = before_ts - after_ts
    if span <= 0:
        return 1.0
    frac = (commit_ts - after_ts) / span
    return max(0.0, min(1.0, frac))


def _recency_decay(commit_ts, now_ts, half_life_s=_RECENCY_HALF_LIFE_S):
    """Exponential decay by absolute age: a commit `half_life_s` old scores 0.5,
    a fresh commit ~1.0. Dampens stale candidates surfaced by a wide window.
    Future-dated commits (clock skew) clamp to 1.0."""
    age = now_ts - commit_ts
    if age <= 0:
        return 1.0
    return math.exp(-math.log(2) * age / half_life_s)


def _correlation_weight(path, signal, default_weight, rules):
    """Highest correlation_weight among rules for `signal` whose globs match
    `path`; `default_weight` (unknown) when no rule matches. A path matching a
    direct (1.0) glob for the degraded signal outranks the default."""
    p = _norm_relpath(path)
    best = None
    for rule in rules:
        if rule.get("signal") != signal:
            continue
        w = rule.get("weight")
        if w is None:
            continue
        for g in rule.get("globs") or []:
            if fnmatch.fnmatch(p, g):
                best = w if best is None else max(best, w)
                break
    return float(best) if best is not None else float(default_weight)


def score_candidate(path, commit_ts, signal, *, after_ts, before_ts, now_ts,
                    default_weight, rules, half_life_s=_RECENCY_HALF_LIFE_S):
    tp = _temporal_proximity(commit_ts, after_ts, before_ts)
    cw = _correlation_weight(path, signal, default_weight, rules)
    rd = _recency_decay(commit_ts, now_ts, half_life_s)
    return round(tp * cw * rd, 4)


# ---------------------------------------------------------------------------
# Config / registry / git readers (tolerant; fail-open to empty).
# ---------------------------------------------------------------------------
def _load_correlation(config_dir):
    """(default_weight, rules) from
    aspirations.yaml health_regression.signal_file_correlation. Fail-open to
    (0.3, []) so attribution still runs (every candidate = unknown weight)."""
    try:
        import yaml
        with open(Path(config_dir) / "aspirations.yaml", encoding="utf-8") as f:
            full = yaml.safe_load(f) or {}
        sfc = ((full.get("health_regression") or {}).get("signal_file_correlation")) or {}
        return float(sfc.get("default_weight", 0.3)), list(sfc.get("rules") or [])
    except Exception as e:  # noqa: BLE001 — fail-open
        _eprint(f"correlation config read failed ({e}) — default 0.3, no rules")
        return 0.3, []


def _load_dead_ends(agent_dir, signal, now_ts):
    """Set of file paths in the per-agent dead-end registry for `signal` whose
    entries have NOT expired. Absent file → empty set (Phase 3 writes it)."""
    out = set()
    try:
        import yaml
        path = Path(agent_dir) / "health-dead-ends.yaml"
        if not path.exists():
            return out
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for e in data.get("dead_ends") or []:
            if e.get("signal") != signal:
                continue
            exp = e.get("expires_at")
            if exp:
                try:
                    if datetime.strptime(exp, "%Y-%m-%dT%H:%M:%S").timestamp() < now_ts:
                        continue  # expired — no longer a dead end
                except ValueError:
                    pass
            if e.get("file"):
                out.add(_norm_relpath(e["file"]))
    except Exception as e:  # noqa: BLE001 — tolerant
        _eprint(f"dead-end registry read failed ({e})")
    return out


def _git_changed_files(after_iso, before_iso, repo_root):
    """Files changed in the window, newest commit per file. Each entry:
    {path, commit, ts, health_revert}. Commits carrying a Health-Revert trailer
    are flagged so the caller drops them. Fail-open to [] (best-effort)."""
    # NUL-delimited so paths with spaces survive; trailer value isolates reverts.
    # `%x00` is git's pretty-format escape for an output NUL byte — the ARGUMENT
    # stays printable ASCII (a literal "\x00" in argv is rejected by Python with
    # "embedded null character"); git emits real NULs in the OUTPUT, which
    # _parse_git_log splits on.
    fmt = "%x00C%x00%H%x00%ct%x00%(trailers:key=Health-Revert,valueonly,separator=|)"
    cmd = ["git", "-C", str(repo_root), "log",
           f"--after={after_iso}", f"--before={before_iso}",
           "--no-merges", f"--format={fmt}", "--name-only"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except Exception as e:  # noqa: BLE001 — fail-open (git missing, timeout, etc.)
        _eprint(f"git log failed ({e})")
        return []
    if r.returncode != 0:
        _eprint(f"git log rc={r.returncode}: {(r.stderr or '').strip()[:200]}")
        return []
    return _parse_git_log(r.stdout)


def _parse_git_log(stdout):
    """Parse the NUL-delimited `git log --name-only` stream into per-file
    newest-commit records. Separated from _git_changed_files so it is unit-
    testable without a real repo."""
    best = {}  # path -> (ts, commit, health_revert)
    cur_commit = cur_ts = None
    cur_revert = False
    for raw in stdout.split("\x00C\x00"):
        if not raw.strip("\x00\n "):
            continue
        # raw = "<H>\x00<ct>\x00<revert>\n<file>\n<file>...". First line is meta.
        parts = raw.split("\x00", 2)
        if len(parts) < 3:
            continue
        cur_commit = parts[0].strip()
        try:
            cur_ts = int(parts[1].strip())
        except ValueError:
            continue
        rest = parts[2]
        nl = rest.find("\n")
        revert_val = (rest[:nl] if nl >= 0 else rest).strip()
        cur_revert = bool(revert_val)
        files = rest[nl + 1:].splitlines() if nl >= 0 else []
        for f in files:
            f = f.strip()
            if not f:
                continue
            key = _norm_relpath(f)
            prev = best.get(key)
            if prev is None or cur_ts > prev[0]:
                best[key] = (cur_ts, cur_commit, cur_revert)
    return [{"path": k, "ts": v[0], "commit": v[1], "health_revert": v[2]}
            for k, v in best.items()]


# ---------------------------------------------------------------------------
# Orchestrator — pure given injected changed_files (for tests) or live git.
# ---------------------------------------------------------------------------
def attribute(after_ts, before_ts, signal, *, config_dir, repo_root, agent_dir,
              now_ts, confidence_gate=0.70, changed_files=None):
    """Return scored, ring-classified, filtered candidates (highest score first).
    `changed_files`: inject a list of {path, ts, commit, health_revert} to bypass
    git (tests). When None, runs `git log` over [after_ts, before_ts]."""
    default_weight, rules = _load_correlation(config_dir)
    dead_ends = _load_dead_ends(agent_dir, signal, now_ts)

    if changed_files is None:
        after_iso = datetime.fromtimestamp(after_ts).strftime("%Y-%m-%dT%H:%M:%S")
        before_iso = datetime.fromtimestamp(before_ts).strftime("%Y-%m-%dT%H:%M:%S")
        changed_files = _git_changed_files(after_iso, before_iso, repo_root)

    candidates = []
    for cf in changed_files:
        path = _norm_relpath(cf["path"])
        if cf.get("health_revert"):
            continue  # never re-attribute a prior health-revert (feedback loop)
        if path in dead_ends:
            continue  # known dead end for this signal
        ring = _classify_ring(path)
        if ring == "0":
            _eprint(f"attribution matched Ring 0 anchor {path} — discarding (wrong by construction)")
            continue
        score = score_candidate(path, cf["ts"], signal, after_ts=after_ts,
                                before_ts=before_ts, now_ts=now_ts,
                                default_weight=default_weight, rules=rules)
        candidates.append({
            "path": path,
            "commit": cf.get("commit"),
            "ts": cf["ts"],
            "ring": ring,
            "authority": _ring_authority(ring, score, confidence_gate),
            "score": score,
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def main():
    ap = argparse.ArgumentParser(description="Attribute a health regression to recent file changes (Phase 2: report only).")
    ap.add_argument("--after", required=True, help="window start, ISO 8601 local (YYYY-MM-DDTHH:MM:SS)")
    ap.add_argument("--before", required=True, help="window end, ISO 8601 local")
    ap.add_argument("--signal", required=True, help="degraded signal name (e.g. deep_ratio)")
    ap.add_argument("--json", action="store_true", help="emit JSON (default: human table)")
    args = ap.parse_args()

    try:
        import _paths
        config_dir = _paths.CONFIG_DIR
        repo_root = _paths.PROJECT_ROOT
        agent = (_paths.AGENT_NAME or "")
        agent_dir = _paths.agent_dir(agent) if agent else None
    except Exception as e:  # noqa: BLE001 — fail-open
        _eprint(f"path resolution failed ({e})")
        print("[]" if args.json else "")
        return 0

    def _ts(iso):
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").timestamp()

    try:
        after_ts, before_ts = _ts(args.after), _ts(args.before)
    except ValueError as e:
        _eprint(f"bad --after/--before ({e}); expected YYYY-MM-DDTHH:MM:SS")
        return 2

    now_ts = datetime.now().timestamp()
    cands = attribute(after_ts, before_ts, args.signal, config_dir=config_dir,
                      repo_root=repo_root, agent_dir=agent_dir, now_ts=now_ts)

    if args.json:
        print(json.dumps({"signal": args.signal, "window": [args.after, args.before],
                          "candidates": cands}, ensure_ascii=True, indent=2))
    else:
        if not cands:
            print(f"No attribution candidates for {args.signal} in window.")
        else:
            print(f"Attribution candidates for {args.signal} ({args.after} → {args.before}):")
            for c in cands:
                print(f"  {c['score']:.4f}  ring={c['ring']:<7} {c['authority']:<6} "
                      f"{c['path']}  ({c['commit'][:8] if c['commit'] else '-'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
