---
name: aspirations-verify
description: "Phase 5 of the aspirations loop: verifies a just-executed goal using hypothesis outcomes, unified outcome/check evaluation, Q1-Q4 escalation, streak tracking, and dependent-goal unblocking. Use when /aspirations-execute finishes and the loop must confirm the goal met its criteria before state-update fires. Internal sub-skill with two callers: the aspirations loop, and worker-loop Phase 4a at scope=own-unit; assumes the just-executed goal is still in working context."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, goal-schemas]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-verify-c60403"
previous_revision_id: null
---

# /aspirations-verify — Goal Completion Verification

Verifies whether a goal achieved its desired end state after execution.
Handles hypothesis goals, unified verification checks, and the Q1/Q2/Q3
structured escalation protocol for goals with empty checks.

## Abbreviation Policy

Mandatory writes for this obligation: see `core/config/obligation-schema.yaml`
→ `obligations.verify`. Abbreviation is permitted only when one of the
`abbreviated_allowed_when` conditions holds (routine outcome, or
`context_budget.zone == tight`). Zone is distance-to-autocompact, not raw
usage — see `core/scripts/context-budget-status.py` `classify_zone`. When
the tight-zone condition is the basis for abbreviation, `Bash: bash
core/scripts/context-budget-banner.sh` and quote its output before claiming
the condition. When abbreviating on the tight-zone condition, log TWO lines
in the iteration journal entry (in this order):
  `OBLIGATION ABBREVIATED: verify — {condition}`
  `CTX: raw N% | of-autocompact N% | zone tight | headroom N tokens | env ... | updated ...`
The banner line must be the actual output captured from the banner script.
`core/scripts/context-citation-audit.sh` audits the pair. For other
conditions (routine outcome), the banner line is not required.
Then satisfy `minimum_inline`. Otherwise invoke this Skill literally — a
bare status write is NOT a verify. The learning-gate audit (Phase 9.5d)
scans those journal lines and logs false claims to
`agents/<agent>/session/obligation-audit.jsonl`.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

## Inputs (from orchestrator)

- `goal`: The executed goal object (with verification field)
- `result`: Execution result (from Phase 4)
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls
- `prior_checks`: dict (optional, default `{}`) — map of checks already passed in a prior turn that was interrupted by autocompact or graceful stop. Keys: `q1_passed`, `q1_artifact`, `q1_5_passed`, `q1_5_checklist`, `q2_passed`, `q2_failure_mode_checked`, `q3_scope`, `q4_passed`, `standard_checks_passed`. Threaded in by the Phase -1.4 Graceful Stop Handler from `iteration-checkpoint.json`'s `phase_progress` field. See [core/config/conventions/compact-recovery.md](core/config/conventions/compact-recovery.md) "Iteration Checkpoint `phase_progress` Field".

- `scope`: `"full"` (default) or `"own-unit"` (g-306-417) — a worker Body verifying only the goal it just executed (worker-loop Phase 4a, before the mechanical close). Runs each per-goal section below for that goal alone; skips the cross-Body residue (streaks, sampled review). A pass means the criteria were met on THIS Body, never that its code landed (guard-4638). Why: `core/config/rationale/worker-verify-own-unit.md`.

## Outputs (to orchestrator)

- `goal_completed`: Boolean — did the goal pass verification?
- `aspiration_complete`: Boolean — is the parent aspiration now fully complete? Under `scope == "own-unit"` this is REPORTED only; closing the parent aspiration stays reducer-side.

## Hypothesis Goal Verification

```
if goal.hypothesis_id:
    if result == "CONFIRMED" or result == "CORRECTED":
        if not goal.recurring:
            Bash: aspirations-update-goal.sh --source {source} <goal-id> status completed
            Bash: aspirations-update-goal.sh --source {source} <goal-id> completed_date <today>
            Bash: aspirations-update-goal.sh --source {source} <goal-id> achievedCount <N+1>
        # RECURRING GOALS: do NOT write completed_date, achievedCount, lastAchievedAt,
        # currentStreak or longestStreak by hand — the canonical close path already
        # writes all five, so a hand-write here is a SECOND WRITER. It is not a
        # harmless duplicate: see "Recurring Streak Logic" below (guard-1604).
        Unblock dependent goals
    elif result == "EXPIRED":
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status expired
    else:
        # PENDING — hypothesis hasn't resolved yet
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending
```

## Unified Verification Checks

### Scripted Check Evaluation

Structured `verification.checks[]` entries are evaluated by
`verify-check-eval.sh` (sister script to `predicate.py`). Empty `checks[]`
falls through to the Q1/Q2/Q3 Empty-Checks Escalation Protocol below — the
scripted path handles structured checks only; Q1/Q2/Q3 is the LLM-judgment
path for investigation / novel-research goals that legitimately lack
structured checks.

```
Bash: bash core/scripts/verify-check-eval.sh --goal <goal-id> --all
Read JSON result:
  flags = []:                     all_passed → standard-pass path
  flags = ["checks_empty"]:       fall through to Q1/Q2/Q3 (LLM evidence)
  flags = ["checks_unevaluatable"]: fall through to Q1/Q2/Q3 — same as checks_empty
  flags = ["checks_failed"]:      goal fails verification; mark pending; record blocker if warranted
  flags = ["has_string_checks"]:  structured passed but string checks exist; run Q1/Q2/Q3 too
```

`checks_unevaluatable` (g-115-4849) means the evaluator COULD NOT RUN one or
more checks, as opposed to running them and finding the work undone. `all_passed`
is **null** on that path, exactly as on `checks_empty` — nothing was verified, so
it must not read `true`; nothing failed, so it must not read `false`. A genuine
failure OUTRANKS an unevaluatable one: when both are present the flag is
`checks_failed`, so an unevaluatable check can never launder a real failure into
a fall-through; the tally survives in the separate `unevaluatable_count` field,
which you read rather than inferring from flags.

Why the flag exists, the measured size of the unevaluatable population (46.9% of
145 structured checks, and why a type-name census under-counts it fourfold), and
how wide this fall-through is (~98.7% of the live queue reaches Q1-Q4):
`core/config/rationale/verify-check-unevaluatable.md`.

### Sub-Phase Checkpoint Helper (shared by Q1/Q2/Q3 and standard checks)

Each Q-check and the standard-checks block writes its outcome to
`agents/<agent>/session/iteration-checkpoint.json` via the single-writer wrapper
(g-248-36). This makes verify resumable mid-phase: if autocompact or graceful
stop interrupts after Q1 passes but before Q3 runs, the re-invoked verify
sees `prior_checks.q1_passed` and skips Q1. Pattern (reused below, one line
per write — dotted `phase_progress.<key>` paths deep-merge into existing
phase_progress dict):

```bash
bash core/scripts/loop-state-save.sh update --set "phase_progress.<key>=<value>"
```

The wrapper provides typed-key validation, atomic tempfile+rename, and
deep-merge into nested dicts. The checkpoint auto-deletes at `LOOP_CONTINUE`,
so `phase_progress` resets on any successful iteration.

Each check also appends a diary breadcrumb (`execution-diary.sh append`, JSON
on stdin) so `postcompact-restore` surfaces Q-check outcomes in fresh context.

### Pre-Escalation Retrieval (G2 / R6)

Before answering Q1/Q2/Q3, load verification-related guardrails and
reasoning-bank entries so the answers are checked against accumulated
anti-patterns rather than written from memory.

Per `.claude/rules/retrieve-before-deciding.md` decision point 2 ("verifying
a goal's outcome"), this retrieval is mandatory whenever `checks` is empty
AND no prior_checks shortcut applies. Fail-open: if retrieve.sh errors,
log the failure and proceed to Q1/Q2/Q3 anyway — verify must not block.

```
IF len(checks) == 0 AND NOT (prior_checks.q1_passed AND prior_checks.q2_passed AND prior_checks.q3_scope):
    # Token-overlap match against verification anti-patterns
    Bash: retrieve.sh --category "verification escalation Q1 Q2 Q3 evidence anti-pattern" --depth shallow

    From the returned JSON, surface to the Q1/Q2/Q3 answers:
      - guardrails[] whose rule mentions positive-state, negation-claim,
        artifact-evidence, schema-probe, exhaustive-search, or canonical-probe
      - reasoning_bank[] entries flagged with verification or
        verify-before-assuming categories

    Use the loaded entries to:
      1. Sharpen Q1: does the claimed artifact match an entry's "what counts
         as evidence" pattern? If a guardrail (e.g., guard-346 output-completeness,
         positive-state-gate companion) flags the claim shape, Q1 must run
         the explicit probe before passing.
      2. Sharpen Q2: do any retrieved entries describe failure modes for this
         goal's category that the agent should explicitly check?
      3. Sharpen Q3: do any entries warn about unit-only verification for this
         class of work?

    Diary breadcrumb:
      echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"Verification retrieval: rb=<N> guard=<M> loaded"}' | bash core/scripts/execution-diary.sh append

ELSE:
    # checks[] is non-empty (scripted path) OR all Q-checks already passed via prior_checks.
    # Skip pre-escalation retrieval — checks evaluation is already structured,
    # and prior_checks means a prior turn loaded what it needed.
    Skip
```

### Collaborative Goal Agent-Leg Terminal State (g-305-04)

Fires BEFORE Q1/Q2/Q3, ONLY for a collaborative goal that declares an agent
leg. Lets the agent close its own leg cleanly instead of inventing closure
justification for the user-gated portion (the US-04 anti-pattern: zeta hedged
g-250-80 closure twice because the schema forced "agent done, user pending"
through the single `outcomes` field). Backward-compatible: goals without
`verification.outcomes_agent_leg`, or non-collaborative goals, skip this branch
entirely and verify exactly as before.

```
IF "user" in (goal.participants or []) AND goal.verification.outcomes_agent_leg is a non-empty list:
    # Evaluate ONLY the agent-leg outcomes against in-hand artifacts -- these
    # are the criteria the agent can satisfy WITHOUT the user. Apply the same
    # artifact-checking rigor as Q1 (cite the concrete artifact per outcome;
    # no hedging on observed evidence, communication-clarity.md Rule 6).
    agent_leg_met = every entry in outcomes_agent_leg is satisfied by a cited artifact
    IF agent_leg_met:
        # VALID TERMINAL STATE -- "agent-leg-complete". The agent leg is the
        # authoritative closure criterion for the agent's portion. The user leg
        # (outcomes minus outcomes_agent_leg) is tracked SEPARATELY via
        # user_leg_scope + any follow-up goal -- NOT hedged into this closure
        # and NOT a reason to hold the goal pending.
        all_passed = true
        status = completed
        outcome_note = "agent-leg-complete; user-leg pending (user_leg_scope: <scope or participants:[...,user]>)"
        Assert (Rule 6 form): "Agent leg complete because <artifact> satisfies each
          outcomes_agent_leg entry; user leg (<remaining outcomes>) remains pending,
          tracked via user_leg_scope -- closing agent-leg-complete, not hedged."
        Bash: echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"agent-leg-complete terminal state: agent outcomes met, user leg pending"}' | bash core/scripts/execution-diary.sh append
        SKIP Q1/Q2/Q3 -- agent-leg closure is established. Proceed to streak/attribution (Phase 5.3).
    ELSE:
        # The agent's OWN leg is not done yet -- fall through to the standard
        # Q1/Q2/Q3 escalation below (do not close on a partial agent leg).
        Continue to Empty-Checks Escalation Protocol.
```

### Empty-Checks Escalation Protocol (Q1/Q2/Q3/Q4)

When `len(checks) == 0`, the agent MUST answer three structured questions.
Each Q respects `prior_checks` — a Q already passed in a prior turn is skipped.

**Assertion format requirement**: every Q-answer obeys `communication-clarity.md`
Rule 6 — "X is Y because Z", citing the evidence source. A hedge on an outcome the
execution actually produced ("possibly", "appears to") forces re-verification,
because a reader cannot tell ambiguous evidence from a shallow look.

**Q1 EVIDENCE**: "What concrete artifact (file, output, state change, commit) proves
this goal succeeded?" Must reference a checkable artifact.
- `IF prior_checks.q1_passed`: log `"Q1 skipped (prior checkpoint: artifact={prior_checks.q1_artifact})"`; skip to Q2.
- Else: if references concrete artifact → attempt to verify (Read file, check existence).
  - If artifact verification fails: `all_passed = false`, status → pending.
  - If no concrete reference: `all_passed = false`.
- **On Q1 PASS** (artifact verified):
  ```bash
  bash core/scripts/loop-state-save.sh update --set "phase_progress.q1_passed=true" --set "phase_progress.q1_artifact=<artifact-path>"
  echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"Q1 passed: artifact=<artifact-path>"}' | bash core/scripts/execution-diary.sh append
  ```
- **Positive-state audit on Q1 claim** (verify-before-assuming.md Positive
  File-State Claims): if the Q1 artifact claim references a specific file
  (e.g., "handoff.yaml reflects session N", "config.yaml contains Y"), the
  claim must be backed by an in-turn Read/ls/stat of that file — not narrated
  from prior-session memory. Run:
  ```bash
  py core/scripts/positive-state-gate.py --claim "<Q1 artifact claim>" --evidence "<concatenated in-turn Read/ls outputs referencing the file>"
  ```
  Exit 1 = claim unverified → `all_passed = false`, status → pending, and
  append `verification_gap` to sensory_buffer citing the gate reason. Re-read
  the file before re-verifying. Known false positive → re-call with
  `--override "<justification>"`.

**Q1.5 GENERATED CHECKLIST** (TICKing All the Boxes, 2410.03608 — BRD Gap 15;
runs only when Q1 passed): decompose "did this goal succeed?" into a concrete,
artifact-checkable checklist, then surface verification dimensions the goal's
OWN criteria never declared. Primarily DETECTIVE — its escalation gate is
conservative (covered-criterion failures only); its main product is the
goal-template-improvement signal in `meta/missing-verification-criteria.jsonl`.
- `IF prior_checks.q1_5_passed`: log `"Q1.5 skipped (prior checkpoint)"`; skip to Q2.
- `IF outcome_class == "routine" OR context_budget.zone == "tight"`: abbreviate —
  run a 3-item mental checklist, skip logging, log `"Q1.5 abbreviated ({condition})"`;
  skip to Q2. (The gaps it would surface on routine / at-budget work are low-value.)
- Else:
  1. GENERATE 5–10 yes/no checklist items from `goal.title` + `goal.description`
     + the goal's primary action/skill. Each item is one concrete property a
     correct artifact MUST have — phrased as a checkable yes/no question, NOT a
     restatement of the goal. Span four dimensions: deliverable PRESENCE, content
     CORRECTNESS, INTEGRATION/wiring, and absence of obvious BREAKAGE.
  2. EVALUATE each item against the produced artifact(s) → pass / fail / NA.
     Reuse the evidence already in hand from Q1 plus targeted Read/Grep. Do NOT
     re-execute the goal.
  3. CLASSIFY each item's coverage: is this property already represented in
     `goal.verification.outcomes` or `goal.verification.checks` (semantic match,
     LLM judgment)? COVERED = an existing verification entry would catch its
     failure; UNCOVERED otherwise.
  4. `covered_failures`   = items with result==fail AND coverage==covered.
     `uncovered_failures` = items with result==fail AND coverage==uncovered.
  5. CONSERVATIVE HARD GATE: IF `covered_failures` is non-empty → the checklist
     caught a real miss Q1's single-artifact check was too shallow to see:
     `all_passed = false`, status → pending, append each covered failure to
     sensory_buffer as a `verification_gap`. (UNCOVERED failures NEVER gate —
     they are gaps in how the goal DECLARED its criteria, not current-goal
     failures. This keeps Q1.5 low-risk per the BRD: an over-strict generated
     item can only log, never block.)
  6. RECORD `uncovered_failures` for later goal-template improvement (the BRD's
     "misses → `missing-verification-criteria.jsonl`"). One append per verify:
     ```bash
     # only when uncovered_failures is non-empty
     echo '{"goal_id":"<goal.id>","source":"<source>","category":"<goal.category>","artifact":"<Q1 artifact>","items":[<uncovered item strings>],"note":"Q1.5 generated-checklist gap"}' \
       | bash core/scripts/missing-criteria-log.sh
     ```
     The companion script appends one locked JSONL line to
     `meta/missing-verification-criteria.jsonl` (detective/telemetry only — it
     never mutates goal state; fail-open so it cannot block verify).
  7. On Q1.5 assessed (whether or not items failed):
     ```bash
     bash core/scripts/loop-state-save.sh update --set "phase_progress.q1_5_passed=true" --set "phase_progress.q1_5_checklist=<n_pass>/<n_total>"
     echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"Q1.5 checklist: <n_pass>/<n_total> pass, <n_uncovered> uncovered gap(s) logged"}' | bash core/scripts/execution-diary.sh append
     ```

**Q2 NEGATIVE CHECK** (only when Q1 passes): "What would it look like if this
APPEARED to succeed but actually failed? Did I check for that?"
- `IF prior_checks.q2_passed`: log `"Q2 skipped (prior checkpoint: failure-mode={prior_checks.q2_failure_mode_checked})"`; skip to Q3.
- Else: HARD GATE — if you CAN name a failure mode but DIDN'T check → check NOW.
  - If check reveals problem: `all_passed = false`, status → pending.
  - If vague/empty: soft signal → append `verification_gap` to sensory_buffer.
- **Automated negation check (rb-229 capability-gate extensions, g-115-44/g-115-45 wire-up, rb-245 zero-count extension):**
  Extract any negation claim from the result text or your Q2 failure-mode answer. Three classes:
  - **Knowledge / capability** ("isn't built", "doesn't exist", "can't be done") → exhaustive-search-gate.py
  - **Infrastructure / operational** ("is down", "not responding", "connection refused") → verify-before-assuming-gate.py
  - **Statistical / audit** ("N% have X=0", "0 records have Y", "missing field", "zero-utilization") → zero-count-gate.py (requires a jsonl-field-probe.py run first)
  ```bash
  # ALL gates are Python scripts — invoke with `py` (or `python3`), NEVER `bash`.
  # `bash <file>.py` silently fails: bash tries to parse Python as shell, exits 2,
  # stdout is empty, and the caller can't see `would_block` or `reason`.
  # Knowledge / capability negations:
  py core/scripts/exhaustive-search-gate.py --claim-text "<negation>" --tiers-used "<csv>" --queries-count <N>
  # AND (for infrastructure negations only):
  py core/scripts/verify-before-assuming-gate.py --claim-text "<negation>" --signals-count <N_nonsilent> [--infra-check-run]
  # AND (for statistical/audit negations — rb-245): run probe FIRST, then gate.
  py core/scripts/jsonl-field-probe.py --file <jsonl-path> --field <dot.path>
  #   → capture field_present into <found|missing>
  py core/scripts/zero-count-gate.py --claim-text "<negation>" --file-probed "<jsonl-path>" --field-probed "<dot.path>" --probe-result "<found|missing>"
  ```
  A claim may trigger multiple gates (knowledge + statistical for "no records found with field X"). ALL triggered gates must pass (exit 0). Overlap is intentional defense-in-depth, not a bug.
  Interpret exit codes: `0` = evidence sufficient (or no trigger matched — no-op), pass; `1` = evidence insufficient, treat as Q2 FAIL → `all_passed = false`, status → pending, and append `verification_gap` to sensory_buffer citing the gate reason. If a gate's `would_block=true` is a known false positive, re-call with `--override "<justification>"` (audit trail on stderr is automatic). No negation claim in the result → skip; the gates are no-ops on non-negation text.
- **On Q2 PASS** (failure mode named and checked, no problem revealed):
  ```bash
  bash core/scripts/loop-state-save.sh update --set "phase_progress.q2_passed=true" --set "phase_progress.q2_failure_mode_checked=<one-liner>"
  echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"Q2 passed: failure-mode=<one-liner>"}' | bash core/scripts/execution-diary.sh append
  ```

**Q3 INTEGRATION SCOPE**: "Did I verify at the integration level
(caller → target → side effect), or only the unit level?"
- `IF prior_checks.q3_scope is set`: log `"Q3 skipped (prior: {prior_checks.q3_scope})"`; proceed.
- Else: assess scope → `"unit"` or `"integration"`.
- **On Q3 assessed**:
  ```bash
  bash core/scripts/loop-state-save.sh update --set "phase_progress.q3_scope=<unit|integration>"
  echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"Q3 scope: <unit|integration>"}' | bash core/scripts/execution-diary.sh append
  ```

**Q4 ENTITY-FACT PROVENANCE** (g-357-44): "Do the artifact's factual claims rest
on sources this session actually fetched?" Runs only when Q1 named a concrete
file artifact; skip otherwise.
- `IF prior_checks.q4_passed`: log `"Q4 skipped (prior checkpoint)"`; proceed.
- Else run the sampler. You do NOT choose which claims it checks — that is the
  point. Add `--source-file` when the goal cites a source the artifact must be
  faithful to:
  ```bash
  bash core/scripts/q4-provenance-sample.sh --goal <goal.id> --artifact <Q1 artifact> [--source-file <cited source>]
  ```
- Exit `1` = FAIL (a sampled claim is uncited, decoratively cited, or reversed
  against its source) → `all_passed = false`, status → pending, append each
  finding to sensory_buffer as a `verification_gap`. Exit `0` is `pass` OR
  `skipped` — **not the same answer**; read the verdict and never count
  `skipped` as evidence. No override flag, by design.
- **On Q4 assessed**:
  ```bash
  bash core/scripts/loop-state-save.sh update --set "phase_progress.q4_passed=true"
  echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"Q4 provenance: <verdict>, <sampled>/<total> cluster(s)"}' | bash core/scripts/execution-diary.sh append
  ```
  Why the sample is scripted, why `--session-id` must stay defaulted on a worker
  Body, and why a `cat`-read file reads as uncited:
  `core/config/rationale/verify-check-unevaluatable.md` § Q4.

### Standard Checks

```
IF len(checks) > 0:
    IF prior_checks.standard_checks_passed matches "<N>/<N>" where N == len(checks):
        log "Standard checks skipped (prior checkpoint: <N>/<N>)"
        all_passed = true
    ELSE:
        all_passed = all(check_passes(c) for c in checks)
        passed_count = sum(1 for c in checks if check_passes(c))
        # Record outcome to checkpoint + diary
        bash core/scripts/loop-state-save.sh update --set "phase_progress.standard_checks_passed=<passed_count>/<len(checks)>"
        echo '{"entry_type":"finding","goal_id":"<goal.id>","content":"Verify: <passed_count>/<len(checks)> standard checks passed"}' | bash core/scripts/execution-diary.sh append
```

### On Pass

```
if all_passed:
    if not goal.recurring:
        Bash: aspirations-update-goal.sh --source {source} <goal-id> status completed
        Bash: aspirations-update-goal.sh --source {source} <goal-id> completed_date <today>
        Bash: aspirations-update-goal.sh --source {source} <goal-id> achievedCount <N+1>
    # RECURRING GOALS: do NOT write completed_date, achievedCount, lastAchievedAt,
    # currentStreak or longestStreak by hand — the canonical close path already
    # writes all five, so a hand-write here is a SECOND WRITER. It is not a
    # harmless duplicate: see "Recurring Streak Logic" below (guard-1604).

    # OUTPUT-CENTRIC HANDOFF (arXiv 2603.28990: factual outputs > status)
    # Generate output_summary BEFORE unblocking — dependent goals need it.
    IF source == "world":
        output_summary = one-line factual summary of WHAT was produced/found/changed
        echo "${output_summary}" | Bash: board-post.sh --channel coordination --type handoff --tags ${goal.id},${goal.category}

    Unblock dependent goals (with output passing — see below)
```

### Unblock Dependent Goals (with Output Passing)

When unblocking goals that depend on this completed goal, inject the output
into dependent goals that declared `depends_on` (see goal-schemas.md). This
is bash-enforced via `dependent-unblock.sh` (rb-428 lineage; siblings:
experience-archive-goal.sh, findings-gate.sh, decision-rules-append.sh,
loop-state-save.sh, skill-quality-score.sh, journal-append.sh). The wrapper
walks both world and agent aspirations.jsonl, removes `<completed-goal-id>`
from any goal's `blocked_by`, and prepends `## Predecessor Output (<id>)\n
<summary>\n\n` to the description of any goal whose `depends_on` lists this
id. Idempotent via the marker check; per-goal failures are reported in the
returned `skipped[]` array without aborting the loop.

```
Bash: bash core/scripts/dependent-unblock.sh --goal <completed-goal-id> --summary "${output_summary}"
```

The wrapper stamps `unblocked_by = <completed-goal-id>` and
`unblocked_summary = ${output_summary}` on each dependent goal so consumers
(downstream agents, audit tooling, postcompact-restore) can see which
predecessor produced the injected text. Replaces the prior LLM-orchestrated
loop that was silently drifting (early sessions skipped the description
prepend; the rb-428 family hoists each such loop into a single bash unit).

### On Fail

```
Bash: aspirations-update-goal.sh --source {source} <goal-id> status pending  # retry next cycle
log "Goal executed but verification check failed"

# Breadcrumb: postcompact-restore surfaces the last 10 diary entries to the next turn
echo '{"entry_type":"failure","goal_id":"<goal.id>","content":"Verify failed: <reason>"}' | bash core/scripts/execution-diary.sh append

# Post what was learned from the failure (not just "failed")
IF source == "world":
    failure_summary = one-line summary of what was attempted and why it failed
    echo "${failure_summary}" | Bash: board-post.sh --channel coordination --type release --tags ${goal.id},${goal.category}
```

## Recurring Streak Logic

Recurring goals NEVER set status to "completed" — they stay "pending".
Goal-selector time gate prevents re-selection until `interval_hours` elapses.

**DO NOT WRITE ANY OF THESE FIELDS BY HAND (guard-1604).** `lastAchievedAt`,
`currentStreak`, `longestStreak`, `completed_date` and `achievedCount` are all
written by the canonical close path on a recurring goal. The block below
DESCRIBES what that path computes so a reader can interpret the values — it is
NOT a set of commands to run.

It is worse than a harmless duplicate: `lastAchievedAt` is an INPUT read
immediately before it is overwritten, so a hand-write collapses `elapsed` to ~0
and the close derives a flattering UNBROKEN streak from it (measured: true 19.1h
gap recorded as streak 4, not 1).

**Re-running the close does NOT repair it.** The original timestamp is gone, so
each re-run reproduces the same wrong streak. The suppressed streak-break signal,
not the wrong number, is the real cost. Why:
`core/config/rationale/recurring-streak-hand-write.md`.

```
# DESCRIPTIVE — what the canonical close path computes. Do not execute.
interval = goal.interval_hours (fallback: remind_days * 24, default: 24)
elapsed  = hours_since(goal.lastAchievedAt)   # read BEFORE the overwrite
                                              # (this is why a hand-write corrupts it)
if elapsed is not None and elapsed > 2 * interval:
    new_streak = 1                 # Missed interval — reset
else:
    new_streak = currentStreak + 1
# the close path then writes lastAchievedAt, currentStreak and longestStreak
```

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 5)
- **Calls**: `aspirations-update-goal.sh --source {source}`
- **Reads**: Goal verification field, hypothesis result
