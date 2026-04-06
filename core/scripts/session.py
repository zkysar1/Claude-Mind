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
import sys
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR

# AGENT_DIR is None when AYOAI_AGENT is not set (no-agent mode).
# Session scripts return "NO_AGENT" instead of crashing.
SESSION_DIR = AGENT_DIR / "session" if AGENT_DIR else None

VALID_STATES = {"RUNNING", "IDLE"}
VALID_PERSONA = {"true", "false"}
VALID_SIGNALS = {"loop-active", "stop-loop", "stop-requested"}
VALID_MODES = {"reader", "assistant", "autonomous"}
DEFAULT_MODE = "reader"


def require_agent():
    """Exit with clear error if no agent is bound to this session."""
    if SESSION_DIR is None:
        print("Error: no agent active (AYOAI_AGENT not set). Use /start <name> first.", file=sys.stderr)
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


def cmd_state_set(args):
    """Write agent-state after validation."""
    require_agent()
    value = args.value
    if value not in VALID_STATES:
        print(f"ERROR: Invalid state '{value}'. Must be one of: {', '.join(sorted(VALID_STATES))}", file=sys.stderr)
        sys.exit(1)
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


def cmd_mode_set(args):
    """Write agent-mode after validation."""
    require_agent()
    value = args.value
    if value not in VALID_MODES:
        print(f"ERROR: Invalid mode '{value}'. Must be one of: {', '.join(sorted(VALID_MODES))}", file=sys.stderr)
        sys.exit(1)
    write_file(SESSION_DIR / "agent-mode", value)


# ---------------------------------------------------------------------------
# Subcommand: signal
# ---------------------------------------------------------------------------

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
