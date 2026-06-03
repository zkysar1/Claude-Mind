---
name: aspirations-graceful-stop
description: "Handles the graceful-stop path for the aspirations loop: recovers in-flight iteration checkpoints, completes pending verify/state-update obligations, and runs the deferred stop sequence D1-D7. Use whenever {agent}/session/stop-requested is detected at Phase -1.4 of the aspirations loop, or the loop needs to exit cleanly without dropping in-flight work. Internal handler — only /aspirations invokes it; the user-facing /stop command writes stop-requested which this handler then reads."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, compact-recovery, session-state]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-graceful-stop-b5abe5"
previous_revision_id: null
---

# /aspirations-graceful-stop — Graceful Stop Handler

Detects `stop-requested` signal (set by `/stop`) and completes in-flight obligations
before running the full stop sequence. This ensures no learning is lost.

**Invocation contract.** The caller (aspirations orchestrator Phase -1.4) has already
verified `session-signal-exists.sh stop-requested` returned exit 0. This skill MUST
end its execution by returning control to the harness (not to the orchestrator) —
after the deferred stop sequence completes, state is IDLE and mode has been updated,
so the orchestrator's iteration loop MUST NOT continue.

**Mode invariant.** This skill runs at the RUNNING→IDLE transition. D1 sets IDLE,
D7 sets the target mode. The `minimum_mode: autonomous` front matter is evaluated
at invocation time (before D1 runs), so the skill retains autonomous capabilities
through D7 even though agent-state changes mid-skill.

## Inputs

None mandatory. The skill reads directly from:
- `agents/<agent>/session/iteration-checkpoint.json` (if exists)
- `agents/<agent>/session/stop-target-mode` (MUST exist per invariant — see below)
- `agents/<agent>/session/latest-session-id`
- Session state via `session-state-*.sh`, `session-signal-*.sh`

## Outputs

No orchestrator return value. Control flow ends with the harness receiving the
stop-complete state. On re-entry, the agent is IDLE in the user-selected mode.

## Step 0: Load Conventions

`Bash: load-conventions.sh aspirations compact-recovery session-state`

Read only the paths returned (files not yet in context). If output is empty, all
conventions already loaded — proceed to next step.

## Phase GS-0: Cache Target Mode (race-safe pre-read)

Read `stop-target-mode` ONCE at entry and cache the value into the LLM's working
context as `target_mode` for use at D7. The file IS in `session-manifest.yaml`'s
`recovery_action: clear` list, which means it can be deleted mid-stop by:

- A parallel agent's SessionStart hook firing `recovery-gate.sh` →
  `session-manifest-clear.sh` (recovery-gate iterates ALL agents, not just the
  triggering one — Path B can fire on a different agent if its conditions match).
- An untraced race observed in alpha session-61 (2026-05-07): file existed at
  graceful-stop entry (`cat` returned "assistant"), then was missing 30+ bash
  calls later when D7 tried to re-read. Root-cause investigation deferred —
  see Investigate goal filed at the time of this skill change.

Caching at GS-0 makes D7 independent of the file's continued existence. If
the file is missing at GS-0 itself (the truly catastrophic case), default to
`"assistant"` (the post-stop default per CLAUDE.md modes table) and log a
desync-warning so the invariant violation stays visible.

```
# Single source of truth: read once, default once, log once.
# Both writers (/stop Step 1, productivity-stop-gate.sh) produce non-empty
# content. So `missing` and `empty` both represent invariant violation —
# treat them identically: default to "assistant" and log ONE desync warning.
# Do NOT re-add separate branches for missing vs empty — it produced a
# dead `reason` variable and asymmetric logging the first time around.
Bash: val=""
      if [ -f agents/<agent>/session/stop-target-mode ]; then
        val=$(tr -d '\r\n' < agents/<agent>/session/stop-target-mode)
      fi
      if [ -z "$val" ]; then
        val="assistant"
        # Single-line JSONL desync-warning (matches recovery-gate.sh idiom).
        # Surfaces in next /prime so the invariant violation stays visible
        # without blocking the stop sequence.
        python3 -c "import json,sys; print(json.dumps({'id':'stop_target_mode_missing_at_gs0','severity':'warning','description':'stop-target-mode missing or empty at graceful-stop GS-0 entry; defaulted to assistant. /stop should have written non-empty content before setting stop-requested (stop/SKILL.md Step 1).','logged_at':sys.argv[1]}))" "$(date +%Y-%m-%dT%H:%M:%S)" >> agents/<agent>/session/desync-warnings.jsonl
      fi
      echo "$val"
# LLM: cache the bash stdout above as `target_mode` for use at D7.
# Valid values: "assistant" or "reader". Any other value should never be
# produced by /stop's flag parser (stop/SKILL.md Step 0.5) — if seen,
# treat as "assistant" defensively.
Output: "▸ GRACEFUL STOP: target_mode = {target_mode} (cached for D7)"
```

## Phase GS-1: Iteration Checkpoint Recovery

Output: "▸ GRACEFUL STOP: stop requested — checking for in-flight obligations..."

```
IF agents/<agent>/session/iteration-checkpoint.json EXISTS:
    Read agents/<agent>/session/iteration-checkpoint.json
    # Staleness uses last_updated (refreshed every phase). Falls back to
    # started_at only for pre-migration checkpoints that predate the
    # last_updated field — new writes always include last_updated.
    ts_field = checkpoint.last_updated OR checkpoint.started_at
    should_reconstruct = true

    IF ts_field is > 1 hour ago:
        # EVIDENCE GATE — never revert a goal whose state-update already
        # set status to "completed". This is the fix for the bug where a
        # completed goal was reverted because the checkpoint timestamp
        # aged out. has_evidence := (status == "completed") — see
        # core/scripts/goal-completion-evidence.sh.
        Bash: core/scripts/goal-completion-evidence.sh {checkpoint.goal_id}
        evidence = parsed JSON
        IF NOT evidence.has_evidence:
            Output: "▸ GRACEFUL STOP: stale checkpoint (>1h, status={evidence.status}, no completion evidence) — reverting {checkpoint.goal_id} to pending"
            Bash: aspirations-update-goal.sh --source {checkpoint.source} {checkpoint.goal_id} status pending
            # Clear team-state in_flight on revert (g-284-03). Without this, a stale
            # phase=4 entry persists in team-state until manually cleared, mis-signalling
            # cross-agent claim conflicts on the partner-claim filter (aspirations-select
            # Phase 2). The script is idempotent — no-op if in_flight is already cleared.
            Bash: MIND_AGENT=<agent> bash core/scripts/team-state-clear-in-flight.sh --agent <agent>
            rm agents/<agent>/session/iteration-checkpoint.json
            should_reconstruct = false
        ELSE:
            Output: "▸ GRACEFUL STOP: stale checkpoint (>1h) but status={evidence.status} (journal={evidence.journal_entries}, exp={evidence.experience_entries}) — reconstructing from phase_completed={checkpoint.phase_completed}"

    IF should_reconstruct:
        # Reconstruct goal context from checkpoint. goal_id is globally
        # unique; aspirations-query filters the field exactly.
        Bash: aspirations-query.sh --goal-field id {checkpoint.goal_id}
        results = parsed JSON array
        goal = first result where source == checkpoint.source
        outcome_class = checkpoint.outcome_class
        result = checkpoint.result_summary

        # Read sub-phase progress so the re-invoked verify skips already-passed
        # Q-checks. See core/config/conventions/compact-recovery.md
        # "Iteration Checkpoint phase_progress Field".
        prior_checks = checkpoint.phase_progress or {}

        # Forced reasoning-snapshot flush — the re-invoked verify needs a fresh
        # synthesis in context. Captures which phase is being re-entered and
        # which prior_checks are being respected (Layer 3 flush site 2, see
        # compact-recovery.md "Framework-Forced Write Sites").
        # Optional: agents MAY mirror this as a short musing on the reasoning
        # board channel for cross-agent visibility (see board.md "Casual
        # Reasoning Channel").
        echo '{
          "current_goal":"<goal.title> [<goal.id>]",
          "approach":"Graceful-stop re-invoke after interruption",
          "current_theory":"Resume from phase_completed=<checkpoint.phase_completed>; prior_checks=<keys of prior_checks>",
          "next_step":"Complete remaining obligations and run deferred stop sequence",
          "key_decisions_this_session":"<brief bullets from loop_state>",
          "trigger":"pre-stop-resume-auto"
        }' | bash core/scripts/reasoning-snapshot.sh write

        # Complete remaining obligations based on phase_completed
        IF checkpoint.phase_completed == "execute":
            Output: "  Completing verification..."
            Skill(aspirations-verify) with: goal, result, checkpoint.source, prior_checks
            Output: "  Completing state update..."
            Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, checkpoint.source
        ELIF checkpoint.phase_completed == "verify":
            Output: "  Completing state update..."
            Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, checkpoint.source
        ELIF checkpoint.phase_completed == "spark":
            Output: "  Completing state update..."
            Skill(aspirations-state-update) with: goal, result, session_count, outcome_class, checkpoint.source
        ELIF checkpoint.phase_completed == "state_update":
            Output: "  All critical obligations already complete"

        rm agents/<agent>/session/iteration-checkpoint.json
        Output: "  Obligations complete."
ELSE:
    # No checkpoint — check for orphaned in-progress goals.
    # Partner-claim filter: read every OTHER agent's in_flight goal_id first
    # and skip those — they belong to that agent's live execution, not ours.
    # Without this filter the revert loop will set partner-claimed goals back
    # to pending mid-execution (2026-05-11 bravo nearly reverted alpha's
    # g-250-57 at phase=4). Same partner-skip pattern aspirations-select uses
    # at the candidate-rank stage.
    Bash: team-state-read.sh --field agent_status --json
    partner_claimed = set of agent_status[name].in_flight.goal_id
                      for every name != <agent> where in_flight is non-null
    Bash: aspirations-query.sh --goal-status in-progress
    FOR EACH returned goal:
        IF goal.id in partner_claimed:
            Output: "  Skipped {goal.id} (partner-claimed — not ours to revert)"
            CONTINUE
        Bash: aspirations-update-goal.sh --source {goal.source} {goal.id} status pending
        Output: "  Reverted {goal.id} to pending (execution interrupted)"
    # Clear team-state in_flight on revert (g-284-03). Called unconditionally —
    # not per-goal, because in_flight holds at most one entry per agent. The
    # script is idempotent. Same rationale as the stale-checkpoint branch above:
    # stale phase=4 entries in team-state mis-signal cross-agent claim conflicts.
    # Skipped when no orphans are found (zero iterations of FOR EACH above means
    # no in_flight could have been set by this agent's last iteration anyway).
    Bash: MIND_AGENT=<agent> bash core/scripts/team-state-clear-in-flight.sh --agent <agent>
```

## Phase GS-2: Deferred Stop Sequence (D1-D7)

This replaces the old /stop steps 1-7. Runs here because state is still
RUNNING (mode still autonomous) at entry, so consolidation can run with full permissions.

Output: "▸ Running stop sequence..."

**IMPORTANT — belt-and-suspenders MIND_AGENT prefix**: every script call in
D1-D7 below uses the explicit `MIND_AGENT=<agent>` prefix. Not redundant:
(1) D6 deletes the `.active-agent-$SID` binding file at end-of-sequence, so
any script call after that line would lose the PreToolUse hook's auto-inject
source (bug traced 2026-04-22 bravo session-56 — D7's `session-mode-set.sh`
fired without a prefix right after D6's binding delete, hit "no agent active",
retried with explicit prefix as workaround). (2) Belt-and-suspenders against
hook cold-start timeouts (same rationale as /start IDLE-branch Step 2).
Mirrors the pattern in `.claude/skills/start/SKILL.md` autonomous sub-path.

```
# D1: Set IDLE
Bash: MIND_AGENT=<agent> bash core/scripts/session-state-set.sh IDLE
# D2: Set stop-loop (allows the stop hook to pass on next fire)
Bash: MIND_AGENT=<agent> bash core/scripts/session-signal-set.sh stop-loop
# D3: Clear stop-requested
Bash: MIND_AGENT=<agent> bash core/scripts/session-signal-clear.sh stop-requested
# D4: Consolidation
Bash: MIND_AGENT=<agent> bash core/scripts/consolidation-precheck.sh
IF verdict == "FULL": invoke /aspirations-consolidate with: stop_mode = true
ELIF verdict == "FAST":
    Bash: MIND_AGENT=<agent> bash core/scripts/load-consolidation-housekeeping.sh → IF path returned: Read it
    # Follow housekeeping steps with stop_mode = true
ELSE: invoke /aspirations-consolidate with: stop_mode = true
# D4.5: Clear session-identity fields (session_id, session_start).
# wm-reset inside consolidate preserves these across the autocompact boundary
# because the session continues there. /stop is the ONE place the session
# genuinely ends, so clear identity explicitly here. Without this, stale
# session_start would survive until the next /start rewrites it — wrong
# semantic for "this session is over". See wm.py SESSION_IDENTITY_FIELDS.
Bash: MIND_AGENT=<agent> bash core/scripts/wm-clear-identity.sh
# D5: Clear loop_state
echo 'null' | MIND_AGENT=<agent> bash core/scripts/wm-set.sh loop_state
# D5.5: (Retired — the agent-watchdog no longer runs as a daemon. It is now
# a periodic probe invoked from iteration-close.sh productivity-check via
# `agent-watchdog.py --tick`. No process to kill here. The probe's state
# file agents/<agent>/session/watchdog-prev-state.json is cleared by D6 below
# via session-manifest-clear semantics.)
# D6: Session cleanup — runner-session files only, NOT the SID binding.
# The .active-agent-$SID binding deletion moved to D7.1 below so D7's
# session-mode-set.sh call still has the hook auto-inject source if its
# MIND_AGENT= prefix were ever dropped. Even with the explicit prefix on
# D7, keeping the binding through D7 is the right semantic (the session
# IS this agent until the stop sequence completes).
#
# CRITICAL — `loop-active` and `stop-loop` ARE cleaned here on the happy
# path. Earlier reasoning assumed stop-hook.sh Gate 2 would clear stop-loop
# on the next Stop event after D7.1, but Gate 2 is unreachable on the
# graceful-stop happy path: D6 deletes running-session-id which makes the
# hook exit at Gate 0 (no-runner), AND D1 already set state=IDLE which
# makes Gate 1 short-circuit before Gate 2 anyway. So Gate 2's clear-on-
# allow path only runs in crash-recovery scenarios. /start's defensive
# sweep (start/SKILL.md Steps 2.5 / 4 — clearing stop-loop and loop-active
# at session entry) stays in place as the second layer of defense for
# crashed-stop recovery — DO NOT remove it. This D6 cleanup is the primary
# happy-path cleaner; /start's sweep handles the cases where /stop never
# reached D6.
Bash: rm -f agents/<agent>/session/running-session-id agents/<agent>/session/aspirations-compact.json agents/<agent>/session/context-budget.json agents/<agent>/session/context-reads.txt agents/<agent>/session/background-jobs.yaml agents/<agent>/session/compact-pending agents/<agent>/session/iteration-checkpoint.json agents/<agent>/session/loop-active agents/<agent>/session/stop-loop
# D6.5 (Phase 2.6.D): Write per-session summary into the bound session dir.
# Makes agents/<agent>/sessions/<SID>/ self-describing for queryable history
# (the user-stated goal of "long term we want to reply how many sessions there
# were"). Best-effort — if the session dir was never created, the script exits
# 0 silently. Goal/tree counts are 0 here because graceful-stop doesn't have a
# rolling counter; future enhancement is to seed them from team-state intel.
Bash: MIND_AGENT=<agent> SID=$(cat agents/<agent>/session/latest-session-id 2>/dev/null | tr -d '\r\n'); [ -n "$SID" ] && bash core/scripts/session-summary-write.sh --sid "$SID" --agent "<agent>" --reason graceful-stop >/dev/null || true
# D6.7 (session-continuity redesign, 2026-06-02): Flush this machine's governed
# writes to S3 NOW so a machine-move right after this clean stop cannot strand
# the session's last continuity writes locally. By this point ALL continuity
# files are written: handoff (D4 consolidate), working-memory (D4.5/D5),
# execution-diary, session-summary (D6.5); machine-local files were deleted in
# D6. The flush is the deterministic race-closer over the daemon's 120s periodic
# sweep — under the own-cloud backend it pushes continuity/ephemeral session
# files (+ world/meta) immediately; under the local backend it is a clean no-op.
# Best-effort: the daemon's periodic sweep is STILL running after this agent
# goes IDLE and will retry any file the flush missed, so a flush error warns
# but never blocks the stop. (Runs as its own Bash line — its rc cannot break
# D7's chain.)
Bash: MIND_AGENT=<agent> bash core/scripts/owncloud-flush.sh || echo "[owncloud-flush] WARN: flush returned non-zero — periodic sweep (interval 120s) will retry; avoid an immediate machine-move until the next sweep tick"
# D7: Apply post-stop mode AND emit the final user-facing message in ONE Bash
# call. The Bash stdout IS the stop-complete message — there is no separate text
# Output block after this. That makes the Bash invocation the unambiguous last
# action of the skill, satisfying return-protocol.md.
#
# CRITICAL — uses {target_mode} CACHED AT GS-0, not a re-read. The file may have
# been deleted mid-stop by a parallel session's recovery-gate manifest-clear (or
# the untraced race observed alpha session-61, 2026-05-07). GS-0 already handled
# the missing-file case with a desync-warning + "assistant" default. D7 trusts
# the cached value; the rm -f below is idempotent (no error on missing file)
# so it cleans up only if the file still exists.
# Single Bash line: shell vars don't persist across separate `Bash:` tool calls.
# Do NOT split this back into "set mode" + "emit message" steps — that reintroduces
# the text-ending-turn problem that return-protocol.md forbids.
# {target_mode} is exactly "assistant" or "reader" — /stop's flag parser
# (stop/SKILL.md Step 0.5) produces only these two values, and GS-0's default
# is "assistant". The binary if/else below is safe by that invariant: else is
# unambiguously "reader". Do not trust session-mode-set.sh as a second line of
# defense — it also accepts "autonomous", so any new caller that could write
# "autonomous" via target_mode would print the reader message while setting
# autonomous mode (silent mismatch). Add a third branch if the set of valid
# targets ever expands.
#
# CRITICAL — `_STATE=$(cmd || echo "?")` and `_MODE=$(...)`: the `|| echo "?"`
# is REQUIRED. Without it, if session-state-get.sh exits non-zero the subshell
# inherits that exit code, breaking the && chain — the entire farewell message
# (heredoc + stop-verified block) silently disappears, regressing the very
# user-visible-summary fix this verification block was added for. Mirror of
# the pattern used in recovery-gate.sh's three state-get probes.
Bash: MIND_AGENT=<agent> bash core/scripts/session-mode-set.sh "{target_mode}" && rm -f agents/<agent>/session/stop-target-mode && _STATE=$(MIND_AGENT=<agent> bash core/scripts/session-state-get.sh 2>/dev/null || echo "?") && _MODE=$(MIND_AGENT=<agent> bash core/scripts/session-mode-get.sh 2>/dev/null || echo "?") && _RESID=$(ls agents/<agent>/session/running-session-id agents/<agent>/session/aspirations-compact.json agents/<agent>/session/iteration-checkpoint.json agents/<agent>/session/loop-active 2>/dev/null | wc -l | tr -d ' ') && if [ "{target_mode}" = "assistant" ]; then cat <<'EOF'
Agent stopped. Session consolidated — encoding, journal, and handoff saved.
Mode set to assistant (reconciliation-ready). You can mark goals complete, edit tree
nodes, or add guardrails without a mode-switch ceremony. Full access to accumulated
knowledge — ask me anything.
Type `/start <agent-name>` to resume autonomous mode; `/stop <agent-name> --reader` for walk-away safety next time.
EOF
else cat <<'EOF'
Agent stopped. Session consolidated — encoding, journal, and handoff saved.
Mode set to reader (read-only). Chat and query knowledge freely — no writes allowed.
Type `/start --mode assistant` for user-directed edits, or `/start` to resume autonomous.
EOF
fi && echo "" && echo "═══ Stop verified ═══════════════════════════════" && echo "  state=$_STATE  |  mode=$_MODE  |  residual session files=$_RESID" && if [ "$_RESID" -gt 0 ]; then echo "  (residuals from D6 deny — /start's defensive sweep cleans on next session entry)"; fi && echo "═══════════════════════════════════════════════════"
# D7.1: Final housekeeping — delete SID binding. Must run AFTER D7 so the
# PreToolUse hook can resolve MIND_AGENT for D7's mode-set call in the event
# that a future edit drops the explicit prefix (defense in depth against
# prefix-forgetting regressions). This is the terminal Bash of the skill.
# No text output may follow — return-protocol.md.
Bash: SID=$(cat agents/<agent>/session/latest-session-id 2>/dev/null | tr -d '\r\n'); [ -n "$SID" ] && rm -f ".active-agent-$SID"
```

## Return Protocol

Does NOT return control to the orchestrator. Control flow ends with D7.1 — a
final single-line Bash command that deletes the SID binding file. D7 emits
the user-facing stop-complete message via heredoc; D7.1 is the trailing
binding-cleanup that exists purely as the terminal tool call after D7's
heredoc ends (which is itself a Bash tool call). The harness exits the session
turn after D7.1. On next user message, the agent is IDLE and will follow the
Session Start Protocol (`.claude/skills/start/SKILL.md`).

See `.claude/rules/return-protocol.md` — last action must be a tool call, not
text. The D7.1 `rm -f ".active-agent-$SID"` IS that tool call. No text output
may follow — the D7 heredoc's content IS the user-facing stop message; D7.1
runs silently after (its stdout is just the shell test-and-rm combination,
which produces no user-visible output when the file exists).

## Chaining

- **Called by**: `/aspirations` orchestrator Phase -1.4 (only when `stop-requested` signal exists)
- **Calls**: `aspirations-verify`, `aspirations-state-update` (for in-flight obligation completion); `aspirations-consolidate` OR `load-consolidation-housekeeping.sh` (D4); many scripts for D1-D7
- **Reads**: iteration-checkpoint.json, stop-target-mode, handoff context
- **Writes**: agent-state (IDLE), agent-mode (target), various session signals
