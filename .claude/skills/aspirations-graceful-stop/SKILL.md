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

**Resume mode (`--resume`, FW-11 / g-317-09).** Autocompact can interrupt this
handler mid-sequence — most often during the long D4 consolidate. Because D1
flips agent-state to IDLE early, the aspirations loop then bails at its Phase
-1.5 state gate and never re-enters Phase -1.4 to re-detect `stop-requested`
(which D3 also clears), so the half-finished stop strands: consolidation/handoff
incomplete, agent-mode stuck at autonomous, `loop_state` lingering. To make this
recoverable, GS-0 writes a persistent `stop-checkpoint.json` sentinel that is
cleared ONLY at clean completion (D7.1). Its presence is the detection signal:
the Session Start Protocol (CLAUDE.md, IDLE branch) probes
`stop-checkpoint.sh resume-needed` and, on a hit, invokes
`/aspirations-graceful-stop --resume`. The `--resume` path is identical to the
fresh path (GS-0 → GS-2) — every D-phase is idempotent (set-IDLE, clear-signal,
consolidation-precheck FAST-when-done, `rm -f`, set-mode), so re-running the
whole sequence is safe and correct. The only differences on `--resume`: (1) the
caller has NOT verified `stop-requested` (the checkpoint is the trigger instead),
and (2) GS-0 prefers the checkpoint's cached `target_mode` because D7 may have
already deleted `stop-target-mode` before the interruption. A resume-count
breaker (cap 3, in `stop_checkpoint.py`) stops auto-resuming a persistently
failing stop and surfaces it for manual intervention.

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

# RESUME target_mode source (--resume only, FW-11 / g-317-09): on resume, the
# stop-checkpoint is the authoritative record of the user's chosen post-stop
# mode — D7 may have already deleted stop-target-mode before the interruption,
# so the read above can have defaulted to "assistant" and lost the real choice.
IF invoked with --resume:
    Bash: cp_json=$(MIND_AGENT=<agent> bash core/scripts/stop-checkpoint.sh read)
    IF cp_json != "null": target_mode = cp_json.target_mode   # checkpoint wins on resume
Output: "▸ GRACEFUL STOP: target_mode = {target_mode} (cached for D7)"

# Persist the stop-checkpoint sentinel (FW-11 / g-317-09). Its PRESENCE = "a
# graceful stop is in progress / was interrupted"; it is cleared ONLY at D7.1
# (clean completion). Fresh stop -> resume_count 0; --resume re-entry -> ++ (the
# breaker input). Stdout (the confirmation JSON) is noise here so it is dropped;
# stderr is preserved so a real write failure stays visible. Fire-and-forget —
# a checkpoint-write failure degrades resume-detection but must NEVER block the
# stop sequence.
Bash: MIND_AGENT=<agent> bash core/scripts/stop-checkpoint.sh write --target-mode "{target_mode}" >/dev/null || echo "[graceful-stop] WARN: stop-checkpoint write failed (resume-detection degraded; stop proceeds)"
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
    # --full is REQUIRED: the default projection returns only six keys
    # (asp_id, category, goal_id, source, status, title), so `goal.id` below
    # resolves to nothing and the revert would pass an empty goal-id to a
    # status mutation. --full widens the projection to include `id`.
    Bash: aspirations-query.sh --goal-status in-progress --full
    # OWNERSHIP CASCADE (g-115-5719). Revert ONLY what THIS Body owns. Take the
    # most specific ownership signal the goal carries; in_flight is the LAST
    # resort, never the primary, because it holds AT MOST ONE goal per agent —
    # so as a filter it protects one goal and exposes every other one the
    # partner holds. That is guard-1802's narrow-predicate class inverted: the
    # protective predicate is narrower than the population it must protect, so
    # the blast radius GROWS the harder partners are working.
    #
    # claimed_by_sid FIRST, not claimed_by — the discriminator is the BODY, not
    # the agent. One agent runs many Bodies (Mind/Body split), and they all
    # write the SAME claimed_by. Measured 2026-08-11 on cc-08 (alpha, worker
    # Body, SID 7f7f3513): `--goal-status in-progress --full` returned 240 rows,
    # claimed_by == "alpha" on ALL 240, spread across FIVE distinct
    # claimed_by_sid values (182/37/19/1 belonging to other live alpha Bodies,
    # and exactly 1 mine). An agent-name comparison is `alpha != alpha` there —
    # it matches nothing and reverts 239 of a partner Body's goals. The sibling
    # stranded-claim-sweep already compares claimed_by_sid for this reason
    # (g-115-3176 / g-115-4004); GS-1 is inheriting that fix here.
    #
    # Both fields ARE in the --full projection (verified live, non-null on
    # 240/240) — check that before trusting this cascade, because a missing key
    # makes every test below false, skips nothing, and reverts EVERYTHING. The
    # default projection carries neither (guard-1242 class).
    #
    # A goal claimed by an EARLIER, now-dead Body of this agent is deliberately
    # NOT reverted here: its sid is not ours, so the first test skips it. That
    # is the safe direction and it is not a gap — reclaiming genuinely stranded
    # claims needs a liveness/age judgement, which is stranded-claim-sweep's
    # job, not a stop path's. GS-1 reverts what it owns and leaves the rest.
    # Written as ONE decision point on purpose. An earlier draft of this fix
    # nested the sid test inside an `IF sid is non-null` and then continued with
    # `ELSE IF claimed_by ...` at the outer level — which reads two ways
    # depending on which IF the ELSE binds to, and one of those readings reverts
    # everything. Never leave a dangling-else ambiguity in a destructive branch:
    # select the signal first, decide once, act once.
    FOR EACH returned goal:
        # Pick the MOST SPECIFIC ownership signal this goal carries. Exactly one
        # arm fires, and each arm answers the same question: is this goal MINE?
        IF goal.claimed_by_sid is non-null:
            ours   = (goal.claimed_by_sid == <this session's SID, $MIND_SID>)
            reason = "claimed by another Body {goal.claimed_by_sid}"
        ELSE IF goal.claimed_by is non-null:
            ours   = (goal.claimed_by == <agent>)
            reason = "claimed by {goal.claimed_by}"
        ELSE:
            ours   = (goal.id NOT in partner_claimed)
            reason = "partner-claimed"
        IF NOT ours:
            Output: "  Skipped {goal.id} ({reason} — not ours to revert)"
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
# D6.6 (session-telemetry WP3, 2026-06-03): Finalize the durable per-session
# telemetry record at world/telemetry/session-records/<agent>/<SID>.json with
# status=completed, ended_reason=graceful-stop. The record lives under world/
# so the own-cloud sweep (the D6.7 flush below, then the 120s periodic sweep)
# carries it to S3 cross-machine with NO new infra. write_close reads the open
# (WP1) record to preserve started_at/machine_id and compute duration; if WP1
# was never written it synthesizes from binding.yaml (wp1_missing=True) so the
# session is still captured. Pure library module invoked via `py -3 -c` (NOT a
# .sh wrapper — avoids the no-python-cli-fallback gate; NOT a daemon endpoint —
# the writer must work even when the daemon is dead). guard-165: SID/agent/mode
# pass through ENV VARS, python source single-quoted — never interpolated.
# Fire-and-forget (|| true): telemetry must never block the graceful stop.
Bash: SID=$(cat agents/<agent>/session/latest-session-id 2>/dev/null | tr -d '\r\n'); TMODE=$(cat agents/<agent>/session/stop-target-mode 2>/dev/null | tr -d '\r\n'); [ -n "$SID" ] && TSID="$SID" TAGENT="<agent>" TMODE="$TMODE" py -3 -c 'import os,sys; sys.path.insert(0,"core/scripts"); from _session_telemetry import write_close; write_close(sid=os.environ["TSID"], agent=os.environ["TAGENT"], status="completed", ended_reason="graceful-stop", mode_at_end=(os.environ.get("TMODE") or None))' >/dev/null 2>&1 || true
# D6.65 (g-115-4134): Unconditional git push flush. iteration-push.sh is
# deliberately RATE-LIMITED in the per-iteration path (g-115-1734, USER DIRECTIVE
# user 2026-07-02): it pushes only when ahead >= ITERATION_PUSH_MIN_COMMITS (5)
# OR oldest-unpushed >= ITERATION_PUSH_MAX_AGE_MIN (20). That batching is correct
# mid-loop and WRONG at shutdown — a session whose final commits sit under BOTH
# thresholds leaves them stranded with no later iteration to flush them. Observed
# on ZDS cc-06: omni's last run made 4 commits over 11 minutes (under 5, under
# 20m), stopped, and 7 commits sat unpushed ~12h. Forcing all three thresholds to
# zero converts the batch decision into "push whatever is ahead, now".
# --fetch-interval-min 0 defeats the 10m fetch throttle so the merge/push decides
# against a FRESH origin ref rather than a cached one — at shutdown there is no
# next iteration to correct a stale read.
#
# ORDERING IS LOAD-BEARING — this runs BEFORE the D6.7 S3 flush, not after.
# iteration-push does fetch+MERGE, which mutates git-tracked files under
# agents/<agent>/ (journal.jsonl, experience.jsonl, changelog.jsonl are all
# tracked; only session/ and sessions/ are gitignored). Those paths are inside
# the owned-set D6.7 pushes to S3, so a merge landing AFTER the flush would leave
# local newer than S3 — precisely the machine-move stranding D6.7 exists to
# prevent. Merge-then-flush keeps the two durability channels consistent. Safe by
# construction: on a merge conflict iteration-push runs `git merge --abort` and
# defers, so it never hands D6.7 a conflicted tree.
#
# FAIL-SOFT BY CONSTRUCTION, and the `|| true` is NOT what makes it so. Without
# --strict, iteration-push's soft_exit() returns 0 on EVERY path including genuine
# push/merge failure, so an rc-gated `|| handle_failure` here would be DEAD CODE
# (guard-775). Do NOT add --strict to "improve" error handling: that would let a
# transient network failure abort the stop sequence. The script logs every
# decision to stderr, which is the real visibility channel. The `|| true` mirrors
# iteration-close.sh's production shape as belt-and-suspenders only.
# D6.62 (g-115-4145): commit BEFORE D6.65's push+merge — nothing in D1-D7 ever
# committed, so D4's git-tracked writes sat uncommitted and the push had
# nothing ahead to flush. Fail-soft; rationale in the commit message.
Bash: MIND_AGENT=<agent> bash core/scripts/iteration-commit.sh || true
Bash: MIND_AGENT=<agent> bash core/scripts/iteration-push.sh --min-commits 0 --max-age-min 0 --fetch-interval-min 0 || true
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
# D6.7+D6.8 (flush->VERIFY->release HARD GATE, design §6 — g-115-1339): the two
# steps are fused into ONE Bash call so the flush's rc is in scope for the
# release decision (shell vars do not persist across separate `Bash:` calls).
#   D6.7 FLUSH: push this machine's governed writes to S3 NOW (full owned-set —
#     world/meta/all owned agent dirs incl. agents/<agent>/). By this point ALL
#     continuity files are written: handoff (D4), working-memory (D4.5/D5),
#     execution-diary, session-summary (D6.5). On a non-zero flush, RETRY ONCE
#     (transient S3/contention errors clear on retry). The full-flush scope is
#     the FULL owned-set (NOT narrowed to --agent) — owncloud-flush.sh is
#     backend-agnostic (a no-op on the local backend, a real S3 push under
#     own-cloud). (The per-agent `owncloud-flush.sh --agent <name>` scope exists
#     for the §7 A-stop-B-move acceptance test; agents/<agent>/ is a
#     subset of this full flush, so the §6 "flush the agent dir before release"
#     invariant holds either way.)
#   D6.8 RELEASE: clean RUNNING->IDLE release of the cross-machine DDB
#     session-lock. Under STORAGE_BACKEND=own-cloud the daemon does the real
#     release; on any other backend runner-claim.sh no-ops (exit 0). GATED on a
#     verified-clean flush: release runs ONLY if the flush (after one retry)
#     succeeded. design §6: a failed-flush + release would strand INCOMPLETE S3
#     state — the next machine that acquires would pull a partial agent dir. So
#     on a persistent flush failure we SKIP the release; the claim stays RUNNING
#     and expires via stale-lock-break (~OWNERSHIP_STALE_SECONDS) while the
#     daemon's periodic sweep (120s) keeps retrying the flush. On the local
#     backend the release is a no-op regardless. fail-open: the combined call
#     always exits 0 — its rc cannot break D7's chain.
Bash: MIND_AGENT=<agent> bash -c '
  set +e
  bash core/scripts/owncloud-flush.sh; frc=$?
  if [ "$frc" -ne 0 ]; then
    echo "[stop-flush] D6.7 flush rc=$frc — retrying once before the release decision" >&2
    bash core/scripts/owncloud-flush.sh; frc=$?
  fi
  if [ "$frc" -eq 0 ]; then
    # D6.8 RELEASE — capture rc so a released=False (rc=5, g-328-31) surfaces a
    # WARN + handoff note instead of being buried in a clean-looking exit-0.
    bash core/scripts/runner-claim.sh release --agent "<agent>"; rrc=$?
    if [ "$rrc" -eq 5 ]; then
      # released=False: the DDB claim was NOT confirmed RUNNING->IDLE. Under a
      # wedged daemon this strands a RUNNING self-claim (the g-115-1787 incident);
      # the /start acquire-before-heartbeat reorder now self-heals the next start,
      # but surface loudly (stderr) AND drop a durable handoff note so the operator
      # knows a LIVE-claim release may have been missed.
      echo "[runner-claim] WARN: DDB release UNCONFIRMED (released=False) — claim may be stranded RUNNING if the daemon was wedged. Next /start reclaims a genuinely-stale claim automatically; verify runner-state if this stop was expected to release a LIVE claim. Proceeding with stop." >&2
      echo "$(date +%Y-%m-%dT%H:%M:%S) g-328-31: /stop DDB runner-claim release UNCONFIRMED (released=False) — DDB claim not confirmed IDLE. Next /start reclaims a stale self-claim (acquire-before-heartbeat); if a LIVE claim was expected to release, verify DDB runner-state before restart." >> "agents/<agent>/session/runner-release-warning"
    elif [ "$rrc" -ne 0 ]; then
      echo "[runner-claim] WARN: release rc=$rrc nonzero — claim expires via stale-lock-break (~OWNERSHIP_STALE_SECONDS); proceeding with stop" >&2
    fi
  else
    echo "[stop-flush] WARN: flush FAILED twice (rc=$frc) — SKIPPING runner-claim release per design §6 (a failed-flush+release strands incomplete S3 state). Claim stays RUNNING, expires via stale-lock-break; daemon periodic sweep (120s) keeps retrying. Avoid a machine-move until S3 is confirmed current." >&2
  fi
  exit 0
'
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
# D7.1: Final housekeeping — clear the stop-checkpoint sentinel (the "stop
# complete" marker, FW-11 / g-317-09) AND delete the SID binding. Must run
# AFTER D7 so the PreToolUse hook can resolve MIND_AGENT for D7's mode-set call
# in the event that a future edit drops the explicit prefix (defense in depth
# against prefix-forgetting regressions). Clearing the checkpoint LAST (only
# after D7 has set the target mode) is load-bearing: it is the single signal
# that the stop ran to completion. If autocompact interrupts before this line,
# the checkpoint persists and the Session Start Protocol re-invokes
# /aspirations-graceful-stop --resume to finish. This is the terminal Bash of
# the skill. No text output may follow — return-protocol.md.
Bash: SID=$(cat agents/<agent>/session/latest-session-id 2>/dev/null | tr -d '\r\n'); [ -n "$SID" ] && rm -f ".active-agent-$SID"; MIND_AGENT=<agent> bash core/scripts/stop-checkpoint.sh clear >/dev/null || true
```

## Return Protocol

Does NOT return control to the orchestrator. Control flow ends with D7.1 — a
final single-line Bash command that clears the stop-checkpoint sentinel (marking
the stop complete) and deletes the SID binding file. D7 emits
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

- **Called by**: `/aspirations` orchestrator Phase -1.4 (fresh stop, when `stop-requested` exists); the Session Start Protocol IDLE branch with `--resume` (FW-11, when a `stop-checkpoint.json` is detected after an autocompact-interrupted stop)
- **Calls**: `aspirations-verify`, `aspirations-state-update` (for in-flight obligation completion); `aspirations-consolidate` OR `load-consolidation-housekeeping.sh` (D4); `stop-checkpoint.sh` (write at GS-0 / clear at D7.1); many scripts for D1-D7
- **Reads**: iteration-checkpoint.json, stop-checkpoint.json, stop-target-mode, handoff context
- **Writes**: agent-state (IDLE), agent-mode (target), stop-checkpoint.json (write/clear), various session signals
