# Rationale: The /start Hand-Off Stop Route

Referenced from `.claude/skills/start/SKILL.md` (IDLE branch, autonomous path —
the terminal hand-off marker). Explains why that marker is a script echo, why it
branches on `stop-requested`, and why the branch is not the enforcement.

## Why the marker is a script echo, not only prose

Incident 2026-09-11 (agent `bobby`): `/start` set RUNNING, then stopped and asked
the user whether to boot, so the loop never started and the user had to re-run
`/start`. A prose "invoke /boot now" was readable as an offer. The echo mirrors
`iteration-close.sh`'s loop-reentry imperative: a tool output the mind sees in the
same turn, phrased as `NEXT ACTION REQUIRED`.

Autonomous `/start` from IDLE has no confirmation step. Only UNINITIALIZED
first-boot gates, on Self confirmation (`start-phase-c.md` C1.9). A turn that ends
on a summary or a question after the state flip leaves the agent RUNNING with no
loop.

## Why the marker branches on a pending stop

A stop can be raised while `/start` is still running, by a writer outside the
session. The vessel sidecar writes `stop-target-mode` then `stop-requested` when a
served run ends (`vessel-sidecar-stop-caller.md`). Step 2.5's clear REFUSES a
signal newer than the session's start (`vessel-stop-clear-guard.md`), so the
signal survives into the hand-off by design.

Before this branch, such a session held two orders that exclude each other. The
marker said "invoke Skill(boot) NOW — the non-optional terminal step". The
PreToolUse[Bash] advisory said a stop was pending and pointed at Phase -1.4, a
phase `/start` cannot reach.

Measured 2026-09-14 on dev vessel `i-058c0073c76d79e18`, drive session
`0317285b…`. The advisory arrived in a tool result at message 166 and `/start`'s
last page at message 169. At messages 172 and 174 the mind ended the turn having
done neither: it declined to boot and never ran the graceful stop. No
consolidation ran and no handoff was written. It never ran the marker Bash at all.

With a stop pending, the marker now names `Skill(aspirations-graceful-stop)` and
says "not Skill(boot)". Invoking that skill is the only thing Phase -1.4 does with
the signal, and the skill needs no loop context: GS-0 caches the target mode and
writes its checkpoint, and D1 sets IDLE before D2 writes `stop-loop`. The route is
not new outside the loop either. The Session Start Protocol in `CLAUDE.md`
already invokes the same handler with `--resume`.

## Why not boot and let Phase -1.4 find it

`/boot` then the loop do reach Phase -1.4, but late. On the 2026-09-13 run
(`i-022f74084032d8c56`, reserve 350s) the mind first probed the signal 324s after
it was raised: 213s of `/start`, then 111s of loop preamble. That left 26s of
grace. `/boot`'s prime sits between those two stretches. Every second spent
reaching the phase comes out of the consolidation the phase exists to run.

## Why the branch is enrichment, not the enforcement

guard-399: an instruction's form does not change who executes it, and the mind
above never ran this Bash. The script-owned baseline is the PreToolUse[Bash]
advisory, `core/scripts/bash-agent-inject.py::_maybe_surface_stop`. It fires on
every Bash call whatever skill is running, and it now names the same handler
(pinned by `core/scripts/tests/test_bash_inject_stop_surface.py`). The branch
adds no call, because it rides the Bash call the marker already makes. It makes
sure a mind that does follow the page receives one imperative instead of two.

The hand-off also sits past byte 65,536 of `start/SKILL.md` (line 913 starts at
byte 65,884, measured 2026-09-14), and `core/config/hot-path-budget.yaml` records
this skill arriving in context truncated on 2026-08-18. Where a harness truncates
the skill injection, the mind never sees this branch and the hook is the only
route. On the vessel, the pager delivered that page, marker included, as page 7
of 7.

Prediction, NOT yet measured: naming the handler turns the measured run's
"neither" into a graceful stop. It is settled by a capped dev-vessel run with
this seed deployed.

## Why an un-evaluatable check falls to the boot marker

guard-6178: a check that cannot evaluate must not read as an all-clear. When
`session-signal-exists.sh` exits 2 (invalid name) or the call fails, the `else`
branch prints the boot marker. That is safe in this one direction because the boot
route is itself stop-aware. The hook keeps naming the handler on every Bash call,
and Phase -1.4 reads the signal again. A false "no stop" costs latency, never the
stop.

## Cross-references

- `vessel-stop-clear-guard.md` — why Step 2.5 lets a live signal survive
- `vessel-sidecar-stop-caller.md` — the writer that can raise it mid-`/start`
- `.claude/skills/aspirations-graceful-stop/SKILL.md` — the handler, and its
  invocation contract for callers outside the loop
- `.claude/rules/stop-hook-compliance.md` rule 2 — only `/stop` and the handler
  write `stop-loop`, so the route never clears the signal itself
- guard-399, guard-6178
- g-373-16 — the stop-architecture goal (unit R3)
