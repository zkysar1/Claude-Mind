# Recovery Gate (Crashed-Runner Auto-Recovery)

## Purpose

This convention catalogs the 6-condition AND-gate that
`core/scripts/recovery-gate.sh` evaluates on every SessionStart hook fire
to decide whether to perform `RUNNING → IDLE` auto-recovery on a crashed
runner session.

The behavioral rule (`MUST NOT invoke recovery-gate.sh directly`) lives
in `.claude/rules/stop-hook-compliance.md`. This convention documents the
script-internal gate spec so future authors can change the gate (or audit
its conditions) without re-reading the rule file's mixed behavioral +
implementation prose.

## Caller

`core/scripts/recovery-gate.sh` — invoked **only** by the SessionStart
hook in `.claude/settings.json`. The LLM MUST NOT invoke this script
directly (enforced by `stop-hook-compliance.md` rule 2).

## Authorization Scope

Outside the user-only `/start` and `/stop` skills, exactly TWO scripts
are authorized to mutate `agent-state` or set `stop-requested`:

| Caller | What it mutates | When | Detail |
|---|---|---|---|
| `productivity-stop-gate.sh` | sets `stop-requested` only (state stays RUNNING) | End of every iteration | Triggers `/stop` if productivity score below floor |
| **`recovery-gate.sh`** (this convention) | `session-state-set.sh RUNNING → IDLE` | SessionStart hook + 6-condition AND-gate | Crashed-runner auto-recovery |

For comparison, the user-only paths that DO call `session-state-set.sh`
are: `/start` IDLE branch (IDLE → RUNNING), `/start --recover` (RUNNING
→ IDLE), and `/stop`'s graceful-stop sequence (RUNNING → IDLE inside
Phase -1.4 D1). Those are user-invoked, not autonomous, so they are
not counted under the "authorized non-user callers" list.

The 6-condition AND-gate is the safety mechanism that makes the LLM-side
restriction (no LLM-discretionary state changes) tractable: the script
cannot move state without ALL six external signals agreeing.

## The 6-Condition AND-Gate

ALL six conditions MUST hold simultaneously. If any condition fails, the
gate refuses to recover (returns 0 with reason).

| # | Condition | Probe script | Rationale |
|---|---|---|---|
| 1 | `agent-state == RUNNING` | `session-state-get.sh` | Recovery is only relevant for sessions that claim to be running. |
| 2 | Heartbeat is stale per `heartbeat-stale.sh` | `core/scripts/heartbeat-stale.sh` | Live runner ticks `runner-heartbeat` on every iteration. Stale heartbeat = crashed iteration loop. Threshold: `runner_heartbeat.stale_minutes` in `core/config/aspirations.yaml`. |
| 2.5 | `runner-recent-block.sh` returns 1 (no BLOCK in `.stop-hook-log` within last 5 min) | `core/scripts/runner-recent-block.sh` | Multi-signal liveness fix from g-115-492 (2026-05-09 cross-binding stomp). A recent stop-hook BLOCK means the runner WAS alive moments ago — a stale heartbeat alone could be misleading. |
| 2.7 | `execution-diary.jsonl` mtime older than `DIARY_STALE_MINUTES` (default 15 min) | `mtime` check | Second multi-signal liveness fix from g-115-494 (2026-05-10). Execution diary appends at sub-minute granularity and survives stop-hook interruptions where heartbeat-tick may not — covers race where heartbeat is stale but the session was actually mid-iteration. |
| 3 | `stop-requested` is NOT set | `session-signal-exists.sh stop-requested` | If the user explicitly typed `/stop`, the in-flight obligation path (Phase -1.4 graceful stop) is the correct recovery path — not this gate. |
| 4 | `background-jobs.sh has-pending` exits 1 (no Tier-A registered job) | `core/scripts/background-jobs.sh has-pending` | A Tier-A background job (forged-skill long-running task) means the loop is legitimately blocked on external work, not crashed. |

All six conditions are evaluated AT THE SCRIPT LEVEL — the LLM cannot
override the AND, cannot pass `--force-recover`, cannot bypass any single
condition. The gate is the SOLE authority on whether crashed-runner
recovery fires automatically.

## What `recovery-gate.sh` Does (on AND-gate pass)

1. `session-state-set.sh IDLE` — flips state RUNNING → IDLE.
2. `session-manifest-clear.sh` — deletes transient session files marked
   `recovery_action: clear` in `core/config/session-manifest.yaml`. This is
   the SAME script that `/start --recover` Phase 0.7 calls — the file list
   and clearing semantics are SSOT in `session-manifest.yaml`.
3. Writes `agents/<agent>/session/recovery-notice` with the cause (which
   conditions triggered) so the next user message surfaces a diagnostic.

The script-gated nature means the LLM CANNOT call `session-state-set.sh`
directly — it can only OBSERVE that recovery happened by reading the
notice file or seeing state=IDLE on next entry.

## Why This Is a Convention (Not Just a Rule)

`.claude/rules/stop-hook-compliance.md` teaches the BEHAVIORAL rule
("Claude MUST NOT modify session state directly"). The 6-condition gate
is the IMPLEMENTATION DETAIL that makes the rule tractable. Mixing the
two in the rule file:

- Made the rule file dense and hard to scan (rule body = 1 sentence; gate
  spec = 20+ lines of conditional detail).
- Hid the gate from future authors who would change the script without
  reading the rule file.
- Made it hard to audit "which gates exist in the framework?" — by
  promoting this to a convention, the gate joins the catalog of
  framework-internal protocols (board, session-state, infrastructure,
  recovery-gate, …).

The split is healthy. The rule stays domain-agnostic behavioral
discipline. The convention catalogs the gate spec.

## Related Recovery Paths (B/C/D)

The 6-condition gate above is **Path A** — crashed-runner recovery keyed on a
STALE heartbeat. `recovery-gate.sh` runs three additional source-independent
paths BEFORE the Path-A source gate (they fire on `compact`/`resume`
SessionStart sources too, where Path A is skipped):

| Path | Function | Trigger | Heartbeat |
|---|---|---|---|
| **B** — state-corruption | `_check_state_corruption` | Malformed/contradictory session-state signals | n/a |
| **C** — hung-autocompact | `_check_hung_autocompact` | A compact/resume that never progressed | STALE |
| **D** — wedged-loop (g-328-23) | `_check_wedged_loop` | Loop wedged behind a storage-layer `_fileops.acquire_lock` failure: the execution-diary's most-recent marker is an unclosed `phase_start` past `runner_heartbeat.wedge_stale_minutes` (65min, > `stale_minutes` by the g-328-25 invariant) | **FRESH** |

Path D is the mirror image of Paths A/C: it keys on a **FRESH** heartbeat. The
2026-07-04 own-cloud fleet-wedge (g-328-19 #4/#5) is its origin — a wedged loop
keeps re-ticking the DDB heartbeat (which is a simple put) while diary writes
stall behind the wedged `_fileops` lock, so Paths A/C's STALE-heartbeat gate
never fires and the wedge required a manual restart. Path D shares Path A/C's
suppressors: `state==RUNNING`, no `stop-requested`, no pending background job,
plus the `execute-in-flight` suppressor (a genuine long `phase-4-execute` within
240min is NOT recovered). Because A/C gate on STALE and D on FRESH, at most one
path's heartbeat gate passes for a given liveness state — no double-recovery.
Detector: `core/scripts/phase-wedge-check.py` (exit `0`=wedged, `1`=clean,
`2`=error → fail-open to no-recovery). rb-2768: the wedge signal is the LAST
unclosed `phase_start`, never the oldest — the execution-diary accumulates
20+ historically-unclosed `phase_start`s across autocompact boundaries, so an
oldest-marker detector false-positives on every call.

**g-328-25 invariant** (`wedge_stale_minutes` MUST stay > `stale_minutes`,
currently 65 > 60): a HEALTHY non-phase-4 phase's local `runner-heartbeat` ages
WITH its `phase_start` (both stamped near the Phase -0.5 → Phase 0 boundary; no
mid-phase re-tick during active work — `heartbeat-stale.sh` reads the local
mtime, ticked only by `heartbeat-tick.sh` and `interruptible-sleep.sh`). So by
the time a healthy `phase_start` crosses `wedge_stale`, its heartbeat is already
past `stale_minutes` → STALE, and Path D's heartbeat-FRESH gate suppresses
FIRST. Only a genuine wedge (heartbeat re-ticked FRESH while the diary freezes)
presents `phase_start` > `wedge_stale` WITH a fresh heartbeat. At 45 (< 60) a
long precheck/state-update could false-recover a HEALTHY agent (the g-328-25
defect, found by fresh-eyes-code self-review of g-328-23). Raising the threshold
is monotonically safe — it can only reduce Path D firings.

**The invariant's SAFETY ARGUMENT is falsified for any phase that writes to the
diary** (measured 2026-08-08, g-115-5227, zeta, hostname cc-02, `uname -r`
6.8.0-136-generic). The paragraph above concludes "Only a genuine wedge
(heartbeat re-ticked FRESH while the diary freezes) presents `phase_start` >
`wedge_stale` WITH a fresh heartbeat." A HEALTHY loop presented exactly that
combination on 2026-08-07 and was recovered 70 minutes into a deep goal it then
completed; the gate's own notice records `heartbeat=fresh, state=RUNNING`.

The mechanism is a coupling the argument does not account for.
`execution-diary.py::_advance_heartbeat()` ticks `session/runner-heartbeat` by
direct file touch on **every successful diary write** (called from `cmd_append`
and `_emit_phase_marker`), and `heartbeat-stale.sh` reads that same file by pure
mtime. So the parenthetical above — "ticked only by `heartbeat-tick.sh` and
`interruptible-sleep.sh`" — is incomplete: the diary is a third writer, and
`heartbeat-stale.sh`'s own header comment ("heartbeat-tick.sh is the single
[writer]") is stale for the same reason. A long phase that keeps writing
findings/observations therefore keeps its heartbeat FRESH indefinitely, and the
"heartbeat goes stale first" suppression never fires for it.

Note the ordering: `_advance_heartbeat` was added 2026-05-13 to FIX a
false-positive recovery (a 75-minute goal whose heartbeat staled while diary
appends continued), deliberately making the two "independent multi-signal
liveness probes" move together. The g-328-25 invariant was written later, on the
premise that they were still independent. Coupling them fixed the STALE-side
false positive and created the FRESH-side one.

This is closed at the detector rather than by re-tuning the threshold:
`check_wedge` now vetoes a wedged verdict when any diary write landed inside the
window (`liveness_veto: recent_diary_write`) — the same "diary activity means
alive" insight `_advance_heartbeat` encodes, applied where the verdict is formed.
Raising `wedge_stale_minutes` would NOT have helped: the heartbeat stays fresh
for the whole phase regardless of threshold, so any finite threshold is crossed
eventually by a long healthy phase.

## Cross-References

- `.claude/rules/stop-hook-compliance.md` — Behavioral rule (no LLM
  state mutation; recovery-gate exception clause)
- `core/scripts/phase-wedge-check.py` — Path D wedge detector (g-328-23)
- `core/config/aspirations.yaml` — `runner_heartbeat.wedge_stale_minutes` (Path D threshold)
- `.claude/rules/user-interaction.md` — Immutable user-only constraints
- `core/scripts/recovery-gate.sh` — The script implementing this gate
- `core/scripts/heartbeat-stale.sh` — Condition 2 probe
- `core/scripts/runner-recent-block.sh` — Condition 2.5 probe
- `core/scripts/session-manifest-clear.sh` — Recovery cleanup helper
- `core/config/session-manifest.yaml` — `recovery_action: clear` list
- `core/config/aspirations.yaml` — `runner_heartbeat.stale_minutes`,
  `DIARY_STALE_MINUTES`
