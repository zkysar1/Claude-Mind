#!/usr/bin/env python3
"""_sleep_directive.py -- the ONE owner of the loop's "how to yield on a sleep" block.

Four printers emit it -- idle-tick.sh (through sleep-directive.sh), dry-idle-cycle-cache.py,
dry-spin-guard.py and quiescence-cycle-cache.py -- and they must say the same thing
(guard-2485: the per-site context is exactly the three operands). It is ONE text on every
harness. The loop's yield contract is Claude Code's: a Bash call launched with
run_in_background=true produces a task notification when it exits, so "launch the
registered sleep in the background and end the turn" re-invokes the loop by itself -- and
every harness the framework runs on implements that contract (Claude Code natively; Zak
Code since its ADR-0191, 2026-09-18). A backgrounded interruptible-sleep still polls its
wake signals every second and exits rc=2 the moment one lands, and the notification
re-enters the loop right then, on either harness.

History. From g-357-89 (2026-09-03) to 2026-09-18 this text lived in _harness_caps.py and
was KEYED ON THE HARNESS: a capability table said the vessel could not notify on a
background job's exit (true then), so a second branch launched the sleep with a trailing &
and armed a sized ScheduleWakeup as the re-entry, and a third (g-373-10 leg c) chunked a
foreground sleep under the vessel's 600 s tool cap. Both were the framework asking which
harness ran it -- the one thing the one-contract ruling (loop-terminal-protocol.md §4.1)
forbids -- and both became unnecessary the day the vessel reported the exit. The table,
its MIND_HARNESS_BG_NOTIFY override, the harness-capabilities.sh wrapper and both
branches were deleted with it (loop-terminal-protocol.md §4.2).

env_prefix is load-bearing: interruptible-sleep.sh registers as a Tier-A background job
only when invoked with QUIESCENCE_SLEEP=1 or DRY_SLEEP=1 (guard-1230), and without that
registration stop-hook Gate 2.6 BLOCKs the very turn-end the sleep exists to allow.
"""
from __future__ import annotations

import sys


def sleep_directive(sleep_seconds, agent, env_prefix) -> str:
    """The whole "how to yield" block a sleep-directive printer emits."""
    try:
        total = max(int(sleep_seconds), 0)
    except (TypeError, ValueError):
        total = 0
    cmd = f"MIND_AGENT={agent} {env_prefix} bash core/scripts/interruptible-sleep.sh"
    return (
        "Emit exactly ONE tool call:\n"
        f"  Bash(\"{cmd} {total}\", run_in_background=true)\n"
        "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
    )


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 3:
        print("usage: sleep-directive.sh <sleep_seconds> <agent> <env_prefix>", file=sys.stderr)
        return 2
    sys.stdout.write(sleep_directive(argv[0], argv[1], argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
