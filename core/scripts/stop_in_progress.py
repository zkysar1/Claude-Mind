#!/usr/bin/env python3
"""stop-in-progress -- is THIS session's graceful stop still unfinished? ()

WHY. Measured 2026-09-24 on a DEV vessel (the g-373-128 outcome-3 run): the mind
ran graceful-stop D1-D3 and was part-way through D4 (consolidation) when it
called the deadman sentinel. schedule-wakeup-gate refused the arm, correctly --
D1 had set IDLE -- but its text said "Nothing. You are IDLE: the loop is
stopped ... and this turn ends normally." The mind believed it: it wrote that
the handoff "was not completed before the stop state took effect", declared the
stop complete, and ended the turn. stop-hook Gate 1 ALLOWs every non-RUNNING
turn-end, so nothing held the turn open, and Step 9 (the handoff) and D4.5-D7
never ran. agent-state IDLE is a MODE signal, not a completion signal (rb-10943),
and what a harness tells a model must be TRUE FOR THIS TURN (rb-11311). Both
surfaces were wrong in the one window between D1 and D7.

THE PREDICATE. A graceful stop is unfinished, for session SID, when ALL hold:
  1. agent-state is readable and is not RUNNING    -- D1 has run
  2. stop-target-mode exists                       -- D7 has not (it deletes it)
  3. stop-requested does not exist                 -- D3 consumed the request
  4. agent-mode is autonomous                      -- D7's mode flip has not run
  5. SID is the stopping session: running-session-id, or latest-session-id once
     D6 has deleted the former
Each clause excludes a named look-alike:
  (1) a RUNNING agent with a stop pending: the ordinary BLOCK path owns that
      turn-end already.
  (3) an unconsumed request on an agent no stop handler is running for -- e.g.
      the vessel sidecar raising its stop on an IDLE assistant-mode mind. Nothing
      is running a stop there, so there is nothing to continue.
  (4) a /stop typed from IDLE mid-stop (its IDLE branch sets the mode itself and
      never deletes stop-target-mode), and every post-D7 state.
  (5) observers and worker Bodies. Only the runner writes these two files (the
      /start observer and worker paths are forbidden to), so no other session's
      SID can match, and a turn-end this check did not positively attribute to
      the stopping session is never held.
stop-checkpoint.json is deliberately NOT a clause: GS-0 writes it in prose, and
the measured run skipped that write, so a predicate keyed on it would have
missed the one case it exists for.
Sources: the D-step effects are .claude/skills/aspirations-graceful-stop/SKILL.md
(GS-0, D1, D3, D6, D7); the IDLE branch is .claude/skills/stop/SKILL.md; the
worker path is .claude/skills/start/SKILL.md step W0; the skipped GS-0 write was
measured on g-373-128.

FAIL-SAFE. Every unreadable input makes the predicate FALSE, which is today's
behaviour: the turn-end is allowed and the gate's text is unchanged. Holding a
turn open is the direction a mistaken read must never push.

BOUNDED (rb-11273). The hook runs this check AHEAD of Gate 0, so every later
bound sits behind it; the veto therefore carries its own. It BLOCKs at most
MAX_BLOCKS times per stop, counted from the hook's own log since stop-target-mode
was written (the stop request). The next turn-end is allowed and logged, so a
stop that cannot finish never wedges a session. Residual: the hook rotates its
log at 500 KB down to the last 1000 lines, and a rotation landing between two
blocks can drop the earlier ones from the count -- extra blocks, never an
unbounded run, since every rotation needs another 500 KB of box-wide turn-ends.

CONSUMERS -- one predicate, two surfaces (rb-11636):
  - stop-hook.sh Gate 0-stop runs this file as a script (`--session-dir --sid
    --log`); stdout line 1 is `pass <why>`, `block n=<k>/<cap>` or
    `cap n=<k>/<cap>`, and line 2 is the decision payload on `block` only.
  - schedule-wakeup-gate.py calls arm_refusal_for() when it refuses the deadman
    sentinel from IDLE, so the refusal names the continuation instead of
    "this turn ends normally".
Neither reads an agent-name env var: both pass the session dir and the SID from
the hook payload, so the fleet's MIND_* shape and a vessel's renamed shape
behave identically.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

GATE_NAME = "stop-unfinished"
MAX_BLOCKS = 3
TARGET_MODE_NAME = "stop-target-mode"

_HANDOFF_OWED = {
    "absent": "there is no handoff.yaml",
    "stale": "the handoff.yaml on disk predates this stop",
    "invalid": "handoff.yaml is empty or not a YAML mapping",
    "unverifiable": "nothing dates when this stop began",
}
_RESUME = ("If the /aspirations-graceful-stop steps are no longer in your context, "
           "invoke Skill(aspirations-graceful-stop) with args='--resume': it restarts "
           "at GS-0, and every step is safe to repeat.")
_DONE = ("The stop is finished only when D7 has printed 'Stop verified' and D7.1 "
         "has run.")


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def read_evidence(session_dir: Path) -> Dict[str, Any]:
    """The raw facts the predicate reads, from the agent's session/ dir."""
    try:
        tm_mtime = (session_dir / TARGET_MODE_NAME).stat().st_mtime
    except OSError:
        tm_mtime = None
    return {
        "state": _read(session_dir / "agent-state"),
        "mode": _read(session_dir / "agent-mode"),
        "target_mode_mtime": tm_mtime,
        "stop_requested": (session_dir / "stop-requested").exists(),
        "runner_sid": _read(session_dir / "running-session-id") or "",
        "latest_sid": _read(session_dir / "latest-session-id") or "",
    }


def classify(ev: Dict[str, Any], sid: str) -> Tuple[bool, str]:
    """Pure. (True, 'unfinished') or (False, <which clause failed>)."""
    if not sid:
        return False, "no-sid"
    if ev.get("target_mode_mtime") is None:
        return False, "no-stop"
    state = ev.get("state")
    if not state:
        return False, "state-unreadable"
    if state == "RUNNING":
        return False, "running"
    if ev.get("stop_requested"):
        return False, "unconsumed"
    if ev.get("mode") != "autonomous":
        return False, "mode-set"
    stopping = ev.get("runner_sid") or ev.get("latest_sid")
    if not stopping:
        return False, "no-stopping-sid"
    if sid != stopping:
        return False, "other-session"
    return True, "unfinished"


def count_blocks(log_text: str, sid: str, since_epoch: float) -> int:
    """Pure. This gate's BLOCK lines for `sid` written at or after `since_epoch`.

    The log stamps whole seconds, so the reference is floored: a line in the
    same second as the stop request counts, which errs toward the cap (allow).
    """
    needle = f" BLOCK gate={GATE_NAME} sid={sid} "
    floor = int(since_epoch)
    n = 0
    for line in log_text.splitlines():
        if needle not in line:
            continue
        try:
            when = dt.datetime.fromisoformat(line.split(" ", 1)[0]).timestamp()
        except ValueError:
            continue
        if when >= floor:
            n += 1
    return n


def decide(unfinished: bool, prior_blocks: int, cap: int = MAX_BLOCKS) -> str:
    """Pure. 'pass', 'block', or 'cap' (unfinished, but the bound is spent)."""
    if not unfinished:
        return "pass"
    if prior_blocks >= cap:
        return "cap"
    return "block"


def handoff_status(session_dir: Path) -> str:
    """stop-handoff-check's own verdict for THIS stop, or 'unknown'.

    Reused rather than re-derived, so "a handoff this stop wrote" means exactly
    what D7's check will enforce.
    """
    try:
        from stop_handoff_check import (  # type: ignore
            decide as handoff_decide, inspect_handoff, resolve_reference)
        return handoff_decide(inspect_handoff(session_dir),
                              resolve_reference(session_dir), None)["verdict"]
    except Exception:
        return "unknown"


def _facts(ev: Dict[str, Any], handoff: str) -> str:
    d6 = ("D6 has not run yet (running-session-id is still present)"
          if ev.get("runner_sid") else
          "D6 has run (running-session-id is gone)")
    text = (f"agent-state is {ev.get('state')} only because D1 set it; "
            "stop-target-mode is still present and agent-mode is still autonomous, "
            f"so D7 has not run. {d6}.")
    if handoff == "fresh":
        text += (" handoff.yaml was written during this stop, so D7's "
                 "stop-handoff-check will pass.")
    elif handoff in _HANDOFF_OWED:
        text += (f" No handoff has been written during this stop "
                 f"({_HANDOFF_OWED[handoff]}): consolidation Step 9 writes it, and "
                 "D7's stop-handoff-check refuses until it exists.")
    return text


def _remaining(ev: Dict[str, Any], handoff: str) -> str:
    if ev.get("runner_sid"):
        return ("the rest of D4 (consolidation, through its Step 9 continuation "
                "handoff), then D4.5, D5, D6 through D6.8, D7, D7.05 and D7.1")
    tail = "whatever of D6.5 through D6.8 has not run, then D7, D7.05 and D7.1"
    if handoff == "fresh":
        return tail
    return ("the continuation handoff (consolidation Step 9 -- D7's refusal prints "
            "it verbatim), then " + tail)


def block_reason(ev: Dict[str, Any], handoff: str, n: int, cap: int) -> str:
    return (f"GRACEFUL STOP NOT FINISHED -- this turn may not end yet (block {n} of "
            f"{cap}). " + _facts(ev, handoff) +
            " Your FIRST action MUST be to continue the stop in this turn: "
            f"{_remaining(ev, handoff)}. " + _RESUME +
            " Do NOT end the turn with a summary. " + _DONE +
            f" After {cap} blocks this hook lets the turn end and logs the stop "
            "as unfinished.")


def arm_refusal(ev: Dict[str, Any], handoff: str) -> str:
    return (
        "ScheduleWakeup re-arm rejected: this is the autonomous-loop deadman "
        "sentinel, and agent-state is not RUNNING -- there is no loop under the "
        "net to resurrect.\n\n"
        "Do NOT end the turn, though: your graceful stop is NOT finished. "
        + _facts(ev, handoff) + "\n\n"
        "What to do instead:\n"
        "  - Leave the net alone: there is no loop for it to protect, and D7.05 "
        "stands any leftover net down.\n"
        f"  - Continue the stop now, in this turn: {_remaining(ev, handoff)}.\n"
        f"  - {_RESUME}\n"
        f"  - {_DONE}\n\n"
        "See .claude/skills/aspirations-graceful-stop/SKILL.md (Phase GS-2)."
    )


def arm_refusal_for(session_dir: Path, sid: str) -> Optional[str]:
    """The continuation refusal when THIS session's stop is unfinished, else None."""
    ev = read_evidence(session_dir)
    if not classify(ev, sid)[0]:
        return None
    return arm_refusal(ev, handoff_status(session_dir))


def hook_verdict(session_dir: Path, sid: str, log_path: Optional[Path],
                 cap: int = MAX_BLOCKS) -> Tuple[str, Optional[str]]:
    """(stdout line 1, decision payload or None) for stop-hook Gate 0-stop."""
    ev = read_evidence(session_dir)
    unfinished, why = classify(ev, sid)
    if not unfinished:
        return f"pass {why}", None
    prior = 0
    if log_path is not None:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        prior = count_blocks(text, sid, ev["target_mode_mtime"])
    if decide(True, prior, cap) == "cap":
        return f"cap n={prior}/{cap}", None
    n = prior + 1
    payload = {"decision": "block",
               "reason": block_reason(ev, handoff_status(session_dir), n, cap)}
    return f"block n={n}/{cap}", json.dumps(payload)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session-dir", required=True,
                    help="the agent's session/ dir (agent-wide, not sessions/<SID>)")
    ap.add_argument("--sid", required=True, help="the hook payload's session_id")
    ap.add_argument("--log", default=None, help="stop-hook.log, for the bound")
    args = ap.parse_args(argv)
    try:
        first, payload = hook_verdict(Path(args.session_dir), args.sid.strip(),
                                      Path(args.log) if args.log else None)
    except Exception as e:  # noqa: BLE001 -- fail open: never hold a turn on a fault
        first, payload = f"pass error:{type(e).__name__}", None
    print(first)
    if payload:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
