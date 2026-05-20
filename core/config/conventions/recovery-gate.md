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

## Cross-References

- `.claude/rules/stop-hook-compliance.md` — Behavioral rule (no LLM
  state mutation; recovery-gate exception clause)
- `.claude/rules/user-interaction.md` — Immutable user-only constraints
- `core/scripts/recovery-gate.sh` — The script implementing this gate
- `core/scripts/heartbeat-stale.sh` — Condition 2 probe
- `core/scripts/runner-recent-block.sh` — Condition 2.5 probe
- `core/scripts/session-manifest-clear.sh` — Recovery cleanup helper
- `core/config/session-manifest.yaml` — `recovery_action: clear` list
- `core/config/aspirations.yaml` — `runner_heartbeat.stale_minutes`,
  `DIARY_STALE_MINUTES`
