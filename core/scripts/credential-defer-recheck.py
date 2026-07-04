#!/usr/bin/env python3
"""Credential Defer Recheck — re-probe human_blocked: defers naming env-read probes.

For each goal with defer_reason starting "human_blocked:" that names an
env-read/credential probe (e.g., "human_blocked: credential SERVICE_API_KEY
missing"), re-runs `env-read.sh has <KEY>`. When the value is now present,
clears defer_reason and returns the goal to pending.

CONSTRAINT: only auto-clears defers where:
  (a) an env-var key can be extracted from the defer text, AND
  (b) env-read.sh has <KEY> exits 0 (key now present).

Defers with no extractable key (genuinely human-only: user approve-click,
legal counsel, GUI, hardware) are NEVER auto-cleared. This ensures the sweep
only touches agent-re-provisionable blocks, as required by g-115-1709 /
probe-before-defer.md / capability-before-user.md.

Part of the Phase 0.5b re-probe family (mirrors defer-recheck.py pattern):
  Phase 0.5b.3: precondition_unmet: (precondition-defer-recheck.sh)
  Phase 0.5b.4: blocked_on_dependency: (defer-recheck.sh)
  Phase 0.5b.9: human_blocked: env-read-provisionable (this script)

Called by aspirations-precheck Phase 0.5b.9 (Credential Defer Auto-Clear).
Dry-run by default; --apply actually clears defer_reason.

Exit: always 0 (reporting tool, never blocks the loop on error).

JSON output:
  {
    "scanned": N,
    "eligible": N,          # human_blocked: with age >= max_age_hours
    "skipped_no_key": N,    # human-only (no extractable env key)
    "skipped_probe_fail": N, # key extracted but env-read still absent
    "cleared": N,           # cleared on --apply
    "would_clear": [...],   # dry-run list of goal_ids
    "details": [...]
  }
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _rt
from _paths import WORLD_DIR
from _fileops import locked_append_jsonl

# ---------------------------------------------------------------------------
# Env-key extraction — conservative patterns
# ---------------------------------------------------------------------------
# Order matters: more specific patterns are tried first. The fallback bare-key
# pattern only fires when at least one "env/credential" indicator word appears
# in the defer text, so purely human-only defers ("user approve-click",
# "legal counsel sign-off") that contain no such indicator never match.

_INDICATOR_RE = re.compile(
    r'\b(?:credential|env[-_\s]?read|env[-_\s]?var|env[-_\s]?key)\b',
    re.IGNORECASE,
)

# Env-var name: uppercase, may include digits and underscores, at least one
# underscore (distinguishes from English words like "NOT", "THE"), min 4 chars.
_KEY_NAME_RE = re.compile(r'\b([A-Z][A-Z0-9]*_[A-Z0-9][A-Z0-9_]{2,})\b')

# Ordered extraction patterns (most explicit first).
_EXTRACT_PATTERNS = [
    # "env-read.sh has SERVICE_API_KEY" or "env-read has SERVICE_API_KEY"
    re.compile(r'env-?read(?:\.sh)?\s+has\s+([A-Z][A-Z0-9]*_[A-Z0-9][A-Z0-9_]{2,})',
               re.IGNORECASE),
    # "credential SERVICE_API_KEY"
    re.compile(r'\bcredential\s+([A-Z][A-Z0-9]*_[A-Z0-9][A-Z0-9_]{2,})'),
    # "env var/key SERVICE_API_KEY"
    re.compile(r'\benv[-_\s]?(?:var|key)\s+([A-Z][A-Z0-9]*_[A-Z0-9][A-Z0-9_]{2,})'),
]


def _extract_env_key(defer_reason: str) -> str | None:
    """Return the first env-var key embedded in a human_blocked: defer text.

    Returns None when:
      - No env/credential indicator word found (human-only defer → never clear).
      - An indicator is found but no well-formed KEY can be extracted.

    The extracted key must be ALL_CAPS_WITH_UNDERSCORE (min 4 chars, at least
    one underscore) so English words like "NOT" or "THE" never match.
    """
    # Strip the structured prefix before scanning.
    text = re.sub(r'^human_blocked:\s*', '', defer_reason, flags=re.IGNORECASE)

    # Try explicit extraction patterns first (most specific → least).
    for pat in _EXTRACT_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)

    # Fallback: any uppercase-with-underscore word — but ONLY when the defer
    # text contains an env/credential indicator. Pure human-only defers
    # ("user approve-click the deployment") never contain an indicator and
    # never reach this branch.
    if _INDICATOR_RE.search(text):
        m = _KEY_NAME_RE.search(text)
        if m:
            return m.group(1)

    return None


# ---------------------------------------------------------------------------
# Probe — env-read.sh has <KEY>
# ---------------------------------------------------------------------------

def _probe_env_key(key: str) -> bool:
    """Return True iff env-read.sh reports the key as present (exit 0)."""
    env_read = SCRIPT_DIR / "env-read.sh"
    result = subprocess.run(
        ["bash", str(env_read), "has", key],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Goal reading + defer clearing — mirrors defer-recheck.py exactly
# ---------------------------------------------------------------------------

def _read_goals(source: str) -> list[dict]:
    """Read world or agent goals via daemon (_rt). Fatal on RtError."""
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[credential-defer-recheck] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _rt.tolerant_decode_aggregate(f"[credential-defer-recheck] {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", ""))
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _clear_defer(source: str, goal_id: str) -> bool:
    """Clear defer_reason via aspirations.py update-goal.

    Calls aspirations.py directly (not via bash) — same rationale as
    blocker-recheck.py / defer-recheck.py: shell wrappers are unreliable
    from Python on Windows (bash may resolve to WSL).
    """
    result = subprocess.run(
        [sys.executable,
         str(SCRIPT_DIR / "aspirations.py"),
         "--source", source,
         "update-goal",
         goal_id, "defer_reason", "null"],
        capture_output=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Metrics logging
# ---------------------------------------------------------------------------

def _resolve_metrics_log(cli_path: str | None) -> Path | None:
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "credential-defer-recheck-metrics.jsonl"


def _append_metric(path: Path | None, record: dict) -> None:
    if path is None:
        return
    try:
        locked_append_jsonl(path, record)
    except Exception:
        pass  # metrics are advisory; never block the sweep


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-probe human_blocked: defers that name an env-read probe.")
    ap.add_argument("--max-age-hours", type=float, default=2.0,
                    help="Minimum defer age before re-probe (default 2h)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually clear defer_reason (default: dry-run)")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help="Path to JSONL metrics log. Empty string to disable.")
    args = ap.parse_args()

    metrics_path = _resolve_metrics_log(args.metrics_log)
    run_ts = dt.datetime.now().isoformat(timespec="seconds")

    all_goals = _read_goals("world") + _read_goals("agent")

    scanned = 0
    eligible = 0
    skipped_no_key = 0
    skipped_probe_fail = 0
    cleared = 0
    would_clear: list[str] = []
    details: list[dict] = []

    for goal in all_goals:
        if goal.get("status") not in ("pending", "in-progress"):
            continue
        defer_reason = goal.get("defer_reason") or ""
        if not defer_reason.lower().startswith("human_blocked:"):
            continue

        scanned += 1

        # Age gate: skip defers younger than max_age_hours to prevent thrash.
        age = _age_hours(goal.get("defer_reason_set_at"))
        if age is not None and age < args.max_age_hours:
            details.append({
                "goal_id": goal["id"],
                "action": "skipped_too_young",
                "age_hours": round(age, 1),
                "defer_reason": defer_reason[:120],
            })
            continue

        eligible += 1

        # Try to extract an env-var key.
        key = _extract_env_key(defer_reason)
        if key is None:
            skipped_no_key += 1
            details.append({
                "goal_id": goal["id"],
                "action": "skipped_no_key",
                "reason": "no env-read/credential indicator in defer text; human-only defer",
                "defer_reason": defer_reason[:120],
            })
            continue

        # Re-probe the named env key.
        try:
            present = _probe_env_key(key)
        except Exception as e:
            details.append({
                "goal_id": goal["id"],
                "action": "probe_error",
                "key": key,
                "error": str(e),
            })
            continue

        if not present:
            skipped_probe_fail += 1
            details.append({
                "goal_id": goal["id"],
                "action": "skipped_probe_fail",
                "key": key,
                "reason": f"env-read.sh has {key} returned false; defer stays",
            })
            continue

        # Credential is now present → clear the defer.
        if args.apply:
            source = goal.get("_source", "world")
            ok = _clear_defer(source, goal["id"])
            action = "cleared" if ok else "clear_failed"
            if ok:
                cleared += 1
                _append_metric(metrics_path, {
                    "type": "defer_cleared",
                    "goal_id": goal["id"],
                    "key": key,
                    "source": source,
                    "ts": run_ts,
                })
        else:
            would_clear.append(goal["id"])
            action = "would_clear"

        details.append({
            "goal_id": goal["id"],
            "action": action,
            "key": key,
            "defer_reason": defer_reason[:120],
        })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "skipped_no_key": skipped_no_key,
        "skipped_probe_fail": skipped_probe_fail,
        "cleared": cleared,
        "would_clear": would_clear,
        "details": details,
    }

    _append_metric(metrics_path, {
        "type": "run_summary",
        "ts": run_ts,
        "apply": args.apply,
        **{k: result[k] for k in ("scanned", "eligible", "cleared")},
    })

    if args.output == "json":
        print(json.dumps(result))
    else:
        print(f"credential-defer-recheck: scanned={scanned} eligible={eligible} "
              f"skipped_no_key={skipped_no_key} skipped_probe_fail={skipped_probe_fail} "
              f"cleared={cleared}")
        for d in details:
            print(f"  {d['goal_id']}: {d['action']}"
                  + (f" key={d['key']}" if "key" in d else ""))


if __name__ == "__main__":
    main()
