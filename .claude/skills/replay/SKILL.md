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

  ⚠ THIS PREFERENCE AND THE PRIORITY ORDER ABOVE ARE ANTI-CORRELATED, AND
  NOTHING HERE SAYS WHICH DOMINATES. Resolving it rc-first costs the entire
  violation enrichment. Measured 2026-08-19 (zeta, hostname cc-02, uname -r
  6.8.0-137-generic, 588-record pool), replay_count x surprise:

      rc   surp>=6   surp==5   surp<5
       0         1        12      194
       1        67        25      110
       2        31         4       49

  The never-replayed stratum is surprise-POOR (194 of 207 below 5; exactly ONE
  record at rc==0 AND surprise>=6), because spaced repetition is WORKING — a
  high-surprise record gets replayed, so it accumulates rc and leaves the rc==0
  stratum. The two preferences therefore pull apart by construction and will
  keep doing so.
  Cost, measured on the same run: sorting (rc asc, surprise desc) and taking the
  top 8 produced a batch at 30% CORRECTED — the corpus BASE rate of 29.2% — while
  rule 1's own population (surprise>=5) runs 69.0% CORRECTED, a +39.8pp
  enrichment. Sorting rc-first did not weaken the enrichment; it discarded ALL of
  it, and the resulting batch looks perfectly normal.
  RESOLUTION: order by the PRIORITY RULES first and use replay_count only to
  break ties WITHIN a priority band. The spaced-repetition filter (7-day skip +
  `next_review_date`) already prevents re-replaying anything recent, so rc==0 is
  not needed as a freshness guard — it is a tiebreak, and promoting it to the
  primary key silently inverts what this step selects for.

  ⚠ THAT RESOLUTION IS NOT SUFFICIENT WHEN THE BAND IS COARSE, and "within a
  priority band" is exactly where it hides. `surprise` is a small integer, so
  band 1's top tie group is large and the "tiebreak" ends up doing most of the
  selecting — reproducing the rc-first failure through the back door while
  looking like compliance. Measured 2026-08-21 (foxtrot,
  `hostname` LAPTOP-3IOFCNEO, `uname -r` 6.6.87.2-microsoft-standard-WSL2,
  626-record pool): NO record scored `surprise >= 7` at all, so rule 2
  contributed nothing and rule 1's top tie group was the 118 records at
  `surprise == 6`. The anti-correlation PERSISTS INSIDE that single band —
  CORRECTED by replay_count runs rc=0 **50.0%** (n=12), rc=1 **70.4%** (n=71),
  rc=2 **93.9%** (n=33). So an rc-asc tiebreak selects the band's LEAST-enriched
  corner. Following the RESOLUTION literally produced a batch at 30.0%
  CORRECTED against a 27.3% corpus (+2.7pp — the enrichment gone); stratifying
  the same 8 slots across rc 3/3/2 gave 50.0%. FIX: when the top band is larger
  than N, STRATIFY across replay_count rather than sorting by it, and stratify
  on the TIEBREAK axis — never on `outcome`, which is the variable every rate
  below is computed over (selecting on it makes the reported rate circular).
  Check the band's surprise ceiling before trusting the priority sort: if the
  ceiling is also the modal value, the priority rules have already stopped
  discriminating.

  (Do not read this as contradicting guard-2129: rule 1 IS enriched for
  corrections, by 39.8pp, so every batch-scoped corrected-rate remains
  upward-biased and still must be re-measured against the corpus.)

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
#   ^ READ THAT AS COUNTS, NOT RATES — the "second-largest cohort" ranking is an
#   artifact of month SIZE and inverts once normalized. Re-measured 2026-08-11
#   (foxtrot, hostname LAPTOP-3IOFCNEO, uname -r 6.6.87.2-microsoft-standard-WSL2)
#   over the 444 scoreable replay candidates: empty-outcome_detail by month runs
#   2026-04 20/38 = 52.6% · 2026-05 10/43 = 23.3% · 2026-06 6/38 = 15.8% ·
#   2026-07 44/239 = 18.4% · 2026-08 13/84 = 15.5%. July ranks high by COUNT only
#   because it holds 239 of 444 records — by RATE it is among the best months, and
#   the trend since April is monotone-ish improvement that has been flat since June.
#   So the sentence above is right that the gap is not confined to legacy records,
#   but its evidence does not support that: a raw-count ranking over unequal
#   denominators cannot distinguish "this month is worse" from "this month is
#   bigger" (guard-3141 — composition is the denominator that makes a count
#   decomposable). NOTE THE POPULATIONS DIFFER and neither figure supersedes the
#   other: 29.2% is over 459 resolved+archived records, 20.9% is over the 444
#   scoreable REPLAY CANDIDATES (recently-replayed excluded), so do not read
#   29.2% -> 20.9% as a measured improvement. Rate-split by outcome, same run:
#   CONFIRMED 78/344 = 22.7% empty vs CORRECTED 15/100 = 15.0% — so a
#   violation-first batch is drawn from the BETTER-documented half, and a batch
#   scoring 0/10 bare is unremarkable (expected ~1.5) rather than evidence the
#   gate improved. Compare a batch against the CORRECTED row, never the pool row.
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
# OUTPUT SHAPE (bravo, cc-05, 2026-08-13 — cost 2 turns to rediscover): the call
# emits a PRETTY-PRINTED JSON **LIST** wrapping the record, not one JSONL line and
# not a bare dict. So `json.loads(line)` over a concatenation of N calls parses
# ZERO records, and `.get()` on each decoded value raises AttributeError on a list.
# Accumulating N calls into one file needs a streaming `JSONDecoder().raw_decode`
# loop plus a flatten step. Both failures are SILENT in the bad direction — a
# "0 narratives parsed" reads as a finding about the CORPUS (the bare-record rate
# this very block is about) when it is a fact about your parser. Assert the parsed
# count equals the number of ids you asked for before drawing any conclusion.
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
any rate or divergence as a finding, re-measure it over the CORPUS — the union of
`pipeline-read.sh --stage resolved` AND `pipeline-read.sh --stage archived` — or state
explicitly that the number is batch-scoped and not comparable to a corpus average.
`--stage resolved` ALONE IS A SURVIVORSHIP FILTER, not the corpus; the wording
that stood here until 2026-08-28 invited exactly that read.
Measured that day (echo, cc-03): resolved=45 vs archived=1397, union=1442 at ZERO
overlap — resolved-only is 3.1% of the corpus, and records migrate OUT of resolved as
they age, so a resolved-only read is blind to the long-lived evidence BY CONSTRUCTION.
It has already produced one false conclusion here (g-115-5211, "Step 3.6 has never
fired": 0 of 71 on resolved, 114 of 1007 on the union). `--replay-candidates` is not the
corpus either — guard-2148 measured it +22.9pp accuracy-inflated, dominated by the
encoded_via_chronic filter, which excludes a population that is 100% CORRECTED.
A guardrail cannot outvote the instrument it guards — guard-2129 sits in the
guardrail store, and this paragraph is the instrument.

**THE CORPUS RE-MEASUREMENT IS NOT SELF-INTERPRETING — RUN A MEANINGLESS-MARKER CONTROL
IN THE SAME CALL.** Re-measuring tells you the delta; it does not tell you how large a
delta this instrument produces from *nothing*, and a small-but-nonzero result is exactly
where that matters. Split the same corpus on a marker with NO theoretical link to the
hypothesis (title contains a common word, id is even, category name length) and read its
delta as the noise floor. Anything at or below the floor is nothing, however good the
story is.

Measured 2026-08-15 (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic, 535 resolved):
4 of 6 corrected hypotheses in the batch asserted something was *eliminated / clean /
holds / is-not* — a stability-or-absence claim, coherent and connected to an existing
rule (`verify-before-assuming`), which is what made it persuasive. Corpus re-measurement:
**27.1% (23/85) vs 27.8% (125/450), delta -0.7pp, z=-0.14.** The control — titles
containing `the|a|of|to|and|in` — returned **+9.9pp**, roughly 14x the hypothesized
effect. Without the control, -0.7pp reads as "small, maybe real, worth watching"; with
it, the pattern is an order of magnitude below the floor and unambiguously nothing.

Note the control is also a live warning about the corpus: a bare-common-word split moving
~10pp means title-derived splits carry a large confound (likely title LENGTH). Do NOT
chase that number either — guard-1923 (bare-common-word over-match) is exactly this trap,
and the control's job is to be discarded, not investigated.

**Run a SECOND control with no possible confound — the common-word one is not enough on
its own.** Its own caveat above concedes it carries a title-LENGTH confound, which means a
reader cannot tell how much of its delta is noise and how much is that confound; a floor
you cannot decompose is a weak floor.

**Do not hand-roll the batch-vs-corpus comparison — invoke `/compare-batch-vs-corpus-rate`** (forged; its SKILL.md already declares `Called by: /replay --sharp-wave`). It does NOT supply the size-matched GROUP-SIZE PERMUTATION FLOOR this section asks for — treating its output as that floor drops the control (g-115-6627). Build the floor yourself, over `id[11:]` per the correction below.

⚠ **CORRECTED 2026-08-18 (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic,
g-001-05, 570 scoreable candidates, base corrected rate 29.3%). This block used to
prescribe the parity of a checksum over the record id, `sum(ord(c) for c in id) % 2 == 0`,
and asserted it "cannot correlate with length, topic, author, month, or outcome". That
assertion is MEASURED FALSE. Record ids begin `YYYY-MM-DD_`, so ten date characters enter
the checksum — the "meaningless" marker is a deterministic function of the record's DATE,
and date is the strongest predictor of `CORRECTED` in this corpus.** Decisive one-call
experiment; same pool, same construction, only the prefix moved:

| split (identical construction) | delta |
|---|---|
| checksum over the DATE PREFIX ALONE, `id[:10]` | **+6.91pp** |
| checksum over the FULL id (what this block prescribed) | **+7.22pp** |
| checksum over the id MINUS the date, `id[11:]` | **−1.00pp** |
| checksum over the TITLE (contains no date) | **+1.20pp** |

~96% of the "floor" was ten characters of date. **Use `id[11:]`, not `id`** — and prefer a
permutation floor over any single fixed split.

WHY date carries outcome here is a POOL ARTIFACT and must NOT be reported as a change in
resolution practice. Corrected-rate by outcome month runs 0.0% (2026-04, n=18) · 0.0%
(05, n=10) · 0.0% (06, n=35) · 34.5% (07, n=194) · 36.0% (08, n=258). This very skill
produces the early zeros: Step 3.6 encodes chronic-CORRECTED records and the
`replay_candidates` endpoint then EXCLUDES them, so OLD corrected records are
preferentially drained from the pool — measured, 67 pool records at `replay_count >= 3`
and **zero** of them CORRECTED. The pool ages into a CONFIRMED-only tail.

⚠ **THE MECHANISM ABOVE IS CONFIRMED — AND THE MONTH FIGURES IT QUOTES ARE POOL-SCOPED,
WHICH MAKES THEM THE OPPOSITE OF THE CORPUS. Name your population before quoting any
corrected-rate.** Re-measured 2026-08-22 (zeta, `hostname` cc-02, `uname -r`
6.8.0-137-generic, g-001-05) on BOTH arms in one run. The counterfactual the paragraph
above never had: `encoded_via_chronic` is TRUE on **125** records in the full
resolved+archived store and on **ZERO** of the 664 pool records — so the drain is real and
complete, not inferred. But run the same by-month split on the full store and the sign
flips:

| | 03 | 04 | 05 | 06 | 07 | 08 |
|---|---|---|---|---|---|---|
| full store (n=863) | 60% | 51% | 50% | 42% | 43% | **39%** |
| replay pool (n=623) | — | **0%** (n=35) | **0%** (n=20) | **0%** (n=42) | 37% | 36% |

The corpus runs **OLD = MORE corrected**; the pool runs OLD = *zero*, because every
pre-July CORRECTED record has been encoded and excluded. So "date carries outcome" is
true in both arms **in opposite directions**, and a reader who computes corrected-rate
by month without naming the population gets the opposite answer with no error to warn
them. That is why `id[11:]` remains the right control — it strips the date either way.

**Consequence for this step's headline number.** Base corrected is **30.5% (pool)** vs
**42.6% (full store)** — the arms differ by **12.1pp before any marker is tested**. The
rule-1 (`surprise>=5`) enrichment this series has recorded four times as
+39.8/+39.2/+39.0/+38.5pp is therefore POOL-SCOPED; the same band against the true
resolved corpus is 72.9% vs 42.6% = **+30.2pp**. Real either way — a magnitude
correction, not a refutation. Step 3's own instruction already says "re-measure over the
unfiltered **resolved corpus**"; the recorded executions used `--replay-candidates`. Read
the resolved+archived union (`pipeline-read.sh --stage resolved` + `--stage archived`)
for any RATE.

**Do NOT re-derive the permutation floors from this.** Measured the same run, the noise
floor is robust to the arm — balanced p95 **6.85** (full store) vs **7.16** (pool);
size-50 14.18 vs 13.59; size-10 33.02 vs 29.98 — because the floor is a function of group
size and n, not of the base rate. Fix the denominator of the RATE and leave the floors
alone. (guard-4757.)

THE REPLICATION WAS THE TRAP, and it is the reusable half. +6.7pp (foxtrot, `hostname`
LAPTOP-3IOFCNEO, 527 records) / +7.5pp (bravo, cc-05, 528) / +7.2pp (zeta, cc-02, 570) across three
boxes read as "a stable property of this corpus, not one box's artifact" — and it IS
stable, but the stable property is a CONFOUND, not a floor. Three probes agreeing because
they share one construction are ONE probe (sig-222). Cross-box agreement tests
PORTABILITY; it cannot test VALIDITY. (Common-word replicates the same way: 9.7 / 9.9 /
13.2 / 13.0pp.)

**The honest floor is a permutation distribution, not one split.** 2000 random balanced
splits of the same 570-record pool: |delta| median **2.57pp**, p90 **6.29pp**, p95
**7.57pp**, p99 **9.72pp**, max 13.68pp. Only **6.3%** of random splits reach the 7.22pp
the checksum returned — the prescribed control was not sampling the middle of the noise
distribution, it sat near its 94th percentile. Compute the distribution for the pool in
front of you and use its **p95 as the bar**; it is ~10 lines and it re-derives per corpus
instead of inheriting a constant (guard-1511 — a threshold swept against one corpus
snapshot does not travel).

DIRECTION OF THE ERROR, so nobody over-corrects it later: the old floor was ~3x too HIGH
(7.2pp against a 2.6pp median), so it **discarded real signal and never manufactured
any**. Markers previously rejected as "below the checksum floor" are UNDETERMINED, not
refuted — digit-in-title −2.0pp, comparative-title +3.3pp (n=10), and
resolved-after-`resolves_by` −9.1pp, the last of which is above p95 and worth re-testing.
Today's hypothesized marker (`position` states a threshold/aggregate, **+4.8pp**, n=250
vs 320) stays DISCARDED on the better evidence: **21.2%** of random splits reach 4.8pp.

Carry the number, not just the method: **on this corpus a random split moves ~2.6pp at the
median and reaches ~7.6pp at p95 (2000 permutations, 2026-08-18).** Do not inherit
"6-10pp", and never use a single fixed "meaningless" split as the floor — the one this
block used to prescribe was ~96% date.

⚠ **PERMUTE AT THE MARKER'S GROUP SIZE, NOT BALANCED — and note this error runs the
OPPOSITE way from every other correction above.** "2000 random *balanced* splits" is the
floor for a 50/50 marker. A marker DISCOVERED IN THE BATCH is almost never 50/50: the
batch is 10 records, so anything it surfaces is rare in the corpus, and the noise floor
for a rare group is several times the balanced one. Measured 2026-08-19 (alpha, `hostname`
cc-04, `uname -r` 6.8.0-137-generic, g-001-05, 564 scoreable candidates, base corrected
rate 29.4%, 2000 permutations per row):

| group size | median \|delta\| | p95 |
|---|---|---|
| 5 | 10.66pp | **30.84pp** |
| 7 | 13.59pp | **29.80pp** |
| 10 | 9.60pp | **29.96pp** |
| 20 | 5.77pp | 20.15pp |
| 50 | 5.01pp | 13.79pp |
| 100 | 3.12pp | 10.25pp |
| 282 (balanced) | 2.84pp | **7.80pp** |

The balanced row REPLICATES the 2026-08-18 zeta figure (7.80 vs 7.57pp, different pool,
different box, same construction) — so it is the positive control, not a rival number. At
n=7 the bar is **3.8x** higher. Live instance from the run that measured this: the batch
suggested "the narrative reports a defect in the MEASUREMENT INSTRUMENT rather than in the
claim" (2 of 10 in-batch vs 1.2% of the corpus). Corpus re-measurement gave **+28.06pp
(n=7 vs 557)** — a 3.6x exceedance of the balanced p95, and it would have been ENCODED.
Against the size-matched floor, **20.3%** of random splits reach it: nothing. Every other
correction in this block made the floor too HIGH (discarding real signal, the safe
direction); this one makes it too LOW, so it MANUFACTURES findings — the direction that
puts a fabricated lesson into the stores. Compute the floor at the size your marker
actually has. (guard-4363; extends guard-3858.)

⚠ **REPORT THE EXCEEDANCE PROBABILITY, NOT THE p95 COMPARISON — at small n the test above
degenerates.** A group of n records can take only n+1 distinct corrected-rates, so the delta
is QUANTIZED and the size-matched p95 lands *on* an attainable value; `|delta| >= p95` then
resolves on a tie. Measured 2026-08-20 (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic,
584 narrative-bearing candidates, base 30.5%): a batch-discovered marker gave **+36.60pp at
n=3 against a size-matched p95 of exactly 36.60pp** — the boolean printed SURVIVES while
**20.9%** of random splits reached the same value. One in five is noise. Compute
`fraction of permutations with |perm_delta| >= |delta|` and treat anything above ~5% as
nothing, whatever the p95 says.

**De-circularize BEFORE computing any of this.** A marker discovered inside a
violation-enriched batch is partly measuring its own selection. Re-run with the batch
EXCLUDED from both arms: on the run above, only 2 of 5 in-group records were batch members,
and dropping them took n from 5 to 3 — which is exactly what exposed the quantization. If the
marker has NO members outside the batch, it is a pure selection artifact; encode nothing.
Same run, both controls replicated and are worth keeping as the instrument's self-check:
balanced p95 **7.19pp** (vs 7.57 / 7.80 on two other boxes) and the date-free `id[11:]`
checksum at **−2.04pp** (vs −1.00pp).

⚠ **DE-CIRCULARIZATION IS NOT ONLY A SMALL-n CORRECTIVE — IT CAN REMOVE MOST OF THE EFFECT
SIZE AT A GROUP SIZE WHERE QUANTIZATION IS NOT IN PLAY.** The paragraph above reaches for it
because dropping batch members took n from 5 to 3 and exposed the quantization; that framing
invites a reader with a comfortably-sized group to treat the step as optional. Measured
2026-08-24 (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic, g-001-05, 890 scoreable
resolved+archived, store base **42.9%**): a marker discovered in a 10-record batch —
*the CORRECTED verdict landed on the FRAMING rather than the substance* (premise dissolved /
weakest conjunct / substantive finding intact) — measured **+12.90pp at n=18**. Only **3** of
those 18 were batch members. Dropping them gave **+3.88pp at n=15**: de-circularization alone
removed **70% of the effect** while leaving the group comfortably large. Three of eighteen is
not a small-n problem; it is 17% of the group carrying most of the signal, and nothing about
n=18 warns you.

Two calibration anchors from the same run, both worth carrying:

- **The size-matched MEDIAN, not just p95, is worth printing.** At n=15 the floor was
  median **9.69pp** / p95 **24.22pp**, so the de-circularized +3.88pp sat *below the median* —
  exceedance probability **79.1%**, i.e. four in five random splits reach it. A p95-only
  comparison tells you the marker failed; the median tells you it was not close, which is what
  stops a reader relitigating it next cycle.
- **Compare the marker against the date-only control directly.** The `id[:10]` date checksum
  returned **+3.18pp** on the same corpus — indistinguishable from the hypothesized marker's
  +3.88pp. The most persuasive thing in the batch performed about as well as ten characters of
  date. That one-line comparison is faster to read than any permutation table.

⛔ **AND THE MARKER ABOVE WAS ITSELF SEMANTICALLY CIRCULAR — READ THIS BEFORE COPYING ITS
METHOD.** It was matched against the RESOLUTION NARRATIVE (`outcome_detail` and its nine
fallback keys), which is written AFTER and ABOUT the outcome, so the predicate is a linguistic
proxy for the very variable being measured. `guard-4758` forbids exactly this and says
plainly that **de-circularization does not catch it**: excluding the batch removes SELECTION
circularity and is silent on SEMANTIC circularity. Compute a marker over PRE-RESOLUTION fields
only — `title`, `claim`, `rationale`, `measurement_channel`, `position` — and report THAT
number as the verdict.

⚠ **THAT FIVE-FIELD LIST IS ITSELF CONTAMINATED, AND ITS TWO RICHEST FIELDS ARE THE WORST.
Prefer `title`.** Verdict-token census 2026-08-24 (alpha, `hostname` cc-04, `uname -r`
6.8.0-137-generic, g-001-05) over the resolved+archived union, 1372 records, regex
`\b(CORRECTED|CONFIRMED|FALSIFIED|UNRESOLVABLE|REFUTED)\b`, as a fraction of records where
the field is present and non-empty:

| field | verdict-token | share |
|---|---|---|
| `title` | 7/1372 | **0.5%** |
| `position` | 65/1322 | 4.9% |
| `claim` | 69/1133 | 6.1% |
| `measurement_channel` | 175/948 | **18.5%** |
| `rationale` | 239/1019 | **23.5%** |

So a marker over `rationale` or `measurement_channel` can key on the VERDICT WORD ITSELF on
about one record in five — the exact semantic circularity the rule above forbids. Following
that sentence literally routes a reader OUT of the narrative and INTO two fields that carry
the narrative's verdict anyway. Among the 666 scoreable records carrying a rationale, 191
(28.7%) are contaminated.

**The contamination is a CONSEQUENCE OF A WORKING GATE, which is why it will keep accruing
and why the fix is stripping rather than scolding.** Step 2's own fallback chain resolved 4
of 10 batch records to `narrative_key='rationale'` on the run that measured this, and the
write-time resolution-evidence gate (guard-870 / guard-1126) is satisfied by `rationale`
alone — so agents write the post-hoc verdict there legitimately. Prefer `title`, then
`position` / `claim`; if a marker MUST use the contaminated two, strip verdict tokens first
or drop contaminated records from BOTH arms and report the reduced n.

Do NOT re-derive the accompanying outcome delta: CORRECTED inside contaminated rationale
37.2% (n=191) vs 44.2% clean (n=475) = **−7.04pp**, below the size-matched p95 of **8.38pp**
at exceedance **9.0%** (2000 permutations at n=191), against a pure-date `id[:10]` control of
+3.40pp. **Nothing.** Recorded so the next reader does not spend the hour. The CENSUS is the
finding and needs no floor — it counts fields, not outcomes. (guard-4758, amended.)

The finding above survives the objection, in one direction only: semantic circularity INFLATES
a marker's apparent effect, and this one came back at exceedance **79.1%** even so. A marker
that is nothing under an upward-biased method is still nothing. Do NOT read that as licence —
had it come back positive, the number would have been uninterpretable and the de-circularization
step would have signed off on it.

**Why this is in the instrument and not only in the guardrail.** `guard-4758` was written
2026-08-22 by zeta on cc-02 from a run of THIS SAME recurring goal, and on 2026-08-24 the same
agent on the same box ran a narrative-derived marker again — because nothing in this block said
not to, and the block is what a reader follows at the moment of use (`guard-1984`: a guardrail
cannot outvote the instrument it guards). Its `times_helpful` was **0** against `times_active`
**3** at that moment: firing, and not reaching anyone. That is also `guard-4070` — *retrieve
BEFORE you measure, not after* — landing on the one decision point
`retrieve-before-deciding.md` does not list, since "I am about to spend an hour measuring" is
not a write and no dedup check can refund the hour.

Controls replicated a fourth time: date-free `id[11:]` **−0.71pp** (vs −1.00 / −2.04), balanced
p95 **6.35pp** (vs 7.57 / 7.80 / 7.19), store base **42.9%** (vs 42.6%). Also note the
pool-vs-store base gap has WIDENED to **16.4pp** (26.5% pool vs 42.9% store) against the
12.1pp recorded 2026-08-22 — the chronic drain the block above describes is still running, so
re-measure the gap rather than inheriting it, and keep naming the population on every rate.

```
1. SHARED CONDITIONS in corrected hypotheses:
   Group all corrected hypotheses
   Extract common antecedents (conditions, strategy used, timing)
   Flag: "N of M corrected hypotheses shared condition X"

2. STRATEGY PERFORMANCE by pattern signature:
   ⚠ "matched in this batch" HAS NO FIELD TO READ. Measured 2026-08-06 (zeta),
   re-confirmed 2026-08-28 (echo): NO pipeline record carries a pattern-signature
   reference — the only signal-ish keys are origin_signal / resolution_signal /
   settling_signal. This step is not usually-empty, it is structurally unreachable
   from the pipeline store, so a number here is not a small sample — it is no sample.
   USE THE JOIN THAT EXISTS rather than inventing one:
     - retrieved-and-applied signatures → aspirations-spark Phase 6.5's
       pattern-outcome block, joined via retrieval-session.json supplementary_detail
     - hypothesis-linked signatures → reflect-on-outcome's ABC chains
   Both are wired and working; the gap is specific to the replay-batch path.
   Then, for each signature the join returns: matches attempted, matches confirmed,
   accuracy; flag divergence > 10pp from historical. Read guard-4715 FIRST —
   outcome_stats counters are NOT monotonic (a reset truncates any rate over them),
   so diff history.py snapshots rather than reading counters live.
   PRICED so the next reader need not re-derive it: the alternative remedy — stamp a
   signature ref onto each pipeline record at formation — needs a store-spec change,
   a formation-time writer, and a backfill over 1442 records, and spec.validate runs
   over the WHOLE record (guard-2475). That is the expensive branch; this is the
   cheap one, and it is sufficient for what Step 3 actually asks.

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

SCHEMA NOTE (verified 2026-05-27, g-115-1104; CORRECTED 2026-08-19, g-001-05,
zeta/cc-02): this note used to read "there is NO stored `reconsolidation_updates`
field on pipeline records." That is FALSE as written and a reader who greps will
find the field and distrust the rest of the note. Measured over the 588-record
replay-candidate pool: `replay_metadata.reconsolidation_updates` is PRESENT on
164 records (27.9%) and NON-EMPTY on 3. So the field exists but is inert — which
leaves the note's actual CONCLUSION intact and is why the correction is a
premise-swap, not a reversal: a key that is empty on 98% of the records carrying
it cannot serve as an idempotency guard, whether or not it exists. State the
measured reason, not the absent-field reason.
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
Report BOTH numbers: "stamped N, verified M". Only M is a measurement — and only
when the instrument producing it is INDEPENDENT of the write. Before using any
filtered or derived surface as a read-back, ask whether a field this step just
wrote appears in that surface's OWN selection criteria. If it does, that surface
cannot verify the write at ANY value. Here it does: `--replay-candidates` excludes
on `next_review_date > today` (mind_api/src/world/pipeline.py:283) and on
`replay_count >= 5` (:272), and this step sets `next_review_date = today+7`, which
is ALWAYS > today. So a batched `--replay-candidates` read-back is GUARANTEED to
report M=0 on a fully successful stamp — absence CAUSED BY success, an inverted
signal rather than a lossy one, which no amount of inspecting the reader's field
list reveals. Do NOT substitute it for the per-id loop above to save N calls: that
is the natural optimization and it is precisely what breaks. The per-id
`pipeline-read.sh --id` above is the correct instrument because record identity
does not depend on any field this step writes. (guard-1755; measured 2026-08-07,
echo, cc-03, g-001-05: "stamped 10, verified 0" with all ten writes landed, where
the repair invited by M=0 is a re-stamp that double-increments `replay_count` on
ten healthy records and pushes them toward the `>= 5` archive cap early.)
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
