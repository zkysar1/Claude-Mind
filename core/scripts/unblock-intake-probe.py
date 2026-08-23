#!/usr/bin/env python3
"""Unblock-intake probe — fast re-check at blocker-claim time.

Detects the rb-1111 pattern: Unblock goals filed against named bugs (file:line,
commit hash, function name in `failure_reason`) can be resolved by independent
commits between filing and pickup. Without an intake-recheck gate, the agent
claims the Unblock and either applies a redundant fix or wastes verification
cycles re-confirming an absent symptom.

CANONICAL INCIDENT (rb-1111, g-115-985, 2026-05-20): filed 2026-05-19T20:30
against `loop-state-save.py:82` recursive-shadow TypeError. Picked up at
2026-05-20T08:30+. Bug already fixed by commit a49e4805 landing between
filing and pickup. Five-signal probe confirmed verify-and-close was the
right path — no code change needed.

USAGE (called by aspirations-execute Phase 4 just-after-claim):
    bash core/scripts/unblock-intake-probe.sh --goal-id g-XXX-NN --source world|agent

Output: single-line JSON to stdout. Exit code is always 0 (probe is
ADVISORY — never blocks the caller). On any failure (missing goal, broken
git, etc.) emits `{"status": "inconclusive", "reason": "..."}` and exits 0.

OUTPUT SHAPE:
  {
    "status": "probable-fix-landed" | "bug-still-present" | "inconclusive" | "skipped",
    "skip_reason": <str|null>,         # set when status == "skipped"
    "age_hours": <float|null>,         # hours since goal.created_at (or detected_at)
    "min_age_hours": <float>,          # configured threshold
    "probed_artifacts": {
        "commit_hashes": [<7-40 hex>, ...],
        "file_refs":     [{"path": <str>, "line": <int>}, ...],
        "function_names": [<str>, ...]
    },
    "signals": [<str>, ...],            # human-readable per-artifact verdict
    "recommendation": <str>,            # "verify-and-close" | "execute-normally" | "investigate"
    "goal_id": <str>,
    "source":  <str>
  }

CONFIG (core/config/aspirations.yaml unblock_intake_probe):
    enabled:        true (default)
    min_age_hours:  6.0  (skip Unblocks filed less than N hours ago — they
                          couldn't realistically have a fix-after-filing race)

DESIGN PRINCIPLES:
  - Fast: file reads + git log only. No supplementary-store retrieval.
  - Conservative: when in doubt → "inconclusive" + "execute-normally".
  - Title-anchored: only probes goals whose title starts with "Unblock:".
  - rb-1111 + rb-428 (sentinel-lifecycle): a recurring probe gate, not a
    one-shot encoder.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )

from _paths import WORLD_DIR, AGENT_DIR


# ──────────────────────────────────────────────────────────────────────────
# Artifact extraction regexes
# ──────────────────────────────────────────────────────────────────────────

# Commit hashes: 7-40 hex chars at word boundaries. Exclude pure-decimal
# (timestamps), pure-uppercase (e.g. "FIXED") that happen to be hex,
# and trailing hash fragments that are part of longer non-hex tokens.
# Word-boundary anchors keep us from matching inside larger identifiers.
_COMMIT_HASH_RE = re.compile(r"\b([0-9a-f]{7,40})\b")

# File:line refs: path with at least one slash OR a dotted module name + line.
# Examples that match: "loop-state-save.py:82", "core/scripts/foo.sh:42",
# "mind_api/src/agent_paths.py:101".
# Examples that don't match: ":82" (no path), "foo.py" (no line).
_FILE_LINE_RE = re.compile(
    r"\b([\w./\-]+\.(?:py|sh|js|ts|tsx|lua|luau|yaml|yml|md|json|jsonl)):(\d+)\b"
)

# Function names: heuristic — uppercase or snake_case identifier followed by
# parentheses, OR "function NAME" / "def NAME" / "class NAME" prefix.
# Conservative — only matches identifiers with explicit function context.
_FUNCTION_NAME_RE = re.compile(
    r"\b(?:def|function|class|fn)\s+(\w[\w_]*)\b"
    r"|\b(\w[\w_]+)\(\)"
)


# ──────────────────────────────────────────────────────────────────────────
# Config + age helpers
# ──────────────────────────────────────────────────────────────────────────

_DEFAULT_MIN_AGE_HOURS = 6.0


def _load_config():
    """Read unblock_intake_probe block from core/config/aspirations.yaml.

    Returns (enabled, min_age_hours). Falls back to (True, 6.0) on any error —
    the probe should run by default when config is missing or malformed.
    """
    cfg_path = CORE_ROOT / "config" / "aspirations.yaml"
    if not cfg_path.exists():
        return True, _DEFAULT_MIN_AGE_HOURS
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        block = data.get("unblock_intake_probe") or {}
        enabled = block.get("enabled", True)
        min_age = float(block.get("min_age_hours", _DEFAULT_MIN_AGE_HOURS))
        return bool(enabled), min_age
    except Exception:
        return True, _DEFAULT_MIN_AGE_HOURS


def _age_hours(ts) -> "float | None":
    """Hours since a timestamp. Returns None if unparseable."""
    if not ts:
        return None
    try:
        t = parse_naive_iso(ts)
    except Exception:
        return None
    return (dt.datetime.now() - t).total_seconds() / 3600.0


# ──────────────────────────────────────────────────────────────────────────
# Goal lookup
# ──────────────────────────────────────────────────────────────────────────

def _find_goal(goal_id: str, source: str):
    """Locate a goal record in world or agent queue. Returns the goal dict or None."""
    base = WORLD_DIR if source == "world" else AGENT_DIR
    p = base / "aspirations.jsonl"
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            for ln in f:
                try:
                    a = json.loads(ln)
                except Exception:
                    continue
                for g in a.get("goals") or []:
                    if g.get("id") == goal_id:
                        return g
    except Exception:
        return None
    return None


def _failure_reason_text(goal: dict) -> str:
    """Extract failure-reason narrative from a goal record.

    Probe-eligible text lives in (priority order):
      1. failure_reason field (top-level)
      2. diagnostic_context.failure_reason (CREATE_BLOCKER payload)
      3. description (free-form Unblock body)
    Returns concatenation of all present fields so artifact extraction sees
    the full picture.
    """
    parts = []
    fr = goal.get("failure_reason")
    if isinstance(fr, str) and fr:
        parts.append(fr)
    dc = goal.get("diagnostic_context") or {}
    if isinstance(dc, dict):
        dfr = dc.get("failure_reason")
        if isinstance(dfr, str) and dfr:
            parts.append(dfr)
    desc = goal.get("description")
    if isinstance(desc, str) and desc:
        parts.append(desc)
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Artifact extraction
# ──────────────────────────────────────────────────────────────────────────

def _extract_artifacts(text: str) -> dict:
    """Parse text for commit hashes, file:line refs, function names.

    Deduplicates within each category preserving first-seen order.
    """
    commits = []
    seen_c = set()
    for m in _COMMIT_HASH_RE.finditer(text):
        h = m.group(1)
        # Skip if it looks like a date fragment (e.g. "2026052") or all-decimal
        if h.isdigit():
            continue
        if h not in seen_c:
            seen_c.add(h)
            commits.append(h)

    files = []
    seen_f = set()
    for m in _FILE_LINE_RE.finditer(text):
        path, line = m.group(1), int(m.group(2))
        key = f"{path}:{line}"
        if key not in seen_f:
            seen_f.add(key)
            files.append({"path": path, "line": line})

    fns = []
    seen_n = set()
    for m in _FUNCTION_NAME_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if not name or len(name) < 3:
            continue
        # Skip common false positives
        if name.lower() in {"def", "function", "class", "self", "args", "kwargs"}:
            continue
        if name not in seen_n:
            seen_n.add(name)
            fns.append(name)

    return {"commit_hashes": commits, "file_refs": files, "function_names": fns}


# ──────────────────────────────────────────────────────────────────────────
# Probes
# ──────────────────────────────────────────────────────────────────────────

def _git(args: list) -> "tuple[int, str]":
    """Run git with PROJECT_ROOT cwd. Returns (rc, stdout)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def _probe_commit(commit_hash: str) -> "tuple[str, str]":
    """Probe: does commit_hash exist in history, and is HEAD past it?

    Returns (verdict, signal-text). Verdicts:
      - "ancestor"     : commit is reachable from HEAD (fix likely landed)
      - "head"         : commit IS HEAD (the named bug fix IS the current state)
      - "missing"      : commit not found
      - "not-ancestor" : commit exists but HEAD is not past it (rare; branch divergence)
      - "inconclusive" : git command failed
    """
    rc, out = _git(["cat-file", "-e", f"{commit_hash}^{{commit}}"])
    if rc != 0:
        return "missing", f"commit {commit_hash[:8]} not in repo history"
    rc, head_sha = _git(["rev-parse", "HEAD"])
    if rc != 0:
        return "inconclusive", f"commit {commit_hash[:8]} exists; could not resolve HEAD"
    head_sha = head_sha.strip()
    if head_sha.startswith(commit_hash) or commit_hash.startswith(head_sha[:7]):
        return "head", f"commit {commit_hash[:8]} IS current HEAD"
    rc, _ = _git(["merge-base", "--is-ancestor", commit_hash, "HEAD"])
    if rc == 0:
        # Look for any commit AFTER it that mentions "fix" or the file
        rc2, log = _git(["log", "--oneline", f"{commit_hash}..HEAD"])
        n_after = len([ln for ln in log.splitlines() if ln.strip()]) if rc2 == 0 else 0
        return "ancestor", (
            f"commit {commit_hash[:8]} reachable from HEAD; "
            f"{n_after} commit(s) landed after it"
        )
    return "not-ancestor", f"commit {commit_hash[:8]} exists but is not ancestor of HEAD"


def _probe_file_line(ref: dict, failure_text: str) -> "tuple[str, str]":
    """Probe: read the named file at the named line. Does the bug-shape
    described in failure_text still appear?

    Returns (verdict, signal-text). Verdicts:
      - "file-deleted"       : gone at HEAD but present in git history (fix-landed evidence)
      - "file-never-existed" : gone at HEAD with NO git history (scores 0 — see below)
      - "line-out-of-range" : file shorter than named line
      - "shape-present"     : a keyword from failure_text appears near the line
      - "shape-absent"      : no failure_text keyword near the named line
      - "inconclusive"      : read error
    """
    path = ref["path"]
    line = ref["line"]
    abs_path = PROJECT_ROOT / path
    if not abs_path.is_file():
        # Try the path verbatim (might be absolute or relative to cwd)
        if not Path(path).is_file():
            # ABSENCE IS EVIDENCE ONLY IF THE FILE ONCE EXISTED. A path that
            # never existed anywhere — pulled out of prose, a typo, a rename,
            # a path from another repo — says nothing about whether a fix
            # landed. Scoring it as fix-landed is what produced a
            # verify-and-close verdict on a live defect (): with
            # EPSILON=0.25 a single bogus file_ref outweighs an empty other
            # side and flips the whole verdict.
            # This mirrors the commit axis, where _probe_commit resolves via
            # cat-file and an unresolvable hash returns "missing" -> weight 0.
            # The two axes disagreed about what absence means; only the commit
            # axis was safe. Keep them agreeing.
            rc, out = _git(["log", "--oneline", "-1", "--", path])
            if rc == 0 and out.strip():
                return "file-deleted", (
                    f"{path} does not exist at HEAD but IS in git history "
                    f"(deleted) — real fix-landed evidence")
            return "file-never-existed", (
                f"{path} does not exist at HEAD and has no git history — "
                f"no evidence either way")
        abs_path = Path(path)
    try:
        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return "inconclusive", f"{path} unreadable: {e}"
    if line > len(lines) or line < 1:
        return "line-out-of-range", f"{path} has {len(lines)} lines; named line {line} is past EOF"
    # Take a 3-line window centered on the named line.
    window = "\n".join(lines[max(0, line - 2):min(len(lines), line + 1)])
    # Extract candidate keywords from failure_text (alphanumeric tokens >=4 chars,
    # excluding common stopwords + the path itself + line numbers).
    stop = {"the", "and", "from", "with", "into", "this", "that", "have", "been",
            "line", "path", "file", "code", "fixed", "error", "issue"}
    keywords = []
    for tok in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", failure_text):
        if tok.lower() in stop:
            continue
        if tok == path or tok in path:
            continue
        keywords.append(tok)
    # Dedup
    seen = set()
    uniq = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    keywords = uniq[:15]  # cap
    if not keywords:
        return "inconclusive", f"{path}:{line} — no probeable keywords in failure_reason"
    hits = [kw for kw in keywords if kw in window]
    if hits:
        return "shape-present", (
            f"{path}:{line} window contains failure-reason keywords: "
            f"{', '.join(hits[:5])}"
        )
    return "shape-absent", (
        f"{path}:{line} window does NOT contain failure-reason keywords "
        f"({', '.join(keywords[:5])})"
    )


def _probe_function(name: str) -> "tuple[str, str]":
    """Probe: does the named function/class still exist in core/scripts/?

    Returns (verdict, signal-text). Verdicts:
      - "defined"      : at least one definition found
      - "absent"       : no definition found
      - "inconclusive" : grep failed

    POSIX ERE compatibility: git grep -E uses POSIX ERE which does NOT support
    PCRE escapes like \\b (word boundary) or \\s (whitespace). The pattern
    below uses [[:space:]]+ for whitespace and relies on the def/function/class
    keyword prefix to anchor the match. False-positive risk: matches "def
    foo_namebar" if name is "foo_name" — accepted because the caller's
    extraction regex already filters to whole identifiers.
    """
    pattern = rf"(def|function|class)[[:space:]]+{re.escape(name)}"
    rc, out = _git(["grep", "-l", "-E", pattern, "--", "core/scripts/", "mind_api/src/"])
    if rc == 0 and out.strip():
        files = out.strip().splitlines()[:3]
        return "defined", f"{name} defined in {len(files)} file(s): {', '.join(files)}"
    if rc == 1:  # git grep "no matches" rc
        return "absent", f"{name} has no def/function/class in core/scripts/ or mind_api/src/"
    return "inconclusive", f"{name} grep failed (rc={rc})"


# ──────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────

def _aggregate(commits, files, fns):
    """Combine per-artifact verdicts into a single status + recommendation.

    Weighted signals — commit-ancestor and file-shape-absence are strong
    fix-landed evidence (weight 1.0); function-existence is weak evidence
    because a function can be REWRITTEN without removal (canonical: g-115-985
    where `_paths_agent_dir` retained the name but the recursive shadow was
    removed). Weight 0.5 for function signals.

    Decision rule: compare weighted fix-landed and bug-present sums.
      - fix_landed_w > bug_present_w + EPSILON: probable-fix-landed
      - bug_present_w > fix_landed_w + EPSILON: bug-still-present
      - tie or both zero: inconclusive
    """
    all_signals = []
    fix_landed_w = 0.0
    bug_present_w = 0.0

    for _h, (v, sig) in commits:
        all_signals.append(sig)
        if v in ("head", "ancestor"):
            fix_landed_w += 1.0
        elif v == "not-ancestor":
            bug_present_w += 1.0

    for _ref, (v, sig) in files:
        all_signals.append(sig)
        # "file-never-existed" is deliberately absent from BOTH branches — it
        # scores 0, exactly like an unresolvable commit hash. See
        # _probe_file_line for why ().
        if v in ("file-deleted", "line-out-of-range", "shape-absent"):
            fix_landed_w += 1.0
        elif v == "shape-present":
            bug_present_w += 1.0

    for _fn, (v, sig) in fns:
        all_signals.append(sig)
        if v == "absent":
            fix_landed_w += 0.5
        elif v == "defined":
            bug_present_w += 0.5

    EPSILON = 0.25  # avoid flipping on near-ties
    if fix_landed_w == 0 and bug_present_w == 0:
        return "inconclusive", "execute-normally", all_signals
    if fix_landed_w > bug_present_w + EPSILON:
        return "probable-fix-landed", "verify-and-close", all_signals
    if bug_present_w > fix_landed_w + EPSILON:
        return "bug-still-present", "execute-normally", all_signals
    return "inconclusive", "execute-normally", all_signals


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Probe Unblock goal intake state")
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--source", choices=("world", "agent"), default="world")
    ap.add_argument("--min-age-hours", type=float, default=None,
                    help="Override config min_age_hours threshold")
    ap.add_argument("--force", action="store_true",
                    help="Skip min-age and title-prefix gates")
    args = ap.parse_args()

    enabled, cfg_min_age = _load_config()
    min_age = args.min_age_hours if args.min_age_hours is not None else cfg_min_age

    def emit(status, **extra):
        # Default recommendation: "execute-normally" for any verdict that does
        # NOT clearly indicate fix-landed. Aggregate path overrides via extra.
        recommendation = extra.pop("recommendation", "execute-normally")
        out = {
            "goal_id": args.goal_id,
            "source": args.source,
            "status": status,
            "recommendation": recommendation,
            "min_age_hours": min_age,
        }
        out.update(extra)
        print(json.dumps(out))
        sys.exit(0)

    if not enabled and not args.force:
        emit("skipped", skip_reason="config disabled (unblock_intake_probe.enabled=false)")

    goal = _find_goal(args.goal_id, args.source)
    if goal is None:
        emit("inconclusive", skip_reason=f"goal {args.goal_id} not found in {args.source} queue")

    # Title gate: only probe Unblock-titled goals.
    title = (goal.get("title") or "").strip()
    if not args.force and not title.lower().startswith("unblock:"):
        emit("skipped", skip_reason=f"title does not start with 'Unblock:' ({title[:40]})",
             age_hours=None)

    # Age gate: skip very-fresh Unblocks.
    age = _age_hours(goal.get("created_at") or goal.get("detected_at"))
    if not args.force and age is not None and age < min_age:
        emit("skipped", skip_reason=f"age {age:.1f}h < min {min_age}h",
             age_hours=age)

    text = _failure_reason_text(goal)
    if not text.strip():
        emit("inconclusive", skip_reason="goal has no failure_reason or description text",
             age_hours=age, probed_artifacts={"commit_hashes": [], "file_refs": [], "function_names": []})

    artifacts = _extract_artifacts(text)
    if not (artifacts["commit_hashes"] or artifacts["file_refs"] or artifacts["function_names"]):
        emit("inconclusive", skip_reason="no named artifacts in failure_reason",
             age_hours=age, probed_artifacts=artifacts)

    commit_results = [(h, _probe_commit(h)) for h in artifacts["commit_hashes"]]
    file_results = [(ref, _probe_file_line(ref, text)) for ref in artifacts["file_refs"]]
    fn_results = [(fn, _probe_function(fn)) for fn in artifacts["function_names"]]

    status, recommendation, signals = _aggregate(commit_results, file_results, fn_results)

    emit(status,
         age_hours=age,
         probed_artifacts=artifacts,
         signals=signals,
         recommendation=recommendation)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # Catch-all fail-open: never break the caller.
        print(json.dumps({"status": "inconclusive", "skip_reason": f"probe crashed: {e}"}))
        sys.exit(0)
