# Rationale: /forge-skill Gates

Referenced from `.claude/skills/forge-skill/SKILL.md`. The structural WHY behind
forge-skill's gates, and the incident traces that motivated them — extracted so
the skill body fits the ≤500-line small-model budget (g-115-9041). The skill body
keeps the commands, the one-line imperatives, and the rb/guard ids; this file
keeps the reasoning. Time-anchored incident traces already in the reasoning bank
are referenced by id, not re-told (per `.claude/rules/rationale-extraction.md`).

## Why the confidence thresholds are not free-standing

The Forge-Criteria numbers (`utility` = CALIBRATE / confidence >= 0.50,
`analytical` = EXPLOIT / confidence >= 0.75) MIRROR `core/config/tree.yaml` →
`domain_health.competence_mapping`, which is the SSOT
(`EXPLORE 0.25 / CALIBRATE 0.50 / EXPLOIT 0.75 / MASTER 1.00`; guard-1195 —
capability_level/confidence travel together in `_tree.yaml`). If that mapping is
retuned, update the two SKILL.md lines with it. Prefer reading the node's stored
`capability_level` string over re-deriving a level from `confidence`; the
resolver is `_graduate_from_confidence` in
`core/scripts/backfill-tree-node-fields.py` (highest threshold whose value the
confidence meets or exceeds).

There is NO automated script for this type/confidence gate — the SKILL.md text
IS the enforcement — so a wrong number silently authorizes under-qualified
forges. This is distinct from `curriculum-contract-check.sh --action
allow_forge_skill` (resolved in `core/scripts/curriculum.py`), which IS
automated: the curriculum contract gates whether forging is unlocked AT ALL for
the agent's stage; the type/confidence gate decides whether THIS gap clears its
bar. Only the second is text-only.

Corrected 2026-07-25 (g-250-269): the parentheticals once read `>= 0.30` and
`>= 0.60`. Both were wrong and both erred LOW — an agent trusting the gloss would
forge BELOW the real bar. Caught live: `npc-intelligence` reads confidence 0.7429
/ capability_level CALIBRATE; the stale `>= 0.60` gloss said PASS for an
analytical gap, the real EXPLOIT threshold (0.75) said BLOCK, by 0.0071.

## Typeless default — why utility (CALIBRATE), not analytical (EXPLOIT)

Decided g-115-3131 (2026-07-25, bravo): a gap with no `type` defaults to
`utility` (CALIBRATE), not `analytical` (EXPLOIT).

Evidence at decision time: 22 of 24 registered gaps carried no `type` at all, so
the default was the operative policy for 92% of the corpus, chosen by omission.
Classifying the 22 against the two `gap_types` descriptions in
`core/config/skill-gaps.yaml` (utility = "well-defined procedures… retrieval
workflows"; analytical = "requiring domain understanding… pattern recognition")
gave 20 utility / 2 analytical (the analytical two: gap-008 derives win-condition
semantics from recordings, gap-015 designs pre-registration thresholds). The old
default was inverted against ~91% of the population it governed.

Why it went unnoticed for 24 gaps: it errs STRICT, the safe direction. 790 of
1246 capability-bearing tree nodes (63%) already sit at EXPLOIT, so the harder
bar usually passed anyway — which is also why 9 typeless gaps were forged without
anyone noticing a gate had been applied by accident. It only bites a
utility-shaped gap in a CALIBRATE category, the narrow 0.50–0.75 band (the live
case: `npc-intelligence` at 0.7429).

Rejected alternatives: (a) *"the default is right and the historical forges were
under-gated"* — refuted on its premise: those forges cleared the STRICTER bar
because most categories are EXPLOIT; there is no under-gating to backfill away.
(b) *"retire the gate; category confidence is the wrong proxy"* — a real critique
(a category's maturity does not measure whether the agent understands the
specific procedure being mechanized), but retiring a gate with no replacement
trades a narrow false-block for an open door; filed separately.

Safety of lowering the default: the flip only matters for a gap that is BOTH
typeless AND in a CALIBRATE category. All 22 existing gaps were backfilled with an
explicit `type` in the same change, and the registration site in
`aspirations-spark` now sets `type` at gap-creation, so a typeless gap should be
rare. When one appears, `utility` matches the modal shape and explicit
`type: analytical` opts INTO the higher bar.

## Step 3.6 companion-script dogfood — why each guard exists

**Aggregate assertions (guard-1793).** If a suite carries a SUMMARY assertion as
its anti-vacuity guard, mutate against THAT ASSERTION ALONE — not the suite as a
whole. An aggregate summarises ONE axis, and a defect that corrupts a DIFFERENT
axis leaves it untouched, so it reads green through the exact bug it was written
to catch. Measured g-335-439: a `4 distinct floors across 4 fixtures` line stayed
green through two deliberate mutations that each reintroduced a real production
bug, because both corrupted the at-floor *enumeration* while leaving the *floor*
intact; only the per-fixture assertions fired. Test: re-run each mutation and
check whether the AGGREGATE moves — if it does not, it is not a health check, it
is a number that happens to be printed. Distinct from guard-1220 (self-supplied
expectation), guard-920 (wrong input shape), guard-1462 (absent layer).

**Fixture-seam exclusions (guard-1462).** Wherever the fixture is injected is a
silent scope declaration: everything UPSTREAM of the injection point is
structurally unfalsifiable by ANY fixture, and a green run announces nothing about
where that line fell. State the excluded layers explicitly in the forge log. The
common split is a script that both SELECTS records and INTERPRETS them — a seam
between the two tests only the interpreter, leaving enumeration, filtering,
ordering and the limit/cap with no coverage at all.

**The live run is a THIRD failure mode (g-250-269).** A forge followed Step 3.6
exactly — 7/7 fixtures including a valid two-way vacuity proof whose decisive pair
differed in exactly one field — and still shipped a real defect, because the
fixture substituted the payload AFTER enumeration. A non-session directory both
consumed a `--limit` slot and tripped the guard-1214 positive control, and no
fixture could reach that layer; one voluntary live run surfaced it in seconds.
This is distinct from its neighbours: guard-920 is the right layer with the wrong
input shape, guard-1220 is the right layer with a self-supplied expectation, and
this one is a layer that is not in the suite at all — so satisfying both of those
does NOT protect you here.

**Success-path audit on the live run (g-115-4466, rb-6343, guard-2329).** Budget
the live run as a SUCCESS-PATH audit, not a smoke test that the thing runs. For
every write the tool performs, read the record back from the store and DIFF the
stored fields against what was supplied; for every non-zero exit, read the
script's own contract before calling it a failure. A fixture supplies its own
expectation, so a call that SUCCEEDS while quietly storing something other than
what you passed matches that expectation exactly — which is why this class is
structurally unreachable by fixtures. Measured g-115-4466 (2026-08-01): one
3-item live run found three defects, ALL on the success path (a silently-rewritten
`origin_signal`, a read-back keyed on the value that was never stored, and an
rc=3 that means success).

**Judge the exemption per subcommand (g-115-3475, rb-5355).** A companion script
is a BUNDLE of subcommands with heterogeneous risk, so one whole-artifact verdict
launders the riskiest member through the average — and the riskiest member is
exactly the one carrying the harness's safety claim. Measured while forging
`launch-env-server-session`: 4 of 8 subcommands were exempted as thin wrappers,
and TWO were ineligible — `verify-terminated` returns exit 1 plus a STILL-BILLING
action on a non-terminated instance (a pass/fail verifier), and `teardown` is
state-mutating; both shipped unvalidated. Walk the subcommand list and write one
verdict per entry. (guard-1220, rb-4004, rb-4124.)

## Step 4 registration — why the gates

**Phantom registration (rb-10227, guard-2242, guard-2335).** Measured on a
downstream clone 2026-09-05 11:55Z: `mkdir -p` succeeded, the `Write` of SKILL.md
was refused by the L1 hook, and registration proceeded anyway — registry row
written, skill dir EMPTY, a test goal filed to exercise a skill with no body, two
"forge-skill,complete" board posts, and the model declared SUCCESS. A row
pointing at an absent body is a PHANTOM registration: it advertises a trigger
fleet-wide that dispatches to nothing (guard-2242: a pointer field is not evidence
until its referent is confirmed to exist). The body gate checks DISK PRESENCE at
the load path, deliberately not catalog listing — a skill forged mid-session
cannot appear in its own session's catalog on Claude Code (guard-2335), so a
catalog assertion would refuse every correct forge.

**`amended_at` is LWW tier 0 (guard-1153).** `merge_forged_skills` resolves a
same-name conflict WHOLE-RECORD, and an amendment bumps no `forged_date` and adds
no FIELD, so without the stamp it falls to a `_canon` lexicographic tiebreak and
can lose DETERMINISTICALLY to an untouched peer copy — every write path reporting
success while nothing lands. Measured cc-05 2026-07-28 (g-115-3506 → g-115-3638):
a 4-trigger addition lost 10-to-6 via the Edit tool, a plain python write, AND
`OwnCloudBackend.write_text`, and was byte-identical with the merge arguments
swapped, so retrying could never win. `amended_at` is tier 0 of
`_merge_forged_skill` (LWW on a timestamp written BY THE SAME MUTATION that
writes the field).

**Git-distribution of forged bodies (g-115-2373, 2026-07-16).** The registry
syncs fleet-wide through the governed store and advertises triggers on every box,
but `.claude/` is NOT an own-cloud governed root — a gitignored body existed ONLY
on its birth box, so trigger resolution dispatched to un-invokable skills on 4/5
boxes (found by g-115-2358 validation). `git add .claude/skills/{name}/` lets the
iteration close-commit sweep it to origin; every fleet box picks it up on its next
`iteration-push` pull. Both ignore forms are retired (the g-115-2272
parallel-forge collision was the shared root-`.gitignore` FILE; disjoint new skill
DIRS cannot conflict). Promotion-seed purity is unaffected: `_seed_engine.py`
auto-derives seed exclusions from the registry (g-306-88), so a committed forged
body still never leaks into the domain-free seed.

## Why extension-before-forge is a gate, not a suggestion

A forge is not free and its cost is permanent: Claude Code loads every skill's
name + description into the system prompt at startup, so each new SKILL.md is
standing per-turn weight on every agent forever, while extension costs zero. The
corpus is a pure additive ratchet — forging adds, nothing subtracts — and
`max_skills` is ratchet-down-only by construction (`modifiable.max_skills` is
`{min: 10, max: 100, default: 100}`, so the modifiable maximum equals the default
and the cap cannot be raised). Measured 2026-08-11: 116 skill dirs against a cap
of 100, and 16 of 130 gaps had already been resolved as
`satisfied-by-extension` — the practice was established and load-bearing while the
skill file mentioned it zero times, so the cheaper path existed and was invisible
at exactly the moment it was needed. Enforced-by-visibility only: `/verify-learning`
check `skill-corpus-count-under-cap` counts the directories, and nothing refuses a
forge.

## Why the overlap check is by-hand (guard-4841, guard-2119)

Do NOT reach for `skill-relations.sh read --similar {candidate_name}` in the
overlap check: it returns `[]` for EVERY input, including names that certainly
exist (measured 2026-08-22: `uncrossed-seams` → `[]`, `reflect` → `[]`), so its
empty answer is ZERO signal, not evidence of no overlap — and the Constraints
bullet was the one place it was prescribed, which is the one place that empty
answer is most load-bearing. Even a working name-matcher would not suffice: two
skills covering overlapping procedures routinely share no name tokens
(guard-2119). Instead grep the skills corpus for the PROCEDURE'S OWN vocabulary
(the distinctive nouns in the gap's step list) across `.claude/skills/` and
`core/scripts/`, grep `forged_from` across `.claude/skills/*/SKILL.md`, and read
the front matter of the nearest neighbours before concluding the capability is
absent. If a similar skill exists, strengthen it or register a `compose_with`
relation instead of forging a new one.

## Cross-references

- rb-4004, rb-4124, rb-5355, rb-6343 — Step 3.6 dogfood incident traces
- rb-10227 — phantom-registration incident (downstream clone, 2026-09-05)
- guard-1153 — `amended_at` LWW; guard-1195 — capability_level/confidence coupling
- guard-1220, guard-1462, guard-1793, guard-920, guard-2329 — dogfood vacuity/seam guards
- guard-2242, guard-2335 — pointer-not-evidence, mid-session catalog absence
- guard-4841, guard-2119 — overlap-check by-hand
- g-115-3131, g-250-269 — typeless default + confidence-gloss correction
- g-115-2373 — forged-body git-distribution
- `.claude/skills/forge-skill/SKILL.md` — the consumer of this rationale
- `.claude/rules/rationale-extraction.md` — the extraction convention this follows
