# Rationale: `checks_unevaluatable` and the Q-family fall-through

Referenced from `.claude/skills/aspirations-verify/SKILL.md` ("Scripted Check
Evaluation"). Why the evaluator distinguishes "could not run this check" from
"ran it and it failed", and what the measured population behind that split is.

Extracted from the SKILL.md 2026-09-03 (g-357-44) under
`.claude/rules/rationale-extraction.md`: the SKILL.md is in the `loop-skills`
hot-path set, so these paragraphs were re-read on every loop iteration of every
agent while being needed only by someone changing the evaluator. The procedural
half — the flag table and the branch each flag takes — stays in the SKILL.md.

## Why the flag exists

`checks_unevaluatable` (g-115-4849) means the evaluator COULD NOT RUN one or
more checks — an unknown type name, a missing required field, a command outside
the allowlist — as opposed to running them and finding the work undone. Until it
existed both landed in `checks_failed`, so a goal that genuinely succeeded was
marked `pending` because its check used a type name `predicate.py` does not
implement.

## How large the unevaluatable population actually is

Measured on the live world queue 2026-08-11 (20 active aspirations, 145
structured checks): **68 of 145 — 46.9% — cannot be evaluated**, split
`not-in-allowlist` 29, `missing-command` 22, `unknown-type` 17. That is a
static LOWER BOUND: it is computed by inspection rather than by calling
`evaluate()`, because evaluating an allowlisted `command_succeeds` would
actually run the command 145 times, and it cannot see run-time schema failures
such as an unresolvable `after_ref`.

**Do not read the type-name bucket as the whole population.** The first pass of
this very measurement counted only unknown type names, got 17/145 = 11.7%, and
was wrong by a factor of four — the two larger buckets dispatch to a real
evaluator and then get refused before anything is checked, so a census keyed on
`type not in PREDICATE_TYPES` cannot see them. The 71% figure in g-115-4849's
description is the pre-alias type-name count; g-115-5186's alias tables cut that
bucket specifically, which is why the type-name share fell while the true
unevaluatable share did not.

## Why `all_passed` is null rather than true or false

`all_passed` is **null** on this path, exactly as on `checks_empty`: nothing was
verified, so it must not read `true`; nothing failed, so it must not read
`false`. Exit code is 0 — only a genuine failure exits 1.

## Why a genuine failure outranks an unevaluatable one

When both are present the flag is `checks_failed`, because an unevaluatable
check must never launder a real failure into a fall-through. The unevaluatable
tally survives that branch in the separate `unevaluatable_count` field — read it
rather than inferring from flags.

## How wide the fall-through actually is (g-357-44)

Relevant to anything hosted in the Q-family, because a check placed there
inherits the family's trigger. The Q-family runs on three of the five flag
branches — `checks_empty`, `checks_unevaluatable`, `has_string_checks` — and is
skipped only on `flags = []` (every structured check evaluated AND passed) and
`checks_failed` (the goal fails regardless).

Measured 2026-09-03 over all 2362 pending goals in the live queue
(`aspirations-query.sh --goal-status pending --full`):

| shape | count | Q-family |
|---|---|---|
| no verification block at all | 1234 | FIRES (`checks_empty`) |
| `outcomes` only, no `checks` | 550 | FIRES (`checks_empty`) |
| all-STRING checks | 533 | FIRES (`has_string_checks`) |
| all-structured checks | 45 | may not fire |

Of a 10-goal sample of that last bucket run through the real evaluator, 6
returned `flags = []`, 2 `checks_unevaluatable` and 1 `checks_failed` (1 timed
out) — so the true bypass is on the order of **30 of 2362, ~1.3%**. Hosting a
check in the Q-family therefore reaches ~98.7% of the live queue, and the
excluded remainder is precisely the population whose criteria are fully
structured and machine-passing — the *least* likely to be a prose artifact
carrying unverified entity facts. A scoping note on g-357-44 had reasoned the
opposite from first principles ("silently inert on exactly the well-formed goals
most likely to carry entity facts"); the measurement falsifies it, and the
falsification is recorded here rather than left for the next reader to
re-derive.

## Q4 — why the sample is scripted, and its traps

**Why a script picks the claims.** The goal's own wording is "script-gated sample
selection so the executor cannot cherry-pick". An executor asked to spot-check a
few claims checks the few it already knows are cited. `sample_clusters` orders by
`sha256(goal_id, artifact path, cluster text)` and takes the lowest N, so the
sample is deterministic, reproducible by anyone holding the artifact, and not
re-rollable — the only way to change which claims are sampled is to change the
artifact. It removes CLAIM-level cherry-picking and NOT artifact-level: the caller
still names the artifact, which is why the result carries `artifacts_read`,
`artifacts_missing` and `clusters_total` (guard-3489).

**Trap 1 — `--session-id` on a worker Body.** It defaults to `$MIND_SID` and must
stay defaulted. `context-reads.tracker_path` routes to the session's own tracker
`sessions/<sid>/body-context-reads.txt` whenever that session has a per-session dir
(every bound session since g-115-11179), and to the agent-wide
`session/context-reads.txt` otherwise — and for such a session the agent-wide file
may not exist at all. Measured 2026-09-03 (alpha worker,
cc-07), same turn, one query: the value `framework-verification`, written by that
session's own `retrieve.sh`, answered rc=1 without the flag and rc=0 with it, while
the agent-wide tracker was absent entirely. A Q4 that dropped the session id would
report every citation on every worker Body as unretrieved — wrong in the ALARM
direction, which is how a check gets switched off.

**Trap 2 — a `cat`-read file reads as uncited.** The provenance manifest is fed by
PostToolUse hooks bound to the Read / WebFetch / WebSearch TOOLS. A file read with
`cat` in a Bash call, or a page pulled with `curl`, is invisible to it by
construction (guard-4407, guard-1150 lineage). So a `decorative-citation` finding
against a file you genuinely did read that way is EXPECTED, not a defect: re-read
it with the Read tool, or say so explicitly in the verify summary. This is also
why `retrieved_predicate` returns None — never a permissive lambda — on an
unreadable or empty manifest, so `analyze` skips the decorative test rather than
manufacturing a pass (guard-1760).

**Trap 3 — the recorded key is a BOOL and Q4 has THREE outcomes.** The sampler
exits `0` for both `pass` and `skipped` (no artifact, nothing samplable), and the
skill has always said in as many words that those "are not the same answer". But
`phase_progress.q4_passed` could only ever be `true` or `false`, so a Body that
correctly read a SKIPPED verdict as non-blocking wrote `q4_passed: true` — and
from that record no later reader can tell a verified provenance pass from a run
that verified nothing. Measured 2026-09-11 (alpha, cc-08) across asp-115's 77
worker closures: 44 carry a `q4_passed` key, 35 of those carry a bare `true`
whose pass-vs-skipped meaning is UNRECOVERABLE — and the tri-state tally reports
`skipped=0`, not because no Q4 was ever skipped but because no record could
SAY so.

Hence `phase_progress.q4_verdict` (`pass` | `fail` | `skipped`), written
alongside the bool rather than replacing it — `prior_checks.q4_passed` is what
the graceful-stop handler threads back on resume, so dropping the bool would
break mid-verify recovery. The bool stays the RESUME key; the tri-state is the
EVIDENCE key.

The consumer is `core/scripts/worker-closure-audit.py::q4_state_of`, which
resolves an explicit `q4_verdict` first and falls back to the legacy boolean as
`unknown` — never as `pass`. That direction is the whole point: reading a bare
`true` as a pass would convert the audit's silent blindness into confident wrong
agreement, which is strictly worse than no reading at all. `unknown` is reported
in the audit's `q4_states` tally so the ambiguous population stays countable
while it drains. (g-115-9618; guard-1753 "could not resolve" != "resolved and
found nothing"; guard-2223 tri-state over a collapsed bool.)

**Trap 4 — a product-repo file (g-115-9674).** Until 2026-09-26 the recorder
admitted only in-repo and world paths. A product-repo file (under an
`AGENT_WRITE_PATH` root, outside the repo) was never recorded, however it was
read. So an accurate citation of it scored `decorative-citation`, the same verdict
as a file never opened. Re-reading did not help, because the barrier was scope,
not read method. `context-reads.is_in_scope_record` now records those reads, for
the recorder ONLY; the dedup gate and the pre-edit advisory keep their own
scopes. Trap 2's remedy therefore reaches product files too. A decorative finding
on one means the file was not Read with the Read tool this session, or was read
before a compaction cleared the manifest (g-115-10759).

Two other dispositions were rejected, with their costs measured on one control
artifact. Marking an untrackable path not-adjudicable would also have demoted
the UNREAD product citation. That citation still fails after the fix, and under
this option it would have become advisory, silencing Q4 on product work (the
largest work class at the 2026-08-21 census). An audited override would weaken
the no-override stance defended by guard-6180 and g-115-9664. Still outside
every scope: tokens that are not files (HTTP routes, parameter names, remote
paths, git refs; g-115-9121), and Bash reads (g-115-8776).

**Trap 5 - a fold's carried text is not the folder's claim (g-115-9664).** Q4 asks
whether THIS session retrieved the source a claim cites. A fold, digest or
extraction exists to carry text it deliberately does not re-derive, so a whole-file
sample grades the carried clauses too, and the better the fold preserves
load-bearing text, the more of it trips `decorative-citation`. Measured 2026-09-10
(alpha worker Body, cc-10, g-335-1537): 5 of 48 clusters sampled, 2 decorative,
both on clauses carried forward verbatim from earlier runs by other agents. Paying
the check literally means re-fetching the sources of every prior run, so under the
whole-file sample no fold could ever close.

THE DISPOSITION (decided 2026-10-13, zeta, hostname cc-02, uname -r
6.8.0-142-generic): carried text is OUT of Q4 scope, and what the folder wrote is
IN. The sampler recognises carried text by a diff, never by a mark.
`--authored-against BASELINE` is the non-git twin of `--authored-since` (which
cannot scope an untracked node, because `git diff` lists no untracked path:
guard-6919). It grades only the lines the single `--artifact` ADDS to BASELINE, a
copy of that artifact as it stood before this goal wrote to it. For a fold,
BASELINE is the pre-fold copy archived before anything is dissolved
(.claude/rules/archive-before-delete.md); for any other edit of an untracked file, copy the file
aside before the first write. An EDITED line counts as added, so rewording another
author's clause to get past the check does not launder it.

Three other dispositions were rejected, each for a reason that holds on any
artifact. A fold-marker convention would let the folder's own assertion about its
own text decide what is graded, the one input a provenance check must not accept; a
diff against a saved copy cannot be asserted into existence. An override would
break the no-override stance that guard-6180 and this goal defend: carried text is
excluded by measurement, never by waiver. Moving folds to a different check was
rejected because a fold's own header and summary are claims like any other.

Measured the same day on a fold-shaped artifact (hostname cc-02): unscoped, the
carried line is a blocking `decorative-citation` and the fold's own archive pointer
is an advisory `unadjudicable-citation`. With the baseline the carried line is not
graded and the pointer stays advisory (guard-6848), so only carried text ever
blocked, and a fold that authored nothing else reads `skipped`, which is not a pass.

What a folder does: keep the pre-fold copy, then
`verify-preflight.sh --goal <id> --artifact <node> --authored-against <copy>`. One
artifact per call: the sampler refuses a baseline beside several artifacts, and the
pre-flight reports that refusal as ERROR, never as a whole-file grade. With no
pre-edit copy, scoping `--artifact` to the goal's own excerpt (rb-12548) remains
the fallback. The CLI accepted the flag before any caller passed it; the pre-flight
now does, and its Q4 fix line names the flag at the moment of the FAIL, which is what
makes the disposition usable and not merely documented (guard-2285). That fix line,
not the verify skill, carries the pointer: the skill is a Tier-1 ratchet file
(hot-path-size-budget.md) and the failure output is where a folder looks.

## Q4 on code artifacts — why only comments and docstrings are sampled (g-115-11480)

**The decision.** A code artifact is sampled on its comments and docstrings only
(`q4_code_scope.scope_code`: python, shell, java/js/ts and lua/luau by suffix; any
other suffix is untouched). The goal named two dispositions. (a) An ADJUDICATED
close: each finding is classified with an owning goal and recorded beside
`q4_verdict=fail`, which then permits `completed`. (b) Never send code lines to the
claim detector. (b) was chosen because it keeps the property Trap 4 and the skill
already defend, that a Q4 FAIL has no override; (a) is an audited override under
another name.

**Why code lines were the problem.** The detector wants an entity signal, an
assertion signal and no source token. Code carries all three without making a claim:
string constants that mirror a hook's output, assert f-strings, date literals, and
shell redirects whose `dev/null` reads as a tree-node key. The one edit that clears
such a finding is a change to correct code. Three of the goal's recorded artifacts,
run 2026-10-13 (alpha worker Body, hostname cc-07, uname -r 6.8.0-142-generic) with
the scope switched off and on over the same session state:

| artifact | scope off | scope on |
|---|---|---|
| the g-115-11082 test file, 212 lines | fail, 3/3 clusters: missing-citation at L53 and L112-115 | pass, 1/1 cluster, 48 lines kept |
| the g-372-58 install script, 307 lines | fail, 2/2 clusters: decorative-citation at L165-227 | skipped, 0/0 clusters, 53 lines kept |
| the g-382-46 test file at its merge commit, 184 lines | fail, 2/2 clusters: missing-citation at L12-20 | fail, 2/2 clusters: missing-citation at L12-20, 30 lines kept |

The skipped row is deliberate: a scoped-empty sample is not a pass (guard-1760) and
the skip reason names the scoping. The third row is the control: it is the comment
block that was first closed as a false positive and then measured to state something
false (the account is in g-115-11480's progress_note), and the scope leaves it
failing.

Two of the six cells depend on the provenance manifest and the other four do not.
`missing-citation` reproduces in any session; `decorative-citation`, and the `pass` in
the first row, need the manifest to hold the sources a cluster cites. Re-run the same
day over an empty manifest (a compaction had cleared the Body's tracker, and the
sampler reported `provenance_manifest: unreadable-or-empty`), the first row's scope-on
cell reads `skipped` (1/1 cluster, no finding, because the decorative test cannot run;
guard-1760) and the second row's scope-off cell reads `skipped` (2/2 clusters, no
finding, for the same reason). Anyone reproducing the table in a session that has not
read the cited sources should expect exactly those two cells to differ.

**Why (a) was rejected.** (1) Trap 4 already rejected an audited override because it
weakens the no-override stance (guard-6180, g-115-9664), and an adjudicated close is
that override with a ledger entry. (2) It was exercised by hand and passed a true
positive: g-382-46 closed `completed` on `q4_verdict=fail`, the finding judged a false
positive, and a later iteration measured the flagged claim as wrong against its
source. A scope is mechanical; an adjudication is a judgment by the executor the
check exists to constrain. (3) (b) keeps the true-positive path open, per the third
row above.

**How much it removes, so nobody reads it as a cure.** Over 4,179 code files (tracked
`core/` and `mind_api/` python and shell plus the world scripts; 1,312,346 lines, 30.1%
kept) the manifest-independent missing-citation count fell from 2,471 findings in
1,362 files to 1,943 in 1,118 (-21.4%), and the worst-case count, with every
checkable citation treated as unread, from 3,215 to 2,319 (-27.9%). By group: shell
scripts 38 findings in 28 files to 0; test files 1,053 in 632 files to 858 in 547;
world scripts 594 in 309 files to 343 in 193; other core python 786 in 393 files to
742 in 378. Of 726 test files with any finding, 133 had every finding on a code line.
The rest sit in comments and docstrings and are fixed there: a goal id inside the
cluster (guard-6180 class a), reworded shorthand (guard-7469), or a whole Read of the
cited source.

**What the removed findings are.** Counted per finding, 648 of the 2,472 whole-text
findings in a second run over that population (2026-10-13, the then-current file set)
have no scoped finding over their lines; the net fall above is smaller because a
cluster that mixed comment and code lines splits into several scoped clusters. A seeded
sample of 30 of the 648 (seed 11480), read one by one: 25 were code constants, test
fixtures or output formatting, none a claim the author made; 4 were message-literal
prose, a hook's text or an operator warning that asserts how the system behaves; 1 was
a claim in a trailing `#` comment (a rate marked bill-verified beside an instance
type). Five of thirty carried an arguable claim, and that is the price of the narrowing.

**Limits, stated because a check whose limits are unstated gets read as total.**
Nothing re-verifies what the scope stops examining. The analyzer has two consumers,
this sampler and the write-path gate, and the gate reads only `world/knowledge/tree/**`
and files that declare `ground_truth: true`; so a fact asserted only in a code string,
or in a trailing `#` comment, now passes Q4 and is looked at by neither. What the scope
still catches is pinned by
`test_the_same_claim_fails_as_a_docstring_and_prose_but_not_as_a_code_constant`, its
`test_MUTATION_without_the_scope_the_code_constant_fails_again` twin and
`test_a_fail_on_a_code_artifact_is_cleared_by_editing_a_comment_alone`. Only python
keeps trailing comments, and a kept `# ` comment is inert: the analyzer reads a line
opening `# ` as a heading (measured with its own functions: one sentence drew a finding
after `//`, after `--` and after `#` with no space, and none after `# `). In python that
leaves docstrings as the only prose that can draw a finding; in shell it leaves none, so
a shell script's Q4 reads `skipped`. That belongs to the shared analyzer and is left
alone. The cluster ordinals change under the scope, so a re-run after this change draws
a different sample than one before it, as with `--authored-since`.

**Not decided here.** Whole-file grading of an edited established file and the
pre-flight not forwarding `--authored-since` (g-115-9310, g-115-11325); the manifest
scope and lifecycle classes (g-115-9266, g-115-9121, g-115-10759); carried-forward
text (g-115-9664, decided in Trap 5 above). Whether this discharges the seven finished goals that wait on a Q4
disposition was not measured: their gates differ per goal.

## Cross-references

- `core/scripts/q4_code_scope.py` — the scope; `core/scripts/q4_provenance_sample.py`
  `run()` applies it before the authored scope (g-115-11480)
- `core/scripts/verify-check-eval.py` — `_disposition`, and the only place the
  flags are produced
- `.claude/skills/aspirations-verify/SKILL.md` — the flag table and Q1/Q2/Q3/Q4
- `core/scripts/q4_provenance_sample.py` — the Q4 check hosted on this trigger
- g-115-4849 (the flag), g-115-5186 (alias tables), g-357-44 (the Q4 scope
  measurement above)
