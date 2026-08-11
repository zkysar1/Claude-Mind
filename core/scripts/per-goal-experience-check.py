#!/usr/bin/env python3
"""Phase 4.25 PER-GOAL experience-record check — shared by BOTH close paths.

Extracted from recurring-close.sh (the g-115-547 canary, formerly inline at
~L873-928) by g-115-4661 so the non-recurring close path can use the same
enforcement. recurring-close.sh no longer carries its own copy — per guard-2015
the origin must not keep a fork, or this file's later hardening never reaches it.

WHAT THIS CHECKS, AND WHY IT IS NOT experience-staleness-check.sh
----------------------------------------------------------------
`experience-staleness-check.sh` is STORE-level: it warns when the most-recent
entry of ANY kind exceeds a 12h threshold. It has no goal_id join, so a busy
agent whose store is an hour fresh reads clean while individual deep goals close
with no record at all — structurally invisible, by that check's own contract.

This check is PER-GOAL: it asks whether THIS goal has a record within a short
window, and on a miss sets the `force_experience_archival` WM sentinel naming
the goal. aspirations-precheck Phase 0-pre2 consumes the sentinel next iteration
and forces the LLM to retro-compose. The two are complementary and BOTH should
run — the store-level one remains the long-horizon backstop.

Measured coverage that motivated the extraction (echo, cc-03, 2026-08-02, joined
against experience.jsonl + experience-archive.jsonl + experience/*.md across 5
agents): non-recurring completed goals with ANY experience record ran 16-32%,
while recurring goals — the one lane where this check was wired — ran 95%.

MATCHING (do not "simplify" this to goal_id alone)
--------------------------------------------------
Matches canonical `goal_id` OR legacy `source_goal` (g-115-2511): a minority of
store entries carry only `source_goal`, because writer templates drifted by
analogy with the rb/guardrail stores where `source_goal` IS canonical. Dropping
the fallback makes the sentinel FALSE-fire on closes whose record exists.
guard-697 / guard-713 are the write-side half of the same seam: a record written
with only `source_goal` is invisible to `experience-read.sh --goal`.

FAIL-OPEN, LOUDLY
-----------------
Always exits 0 — a check failure must never block a goal close. But degradation
is VISIBLE on stderr rather than swallowed: a silent `|| true` makes the check
undetectable in exactly the scenario it exists for (insight trigger
msg-20260801-171952-zeta-5643, same file family). Callers should ALSO guard the
invocation itself (`|| echo "WARN ..." >&2`), because a missing interpreter or
missing file never reaches this code to report anything.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WINDOW_SECONDS = 30 * 60
TAIL_LINES = 100


def _warn(msg: str) -> None:
    print(f"[per-goal-experience-check] {msg}", file=sys.stderr)


def has_recent_record(exp_path: Path, goal_id: str, window_seconds: int,
                      now: datetime) -> bool:
    """True when exp_path holds an entry for goal_id created inside the window.

    Reads only the tail — recent entries are at the end, and the whole point of
    the window is recency.

    A MISSING file returns False, so the sentinel fires: an agent with no
    experience store has certainly not recorded this goal. A read failure
    RAISES instead, and main() then skips the check entirely (no sentinel) —
    matching the origin block, which exited silently on a store it could not
    read rather than asserting a miss it had not measured. A malformed single
    LINE is skipped, not fatal.
    """
    if not exp_path.exists():
        return False
    lines = exp_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines[-TAIL_LINES:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if goal_id not in (entry.get("goal_id"), entry.get("source_goal")):
            continue
        try:
            created = datetime.fromisoformat(entry.get("created") or "")
        except (ValueError, TypeError):
            continue
        if (now - created).total_seconds() < window_seconds:
            return True
    return False


def build_payload(goal_id: str, trigger: str, original_outcome: str,
                  now: datetime) -> str:
    """The exact 4-key shape aspirations-precheck Phase 0-pre2 already consumes.

    `trigger` names WHICH close path fired, and `original_outcome` carries the
    caller's pre-flip CLI outcome so a consumer can tell "caller asked for deep"
    from "system flipped routine->deep" (g-115-686 / g-115-688). Do not add keys
    here without checking the consumer.
    """
    return json.dumps({
        "triggered_at": now.isoformat(timespec="seconds"),
        "trigger": trigger,
        "goal_id": goal_id,
        "original_outcome": original_outcome,
    })


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="per-goal-experience-check",
        description="Set force_experience_archival when a goal closed with no recent experience record.",
    )
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--trigger", required=True,
                    help="label recorded in the sentinel payload, names the calling close path")
    ap.add_argument("--original-outcome", default="",
                    help="caller's pre-flip CLI outcome (recurring path); empty elsewhere")
    ap.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the verdict and payload; do not write the sentinel")
    args = ap.parse_args()

    goal_id = (args.goal_id or "").strip()
    if not goal_id:
        _warn("empty --goal-id — nothing to check")
        return 0

    # _paths honors MIND_AGENT_DIR / MIND_AGENT, so the agent resolves the
    # same way it does for every other core/scripts consumer (and for the
    # sandboxed tests). Importing by sys.path insert rather than a relative
    # import: this file is run as a script, never imported as a package member.
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import _paths
    except Exception as exc:                        # pragma: no cover - env-specific
        _warn(f"could not import _paths ({exc}) — skipping check for {goal_id}")
        return 0

    agent_dir = getattr(_paths, "AGENT_DIR", None)
    if not agent_dir:
        _warn(f"no AGENT_DIR resolved — skipping check for {goal_id}")
        return 0

    exp_path = Path(agent_dir) / "experience.jsonl"
    now = datetime.now()
    try:
        recent = has_recent_record(exp_path, goal_id, args.window_seconds, now)
    except Exception as exc:
        _warn(f"could not read {exp_path} ({exc}) — skipping check for {goal_id}")
        return 0

    if recent:
        print(f"[per-goal-experience-check] {goal_id}: experience record found "
              f"within {args.window_seconds}s — no sentinel needed", file=sys.stderr)
        return 0

    payload = build_payload(goal_id, args.trigger, args.original_outcome, now)
    if args.dry_run:
        print(payload)
        return 0

    wm_py = SCRIPT_DIR / "wm.py"
    if not wm_py.exists():
        _warn(f"wm.py not found at {wm_py} — sentinel NOT set for {goal_id}")
        return 0
    try:
        proc = subprocess.run(
            [sys.executable, str(wm_py), "set", "force_experience_archival"],
            input=payload, text=True, capture_output=True, timeout=15,
        )
    except Exception as exc:
        _warn(f"wm.py set failed ({exc}) — sentinel NOT set for {goal_id}")
        return 0

    if proc.returncode != 0:
        _warn(f"wm.py set returned rc={proc.returncode} — sentinel NOT set for "
              f"{goal_id}: {(proc.stderr or '').strip()[:300]}")
        return 0

    print(f"[per-goal-experience-check] Phase 4.25 enforcement: deep close on "
          f"{goal_id} with no recent experience entry — set "
          f"force_experience_archival sentinel (trigger={args.trigger})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
