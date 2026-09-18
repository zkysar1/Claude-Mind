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
#
# max_foreground_sleep_seconds ( half c): the longest single FOREGROUND
# sleep this harness's bash tool will carry. None = no ceiling worth chunking
# for. A capped harness must be handed the sleep in chunks no larger than this,
# because the alternative -- backgrounding it and waking on a timer -- DETACHES
# the 1s signal poll from the turn, and that is precisely what makes the
# perception-received wake signal (half a) useless on a vessel: the loop would
# wake on the clock rather than on the world changing, which is the whole point
# of the signal. Chunking keeps the poll INSIDE the turn, so the sleep returns
# rc=2 the moment a signal lands and the iteration boundary happens immediately.
#   claude-code: None -- its bash tool takes run_in_background and notifies on
#                exit, so the loop never needs a long foreground sleep.
#   zakcode:     600  -- MEASURED, not guessed: the served sidecar's bash tool
#                has no background flag, a 60 s default and a 600 s max
#                (tools/builtins/bash.py:25-26, 752-755).
#   unknown:     600  -- conservative in the SAFE direction. Chunking a harness
#                that did not need it costs extra iteration boundaries and
#                nothing else; NOT chunking one that did means the tool kills
#                the sleep mid-call. Same fail-safe direction as this row's
#                background_job_notify: False.
KNOWN = {
    "claude-code": {"background_job_notify": True, "max_foreground_sleep_seconds": None},
    "zakcode": {"background_job_notify": False, "max_foreground_sleep_seconds": 600},
    "unknown": {"background_job_notify": False, "max_foreground_sleep_seconds": 600},
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


def max_foreground_sleep_seconds(env=None):
    """The harness's foreground-sleep ceiling, or None when uncapped."""
    return capabilities(env).get("max_foreground_sleep_seconds")


def foreground_chunk_plan(sleep_seconds, env=None):
    """(chunk_seconds, n_chunks) for a capped harness, else None.

    None means "ask for the whole thing in one call" -- an uncapped harness, or
    a sleep already within the ceiling. Never returns a plan with n_chunks < 2,
    so a caller can treat None and a 1-chunk plan as the same instruction."""
    cap = max_foreground_sleep_seconds(env)
    try:
        s = int(sleep_seconds)
    except (TypeError, ValueError):
        s = 0
    if not cap or s <= int(cap):
        return None
    cap = int(cap)
    return (cap, -(-s // cap))  # ceil-div: the last chunk is the short one


def foreground_chunk_hint(sleep_seconds, env=None) -> str:
    """The chunking directive, keyed on the CEILING and nothing else.

    Deliberately NOT keyed on background_job_notify. The two capabilities are
    independent and only happen to coincide on the harnesses measured so far
    (zakcode is both no-notify AND capped), so keying this on notify would make
    a future notifying-but-capped harness silently unchunked -- the failure that
    is invisible, because a truncated sleep looks like an early wake. It is
    COMPOSED into no_notify_hint below purely for REACH (that is the one text
    all four printers already call); the predicate stays the cap."""
    plan = foreground_chunk_plan(sleep_seconds, env)
    if plan is None:
        return ""
    chunk, n = plan
    return (
        f"THIS HARNESS CAPS A FOREGROUND SLEEP AT {chunk}s (harness-capabilities.sh "
        f"--get max_foreground_sleep_seconds): do NOT ask for {int(sleep_seconds)}s in one "
        f"call. Sleep in FOREGROUND chunks of {chunk}s ({n} of them, the last one short), "
        "re-checking the loop's signal state between chunks. Foreground is the point: a "
        "backgrounded sleep detaches the 1s signal poll from the turn, so the loop would "
        "wake on the clock instead of on a wake signal -- which would make perception-received "
        "inert on a vessel (g-373-10).\n"
    )


def _wake_arm_phrase(sleep_seconds, env=None) -> str:
    """The 'arm the sized wake-up and end the turn' sentence, ONE owner.

    Extracted so no_notify_hint and sleep_directive cannot drift on the
    re-entry instruction -- they need the SAME sentence and reach it from
    opposite branches (backgrounded launch vs foreground chunk)."""
    delay = wake_delay_seconds(sleep_seconds)
    return (
        f"arm ScheduleWakeup(prompt=\"<<autonomous-loop-dynamic>>\", delaySeconds={delay}) as the "
        "TERMINAL call and END THE TURN -- no Skill(aspirations). The wake IS the re-entry "
        "here (g-357-89, rb-9668)."
    )


def no_notify_hint(sleep_seconds, env=None) -> str:
    """The extra directive lines every sleep-directive printer appends on a
    no-notify harness (empty string on a notifying one). ONE text, so the
    idle-tick, the two cycle caches and B7.2 all say the same thing.

    Also carries the foreground-chunk directive when the harness caps one, so
    the four existing call sites pick it up with no caller change. A notifying
    harness that is nonetheless capped still gets the chunk half."""
    chunk = foreground_chunk_hint(sleep_seconds, env)
    if background_job_notify(env):
        return chunk
    return chunk + (
        "THIS HARNESS CANNOT NOTIFY ON BACKGROUND-JOB EXIT (harness-capabilities.sh): "
        "launch the sleep ONCE with a trailing & (the tool may time out on that call -- "
        "the process survives; a repeat launch JOINS it, never spawns another), then "
        + _wake_arm_phrase(sleep_seconds, env) + "\n"
    )


def sleep_directive(sleep_seconds, agent, env_prefix, env=None) -> str:
    """The whole 'how to yield' block a sleep-directive printer emits, keyed on
    the harness (g-373-10 leg c). ONE owner for the PRIMARY call line.

    WHY THIS EXISTS. Each of the four printers (idle-tick.sh, the two
    cycle-cache modules, dry-spin-guard) carried its OWN hardcoded copy of that
    line, written against Claude Code -- one BACKGROUNDED call at the FULL
    duration, re-entered by Skill(aspirations) on the harness notification --
    and then appended no_notify_hint(), which on a capped no-notify harness says
    the OPPOSITE on all three axes: chunked not whole, FOREGROUND not
    backgrounded, ScheduleWakeup-and-end-the-turn not Skill(aspirations). Every
    capped harness in KNOWN is also a no-notify one (zakcode and unknown both),
    so that was not an edge case -- it was the whole capped population.

    A self-contradicting directive is not a conservative fallback. The model
    cannot tell which half is authoritative, and obeying BOTH produces
    `run_in_background=true` on a command ending in `&`, which double-detaches
    and forfeits the completion notification (guard-3892) -- so the turn ends
    with nothing tracking the sleep at all.

    THE RULING, where the two halves genuinely conflict: FOREGROUND CHUNKING
    WINS, because it is the only shape that keeps interruptible-sleep.sh's 1s
    signal poll inside the turn, and that poll is the entire reason half (a)
    added perception-received as a wake signal -- a backgrounded sleep wakes on
    the clock instead of on the world changing, which this module's own
    capability table calls "useless on a vessel". The wake-up arm is retained as
    the TURN-END net (a foreground chunk returns control by itself, so it needs
    no notification to reach the next chunk; the arm is for when the turn must
    stop before the total is spent, and the printers recompute the remainder on
    re-entry).

    env_prefix is load-bearing and must appear on EVERY chunk:
    interruptible-sleep.sh registers as a Tier-A background job only when
    invoked with QUIESCENCE_SLEEP=1 or DRY_SLEEP=1 (guard-1230), and without
    that registration stop-hook Gate 2.6 BLOCKs the very turn-end the sleep
    exists to allow.

    The uncapped branch is byte-identical to what the four sites printed before,
    so a notifying, uncapped harness (claude-code) sees no change at all."""
    try:
        total = max(int(sleep_seconds), 0)
    except (TypeError, ValueError):
        total = 0
    cmd = f"MIND_AGENT={agent} {env_prefix} bash core/scripts/interruptible-sleep.sh"
    plan = foreground_chunk_plan(total, env)
    if plan is None:
        return (
            "Emit exactly ONE tool call:\n"
            f"  Bash(\"{cmd} {total}\", run_in_background=true)\n"
            "When the harness notifies you of its exit, call Skill('aspirations') with args='loop'.\n"
            + no_notify_hint(total, env)
        )
    chunk, n = plan
    return (
        f"THIS HARNESS CAPS A FOREGROUND SLEEP AT {chunk}s, so this {total}s sleep is {n} chunks "
        "(the last one short).\n"
        "Emit ONE tool call now -- FOREGROUND: no run_in_background, no trailing & "
        "(combining them double-detaches and forfeits the notification, guard-3892):\n"
        f"  Bash(\"{cmd} {chunk}\")\n"
        "It returns rc=2 the moment a wake signal lands, which is the point of foreground -- so\n"
        "re-check the loop's signal state before each further chunk and stop early if one fired.\n"
        f"If the turn must end before the {total}s is spent, {_wake_arm_phrase(chunk, env)}\n"
        "That arm is sized to ONE CHUNK, deliberately, not to the whole remaining sleep: the\n"
        "printers recompute the remainder on every invocation, so the re-entry sleeps whatever is\n"
        "actually left. Arming for the total would ADD to the chunks already slept and overshoot.\n"
    )


def _fmt(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        # lowercase to match true/false -- a shell consumer comparing against
        # "none" should not have to know the value came from Python.
        return "none"
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
    if argv[:1] == ["--sleep-directive"]:
        # The whole yield block, so a SHELL printer gets the same runtime-aware
        # text the Python printers get (idle-tick.sh). Three operands because
        # they are the only per-site context the builder needs (guard-2485):
        # everything else a site prints stays at the site.
        if len(argv) != 4:
            print("usage: harness-capabilities.sh --sleep-directive "
                  "<sleep_seconds> <agent> <env_prefix>", file=sys.stderr)
            return 2
        sys.stdout.write(sleep_directive(argv[1], argv[2], argv[3]))
        return 0
    if argv:
        print("usage: harness-capabilities.sh [--get <capability> | --json | "
              "--hint <sleep_seconds> | --sleep-directive <sleep_seconds> <agent> <env_prefix>]",
              file=sys.stderr)
        return 2
    print(" ".join(f"{k}={_fmt(v)}" for k, v in caps.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
