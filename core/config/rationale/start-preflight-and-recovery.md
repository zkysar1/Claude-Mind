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

## Why each Step 0.5 flag behaves as it does

*(moved from `start/SKILL.md` Step 0.5 by g-115-10958 — the skill keeps the
compact operative bullets; this keeps the reasons. The flag list had been left
ONLY here by the g-115-7706 pass, so Step 0.5 parsed nothing.)*

- `--mode`: the `autonomous` default applies uniformly — including the Phase
  A-0 transplant-resume path, where a bare `/start <agent>` on a
  freshly-cloned agent resumes it autonomously, exactly like a bare `/start`
  on any IDLE agent. Pass `--mode reader` (or `assistant`) explicitly for the
  cautious first-boot-on-a-new-machine case.
- `--force`: the emergency override for the "heartbeat fresh but runner is
  stuck" case.
- `--body`: REMOVED 2026-08-03 — same-day supersede of g-306-119-a's explicit
  flag (user directive: exactly one way, derivation). It is a hard error, not
  a silent no-op, because old docs and muscle memory deserve the explanation,
  not a mystery no-op.
- `--reducer-only`: use it when the intent is to MOVE the reducer to this box
  (/stop on the holder, then /start here) and a temporary worker would be
  unwanted noise.
- `--override-output-style`: C7.7 passes the value to `output-style-gate.sh
  --override` for audit logging; the justification is echoed to
  `world/output-style-overrides.jsonl`.

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

## signals (--force):" followed by the helper's stderr per-co

*(was `start/SKILL.md` L140-143)*

   signals (--force):" followed by the helper's stderr per-condition list.
   Append a JSON audit record to `agents/<agent-name>/session/recovery-force-audit.jsonl`
   using the explicit locked-append helper so the write is race-safe even when
   two terminals attempt `--recover --force` concurrently:

## Why the runner claim is released before the manifest-clear

*(was the prose under Step 0.7's `runner-claim.sh release` bullet; moved by
g-115-10958 to keep the skill under its injection ceiling)*

  DDB claim release with the crashed session's OLD on-disk runner-token
  (2026-07-07 bravo dual-runner follow-through). A crashed runner leaves its
  DDB row RUNNING; local recovery flips only LOCAL state, so without this
  release the fresh acquire below is held hostage by its OWN stale row for
  up to OWNERSHIP_STALE_SECONDS (~65 min post-calibration). MUST run BEFORE
  manifest-clear — `runner-token` is `recovery_action: clear`, so the old
  token is deleted by the next step. Token-conditional and idempotent: if a
  peer machine already stale-broke and re-claimed, the old token no longer
  matches and this is a no-op — it can never steal a peer's claim. Fail-open
  (`|| true`): a DDB hiccup must never block recovery.

## Why the clear is manifest-driven and runs after state-set IDLE

*(was the prose under Step 0.7's `session-manifest-clear.sh` bullet; moved by
g-115-10958)*

  Manifest-driven clear of every session file with `recovery_action: clear`.
  Runs AFTER state-set IDLE succeeded (g-115-683 reorder); the cleanup
  window now shows state=IDLE + sid present instead of state=RUNNING +
  sid=missing. SINGLE SOURCE OF TRUTH for the clear operation —
  `recovery-gate.sh` (SessionStart hook auto-recovery) calls the same
  script, and both consume `session-snapshot.sh --output json` (the
  canonical manifest parser, also used by `session-desync-check.sh`). The
  three signal files (`stop-requested`, `stop-loop`, `loop-active`) are all
  `recovery_action: clear` in the manifest, so this one call handles them
  too — no separate `session-signal-clear.sh` calls are required.

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

