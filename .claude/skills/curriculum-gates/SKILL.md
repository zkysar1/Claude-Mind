---
name: curriculum-gates
description: "Evaluates all graduation gates for the agent's current curriculum stage and promotes to the next stage if every gate passes. Gates include metric_threshold, count_check, log_scan, and command_check types. Use whenever /aspirations-consolidate hits session end, /aspirations-evolve runs a mandatory evolution pass, or the agent completes enough goals to plausibly graduate. Promotion unlocks new capabilities (self-edits, forge-skill, parallelism) per curriculum.yaml."
user-invocable: false
triggers: [curriculum, graduation, stage-promotion, gate-evaluation, developmental-stage, cur-stage]
conventions: [curriculum]
minimum_mode: autonomous
revision_id: "skill-bootstrap-curriculum-gates-871349"
previous_revision_id: null
---

# /curriculum-gates — Curriculum Gate Evaluation & Promotion

Evaluates all graduation gates for the agent's current curriculum stage.
If all gates pass, promotes the agent to the next stage and logs the promotion.
Called by `/aspirations-consolidate` (session end) and `/aspirations-evolve` (evolution cycle).

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Evaluate Gates

```
Bash: curriculum-evaluate.sh → parse JSON output

IF configured == false:
    Bash: echo "Curriculum: Not configured — skipping gate evaluation."
    RETURN

IF terminal_stage == true:
    Bash: echo "Curriculum: At terminal stage — all capabilities unlocked."
    RETURN

Output: "Curriculum gate evaluation for stage: {stage_name}"
For each gate in gates:
    Output: "  {gate.id}: {PASS/FAIL} (current: {current_value}, required: {operator} {threshold})"
Output: "Gates passed: {gates_passed_count}/{gates_total}"
```

## Step 2: Promote If Ready — guard-33 Autonomous Self-Escalation Chokepoint

A curriculum promotion is a **capability-unlock self-escalation**: it unlocks
gated capabilities (self-edits, forge-skill, parallelism) and can REVERSE a
prior resolved self.md decision (e.g. "no bidding before Stage 1"). guard-33
forbids the agent self-authorizing such an action. The assistant-mode path
(`/respond` Step 5.0) already routes user-directed escalations through the
email round-trip; **this is the autonomous-origin counterpart so guard-33
holds regardless of origin**. Protocol:
`world/conventions/self-escalation-confirmation.md`.

Autonomous mode MUST NOT block on user input (no-blocking-in-RUNNING-state),
so the pattern is **register + DEFER**, never register-and-wait. The promotion
is gated behind a `confirmed` pending-escalation record and applied on a LATER
curriculum-gates run, once the user's YES has been parsed by the domain
inbox-poll skill (see `world/conventions/self-escalation-confirmation.md`). The
escalation store IS the state machine
(`awaiting_reply -> confirmed | denied | timed_out`) --- there is no marker file;
`agents/<agent>/session/pending-escalations.yaml` is the single source of truth.

This chokepoint sits at the SOLE promotion call site, so it covers ALL callers
(`/aspirations-consolidate` Step 8.6, `/aspirations-evolve` Step 10, and
`/aspirations-precheck` Phase 0.5i curriculum-cadence — g-115-1801) at once.
Note: self.md evolution is NOT routed here --- it is governed by guard-380
(notify-AFTER, pre-approval explicitly NOT required); routing it through this
chokepoint would contradict guard-380. Curriculum graduation is the one
autonomous capability-unlock that reverses a resolved decision, hence gated.

```
IF all_passed == true:
    # -- Resolve the graduation this run would perform (from -> to) --
    from_stage = current_stage          # from Step 1 curriculum-evaluate output
    Bash: curriculum-status.sh -> parse JSON -> to_stage = .next_stage
    IF to_stage is null or empty:
        # evaluate said all_passed but status resolves no next stage (already
        # terminal, or a race) -- nothing to promote.
        Bash: echo "Curriculum: all gates passed but no next stage resolved -- nothing to promote."
        RETURN
    action_text = "graduate curriculum {from_stage} -> {to_stage}"

    # -- Read the escalation store: has THIS graduation already been asked? --
    # pending_escalations.py is a world CLI script (NOT a daemon wrapper), so
    # call it via py -3 per python-invocation.md. Match on escalation_type +
    # the "-> {to_stage}" token in the action so re-evaluating a DIFFERENT
    # graduation later does not collide.
    Bash: source core/scripts/_paths.sh
          STORE="$PROJECT_ROOT/agents/$MIND_AGENT/session/pending-escalations.yaml"
          command -v cygpath >/dev/null 2>&1 && STORE="$(cygpath -m "$STORE")"
          [ -f "$STORE" ] && py -3 "$WORLD_DIR/scripts/pending_escalations.py" list --path "$STORE" || echo '[]'
    records = [r for r in parsed_list
               if r.escalation_type == "curriculum-graduation"
               and ("-> " + to_stage) in r.action]
    latest = records sorted by created_at DESC, first element (or None)

    # -- State machine --
    IF latest is not None AND latest.status == "confirmed":
        # User replied YES (parsed by the domain inbox-poll skill). APPLY.
        # current_stage advances on success, so the NEXT run no longer matches
        # this to_stage -- no double-promotion. The confirmed record stays as
        # the audit trail; it is not consumed.
        Bash: curriculum-promote.sh -> parse JSON output

        IF promoted == true:
            Output: "CURRICULUM PROMOTION (guard-33 confirmed {latest.correlation_id}): {from_name} -> {to_name}"
            Output: "New unlocks:"
            For each unlock in unlocks where value changed:
                Output: "  {capability}: now {enabled/disabled}"

            # Log promotion to evolution log (records the confirming cid)
            echo '{"date":"<today>","event":"curriculum_promotion","details":"Promoted from {from_stage} ({from_name}) to {to_stage} ({to_name}) -- guard-33 confirmed {latest.correlation_id}","trigger_reason":"curriculum-gates evaluation (autonomous, email-confirmed)"}' | bash core/scripts/evolution-log-append.sh

            # E14: Curriculum-stage transition encoding. Routes the promotion through
            # sensory_buffer so the standard encoding pipeline (consolidation Phase 2
            # / state-update Phase 8) selects/creates the right tree node. We do NOT
            # hard-code a target path: promotion is rare (multi-week cadence), and
            # the encoding gate's category_class_multiplier + curator gate handle
            # routing.
            #
            # `curriculum-promote.sh` JSON returns: {promoted, from_stage, to_stage,
            # unlocks (NEW stage's full unlocks map)}. It does NOT return a diff or
            # a stage description. To get the destination stage's description, read
            # `agents/<agent>/curriculum.yaml` and look up the stage where id == to_stage.
            # The `unlocks` already-printed list above ("New unlocks: ...") drives
            # the observation text -- use those same keys, don't recompute.

            Read agents/<agent>/curriculum.yaml -> find stage where id == to_stage
            to_stage_description = that stage's `description` field (one-line)
            unlocked_capability_keys = [k for k, v in unlocks.items() if v == true]

            IF unlocked_capability_keys is non-empty:
                observation_text = "Promoted from {from_stage} ({from_name}) to {to_stage} ({to_name}) on <today> (guard-33 confirmed {latest.correlation_id}). Capabilities now active: " + ", ".join(unlocked_capability_keys) + ". " + to_stage_description
                echo '{
                  "source_goal": "curriculum-gates-promotion",
                  "observation": "<observation_text>",
                  "encoding_score": 0.0,
                  "scores": {
                    "novelty": 0.8,
                    "outcome_impact": 0.7,
                    "surprise": 0.2,
                    "goal_relevance": 0.8,
                    "repetition_strength": 0.1
                  },
                  "target_article": null,
                  "replay_priority": "goal_completions"
                }' | bash core/scripts/wm-append.sh sensory_buffer
                # Fail-open: append errors log to execution-diary but never block
                # the promotion. The evolution-log entry above is the durable record;
                # tree encoding is the value-add.

        IF promoted == false:
            Bash: echo "Curriculum: confirmed but promotion blocked -- {reason}"
        RETURN

    IF latest is not None AND latest.status == "awaiting_reply":
        # Already emailed; awaiting user YES/NO. DEFER (do not re-email).
        # The domain inbox-poll skill's expire-sweep flips it to timed_out at 48h.
        Bash: echo "Curriculum: promotion {from_stage} -> {to_stage} awaiting user YES/NO ({latest.correlation_id}) -- deferring (guard-33). Not promoting."
        RETURN

    IF latest is not None AND latest.status == "denied":
        # Explicit NO. Respect it: do NOT promote, do NOT re-ask. The denied
        # record persists, so every future run lands here and defers. The user
        # re-opens by removing the record if they later change their mind.
        Bash: echo "Curriculum: promotion {from_stage} -> {to_stage} was DENIED by user ({latest.correlation_id}) -- not promoting, not re-escalating (guard-33)."
        RETURN

    # latest is None (never asked) OR latest.status == "timed_out" (48h
    # fail-closed deny -- missed the email; re-ask now). REGISTER + DEFER.
    # Domain-overlay seam: the self-escalation transport (register + inbox-poll)
    # is domain-supplied in world/scripts/. If this world has not wired it,
    # guard-33 fail-closes -- the agent CANNOT self-authorize the unlock, so it
    # defers rather than promoting. It NEVER falls back to a direct promote.
    IF NOT exists "world/scripts/self-escalation-register.sh":
        Bash: echo "Curriculum: gates passed for {to_stage} but self-escalation transport is not wired in this world -- promotion DEFERRED (guard-33 fail-closed). No self-authorization."
        RETURN
    Bash: bash world/scripts/self-escalation-register.sh \
        --action "{action_text}" \
        --escalation-type curriculum-graduation \
        --session "${MIND_SID:-}"
    Parse JSON: {correlation_id, outbound_subject, expires_at, body_correlation_line}.

    Notify the user about the curriculum-graduation confirmation request.
    (Check world/forged-skills.yaml for a skill whose triggers match "notify
     the user" and invoke it with:
       category: decision-needed
       subject:  {outbound_subject from register}
       message:  a short body stating the action ("{action_text}"), its
                 consequence ("unlocks the capabilities gated behind {to_stage}
                 -- self-edits / forge-skill / parallelism per curriculum.yaml --
                 and may reverse a prior resolved self.md decision"), then
                 "Reply YES to approve or NO to deny.", and a FINAL line that is
                 exactly {body_correlation_line} from register (the redundant
                 carrier the parser falls back to if the subject token is edited).
     If no matching skill is registered, fall back to a participants:[agent, user]
     goal via aspirations-add-goal.sh. Never block on notification failure.)
    This notification send is an INTERNAL agent-to-principal message, NOT external
    outreach -- guard-12 / guard-29 per-send sign-off does NOT apply to it.

    Bash: echo "Curriculum: gates passed for {to_stage}; emailed user a guard-33 confirmation request ({correlation_id}). Promotion DEFERRED until YES (autonomous mode never blocks)."
    RETURN

ELSE:
    Output: "Curriculum: {gates_passed_count}/{gates_total} gates passed -- promotion not yet available."
    Output: "Remaining gates:"
    For each gate where passed == false:
        Bash: echo "  {gate.id}: needs {threshold}, currently at {current_value}"
```

**guard-33 invariant (MUST NOT weaken):** the promotion (`curriculum-promote.sh`)
runs ONLY in the `confirmed` branch --- i.e. only when a pending-escalation record
for THIS graduation has reached status `confirmed`, which requires an
email-round-trip YES from USER_EMAIL (a tracked, non-agent-writable channel).
No code path may call `curriculum-promote.sh` outside that branch. A timeout is
treated as DENY (fail-closed); absence of approval is never approval (mirrors
guard-12 / guard-29).

## Chaining

- **Called by**: `/aspirations-consolidate` (Step 8.6), `/aspirations-evolve` (Step 10)
- **Calls**: `curriculum-evaluate.sh`, `curriculum-status.sh` (resolve next_stage),
  `curriculum-promote.sh` (ONLY in the `confirmed` branch --- guard-33),
  `evolution-log-append.sh`, `world/scripts/self-escalation-register.sh`
  (register the graduation escalation), `pending_escalations.py list`
  (read escalation state), the notification forged skill
  (decision-needed confirmation request)
- **Reads**: `agents/<agent>/curriculum.yaml` (via scripts),
  `agents/<agent>/session/pending-escalations.yaml` (escalation state machine)
- **Writes**: `agents/<agent>/curriculum.yaml` (gate_status update),
  `agents/<agent>/curriculum-promotions.jsonl` (on promotion),
  `agents/<agent>/session/pending-escalations.yaml` (via
  self-escalation-register.sh, on first ask)
- **guard-33 enforcement**: promotion is gated behind an email-round-trip YES
  resolved by the domain inbox-poll skill (reply parser + expire-sweep).
  Protocol: `world/conventions/self-escalation-confirmation.md`. This is the
  autonomous-origin counterpart to `/respond` Step 5.0.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `curriculum-evaluate.sh`, `curriculum-promote.sh`, or
`evolution-log-append.sh`. Never end with a text summary of gate status.
