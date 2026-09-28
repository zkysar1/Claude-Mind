# Stop Signal Writers — authorized callers catalog

Reference catalog behind `.claude/rules/stop-hook-compliance.md` rule 2. The
rule keeps the imperatives (the agent MUST NOT call these directly); this file
carries the full specification of each authorized caller — invariants, trigger
conditions, incident traces, and the `stop-requested` writer count. Loaded on
demand (`load-conventions.sh stop-signal-writers`).

## `stop-requested` writers (3 scripts + 1 out-of-repo sidecar)

Whoever adds a writer must update this count in the same change — an
authoritative-sounding count that has silently gone stale is worse than no
count at all. The recovery-gate / recovery-yank pair and the vessel recipe
below move `agent-state` instead and are counted in their own entries.

### 1. `productivity-stop-gate.sh` (2026-04-18)

Invoked only by `iteration-close.sh --phase productivity-check` at the end of
every iteration. Authorized to set `stop-requested` when the composite
productivity score falls below the configured floor AND the session has run at
least `min_iterations` goals. The gate is script-gated — not LLM-discretionary
— so the agent cannot bypass the threshold math.

The script MUST write `agents/<agent>/session/stop-target-mode` ("assistant")
BEFORE setting the signal, preserving the /stop invariant that Phase -1.4
reads the target mode without a fallback.

Parameters: `core/config/aspirations.yaml` -> `productivity_gate`
(`min_iterations`, `stop_threshold`).

### 2. `reducer-self-fence.sh` (2026-08-05, g-306-225)

Invoked only by `heartbeat-tick.sh`, on every tick, whatever the backend
(g-115-8200). Authorized to set `stop-requested` when the cross-machine
runner lease says this box is no longer the reducer. A lease needs
`T_stepdown < T_takeover`: the holder must stop acting as leader before a
peer may legally seize the claim.

Like productivity-stop-gate, the script MUST write
`agents/<agent>/session/stop-target-mode` ("assistant") BEFORE setting the
signal, preserving the /stop invariant.

The decision is script-gated in `core/scripts/reducer_self_fence.py::decide`
(pure, fully branch-tested; its docstring carries the lease argument, the
2026-08-05 incident and the signal asymmetry) and stands down on exactly THREE
triggers:

- `different-holder` — the live claim names another machine. Decisive alone.
- `superseded-token` — the live claim's runner-token fingerprint is not this
  box's (a same-box reducer restart the machine id cannot show). Decisive
  alone; inert when either fingerprint is unreadable.
- `sustained-renewal-gap` — renewal has failed CONTINUOUSLY for
  `runner_heartbeat.stepdown_seconds` (1950s = half of T_takeover).

Every other signal HOLDS, and that is the load-bearing half: a transient
daemon blip, an unreadable holder id, an unrecognised rc, and `rc=4`
(ABSENT | NOT-RUNNING | STALE | REFUSE) all keep the loop running. Stopping a
healthy loop on a plumbing fault is worse than the disease (guard-1562). Note
`rc=4` is DECISIVE in the sibling `worker_reducer_liveness.py` and INERT here
— the two modules are deliberate mirrors with opposite fail-safe directions,
and `test_reducer_self_fence.py` pins that divergence against the real worker
module so a future fusion of the two fails loudly.

### 3. `loop-exhaustion-fence.sh` (2026-09-04, g-115-8939)

Invoked only by `stop-hook.sh`, in two places: immediately before it builds
the BLOCK payload, and on the Gate 2.6 ALLOW exit (g-115-9467). Authorized to set `stop-requested` when the loop CANNOT EXECUTE:
rules 3-4 of stop-hook-compliance.md, the never-self-stop invariant and the
unconditional BLOCK otherwise leave a loop out of context no legal move but to
iterate emptily.

Like its two siblings it MUST write `stop-target-mode` ("assistant") BEFORE
setting the signal.

The decision is script-gated in `core/scripts/loop_exhaustion_fence.py::decide`
(pure, fully branch-tested; its docstring carries the 2026-09-04 incident and
the user directive) and keys on a BEHAVIOURAL predicate the model supplies no
input to — N consecutive stop-hook turn-ends for one sid, BLOCK or ALLOW
(`TURN_END_VERDICTS`, g-115-9467), with the execution diary's mtime frozen
throughout — so "out of context" is structurally
distinguishable from "feels done" and rule 5 is intact. It deliberately does
NOT decide on `context-budget-status.py`'s zone. Two rungs: `pause` at 4
turn-ends writes NOTHING and only directs the turn to end on a REGISTERED
external-wait sleep; `stop` at 10 writes the signal. Every unreadable input
HOLDS — stopping a healthy loop is worse than the disease (guard-1562).

### 4. The vessel sidecar (2026-09-12, g-373-16)

The **vessel sidecar** (`zakcode`, Zak-Code repo — the first authorized caller
that is NOT a framework script) is authorized to write `stop-target-mode` then
set `stop-requested` when a SERVED run ends: the human's `/run/stop`, or the
run's own duration cap. Vinheim decides WHEN a run ends; the MIND decides what
its ending IS.

Same two-write shape, same order, same revert-on-failure as its three siblings
above (`zakcode.session.framework_stop`), and ADDRESSED rather than
discretionary: no `run_stop_agent` configured = no signal, so a non-seed
workspace is untouched. Rationale, the ruling it enacts, and grace sizing:
`core/config/rationale/vessel-sidecar-stop-caller.md`.

## `agent-state` writers (2 scripts + 1 out-of-repo recipe, outside /start and /stop)

### 5. `recovery-gate.sh` (2026-04-19)

Invoked only by the SessionStart hook in `.claude/settings.json`. Authorized
to call `session-state-set.sh IDLE` (RUNNING -> IDLE only) AND
`session-manifest-clear.sh` under a script-gated 6-condition AND-gate.

The 6-condition spec (state=RUNNING + heartbeat stale + no recent stop-hook
BLOCK + execution-diary stale + no stop-requested + no pending background job)
is cataloged in `core/config/conventions/recovery-gate.md`.

This is the second authorized caller of `session-state-set.sh` outside /start
and /stop — productivity-stop-gate stays in RUNNING and only sets the stop
signal; recovery-gate moves RUNNING -> IDLE.

### 6. `recovery-yank-reverse.sh` (2026-09-01, g-357-51)

Invoked only by `stop-hook.sh` Gate 1-pre, when the turn-ending session finds
agent-state IDLE beside a `session/recovery-log.jsonl`. Authorized to call
`session-state-set.sh RUNNING` (IDLE -> RUNNING only) for the ONE sid the
recovery gate demoted: a process executing its own stop hook is alive by
construction, so that demotion was false (the 2026-09-01 rate-limited-alive
kill).

Script-gated by `recovery_yank.py preconditions` — same sid, bound autonomous
BEFORE the yank, inside the reversal window (default 6h), no user-stop
artifact after the yank, no peer holding the runner claim — and every miss is
a no-op.

Third authorized caller outside /start and /stop; the only one that moves
IDLE -> RUNNING.

### 7. The vessel recipe (2026-09-27, g-377-31 / g-377-35 / g-377-37)

The out-of-repo vessel recipe (`bootstrap.sh` 3.5/3.5b): UNINITIALIZED -> IDLE
at landing, and an every-boot RUNNING -> IDLE heal that removes
`stop-requested`/`stop-loop`. It also resets `persona-active` to false every
boot (`user-interaction.md`). Fourth caller outside /start and /stop.
Liveness-gated. It also checks the runner lease, but that check is inert
today: a vessel's lease answers REFUSE, which is zero signal. The heal writes
nothing while its host unit is active, a runner lease is fresh, the heartbeat
is fresh, or the diary was written within 15m. For a RUNNING mind it also
writes nothing while the heartbeat probe errors, a stop-hook BLOCK is under 5m
old, or `stop-requested` is under 15m old (an interrupted served stop strands
it, so the check is age-bounded). This is `runner-dead-check.sh` without its
two box-local probes and with a weaker condition 2: an absent heartbeat is not
life here, while the script holds it inert (g-377-37) —
`core/config/rationale/vessel-recipe-state-writer.md`.

## `session-state-set.sh`: /start's authorized sub-paths

The authorized /start sub-paths are: (1) the IDLE→RUNNING transition in the IDLE branch, (2) the RUNNING→IDLE rewrite in the explicit `/start --recover` crashed-runner cleanup (see start/SKILL.md Step 0.7), and (3) the RUNNING→IDLE rewrite in the auto-recovery branch under "RUNNING + requested mode is autonomous" when the 6-condition zombie gate passes (state=RUNNING + heartbeat=stale + no recent stop-hook BLOCK in last 5 min + execution-diary.jsonl mtime older than 15 min + no stop-requested + `background-jobs.sh has-pending` exits 1 — identical conditions and probe scripts as recovery-gate.sh), and (4) the UNINITIALIZED→IDLE transition in the Phase A-0 transplant-resume path (a cloned agent landing on a new machine — `.initialized` present but `agent-state` absent; the same init endpoint reader/assistant first-boot already uses, via `agent-resume-scaffold.sh` then `session-state-set.sh IDLE`; see start/SKILL.md Phase A-0). /stop's authorized callers are listed in stop-hook-compliance.md. The script-gated SessionStart hook caller `core/scripts/recovery-gate.sh` is also authorized (RUNNING→IDLE only, under the 6-condition AND-gate in entry 5 above), as is `core/scripts/recovery-yank-reverse.sh` (IDLE→RUNNING only, invoked solely by `stop-hook.sh` Gate 1-pre for the one sid a false recovery demoted, under the `recovery_yank.py preconditions` gate in entry 6 above — g-357-51). So is the out-of-repo vessel recipe (`bootstrap.sh` 3.5/3.5b): (4)'s twin at landing, and an every-boot RUNNING→IDLE heal, liveness-gated (g-377-35, g-377-37) — `core/config/rationale/vessel-recipe-state-writer.md`.
