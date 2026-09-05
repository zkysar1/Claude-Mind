# WHY the worker's claim gate reads `executable_by_role` FIRST

Companion to `.claude/skills/worker-loop/SKILL.md` Phase 1 (ROLE ELIGIBILITY).
The SKILL.md keeps the imperative; the narrative and the measurements live here.

## The incident the gate exists for (g-115-5664)

Measured 2026-08-10 on cc-08: `goal-selector` offered a WORKER Body g-001-05
"Run hippocampal replay" (skill `/replay --sharp-wave`) as the top pick, with
the drain-lane banner reading verbatim:

> This IS the sanctioned top pick — claim it without a deviation code.

`/replay` calls `guardrails-add.sh`, and `LIFECYCLE_DISPOSITIONS["replay"]` is
`reducer-only-by-design` — so a worker following that banner writes guardrails
derived from its own UNMERGED state (the Nth-reducer defect the convergence
forbids), and the artifacts land in the shared world with nothing marking them
pre-merge. It was caught only by opening the skill before claiming; nothing in
the loop prompted that.

It recurred. g-306-284 (which pushes main) and g-115-6886 (which clears the
agent-wide working memory) both reached worker Bodies behind the SKILL-keyed
bridge's skill-less branch, and the drain-lane banner affirmatively promoted
g-306-284. Four first-hand encounters by 2026-09-04.

## Why the GOAL declaration outranks the SKILL bridge (g-306-440)

`executable_by_role` is a deliberate assertion by the goal's author.
`skill_eligibility` is an INFERENCE over a field 919 of 938 live candidates do
not carry. The declaration is therefore consulted first and is decisive where
present; the bridge is the fallback.

`goal_eligibility()` shipped with g-115-7372 and its **only caller was its own
CLI** — the loop still called the role-blind `skill-eligible`, so the gate was
inert on every box. That is guard-1943 exactly (pinning the writer says nothing
about the wiring): the function's own tests stayed green through the entire gap.
The regression tests now assert the CALL SITE in SKILL.md, not just the
function.

## Why `undetermined` is a WORD and not an exit code

Two branches of the bridge cannot answer: a goal naming no skill, and a named
skill the lifecycle table does not map. Both returned `eligible=True` with the
refusal written only into `reason`, and the CLI printed the literal word
`eligible`. A caller reading the verdict rather than the prose saw a PASS — the
guard-1760 class ("a checker must not report what it declined to look at as a
pass").

**The exit code deliberately does not move, and the next reader must not
"finish" this by making it non-zero.** rc is the FAIL-OPEN axis. A non-zero rc
on the can't-judge branch converts "I have no key for this" into a REFUSAL for
~98% of the queue and strands the worker role outright — the exact failure
`skill_eligibility.__doc__` forbids. Discrimination and fail-direction are
different axes and must not be fused: rc stays 0/1, the VERDICT WORD carries
the third state.

## Why the flag ORDER is load-bearing

`skill` is argparse `REMAINDER` (deliberately — guard-920: the production arg
shape is the skill field verbatim, args and all). So a TRAILING `--role` is
swallowed as skill text. Measured on cc-10, 2026-09-04:

| invocation | stdout | rc |
|---|---|---|
| `goal-eligible --role reducer ""` | `reducer-only` | 1 |
| `goal-eligible "" --role reducer` | `undetermined` | 0 |

The trailing form is the natural way to write it and it fails OPEN, so the
mis-ordered call would have read as a cleared check. Two defenses: the SKILL.md
call site puts `--role` first, and the mis-ordered form now surfaces as
`undetermined` rather than `eligible`
(`test_role_flag_must_precede_the_remainder_skill_arg`).

## Why the banner branch does not break the selector's role-blindness

`LIFECYCLE_DISPOSITIONS["select"]` says there is no worker-specific selection
logic and there must not be one; guard-2783 forbids role-conditional behavior
in a component BOTH roles run. The banner's conditional reads a **field on the
goal** (`reducer_selection_policy.is_reducer_only_row`), never who is reading —
both roles get byte-identical bytes for the same row, and the routing call
stays with the reader in worker-loop Phase 1. Pinned by a test that calls the
emitter twice and asserts equality, alongside the existing raw-source greps
forbidding `skill_eligibility` / `worker_execute` in `goal-selector.py`.

Note the asymmetry that makes the banner worth fixing at all rather than
relying on the Phase 1 gate: a banner waiving the deviation code is the
STRONGEST claim-permit the selector emits, so emitting it over a reducer-only
row actively defeats the fence rather than merely failing to help.

## Why the gate runs AGAIN at the claim boundary (g-306-449)

The Phase 1 call judges the **scored row**. On a worker that row's
`executable_by_role` is null for every candidate it can ever see, so the
select-time gate sits downstream of a filter that already removed everything it
could catch.

Measured 2026-09-05 (alpha, cc-13, own-cloud), one worker pass:

| population | count |
|---|---|
| emitted scored rows | 1859 |
| …carrying the `executable_by_role` key | 1859 (100%) |
| …with a NON-NULL value | **0** |
| live goals stamped `reducer` in the store | 42 |
| …present in that pool | **0** |
| …with no competing explanation (not deferred, not claimed) | 27 |
| control: comparable unmarked pending goals present in the pool | 1141/1296 (88%) |

The mechanism is `goal-selector.py`'s
`_skip_reducer_only = (_role != ROLE_REDUCER)`, which drops reducer-only rows
before emission. So the 100%-null corpus cc-08 and cc-10 both measured is not
evidence that the field is unwritten — 61 goals carry it — nor that the
pass-through is broken. It is the filter working. Read that way, the goal's
original title ("non-deterministic … so the worker role gate's input is
unreliable") describes a defect that does not exist: the two runs that
disagreed were separated by a real peer write, not by non-determinism.

**What survives is a narrow, real TOCTOU.** A goal scored while unstamped can be
stamped `reducer` before the worker claims it — observed live: a peer write at
`2026-09-05T02:38:25` stamped a goal `reducer` while it sat at RANK 1 in a
worker's pool. Phase 1 consulted the pre-write null; nothing consulted the
record again.

The claim response is the fix's input because it is the whole record, verified
rather than assumed (guard-4003 — a criterion that asks a store for a field it
does not carry is unsatisfiable forever and fails as a plausible "not yet"). On
cc-13 the same date, a real claim response returned 25 keys against 28 in the
store record; the only three absent were the query wrapper's own
`asp_id`/`goal_id`/`source`, i.e. **zero goal fields dropped**.

`claim-role-recheck` therefore reuses `goal_eligibility()` on the fresh record —
one role implementation, never a second copy (guard-2676). It is a CODE gate
rather than a prose instruction because a "the LLM must check X" step with no
executable backing is the shape guard-399 forbids.

**Fail-open is deliberate and load-bearing.** An unreadable response, a
non-dict, or an unrecognised role value all return `undetermined` / rc 0. The
corpus is ~100% unstamped, so a fail-closed default would fence off nearly every
goal a worker could legitimately take — the severity finding cc-08 already made
against the fail-closed form of this criterion. Only an explicit `reducer`
refuses.

**What the fixtures do NOT cover** (guard-1462 — name the excluded layers): they
inject at the RECORD level, so they say nothing about the claim wrapper actually
producing the file, nor about the worker actually invoking the gate. The null
branch of that upstream path IS covered live (a real claim response from the
production wrapper returned `undetermined`); the `reducer` branch is not
reachable end-to-end from a worker without stamping a goal solely to trip its
own fence, so it is fixture-covered only.
