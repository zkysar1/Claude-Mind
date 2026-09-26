#!/usr/bin/env python3
"""Session state engine for <agent>/session/ control files.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.

Manages:
  agent-state      — plain text: RUNNING or IDLE (absence = UNINITIALIZED)
  agent-mode       — plain text: reader, assistant, or autonomous (absence = reader)
  persona-active   — plain text: true or false (absence = unset, defaults to true)
  loop-active      — empty marker file (presence = true)
  stop-loop        — empty marker file (presence = true)
  stop-block-count — plain text integer (absence = 0)
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, SESSIONS_DIRNAME

# AGENT_DIR is None when MIND_AGENT is not set (no-agent mode).
# Session scripts return "NO_AGENT" instead of crashing.
SESSION_DIR = AGENT_DIR / "session" if AGENT_DIR else None

VALID_STATES = {"RUNNING", "IDLE"}
VALID_PERSONA = {"true", "false"}
# DO NOT remove entries — interruptible-sleep.sh polls these by file presence.
# Add new quiescence-wake signals here; writers must use session-signal-set.sh
# so the manifest stays authoritative (reader side is raw [ -f ] in the sleep loop).
VALID_SIGNALS = {
    "loop-active", "stop-loop", "stop-requested", "blocker-cleared", "pq-resolved",
    "board-activity", "email-received", "goal-claim-released",
    # perception-received (): an environment CHANGE envelope reached
    # /observe on a vessel. BLOCKER class — an environment change unblocks work
    # even inside an approved quiescence sleep, which is the whole point: a
    # resident that cannot wake on its world changing is not perceiving it.
    # Written ONLY by the vessel's /observe on kind:'change' (never a heartbeat)
    # and never in assistant mode, where no loop sleeps and nothing reads it
    # (guard-1806). Adding a signal here is a 7-site change, NOT the 3 guard-374
    # names — see the SIGNAL SYNC SITES block in interruptible-sleep.sh.
    "perception-received",
}
# Runtime session modes — values written to session/agent-mode and read by
# session-mode-get/set. Strict triad. NOTE: skill-structure-gate.py declares
# a SUPERSET ({reader, assistant, autonomous, any, internal}) for the
# `minimum_mode:` front-matter field on SKILL.md files; "any" and "internal"
# are framework-control escape hatches that are NEVER valid runtime modes.
# When adding a new runtime mode, update BOTH this set AND the one at
# skill-structure-gate.py:76. Source of truth for runtime modes: this file.
VALID_MODES = {"reader", "assistant", "autonomous"}
DEFAULT_MODE = "reader"


def require_agent():
    """Exit with clear error if no agent is bound to this session."""
    if SESSION_DIR is None:
        print("Error: no agent active (MIND_AGENT not set). Use /start <name> first.", file=sys.stderr)
        sys.exit(1)


def ensure_session_dir():
    """Create <agent>/session/ if it doesn't exist."""
    require_agent()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def read_file(path):
    """Read a plain-text file, return stripped content or None if missing."""
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def write_file(path, value):
    """Write a plain-text value to a file (atomic where possible)."""
    ensure_session_dir()
    p = Path(path)
    # Write to temp then rename for atomicity
    tmp = p.with_suffix(".tmp")
    tmp.write_text(value + "\n", encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Subcommand: state
# ---------------------------------------------------------------------------

def cmd_state_get(args):
    """Read agent-state: prints RUNNING, IDLE, UNINITIALIZED, or NO_AGENT."""
    if SESSION_DIR is None:
        print("NO_AGENT")
        return
    val = read_file(SESSION_DIR / "agent-state")
    if val is None:
        print("UNINITIALIZED")
    else:
        print(val)


def require_runner_sid():
    """RUNNING implies a non-empty running-session-id (the rb-323/guard-403 invariant).

    /start's runner triple-write (running-session-id + latest-session-id + runner-token)
    MUST precede the RUNNING flip: stop-hook.sh routes the runner on running-session-id,
    so a RUNNING agent without it is treated like an observer at turn end — never BLOCKed —
    and its loop dies silently at the first text-only turn end. Measured 2026-08-29 (a
    paged /start --recover on a small model skipped the triple-write, acquired the claim
    and flipped RUNNING; the file was absent until an operator wrote it by hand). The
    skill already HALTs on a non-zero exit here, so refusing puts the model back on the
    missing step instead of booting a runner the stop hook cannot see.
    """
    sid = read_file(SESSION_DIR / "running-session-id")
    if not sid:
        print(
            "REJECTED: state RUNNING requires a non-empty running-session-id "
            f"({SESSION_DIR / 'running-session-id'}). Run /start's runner triple-write first "
            "(running-session-id + latest-session-id + runner-token), then set RUNNING — "
            "the stop hook routes the runner on that file (rb-323/guard-403).",
            file=sys.stderr,
        )
        sys.exit(1)
    env_sid = os.environ.get("MIND_SID", "").strip()
    if env_sid and sid != env_sid:
        print(
            f"REJECTED: running-session-id names {sid[:8]}… but this session is {env_sid[:8]}… — "
            "a stale runner file. Run /start's manifest-clear and the runner triple-write in THIS "
            "session before setting RUNNING.",
            file=sys.stderr,
        )
        sys.exit(1)
    # heartbeat-tick.sh writes the carrier ONLY under the bound session dir
    # (agents/<agent>/sessions/<SID>/, created by /start Step 0's session-binding-write.sh),
    # so check the binding FIRST and name THAT step. Checked in the other order, a skipped
    # Step 0 surfaces as the carrier refusal below, whose remedy (run heartbeat-tick) cannot
    # succeed — and the model's next move is to hand-write the carrier. Measured 2026-08-30
    # (coach, zc-03, small-model /start): W0 skipped, the pre-flip tick wrote nothing, a
    # wrong-shape carrier was hand-written, RUNNING flipped, and the lease then aged 238 s
    # into /boot with no cadence tick until an operator ran the binding by hand.
    if env_sid and not (AGENT_DIR / SESSIONS_DIRNAME / env_sid).is_dir():
        print(
            f"REJECTED: state RUNNING requires this session's bound session dir "
            f"({AGENT_DIR / SESSIONS_DIRNAME / env_sid}). Run /start Step 0 first "
            "(session-binding-write.sh --sid \"$MIND_SID\" --agent <agent> --mode autonomous "
            "--retire-legacy), THEN the pre-flip heartbeat-tick, then set RUNNING — the tick "
            "writes the liveness carrier only under that dir, so nothing else can satisfy "
            "the carrier check below. Do NOT write the carrier by hand.",
            file=sys.stderr,
        )
        sys.exit(1)
    # The liveness carrier is the other half of the same invariant. The tool-call-cadence
    # tick (bash-agent-inject.py, ) keys on session/body-heartbeat-<SID>.json and
    # never creates it; /start's pre-flip heartbeat-tick does. A runner that flips RUNNING
    # without it renews its lease only at iteration boundaries, so a long single-turn /boot
    # ages the claim past the takeover threshold while the reducer is alive and working
    # (measured 2026-08-29: 3214 s of a 3900 s threshold, 11 minutes from a false takeover).
    if env_sid and not (SESSION_DIR / f"body-heartbeat-{env_sid}.json").is_file():
        print(
            f"REJECTED: state RUNNING requires this session's liveness carrier "
            f"({SESSION_DIR / f'body-heartbeat-{env_sid}.json'}). Run /start's pre-flip "
            "heartbeat-tick (it writes the carrier), then set RUNNING — without it the "
            "tool-call-cadence tick never fires and the runner lease starves during /boot.",
            file=sys.stderr,
        )
        sys.exit(1)


def require_autonomous_mode():
    """RUNNING implies agent-mode == autonomous (CLAUDE.md's mode table, script-enforced).

    /start writes the mode (IDLE Step 2, first-boot C8) BEFORE the RUNNING flip, and no
    other ceremony script writes it. A /start carried out by a small model can drop that
    one step with no error anywhere. Measured 2026-09-21 (a served small-model /start on a
    throwaway world): the step block that binds the session and records the mode named 2 of
    its 6 scripts, the next block ran whole, all three runner checks above passed, and the
    agent was RUNNING for 34 minutes with no agent-mode file. Absence reads as `reader`, so
    every consumer that asks for autonomous mode stayed quiet for the whole session (the
    one measured: iteration-close-reminder.py's mode gate, silent at 5 of 5 completed
    closes) and nothing said so. binding.yaml said `mode: autonomous` throughout; the
    consumers read THIS file, not the binding.

    Checked LAST, after the runner checks. The mode setter depends on none of them, so this
    refusal's remedy can always succeed, and the refusals above keep naming their own
    missing step first. Every production caller already satisfies it: both /start paths
    write the mode before the flip, and recovery-yank-reverse.sh only reaches the flip when
    recovery_yank.py's preconditions found agent-mode == autonomous.
    """
    mode = read_file(SESSION_DIR / "agent-mode")
    if mode != "autonomous":
        found = f"'{mode}'" if mode else "absent, which reads as reader"
        print(
            "REJECTED: state RUNNING requires agent-mode 'autonomous' "
            f"({SESSION_DIR / 'agent-mode'} is {found}). Run /start's mode step first "
            "(session-mode-set.sh autonomous), then set RUNNING — a RUNNING agent under any "
            "other mode leaves every autonomous-gated hook silent for the whole session. "
            "Do NOT write agent-mode by hand.",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_state_set(args):
    """Write agent-state after validation."""
    require_agent()
    value = args.value
    if value not in VALID_STATES:
        print(f"ERROR: Invalid state '{value}'. Must be one of: {', '.join(sorted(VALID_STATES))}", file=sys.stderr)
        sys.exit(1)
    if value == "RUNNING":
        require_runner_sid()
        require_autonomous_mode()
    write_file(SESSION_DIR / "agent-state", value)


# ---------------------------------------------------------------------------
# Subcommand: persona
# ---------------------------------------------------------------------------

def cmd_persona_get(args):
    """Read persona-active: prints true, false, unset, or no_agent."""
    if SESSION_DIR is None:
        print("no_agent")
        return
    val = read_file(SESSION_DIR / "persona-active")
    if val is None:
        print("unset")
    else:
        print(val)


def cmd_persona_set(args):
    """Write persona-active after validation."""
    require_agent()
    value = args.value
    if value not in VALID_PERSONA:
        print(f"ERROR: Invalid persona value '{value}'. Must be one of: {', '.join(sorted(VALID_PERSONA))}", file=sys.stderr)
        sys.exit(1)
    write_file(SESSION_DIR / "persona-active", value)


# ---------------------------------------------------------------------------
# Subcommand: mode
# ---------------------------------------------------------------------------

def cmd_mode_get(args):
    """Read agent-mode: prints reader, assistant, autonomous, or reader (default)."""
    if SESSION_DIR is None:
        print("NO_AGENT")
        return
    val = read_file(SESSION_DIR / "agent-mode")
    if val is None:
        print(DEFAULT_MODE)
    else:
        print(val)


# Files whose presence means a sanctioned stop is in flight (). /stop writes
# stop-target-mode FIRST and stop-requested second (guard-158), the script stop gates
# write the same pair, and graceful-stop writes stop-checkpoint.json at entry.
STOP_IN_FLIGHT_MARKERS = ("stop-requested", "stop-target-mode", "stop-checkpoint.json")


def refuse_demotion_while_running(value):
    """Refuse reader|assistant while agent-state is RUNNING and no stop is in flight.

    agent-mode is agent-wide, so a write from ANY session re-modes the live runner.
    Measured twice (g-115-8154): an observer session on a peer deployment (2026-08-28)
    and a hosted conversation session against an autonomous resident (2026-09-25,
    g-335-1459) each ran the IDLE-branch assistant block and flipped agent-mode under a
    RUNNING runner, which then stopped claiming work. start/SKILL.md's RUNNING branch already forbids this write;
    that is prose, and the model improvised past it both times.

    Every legitimate demotion still passes: /stop's and /start's IDLE branches run at
    IDLE, and graceful-stop's D7 runs after it has set IDLE, with stop-checkpoint.json
    still present as a second exemption. autonomous is never refused — /start's
    IDLE->RUNNING flip needs it first (require_autonomous_mode).
    """
    if value == "autonomous" or read_file(SESSION_DIR / "agent-state") != "RUNNING":
        return
    if any((SESSION_DIR / m).exists() for m in STOP_IN_FLIGHT_MARKERS):
        return
    print(
        f"REJECTED: agent-mode '{value}' while agent-state is RUNNING and no stop is in "
        f"flight (none of {', '.join(STOP_IN_FLIGHT_MARKERS)} exists). agent-mode is "
        "agent-wide: writing it demotes the live autonomous runner. A session started "
        "against a RUNNING agent is an OBSERVER and must not write agent-mode, "
        "persona-active or agent-state (start/SKILL.md RUNNING branch). To change the "
        "agent's mode, /stop <agent> first.",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_mode_set(args):
    """Write agent-mode after validation."""
    require_agent()
    value = args.value
    if value not in VALID_MODES:
        print(f"ERROR: Invalid mode '{value}'. Must be one of: {', '.join(sorted(VALID_MODES))}", file=sys.stderr)
        sys.exit(1)
    refuse_demotion_while_running(value)
    write_file(SESSION_DIR / "agent-mode", value)


# ---------------------------------------------------------------------------
# Subcommand: signal
# ---------------------------------------------------------------------------

# Live-stop guard decision (). PURE: no filesystem, no env — so every
# branch is testable without staging an agent dir. Mirrors the shape the three
# sibling stop-gates use (`reducer_self_fence.decide`,
# `loop_exhaustion_fence.decide`): the predicate is script-owned, never
# LLM-discretionary (guard-399 — changing an instruction's FORM does not change
# WHO executes it; only moving it into code does).
CLEAR = "clear"
REFUSE = "refuse"
CLEAR_UNDETERMINED = "clear-undetermined"


#: First line the vessel sidecar writes INTO `stop-requested` when it raises a
#: served run's ending (zakcode.session.framework_stop). The framework's own
#: writers leave the marker empty, so the line is the sidecar's signature.
SIDECAR_RAISE_MARKER = "raised_by: vessel-sidecar"


def live_stop_decision(signal_exists, stop_loop_exists, signal_mtime, started_at, force,
                       raised_by_sidecar=False):
    """Decide whether clearing `stop-requested` is safe.

    REFUSE when the signal is present, nobody has completed the stop
    (`stop-loop` absent), and EITHER the vessel sidecar signed it OR the
    session start is KNOWN and the signal was raised after that start.
    Everything else clears — `CLEAR_UNDETERMINED` clears too, but names
    itself so an un-evaluatable check can never be read as a verdict that the
    signal was stale (guard-6178 shape).

    The sidecar branch does not consult time at all. Measured 2026-09-17 on
    prod vessel debc47de (user's Alien 2 run B): the sidecar raised at
    20:29:29, /start wrote binding.yaml `started_at` at 20:31:43 — two minutes
    of onboarding on a slow model — and Step 2.5 ran this guard at 20:32:09.
    mtime < started_at read as "stale" and the run's only ending was deleted.
    A sidecar raise is by construction from the CURRENT run: the sidecar
    retires its own unconsumed pair at grace expiry (g-373-92) and at its next
    start, so a signed signal that survives to /start was raised now.
    """
    if force or not signal_exists or stop_loop_exists:
        return CLEAR
    if raised_by_sidecar:
        return REFUSE
    if started_at is None:
        return CLEAR_UNDETERMINED
    return REFUSE if signal_mtime > started_at else CLEAR


def _signal_raised_by_sidecar(path):
    """True when the signal file's first line is the sidecar's signature.

    Reads a few bytes only; any read failure is "not signed" — the mtime rule
    then decides, exactly as before the marker existed.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip() == SIDECAR_RAISE_MARKER
    except (OSError, ValueError, TypeError):
        return False


def _session_started_at():
    """Return this session's start time (epoch float) from its binding, or None.

    The binding (`agents/<agent>/sessions/<SID>/binding.yaml`, written by /start)
    carries a semantic `started_at` — preferred over any file mtime, which drifts
    when the binding is rewritten (`--retire-legacy`). None means UNDETERMINED,
    never "old": callers must not read it as an all-clear.
    """
    sid = os.environ.get("MIND_SID", "").strip()
    if not sid or AGENT_DIR is None:
        return None
    binding = AGENT_DIR / SESSIONS_DIRNAME / sid / "binding.yaml"
    try:
        for line in binding.read_text(encoding="utf-8").splitlines():
            if not line.startswith("started_at:"):
                continue
            raw = line.split(":", 1)[1].strip().strip("'\"")
            return datetime.datetime.fromisoformat(raw).timestamp()
    except (OSError, ValueError):
        return None
    return None


def cmd_signal_set(args):
    """Create an empty marker file."""
    require_agent()
    name = args.name
    if name not in VALID_SIGNALS:
        print(f"ERROR: Invalid signal name '{name}'. Must be one of: {', '.join(sorted(VALID_SIGNALS))}", file=sys.stderr)
        sys.exit(1)


    # Guard: stop-loop requires non-RUNNING state.
    # /stop's deferred stop handler sets IDLE first, then sets stop-loop.
    # This prevents the LLM from accidentally setting stop-loop while the loop is active.
    # NOTE: stop-requested has NO guard — it must be settable while RUNNING.
    # The graceful stop mechanism depends on this: /stop sets stop-requested while
    # RUNNING, the stop hook blocks, the loop re-enters, Phase -1.4 handles the stop.
    if name == "stop-loop":
        state = read_file(SESSION_DIR / "agent-state")
        if state == "RUNNING":
            print("REJECTED: Cannot set stop-loop while RUNNING. Use /stop to stop the agent.", file=sys.stderr)
            sys.exit(1)

    ensure_session_dir()
    (SESSION_DIR / name).touch()


def cmd_signal_clear(args):
    """Remove a marker file if it exists."""
    require_agent()
    name = args.name
    if name not in VALID_SIGNALS:
        print(f"ERROR: Invalid signal name '{name}'. Must be one of: {', '.join(sorted(VALID_SIGNALS))}", file=sys.stderr)
        sys.exit(1)

    # Guard: NEVER destroy a stop-requested that was raised during THIS session
    # and that nobody has acted on yet ().
    #
    # /start Step 2.5 clears stop-requested unconditionally, on the stated
    # premise that "state is already IDLE, so no loop polling could be
    # interrupted; clearing is purely hygienic". That premise held while /stop
    # was the ONLY writer. It is now false: stop-hook-compliance.md authorizes
    # four programmatic writers, and the newest — the vessel sidecar — raises the
    # signal from OUTSIDE the session at a moment it chooses (a served run's
    # duration cap). On a vessel that raise can land while /start is still
    # onboarding, and this unlink would then delete the run's only ending.
    #
    # Measured 2026-09-13 on i-022f74084032d8c56: the sidecar raised at 16:49:25
    # and /start ran this clear at 16:45:35 — 3m50s apart, so the race did not
    # fire. It is not hypothetical, and it TIGHTENS as the reserve grows:
    # turn_deadline = T0 + SOFT_S - RESERVE, so a LARGER consolidation reserve
    # moves the raise EARLIER, toward this clear. At RESERVE≈590 on that run the
    # raise would have preceded the clear and the stop would have been erased.
    #
    # Two conditions, both required, so the legitimate clears still pass:
    #   * the signal is NEWER than this session's start -> it was raised now,
    #     not left behind by a partial /stop in a previous session; and
    #   * `stop-loop` is ABSENT -> nobody has completed the stop yet.
    # aspirations-graceful-stop sets stop-loop at D2 and clears this signal at
    # D3, in that order, so its clear is always permitted. A stale signal from
    # an earlier session is older than started_at, so /start's hygiene still
    # works. UNDETERMINED start time fails toward TODAY'S behaviour (clear) but
    # says so out loud — an un-evaluatable check must never read as an
    # all-clear (guard-6178 shape).
    if name == "stop-requested":
        target = SESSION_DIR / name
        verdict = live_stop_decision(
            signal_exists=target.exists(),
            stop_loop_exists=(SESSION_DIR / "stop-loop").exists(),
            signal_mtime=(target.stat().st_mtime if target.exists() else None),
            started_at=_session_started_at(),
            force=args.force,
            raised_by_sidecar=_signal_raised_by_sidecar(target),
        )
        if verdict == CLEAR_UNDETERMINED:
            print(
                "[session-signal-clear] WARNING: live-stop guard COULD NOT EVALUATE "
                "(no MIND_SID, or binding.yaml unreadable / carries no started_at) — "
                "clearing anyway. This is NOT a verdict that the signal is stale.",
                file=sys.stderr,
            )
        elif verdict == REFUSE:
            print(
                "REJECTED: stop-requested was raised AFTER this session started "
                "(or carries the vessel sidecar's signature, which never reads as "
                "stale) and stop-loop is not set, so the stop has not been handled "
                "yet — clearing it would silently discard a live stop (g-373-16). "
                "Handle the stop (Phase -1.4 / /stop sets stop-loop, then clears), "
                "or pass --force to override deliberately.",
                file=sys.stderr,
            )
            sys.exit(1)

    (SESSION_DIR / name).unlink(missing_ok=True)


def cmd_signal_exists(args):
    """Check if a signal marker exists. Exit 0 if yes, exit 1 if no."""
    require_agent()
    name = args.name
    if name not in VALID_SIGNALS:
        print(f"ERROR: Invalid signal name '{name}'. Must be one of: {', '.join(sorted(VALID_SIGNALS))}", file=sys.stderr)
        sys.exit(2)
    if (SESSION_DIR / name).exists():
        sys.exit(0)
    else:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: counter (stop-block-count)
# ---------------------------------------------------------------------------

COUNTER_FILE = "stop-block-count"


def cmd_counter_get(args):
    """Read stop-block-count: prints integer (0 if missing)."""
    require_agent()
    val = read_file(SESSION_DIR / COUNTER_FILE)
    if val is None:
        print("0")
    else:
        # No fallback — corrupt file crashes here, surfacing the problem.
        # Downstream (stop-hook.sh) treats script failure as allow-stop (fail-open).
        print(int(val))


def cmd_counter_increment(args):
    """Atomic read + increment + write. Prints new value."""
    require_agent()
    val = read_file(SESSION_DIR / COUNTER_FILE)
    current = 0 if val is None else int(val)
    new_val = current + 1
    write_file(SESSION_DIR / COUNTER_FILE, str(new_val))
    print(new_val)


def cmd_counter_clear(args):
    """Delete the stop-block-count file."""
    require_agent()
    (SESSION_DIR / COUNTER_FILE).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Session state management engine")
    sub = parser.add_subparsers(dest="group", required=True)

    # --- state ---
    state_parser = sub.add_parser("state", help="Agent state management")
    state_sub = state_parser.add_subparsers(dest="action", required=True)

    state_sub.add_parser("get", help="Read agent state")
    state_set = state_sub.add_parser("set", help="Set agent state")
    state_set.add_argument("value", help="RUNNING or IDLE")

    # --- persona ---
    persona_parser = sub.add_parser("persona", help="Persona state management")
    persona_sub = persona_parser.add_subparsers(dest="action", required=True)

    persona_sub.add_parser("get", help="Read persona state")
    persona_set = persona_sub.add_parser("set", help="Set persona state")
    persona_set.add_argument("value", help="true or false")

    # --- mode ---
    mode_parser = sub.add_parser("mode", help="Agent mode management")
    mode_sub = mode_parser.add_subparsers(dest="action", required=True)

    mode_sub.add_parser("get", help="Read agent mode")
    mode_set = mode_sub.add_parser("set", help="Set agent mode")
    mode_set.add_argument("value", help="reader, assistant, or autonomous")

    # --- signal ---
    signal_parser = sub.add_parser("signal", help="Signal file management")
    signal_sub = signal_parser.add_subparsers(dest="action", required=True)

    sig_set = signal_sub.add_parser("set", help="Create signal marker")
    sig_set.add_argument("name", help="Signal name: loop-active or stop-loop")

    sig_clear = signal_sub.add_parser("clear", help="Remove signal marker")
    sig_clear.add_argument("name", help="Signal name: loop-active or stop-loop")
    sig_clear.add_argument(
        "--force", action="store_true",
        help="Clear stop-requested even when it was raised during this session and "
             "is unhandled (bypasses the live-stop guard; g-373-16).")

    sig_exists = signal_sub.add_parser("exists", help="Check if signal exists")
    sig_exists.add_argument("name", help="Signal name: loop-active or stop-loop")

    # --- counter ---
    counter_parser = sub.add_parser("counter", help="Stop-block counter management")
    counter_sub = counter_parser.add_subparsers(dest="action", required=True)

    counter_sub.add_parser("get", help="Read counter value")
    counter_sub.add_parser("increment", help="Increment counter")
    counter_sub.add_parser("clear", help="Delete counter file")

    return parser


DISPATCH = {
    ("state", "get"): cmd_state_get,
    ("state", "set"): cmd_state_set,
    ("persona", "get"): cmd_persona_get,
    ("persona", "set"): cmd_persona_set,
    ("mode", "get"): cmd_mode_get,
    ("mode", "set"): cmd_mode_set,
    ("signal", "set"): cmd_signal_set,
    ("signal", "clear"): cmd_signal_clear,
    ("signal", "exists"): cmd_signal_exists,
    ("counter", "get"): cmd_counter_get,
    ("counter", "increment"): cmd_counter_increment,
    ("counter", "clear"): cmd_counter_clear,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    key = (args.group, args.action)
    fn = DISPATCH.get(key)
    if fn is None:
        parser.error(f"Unknown command: {args.group} {args.action}")
    fn(args)


if __name__ == "__main__":
    main()
