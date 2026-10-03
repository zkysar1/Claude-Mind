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

## Why the SOURCE gate keys on claim-HOLDING, not role (g-306-524)

`goal_eligibility` above is FIELD-keyed (`executable_by_role`) and SKILL-keyed
(the bridge). An **agent-queue goal** (`source == 'agent'`) carries NEITHER: the
field is unset and it is usually skill-less. So the bridge answers `undetermined`
on exactly the population a worker can **structurally never claim from its box**.

The structure, measured 2026-09-11 (relayed by the alpha reducer at spark
replay, from worker Bodies cc-07/08/09/13 and DESKTOP-O91DLK2, 2026-09-06..11):

| fact | value |
|---|---|
| agent-queue goals a worker's box CANNOT claim, in the scored pool | 24 of 2058 candidates |
| …of those in the **top 50** the scorer hands the worker | 7 |
| verdict `goal-eligible` returned for each | `undetermined` (a decline, read as a pass) |

Why the refusal is STRUCTURAL, not a transient: an agent-queue goal is claimable
only on the box holding that agent's DDB runner claim. `owncloud_backend` refuses
every other box `no_claim`, and its own comment says the refusal means "no retry
can ever succeed from here". So on a non-claim-holding box the goal is not broken
and not the reducer's — it is simply unreachable from this machine, and the right
word is the same one a worker already knows how to act on: `reducer-only` (skip,
take the next candidate, file nothing, do not burn a select cycle).

**The key is claim-holding, deliberately NOT role.** A worker co-resident on the
claim box CAN claim the goal (g-001-06 closed `completed_by_role=worker` from
cc-08, 2026-08-31). Keying the gate on "is the reader a worker?" would fence the
majority direction — the goal is reachable from exactly the box the worker is
sitting on — so the predicate is "does THIS box hold the agent's live runner
claim?", and the role/skill logic runs on the box that holds it.

**The probe reuses the ONE ownership implementation** rather than re-deriving
(a runner-token scan, a raw claim-table read, a subprocess):
`owncloud_sync._owned_agents_with_provenance` is the same SSOT the write-side
`no_claim` gate consults in `owncloud_backend._put`, so the SELECT-time gate and
the claim endpoint cannot disagree about who may claim. Three copies of the
ownership predicate would be three things to keep in sync (guard-130), and the
freshness threshold (`OWNERSHIP_STALE_SECONDS`) is already the value
`reclaim_if_stale` enforces for the lock-break.

**Provenance is the verdict, not a boolean.**

| provenance | meaning | gate answers |
|---|---|---|
| `local-backend` | a single machine, NO claim store exists; the daemon's `no_claim` machinery is absent (`no_claim_error` is the empty tuple off own-cloud) | falls through — every queue is claimable on the box |
| `live-claims` | a real judgment, both directions | `agent` in the owned set → eligible path; otherwise `reducer-only` |
| `unknown-machine` / `transient-error` / any exception | the box CANNOT prove what it holds | `undetermined` |

The unreadable-provenance row degrades to `undetermined` on purpose, and this is
the load-bearing half of the design (g-115-8028's direction, which fires ONLY on
provenance `live-claims`). A `reducer-only` verdict asserts a STRUCTURAL
impossibility — "no retry can ever succeed from this box". Making that
assertion on a claim table the box cannot read is the confident-and-wrong error
the `no_claim` gate's own comment names: it would fence a legitimate goal behind
an infrastructure fault. The same three-way fail-open the rest of this module
uses (guard-1760 — a checker may not report what it declined to look at as a
pass, and must not assert the opposite on an unreadable signal either).

**Why the claim-holding box answers `eligible`, not `undetermined`.** On the
box that HOLDS the claim, the can't-judge bridge verdicts (skill-less goal,
unmapped skill) concern **ownership** — "is this goal reducer-only *work*?" —
not **claimability**, which the gate has already settled: the claim endpoint
accepts from here. So the verdict word is `eligible`, and the gate appends its
caution to the bridge's reason rather than erasing it. This is NOT a loosening of
any fence: the refusal paths (role `reducer`, a reducer-only skill, the
worker+refused-skill contradiction) return BEFORE the promotion, so no fence ever
reads through it as a pass. The promotion exists so that the one case the defect
named — the skill-less agent-queue goal — resolves to an actionable `eligible` on
the box that can actually take it, instead of a decline that reads as a pass on
every box and a structural refusal on the one box that is reachable.

**Scope — what this gate does NOT fence.** `source == 'agent'` fires it. A
`cross-agent:<owner>` row is the peer-queue variant (g-115-9230, a separate
goal) and is deliberately NOT fenced here; a `world` row passes through. The
`--source` / `--agent` flags both precede the skill arg (argparse REMAINDER —
the same load-bearing ordering as `--role`, one section up). The `--agent` flag
defaults to this session's agent because an `agent` row in a worker's own pool is
its own queue; it is named explicitly only when judging a row read elsewhere.

g-306-514 source-gated the **DRAIN LANE** (`goal-selector.py`). This is its
**SELECT-time twin**: the drain lane moves a goal's ORDER in the pool, this
settles whether the worker may CLAIM it at all.

## Why the walk judges the WHOLE ranking, and filters BEFORE the cut (g-375-53)

Phase 1 used to cut the ranking to 10 or 40 rows and only then ask
`goal-eligible` once per row, a loop of up to 40 calls that the Body typed by
hand. On 2026-09-28 00:09Z a worker Body on a small local model ran
`select --top 40`, made **0** gate calls, and parked on "SELECT returned no
eligible goal ... agent-queue-fenced ... reducer-only". Its transcript shows one
selector run and five record reads, and nothing else. Measured from another
seat at 00:35Z, 33 of that top 40 passed the gate, and two sibling Bodies
claimed its rank-1 and rank-2 goals within 30 min. A small model skips a
40-step loop; it does not skip one command. `worker_execute.py select-walk`
makes the whole Phase 1 check that one command.

**Why before the cut.** Filtering after a fixed cut lets a run of reducer-only
rows at the top empty the view while claimable work sits at rank 41. On the
2026-09-28 queue that could not happen (26 of 2,923 candidates were
reducer-bound), but the order was wrong, and fixing it costs one pass over rows
already in memory. The walk keeps the scorer's order (guard-5135): dropping a
row moves nothing else.

**Why on the worker side, not in goal-selector.** The selector is ONE component
both roles run, and `LIFECYCLE_DISPOSITIONS["select"]` forbids worker logic
inside it ("There is no worker-specific selection logic and there must not be
one"; `test_selection_stays_role_blind`). The first cut of g-375-53 put the
walk in the selector's `--top` view and that test refused it. So the walk runs
the unchanged selector once (`--top` wide enough to hold every row) and judges
its output here, which is the worker loop consulting the contract, mechanized.

**Why a supply park must answer the census.** The walk records what it showed
(`sessions/<SID>/select-census.json`). `body-manifest.py park --supply-gap`
refuses, exit 5, until every row in it is claimed or named in
`--decline <goal-id>=<reason>`, and until the view is fresh and the walk went
deep enough (`--top 40`, or a shorter view that exhausted the ranking). A Body
that never asked the gate has no census, so it cannot declare "no work". The
reducer-gone park (Phase 0.5) and the loop-exhaustion fence pass no flag and
are unchanged. The declines are the record a later `requires_capability` pass
can mine for locus-bound goals.

## Why the role read runs UNDER the claim (g-375-110)

Phase 1 used to have a worker read an `undetermined` row's record with
`aspirations-query.sh --full` BEFORE claiming it, and Phase 2.9 then read the
same record again from the claim response. `undetermined` is the common
verdict: 919 of 938 live candidates name no skill (above), so the bridge
declines on most rows. Measured 2026-10-02 on the zc worker Bodies, after the
cc-04 reducer lapse, from transcripts (tool, script and flag names and goal ids
only) and the Body traces:

- One model call took 33 to 735 s, so every read before the claim cost minutes.
- Select-walk to claim took 22.4 min on zc-02 and 12.0 min on zc-08; zc-04 had
  no claim after 46 min. Over 31 episodes the median was 10 tool calls from
  select-walk to claim (g-375-110 note g110-baseline-1002).
- Convergence: g-115-8602 drew three alpha workers in 35 min. zc-06 read it at
  00:43:49, another worker session closed it at 01:09:27, and zc-08 claimed it
  at 01:12:47 off its 01:00:45 walk. A row nobody has claimed is visible to
  every walk, so each sibling's read bought nothing.
- Double reading: zc-08 ran `--full` on g-115-8602 before its claim (01:03:36)
  and again after it (01:14:21).

The read before the claim existed so that no worker EXECUTES a reducer-only
goal (g-115-5664, above). That needs the read before the first ACTION, which is
where guard-2803 places it, not before the claim, and Phase 2.9 runs before any
execution. So the claim moved first: a worker claims the first eligible or
undetermined row straight after select-walk and judges the role from the claim
response, which IS the full record. A misfit is released unstarted with
`--reason-kind role`, so its `release_negatives` entry is typed and countable
(g-115-8163), and the next Body to claim the goal reads why the last one let go.

What claim-first costs: a misfit row is held for one record read, and siblings
walk past it meanwhile. The old order charged that same read to every sibling,
in parallel, on rows any of them could take. After the release the Body claims
the NEXT row of the same walk rather than walking again: neither the scorer nor
select-walk reads `release_negatives`, so a new walk would rank the released
row first and hand it straight back. A Body that meets the row on a later
cycle claims it again and reads the `role` entry, which names why the last
Body let go, in the claim response at Phase 2.9. A released row also stays in
the walk's census, and `supply_gap_refusals` asks a decline for every census
row, so the supply-gap park text names released rows among those to decline. Reducer-STAMPED rows are not
affected: the selector never shows them to a worker, and the claim-boundary
recheck above (g-306-449) still warns on one stamped after it was scored; its
message now names the same `--reason-kind role` release.
