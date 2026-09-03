#!/usr/bin/env python3
"""_harness_caps.py -- what the hosting harness can do for the loop ().

The loop's yield contract was written against ONE harness fact: a Bash call
launched with run_in_background=true produces a task notification when it
exits, so "launch the registered sleep and end the turn" re-invokes the loop
by itself. That fact is Claude Code's. The framework now also runs under a
harness whose bash tool has no background-run parameter and never notifies on
an &-backgrounded job's exit (measured on a downstream deployment 2026-09-03
03:04-03:07Z: five launch attempts, three sleep processes, then the generic
deadman net re-entered after ~2 minutes of a 30-minute sleep). On such a
harness the ONLY re-entry primitive is the timed wake-up (rb-9668), so the
terminal sequence must change shape: launch the sleep once, arm a wake-up
sized to it, end the turn.

This module answers "which harness am I under, and can it notify?" from the
environment alone -- pure, no I/O -- so every consumer (all-blocked B7.2, the
idle-tick / cycle-cache directive printers, tests) reads ONE answer.

Detection mirrors _runtime.sh::rt_judge_provenance and MUST stay in sync with
it: CLAUDECODE set -> claude-code; ZAKCODE_MODEL or ZAKCODE_SESSION set ->
zakcode; else unknown.

Why unknown -> background_job_notify=False (the fail-safe direction): a spare
wake-up on a notifying harness is a replace-slot net that the next iteration
overwrites (harmless); a missing wake-up on a non-notifying harness is a dead
loop until a human notices. MIND_HARNESS_BG_NOTIFY=1|0 overrides the table for
a harness this file does not know yet.
"""
from __future__ import annotations

import json
import os
import sys

# Capability table. Add a row when a new harness is measured -- never guess.
KNOWN = {
    "claude-code": {"background_job_notify": True},
    "zakcode": {"background_job_notify": False},
    "unknown": {"background_job_notify": False},
}

OVERRIDE_ENV = "MIND_HARNESS_BG_NOTIFY"

# ScheduleWakeup's runtime clamp is [60, 3600] (rb-9668 mirrors it on zakcode).
WAKE_CLAMP_S = 3600
WAKE_MARGIN_S = 60


def detect_harness(env=None) -> str:
    """Name the hosting harness from env markers. Precedence matches
    _runtime.sh: a Claude Code marker wins over a zakcode one."""
    env = os.environ if env is None else env
    if env.get("CLAUDECODE"):
        return "claude-code"
    if env.get("ZAKCODE_MODEL") or env.get("ZAKCODE_SESSION"):
        return "zakcode"
    return "unknown"


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _falsy(v) -> bool:
    return str(v).strip().lower() in ("0", "false", "no", "off")


def capabilities(env=None) -> dict:
    """{'harness': <name>, 'background_job_notify': bool}. The override env
    var wins over the table; an unparseable override is ignored."""
    env = os.environ if env is None else env
    name = detect_harness(env)
    caps = dict(KNOWN[name])
    override = env.get(OVERRIDE_ENV, "")
    if override != "":
        if _truthy(override):
            caps["background_job_notify"] = True
        elif _falsy(override):
            caps["background_job_notify"] = False
    return {"harness": name, **caps}


def background_job_notify(env=None) -> bool:
    return bool(capabilities(env)["background_job_notify"])


def wake_delay_seconds(sleep_seconds, margin=WAKE_MARGIN_S, clamp=WAKE_CLAMP_S) -> int:
    """Delay for the wake-up that re-enters the loop after a registered sleep
    on a no-notify harness: the sleep plus a margin, never past the runtime
    clamp. A remainder beyond the clamp is re-slept by the Phase -0.5e fast
    paths on re-entry (which is why B7 must have written blocked_sleep_until)."""
    try:
        s = int(sleep_seconds)
    except (TypeError, ValueError):
        s = 0
    return int(min(max(s, 0) + margin, clamp))


def no_notify_hint(sleep_seconds, env=None) -> str:
    """The extra directive lines every sleep-directive printer appends on a
    no-notify harness (empty string on a notifying one). ONE text, so the
    idle-tick, the two cycle caches and B7.2 all say the same thing."""
    if background_job_notify(env):
        return ""
    delay = wake_delay_seconds(sleep_seconds)
    return (
        "THIS HARNESS CANNOT NOTIFY ON BACKGROUND-JOB EXIT (harness-capabilities.sh): "
        "launch the sleep ONCE with a trailing & (the tool may time out on that call -- "
        "the process survives; a repeat launch JOINS it, never spawns another), then arm "
        f"ScheduleWakeup(prompt=\"<<autonomous-loop-dynamic>>\", delaySeconds={delay}) as the "
        "TERMINAL call and END THE TURN -- no Skill(aspirations). The wake IS the re-entry "
        "here (g-357-89, rb-9668).\n"
    )


def _fmt(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    caps = capabilities()
    if argv[:1] == ["--get"]:
        key = argv[1] if len(argv) > 1 else ""
        if key not in caps:
            print(f"unknown capability '{key}'; known: {', '.join(caps)}", file=sys.stderr)
            return 2
        print(_fmt(caps[key]))
        return 0
    if argv[:1] == ["--json"]:
        print(json.dumps(caps))
        return 0
    if argv[:1] == ["--hint"]:
        # Empty output on a notifying harness; the appended directive lines otherwise.
        sys.stdout.write(no_notify_hint(argv[1] if len(argv) > 1 else 0))
        return 0
    if argv:
        print("usage: harness-capabilities.sh [--get <capability> | --json | --hint <sleep_seconds>]", file=sys.stderr)
        return 2
    print(" ".join(f"{k}={_fmt(v)}" for k, v in caps.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
