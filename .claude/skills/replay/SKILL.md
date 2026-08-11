---
name: replay
description: "Performs hippocampal replay on resolved hypotheses: compressed sharp-wave review for reconsolidation, reverse-order recency scan, selective encoding-queue replay, category-scoped replay, or domain-transfer bundling. Use whenever the aspirations loop hits the replay cadence, /aspirations-consolidate schedules a replay pass, the user says \"replay recent learning\" or \"cross-reference resolved hypotheses\", or the orchestrator needs to bootstrap cross-domain transfer. Mode selected via --sharp-wave / --reverse / --selective / --category / --domain-transfer."
user-invocable: false
triggers:
  - "/replay"
parameters:
  - name: mode
    description: "--sharp-wave (compressed review), --reverse (recent first), --selective (encoding queue only), --category <cat>, --domain-transfer"
    required: false
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [pipeline, experience, tree-retrieval, reasoning-guardrails, pattern-signatures, handoff-working-memory]
minimum_mode: autonomous
revision_id: "skill-bootstrap-replay-f3b9d2"
previous_revision_id: null
---

# /replay — Hippocampal Replay Engine

Compressed, selective review of resolved hypotheses. Inspired by hippocampal sharp-wave ripples that replay experiences at 20x speed during rest, selectively prioritizing novel, goal-relevant, and high-stakes outcomes.

Based on: Hippocampal sharp-wave ripples (Buzsaki 2015), systems consolidation theory, memory reconsolidation (Nader et al. 2000).

## Quick Links

| Related Skill | Relationship |
|---------------|-------------|
| [/reflect](../reflect/SKILL.md) | Parent — calls /replay during `--full-cycle` |
| [/reflect-on-outcome](../reflect-on-outcome/SKILL.md) | Hypothesis + execution reflection feeds replay candidates |
| [/reflect-on-self](../reflect-on-self/SKILL.md) | Pattern extraction mines replayed hypotheses |
| [/aspirations-consolidate](../aspirations-consolidate/SKILL.md) | Calls /replay during session-end consolidation |

## Parameters

- `--sharp-wave` — Run compressed replay of last N resolved hypotheses (default: 10)
- `--reverse` — Replay in reverse chronological order (recent first)
- `--selective` — Only replay tagged items from working memory encoding_queue (via `wm-read.sh encoding_queue --json`)
- `--category <cat>` — Replay only hypotheses from a specific category
- `--domain-transfer` — Cross-domain replay: find patterns in strong domain applicable to weak domains

Default (no args): equivalent to `--sharp-wave --reverse`

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Select Replay Candidates

```
Bash: pipeline-read.sh --replay-candidates → resolved hypotheses eligible for replay
Bash: wm-read.sh encoding_queue --json  (if --selective mode)
Read core/config/memory-pipeline.yaml → replay_priority_order, max_replay_items

Priority selection (most learning signal first). THE FIELD IS `surprise` — read
it by that exact name, NOT `surprise_level` (g-001-05, measured 2026-08-10):
`surprise_level` is a WRITE-SIDE ALIAS that `core/scripts/pipeline.py:442`
normalizes to `surprise` at write time, so it survives on almost no record. Live
counts over the 464 replay candidates on cc-05: `surprise` present on 425,
`surprise_level` on 1. The canonical field is seeded `"surprise": None` in
DEFAULT_FIELDS (pipeline.py:79) and documented in
`core/config/conventions/pipeline.md`.
Keying on the alias is SILENT and self-concealing: rules 1 and 2 below both go
to zero, so selection falls through to rule 5 and returns a batch of routine
CONFIRMED fillers that looks like a perfectly normal replay. There is no error
and no empty result — the only symptom is a batch with no violations in it,
which is also what a genuinely calm week looks like. Sanity check before
trusting a zero: `surprise>=5` matched 104 of 464 and violations 56 of 464 on
the run that found this.
1. Violations: hypotheses where outcome contradicted expectation (`surprise` >= 5)
2. High-impact outcomes: hypotheses with `surprise` >= 7 or significant consequences
3. Pattern signature mismatches: hypotheses where a pattern was matched but outcome differed
4. EXPLORE/CALIBRATE categories: hypotheses in categories where we're still learning
5. Random sample: 2-3 routine hypotheses (prevents overfitting to extremes)

Apply spaced repetition filter:
  For each candidate, check replay_metadata.last_replayed
  Skip if replayed within last 7 days
  Skip if replay_metadata.encoded_via_chronic == true
    # Chronic-CORRECTED items already encoded as a calibration guardrail by
    # Step 3.6 — re-replaying them yields zero new learning (g-115-1104).
    # As of g-115-1421 pipeline.py's replay_candidates endpoint ALSO excludes
    # these at the source, so they no longer appear in the candidate list;
    # this LLM-side skip remains as defense-in-depth.
  IF replay_metadata.replay_count >= 5:
    # Hard cap (encoded or not): stop infinite cycling. Move to archived,
    # never delete (CLAUDE.md pipeline rule), then drop from candidates.
    Bash: pipeline-move.sh {candidate.id} archived
    Log: "REPLAY CAP: archived {candidate.id} (replay_count >= 5)"
    Skip
  Prefer hypotheses never replayed (replay_count == 0)

Select top N candidates (N = max_replay_items from config, default 10)

   # Add experience-backed candidates
   IF agents/<agent>/experience.jsonl exists:
       Bash: experience-read.sh --type goal_execution
       Bash: experience-read.sh --type hypothesis_formation
       Include experiences with high retrieval_count as additional replay candidates
       Experience candidates complement pipeline-based candidates — they provide
       full-fidelity traces that pipeline summaries may have compressed away
```

## Step 1.5: Load Current Strategy State

Before replaying, load current knowledge to compare against replay memories.

```
Collect unique categories from replay candidates
For each unique category:
  Bash: retrieve.sh --category {hypothesis.category} --depth medium
  # Returns unified JSON with all data stores. Retrieval counters already incremented.

  Cache result — reuse for all hypotheses in same category
```

Use retrieved context to:
- Compare replay memories against CURRENT strategy state (detect drift)
- During reconsolidation (Step 4): know what to reinforce vs. revise
- During domain transfer (Step 5): know source domain strategy to abstract

## Step 2: Compressed Replay (20x Compression)

For each selected hypothesis (max 10 per session):

```
Read the full resolved pipeline record
Read the original evaluation record (scoring, reasoning)

# ⚠ THE OUTCOME NARRATIVE IS NOT ALWAYS IN `outcome_detail` — READ A FALLBACK CHAIN.
# Measured 2026-07-31 (bravo, cc-05) over the FULL resolved+archived population,
# 459 records carrying a CONFIRMED/CORRECTED outcome:
#   `outcome_detail` EMPTY on 134 (29.2%)
#     -> 61 of those (45.5%) carry the narrative under ANOTHER key:
#        resolution_note 20 · resolution 10 · evidence_for 8 · resolution_summary 8
#        · resolution_evidence 7 · reflection_note 4 · outcome_note 3 · actual_outcome 1
#     -> 73 (15.9% of all resolved) have NO narrative under any of 11 keys.
# A reader keyed on `outcome_detail` alone renders BOTH halves as a blank OUTCOME
# line, so "lesson recorded under a different key" and "lesson never recorded" are
# indistinguishable — and a blank OUTCOME reads as the second. That is 61 lessons
# silently dropped per full sweep.
# NOT a legacy artifact: the genuinely-bare 73 are spread 2026-04 (34), 2026-07 (19),
# 2026-05 (13), 2026-06 (7) — this month is the second-largest cohort.
# CONTEXT, so this is not re-derived as a regression: the g-303-15 audit measured
# ~53% missing (`pipeline.py:322`) and the g-303-27 resolution-evidence gate
# (guard-870 / guard-1126) has since roughly HALVED it. The gate is working. What it
# does not do is normalize the key — it is a WRITE-time "is there >=1 evidence
# pointer" check scanning its own chain (`outcome_detail, outcome_notes, rationale,
# verification, links`), which passes on `rationale` alone and never touches the six
# keys above. Read-side normalization is this step's job, not the gate's.
# DO NOT hand-roll the chain — call it (gap-062, forged-by-extension 2026-08-04):
#   bash core/scripts/pipeline-read.sh --narrative --id <hypothesis-id>
# emits {id, stage, outcome, narrative_key, narrative, chars}. `narrative_key` is
# the key the text came from, or NULL when the record is genuinely bare — which is
# exactly the distinction this step needs and a blank string cannot carry. The
# ten-key order lives ONCE in mind_api/src/world/pipeline.py NARRATIVE_CHAIN.
# `--narrative` alone covers the live+archive union; add `--stage resolved` to filter.
outcome_text = the `narrative` field of that call
  # `result` is usually the bare verdict string ("CONFIRMED") — use it for the
  # verdict, never as the narrative. It is deliberately NOT in the chain.
  # Two traps the helper now absorbs, both of which bit hand-rolled variants:
  #   - Do NOT truncate before scanning. A 500-char truncation inside one variant's
  #     own normalizer produced a false 0-of-10 indicator scan (zeta, 2026-07-31).
  #   - Not every narrative value is a str. Measured 2026-08-04 (echo, cc-03) over
  #     351 replay candidates: 6 winning values were LISTS (evidence_for) and 1 was
  #     a DICT (resolution). `.strip()` raises AttributeError on exactly those.
  # CORRECTION 2026-08-04 (g-115-4656, echo, cc-03 / 6.8.0-136-generic): this block
  # previously stated that `--replay-candidates` returns a PROJECTION omitting
  # `resolution`, `resolution_summary`, `resolution_evidence`, `reflection_note` and
  # `actual_outcome`, and instructed a per-record `--id` dereference before concluding
  # a narrative was missing. MEASURED FALSE on this deployment: the endpoint appends
  # the FULL record (mind_api/src/world/pipeline.py, `candidates.append(r)`) and all
  # five keys are present in its output — 351 records carried resolution 19,
  # resolution_summary 37, resolution_evidence 13, reflection_note 7,
  # actual_outcome 42. There is no CLI mirror to differ (core/scripts/pipeline.py has
  # zero `replay` references; the wrapper is daemon-only), so the projection had no
  # second implementation to hide in. The nearby flag that IS a projection is
  # `--summary`, which emits one text line of id/title/stage/outcome only — the
  # likely source of the claim. Cost of the error: 351 needless daemon round-trips
  # per full sweep, to recover fields that were never absent.
IF outcome_text is empty after the full chain AND the full record was read:
    Write the OUTCOME line as "{outcome}, surprise {n} — no lesson narrative
    recorded" — state the absence rather than emitting a blank, so a successor can
    tell an unrecorded lesson from an unread one.

Generate 3-line compressed summary:
  CONDITION: {conditions when hypothesized — category, key signals, data recency, context}
  ACTION:    {what we hypothesized, confidence, strategy used, pattern matched}
  OUTCOME:   {actual result, confirmed/corrected, surprise level, key lesson}

Example:
  CONDITION: Category A, strong signal alignment, fresh data (2min old), 3 confirming indicators
  ACTION:    Hypothesized YES at 0.72 confidence via signal-freshness strategy (sig-001 matched)
  OUTCOME:   CONFIRMED — signals held. Lesson: fresh data + strong alignment = high accuracy

Example (violation):
  CONDITION: Category A, 6 consecutive signals in same direction, data 12min old
  ACTION:    Hypothesized continuation at 0.55 via trend-following (sig-001 matched — WRONG MATCH)
  OUTCOME:   CORRECTED — reversal occurred. DG separation should have triggered sig-002.
             Lesson: extended streaks signal exhaustion, NOT continuation. Stale data compounded error.

   # Dereference experience content for full-fidelity replay
   For each replay candidate that has an experience_ref (pipeline record) or is itself an experience:
       Bash: experience-read.sh --id {experience_id}
       Read the content .md file at content_path
       Use verbatim_anchors for precise CONDITION/ACTION/OUTCOME replay:
       - Anchors provide exact text rather than compressed summaries
       - This enables more accurate cross-hypothesis pattern mining (Step 3)
```

## Step 3: Cross-Hypothesis Pattern Mining

After individual replays, analyze the batch as a whole:

**BATCH IS NOT CORPUS (guard-2129).** Step 1 selects this batch through
`replay_priority_order`, whose rule 1 is violation-first — "hypotheses where outcome
contradicted expectation (surprise >= 5)". The batch is therefore deliberately enriched
for corrections, and every corrected-rate computed below is upward-biased BY
CONSTRUCTION. Items 1 and 2 are where that bites: "N of M corrected hypotheses shared
condition X" and "accuracy diverges > 10pp from its historical average" both read a
violation-first batch rate against a whole-corpus average, so an apparent divergence is
the SELECTION showing through rather than a signal about the strategy. Measured
2026-07-31 (foxtrot, g-001-05): a 10-record batch read as a strong calibration signal;
re-measuring the same signatures across all 252 resolved records showed it was a
selection artifact, and the lesson was retracted before it was encoded. Before emitting
any rate or divergence as a finding, re-measure it over the unfiltered resolved corpus,
or state explicitly that the number is batch-scoped and not comparable to a corpus
average. A guardrail cannot outvote the instrument it guards — guard-2129 sits in the
guardrail store, and this paragraph is the instrument.

```
1. SHARED CONDITIONS in corrected hypotheses:
   Group all corrected hypotheses
   Extract common antecedents (conditions, strategy used, timing)
   Flag: "N of M corrected hypotheses shared condition X"

2. STRATEGY PERFORMANCE by pattern signature:
   For each pattern signature matched in this batch:
     Calculate: matches attempted, matches confirmed, accuracy
   Flag any signature where accuracy diverges > 10pp from its historical average

3. TEMPORAL PATTERNS:
   Check: does batch position correlate with accuracy?
   Check: does time-of-day correlate with accuracy?
   Check: does session fatigue (hypotheses late in session) affect accuracy?

4. CATEGORY CROSS-REFERENCE:
   Group replayed hypotheses by category
   Compare accuracy across categories
   Flag categories performing significantly above or below overall
```

## Step 3.5: Convention Pattern Mining

After cross-hypothesis pattern mining, check if shared conditions in corrected
hypotheses map to missing procedural execution steps (convention candidates).

```
# Prerequisite: only runs if Step 3 found shared conditions in corrected hypotheses
IF no shared_condition groups from Step 3 with N >= 2 corrected hypotheses:
    SKIP convention pattern mining

FOR EACH shared_condition group where N >= 2 corrected hypotheses:
    # Does this shared condition map to a missing execution step?
    # Scan OUTCOME fields for procedural gap indicators
    lesson_texts = [h.outcome_lesson for h in shared_condition.hypotheses]

    procedural_gap_indicators = [
        "should have checked", "forgot to", "didn't verify",
        "missed the step", "would have caught", "if we had run",
        "always need to", "next time must", "should always"
    ]

    is_procedural_gap = any(
        any(indicator in lesson.lower() for indicator in procedural_gap_indicators)
        for lesson in lesson_texts
    )

    IF NOT is_procedural_gap:
        CONTINUE  # Not a convention candidate

    # Slot classification — four-way per core/config/conventions/domain-hooks.md
    # Targeting Guidance. Decision order (check specific before general):
    # outcome-observation → signal-refresh → post-execution → pre-execution → skip.
    IF shared_condition relates to pulling a new outcome metric from real-world
       systems AFTER state update (repo commits, CI pass rate, service health,
       business KPI, process-vs-outcome divergence signal):
        target = "outcome-observation"
    ELIF shared_condition relates to refreshing an input channel BEFORE goal
         scoring (user email/reply, board directive, pending-question silence,
         external queue state):
        target = "signal-refresh"
    ELIF shared_condition relates to cleanup/verification/commit/test AFTER a
         single goal's execution:
        target = "post-execution"
    ELIF shared_condition relates to setup/prerequisites BEFORE a single goal's
         execution:
        target = "pre-execution"
    ELSE:
        CONTINUE  # Unroutable — skip (may indicate a new slot is needed; file
                  # an Idea goal if this recurs across mining passes)

    # Check for existing proposals to reinforce
    Bash: source core/scripts/_paths.sh
    IF file_exists($WORLD_DIR/conventions/convention-changes.jsonl):
        Read convention-changes.jsonl
        similar_proposal = find entry where target matches AND proposed_step is semantically similar
        IF similar_proposal exists AND similar_proposal.status == "pending":
            # Reinforce existing proposal
            Update similar_proposal: reinforcement_count += 1, confidence += 0.15
            Log: "REPLAY CONVENTION: reinforced proposal for {target} — '{similar_proposal.proposed_step.title}' now confidence {new_confidence}, reinforcements {new_count}"
            CONTINUE

    # New proposal from cross-hypothesis pattern
    proposed_step = {
        title: synthesize concise title from shared_condition,
        condition: "IF {shared_condition.common_antecedent}:",
        action: synthesize procedural step from lesson_texts
    }

    hypothesis_ids = [h.id for h in shared_condition.hypotheses]
    echo '{"date":"<today>","type":"add","target":"{target}","proposed_step":<proposed_step JSON>,"source":"replay-pattern-mining","source_hypothesis":"{hypothesis_ids[0]}","source_guardrails":[],"reinforcement_count":1,"confidence":0.5,"status":"pending"}' >> $WORLD_DIR/conventions/convention-changes.jsonl

    Log: "REPLAY CONVENTION: proposed new {target} step from {N} corrected hypotheses sharing condition '{shared_condition.description}'"

# Pass any convention proposals to Step 4 for reconsolidation context
```

## Step 3.6: Chronic-Corrected Strategy Nucleation

Chronic-CORRECTED hypotheses whose claims are about specific systems (zones,
livetests, BTs, memory leaks) reference no named STRATEGY, so Step 4's
reconsolidation loop is a no-op for them — they return to the candidate pool
intact and re-replay forever. The 2026-05-22 survey found 8 of 11 chronic items
(replay_count >= 3) in this state. Step 4 only UPDATES existing strategies; it
never CREATES one from a chronic pattern. This step closes that gap: it encodes
the wrong-prediction shape as a calibration GUARDRAIL, then marks the hypothesis
so it stops cycling. (Refs: g-115-1093, g-115-1104,
agents/echo/reports/chronic-re-replay-encoding-gap-2026-05-22.md.)

SCHEMA NOTE (verified 2026-05-27, g-115-1104): there is NO stored
`reconsolidation_updates` field on pipeline records — the investigation's
"reconsolidation_updates is empty" was a conceptual description, not a field.
The idempotency guard is `replay_metadata.encoded_via_chronic` instead: once a
chronic-corrected hypothesis is encoded here, the flag stops re-processing here
AND makes Step 1's spaced-repetition filter skip it. Dotted field names are
rejected by the pipeline update-field endpoint (pipeline_write.py
`dotted_field_rejected`), so the flag is written via the whole-object pattern
(read replay_metadata, merge, write the whole object back) — the same pattern
Step 4 uses for experience `retrieval_stats`.

```
# g-115-1421: iterate the FULL Step 1 replay-candidate pool (the output of
# `pipeline-read.sh --replay-candidates`), NOT only the top-N batch selected
# for compressed replay above. Chronic rc>=3 CORRECTED items that rank below
# the batch cut never reached this step, so they were never encoded and
# re-surfaced every cycle (~3-5 wasted cycles each until the rc>=5 archive
# cap). Sweeping the full pool encodes each chronic-CORRECTED hypothesis
# exactly once; pipeline.py's replay_candidates filter then excludes it at
# the source on subsequent cycles (defense-in-depth with Step 1's L72 skip).
# SCHEMA: replay_count is stored as a string on some records — coerce to int
# before the >= 3 comparison (int(replay_metadata.replay_count)).
FOR EACH candidate hypothesis in the FULL Step 1 replay-candidate pool
                              WHERE int(replay_metadata.replay_count) >= 3
                              AND outcome == "CORRECTED"
                              AND replay_metadata.encoded_via_chronic is not true:

    # The chronic-CORRECTED hypothesis has no strategy to reinforce/revise.
    # Encode the wrong-prediction shape as a calibration guardrail instead.
    Bash: guardrails-read.sh --category {hypothesis.category}

    IF an existing guardrail already captures "predictions of shape X in this
       category are systematically wrong / apply skepticism" (semantic overlap):
        Bash: guardrails-increment.sh {guard.id} utilization.times_active
        Log: "CHRONIC-CORRECTED ENCODING: strengthened {guard.id} from {hypothesis.id} (replay_count {rc})"
    ELSE:
        # Nucleate a new guardrail. The rule names the prediction shape (from
        # hypothesis.title/question/rationale) and the corrected reality (from
        # the replay OUTCOME lesson). Stdin JSON; id/created auto-set.
        echo '<json>' | Bash: guardrails-add.sh
          rule: "Predictions claiming {claim-pattern from hypothesis} in
                 {hypothesis.category} have been CORRECTED {replay_count}x across
                 replays. Apply skepticism — refuse confidence > 0.5 for this
                 prediction shape until a confirming run reverses the pattern."
          category: {hypothesis.category}
          trigger_condition: "{category-specific signal preceding the wrong prediction}"
          source: "replay:{hypothesis.id}"
          tags: ["chronic-re-replay", "calibration"]
        Log: "CHRONIC-CORRECTED ENCODING: nucleated new guardrail from {hypothesis.id} (replay_count {rc})"

    # Mark encoded so this step + Step 1 stop re-selecting it. WHOLE-OBJECT
    # write — dotted field names are rejected by pipeline-update-field; merge
    # encoded_via_chronic into the existing replay_metadata object.
    updated_rm = {**hypothesis.replay_metadata, "encoded_via_chronic": true}
    Bash: pipeline-update-field.sh {hypothesis.id} replay_metadata '<updated_rm JSON>'
    Log: "CHRONIC-CORRECTED ENCODING: marked {hypothesis.id} encoded_via_chronic=true"
```

The replay_count >= 5 archive cap lives in Step 1's spaced-repetition filter —
the safety net that hard-stops cycling even if this encoding step is skipped.

## Step 4: Reconsolidation Window

When a strategy is recalled during replay, it enters a reconsolidation window. The strategy becomes temporarily "labile" — updatable based on new evidence.

```
For each strategy referenced during replay:
  1. Read the strategy's current state from node articles at any tree depth
  2. Tally replay evidence:
     reinforcing_count = hypotheses where strategy worked as expected
     contradicting_count = hypotheses where strategy failed unexpectedly
     extending_count = hypotheses that reveal new conditions for the strategy

  3. Reconsolidation decision:
     If reinforcing_count > contradicting_count * 2:
       REINFORCE — increase strategy confidence by 0.02
       Log: "RECONSOLIDATION: {strategy} reinforced ({reinforcing}/{total})"

     If contradicting_count >= reinforcing_count:
       FLAG FOR REVISION — the strategy may need updating
       Log: "RECONSOLIDATION: {strategy} FLAGGED — contradictions ({contradicting}/{total})"
       Write revision note to the affected node article

     If extending_count > 0:
       EXTEND — add new conditions or rules to the strategy
       Log: "RECONSOLIDATION: {strategy} extended — new conditions discovered"
       Append new conditions to the strategy article

  4. Update pattern signatures — BUT ONLY FOR SIGNATURES WHOSE OWN `conditions` MATCH:
     "Update pattern signatures" read as a bare imperative is what makes this the
     easiest step in the skill to get wrong. `record-outcome` is a one-liner, the
     instruction sounds like bookkeeping, and there is no write-time complaint.
     Measured 2026-07-31 (bravo, g-001-05, cc-05): three CORRECTED outcomes were
     recorded against sig-40 — ACTIVE, validated, 6/6, accuracy 1.0 — for a batch
     whose instances belonged to a DIFFERENT class, driving it to 6/9 = 0.667 in one
     turn. Two disqualifying signals were already in hand and neither was consulted:
     sig-40's conditions are VERIFICATION-time ("the check returned a POSITIVE/passing
     result") while the instances were FORMATION-time, and the same session had
     minutes earlier written the sentence "sig-40's conditions do not fire on it"
     into guard-900. The degradation is SILENT and self-reinforcing — outcome_stats
     feeds confidence and retrieval weighting, so a wrongly-CORRECTED signature is
     retrieved LESS and the error is less likely to be met again. A signature at
     accuracy 1.0 has the most to lose and shows no discrepancy at write time.
     (This paragraph is here rather than only in guard-486 because guard-486 already
     existed and did not prevent it: guard-1877's lesson — a guardrail cannot outvote
     the instrument it guards — so the rail belongs in the instrument too.)

     BEFORE recording, for each candidate signature:
       a. Read it: bash core/scripts/pattern-signatures-read.sh --active
       b. Restate its `conditions` and name which replayed instance satisfies EACH.
          If you cannot, the instance belongs to a different entry — record it there
          (a guardrail or rb) and record NOTHING here.
       c. A signature matched RETROSPECTIVELY over already-resolved records is not a
          tested prediction and takes NO outcome. Replay reads history; the signature
          was not consulted at the time, so nothing about it was put at risk.
       d. Skip meta-pattern signatures (guard-575) — those resolve via
          reflect-on-outcome, and recording here double-counts.
     THEN record the verdict:
       bash core/scripts/pattern-signatures-record-outcome.sh <sig-id> CONFIRMED|CORRECTED
     If you record in error, restore the prior counts with the whole-object writer —
     record-outcome only increments:
       bash core/scripts/pattern-signatures-update-field.sh <sig-id> outcome_stats '{"total":N,"confirmed":M,"accuracy":A}'

  5. Source node freshness check:
     For each strategy's source tree node:
       IF node.last_updated is older than the strategy's most recent outcome_date:
           Log: "STALE SOURCE: {node_key} last updated {date}, strategy has
                  newer evidence from {outcome_date}"
           echo '{"node_key":"<key>","reason":"<reason>","source":"replay-staleness"}' | wm-append.sh knowledge_debt

   # Update experience retrieval stats for replayed experiences.
   #
   # experience-update-field.sh rejects dotted-path syntax (g-115-529 / g-115-928
   # fail-loud rejection per experience.py:549). Use whole-object JSON: read
   # current retrieval_stats, mutate, write the whole object back in one call.
   For each experience record consulted during replay:
       # Step 1: read current retrieval_stats subobject (may be null/absent for
       # records added before retrieval_stats was a tracked field — default to {}).
       current = $(bash core/scripts/experience-read.sh --id {exp-id} \
                   | py -3 -c "import sys,json; r=json.load(sys.stdin); \
                       print(json.dumps((r if not isinstance(r,list) else (r[0] if r else {})).get('retrieval_stats') or {}))")
       # Step 2: mutate the relevant subkeys.
       useful_flag = "true" if experience content contributed to strategy reinforcement or revision else "false"
       updated = $(echo "$current" | py -3 -c "
import json, sys
s = json.load(sys.stdin) or {}
s['retrieval_count'] = s.get('retrieval_count', 0) + 1
s['last_retrieved']  = '{today}'
if '$useful_flag' == 'true':
    s['times_useful'] = s.get('times_useful', 0) + 1
else:
    s['times_noise']  = s.get('times_noise', 0) + 1
print(json.dumps(s))")
       # Step 3: write whole-object JSON back. experience.py auto-recomputes
       # utility_ratio when retrieval_stats is updated.
       bash core/scripts/experience-update-field.sh {exp-id} retrieval_stats "$updated"
```

## Step 4.5: Stamp Replayed Candidates (g-115-1604)

Spaced repetition depends on each replayed candidate's `replay_metadata`
advancing AFTER the replay. Step 1's filter skips candidates replayed within
the last 7 days (reads `last_replayed`), and `pipeline.py`'s `replay_candidates`
endpoint excludes any candidate whose `next_review_date` is in the future.
Neither field advances on its own — Step 1 only ARCHIVES at `replay_count >= 5`
and Step 3.6 only sets `encoded_via_chronic`. Without this step, every candidate
replayed this cycle RE-SURFACES on the next cycle (the spaced-repetition filter
silently no-ops). Found firsthand during g-001-05 (2026-06-21): the 10 replayed
candidates had to be stamped by hand because no step did it.

Stamp every candidate REPLAYED in Step 2 (the compressed-replay set, ~`max_replay_items`)
— NOT the full Step 1 candidate pool. Skip any candidate already terminal'd this
cycle: those ARCHIVED by Step 1 (`replay_count >= 5`) or marked
`encoded_via_chronic` by Step 3.6 have their own terminal writes; do not
double-stamp.

```
# Compute today + today+7. `date -d "+7 days"` is unavailable on this Windows
# Git Bash (guard-759 sibling) — compute both via py -3 datetime instead, e.g.:
#   dates=$(py -3 -c "import datetime as d; t=d.date(2026,6,21); print(t.isoformat(), (t+d.timedelta(days=7)).isoformat())")
# (pass the run date in; argless date construction is fine in a one-shot script.)
today        = <YYYY-MM-DD>
next_review  = <today + 7 days>
FOR EACH candidate replayed in Step 2 (skip archived / encoded_via_chronic):
    # WHOLE-OBJECT write — dotted field names are rejected by pipeline-update-field
    # (same constraint as Step 3.6). Read current replay_metadata, merge the three
    # fields, write the whole object back. replay_count is a string on some
    # records — coerce to int before incrementing.
    rm = dict(candidate.replay_metadata or {})
    rm["replay_count"]     = int(rm.get("replay_count", 0)) + 1
    rm["last_replayed"]    = today
    rm["next_review_date"] = next_review
    Bash: pipeline-update-field.sh {candidate.id} replay_metadata '<rm JSON>'
    Log: "REPLAY STAMP: {candidate.id} rc={rm.replay_count} next_review={next_review}"

# READ-BACK (MANDATORY). guard-409 already requires it — a SKILL.md step writing
# persistent state that downstream code reads must delegate to a wrapper WITH
# readback verification — and this step was out of compliance with it until
# 2026-08-05. An rc=0 from the writer is not that verification (guard-1404 /
# guard-1870), and a non-null re-read is not either: compare the VALUE (rb-1502).
FOR EACH candidate stamped above:
    Bash: pipeline-read.sh --id {candidate.id}   → live_rm = record.replay_metadata
    verified = (live_rm.last_replayed    == today
            AND live_rm.next_review_date == next_review
            AND int(live_rm.replay_count) == rm["replay_count"])
    IF NOT verified:
        Log: "REPLAY STAMP FAILED: {candidate.id} live={live_rm}"
        Retry the write ONCE, then re-verify.
        IF still unverified: name the id in the Step 6 report under Spaced
        Repetition Stats. Do NOT continue silently — an unstamped candidate
        re-enters the next batch and consumes a slot.
Report BOTH numbers: "stamped N, verified M". Only M is a measurement.
```

Both filters now exclude the candidate for 7 days: Step 1's `last_replayed`
LLM-side skip AND the endpoint's `next_review_date` source-level skip
(defense-in-depth, mirroring the dual Step-1 / Step-3.6 chronic-skip pattern).

**Why the read-back is mandatory: a dropped stamp is self-concealing.** Measured
2026-08-05 (alpha, g-001-05, cc-04): of the 10 candidates replayed on 2026-08-02,
**8 carried `last_replayed=2026-08-02` and 2 still read `2026-07-16`** — two stamps
silently did not land. Both unstamped records were back in the very next batch,
consuming 2 of that cycle's 10 slots (20% of the replay budget) re-reviewing
hypotheses that should have been locked until 2026-08-09. Nothing surfaced the
failure at the time, because a stamp write that does not land produces **no error
and no missing artifact** — its only symptom is the record reappearing in a later
batch, which is exactly what correct spaced-repetition rotation also looks like.
That is what distinguishes this from an ordinary unchecked write: the failure mode
and normal operation have the same signature, so the read-back is the only thing
that can tell them apart.

Note the diagnosis nearly went the other way, and the check that saved it is worth
copying. The first pass found **0 of 10** stamped and read as "Step 4.5 is broken" —
but 8 of the 10 were simply ABSENT from `--replay-candidates`, and absence has two
opposite meanings: chronic-encoded/archived, **or** stamped successfully so that
`next_review_date` sits in the future and the endpoint correctly excludes them.
Dereferencing those 8 by id showed all 8 stamped. A zero whose two explanations
imply opposite actions must be disambiguated before it is believed (guard-1419).

This is Layer-A only (rb-189 — skill docs are not workflow enforcement). It makes
the obligation explicit and checkable at the point of use; it does not enforce it.
A future hardening would move the loop into a wrapper script that exits non-zero on
any unverified stamp, which is what guard-409 actually prescribes.

## Step 5: Domain Transfer Check (--domain-transfer mode)

Find patterns in the strongest domain that could bootstrap weaker domains.

```
leaves_json=$(bash core/scripts/tree-read.sh --leaves)
# Each entry has key, depth, capability_level — extract domain-level capability info
Read agents/<agent>/developmental-stage.yaml → exploration budget allocation

strongest = leaf with highest capability_level (strong domain, EXPLOIT or MASTER level)
weakest = leaf with lowest capability_level (weak domain, EXPLORE or CALIBRATE level)

For each validated pattern/strategy in strongest domain:
  Extract core principle (abstract from domain-specific details):
    "data-freshness signal" → Core: "Fresh data + context detection = high accuracy"
    "dual-filter system" → Core: "Gate hypotheses on context; skip unfavorable conditions"
    "streak exhaustion" → Core: "Long streaks reverse; skip after extended consecutive signals"

  For each weaker domain, ask:
    "Could this abstract principle apply to {weak domain}?"

    The transfer process:
      strong domain "data freshness" → weak domain "equivalent recency signal"
      strong domain "regime/context detection" → weak domain "phase/state detection"
      strong domain "exhaustion detection" → weak domain "mean reversion signals"

  If plausible transfer:
    Bash: echo "SCAFFOLDING: {strong domain} -> {weak domain}: {hypothesis} (Log spark for aspirations: Test {pattern} transfer to {domain})"
    echo '<transfer-json>' | wm-set.sh cross_domain_transfer
Bash: echo "replay phase documented"
```

## Step 6: Replay Report

Write structured output and append to journal:

```
## Hippocampal Replay — {date}

### Configuration
Mode: {mode} | Candidates screened: {N} | Replayed: {N}

### Compressed Replays
| # | Hypothesis | Condition | Strategy | Result | Insight |
|---|-----------|-----------|----------|--------|---------|
| 1 | {id} | {3-word condition} | {strategy} | {outcome} | {lesson} |

### Cross-Hypothesis Patterns
- {pattern description, if any found}

### Reconsolidation Updates
- {strategy}: {reinforced/flagged/extended} — {details}

### Domain Transfers Identified
- {from} → {to}: {hypothesis}

### Spaced Repetition Stats
Hypotheses never replayed: {N remaining}
Next replay due: {date based on 7-day interval}
```

## Chaining Map

| Direction | Skill | How |
|-----------|-------|-----|
| Called by | `/reflect --full-cycle` | After pattern extraction (Step 2.5) |
| Called by | `/aspirations loop` | During session-end consolidation pass |
| Calls | `/research-topic` | When domain transfer generates research question |
| Updates | Pattern signatures via `pattern-signatures-record-outcome.sh` | Outcome stats, new separation markers |
| Updates | Knowledge tree node articles | Reconsolidation updates |
| Updates | Working memory (via `wm-set.sh`) | Cross-domain transfer slot, pattern cache |

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `pattern-signatures-record-outcome.sh`, a tree-node write,
or `wm-set.sh`. Never end with a text summary of the replay.
