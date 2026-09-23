# /replay Instrument Readings

Dated measurement evidence for `.claude/skills/replay/SKILL.md` Step 3
(cross-hypothesis pattern mining). **The METHOD lives in the skill; the EVIDENCE
lives here.** Add new readings to this file, never to the skill.

Extracted 2026-09-15 (g-001-05, alpha, `hostname` cc-04, `uname -r` 6.8.0-139-generic)
under guard-6482: a recurring ritual must not append its dated readings into the skill
file that defines it. `replay/SKILL.md` was **65,596 B against a 65,536 B injection
ceiling** — 60 B over — with Step 3 alone holding 22,582 B (34.4% of the file) of
almost entirely dated series. A skill over the ceiling arrives with its tail silently
absent, and this skill's tail is Steps 4-6, including the MANDATORY Step 4.5 stamp
read-back contract. Fourth instance of the pattern guard-6482 catalogues, after
felt-sense-checkin, run-full-suite-after-deep-code, and agent-completion-report.

Nothing below was rewritten. Everything from `## Step 3` prose as it stood at
extraction is preserved verbatim, including superseded claims and the corrections
that superseded them — the correction chain is the reusable part.

---

## Archive: Step 3 prose as of 2026-09-15 (verbatim)

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
p95 **6.35pp** (vs 7.57 / 7.80 / 7.19), store base **42.9%**. FIFTH 2026-08-29 (zeta, cc-02,
n=942): `id[11:]` −1.40pp, p95 6.55pp, base **43.6%**, `title` contamination **0.5%**. Trust
these; do not re-derive.

⚠ **THE GAP IS NOT WIDENING — that was a direction off TWO points.** Third point 15.3pp;
series 12.1 → 16.4 → 15.3 oscillates ~15pp. The DRAIN half IS real (68 records at
`replay_count >= 3`, **ZERO** CORRECTED — a Step 3.6 zero is the mechanism working).
Re-measure the gap, never inherit it, and name the population on every rate. Detail:
tree `performance/agent-performance/replay-instrument-populations.md` § SIXTH occurrence. Detail: tree node
`performance/agent-performance/replay-instrument-populations.md` § SIXTH occurrence.

---

## 2026-09-15 — alpha, `hostname` cc-04, `uname -r` 6.8.0-139-generic, g-001-05 (replay cycle 91)

**Populations.** `--stage resolved` 45 + `--stage archived` 1740 = union **1785**, zero
overlap (resolved is 2.5% of the corpus — the survivorship filter, live). Scoreable
CONFIRMED|CORRECTED **1080**, corrected 462, base **42.78%**. De-circularized (minus the
10-record batch) **1070**, corrected 457, base **42.71%**. Batch itself 50.0% corrected.

**Verdict-token contamination** (de-circularized, n=1070, regex
`\b(confirmed|corrected|unresolvable|falsified|refuted)\b`): `title` 13 = **1.2%**,
`rationale` 321 = **30.0%**, `measurement_channel` 182 = **17.0%**. Replicates the
2026-08-24 census direction; `title` remains an order of magnitude cleaner.

**Primary marker — PRE-REGISTERED before any rate was read.** Title asserts
persistence/invariance (`recur|persist|stays|remains|still|keeps|unchanged|never|will
not|won't`, word-boundary, on `title` only). IN n=176 **37.50%**, OUT n=894 **43.74%**,
delta **−6.24pp**. Size-matched permutation floor at n=176 (20,000 trials, seed
20260915): p95 **8.04pp**, **EXCEEDANCE 0.1343**. **NOTHING** — and the sign is OPPOSITE
to what the batch suggested (4 of 10 batch rows carried the marker, 3 of those CORRECTED).
A clean instance of guard-2129/guard-2144: the shape was real in the batch and absent in
the corpus.

**Controls, same call.** `id[11:]` parity: A n=552 42.03% vs B n=518 43.44%, delta
**−1.41pp** (series: −0.71 / −1.00 / −1.40 / −2.04 — trust it, do not re-derive).
Common-word-in-title, nearest-size: `within` n=80 +3.83 · `next` n=75 **−11.52** ·
`goal` n=59 **−11.12** · `post` n=53 −1.26 · `live` n=49 +0.15 · `session` n=49 +0.15 ·
`days` n=50 **−15.43** · `class` n=45 +1.81. Three meaningless word-splits clear 10pp.

**Size-matched permutation floor table** (N=1070, base 42.71%, 20,000 trials, seed
20260915), |delta| in pp:

| n | p50 | p95 | p99 |
|---|---|---|---|
| 5 | 17.37 | 42.91 | 57.56 |
| 7 | 14.23 | 28.91 | 43.29 |
| 10 | 12.83 | 33.02 | 37.64 |
| 20 | 7.43 | 22.71 | 28.24 |
| 45 | 5.15 | 14.43 | 19.07 |
| 50 | 4.94 | **13.94** | 18.14 |
| 75 | 4.25 | 11.52 | 14.39 |
| 80 | 3.83 | 11.04 | 14.63 |
| 100 | 3.63 | **10.25** | 13.56 |
| 176 | 2.84 | 8.04 | 10.76 |
| 535 | 2.06 | 5.79 | 7.66 |

INDEPENDENT REPLICATION of guard-2144's 2026-08-19 table on a different corpus (1070 vs
564 records, base 42.71% vs 29.4%): n=100 **10.25 vs 10.25**, n=50 13.94 vs 13.79, n=20
22.71 vs 20.15, n=10 33.02 vs 29.96, n=7 28.91 vs 29.80. n=5 (42.91 vs 30.84) is the
noisiest row and does NOT replicate tightly — do not quote it. This confirms guard-4757:
the floor is a function of group size and N, not of the base rate.

**→ THE CONSEQUENCE THAT BELONGS IN THE SKILL: the 10pp reporting threshold Step 3's
items 1-2 use is BELOW the size-matched floor for every n ≤ 100.** It is conservative in
one direction only. Under 10pp is nothing; over 10pp is UNDETERMINED until the exceedance
probability decides.

**Rule-1 enrichment, corpus — name the contrast.** `surprise>=5` n=461 **72.23%**;
`surprise<5` n=560 **18.04%**. Band-vs-BASE = 72.23 − 42.71 = **+29.52pp**, which
replicates the +30.2pp corpus figure (zeta, 2026-08-22). Band-vs-COMPLEMENT = **+54.20pp**.
Same data, two contrasts — do NOT read the larger as growth. (`surprise` present on 1021
of 1070; `surprise_level` on 2.)

**Pool-ceiling artifact.** Corpus max `surprise` = **10**, modal **4** (n=405). The
candidate POOL's ceiling of 6, observed at Step 1 this cycle, is a POOL fact, not a corpus
fact. This sharpens guard-6131: rule 2 (`surprise>=7`) has no population *in the pool*
because high-surprise records are replayed, encoded and drained out of it — the corpus
does carry them. Do not restate the pool ceiling as a claim about the fleet's work.

**`replay_count` × CORRECTED, corpus** (surprise-bearing, n=1021):

| rc | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| all | 20.42% (377) | 45.45% (264) | 41.51% (106) | 81.37% (204) | 61.54% (39) | 6.67% (30) | 100% (1) |
| `surprise==6` | 78.57% (14) | 66.28% (86) | 69.23% (52) | 96.49% (114) | 66.67% (12) | 12.50% (8) | — |

**NON-MONOTONIC, and it REVERSES against foxtrot 2026-08-21** (rc=0 50.0%, rc=1 70.4%,
rc=2 93.9% in the same band): here rc=0 is the HIGHEST of the first three, not the lowest.
The rc=5 collapse (6.67% / 12.50%) is the archive cap selecting on exhaustion, not on
resolution quality. **Consequence: Step 1's stratification PRESCRIPTION stands, but its
stated REASON — "an rc-asc tiebreak selects the band's least-enriched corner" — does not
hold on this corpus.** The durable reason is stronger: the direction is unstable across
measurements, so any FIXED sort direction is a coin flip on an unknown sign, and
stratifying is the only choice that does not bet on it.

**Step 3.6 cohort.** Pool 824 records; `rc>=3` 75, of which CORRECTED **5** (the eligible
set). All five encoded this cycle and **zero new guardrails nucleated** — every one mapped
onto an existing entry (guard-6629 scope-generalization ×1; guard-2857 corrected-by-its-own
-specification ×3; guard-2242 pointer-shaped evidence gate ×1). 4 of the 5 failed on how
they were written rather than on what happened, replicating guard-2857's founding 4-of-5
on a DIFFERENT population (the chronic rc>=3 cohort, not a replay batch) with ~1 record of
overlap. Mechanism worth carrying: a specification defect is what MAKES a record chronic —
it cannot be resolved by more evidence, so it re-replays until something encodes the shape.

## 2026-09-16 — bravo, `hostname` cc-05, `uname -r` 6.8.0-139-generic, g-001-05 (bravo occurrence 135)

**Populations.** Pool (`--replay-candidates`) **861**: `surprise` present 796, `surprise_level` 2,
pool max `surprise` **8**. Rule 2 had 3 records in the pool this cycle and contributed all 3, so the
09-15 pool ceiling of 6 was a moment, not a constant. Corpus: `--stage resolved` 78 + `--stage
archived` 1745 = union **1823**, zero overlap. Scoreable 1103 (corrected 473, base **42.88%**);
de-circularized **1094** (corrected 466, base **42.60%**).

**Contamination** (de-circularized, n=1094): `title` 15 = **1.4%**, `rationale` 328 = **30.0%**,
`measurement_channel` 190 = **17.4%**. Replicates 09-15 (1.2 / 30.0 / 17.0).

**Pre-registered marker: SUFFICIENCY/EXCLUSIVITY in `title`.** The regex was
`only|alone|sufficient|solely|merely|by itself|on its own|nothing but|no
human/new/other/additional/extra/further|without any/human/additional/new/further`. The
decision rule was fixed in the script header before any rate was read. In the batch, 4 of 10
titles carried the marker and 3 of those 4 were CORRECTED. Corpus result: IN n=82 **37.80%**, OUT
n=1012 42.98%, delta **−5.18pp**. Floor at n=82 (20,000 trials, seed 20260916): p50 3.86, p95
10.64, p99 14.60. **EXCEEDANCE 0.4089: NOTHING.** The sign is OPPOSITE to the prediction, the
third consecutive text marker whose batch shape inverted in the corpus (09-13 universal-negative,
09-15 persistence, 09-16 exclusivity). `only` accounts for 52 of the 82 hits and has two senses:
"ONLY in prose" describes, it does not claim sufficiency. So this failure falsifies the LEXICAL
operationalization more cleanly than the mechanism. Exploratory, NOT pre-registered: without
`only`, n=30, −12.95pp, p95 17.89, exceedance 0.1908. Also nothing, and the same sign.

**Controls, same call.** `id[11:]` parity −0.82pp (exceedance 0.81), continuing the series
−0.71 / −1.00 / −1.40 / −2.04 / −1.41 / −0.82. `id[:10]` date-only parity +4.15pp
(exceedance 0.17). Common-word splits at n≈52-81: `within` +4.66, `next` −8.20, `goal` −9.25,
`post` −1.14, `days` **−12.42**. `days` is a meaningless word, and its exceedance of 0.082 sits
within a hair of the 0.05 line.

**Rule-1 enrichment, corpus.** `surprise>=5` n=482 **71.37%**; `<5` n=563 17.58%. Band-vs-base
**+28.77pp** (replicates +29.52 and +30.2); band-vs-complement +53.78pp. Corpus max 10.

**Temporal (Step 3 item 3).** `formed_at` is present on only **469 of 1094** (42.9%), too sparse to
stand for the corpus. Hour buckets 00-05 / 06-11 / 12-17 / 18-23 UTC: 42.59 / 35.88 / 48.78 /
34.58%, exceedances 1.00 / 0.10 / 0.15 / 0.078. Nothing.

**Categories (the batch's 9).** None clears. Closest: framework-architecture n=203 −6.94pp
(exceedance 0.085) and vinheim-runtime n=12 +24.34pp (exceedance 0.14).

**Step 3.6 cohort.** Pool `rc>=3` **70** (positive control); CORRECTED overall 219. The `rc>=3`
outcomes are CONFIRMED 63, UNRESOLVABLE 4, EXPIRED 3, CORRECTED **0**, so **0** are eligible. The
chronic CORRECTED cohort is fully drained at source.

**Narrative instrument census (canonical `narrative_of`, imported rather than re-implemented).**
Corpus union winners: outcome_detail 1254, **rationale 175 (9.6%)**, resolution_note 163, None
**69**, outcome_note 58, evidence_for 31, resolution_evidence 29, resolution 25,
resolution_summary 10, reflection_note 8, actual_outcome 1. Pool: rationale 64 (7.4%, 62 of them
with a verdict), None 15.

A heuristic split of the 175 corpus `rationale` winners:
- 71 carry a verdict or measurement token in `rationale`.
- **19 carry the real lesson under an UNCHAINED key**: resolution_notes 8, lesson 6,
  reflection_summary 2, resolution_rationale 2, actual_result 1, reflection 1.
- 92 carry neither (34 of those are CONFIRMED or CORRECTED).

This replicates guard-2615 (08-04, 9.8%) and guard-3980 (08-16, 38 of 50 bare records hidden) on a
third population. It also adds the unchained-key half: guard-3980's "bare = None + rationale" rule
throws those 19 lessons away. Batch instance: `2026-08-13_wm-prune-test-red-is-a-live-writer-race`
returned 894 chars of pre-registration rationale, while its 4132-char FALSIFIED verdict sits in
`resolution_rationale`. This was the fourth rediscovery; three guardrails never reached the helper
or the skill sentence. Step 2's sentence is corrected at the point of use, and g-115-10108 owns the
helper fix.

## 2026-09-16T18:5x — zeta, hostname cc-02, uname -r 6.8.0-139-generic (g-001-05 run 72)

**Pool arrivals are BURSTY, so a per-day arrival rate is not a planning basis.** Pool 851
(archived 791, resolved 60). `surprise>=7` is **0** in the pool, so rule 2 is empty. The
never-replayed stratum held s6 **18** and s5 **46**. Run 71 had projected about 35 for s5
(29 left plus ~2.5/day). Of the 64 never-replayed s5/s6 records, **27 carry
`outcome_date` 2026-09-15** and 3 carry 09-16. Those are the boot catch-up
review-hypotheses waves (bravo cc-05, zeta cc-02, foxtrot). One wave outweighs weeks at
the quiet-window rate. So re-count the stratum each run and never subtract from a prior
reading.

**Batch (10, stratified on the surprise axis within rule 1, all rc==0, oldest first):**
5 s6 records came back 5/5 CORRECTED and 5 s5 records 5/5 CONFIRMED. Treat that split as
selection on a post-outcome field, not as a finding.

**Title marker `\bnot\b` (contrastive "X, not Y" framing) is NOTHING.** Batch split was 3/5
CORRECTED vs 1/5 CONFIRMED. The corpus was the union of `--stage resolved` 78 and
`--stage archived` 1745, giving 1823 records. After de-circularizing (batch excluded) the
scoreable CONFIRMED/CORRECTED set was 1093, base CORRECTED 42.8%. Marker group n=331,
delta **+5.8pp**. Permutation floor at n=331 (4000 draws): median 2.0pp, p95 6.4pp,
**exceedance 0.088**, which is above the ~5% bar. Controls: `id[11:]` checksum +1.1pp,
date `id[:10]>=2026-07-02` **−5.4pp**, common-word `the` −2.9pp. The marker moves about as
much as the date control. Encoded nothing.

Stamped 10, verified 10 (per-id `pipeline-read.sh --id`). Step 3.6 eligible: 0.

## 2026-09-17T17:2x — zeta, hostname cc-02, uname -r 6.8.0-139-generic (g-001-05 run 73)

**The never-replayed s5/s6 stratum is not growing.** Due-set pool 827. `surprise>=7` is
**0** in the pool again, so rule 2 stays empty. Never-replayed s5/s6 counted **52** at
selection. Run 72's reading implies 54 after its batch (64 minus its 10 rc==0 picks).
Other agents' replays drain the same stratum, so 52 vs 54 is a net figure, not an
arrival rate. No boot wave like 09-15's landed between the two readings. This batch took
4 of the 52.

**Batch (10): rule 1's top bands stratified on replay_count, never on outcome.** s6 rc0
×2, rc1, rc2; s5 rc0 ×2, rc1, rc2; plus 2 rule-5 routine s4 rc0. Batch CORRECTED 4/10.
Three of the four share one shape: the outcome was decided by a path the hypothesis did
not model (a guardrail reached use through citations; a second reaper path; a deploy base
that is the last SUCCESSFUL deploy). That repeats run 72's shape and is already encoded
(guard-900, guard-1105, guard-6335), so nothing new was encoded. Batch-scoped and
upward-biased (guard-2129).

**Title marker `\b(stays?|still|holds?|keeps?|remains?|persists?|continues?)\b`
(persistence claim) is NOTHING.** Corpus = union of `--stage resolved` 71 and
`--stage archived` 1757, giving 1828. After de-circularizing (batch excluded) the
scoreable CONFIRMED/CORRECTED set was 1097, base CORRECTED 42.8%. Marker group n=131:
38.2% vs 43.4%, delta **−5.21pp**. Permutation floor at n=131: median 3.46pp, p95
8.67pp, **exceedance 0.266**. Controls: `id[11:]` checksum **−7.81pp**, date `id[:10]`
+2.40pp, size-matched common word `on` −6.03pp. The widened marker (adds
`never|will not|won.t`) reads n=177, −7.19pp. The marker moves less than the checksum
control. Before writing, both markers' n, rates and deltas, plus the corpus, scoreable and
base figures, were re-derived from the saved corpus reads, and all reproduced exactly. The
floor and control figures are carried from the pre-write measurement.

**Consequence for sig-244 (stasis-assumption-persists-claim).** Its condition 1 (the
claim predicts a state will still hold) is the only title-derivable half, and on its own
it carries no CORRECTED enrichment in the corpus. The cue rests on conditions 2 and 3: an
ending mechanism the rationale names and prices as a confidence haircut, and that
mechanism's live status left unread at formation. Neither is title-derivable. No change
to sig-244.

**Hindsight-classification catch.** Scoring sig-244's conditions 2-3 against
already-resolved batch records nearly produced a "3/3 CONFIRMED counter-instances" claim.
Classifying a resolved record against a formation-time condition reads the outcome into
the condition (Step 4 item 4c, guard-4758). Caught before any write; no retrospective
outcome was recorded on sig-244.

Stamped 10, verified 10 (per-id, `replay-stamp-verify.sh`; each `replay_count` = prior +
1). Step 3.6 eligible: 0 (positive controls on the same read: rc>=3 65, CORRECTED 197).
Step 3.5 skipped (0 indicators).

## Run 74 — 2026-09-19, zeta, hostname cc-02, uname -r 6.8.0-139-generic (own-cloud)

**The 5/5 "UNRESOLVABLE died at its pre-registered measurement channel" pattern from runs
72/73 did NOT survive pool re-measurement, and nothing was encoded from it.** Runs 72/73
observed the shape in a violation-first BATCH, where every rate is upward-biased by
construction (guard-2129). Re-measured against the POOL: enrichment **+14.3pp at n=63** —
UNRESOLVABLE **39.7%** vs base **25.4%**, with CORRECTED at **35.7%**, only ~4pp behind.
That delta sits INSIDE the size-matched floor of roughly 14pp, so it is not distinguishable
from selection effects, and the CORRECTED arm being nearly as high says the marker is not
picking out UNRESOLVABLE specifically. Population named on every figure above: the pool, not
the batch, not the corpus.

**The ~14pp floor is an ESTIMATE, not a computed permutation floor** — unlike the run
recorded immediately above, which carried median/p95/exceedance from an actual permutation.
That is the one number here worth tightening: if a computed size-matched null at n=63 comes
in materially below 14pp, +14.3pp becomes interesting again and this reading should be
revisited rather than cited as a settled negative.

**Batch composition.** 10 = 6 rule-2 (`surprise >= 5`) + 4 rule-1, STRATIFIED across
`replay_count` rather than sorted by it. Rule 2 was live for the first time in three runs —
runs 72/73 read it as empty, and that reading must not be inherited. Stamped 10, verified 10,
failed 0 (`replay-stamp-verify.sh`, per-id).

**Step 3.6 eligible: 2**, after two consecutive runs of 0 — so its emptiness is not a
standing property and should be re-checked, never assumed. Both were the same shape (predicted
negligible magnitude, CORRECTED because actual was substantial): strengthened guard-398 twice,
nucleated nothing, both marked `encoded_via_chronic` and value-verified.

**Three instrument corrections caught before they became findings.** (1) `while read -r id`
over a join-produced file (no trailing newline) requested 9 of 10, and the self-check
"PARSED == requested" PASSED at 9 — parser and loop were ONE traversal sharing the defect, so
the assertion could not fail. The comparator must be the INTENDED BATCH SIZE, an
independently-known integer. guard-3915 already named this exact failure and never reached the
loop; siting diagnosis in rb-11314, remedy filed as g-115-10302. (2) Narrative-chain gap:
`2026-08-16_client-cap-fix` carries its lesson under `lesson`, an UNCHAINED key `--narrative`
misses (instance for g-115-10108); `2026-08-03_sanitized-script-error` is genuinely bare.
(3) **`--replay-candidates` is pre-filtered to due-only, so NO due-RATE is computable from
it** — do not derive one.

## Run 76 — 2026-09-21, zeta, hostname cc-02, `uname -r` 6.8.0-139-generic (own-cloud)

Logged from the `force_metric_encoding_pending` gate (2 distinct numeric findings in the
close note with no tree edit since `selected_at`) — the readings belong HERE, in the
instrument ledger the SKILL.md cites, not in a tree node.

**Pool 850 → batch 10.** Stratified: 5 band-2 + 5 band-1, ordered by the PRIORITY RULES
with `replay_count` used only to break ties WITHIN a band, and stratified across rc rather
than sorted by it. Band 2 was again under N — **a POOL fact, never a corpus fact**
(guard-6131): high-surprise records are replayed, encoded and drained OUT of the pool,
which is what creates the ceiling.

**Field control re-run, and the alias trap still holds.** `surprise` present on 784 records;
`surprise_level` on 2. Keying on the alias would have zeroed rules 1 and 2 and fallen
through to rule 5 — a batch of routine CONFIRMED fillers that looks like a normal replay.
No error, no empty result.

**0 of 10 bare narratives — AND THAT IS THE UNREMARKABLE READING, NOT A WIN.** The SKILL.md's
own caveat prescribes comparing a violation-first batch against the **CORRECTED row (15.0%
empty)**, not the pool row (20.9%) or the resolved+archived row (29.2%). At 15.0% over 10
records the expectation is ~1.5 bare, so 0/10 is within ordinary variation and is NOT
evidence the resolution-evidence gate improved. The caveat correctly predicted its own
reading; this run CONFIRMS it rather than adding anything. Do not cite 0/10 as a gate
improvement.

**5 of 7 non-CONFIRMED records failed in the claim's OWN terms**, not against the world —
a direct corroboration of guard-2857 ("most of my CORRECTED hypotheses are corrected by
their own specification, not by the world"). Strengthened (`times_helpful`), not re-filed.

**Step 3.5 UNROUTABLE for the second consecutive run.** Declined on existing owners
g-115-7897 and g-115-5449 rather than filing — per the run-75 note, if the formation-shape
observation recurs a THIRD time, check whether those two have closed instead of filing.

**Step 3.6 eligibility genuinely 0** — re-checked this run rather than inheriting either
prior reading, which the run-75 next-run list specifically required.

**Step 4: zero pattern outcomes.**

**Step 4.5 stamp: 10 stamped, 10 verified, 0 failed, `next_review` 2026-09-28**, via
`replay-stamp-verify.sh` with per-id verification. **guard-1755 reproduced again**: the
`--replay-candidates` read-back reports M=0 on a fully successful stamp — the records have
left the due-only candidate set by construction. Verify per-id with
`pipeline-read.sh --id`, never off the candidate endpoint.

### Cycle 91 — 2026-09-21 (foxtrot, hostname LAPTOP-3IOFCNEO, uname -r 6.18.33.2-microsoft-standard-WSL2, --sharp-wave)

Pool 840 (5,759,691 B) / scoreable 774 / eligible after filters 774 / replayed 10 / stamped 10 / **verified 10** (per-id `pipeline-read.sh --id`, VALUES compared per rb-1502 — not `--replay-candidates`, which is guaranteed to report 0 on a successful stamp, guard-1755).

**HEADLINE: NO TITLE-DERIVED MARKER SURVIVED THE FLOOR, AND BOTH CONTROLS WERE CLEAN. Second consecutive cycle whose batch marker is refuted — that is guard-2129 working, not a barren cycle.** All markers derived from `title` only (never the resolution narrative: that text is written after and about the outcome, so mining it is circular and de-circularization does NOT catch it, guard-4758). De-circularized by excluding all 10 batch rows from both arms (1117 -> 1107). Exceedance = fraction of 4,000 permutations at the marker's OWN group size with |perm_delta| >= |delta|.

| marker | n | rate vs rest | delta | exceedance | median abs delta | verdict |
|---|---|---|---|---|---|---|
| scope-generalization (beyond/across/fleet/everywhere/all-/-wide) | 87 | 43.7% vs 42.1% | +1.6pp | **82.3%** | 4.1pp | NOTHING — and BELOW the median, i.e. not close |
| negation (not/without/no-/never/dont/doesnt) | 50 | 36.0% vs 42.5% | −6.5pp | 38.9% | 4.4pp | NOTHING |
| conjunction (`-and-`) | 1 | — | — | — | — | too small; pure selection artifact if pursued |
| CONTROL id[11:] checksum (date-FREE) | 227 | — | −3.7pp | 32.5% | — | ok, no leak |
| CONTROL id[:10] date-only (>=2026-08-01) | 478 | — | −4.3pp | 15.6% | — | ok, date is not carrying it this cycle |

READ THE SCOPE ROW CAREFULLY — IT DOES NOT FALSIFY guard-6629. Cycle 90 nucleated guard-6629 for the SCOPE axis from the *chronic* path (4 records each CORRECTED 3-4x). This row asks a different question: does scope LANGUAGE IN THE TITLE predict CORRECTED across 1,107 corpus records? It does not (+1.6pp, exceedance 82.3%). Both can be true, and the operational consequence is specific: **guard-6629's population is not reachable by title mining and must keep coming from Step 3.6's chronic sweep.** Do not "improve" the scope marker next cycle; it is measuring a different thing.

**NEW NUMBER — THE POOL/CORPUS BASE-RATE DIVERGENCE IS NOW MEASURED, NOT JUST ASSERTED. 16.3pp.** Step 3's header states that the pool is unrepresentative of the corpus because it excludes `encoded_via_chronic` (a 100%-CORRECTED population); it has never carried the size of that gap. This cycle, same run, same instant:
- POOL CORRECTED rate = **26.2%** (200 CORRECTED / 762 CONFIRMED+CORRECTED, of 840 records; the pool also carries EXPIRED 8 and UNRESOLVABLE 62, which is part of why)
- CORPUS CORRECTED rate = **42.5%** (475/1117 scoreable, of the 1,844-record deduped resolved+archived union)
So a batch finding re-measured against the POOL is still being compared against a base rate 16.3pp too LOW, which INFLATES any apparent enrichment a second time. Layer 2 of guard-2144 has a number now. Name the population on every rate (the corpus is `--stage resolved` UNION `--stage archived`, deduped: resolved alone was 79,343 B against archived's 10,098,873 B, i.e. a few percent — the survivorship filter that header warns about, quantified).
Batch vs corpus this cycle: 80.0% vs 42.5% = **+37.5pp**, expected BY CONSTRUCTION and not a finding.

TWO POOL FACTS, both matching what the instrument predicts:
- **`surprise >= 7` is ZERO in the pool** (`>=5` is 226 of 774). Band 1 was empty and the entire batch came from band 2. This is a POOL fact, never a corpus one (guard-6131): high-surprise records get replayed, encoded, and drained OUT. Do not read it as a calm corpus.
- **Step 3.6 chronic queue is EMPTY** — 0 records at rc>=3 CORRECTED not yet `encoded_via_chronic`, against cycle 90's FOUR. Also 0 at the rc>=5 archive cap. Cycle 90 predicted the stratum would REFILL between cycles because records age into rc>=3 while carrying CORRECTED; it has not refilled yet. That prediction is NOT yet falsified — one empty reading is a moment, not a property (rb-10209), and cycle 89 already made exactly this mistake by writing a moment down as a forecast. Recording it as a data point, not a trend.

STRATIFICATION WORKED AS PRESCRIBED: band 2 (226 members) was stratified across `replay_count` rather than sorted by it, giving batch rc spread {0:4, 1:3, 2:3}. Sorting rc-first would have discarded the enrichment entirely (the never-replayed stratum is surprise-poor by construction).

NARRATIVE QUALITY: **0 of 10 bare** (10/10 parsed, count asserted against ids requested before any conclusion was drawn). Expected ~1.5 for a CORRECTED-heavy batch against the CORRECTED row's 15.0%, so 0/10 is unremarkable and is NOT evidence the resolution-evidence gate improved.

## Run 77 — 2026-09-22, zeta, hostname cc-02, `uname -r` 6.8.0-139-generic (own-cloud)

Pool 846 (archived 829, resolved 17), 5,793,205 B, rc=0. Record keys printed before any
field read. Pipeline counts reproduced first (guard-1835 step 1): discovered 11 / active
138 / measurement-pending 9 / resolved 17 / archived 1834 = 2009.

**RULE 2 IS EMPTY AGAIN — 0, not run 76's 5, and this is a POOL FACT, NOT A CORPUS FACT
(guard-6131).** Pool surprise ceiling is 6: s=6 132 · s=5 98 · s=4 394 · s=3 78 · s=2 70 ·
s=1 4 · s=0 4, null 66. Rule 2 has now read empty(72) → empty(73) → 6(74) → 5(76) → 0(77),
so neither "moot" nor "live" is inheritable in either direction — check it first, every run.
The whole batch was therefore band 1 (s>=5, n=230), stratified across `replay_count` x
`surprise` with oldest `formed_date` within stratum: rc coverage {0:4, 1:2, 2:2, 3:1, 4:1}.
Outcome mix REPORTED not selected on: CORRECTED 6 / CONFIRMED 4.

**TWO POOL RECORDS WERE EXCLUDED FROM SELECTION DELIBERATELY, AND THE EXCLUSION IS THE
FIRST FINDING.** `2026-07-29_census-a` and `2026-07-29_census-b` are TEST FIXTURES
(`category: test-cat`, title "Test hypothesis for surprise derivation on write", author /
source_goal / resolved_by all null) carrying `surprise: 6` — so they sit in BAND 1, the
scarce enriched band, every cycle. census-a is at rc=4 and was the oldest member of the
rc=4 s=6 stratum, i.e. it would have won a slot on the stated rule and burned its FINAL
replay on a fixture. The surprise of 6 is not an accident: the fixture exists to test
surprise DERIVATION ON WRITE, and the deriver gave it a 6. Separately, 8 pool records carry
`outcome: null` at stage=archived (6 of them `arc-solver`, 2026-07-12/13) — archived without
ever resolving, so there is no outcome to replay. 10 of 846 = 1.2% of the pool cannot pay.

**THE rc>=5 CAP WORKS AT THE SOURCE, AND STEP 1's LLM-SIDE REMEDY IS DEAD CODE FOR 98% OF
THE POOL.** Run 74 handed forward "ARCHIVE 2026-06-25_delta-g00103 — it is at rc=5". It is
gone from the pool and max rc is now 4 (16 records sit at rc=4). Reading the endpoint rather
than inferring: `mind_api/src/world/pipeline.py` `replay_candidates` carries a source-level
`int(replay_count) >= 5: continue` added by **g-115-2509**, whose comment states the reason
verbatim — "For already-archived records the LLM-side remedy (pipeline-move to archived) is
a no-op". The pool is 829/846 = 98.0% already `stage: archived`, so Step 1's
`pipeline-move.sh {id} archived` is defense-in-depth only. CONSEQUENCE WORTH CARRYING: a
record at rc=4 is on its LAST replay — the source filter retires it permanently at 5. Weigh
that before spending a slot on one.

**THE RUN'S MAIN RESULT: THE "rationale WINNER ⇒ CHECK THE UNCHAINED KEYS ⇒ BARE" RULE
PRODUCES FALSE BARES, AND THE UNCHAINED-KEY CHECK CANNOT CATCH THEM.** Both of this batch's
two `rationale` winners came back with NO unchained key holding content — the skill's
prescribed probe says BARE for both. Both are wrong. `2026-07-27_guardrail-retire-rate-
stays-near-zero` carries its full resolution narrative APPENDED INTO `rationale` under a
`=== RESOLVED CORRECTED 2026-08-26 ===` marker; `2026-07-17_ls20-episode-varying-conversion`
carries its resolution as plain prose with NO marker at all ("SECONDARY 0.816 <= run-4's
0.901 -> CORRECTED, variety family exhausted", plus an OFF-invariance control and an
evidence commit). The lesson is INSIDE the field the rule tells you to distrust.
Measured pool-wide: 73 records resolve to `rationale`. A marker regex
(`=== RESOLVED` / `RESOLVED <OUTCOME>`) finds **3** of them — and misses the 07-17 record,
so the marker is one writing convention among several and cannot be the detector. Widened to
"the rationale text contains the record's OWN outcome token": **26 of 73 = 35.6%**, against a
BASE-RATE CONTROL of 110 of 607 = 18.1% on records whose chain winner IS a real outcome key
(a formation rationale may legitimately name a possible outcome). Enrichment +17.5pp, so the
defensible claim is **~13 net (26 gross) of 73 rationale-winners are NOT bare** — against a
rule that calls all 73 bare. The pool's "genuinely bare" rate is therefore OVERSTATED by the
current read-side discipline, and that rate is the number the whole Step 2 comment block is
about. Concrete instance for **g-115-10108**, which owns the helper fix.

**FALSIFIED BEFORE IT REACHED ANY STORE — `reasoning` MUST NOT JOIN NARRATIVE_CHAIN.** The
07-27 record carries a non-empty `reasoning` key absent from both the chain and this skill's
unchained list, which reads as an obvious eleventh link. Printing its CONTENT first: it opens
"PREMORTEM. Strongest reason wrong: ..." — a FORMATION-time field, the exact hazard
guard-2615 / guard-3980 name for `rationale`. Adding it would manufacture documented-looking
bares. Corpus incidence is negligible anyway: 15 of 846 carry `reasoning`, only 2 of those
are chain-rationale/NULL. This is run 71's named error class (reading a field off a surface
whose semantics were assumed) presenting for the fourth consecutive run, and run 71's own
remedy — probe the shape before reading — is what caught it.

**STEP 3.6 ELIGIBILITY = 1** (not run 76's 0, not run 75's 2 — re-checked, inherited neither):
`2026-07-30_split-without-reduce-recurs`, rc=3, s=6, system-behavior, CORRECTED. Positive
control on the near-zero: rc>=3 total 69, of those CORRECTED 1, of those already
`encoded_via_chronic` 0 — so eligibility is bounded by the CORRECTED-within-chronic rate, not
by an encoding backlog. OVERLAP branch taken, nucleated nothing: **guard-886** is the exact
twin ("predictions that a known framework error/bug will PERSIST ... have been CORRECTED
repeatedly"), surfaced by the MECHANISM query, not the SUBJECT one (the subject query
returned tree/retrieval guardrails and nothing about prediction shape). guard-1105 is its
mirror for durability claims, and rb-11119 already covers the batch's arc/ls20 member.

**AND THE STEP 3 PATTERN IS guard-886 QUANTIFIED — WHICH SHOWS guard-886 IS OVER-STRONG.**
Four of the six CORRECTED records share one FORMATION-time shape: each predicted an
observed-bad condition would PERSIST while naming, in its own claim, a remediation already in
flight (g-115-3571, g-115-3553, the sq-009 gate). Non-circular by construction — the
condition is readable before the outcome. Re-measured against the pool per guard-2129 (the
batch is band-1 and CORRECTED-enriched by +39.8pp, so no batch-scoped rate may stand):
detector = persistence language in title+claim+position+rationale+resolution_criteria AND a
`g-NNN-NN` id in the same text. Over 767 scoreable records, base CORRECTED 25.9%:

| group (persistence, names-goal-id) | n | CORRECTED |
|---|---|---|
| (True, True)   | 287 | **30.3%** |
| (False, True)  | 320 | 24.1% |
| (False, False) | 101 | 23.8% |
| (True, False)  |  59 | **18.6%** |

Delta +4.4pp; size-matched permutation floor (4000 draws) median +0.2pp, p95 +3.3pp,
exceedance **p=0.019**; presence control 287/287. It CLEARS the floor — and it is still too
weak to act on: a +4.4pp shift on a group covering 37% of the corpus is a nudge, not a veto,
and guard-886's own text says "refuse confidence > 0.5". That over-statement is independently
corroborated by the guardrail's own telemetry: **times_noise 42 vs times_helpful 1**. Two
unrelated instruments agreeing that a rule is over-strong is the useful result here.
Note the interaction: persistence language ALONE is ANTI-predictive (18.6%, BELOW base) — the
lift lives entirely in the CONJUNCTION with a named in-flight fix.
**ENCODED NOTHING NEW** (the detector was chosen after seeing the batch, so it is selected on
its own discovery set; and 6 of 10 batch members fall inside a group that large by default).
guard-886's `rule` is `immutable_field` at the store — the store refused the calibration
write, correctly — so the magnitude lives HERE and `utilization.times_active` was incremented.

NARRATIVE QUALITY: 10/10 parsed, flattened count asserted against the INTENDED batch size of
10 (run 74's correction — never against the loop that built the request). The wrapper emits a
LIST per call, so the accumulate step needs a raw_decode loop AND a flatten; parsing without
the flatten raised `'list' object has no attribute 'get'` and the assertion caught it.
Keys: outcome_detail 7, outcome_note 1, rationale 2. Zero NULL, zero verdict-only.

NEXT RUN (78):
  1. Rule 2 read 0 here after run 76's 5. Check it FIRST and inherit nothing — five runs, five
     different readings.
  2. Exclude `category: test-cat` and `outcome: null` records from selection. They are in the
     pool (2 fixtures + 8 outcome-less) and one fixture sits in band 1 at rc=4.
  3. Do NOT re-derive the rc>=5 cap question — it is answered above (g-115-2509, source-level).
  4. Do NOT re-run the persistence-marker permutation test; it is recorded above and ENCODED
     NOTHING. The open, non-circular successor is still run 74's: does `measurement_channel`
     (a FORMATION-time field) predict UNRESOLVABLE? Needs de-contamination first.
  5. When a narrative resolves to `rationale`, READ THE TEXT before calling it bare — the
     unchained-key probe cannot see an appended resolution. 26 of 73 carry one.

## Occurrence 137 — 2026-09-22, bravo, hostname cc-05, `uname -r` 6.8.0-139-generic (own-cloud), g-001-05

**POOL** `pipeline-read.sh --replay-candidates` = **894**. Field check FIRST: `surprise` present
on **828**, `surprise_level` on **2** — canonical key confirmed, so a zero in rules 1-2 would have
been a real zero rather than the silent alias-key fall-through. surprise>=5: **274**; >=7: **10**;
CORRECTED **244**; `replay_count>=5`: **0** (no archive-cap firings this run).

**HEADLINE — STEP 3.6's ELIGIBLE POPULATION IS NOT STRUCTURALLY ZERO. Measured 41.**

This CONTRADICTS `2026-08-07_step36-resumes-after-three-month-dormancy` (CORRECTED, surprise 7,
resolved zeta/cc-02 2026-09-05), which is the fleet's standing answer on why Step 3.6 never fires.
Its replacement mechanism reads, verbatim: *"All 32 un-encoded eligible records are stage=archived
... and NONE is in the replay pool — the pool draws from non-archived stages only"*, therefore
*"it cannot resume from this predicate without either reading archived records or stamping the
step before archival."*

Measured here from **that exact predicate** (rc>=3 AND CORRECTED AND not `encoded_via_chronic`):

| | reading |
|---|---|
| eligible | **41** (was 32 on 2026-09-05 — +9 in 17 days) |
| stage | **41/41 archived** — the archival half HOLDS |
| reflected | 41/41 True |
| replay_count | {3: 37, 4: 4} |
| reached FROM `--replay-candidates` | **yes — all 41** — the unreachability half does NOT hold |
| verified by direct per-id read | 3 (not from my own parse) |

So the step CAN resume from this predicate on this box, and did. **NOT CLAIMED: why the reading
differs.** Endpoint behaviour, box difference (cc-02 vs cc-05) and archival timing are all live
candidates and I measured NONE of them. Read this as *"the predicate reaches archived records on
cc-05 on 2026-09-22"*, never as *"the endpoint was changed"*. The original record's CORRECTED
verdict stands — what is corrected is its replacement MODEL. A dated correction was appended to
its `outcome_detail` (2642 → 4374 chars, original preserved byte-identical, read back and
verified) and posted as `severity:invalidates` (`msg-20260922-194309-bravo-3625`).

**BATCH** 10, STRATIFIED across `replay_count` {0:2, 1:5, 2:1, 3:2} and `surprise` {3:2, 4:1, 6:4,
7:3}; 4/10 CORRECTED. Stratified rather than sorted because the rc-vs-CORRECTED gradient sign is
not stable across measurements — any FIXED sort direction is a coin flip on an unknown sign.

**STEP 3 — NO MARKER PURSUED, AND THAT IS THE MEASUREMENT.** At n=10 the size-matched permutation
floor is ~30pp; no delta this batch could produce would clear it. Declining is the result, not a
skipped step. Qualitative finding instead, and it ENCODED NOTHING NEW: **3 of the 4 CORRECTED were
corrected BY THEIR OWN SPECIFICATION rather than by the world** — a threshold missed while
direction held (+7.1pp), a literal criterion scored on a raw count the record itself calls the
wrong number, and a literal-criterion window. That REPLICATES guard-2857's own measured 4-of-5, so
the existing guardrail predicted the batch; `times_active` incremented, zero created.

**NARRATIVE QUALITY** 10/10 parsed. Bare-or-verdict-only **0/10** — expected ~1.5 against the
CORRECTED row (15.0% empty) that a violation-first batch is drawn from, so this is unremarkable and
is NOT evidence the resolution-evidence gate improved. Compared against the CORRECTED row, never
the pool row.

**THE ASSERTION EARNED ITS KEEP.** The flatten asserted parsed-count == ids-REQUESTED and fired at
**9 vs 10**. Cause was mine, not the corpus: the id file was written with `"\n".join(...)` and no
trailing newline, so `while read` silently dropped the last id. Without the assertion this would
have read as a finding about bare records — the exact misattribution this file's run-74 note warns
about. Assert against the INTENDED batch size, never against the loop that built the request.

**STEP 3.6** 7 chronic records encoded (`encoded_via_chronic=true`), **marked 7, VERIFIED 7,
unverified 0**. Per guard-5492 these are NOT prevention evidence — that counter rises with a
guardrail's own FAILURE to prevent the error. The value is DRAINING records that had each burned
3-4 replay slots. **34 remain** for the next occurrence.

**STEP 4.5** stamped 10/10, verified 10/10, per-id (guard-1755 — never `--replay-candidates`, which
excludes future `next_review_date` and reports zero on success).

**NEXT OCCURRENCE:**
  1. The 34 remaining chronic records are drainable FROM the standard predicate on this box — do
     not re-derive reachability from the contradicted record, read the correction on it.
  2. Re-measure the eligible count before draining. It moved 32 → 41 in 17 days, so it is not a
     fixed residue; a count that keeps RISING while runs drain it is the finding.
  3. If another box reports Step 3.6 eligible = 0 from the same predicate, THAT is the
     discriminator this run deliberately did not chase — record both readings, attribute neither.
