# Rationale: /start — preflight, flag parsing and the recovery branch

Referenced from `.claude/skills/start/SKILL.md`. Extracted 2026-08-25 (g-115-7706):
that skill was 89,106 B against the 65,536 B on-demand injection ceiling, so roughly
its last 27% never reached the model at all. Every block below is VERBATIM — this was
a relocation, not a rewrite. The skill retains ALL procedure: its YAML front matter,
every `Bash:` line, every HALT/refusal branch, every `>` user-facing display line,
every bold directive, and every test-pinned literal. Only explanation moved here.

## The body role is DERIVED, not declared (user directive 202

*(was `start/SKILL.md` L26-39)*

The body role is DERIVED, not declared (user directive 2026-08-03 — the v1
explicit `--body worker` flag was judged needless cognitive load). A bare
`/start <agent-name>` asks the DDB runner-claim acquire: rc=4 (a live reducer
holds the claim from another machine) routes this box into the CW cross-box
worker sequence AUTOMATICALLY, with a loud announcement naming the holder —
the same rule the same-box RUNNING branch has always used to derive the
worker role. With no live peer, this box simply becomes the reducer. One rule
everywhere: `/start <agent>` runs the agent; the framework picks the body
role. There is deliberately NO explicit body flag — derivation is the ONLY
path (user directive 2026-08-03: exactly one way of doing things; the interim
`--body worker` flag was removed the same day derivation superseded it).
`--reducer-only` refuses the auto-join and shows the holder-naming refusal
instead — for the rare case where you intend to MOVE the reducer here and a
temporary worker would be unwanted noise.

## - --mode <value: mode flag. Valid values: reader, assistan

*(was `start/SKILL.md` L51-56)*

- `--mode <value>`: mode flag. Valid values: `reader`, `assistant`, `autonomous`. If omitted, default to `autonomous`. This default applies uniformly — including the Phase A-0 transplant-resume path, where a bare `/start <agent>` on a freshly-cloned agent resumes it autonomously, exactly like a bare `/start` on any IDLE agent. Pass `--mode reader` (or `assistant`) explicitly for the cautious first-boot-on-a-new-machine case.
- `--recover`: recovery flag. Set `recover = true` if any argument is the literal string `--recover`. This flag triggers the crashed-runner cleanup in Step 0.7 below. Only meaningful when agent state is RUNNING; fails loud otherwise.
- `--force`: force flag. Set `force = true` if any argument is the literal string `--force`. Bypasses the heartbeat-staleness precondition on `--recover` (emergency override for the "heartbeat fresh but runner is stuck" case). No effect outside recovery.
- `--body <anything>`: REMOVED (2026-08-03 — same-day supersede of g-306-119-a's explicit flag; user directive: exactly one way, derivation). Any argument that is the literal `--body` (with or without a value) is a HARD ERROR with this exact explanation: "`--body` was removed — the body role is always DERIVED: a bare `/start <agent>` auto-joins as a worker whenever a live reducer holds the claim elsewhere (rc=4). Use `--reducer-only` to refuse the auto-join." Do NOT silently ignore it — old docs and muscle memory deserve the explanation, not a mystery no-op.
- `--reducer-only`: set `reducer_only = true` when any argument is the literal string `--reducer-only`. Consumed at exactly ONE place: the ACQUIRE_RC=4 branch of the autonomous IDLE flow below, where it REFUSES the automatic worker-join and displays the holder-naming refusal instead. Use it when the intent is to MOVE the reducer to this box (/stop on the holder, then /start here) and a temporary worker would be unwanted noise.
- `--override-output-style <justification>`: override flag for the Step 0.6 + C7.7 autonomous+Explanatory gate. When present with a non-empty justification string, Step 0.6 lets the autonomous mode proceed, and C7.7 passes the same value to `output-style-gate.sh --override` for audit logging. The justification is echoed to `world/output-style-overrides.jsonl`.

## The helper checks 6 signals (state == RUNNING, heartbeat s

*(was `start/SKILL.md` L99-106)*

   The helper checks 6 signals (state == RUNNING, heartbeat stale, no recent
   stop-hook BLOCK, execution-diary stale, stop-requested NOT set, no
   background-jobs pending) — the SAME gate that `recovery-gate.sh`
   (SessionStart hook auto-recovery) uses, and that the IDLE-branch
   auto-recovery section below ("RUNNING + requested mode is autonomous")
   mirrors in LLM-orchestrated form. SINGLE SOURCE OF TRUTH at
   `core/scripts/runner-dead-check.sh`. Stderr emits a per-condition summary;
   stdout emits structured JSON for `--force` audit logging.

## Exit codes

*(was `start/SKILL.md` L108-111)*

   Exit codes:
   - `0` = runner is DEAD (all 6 conditions met — safe to recover)
   - `1` = runner is ALIVE (at least one liveness signal positive)
   - `2` = script error (fail-open conservative — refuse recovery)

## returned script error (rc=2). Investigate the helper and i

*(was `start/SKILL.md` L116-118)*

   returned script error (rc=2). Investigate the helper and its sub-probes
   (`heartbeat-stale.sh`, `runner-recent-block.sh`, `session-signal-exists.sh`,
   `background-jobs.sh`) before retrying." and exit without state changes.

## signals (--force):" followed by the helper's stderr per-co

*(was `start/SKILL.md` L140-143)*

   signals (--force):" followed by the helper's stderr per-condition list.
   Append a JSON audit record to `agents/<agent-name>/session/recovery-force-audit.jsonl`
   using the explicit locked-append helper so the write is race-safe even when
   two terminals attempt `--recover --force` concurrently:

## Manual override: clear the recovery-circuit-breaker counte

*(was `start/SKILL.md` L212-220)*

  Manual override: clear the recovery-circuit-breaker counter (2026-05-12
  hardening, Tier 2c). When recovery-gate has refused further automatic
  retries after 3 consecutive `_perform_recovery` failures, `/start --recover
  --force` is the documented escape hatch — it forces a fresh recovery
  attempt by deleting both the counter and the permanent-signal file.
  These two files are `recovery_action: preserve` in the manifest (so they
  survive normal manifest-clear runs to preserve cross-session memory of
  the failure state) — clearing them is a deliberate user-acknowledged
  override, hence the manual rm here outside the manifest pipeline.

## The AYOAIAGENT=<agent-name env prefix ensures we read agen

*(was `start/SKILL.md` L233-235)*

The `MIND_AGENT=<agent-name>` env prefix ensures we read `agents/<agent-name>/session/agent-state`,
not another agent's state. If no `<agent-name>` was provided (bare `/start` or `/start --mode`),
omit the prefix — use the current session binding.

## inlined-helper drift class. When Step 1 returns UNINITIALI

*(was `start/SKILL.md` L238-244)*

inlined-helper drift class. When Step 1 returns `UNINITIALIZED`, the agent
dir might genuinely not exist OR `session-state-get.sh` might have a stale
inlined `_APD` (AGENTS_PARENT_DIR) constant relative to `core/scripts/_paths.sh`
(rb-1092 — five sites inline that constant for latency, see CLAUDE.md
"Agent-dir Resolution"). The latter case would re-initialize a fully working
agent, clobbering aspirations, journal, handoff, and session history. This
probe distinguishes the two before the Phase A re-init begins.

## This is the Layer-A tactical defense (loud diagnostic at /

*(was `start/SKILL.md` L278-281)*

This is the Layer-A tactical defense (loud diagnostic at /start entry).
The companion Layer-B is `/verify-learning`'s inlined-_APD audit, which
grep-checks the 5 sites against `_paths.sh` on a routine cadence so drift
is caught even when no /start re-entry surfaces it.

