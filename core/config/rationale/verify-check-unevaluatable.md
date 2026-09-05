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

## Q4 — why the sample is scripted, and its two traps

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
stay defaulted. `context-reads.tracker_path` routes to the per-Body tracker
`sessions/<sid>/body-context-reads.txt` when that Body has a forked body-WM file,
and to the agent-wide `session/context-reads.txt` otherwise — and on a worker Body
the agent-wide file may not exist at all. Measured 2026-09-03 (alpha worker,
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

## Cross-references

- `core/scripts/verify-check-eval.py` — `_disposition`, and the only place the
  flags are produced
- `.claude/skills/aspirations-verify/SKILL.md` — the flag table and Q1/Q2/Q3/Q4
- `core/scripts/q4_provenance_sample.py` — the Q4 check hosted on this trigger
- g-115-4849 (the flag), g-115-5186 (alias tables), g-357-44 (the Q4 scope
  measurement above)
