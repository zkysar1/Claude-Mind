# Multi-Agent Coordination Convention

## Overview

Multiple agents share a world goal queue and communicate via the message board.
Coordination follows the CAID pattern (Centralized Asynchronous Isolated Delegation):
structured protocols (typed messages + tags), claim-based isolation (prevent duplicate
work), dependency graphs (`blocked_by`), and self-verification (review gate).

Based on ["Effective Strategies for Asynchronous Software Engineering Agents"](https://arxiv.org/html/2603.21489v1)
(Geng & Neubig, CMU) and ["Language Model Teams as Distributed Systems"](https://arxiv.org/abs/2603.12229).

## Agent Roles

Defined in `world/program.md`. Each agent has a distinct role:
- Roles are complementary, not competing — avoid overlapping work
- `participants` field on goals routes work to the right agent
- `claimed_by` field prevents duplicate execution at runtime

## Claim Protocol

World goals MUST be claimed before execution. See `aspirations.md` for full details.

```
claim (before Phase 4) → execute → complete-by (Phase 5.3, on success)
                                  → release (on failure/revert)
```

Scripts: `aspirations-claim.sh`, `aspirations-release.sh`, `aspirations-complete-by.sh`

**Key rules:**
- On claim conflict (exit non-zero): re-enter the selection loop, do not retry
- **A claim is `claimed_by` + `claimed_at` + `claimed_by_sid`, and they travel as
  ONE unit.** The sid distinguishes two live instances of the SAME agent — a
  shape the fleet produces by design (one agent name on multiple boxes; observer
  sessions coexisting with a live runner). Since g-306-132-c the cross-box merge
  registers a conflict on same `claimed_by` + DIFFERENT `claimed_by_sid`, not
  only on differing agent names, and resolves to the older `claimed_at`
  (claimed_at tie → lexicographic agent, then sid; byte-commutative in both
  operand orders). Before that fix `claimed_by_sid` was not merged at all, so a
  record could carry one session's `claimed_by`/`claimed_at` beside another's sid.
- **`aspirations-claim.sh` still keys exclusion on the AGENT NAME**, so a second
  session of the same agent CLAIMS SUCCESSFULLY — nothing refuses it. The merge
  fix is detective, not preventive: the loser learns it was displaced on its next
  `stranded-claim-sweep.py` run (verdict `possible-displacement`), not at claim
  time — and that verdict is AMBIGUOUS by construction: the execution diary it
  reads is `sync_tier: continuity` (per-agent, S3-authoritative) with no session
  id on entries, so it cannot separate "I was displaced" from "a peer instance
  legitimately holds it". Treat it as a prompt to check, never a conclusion
  (g-306-143). The pre-emptive signal is still `guard-1460`'s board read.
- **If you are the LOSER of a claim conflict, ABORT — do NOT release.** After the
  merge the claim belongs to the winner, who is alive and mid-execution;
  releasing clears a live holder's claim and re-opens the goal to a third
  instance. Abort is local, release is a write to state that is now someone
  else's. (`rb-6503`.)
- On infrastructure failure: release claim so other agent can attempt
- Recurring goals: `complete-by` auto-clears claim for next cycle
- Session end: release all held claims in handoff
- **ANNOUNCE a release on the board, as `--type release`** — a claim you
  announced but abandon silently becomes a permanent lien on the goal. If you
  posted a `--type claim`, post the matching release: `board-post.sh --channel
  coordination --type release --tags "<goal-id>,<agent>"`, or open the text with
  `RELEASING <goal-id>`. `goal-pickup-coordination-check.py` pairs claim→release
  per author and lets the LATEST event win, so an announced release CLEARS
  `race_risk` for every other agent; an unannounced one cannot. Measured cost of
  getting this wrong (g-115-3459, 2026-07-27): one abandoned claim stalled
  g-335-292 ~3h across two agents, and took two hand-written board posts to
  break. `aspirations-release.sh` updates the QUEUE; it does not post to the
  board — these are two separate obligations.

## Board Communication

Use typed messages for all board posts. See `board.md` for the full type table and tag taxonomy.

**Actionable types** (scan during idle/boot):
- `escalation` — goal stuck after repeated failures, needs help
- `review-request` — code change needs peer review
- `handoff` — goal done, follow-up needed by other agent
- `blocker-alert` — shared resource blocked, affects multiple goals
- `directive` — strategic direction or priority change

**Noise types** (filter out when scanning for work):
- `status`, `claim`, `release`, `complete`

### Board Scan Protocol (Boot + Idle)

At session start (boot/prime) and during idle time (all-blocked path), scan for
actionable messages from other agents:

```bash
# Escalations — goals the other agent couldn't finish
board-read.sh --channel coordination --type escalation --since 12h --json

# Review requests — code changes awaiting peer review
board-read.sh --channel coordination --type review-request --since 12h --json

# Handoffs — completed work needing follow-up
board-read.sh --channel coordination --type handoff --since 12h --json

# Blocker alerts — shared resources down
board-read.sh --channel coordination --type blocker-alert --since 12h --json
```

For each actionable item: create an investigation or follow-up goal if one doesn't
already exist. Dedup against existing goals by title similarity.

## Circuit Breaker (Escalation)

After 3 consecutive failures on the same goal within a session:

1. Post `escalation` + `urgent` to coordination channel with goal ID
2. Defer the goal: `defer_reason = "Circuit breaker: 3+ consecutive failures, escalated via board"`
3. Other agent picks up during board scan, creates investigation goal
4. Original goal undefers when investigation resolves or `defer_reason` is manually cleared

The circuit breaker operates across goal-selection iterations (not within a single
execution — that's the episode chain protocol in `aspirations-execute`).

## Review Gate (Async Code Review)

After completing a world goal that involves code changes:

1. Executing agent posts `review-request` to coordination channel with goal ID
2. Executing agent sets `review_requested` timestamp on the goal
3. Reviewing agent scans for `review-request` during idle time
4. Reviewing agent reads the experience trace and checks for issues
5. Posts result: `complete` (review passed) or creates investigation goal (issues found)

Review is **asynchronous and non-blocking**. Goals are NOT held pending review.
The reviewing agent picks up reviews during idle time, catching bugs faster than
purely retroactive review without creating bottlenecks.

### Blocking Tier: async is the rule for tier <= 1, tier 2 BLOCKS (g-357-40 / g-357-42)

The paragraph above is the rule for **tier 0 and tier 1 only**. A **tier-2** close is
**BLOCKING**: the goal does not reach `completed` until an independent APPROVE verdict
exists. The SDLC principle it enforces is that **the author must not approve their own
close** — the async lane cannot enforce that, because it lets the close land first and
reviews afterwards.

Motivating incident (coach g-012-02, g-357-39): a goal enumerating 16 named entities
closed green with 6 of them substituted by famous-name priors. Its verification
criterion was count-based — "all 16 entries present" — so the count matched while the
identities did not, and the author self-graded against it. No criterion of that kind
can catch the defect; an independent reader is the remedy. Reproduced end to end in
`core/scripts/tests/test_close_review_coach_fixture.py`.

**Risk tiers** (SSOT: `core/scripts/goal_close_risk_tier.py::classify` — this table
documents it, it does not define it):

| tier | condition | review cost |
|---|---|---|
| **0** | recurring AND `outcome_class: routine` AND no new artifacts. Checked FIRST and short-circuits, so a recurring routine sweep that touches a framework file is still tier 0 | none — the cadence must stay affordable |
| **1** | default: no tier-2 trigger fired | none |
| **2** | ANY one trigger below, each independently sufficient | APPROVE verdict required |

Tier-2 triggers: `entities` (>= 3 distinct id-shaped tokens in title/description),
`user_truth` (participants include user, or directive/mission-sourced), `deliverable`
(new tree node or user-facing output), `framework` (touched `core/` or `.claude/`),
`high_prio` (HIGH priority, non-recurring), `first_of_asp`.

**Verdict artifact** — `agents/<agent>/session/close-reviews/<goal-id>.json`:

```json
{
  "verdict": "APPROVE",
  "reviewer": "<the reviewing agent's MIND key, which MUST differ from the executor's>",
  "checks": ["source-fidelity: artifact identities vs description identities", "..."],
  "findings": ["..."]
}
```

`verdict` is compared **exactly** against `APPROVE` (case-insensitively). Everything
else — `REJECT`, a missing file, an unparseable file, an absent field — leaves the
close refused. That exactness is the reviewer's power to say no, and it is pinned by
`test_a_REJECT_verdict_does_NOT_satisfy_the_gate`: relaxing it to a truthiness test
keeps every other close-review test green while silently closing every rejected goal.

Independence is the MIND-level rule below, unchanged: a different `claimed_by_sid` does
NOT satisfy it, and where no other mind is available the request stays OPEN.

**Fail directions.** Everything degrades to tier 1 (no review) on a classifier or
config fault — a gate that blocks work because of its own bugs is worse than the
problem it catches (guard-142). The single exception, and the reason the gate exists:
the **absence** of an APPROVE verdict on a tier-2 goal is a REFUSAL, never an error.
Overrides take `--override-close-review "<justification>"` and land in
`world/close-review-overrides.jsonl` (the per-gate ledger, NOT the `--override-all`
bulk ledger, whose `slots_filled` field means blast radius across gates).

**Ship state:** both flags default OFF in `core/config/aspirations.yaml`
(`close_review_gate.enabled`, `.note_marker_enabled`). Check A's remedy names a
producer — the fresh-eyes close reviewer, sibling goal g-357-41 — that has not landed;
enabling before it exists would force every tier-2 close onto the override path and
manufacture false records in the ledger that measures whether to enable at all
(rb-4452: ship a dep-blocked gate's invariant BEFORE the dependency so it constrains
that dependency's design).

### Independence Is At The MIND Level, Not The Session Level

**The reviewing identity MUST differ from the executing identity by MIND KEY —
the agent name — not merely by session id.** Under the Mind/Body split a single
mind can run several Bodies concurrently (separate sessions, possibly separate
boxes). Those Bodies share one `self.md`, one identity, one accumulated set of
priors, and therefore one set of blind spots. Body B of agent A reviewing Body
A's work is the same reviewer reading its own output through a different
terminal: it satisfies "a different session looked at it" while delivering none
of what the gate exists to buy.

Concretely:

- A review request is satisfiable ONLY by an agent whose name differs from the
  executing goal's `claimed_by` / completing agent. A differing
  `claimed_by_sid` alone does NOT satisfy it.
- When no other mind is available, LEAVE the request open. A review gate that
  auto-satisfies under scarcity reports coverage it does not have — the same
  failure shape as a completeness tool counting what it declined to look at
  (`guard-1760`). An unclaimed review request is honest; a same-mind review is
  not.
- This is a property of the GATE, so it binds every entry point: the async
  review-request flow above, the Deep Review Protocol below, and any future
  automated reviewer. A reviewer that cannot establish the executor's mind key
  must treat independence as UNPROVEN and decline, not assume.

The session id remains the right discriminator for *contention* questions (who
holds a claim, which Body wrote a record). It is the wrong discriminator for
*independence* questions. Do not reuse one for the other.

### Deep Review Protocol (Hypothesis-Driven Review)

Surface-level pass/fail reviews waste a learning opportunity. The deep review protocol
replaces the basic "check for issues" step with a 5-phase hypothesis-driven process
that produces testable predictions, detects downstream risks, and feeds into the
normal pipeline resolution cycle.

**Phases:**

1. **Context Loading (R1)**: Load the full experience trace via `experience-read.sh --goal {goal_id}`,
   read the content `.md` file referenced in the trace, and load the goal description with its
   verification outcomes. The reviewer must have the same evidence the executor had.

2. **Architectural Assessment (R2)** — answer three questions:
   - **Q1: Verification match** — Do ALL verification outcomes match the claimed result?
     Compare each verification check against the experience trace. Flag any mismatch
     between claimed outcome and actual evidence.
   - **Q2: Downstream dependents** — What goals depend on the changed artifact?
     Use `goal-selector.sh blocked` and scan for goals whose `blocked_by` or description
     references the same artifact, file, or module touched by the change.
   - **Q3: Knowledge/hypothesis invalidation** — Does the change invalidate existing
     tree knowledge or active hypotheses? Use `tree-find-node.sh {artifact_or_topic}` and
     `pipeline-read.sh --stage active` to check for stale facts or assumptions that
     depended on the pre-change state.

3. **Hypothesis Formation (R3)**: Form a testable prediction about the change's downstream
   impact (e.g., "Change to X will cause Y in the next N executions"). Apply the same
   calibration gate used in spark Step 0.5 — read recent accuracy for the `code_review`
   category from resolved pipeline entries, compute `recent_accuracy`, and cap confidence
   accordingly (< 0.40 accuracy caps at 0.55, < 0.60 caps at 0.65, < 0.80 caps at 0.80).
   Add to pipeline via `pipeline-add.sh`, which reads a JSON record on STDIN (NOT
   `--type`/`--horizon` flags — the flag form sends an empty body and the wrapper
   still exits 0). Use `type: calibration`, `category: code-review`, `horizon: short`,
   and put the prediction in `position`. Confirm success from stdout (it has `id`+`stage`,
   no `error` key) — the exit code is 0 even on a validation error.

4. **Findings Post (R4)**: Share the review hypothesis on the findings channel with
   `board-post.sh --channel findings --type finding --tags "code_review,{goal_id}"`.
   This ensures both agents see the architectural assessment and prediction.

5. **Outcome Tracking**: The review hypothesis resolves through the normal pipeline cycle.
   When the predicted downstream effect is observed (or not), the hypothesis is confirmed
   or corrected via `pipeline-move.sh`, producing a standard learning signal. No special
   resolution mechanism is needed — the existing pipeline handles it.

**Issue handling**: If the architectural assessment (R2) reveals concrete issues, the
reviewer creates an investigation goal as before. The deep review protocol augments
the existing review gate; it does not replace issue detection.

**Cost bound**: Maximum 3 deep reviews per B0 scan iteration. Deep reviews require
loading full experience traces and running multiple queries, so the cap prevents
unbounded context growth during the all-blocked path.

### Duplication-Gate Interaction (Reviewer's Response Goal)

When the reviewer creates an Investigate goal citing the review target's files,
`goal-duplication-gate.py` may fire because the executor's recent completion
touches the same file. The gate's `_expected_coverage_paths` carve-out
(g-115-289 + g-115-359) clears the false positive when both conditions hold:

1. The responding goal's `origin_signal` starts with one of
   `_RESPONSE_ORIGIN_PREFIXES` (`investigate:`, `idea:`, `maintain:`,
   `unblock:`) — the goal is a response, not de-novo work.
2. A fresh (≤24h) cross-agent signal cites the file via an `affects:<file>`
   tag. Two signal sources are scanned and unioned:
   - `world/board/findings.jsonl` entries with `insight_trigger` tag
     (g-115-289 — original source for fresh-eyes-code outputs).
   - `world/board/coordination.jsonl` entries with `type=review-request`
     (g-115-359 — added so cross-agent code reviews clear the gate without
     a dual-post pattern).

#### Convention: review-request posts include `affects:<file>` tags

When posting a `review-request` for a code change, **include one
`affects:<path>` tag per changed file**:

```
echo "Review request <goal-id>: <short summary>" | \
  board-post.sh --channel coordination --type review-request \
    --tags "review,affects:core/scripts/foo.py,affects:core/config/conventions/bar.md,<goal-id>"
```

The reviewer's response Investigate goal — filed with
`origin_signal: "investigate:<short-reason>"` — then clears the
duplication gate without `--override-duplication`. The gate falls back to
override-only behavior when `affects:` tags are absent (older posts,
ad-hoc reviews); the convention is not retroactive and not enforced.

#### Override path (low-frequency or untagged review-request posts)

```
aspirations-add-goal --override-duplication "cross-agent review of <commit>: file overlap is intentional" ...
```

Each override is logged to `world/goal-duplication-overrides.jsonl` for
audit. Use when the upstream review-request lacks `affects:` tags, or for
one-off cases where the convention does not apply.

## Dependency Chains (Goal Creation)

When creating goals that depend on other goals:

1. **Always populate `blocked_by`** with prerequisite goal IDs
2. **For code-change goals**: include file paths in the description
   (prefix with `Touches: path/to/file1, path/to/file2`)
3. **Use `participants` field** for static routing:
   - `["alpha"]` — only alpha can execute
   - `["bravo"]` — only bravo can execute
   - `["agent"]` — any agent (default)

The `blocked_by` field resolves globally across all active aspirations.
`goal-selector.py` enforces blocks recursively — a goal blocked by a blocked
goal is also ineligible.

### Output-Passing Dependencies (`depends_on`)

For cross-agent workflows, use `depends_on` alongside `blocked_by` to pass factual
outputs from a completed goal into a dependent goal. When the upstream goal completes,
the verify skill reads its `handoff` board message and injects the output into the
downstream goal's description. See `goal-schemas.md` for full schema and
`aspirations-verify/SKILL.md` for the injection protocol.

### Self-Abstention

Agents can decline goals outside their capability band via `abstained_by`. The
goal-selector skips abstained goals for the abstaining agent; other agents see them
normally. See `goal-schemas.md` and `aspirations-select/SKILL.md` Phase 2.55.

## Co-Investigation Protocol (g-115-563)

Existing primitives are sequential: claim, handoff, board-post, review-gate.
None describe how two agents iterate concurrently on the SAME parent
investigation, posting interim findings to a shared thread until both halves
inform each other. The empirical baseline (30 days through 2026-05-09):
zero goals carried `parent_goal_id` or `parent_id` schema fields; reasoning-
channel reply rate was 0% across 16 posts. The framework had four
ingredients (`team-state.in_flight`, `board/reasoning.jsonl`,
`discovered_by`, `related_goals`) that COULD enable co-iteration, but none
were wired together.

Full design rationale + 30-day baseline: co-investigation-protocol design (2026-05-09, git-archived).

### Schema additions

| Field | Where | Type | Purpose |
|---|---|---|---|
| `co_parent_id` | goal | str (goal-id) or null | "this is a sub-goal of co-investigation X" |
| `co_investigators` | aspiration or top-level goal | list of agent names | "agents committed to co-iterate on this" |

Both fields validated by `aspirations.py` (`validate_goal`, `validate_aspiration`):
`co_parent_id`, when non-null, MUST match `GOAL_ID_RE` (`g-NNN-NN[-a]`); `co_investigators`
MUST be a list of strings. No other enforcement at schema layer — the protocol
is consumer-driven (selector + board), not prescriptive.

### Board types

- `co-investigation-claim` — replaces individual `claim` for a co-invest
  parent. Reserves the parent across both agents; lists the agreed sub-goal split.
- `co-investigation-update` — interim findings posted to the parent's thread.
  Both agents post; both read. Tag with the parent's goal-id so retrieval
  via `board-read.sh --tags <parent-goal-id>` returns the full conversation.

### Selector adjustment

`goal-selector.py` adds `co_invest_alignment` (weight 0.5,
`meta/goal-selection-strategy.yaml`). Bonus value is `1.0 raw` when this
candidate's `co_parent_id` matches a partner's live
`team-state.agent_status.<other>.in_flight.co_parent_id`, biasing the selector
toward "pair on the same parent right now." Small magnitude — co-investigation
is opt-in coordination, not a hard override of priority/recurring-urgency.

### Acceptance criteria for "co-investigative"

A test case is co-investigative when:

1. Both agents post ≥3 entries to the reasoning channel under the shared tag during the session
2. Each agent's findings reference the other's at least once
3. The output (tree node, goal, or report) lists both agents
4. Neither agent could have produced the same output alone in the same wall-clock time

### First test case shape

The pattern: one shared Investigate parent goal under a tracking aspiration,
two agents each take a distinct slice of the pipeline (e.g., agent-a takes
the upstream scoring path; agent-b takes the downstream materialization
path). Both halves NEED the other's findings to make sense — forces actual
collaboration, not parallel work.

## Restricted Files (Concurrent Modification Prevention)

All shared JSONL files are protected by `_fileops.py` file-level locking.
Agents MUST use scripts (never direct Edit/Write) for:

- `world/aspirations.jsonl` — via `aspirations-*.sh`
- `world/board/*.jsonl` — via `board-post.sh`
- `world/knowledge/tree/_tree.yaml` — via `tree-update.sh`
- `world/pipeline.jsonl` — via `pipeline-*.sh`

### Cross-Agent Direct Edit: Sanctioned Exception for `agents/<agent>/session/pending-questions.yaml`

The general rule "cross-agent communication routes through the board, not direct file
writes" (see `path-resolution.md` cross-agent section) has ONE sanctioned exception:
**cross-agent resolution of `agents/<agent>/session/pending-questions.yaml` entries via
direct Edit is permitted** when an out-of-agent session needs to mark a PQ as
resolved (e.g., user-delegated bookkeeping, an observer session applying a user
directive across all three agents).

Why this is safe:
- `pending-questions.yaml` is gitignored at `.gitignore:81 */session/`
- `pending-questions-sweep.py` is **read-only unless an apply flag is passed**
  (its docstring is the SoT). This line read "read-only by design" until
  2026-08-10; that was false from the moment `--apply` shipped, and the safety
  argument here rested on it. The writers are `--apply` (verdict=auto_resolve)
  and `--apply-cleanup` (verdict=needs_transition), both atomic via
  tempfile + os.replace. Neither runs without being asked, so the argument still
  holds for the READ path — but a caller passing an apply flag IS a writer and
  must be counted as one.
- The runner only writes pending-questions.yaml during rare consolidation Step 2.8;
  same-second concurrent writes between an observer and runner are extremely rare
- The file is small (kB-scale) and append-mostly; read-modify-write races are tolerable

What the editor MUST do:
- Preserve the file's existing YAML structure (bravo=flat list, alpha=nested list,
  zeta=top-level dict with `questions:` key — structures differ across agents)
- Set `status: resolved`, `resolved_at: <ISO 8601 local time>`, and a
  `resolution: <narrative>` field on the resolved entry
- Optionally set `resolution_source` to a short tag (e.g., `user_directive_delegated_call`,
  `auto_resolve_no-op_default_action`, `superseded_by:<id>`)

This exception does NOT extend to other per-agent files (working-memory.yaml,
journal.jsonl, experience.jsonl). Those use their own scripts.

## Session Boundary Protocol

**At session end (consolidation/handoff):**
1. Release all held claims in BOTH queues via `aspirations-release.sh <goal-id> --source {source}`
   (world-only was correct until g-306-238 gave agent-queue goals claims; see
   `aspirations-consolidate/SKILL.md` Step 8.9 and g-306-258)
2. Post session summary to coordination: `--type status`
3. Include `held_claims: []` in `handoff.yaml`

**At session start (boot):**
1. Run board scan protocol (see above) for cross-agent context
2. Pick up any handoff items from other agents
3. Check for stale claims from own previous session (claim expiry handles this automatically)

## Directive Protocol

Directives are the primary mechanism for one agent to influence another agent's
priority selection in real-time. The `directive` message type (already in the board
schema) carries structured intent that the goal-selector mechanically applies.

### Directive Payload

Post directives to the coordination channel with `--type directive`. The message
text is a human-readable summary. Tags carry structured metadata:

| Tag | Format | Purpose |
|-----|--------|---------|
| `directive_type` | `priority_shift\|focus_window\|veto` | What kind of influence |
| `scope` | `session\|until_completed\|permanent` | How long it lasts |
| `target:<id>` | `target:g-166-06` | Specific goal to boost/deprioritize |
| `category:<name>` | `category:infrastructure` | Category-level influence |
| `weight:<N>` | `weight:+2.0` or `weight:-1.5` | Additive score modifier |
| `expires:<ISO>` | `expires:2026-04-05T00:00:00` | Auto-expiry timestamp |

### Protocol Flow

The **Body scope** column is load-bearing — see the subsection below before
assuming a directive reached the Body you meant it for.

| # | Step | Who runs it | Body scope |
|---|---|---|---|
| 1 | **Post**: Sender posts directive to coordination channel with relevant tags | sender | any Body |
| 2 | **Scan**: Receiver's Phase 2.07 (Directive Scan) reads directives since last scan | `aspirations-select` | **REDUCER ONLY** |
| 3 | **Score**: `goal-selector.py` reads active directives, applies `directive_boost` as a weighted scoring criterion to matching goals/categories | `goal-selector.py` | **EVERY Body** |
| 4 | **Acknowledge**: Receiver posts `--type status --reply-to <directive-id>` with tag `acknowledged,<agent-name>` | `aspirations-select` | **REDUCER ONLY** |
| 5 | **Expire**: Directives auto-expire per their `expires` tag, or `scope: session` directives expire at session end | mechanical | n/a |

### Body Scope — what actually reaches a worker

A worker Body skips `/prime` and strategic-scan, so it never *reads* the board.
It does **not** follow that a directive cannot steer it: **steps 2 and 4 are
reducer-only, but step 3 runs on every Body**, because `goal-selector.py` reads
`world/board/coordination.jsonl` itself rather than being handed the board by a
prior phase, and `worker-loop` calls `goal-selector.sh` like any other Body. The
board-reading half and the board-*acting* half are different halves.

Measured 2026-08-17 (zeta, `hostname` cc-02, `uname -r` 6.8.0-137-generic) on a
live 1,228-candidate selector run: **21 candidates carried `directive_boost=1.5`**
— exactly the goals targeted by the standing directive in force at the time. The
key is present-and-`0.0` on the untargeted candidates including rank 1, so the
zeros are real absences rather than a missing field (the rb-245 check).

So the contract has three parts, and only the first is what a naive reading
expects:

1. **Additive directives DO reach every Body — through the score, not the board.**
   A `priority_shift` / `focus_window` directive carrying `target:<goal-id>` tags
   raises those goals for the reducer and every worker alike. No peek is needed
   and none should be added: a worker directive-read would duplicate scoring that
   already happens, at the cost of a board call per cycle.
2. **Exclusionary directives reach NO Body through scoring at all** — this is the
   genuine gap, and it is not the one the worker-blindness framing predicts.
   `directive_boost` is purely ADDITIVE: it can raise an in-lane goal's score but
   has no mechanism to *suppress* an out-of-lane candidate. A lane pin ("take only
   X and Y, skip everything else") therefore has no representation in the selector
   whatsoever and can only be honored by the agent at claim time. On the reducer
   that honoring exists (Phase 2.07's guard-1310 MUST-SELECT banner); a worker has
   no equivalent. Steer exclusively at your peril — prefer expressing the same
   intent additively, or encode it in the goal records. (guard-2912.)
3. **`target:` must name a GOAL id.** `target_goals` is compared against goal ids,
   so `target:asp-NNN` contributes exactly zero boost while looking like a
   correctly-formed directive. Enumerate the goals and tag them individually.

The LLM-side work that is genuinely reducer-only is Phase 2.07's judgment half:
acknowledgment replies (step 4), insight-trigger processing, and the guard-1310
honor banner. A directive is therefore *scored* fleet-wide but *answered* by one
Body — which is the correct division, since N Bodies acking one directive would be
N times the board noise for no added signal.

**This corrects the premise of g-306-222**, which filed the defect as "structurally
invisible to every worker Body, which ranks purely by goal-selector score" and
prescribed steering via priority fields instead. The scorer reads `directive_boost`
as well as `priority`, so board steering does work on workers — for additive
directives. Documenting it as "board-directives-are-reducer-only", the resolution
that goal proposed, would have shipped a contract that is false in the common case
and silent on the real gap (2). Note also that steering which must produce *work*
still has to reach a queue: a board post alone is advisory context and the selector
only sees queues (guard-732).

### Rules

- Directives are **non-blocking** — the agent acknowledges and factors in, never waits
- `veto` directives carry a strong negative weight (-5.0) — effectively deprioritizes a goal
- Multiple active directives stack (additive scoring)
- Directive influence is metered by `directive_boost` (+3.0); the agent MUST NOT override a
  directive-boosted pick citing metacognitive assessment (Phase 2.5) alone — the guard-1310
  DIRECTIVE-HONOR hard rule governs. A directive-targeted pick is deferred only via a
  justified-deferral ack naming a hard blocker (infra-blocked / genuine capability gap),
  never a silent lane/focus skip

## Cross-Lane Evidence-Clear Coordination (FW-8)

Coordination depth is 1: an agent that finds an evidence-clear issue in another
agent's lane can post a finding or directive, but the work then waits for the
owner to organically reach it. When the owner is dormant (between sessions, deep
in a long goal), an evidence-clear one-line fix can sit for hours or days. This
section adds two latency-reducing primitives along a spectrum of fix-clarity:

| Fix clarity | Mechanism | Who acts |
|---|---|---|
| Trivial + unambiguous (one logical line, correct value evidence-confirmed) | **Discoverer-Applies-Trivial-Fix** (below) | discoverer (with mandatory notification) |
| Clear but not trivial (owner's judgment needed, but evidence shows it matters NOW) | **Priority-Promotion-With-Evidence** (below) | owner (surfaces elevated next iteration) |
| Needs design / multi-line / behavior change | Normal goal / finding / handoff (existing primitives) | owner |

The discriminator is honest fix-clarity, not convenience. When in doubt between
trivial and clear-but-not-trivial, promote (do not apply); between clear and
needs-design, file a normal goal. Escalating clarity earns lighter coordination;
overclaiming clarity to skip coordination is the anti-pattern this section guards.

### Priority-Promotion-With-Evidence

Board type: `priority-promotion` (coordination channel). An evidence-carrying
elevation request: the discoverer found concrete evidence that work in the
owner's lane deserves elevation NOW, and asks the OWNER to surface it elevated on
the owner's next iteration.

Distinct from its neighbors:
- vs `directive` (priority_shift) -- a directive is a weight nudge the sender
  applies to the receiver's scoring; a priority-promotion carries the EVIDENCE
  that justifies the elevation, and is pull-based (owner verifies, then acts).
- vs `finding` + `insight_trigger` (severity:invalidates|constrains|enables|informs)
  -- an insight_trigger says "my finding affects your EXISTING goal"; a
  priority-promotion says "here is evidence that work in your lane deserves
  elevation NOW" and may name new work the owner should file.
- vs `handoff` -- a handoff follows a COMPLETED goal needing follow-up; a
  priority-promotion concerns a DISCOVERED issue with no completed predecessor.

Payload (tags carry structured metadata, same convention as the Directive Protocol):

| Tag | Format | Purpose |
|-----|--------|---------|
| `evidence:<locator>` | `evidence:core/scripts/foo.py:42` | **REQUIRED** -- concrete evidence (file:line, failing-probe id, measured regression). Its presence is what distinguishes a promotion from a bare directive. |
| `owner:<agent>` | `owner:alpha` | the lane owner who should surface it elevated |
| `promotes:<id>` | `promotes:g-317-09` | the existing goal to elevate; omit when the work is not yet a goal (then the body describes it and the owner files it) |
| `weight:<N>` | `weight:+1.5` | additive scoring boost -- same semantics and units as the Directive Protocol `weight` tag |
| `expires:<ISO>` | `expires:2026-06-25T00:00:00` | auto-expiry |

Protocol flow:
1. **Post**: discoverer posts `priority-promotion` to coordination with
   `evidence:` + `owner:` + (`promotes:<id>` OR a body describing new work).
2. **Scan**: the owner's Phase 2.07 (Directive & Insight Trigger Scan) reads
   priority-promotions whose `owner:` matches it.
3. **Verify + surface**: the owner reads the evidence (it is a locator, so it is
   checkable). If confirmed -- when `promotes:<id>` is set, the elevation rides
   the existing `directive_boost` scoring path (see wiring below); when no goal
   exists, the owner files one from the evidence. If the evidence does NOT hold
   on verification, the owner declines and replies with the disconfirming probe.
4. **Acknowledge**: owner posts `--type status --reply-to <promotion-id>` tagged
   `acknowledged,<agent>`.
5. **Expire**: per the `expires` tag (same lifecycle as directives).

Rules:
- **Evidence is mandatory.** A priority-promotion without an `evidence:` tag is a
  bare directive -- use `--type directive` instead. The evidence requirement is
  what makes the mechanism pull-based and trustworthy: the owner verifies before
  elevating, rather than boosting on the sender's say-so.
- **Owner decides.** Like directives, a priority-promotion is advisory -- the
  owner's metacognitive assessment (Phase 2.5) still applies. The owner may
  decline with a disconfirming probe.
- **Non-blocking.** The discoverer posts and moves on; it never waits.

Wiring (implementation follow-up): `goal-selector.py`'s `directive_boost`
criterion currently reads only `type=directive` posts. To make `promotes:<id>` +
`weight:<N>` elevate the named goal, extend the directive reader's type filter to
ALSO match `type=priority-promotion`. This reuses the existing scoring path (no
new selector criterion -- avoids the rb-335 writer-without-reader trap). The
Phase 2.07 consumer handling (acknowledge + file-from-evidence) is a SKILL.md
edit to `aspirations-select`. Both are filed as wiring follow-ups; until wired, a
priority-promotion is honored by the owner manually reading the post in Phase
2.07 (the board type works the instant it is posted -- board.py does not enforce
the `--type` enum).

### Discoverer-Applies-Trivial-Fix

When an agent discovers a one-line, evidence-confirmed fix in another agent's
lane, waiting for a dormant owner is wasteful. The discoverer MAY apply the fix
directly -- WITH mandatory cross-agent notification -- when ALL of the following
hold:

1. **Trivial**: one logical line; no design judgment; the change does not alter
   behavior beyond the obvious correction (typo, wrong constant, wrong path
   segment, off-by-one, missing flag).
2. **Unambiguous evidence**: a probe, test, grep, or schema read confirms BOTH
   the defect AND the corrected value -- not a hypothesis. The same evidence bar
   as any negative/positive conclusion (`verify-before-assuming.md`).
3. **Not a restricted file**: the target is NOT one of the script-gated shared
   JSONL files (see [Restricted Files](#restricted-files-concurrent-modification-prevention))
   and NOT inside another agent's private dir (`agents/<other>/...` routes
   through the board, never direct edit -- see `path-resolution.md`). The
   exception applies to shared, git-tracked framework files (`core/`, `.claude/`,
   `core/config/`, `CLAUDE.md`) and product-repo code, where direct Edit is the
   normal authoring path and git is the safety net.

Commit + notify discipline (MANDATORY):
- Commit with an EXPLICIT pathspec naming ONLY the fixed file (guard-741 -- never
  a bare `git commit`; guard-1120 -- never `git add -A`). This keeps the
  discoverer's one-line change from sweeping up the owner's uncommitted in-flight
  work (guard-739/797/834).
- Small, descriptive, goal-tagged commit message; co-author trailers as usual.
- Notify the owner: post `--type handoff` to coordination naming the file, the
  one-line change, the confirming evidence, and the commit hash, tagged
  `affects:<path>,<owner>`. The owner reviews async (Review Gate) and reverts if
  they disagree -- reversibility via git is what makes the direct apply safe.

Boundary (respect guard-732 -- still file a goal for non-trivial work): if the
fix needs design judgment, spans multiple lines, or changes behavior beyond the
obvious correction, the discoverer does NOT apply it. They file a normal goal, or
post a Priority-Promotion-With-Evidence (above) if it is evidence-clear and
should be elevated. Overclaiming triviality to skip the owner's judgment is the
anti-pattern this rule guards against -- when uncertain, promote, do not apply.

## Inbox Alert Flow (Generic Contract)

Inbound alert events (production failures, deploy failures, monitoring noise,
test alerts) route into the agent queue via a single ingest path: a recurring
sweep claims new alert messages, classifies them, and files them as cognitive
primitive goals (Unblock/Investigate) under a tracking aspiration so the
regular selection/claim mechanism picks them up.

### Contract

A domain deployment provides two scripts (typically in `world/scripts/`):

- A **reader script** — reads the alert source (email inbox, webhook log,
  message queue, etc.), enumerates unseen messages, returns each with its
  dedup key, classifier subject, and parsed body.
- A **sweep script** — calls the reader, classifies each unseen message,
  files matching Unblock / Investigate goals under a tracking aspiration,
  and appends a dedup ledger entry. Run from a recurring goal (typical
  interval: 1h) so every agent that picks the recurring sees fresh alerts.

### Classifier (recommended)

The sweep classifies each message into one of three outputs:

| Subject pattern | Goal type | Priority | Notes |
|---|---|---|---|
| production/deploy failure | `Unblock:` | HIGH | Filed for immediate selection; surfaces in next iteration's algorithmic scoring. |
| other failure / monitoring noise | `Investigate:` | MEDIUM | Filed for diagnostic follow-through. |
| success / synthetic test message | log-only | — | No goal filed; ledger entry only. Prevents the success/test stream from flooding the tracking aspiration. |

### Schema Markers

- **origin_signal**: every filed goal carries
  `origin_signal: "alert-email:<dedup-key>"` where `<dedup-key>` is the
  message ID, S3 key, or other stable identifier. Selector criteria
  (`directive_boost`, `user_signal_boost`) and pre-selection sweeps
  (`inbox-alert-age-check.py`) match on the `alert-email:` prefix to
  distinguish inbox-originated goals from organically-filed work.
- **dedup ledger**: every processed message (filed AND log-only) appends
  to `<WORLD_PATH>/audit-reports/alert-sweep-seen.jsonl`. The sweep
  reads this ledger before classifying to skip already-handled keys —
  re-running the sweep is idempotent.

### Pull Mechanics

Once filed, an inbox-originated Unblock/Investigate goal is indistinguishable
from any other goal under the tracking aspiration: standard goal-selector
scoring ranks it, standard `aspirations-claim.sh` (see
[Claim Protocol](#claim-protocol)) takes it for execution. No special claim
path exists for alert goals.

### Active-Push Gap

The sweep is a **pull** flow — it only runs when an agent is running the
loop and the selector ranks the recurring sweep goal above all other
candidates. When all agents are idle (or every running agent is busy with
longer-running deep work), a freshly-filed HIGH-severity Unblock from the
sweep can sit in the queue with no one selecting it. The async escalation
hook `Phase 0.5b.1b: Inbox-Alert Age Escalation` (see `aspirations-precheck`)
covers this gap: it scans the tracking aspiration every iteration for
`origin_signal: alert-email:*` goals past the age thresholds
(`proactive_escalation.inbox_alert_age_hours`, default HIGH=4h / MEDIUM=12h)
and fires a notification when an alert ages without being claimed. This
pushes urgency upward rather than waiting for the loop to organically reach
the goal.

## Team State Protocol

Both agents maintain a shared situational awareness document at `world/team-state.yaml`.
This provides instant context about what the other agent is doing, what's strategically
important, and what's blocked — without scanning hundreds of board messages.

### Sharded Storage Layout (g-328-27)

The LOGICAL schema below is unchanged, but the STORAGE is sharded: each
agent's `agent_status.<name>` row physically lives in its own file
`world/team-state/agents/<name>.yaml`, written ONLY by that agent's
sessions. Shared fields (`strategic_focus`, `active_blockers`,
`recent_completions`, `critical_blockers`, `inbox_alert_backlog`,
`shared_cadences`, ...) stay in `world/team-state.yaml` (the "core" file).
Rationale: team-state was the hottest cross-writer file in the fleet —
every agent bumped its own `last_active`/`in_flight` every iteration, so N
agents contended on one lock (local) / one CAS-fenced S3 object
(own-cloud). With one row file per agent, no two writers ever touch the
same object; contention disappears by construction.

Mechanics (single source of truth: `core/scripts/_team_state.py`, imported
by BOTH the CLI `team-state.py` and the daemon `team_state{,_write}.py` —
guard-742 parity by construction):

- **Write routing**: `update --field agent_status.<name>[...]`,
  `in-flight`, and `clear-in-flight` land in that agent's row file (row
  writes also stamp `row_updated`/`row_updated_by`). All other fields
  route to the core file exactly as before.
- **Composed reads**: `team-state-read.sh` / `GET /v1/team-state/read` /
  `read_state()` overlay row files onto the core document — per-agent
  WHOLE-ROW newest-wins (same side-pick semantics as
  `coordination_merge._merge_agent_status`; rows win ties), and
  `last_updated`/`last_updated_by` lift to the newest stamp across core +
  rows. Consumers see the exact pre-shard schema.
- **Raw readers must compose**: any code reading `agent_status` straight
  off `team-state.yaml` sees only stale residuals. Use
  `_team_state.read_agent_row(world, agent, core_path=...)` for one agent
  (in-flight inference) or `_team_state.compose_state / compose_agent_status`
  for the full map; roster = core keys ∪ row-file stems
  (`row_agent_names`, or `_agents.get_active_agents()` which already
  unions both).
- **Lazy migration**: the first row write for an agent self-seeds from the
  core-file residual, so no coordinated migration is required.
  `team-state.py migrate-shard` is an optional one-shot cleanup that moves
  residuals into rows and empties the core `agent_status` map.
- **Own-cloud**: row files are ordinary governed world files (the sweep
  walks the tree). They are single-writer by construction, so
  both-diverged conflicts should not occur; they are intentionally NOT
  merge-registered (safe-freeze is the correct conservative behavior for
  a violated single-writer invariant).

### Schema

```yaml
last_updated: "ISO 8601 timestamp"
last_updated_by: "agent-name"

strategic_focus:
  primary: "Short description of current strategic priority"
  rationale: "Why this is the focus"
  set_by: "agent-name"
  set_at: "ISO 8601 timestamp"
  acknowledged_by: ["agent-names who have read this"]

active_blockers:
  - id: "blocker-identifier"
    description: "What is blocked and why"
    affects: ["goal-ids or patterns"]
    reported_by: "agent-name"
    reported_at: "ISO 8601 timestamp"

recent_completions:  # ring buffer, last 50 (raised from 10 in asp-248 to deepen goal-duplication-gate signal)
  - goal_id: "g-NNN-NN"
    title: "Goal title"
    completed_by: "agent-name"
    completed_at: "ISO 8601 timestamp"
    key_finding: "One-line factual summary of what was produced/discovered"

agent_status:
  <agent-name>:
    last_active: "ISO 8601 timestamp"
    current_focus: "Lane the agent is working in — auto-stamped at claim by team-state-in-flight.sh as '<aspiration>: <title>' (prospective; persists across clear-in-flight as the last-claimed lane). Read by the ToM belief-contradiction check. (g-115-1575)"
    live_phase: "phase-4-execute g-115-157"  # set by heartbeat-tick from diary tail
      # Informational — NOT a liveness signal. Partners use last_active for
      # liveness; a stale live_phase just means heartbeat-tick stopped ticking.
      # Freshness: ≤60s during B7 waits, ≤iteration-length otherwise.
      # Values: "<phase> [goal_id]"  — last diary entry was phase_start
      #         | "between-phases"    — last entry was phase_end
      #         | "finding [goal_id]" — last entry was a finding
      #         | "no-diary"          — diary file missing or empty
      # Source: tail of agents/<agent>/session/execution-diary.jsonl
      # Writer: core/scripts/live-phase-emit.sh (called by heartbeat-tick.sh)
    session_goals_completed: N
    in_flight:                  # prospective — set at claim, cleared at completion
      goal_id: "g-NNN-NN"
      title: "Short goal title"
      claimed_at: "ISO 8601 timestamp"
      phase: "4"                # current aspirations-loop phase
    last_fresh_eyes_run:        # cross-agent fresh-eyes coverage window (g-115-291)
      files: ["path/relative/to/repo", ...]   # unique reviewed paths, capped at 20
      time: "ISO 8601 timestamp"               # local time at end-of-review
      count: N                                  # len(files), redundant for grep-friendliness
      content_signatures:                       # g-115-573 amend-detection
        "path/relative/to/repo": "<sha1[:12]>"  # content hash at review time
        ...                                     # missing path = no sig (path-only fallback)
    beliefs:                    # g-306-18 -- Theory-of-Mind: what THIS agent
                                # BELIEVES about a partner, derived from
                                # OBSERVATION (not the partner's self.md /
                                # aspirations, which are claims-of-intent, not
                                # ground truth). Supersede-or-cap list (g-306-28):
                                # one entry per partner (the current belief),
                                # hard cap 10. Writer: team-belief-write.sh.
      - about: "agent-name"     # the partner this belief concerns
        belief: "one-line observed claim about the partner's focus/behavior/state"
        confidence: 0.0         # 0.0-1.0, calibrated: single observation -> ~0.5,
                                # repeated-consistent -> higher, contradicted -> lower
        last_observed: "ISO 8601 timestamp"  # when the supporting observation occurred

critical_blockers:  # updated by consolidation, read by boot
  - goal_id: "g-NNN-NN"
    title: "Goal title"
    cause: "Why it's blocked"
    downstream_count: N
    updated_by: "agent-name"
    updated_at: "ISO 8601 timestamp"

inbox_alert_backlog:  # g-115-849 — null when zero matching goals, else the map below
  count: N                          # pending, un-claimed "Unblock:" inbox goals
  oldest_age_hours: N.N             # age of the oldest such goal (hours)
  oldest_goal_id: "g-NNN-NN"        # id of the oldest such goal
  updated_at: "ISO 8601 timestamp"  # when this counter was last recomputed
```

### beliefs Field -- Theory-of-Mind Partner Belief Tracking (g-306-18)

`agent_status.<agent>.beliefs` holds what an agent BELIEVES about a partner,
derived from OBSERVATION -- distinct from the partner's `self.md` / aspirations,
which state intent, not ground truth. Motivated by BRD Gap 9 (OpenToM,
2402.06044): an agent that treats a partner's stated role as fact misreads a
partner who has drifted or is on a cross-domain stretch.

- **Value**: supersede-or-cap list of `{about, belief, confidence, last_observed}`
  (g-306-28); exactly one entry per partner is the current belief, hard cap 10.
  `confidence` is calibrated (single observation ~= 0.5; repeated-consistent ->
  higher; contradicted -> lower).
- **Storage**: canonical writer is `team-belief-write.sh --about <partner>
  --belief "<text>" [--confidence <0..1>]` (g-306-28). It reads the current
  sublist via the daemon, runs the pure supersede/cap compute in
  `_team_belief.py` (one entry per `about`, hard cap `MAX_BELIEFS=10`; unit-
  tested in `core/scripts/tests/test_team_belief_write.py`), and writes the whole
  list back via `team-state-update.sh ... --operation set` -- race-free at the
  field level because each agent is the SOLE writer of its own `beliefs` sublist
  (verified end-to-end against the live daemon, supersede held at list length 1,
  2026-06-18). The low-level `--operation append` still works for ad-hoc use but
  bypasses supersede -- prefer the wrapper. Read:
  `team-state-read.sh --field agent_status.<partner>.beliefs --json` (returns
  literal `null` for a partner with no beliefs yet -- handle gracefully).
- **Rule (consumer discipline)**: a consumer MUST treat beliefs as
  confidence/staleness-weighted hypotheses, NOT ground truth. When a partner's
  observed action contradicts a held belief, that is a signal to REFLECT and
  revise the belief (lower confidence / supersede) -- not to assume the belief
  was right.
- **Hygiene**: supersede the prior belief about a given partner rather than
  growing the list unbounded. ENFORCED in code (g-306-28): `_team_belief.py`
  drops every prior entry whose `about` matches before appending, then caps at
  `MAX_BELIEFS=10`. No longer a writer-discipline honor rule.
- **Write+consume loop (g-306-28, LANDED 2026-06-18)**: the WRITER runs at
  `fresh-eyes-review` Phase 2.6c -- once per 25-goal review (a real decision
  point, not every tick), it records ONE calibrated belief (~0.5) about the most
  salient partner observed, via `team-belief-write.sh`. The CONSUMER runs at
  `fresh-eyes-review` Phase 2.6b -- it reads every partner's `beliefs` sublist,
  filters to beliefs `about` THIS agent, and surfaces them as confidence- AND
  staleness-weighted self-evolution signals (NOT ground truth), feeding the
  Phase 5.5 `self_evolution_signals_count`. Readers must still handle an
  empty/`null`/short list gracefully.
- **Contradiction -> forced-reflection trigger (g-306-29, LANDED 2026-06-18)**:
  outcome 3 of the loop is built. `aspirations-precheck` Phase 0-pre.0a runs
  `belief-contradiction-check.sh` (thin daemon orchestrator) +
  `_belief_contradiction.py` (pure, unit-tested) once per iteration, right after
  the Phase 0-pre.0 partner snapshot. It compares every partner's freshly OBSERVED
  focus (`agent_status.<partner>.current_focus`) against the domain-belief THIS
  agent holds about that partner. Beliefs carry an optional `domain` field (set by
  the Phase 2.6c writer's `--domain` flag); only domain-tagged beliefs held at
  confidence >= threshold (default 0.5) are contradiction-checkable -- free-form
  beliefs skip, the conservative source of the no-false-trigger guarantee for
  un-held beliefs. On N CONSECUTIVE contradicting observations of the SAME observed
  domain (default N=2, persisted as per-partner streaks in agent-private WM
  `belief_contradiction_streaks`), a forced reflection REVISES the held belief:
  `mode=lower` (default) multiplies its confidence by 0.5 (keeps the held domain,
  just less sure), or `mode=supersede` flips the domain to the observed reality at
  a calibrated 0.5. Both stamp `prior_domain`/`prior_confidence`/`revised_at` onto
  the belief (that annotation IS the recorded surprise) and append an
  `evolution-log` entry. The N-consecutive gate is what guarantees NO false-trigger
  on a FIRST or one-off divergence (count 1 < N): a `match` or `skip`, or an
  observed-domain CHANGE, resets the streak, so only a SUSTAINED contradiction on
  the same observed domain accumulates to the threshold. Fail-open: the check runs
  in the precheck hot path and never blocks the loop -- a detector error degrades
  to "no revision this iteration", never a stalled precheck. The manual
  lower/supersede that the consumer-discipline rule above describes is now
  automated.

### inbox_alert_backlog Field — Inbox Backlog Counter (g-115-849)

`inbox_alert_backlog` surfaces how many inbox-derived action goals are piling up
unhandled. It is the team-state companion to the [Inbox Alert Flow](#inbox-alert-flow-generic-contract):
the sweep files `Unblock:` / `Investigate:` goals tagged `origin_signal: alert-email:<key>`,
and this counter reports the subset still waiting.

- **Value**: `null` when zero matching goals; otherwise the map shown in the
  schema (`count`, `oldest_age_hours`, `oldest_goal_id`, `updated_at`).
- **Match criteria**: a goal counts when its `origin_signal` starts with the
  configured prefix (`alert-email:`), its `status` is `pending` or
  `in-progress`, it is **not** claimed (`claimed_by` absent), and its title
  starts with `Unblock:`. Actively-claimed goals are excluded — they are being
  handled, not backlogged.
- **Writer**: `core/scripts/inbox-backlog-update.py` (framework, domain-free —
  the aspiration id, origin prefix, and field name are arguments). It writes via
  team-state.py's `update` CLI, so the write is atomic under the same lock as
  every other team-state mutation. The domain sweep (`world/scripts/alert-sweep.sh`)
  invokes it fail-open after each batch and on empty-inbox ticks, so the counter
  drains as goals complete during quiet periods.
- **Consumer**: `aspirations-precheck` Phase 0-pre.0 reads the field from the
  per-iteration team-state snapshot and surfaces
  `inbox-alert-backlog={count} oldest={age_hours}h goal={goal_id}` when
  `count > 0`; silent when `null`. This complements the
  [`Phase 0.5b.1b` age-escalation](#active-push-gap) — that one pushes a
  notification when a single alert ages past threshold; this one shows the
  aggregate queue depth in the iteration header.

### Script API

```bash
# Read full state
bash core/scripts/team-state-read.sh [--json]

# Read a specific field (dot-notation)
bash core/scripts/team-state-read.sh --field strategic_focus.primary

# Set a field
bash core/scripts/team-state-update.sh --field strategic_focus.primary --value '"2-Day Demo"'

# Append to a list (ring buffer enforced for recent_completions)
bash core/scripts/team-state-update.sh --field recent_completions --operation append --value '{"goal_id":"g-165-03","title":"Social framework","completed_by":"bravo","completed_at":"2026-04-03T22:07:40","key_finding":"shared channel IS the framework"}'

# Remove a blocker by id
bash core/scripts/team-state-update.sh --field active_blockers --operation remove --value '"blocker-processor-gpu"'

# Initialize (idempotent — skips if exists)
bash core/scripts/team-state-init.sh

# Mark agent in-flight on a goal (auto-stamps claimed_at)
bash core/scripts/team-state-in-flight.sh --agent alpha --goal-id g-001-99 --title "short title" --phase 4

# Clear the in_flight block when goal completes or releases.
# --if-goal is an optional compare-and-swap: the row is cleared ONLY if its live
# goal_id matches, re-checked inside the row lock. Prefer it whenever you are
# clearing a row YOU set — in_flight is keyed by agent name with no sid, so the
# unscoped form blanks whatever is present and can destroy a concurrent
# sibling's live claim (guard-2474). Omit it only when you genuinely mean
# "normalize whatever row is there" (retire, release, graceful stop).
bash core/scripts/team-state-clear-in-flight.sh --agent alpha --if-goal g-001-99
bash core/scripts/team-state-clear-in-flight.sh --agent alpha              # unconditional
```

### in_flight Field — Live Claim Snapshot

`agent_status.<agent>.in_flight` is the LIVE snapshot of what an agent is currently
working on. Written at goal-claim time (before primary execution), cleared at goal
completion or release. Other consumers — prime, precheck, select, all-blocked, the
pre-silence guardrail (`.claude/rules/check-team-state-before-silent.md`) — read this
field to coordinate without polling the board.

**Single source of truth.** The board `claim`/`complete`/`release` posts remain the
audit trail (immutable, queryable per-event). team-state.in_flight is the live
snapshot (mutable, lookup-by-agent). Both write at the same instant; do not rely on
one to derive the other.

**TWO SURFACES. READING ONLY `in_flight` IS A PERMANENTLY-OPEN GATE, NOT A PARTIAL
ONE** (g-306-223, documenting the shard g-306-132-d shipped). A Mind can run several
Bodies, and the two surfaces are written by *disjoint* sets of them:

| surface | written by | writer |
|---|---|---|
| `agent_status.<agent>.in_flight` | the REDUCER Body only — this box's `session/running-session-id` exists AND equals this session's `MIND_SID` | `team-state-in-flight.sh` |
| `agent_status.<agent>.in_flight_bodies.<MIND_SID>` | every OTHER Body (cross-box worker: rsid file absent; same-box worker: rsid mismatch) | same script, SKIP-then-body-row branch |

The branch is exclusive: a non-reducer Body **never** touches `in_flight` (that
guarantee is what stops a worker blanking its reducer's live row), and a reducer
never writes a body row. **No SID means no row at all** — an unkeyed body row could
not be cleared by its owner.

So a consumer reading only `in_flight` does not see "the last claimer"; on a
worker-executed goal it sees **nothing**. Measured 2026-08-16 (zeta, `hostname`
cc-02, `uname -r` 6.8.0-137-generic, own-cloud, live 5-agent fleet, two reads 22
min apart): `in_flight` **null for all five agents** while `in_flight_bodies`
carried **7 live claims** (alpha 5, bravo 2). Same shape measured 2026-08-10 on
four agents (guard-997's corrected mechanism clause). Workers now execute most
units, so this is the ordinary state, not an edge case.

**Do not read the older framing that says two Bodies "overwrite each other" so the
snapshot shows the last claimer.** That described the pre-shard store. The failure
mode inverted rather than shrinking: a blind reader used to undercount N live
claims as 1, and now undercounts them as **0** — which is worse, because zero is
indistinguishable from "partner genuinely idle" and therefore fails silently
(guard-997).

**Read obligation — read BOTH, and union them.** Canonical form:

```bash
team-state-read.sh --field agent_status.<partner>.in_flight.goal_id --json
team-state-read.sh --field agent_status.<partner>.in_flight_bodies --json   # whole map
```

Staleness is the REAPER's job, not each reader's: `body_row_reaper.py` (g-306-191,
wired via `stranded-claim-sweep`) deletes rows whose carrier has been stale for
`DEFAULT_REAP_STALE_MINUTES` (180). Do NOT add a second freshness heuristic in a
consumer — it would drift from the reaper's and put two policies on one store. The
bound to know: a dead Body can withhold a goal for at most ~3h, which is the safe
direction versus a duplicate claim.

**Reader inventory, measured 2026-08-16.** Bodies-aware: the loop digest Phase-4
claim-conflict hard gate, `aspirations-select` Phase 2 partner-claim filter
(g-306-276), `goal-pickup-coordination-check.py` (g-306-160),
`stranded-claim-sweep.py`, `_cross_agent_attribution_filter.py`, and — since
g-306-301, later the same day — `gates/goal_duplication.py`
(`_check_partner_in_flight`). Bodies-BLIND at that date — each reads
`in_flight` and never `in_flight_bodies`: `iteration-commit.sh`,
`.claude/skills/aspirations-graceful-stop/SKILL.md`, `tree.py`. Blindness is not
automatically a defect (a consumer that only ever asks about the reducer's own row
is correct as written) — but a consumer that asks "is any Body of this Mind on this
goal?" and reads one field is wrong, silently, in the permissive direction. Check
which question yours asks before adding a reader.

**Write contract.**

| When | Caller | Action |
|------|--------|--------|
| Phase 4 claim, before board post | `aspirations-claim.sh` — **AUTOMATIC** (`_post_claim_effects`, g-115-3199) | `team-state-in-flight.sh --agent <self> --goal-id ... --title ... --phase 4`. Fires on every rc=0 claim, fail-open, deliberately ungated on `claimed_by`. This row named `aspirations-execute` (LLM pseudocode) until 2026-07-26. Verified that day: the setter had ZERO callers in the codebase, so every write depended on an LLM remembering the step, and execution was UNEVEN rather than uniformly absent — zeta stamped correctly while foxtrot never did (its `current_focus` sat frozen 2h+ on an already-yielded goal across six claims). Uneven is worse than absent for the three readers: a null `in_flight` is indistinguishable from "partner genuinely idle", so the select partner-claim filter, the `goal-pickup-coordination-check` uncommitted-collision probe, and `_cross_agent_attribution_filter`'s Source 1 silently mis-answer instead of failing loud. Covers `source==world` ONLY — agent-queue goals never invoke `aspirations-claim.sh`, so the loop digest's Phase-4 LLM-side `team-state-in-flight.sh` call remains their only stamp path and must NOT be deleted. On the world path that LLM call is now redundant-but-idempotent (same values re-written), not harmful. |
| Phase 4 → 5 transition (optional) | `aspirations-execute` / `aspirations-verify` | re-run in-flight with `--phase 5` to surface progress |
| Goal completion | `iteration-close.sh` do_verify Step 3 | `team-state-clear-in-flight.sh --agent <self> --if-goal "$GOAL_ID"` — SCOPED since g-306-161. `$GOAL_ID` cannot be empty here (do_verify refuses without `--goal`). The scoping does NOT weaken the idempotence blocked/skipped closes rely on: an already-released claim leaves no `in_flight` key, and the modifier returns on key-absence *before* the comparison. Scoped and unscoped differ only when the row holds a DIFFERENT goal — the case being fixed. |
| Crash forward-recovery | `iteration-close.sh` do_recover Case B | `team-state-clear-in-flight.sh --agent <self> --if-goal "$_gid"` — scoped to the CHECKPOINT's goal, **not** `$GOAL_ID`. `do_recover` never reads `$GOAL_ID` and `--phase recover` is invoked without `--goal`, so scoping it to `$GOAL_ID` would compare against an empty string, which the endpoint coerces back to `None` — an unconditional clear wearing a CAS flag. |
| Goal release (failure / re-select) | `aspirations-execute` release path | `team-state-clear-in-flight.sh --agent <self>` — deliberately UNSCOPED: release normalizes whatever row is present, including a malformed one. |
| Claim-conflict abort (partner already in_flight on same goal) | `aspirations-execute` Phase 4 pre-claim | abort + return to select; do NOT write in_flight |
| Phase 4 claim by a NON-reducer Body | `team-state-in-flight.sh` SKIP branch — **AUTOMATIC**, same call site | Writes `agent_status.<agent>.in_flight_bodies.<MIND_SID>` and leaves `in_flight` untouched. Fail-open: a failed body-row write logs a WARN and does NOT fail the claim (visibility is not correctness). With no `MIND_SID` it writes NO row and says so on stderr. |
| Body turn-end, clean | `worker_close_in_flight_clear.clear_body_row` | Pops `in_flight_bodies.<sid>`, sweeps null siblings, and removes the whole `in_flight_bodies` key once it empties (so no `{}` is left behind). |
| Body death, unclean (crash / kill / text-death under API storm) | `body_row_reaper.py` (g-306-191), wired via `stranded-claim-sweep` | Reaps rows whose carrier has been stale ≥ `DEFAULT_REAP_STALE_MINUTES` (180). This is the ONLY reclaimer for the unclean path — `clear_body_row` runs only on a clean turn-end. Measured at filing: 4 live phantom rows fleet-wide, oldest 66.3h. |

⚠ **Clearing: use the dedicated clearer, and know it is TWO surfaces.**
`team-state-update.sh --field in_flight --value null` does NOT clear the row — a bare
leaf name is a valid 1-segment path, so `_set_nested` creates a NEW TOP-LEVEL
`in_flight` key and the wrapper prints a success line for a write that never touched
the row you meant (guard-3780, measured). Pass the full dotted path
`agent_status.<agent>.<field>`, prefer `team-state-clear-in-flight.sh --agent <a>
--if-goal <goal-id>` for the reducer row, and note a single-field clear leaves the
SID-keyed body row STRANDED (g-306-160, measured 30h stale). `aspirations-release.sh
::_clear_in_flight` is the reference implementation that handles both. Verify by
reading back the nested path, never by the success line.

### `last_fresh_eyes_run` Field — Cross-Agent Coverage Window

`agent_status.<agent>.last_fresh_eyes_run` is the cross-agent coverage record
of the most recent `/fresh-eyes-code` invocation by that agent. Written at
end-of-review (Phase 5b), read by every agent's
`post-state-update-gate.sh` cooldown block to suppress re-dispatch when a
peer just covered the same files. Without this field the gate would only
see the running agent's own `fresh_eyes_last_fire` (per-agent WM) and
re-dispatch peer-reviewed files within the cooldown window — the failure
mode g-115-288 / rb-593 surfaced (alpha's review covered 6 of bravo's
33 files; the other 27 were genuinely novel only because the gate had
no visibility into alpha's coverage).

**Single source of truth.** Per-agent WM `fresh_eyes_last_fire` remains the
own-agent self-cooldown record; team-state `last_fresh_eyes_run` is the
cross-agent peer-coverage record. Both write at the end of /fresh-eyes-code;
the gate reads both and unions their `files` lists within `COOLDOWN_HOURS`
(`post_state_update_gate.cooldown_hours` in `core/config/aspirations.yaml`,
default 4h). Staleness is per-source — an agent's record expires when its
own `time` is older than the cooldown window, not when team-state itself
becomes stale.

**Write contract.**

| When | Caller | Action |
|------|--------|--------|
| End of `/fresh-eyes-code` Phase 5 (after all board-posts) | `/fresh-eyes-code` Phase 5b | `team-state-update.sh --field "agent_status.${MIND_AGENT}.last_fresh_eyes_run" --value '{"files":[...],"time":"...","count":N,"content_signatures":{<path>:<sha1[:12]>,...}}' --operation set` (g-115-573 — `content_signatures` populated by `core/scripts/_fresh_eyes_signatures.py`) |
| Empty target_files (Phase 1 fall-through, no review actually ran) | `/fresh-eyes-code` Phase 5b | SKIP — no record. The gate's existing peer set stays untouched (still ages out via cooldown). |

**Read contract** (post-state-update-gate.sh cooldown block):

```python
for agent_name, agent_data in (ts_data.get("agent_status") or {}).items():
    if agent_name == self_agent: continue       # peer-coverage requires non-self
    last_run = (agent_data or {}).get("last_fresh_eyes_run")
    if not isinstance(last_run, dict): continue
    if (now - parse(last_run["time"])) > timedelta(hours=COOLDOWN_HOURS): continue
    peer_records.append({
        "files_set": set(last_run.get("files", [])),
        "sigs": last_run.get("content_signatures") if isinstance(last_run.get("content_signatures"), dict) else None,
        "source": f"peer:{agent_name}",
    })
# g-115-573 per-path coverage check — suppress only when every current path is
# covered (sig-match wins; sig-mismatch ignores path-only fallback; pre-573
# records without content_signatures fall through to path-only by design).
```

**Fail-open everywhere.** A missing/corrupt YAML, missing field, missing
sub-keys, malformed timestamp, or peer agent with no record at all — every
path falls through to `peer_files = set()` and the gate proceeds with
own-agent cooldown only. The cross-agent layer is additive; it can never
prevent dispatch that would have fired without it.

### Proxy-With-Proof — supplying a starved peer's attestation (g-115-6592)

**The rule: a verifiable fact must never be a scheduling problem.** When a fleet
gate waits on an agent-local attestation that any peer can VERIFY from shared
evidence — git ancestry, byte-identity to `origin/main`, an object HEAD in the
store of record — a peer MAY supply that field after 24h of starvation. The
holdout is not withholding anything; it is simply busy, and the fact was true
the whole time.

This is the FALLBACK, not the first answer. Where a gate can check the evidence
itself, build that instead (g-115-6578 A, attest-by-proof) — this section covers
the window while A is unbuilt and the gates A does not reach. Motivating trace:
`rb-8202`, a ~5-second one-command chore that starved 3 days at HIGH because the
selector has no dependency-pull term, so a fleet-blocking chore competes on the
same axis as the holder's own product work.

**The shape.** Write ONLY the specific field, through the ordinary row-routed
update:

```bash
bash core/scripts/team-state-update.sh \
  --field agent_status.<holdout>.<the-one-field> \
  --value '{"...": "...", "attested_by": "<peer> proxy", "proof": "<the checkable evidence>"}'
```

Four properties make this safe, and three of them are enforced by the writer
rather than by this text:

- `route_field` sends `agent_status.<holdout>.*` to the holdout's OWN row file,
  so no shared object is touched and no other writer contends.
- `stamp_row_metadata` records `row_updated_by: <you>` automatically. The proxy
  is self-attributing whether or not the payload says so — put `attested_by` in
  the payload anyway, because that is the half a human reads.
- It does **not** bump `last_active`. That is the load-bearing difference from
  the cross-agent `in_flight` clear, which DOES bump it and makes a dormant peer
  read `alive` for up to the full 6h window (`guard-3604`). A proxy attestation
  leaves every liveness signal honest.
- The write is durable across the owner's next write because the owner refreshes
  from the store of record before its read-modify-write.

Then: post the proof to the coordination board (precedent
`msg-20260817-193329-alpha-5467`), and **file nothing on the holdout**. No goal,
no board request, no nudge — the work is done.

Never touch `in_flight`, never flip the gate's own flag, and never override its
verdict. In the precedent the fail-closed cutover check read SAFE 5/5 **on its
own** once the missing evidence was present; the verdict was never overridden,
only the evidence supplied. If supplying the fact does not clear the gate, the
gate is telling you the fact is not what was missing — stop.

**The boundary: proxy applies to FACTS, never to consent, credentials, or
judgment calls.** "This commit is an ancestor of that one" is a fact any peer can
check. "I approve this" and "I accept this risk" are not, and no amount of
evidence makes them proxyable. A hand-stamp the holdout later writes SUPERSEDES
the proxy — it is the owner's own record and outranks a peer's reading of it.

**Enforcement: none. This section is honor-system**, in the same way
`guard-349`'s standing-grants section is — nothing in the tree reads it, and no
gate refuses a write that ignores it. Stated plainly because a documented control
with no reader looks exactly like a live one (`guard-3130`, `guard-3485`), and a
convention that implies enforcement it does not have is worse than one that
admits it. Cross-references: `guard-4215` (cost burn is revenue-class — the
class of gate most worth unblocking), `rb-8202`, g-115-6578 A.

### Integration Points

- **Boot** (Step 2): Read `world/team-state.yaml` → display strategic focus and recent completions
- **Boot** (continuation Step 0.5): Read team state for fast situational awareness
- **Prime** (Phase 2): Read team-state and surface partner.in_flight in the PRIMED summary
- **Precheck** (iteration top): Read team-state and surface partner.in_flight in the iteration header
- **Select** (candidate filter): Drop goals from the candidate set whose goal_id matches partner.in_flight.goal_id **OR any `partner.in_flight_bodies.<sid>.goal_id`** — both shapes, per "TWO SURFACES" above. Reading only the first opens this filter completely on a worker-executed goal (g-306-276).
- **Execute Phase 4** (claim-conflict gate): Read team-state immediately before posting board claim; if the selected goal matches partner.in_flight.goal_id **or any partner.in_flight_bodies.<sid>.goal_id** → abort + log + re-select. Otherwise proceed — `aspirations-claim.sh` stamps the appropriate surface AND posts the board claim automatically (g-115-3199 / g-115-2123); no LLM-side call for either.

  The two bullets above are the only integration points that make a COORDINATION
  DECISION from this field, so they are the two that must union both surfaces;
  Prime and Precheck merely display it, and a display that omits body rows
  under-reports live work without mis-routing it. Until both surfaces are read
  everywhere, **the board `claim` post is the PRIMARY cross-Body claim signal,
  not a supplement to `in_flight`** (guard-997).
- **State Update** (Step 3.5): After meta update, append to recent_completions and update agent_status
- **iteration-close.sh**: After completion, call `team-state-clear-in-flight.sh` to release the live snapshot
- **All-Blocked** (B0): Before concluding partner is silent, read `agent_status.<partner>.last_active` and apply the 6h pre-silence threshold per `.claude/rules/check-team-state-before-silent.md`
- **Consolidation** (Step 8.85): Update agent_status with session summary at session end
- **Create Aspiration**: Read strategic_focus to align new aspirations with team direction
- All writes go through `team-state-*.sh` (locked via `_fileops.py`)
