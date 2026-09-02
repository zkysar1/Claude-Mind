# Rationale: /start Recovery Ceremony

Referenced from `.claude/skills/start/SKILL.md` — Step 0.7 (`--recover` flow),
the RUNNING zombie-gate section, and the RUNNING auto-recovery branch.
Explains WHY the 6-condition gate exists, WHY ordering is state-set-first,
and WHY the inline checks duplicate `runner-dead-check.sh` instead of calling it.

## Why `_fileops.locked_append_jsonl` for the force-audit record

`_fileops.locked_append_jsonl` creates the file if missing and uses the same
lockfile protocol as every other JSONL writer in the framework — single source
of truth for race-safe appends.

## Why the 6-condition gate replaced single-signal heartbeat-stale (g-115-947, 2026-05-19)

Origin: replaced the single-signal heartbeat-stale precondition with the
6-condition helper. Prior to this, `/start --recover` only checked
`heartbeat-stale.sh`, while `recovery-gate.sh` and the auto-recovery branch
used 6 conditions (g-115-492 multi-signal + g-115-494 diary freshness). A
non-runner terminal running `/start --recover` during a heartbeat-stale window
(common between iterations) could stomp the live runner's `running-session-id`
— canonical incident 2026-05-18 (alpha session 4334f019 vs d964c395). The
6-condition gate closes that gap.

## Why state-set IDLE fires BEFORE manifest-clear (g-115-683, 2026-05-13)

Ordering (inverse of rb-323/guard-403): state-set IDLE fires BEFORE
manifest-clear so observers never see state=RUNNING + sid=missing during the
cleanup window. If state-set fails, manifest-clear is SKIPPED — leaves
state=RUNNING + sid present (normal RUNNING, recoverable on next SessionStart)
rather than the half-recovered zombie state (sid gone + state=RUNNING) that
the previous ordering produced. Mirrors aspirations-graceful-stop D1→D6 pattern
(state flip first, observer cleanup after).

This applies at all three cleanup sites: the `--recover` Step 0.7 branch,
the RUNNING auto-recovery branch, and `recovery-gate.sh`'s `_perform_recovery`
— all loud-fail, all state-set-first.

## Why the zombie-gate checks are inline instead of calling runner-dead-check.sh

The six conditions and the exact probe scripts MUST match
`core/scripts/runner-dead-check.sh` (g-115-947 canonical helper, 2026-05-19)
— that helper is the single source of truth, and both
`core/scripts/recovery-gate.sh` (`run_gate_for_agent`) and `/start --recover`
Step 0.7 defer to it. This LLM-orchestrated section is the THIRD parallel
implementation: it stays inline rather than calling the helper because each
branch below produces a different user-facing message (live runner detected /
safety hold-back with per-condition cause / auto-recover success), and a bash
helper that emits JSON cannot drive the LLM's per-branch user prose. Any change
to the 6 conditions MUST update BOTH `runner-dead-check.sh` AND the inline
checks — they cannot diverge without producing inconsistent recovery behavior
between the silent-script and LLM paths.

## Why condition 2.5 (runner-recent-block.sh) — multi-signal liveness (g-115-492)

Heartbeat staleness alone is too weak when transient platform issues (e.g.,
Claude Code 2.1.133 stop-hook timeouts) cause heartbeat to stale even though
the runner is alive. A recent BLOCK in `core/logs/stop-hook.log` proves
stop-hook fired AND the loop re-entered — three events that don't happen on a
dead runner. Without 2.5, the 2026-05-09 cross-binding stomp recurs.

## Why condition 2.7 (execution-diary.jsonl mtime) — second liveness signal (g-115-494)

When stop-hooks are intermittently timing out, the runner can stay alive
(processing user messages, background-task notifications) but the
heartbeat-tick path is not reliably executed. `execution-diary.jsonl` is
appended at every Phase start/end (sub-minute granularity) AND survives
stop-hook interruptions because phase-end writes BEFORE the LLM yields the
turn — making diary mtime a more reliable liveness signal than heartbeat when
stop-hooks are flaky.

## Why auto-recovery exists (the user-facing outcome when all six hold)

When all six conditions hold, `/start` auto-recovers inline so the user can
simply re-run `/start <agent-name>` without a manual `--recover` ceremony. The
explicit `--recover` / `--recover --force` paths still exist for cases where
the gate holds back (heartbeat fresh but runner is wedged, or stop-requested
set, or background-job is registered but actually orphaned).

## Why the session-telemetry crash-close fires before manifest-clear (WP5, 2026-06-03)

The /start RUNNING+autonomous auto-recover branch handles the SAME event
`recovery-gate.sh` handles via its SessionStart hook (WP4), but LLM-orchestrated
at /start time. The crashed runner's SID is still in `running-session-id`
(manifest-clear has not run yet), so its durable telemetry record is finalized
there with status=crashed, ended_reason=recovery-gate — and it MUST run BEFORE
manifest-clear, which deletes `running-session-id`. `write_crash` forces
goals_completed=-1 (the crashed runner's outcome is unknown). Fire-and-forget
(`|| true`): a telemetry failure must NEVER abort recovery. guard-165: SID and
agent travel via ENV with the python source single-quoted; `py -3` because this
is Bash-tool context, not a sourced .sh (the Microsoft-Store-stub rule applies).
Runs only when a crashed SID is present.

## Why an absent heartbeat is inert at /start (g-357-51, 2026-09-01)

`heartbeat-stale.sh` prints `absent` when no heartbeat file exists (never
seeded, or cleared by an earlier recovery). Absence is not evidence of a crash:
a box with no heartbeat infrastructure would otherwise read "crashed" forever,
and the 2026-09-01 false-positive kill of a live, rate-limited loop showed that
every inline condition passes on a LIVE loop during a multi-hour provider
backoff. So /start never walks the inline conditions on `absent` and never
auto-recovers from them alone: it asks `runner-dead-check.sh`, which admits an
absent heartbeat only beside a positive death signal (dead runner-proc stamp,
stale assistant turn) and vetoes on positive life evidence (live process,
recent assistant turn, live sidecar, provider-retry activity) — the same
pre-kill re-check the confirmed-zombie path runs once more before touching
state.

## Cross-references

- `core/scripts/runner-dead-check.sh` — SSOT for the 6-condition gate; MUST match inline checks
- `core/scripts/recovery-gate.sh` — SessionStart hook that calls runner-dead-check.sh
- `rb-323`, `guard-403` — original ordering incident (state-set-after ordering that produced zombies)
- `g-115-947` — 6-condition helper forge
- `g-115-492` — condition 2.5 (multi-signal) origin
- `g-115-494` — condition 2.7 (diary-mtime) origin
- `g-115-683` — state-set-first ordering fix
