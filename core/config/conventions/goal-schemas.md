# Goal Scoring Script Access

Goal selection scoring is implemented by `core/scripts/goal-selector.py` with exploration noise.
The script computes 17 deterministic criteria plus 1 stochastic criterion (`exploration_noise`)
scaled by the developmental stage's epsilon. The LLM never computes goal scores — the script
handles all arithmetic. The LLM reads the ranked output and applies metacognitive assessment
(Phase 2.5), which may override.

| Script | Purpose | Stdin |
|--------|---------|-------|
| `goal-selector.sh` | Score and rank unblocked goals | — |

Output: JSON array sorted by score descending. Each entry:
```json
{"goal_id": "g-001-01", "aspiration_id": "asp-001", "title": "...", "skill": "...",
 "category": "...", "score": 8.7,
 "breakdown": {"priority": 3.0, "deadline_urgency": 0, ..., "exploration_noise": 0.42},
 "raw": {"priority": 3, "deadline_urgency": 0, ..., "exploration_noise": 0.73},
 "exploration_params": {"epsilon": 0.19, "noise_scale": 3.0, "noise_weight": 0.57}}
```

All backed by `core/scripts/goal-selector.py` (Python 3, PyYAML optional).

---

# Goal Verification Schema (Unified)

Goals use a unified `verification` field that replaces the legacy `desiredEndState` +
`completion_check` pair. Both old and new formats are accepted (backward compatible).

```yaml
# NEW format (preferred for all new goals):
verification:
  outcomes:        # Human-readable success criteria (replaces desiredEndState)
    - "L2 knowledge node exists for identified domain"
    - "Node has at least 1 source article"
  checks:          # Machine-verifiable conditions (replaces completion_check)
    - type: file_check
      target: world/knowledge/tree/_tree.yaml
      condition: "Has at least one L2 node registered"
  preconditions:   # What must be true before execution (mixed form — see below)
    - "Root domain node exists (from g-001-00 or bootstrap)"   # string: LLM-evaluated
    - type: file_exists_after                                  # dict: selector-evaluated
      id: pc-tree-seeded
      path: "world/knowledge/tree/_tree.yaml"
      after_ref: "git:HEAD~1"

# LEGACY format (still accepted, auto-mapped):
desiredEndState: "At least one L2 node exists..."   # → verification.outcomes[0]
completion_check:                                    # → verification.checks[0]
  type: file_check
  target: world/knowledge/tree/_tree.yaml
  condition: "Has at least one L2 node registered"
```

`verification.outcomes` = what success looks like (for spark checks, aspiration assessment).
`verification.checks` = how to verify it (for Phase 5 completion, Phase 0 auto-detect).
`verification.outcomes_agent_leg` (optional) = the agent-side subset of `outcomes`
for a `participants: [agent, user]` collaborative goal. When present and all its
entries are met, Phase 5 verify recognizes "agent leg complete, user leg pending"
as a valid terminal state and closes the goal without inventing closure
justification for the user-gated outcomes (US-04 / g-305-04). Omit on agent-only
goals — `outcomes` alone governs them. The sibling `user_leg_scope` field (see
Participant-Based Goal Routing below) names what the user leg requires.
`verification.preconditions` = what must be true before execution. Supports two forms
side by side — all must pass (AND semantics):
  - **String** — natural-language condition, evaluated by the LLM in
    aspirations-select Phase 2.2 (soft judgment calls).
  - **Dict with `type`** — structured predicate, evaluated mechanically in the
    goal-selector COLLECT filter by `core/scripts/predicate.py`. See
    [preconditions.md](preconditions.md) for the three v1 types
    (`file_exists_after`, `command_succeeds`, `goal_completed_after`), the
    `after_ref` grammar, and the auto-clear deferral flow.
`verification.verification_hint` = advisory text suggesting what machine checks to consider when
creating a goal. Present on templates with `checks: []`. Read during goal creation to prompt
the agent to populate `checks` with concrete machine-verifiable conditions. Not enforced —
Phase 5 Verification Escalation handles empty-checks goals structurally.

---

# Origin Signal (MANDATORY)

Every goal MUST carry an `origin_signal` field citing the upstream cause that
spawned it. Enforced at write time by `core/scripts/origin-signal-gate.py`,
invoked from both `cmd_add_goal` and `cmd_add` in `core/scripts/aspirations.py`.
Goals that reach the gate without a valid signal are rejected unless an
override (`--override-signal "<justification>"`) is supplied, which audit-logs
to `world/origin-signal-overrides.jsonl`.

## Allowed prefixes

| Prefix | Cites | Typical caller |
|--------|-------|----------------|
| `user_directive` | Direct user request | `/respond`, `/create-aspiration from-user` |
| `board_post:<id>` | Coordination-board post | `insight-trigger-gate.py`, directive-routing |
| `pending_question:<id>` | Unanswered user question | pending-question handlers |
| `failing_test:<id>` | Failing test run | `run-test-circuit` |
| `resolved_hypothesis:<id>` | Pipeline resolution | `review-hypotheses`, signal-gated fallback |
| `low_confidence_node:<path>` | Tree node below confidence floor | retrieval-driven follow-up |
| `recurring_cadence:<id>` | Cadence-driven re-fire | `/run-processor` monitor, relocated recurrings |
| `decomposition:<parent-id>` | HTN breakdown | `/decompose` sub-goal loop |
| `parent_aspiration:<asp-id>` | Aspiration's motivation | `/create-aspiration`, complete-review |
| `unblock:<goal-id>` | CREATE_BLOCKER or unblock conversion | `create-blocker.py`, stall-goal-filer |
| `investigate:<ctx>` | Cognitive primitive — investigate | weakness analysis, execution reflection |
| `idea:<ctx>` | Cognitive primitive — idea | cargo-cult detector, scope-creep deferral |
| `maintain:<ctx>` | Cognitive primitive — maintain | self-correction framework maintenance |
| `idle_fallback` | Honest idle exit | `aspirations-all-blocked` B3 |

## Derivation rules

1. Cite the earliest upstream cause, not the immediate parent. "The strategic
   scan detected X → investigate" is `investigate:strategic-scan-X`, not
   `investigate:{just-completed-goal-id}`.
2. When the trigger has an ID (blocker_id, question_id, finding_id, goal_id),
   include it: `investigate:blocker-{bid}` beats `investigate:generic`.
3. For recurring goals, cite the original recurring cadence id, not the
   relocated destination: `recurring_cadence:{original.id}`, not
   `maintain:asp-001`.
4. For cognitive-primitive conversions from an executing goal, cite the
   triggering goal: `unblock:{goal.id}`, `idea:{goal.id}`,
   `investigate:{goal.id}`.
5. User-originated work (CLI directives, replies) always uses
   `user_directive` — never `investigate:user-said-X`.

## Override flag

```
aspirations-add-goal.sh <asp-id> --override-signal "<≥20-char justification>"
```
Use only when the spawn cause genuinely cannot be classified. The override is
audited; the reviewer on `world/origin-signal-overrides.jsonl` is expected to
extract a new prefix into this table if a pattern emerges.

---

# Goal Source Field (g-305-01, 2026-05-13)

Optional `goal_source` field captures **who initiated this goal** in a closed
vocabulary, separate from `origin_signal` (which cites the upstream cause).
Where `origin_signal` says "spawned from board-post msg-X", `goal_source`
says "this is user-pushed work" or "this is agent-self-generated work".

The distinction matters for cycle / strategic-drift detectors: if a user
pushes 30 framework goals in a sprint, the agent shouldn't be penalized by a
"framework-vs-domain" metric that conflates user-pushed framework work with
agent-self-generated framework work. Filtering by `goal_source` separates the
two signals.

## Allowed values

Single source of truth: `VALID_GOAL_SOURCES` in `core/scripts/aspirations.py`.

| Value | Meaning | Typical caller |
|-------|---------|----------------|
| `user` | User asked for this goal (CLI directive, email reply, /respond directive, pending-question answer) | `/respond`, `/create-aspiration from-user`, pending-question handlers |
| `agent-self` | Agent generated this goal autonomously (cognitive primitives, decompose, idle playbook, in-flight observation) | `aspirations-execute`, `/decompose`, `/create-aspiration from-self`, `/create-aspiration from-followup`, `aspirations-all-blocked` |
| `recurring-cycle` | This is a recurring cadence re-fire | `recurring-close.sh`, `aspirations-precheck` recurring re-arming |
| `cycle-detector` | Filed by a cycle / failure-mode detector based on telemetry (failing test, low-confidence node, resolved hypothesis, drift signal) | `run-test-circuit`, `/review-hypotheses`, `cargo-cult-detector.py`, retrieval-driven follow-up |
| `forge-skill` | Filed by `/forge-skill` (skill creation + validation pipeline) | `/forge-skill` Step 5 validation goal |
| `null` | Source unknown or pre-backfill legacy | (default for absent field; backfill assigns where inferable) |

## Auto-derivation from origin_signal

When `goal_source` is absent at goal-add time, `cmd_add_goal` and `cmd_add`
infer it from `origin_signal` prefix using the mapping in
`core/scripts/_goal_source.py::infer` (the single source of truth, imported
by both the CLI path and the daemon writer endpoint):

| `origin_signal` prefix | Inferred `goal_source` |
|------------------------|------------------------|
| `user_directive`, `user-directed:*`, `user_directed:*`, `pending_question:*` | `user` |
| `recurring_cadence:*`, `recurring:*` | `recurring-cycle` |
| `failing_test:*`, `resolved_hypothesis:*`, `low_confidence_node:*`, `drift_detected:*`, `monitor:*`, `alert-email:*`, `routing-mismatch:*`, `routing-either-resolve:*`, `insight_trigger:*` | `cycle-detector` |
| `decomposition:*`, `parent_aspiration:*`, `unblock:*`, `investigate:*`, `investigation:*`, `idea:*`, `maintain:*`, `apply:*`, `brief:*`, `board_post:*`, `program-change-proposal:*`, `idle_fallback` | `agent-self` |

Variant prefixes (`investigation:` vs canonical `investigate:`, `user-directed:`
vs canonical `user_directive`, etc.) are recognized for backward compatibility
with production records pre-dating this field. New goals should use the
canonical prefixes from the Origin Signal table above.

Explicit `goal_source` in the goal record always wins; auto-derive only fills
absent values. Forge-skill goals must set `goal_source: forge-skill` explicitly
(no signal-prefix maps to it).

## Consumers

- A future cycle-detector consumer (not yet implemented; would live at
  `core/scripts/cycle-detector.py`) should filter to
  `goal_source in {agent-self, cycle-detector}` before computing drift ratios,
  so a user-pushed framework sprint doesn't inflate the "framework dominates"
  signal. Until that script exists, this filter is the responsibility of any
  consumer that calculates drift ratios from `goal_source`.
- `agent-completion-report` — surface "of N goals this window, X were user-
  initiated, Y agent-self, Z recurring" so the user can see the work-source
  split, and the agent isn't perceived as dragging when it's executing user
  instructions.

## Set via

- Goal creators pass `"goal_source": "user"` (or any valid enum value) in the
  JSON to `aspirations-add-goal.sh` / `aspirations-add.sh`. Omit to let auto-
  derive fill it.
- Backfill on existing goals: `bash core/scripts/backfill-goal-source.py
  --apply` (dry-run by default).
- Manual update: `aspirations-update-goal.sh <goal-id> goal_source <value>`.

## Backward compatibility

Field is optional and defaults to `null` for goals predating this change. The
backfill script populates `null` entries where the origin_signal prefix maps
cleanly; entries that cannot be classified remain `null` and contribute zero
to drift-detector denominators (excluded, not penalized).

---

# Filed-By Agent Field (g-318-01, 2026-06-13)

Optional `filed_by_agent` field records **which agent filed (added) this goal**,
stamped at add time from the requesting agent (the `X-Mind-Agent` header).
Distinct from `goal_source` (the *initiation class* — user vs agent-self vs
recurring) and from `completed_by` / `claimed_by` (which name the agent that
*executed* the goal). `filed_by_agent` answers a question none of the others
can: **"who filed the goal that later expired?"** — the churn signal for the
per-agent contribution-vs-harm scorecard (asp-318 / g-318-02). Previously this
could only be inferred by heuristics (which queue the goal lives in, or
git-blame on the add).

## Stamped at

The sole live goal-add path: the daemon endpoint
`mind_api/src/endpoints/aspirations_write.py` `add_goal` handler (what
`aspirations-add-goal.sh` POSTs to). The pre-2026-05-14 Python CLI
`cmd_add_goal` no longer exists (daemon-only migration). The stamp uses
`goal.setdefault("filed_by_agent", agent)` — so an explicit caller-supplied
value (a goal filed on behalf of another agent) is preserved, and an empty
agent resolution leaves the field unset (read-time `unknown`) rather than blank.

## Validation

`aspirations.py::validate_goal` accepts null or string and rejects non-string.
It is NOT constrained to the active-agent roster — a goal filed by an agent
later removed from the team still validates (same policy as `abstained_by`).

## Backward compatibility

Field is optional; goals predating this change have no `filed_by_agent` and read
as `unknown`. No requirement clause (absence never raises). Like `claimed_by`,
the field is intentionally NOT in `COMPACT_GOAL_KEEP` — attribution fields are
dropped from compact goals.

---

# Recurring Goal Fields

Goals that re-fire on a schedule use these fields:

- `recurring`: `true`/`false` — whether the goal repeats. Set to `false` to permanently stop.
- `interval_hours`: positive number — hours between executions (e.g., 0.25, 4, 8, 24). Default: 24.
- `offload_decision`: string — **REQUIRED at add time when `recurring: true` or `interval_hours` is present** (operator-offload gate, gh-005 in `meta/aspiration-generation-strategy.yaml`). One line stating why this work stays on the LLM loop instead of becoming an Ayoai-Operator scheduled job: `"stays-mind: judgement/retrieval-heavy (<what>)"`, `"stays-mind: mind-box-local state (<what>)"`, `"stays-mind: web research"`, or `"operator-pull: reads <JobName> audit rows"`. Enforced at both daemon add sites in `mind_api/src/endpoints/aspirations_write.py`; bypass with `--override-offload "<reason>"`. Rationale: a recurring goal burns a full LLM iteration every cycle — deterministic + clocked + checkable work belongs on the operator (`/build-operator-job`, `world/conventions/operator-ramp.md`, rb-3281). Goals predating 2026-07-13 have no field (gate fires only on ADD, never retroactively).
- `remind_days`: DEPRECATED — converted to `interval_hours * 24` for backward compatibility. Use `interval_hours` for new goals.
- `lastAchievedAt`: `YYYY-MM-DDTHH:MM:SS` — full ISO 8601 timestamp of last completion. Legacy `YYYY-MM-DD` format is accepted (assumes start of day).
- `achievedCount`: integer — total times completed
- `currentStreak`: integer — consecutive on-time completions. Resets to 1 when `hours_since(lastAchievedAt) > 2 * interval_hours` at completion time (missed interval). First completion always starts at 1.
- `longestStreak`: integer — best streak ever
- `windowStreak`: integer (default 0, schema-additive) — tolerant streak counting cycles within `windowStreakMultiplier × interval_hours`. Distinct from `currentStreak`: tolerates wider gaps for "fire at least once per N intervals" semantics. Origin: LifingPolls plan item 6 (2026-05-08).
- `longestWindowStreak`: integer (default 0) — best window streak ever.
- `windowStreakMultiplier`: integer (default 7) — how many intervals can elapse before window streak resets. 7 is suitable for daily-ish goals expected to fire at least weekly; raise for quarterly cadences, lower for stricter.
- `consecutive_routine`: integer (default 0, schema-additive) — cross-session count of consecutive `outcome=routine` closes via `recurring-close.sh`. Increments on routine, resets on deep, resets to 0 again after `cargo-cult-detector.py` files an Idea. Distinct from `loop_state.routine_streaks[goal_id]` which is per-session anti-drift; this counter survives session boundaries so a recurring goal that returns nothing actionable across multiple sessions is detectable.
- `consecutive_deep`: integer (default 0, schema-additive) — mirror counter to `consecutive_routine`. Increments on `outcome=deep` closes, resets on routine. Drives auto-contract: when `consecutive_deep >= recurring.deep_streak_contract_threshold` (default 3), `cargo-cult-detector.py --contract-mode` divides `interval_hours` by `recurring.deep_streak_contract_divisor` (default 1.5) — capped at `recurring.contract_floor_ratio × original_interval_hours` (default 0.33). Past the floor, files an Idea proposing config-level rebase. Origin: LifingPolls plan item 4 (2026-05-08).

---

# Cross-Aspiration Support (Item 2 — LifingPolls 2026-05-08)

Goals can declare that completing them advances OTHER aspirations in addition
to their own parent. Soft attribution only — completion still ticks the
parent aspiration; this is a scoring-time bonus, not a structural change.

```yaml
supports: ["asp-181", "asp-274"]   # additive, optional
```

`goal-selector.py` criterion `cross_aspiration_support` reads each entry,
looks up the supported aspiration's completion ratio, and adds
`(ratio² × 2.5) × 0.3` per support — capped at +2.0 total. Goals
supporting near-complete aspirations score higher (consolidation pull
extends across aspiration boundaries). Empty/missing field contributes 0.

Weight: `meta/goal-selection-strategy.yaml → cross_aspiration_support`
(default 0.5). Set via `aspirations-update-goal.sh <goal-id> supports
'["asp-id-1","asp-id-2"]'`.
- `fire_when`: optional structured precondition — sugar for "fire only when this upstream signal is present." Same predicate registry as `verification.preconditions` (see `preconditions.md` for predicate types). When present and evaluates `false`, the recurring goal is filtered out by `goal-selector.py` AND `recurring-precondition-sweep.py` advances `lastAchievedAt` to suppress overdue-ratio runaway. Use this for recurring probes whose target may not exist (e.g., a deploy-status probe only fires when a service is actually deployed; an archival probe only fires when stale > threshold). Distinct from `verification.preconditions` semantically — preconditions block execution; `fire_when` declares "this recurring schedule is contingent on an external signal." Mechanically they go through the same evaluator. Schema example:
  ```yaml
  fire_when:
    type: command_succeeds
    command: "bash world/scripts/probe-<deployment-resource>.sh"
  ```
- `deliverable_file`: optional string (schema-additive, g-115-2036) — path to the file a recurring goal is expected to REGENERATE on every cycle (e.g. a completion report, a metrics snapshot). When set, `recurring-close.sh` runs `deliverable-verify.py` BEFORE the verify phase bumps `lastAchievedAt`, comparing the file's mtime against the CURRENT (prior-close) `lastAchievedAt`. If the file was NOT modified since the prior close, the close emits a non-blocking `⚠ DELIVERABLE NOT REGENERATED` warning to stderr — catching the rb-428 LLM-abbreviation drift where a close advances `lastAchievedAt` without the skill's deliverable-writing step having run (canonical g-001-04: 2026-07-11 close bumped `lastAchievedAt`, no write touched the report). FLAG-ONLY + fail-open: it never blocks a close (a false-stale mtime, e.g. an own-cloud stale pull, must not gate real work) and goals WITHOUT the field close exactly as before. The literal `{agent}` placeholder expands to the running agent, so a SHARED recurring goal (run by several agents under an `MIND_AGENT` override) can name a per-agent deliverable — e.g. `"agents/{agent}/COMPLETION-REPORT.md"` for the `/agent-completion-report` goal (rb-1556). Relative paths anchor to `PROJECT_ROOT`.

To permanently stop a recurring goal: set `recurring: false` via `aspirations-update-goal.sh <goal-id> recurring false`. The data layer (`cmd_update_goal` in `aspirations.py`) cascading-clears `interval_hours` and `lastAchievedAt` in the same write — preserving `achievedCount` / `currentStreak` / `longestStreak` as historical record. Without the cascade, the orphan timing fields would mislead the goal-selector's "not yet due" filter and require the archive sweep to silently repair.

To complete one cycle of a recurring goal atomically: `bash core/scripts/recurring-close.sh <goal-id> <routine|deep>`. Wraps the 4 iteration-close phases, bumps `lastAchievedAt` via `aspirations-complete-by.sh` (routed inside `iteration-close.sh do_verify` when the goal is recurring), updates `consecutive_routine`, and fires the cargo-cult detector at threshold (`recurring.cargo_cult_threshold`, default 3 in `core/config/aspirations.yaml`).

Phase 0 Recurring Goal Checks resets completed recurring goals to `pending` after `interval_hours` elapses. Phase 7 skips "aspiration fully complete" for aspirations where ALL goals are recurring (perpetual aspirations).

---

# Cross-Agent Handoff Fields (Item 3, added 2026-04-18)

Optional fields that tag a goal as routed to a specific agent. Used when one
agent (e.g., an agent in a planning/reviewer role) identifies work for another
agent (e.g., an agent in an implementer role). Goal-selector applies scoring
bonus for the matched agent and penalty for others; precheck escalates aged
handoffs.

```yaml
handoff_to: <agent-name>      # Scoring bonus when current MIND_AGENT matches.
                              # Goals routed to other agents get -0.20 penalty.
                              # Target must be reachable via participants —
                              # [agent], [agent, user], or explicit [<name>]
                              # all qualify.
handoff_from: <agent-name>    # Creator agent for attribution.
handoff_created_at: <ISO>     # Timestamp for aging-based escalation.
```

When any handoff field is set, the precheck aging check kicks in:
- At `handoff_aging.warn_hours` (default 48h): goal-selector adds escalating bonus.
- At `handoff_aging.escalate_hours` (default 72h): precheck Phase 0.5b.2 posts
  board message tagged `handoff-aged` and notifies the target agent on next boot.

Parameters: `core/config/aspirations.yaml` → `scoring.handoff_bonus` (default 0.30),
`handoff_aging.warn_hours` / `escalate_hours`.

Scoring: goal-selector.py criterion 13c. Schema is additive — goals without
`handoff_to` are unchanged.

---

# Episode Chaining Fields (MR-Search)

Goals that undergo multi-episode chaining (retry with inter-episode reflection) accumulate an `episode_history` tracking each attempt. Populated by the Episode Chain Protocol in Phase 4 of aspirations-execute. See `core/config/aspirations.yaml` `episode_chaining` for config.

```yaml
episode_history:               # Accumulated attempts (populated by Episode Chain Protocol)
  - episode: 1
    approach: "Web research on topic X using broad search terms"
    outcome: "failed"
    key_observations: ["Search results too generic", "Domain terminology unknown"]
    reflection: "Need domain-specific terminology — check tree nodes first"
    timestamp: "2026-03-25T14:30:00"
  - episode: 2
    approach: "Targeted search using domain terms from tree node"
    outcome: "completed"
    key_observations: ["Found 3 relevant sources", "Key insight encoded"]
    reflection: null           # null on final/successful episode
    timestamp: "2026-03-25T14:45:00"
```

The episode chain captures the *progression of understanding* — how the agent's approach evolved across attempts. Step 8 tree encoding uses the full chain when present.

---

# Knowledge-Debt Closure Field

Goals that resolve an outstanding `knowledge_debt` entry in working memory declare it via `closes_knowledge_debt`. A goal with this field set is NEVER routine-classified (see the semantic override in `.claude/skills/aspirations-execute/SKILL.md` Phase 4-post) — closing a debt produces a knowledge event that must go through the deep pipeline including immediate tree encoding.

```yaml
closes_knowledge_debt:                # Optional; default []
  - "service-response-patterns"       # node_key from working-memory knowledge_debt[]
  - "guardrail-utilization-counters"
```

- `closes_knowledge_debt`: list of `node_key` strings that appear (or appeared, before this goal ran) in working-memory `knowledge_debt`. Empty or absent = no debt closure.

Population:
- **By decompose** during goal field assembly: when a proposed goal's description/category references a `knowledge_debt[].node_key`, the field is pre-populated.
- **By aspirations-state-update Step 8** as a fallback: after tree encoding, if the updated node matches a `knowledge_debt[].node_key`, the field is back-populated via `aspirations-update-goal.sh`.

Classifier effect: `aspirations-execute` Phase 4-post checks this field after assigning `outcome_class = "routine"`. If non-empty AND the corresponding debt entry is gone from working memory after this goal ran (or the tree node's `last_updated` is today), the classifier flips to `deep`.

---

# Execution Mode Field (MR-Search Exploration Masking)

Goals can be designated as "exploration" to shield them from negative evaluation pressure while retaining all learned information. Populated by Phase 2.5 auto-designation or set manually during goal creation. See `core/config/aspirations.yaml` `exploration_mode` for config.

- `execution_mode`: `"standard"` (default) or `"exploration"`
  - `standard`: Normal evaluation — outcomes count toward accuracy stats, streaks, evolution triggers
  - `exploration`: Shielded — outcome does NOT count toward accuracy stats, streak resets, or negative evolution triggers. But ALL information IS retained in experience archive and knowledge tree.

Auto-designation: Phase 2.5 designates goals as `exploration` when the goal's category capability_level is below `exploration_mode.auto_designate_below_capability` and the session exploration fraction is below `max_exploration_fraction`.

---

# Deferred Goal Fields

Goals that should not execute until a specific future time use these fields:

- `deferred_until`: `YYYY-MM-DDTHH:MM:SS` or `null` — ISO 8601 timestamp. Goal is filtered out of COLLECT if `now < deferred_until`. Once the time passes, the goal competes normally. One-shot: not reset after execution.
- `defer_reason`: string or `null` — why this goal is deferred. **Functional filter**: a non-null `defer_reason` prevents the goal from appearing as a candidate in `goal-selector.sh`, regardless of `deferred_until`. Must be explicitly cleared (set to `null`) to re-enable the goal. Cleared automatically by aspiration grooming (check 1e) when the reason is no longer backed by an active decision.

**Auto-pairing**: when `defer_reason` is set with prose containing a date phrase ("Not before 2026-07-14", "after July 14, 2026", "in 7 days", "tomorrow", etc.) AND `deferred_until` is null, `cmd_update_goal` runs `defer-date-extractor.py` and auto-sets the structured time gate from the earliest future date in the prose. Caller-supplied `deferred_until` always wins (extractor is skipped if the field is already set). Audit trail at `world/defer-date-extractions.jsonl`. Origin: LifingPolls plan item 5 (2026-05-08).

To defer a goal with a time gate: `aspirations-update-goal.sh <goal-id> deferred_until "2026-03-13T20:00:00"` and `aspirations-update-goal.sh <goal-id> defer_reason "Waiting for test results"`.
To defer indefinitely (until condition resolves): `aspirations-update-goal.sh <goal-id> defer_reason "Dependency not available"` (no `deferred_until` needed).
To un-defer: `aspirations-update-goal.sh <goal-id> defer_reason null`.

Compatible with all goal types including recurring. A recurring goal with `deferred_until` delays only its first execution; subsequent cycles use `interval_hours` normally.

---

# Participant-Based Goal Routing

Goals use the `participants` field to control which agents and users can execute them.
The goal-selector filters candidates based on the current agent's identity (`AGENT_NAME`).

**Values:**
- `[agent]` — any agent can execute (default, backward compatible wildcard)
- `[user]` — requires user action (agent skips entirely)
- `[agent, user]` — collaborative, any agent + user
- `["alpha"]` — only the agent named "alpha" can execute
- `["bravo"]` — only "bravo" can execute
- `["alpha", "bravo"]` — either alpha or bravo (explicit multi-agent)
- `["alpha", "user"]` — alpha + user collaborative

**Rule**: `"agent"` is the wildcard — it matches any agent. Specific agent names are restrictive.
If a goal's participants contain specific names but NOT `"agent"`, only named agents see it.

**Goal-selector behavior**:
- COLLECT phase: ineligible goals are filtered out (never scored)
- SCORE phase: eligible goals get `agent_executable: +2` (weight 0.8 → +1.6 effective)
- Bottleneck trace: ineligible goals report `"OTHER AGENT (alpha)"` or `"NEEDS USER"`

### User-Leg Scope (`user_leg_scope`)

When `participants` contains `user`, a goal SHOULD declare `user_leg_scope` — a
single string naming what the user would otherwise have to approve. The selector
and `guard-349` match this string against standing grants in
`world/conventions/capability-routing.md` to decide whether a grant already
authorizes the goal (in which case it routes as pure-agent).

**Valid values** (single source of truth: `VALID_USER_LEG_SCOPES` in
`core/scripts/aspirations.py`):
`commit`, `push`, `deployment-approval`, `architecture-decision`,
`credential-grant`, `data-provision`, `new-resource`.

Extend when a new *kind* of user approval is recognized as a category (whether
or not a matching grant exists yet). Keep vocabulary closed to prevent sprawl:
add a value here first, then — if appropriate — a matching grant row in
`capability-routing.md`. The two vocabularies must stay locked on the matching
direction (every scope a grant lists must exist here).

**Backward compatibility**: the field is optional. Legacy goals without
`user_leg_scope` still work; `aspirations-add-goal.sh` emits a stderr WARN
(not a block) when `user` is in `participants` and the field is absent.
Guard-349 falls back to prose recognition in that case. Backfill via
`aspirations-update-goal.sh <goal-id> user_leg_scope <scope>`.

### Blocker Reference Schema (`blocker_ref`)

Every narrative `defer_reason` MUST be paired with a structured `blocker_ref`
so the quiescence gate can distinguish genuine external gating from narrative
laundering ("awaiting user feedback" on every deep goal to qualify for long
sleep). Enforced at write time by `aspirations.py cmd_update_goal` — missing
ref → exit 1 unless `--force-unstructured-defer "<justification>"` is passed
(which logs to `world/blocker-gate-overrides.jsonl` and disqualifies the goal
from quiescence eligibility).

**Field shape**:

```yaml
blocker_ref:
  # --- core keys (always present after validation) ---
  type:         <enum — see BLOCKER_REF_TYPES>
  external_id:  <string — observable identifier the next wake-cycle can probe>
  state_hash:   <string or null — optional snapshot for wake-miss detection>
  created_at:   <ISO 8601 timestamp, auto-populated>
  expires_at:   <ISO 8601 timestamp, auto-populated from per-type TTL>
  # --- promoted optional keys (present only when supplied) ---
  unblock_goal: <goal-id or board-msg ref — the thing whose completion clears this>
  why:          <free text — human-readable rationale>
```

**Key vocabulary is closed (g-115-3532).** `validate()` accepts exactly the keys
above. Three input aliases fold onto their canonical spelling — `unblocking_goal`
and `unblocking_goal_id` → `unblock_goal`, `reason` → `why`, `ref` →
`external_id` — and an explicit canonical key wins over its alias. **Any other
key is REFUSED, not silently dropped** (`blocker_type` → use `type`;
`blocking_goal` → use `unblock_goal`; `blocker_id` → use `external_id`;
`denied_action` / `principal` / `probe` → put it in `why`). Refusing is
deliberate: absorbing variants one at a time is how a vocabulary reaches five
spellings, which is exactly how this schema got into the state described below.

`unblock_goal` and `why` are PROMOTED rather than stripped because a live reader
consumes them — `blocked-signal-resolution-check._resolve_blocker_ref` resolves
a ref via `unblock_goal`, so stripping it would convert a resolvable block into
an opaque one.

> **AUTO-POPULATION CAVEAT — RESOLVED for the write path (g-115-3532,
> 2026-07-27).** "Auto-populated" was previously a claim about ONE write path
> and FALSE for the others: population happens inside
> `gates/blocker_ref.validate()`, which was reached ONLY via the `--blocker-ref`
> flag (CLI) / `X-Mind-Blocker-Ref` header (daemon). A DIRECT field write —
> `aspirations-update-goal.sh <id> blocker_ref '<json>'` — landed verbatim with
> no validation, no normalization and no TTL.
>
> Measured consequence (2026-07-27, g-115-3505): of 11 live blocked goals
> carrying a dict `blocker_ref`, exactly ONE matched `validate()`'s output
> shape and 6 carried no `expires_at` at all. A ref with no `expires_at` never
> TTL-expires, so the auto-conversion promised in "Expiry behavior" below could
> never fire for it.
>
> **Both** write paths now normalize: `aspirations.py cmd_update_goal` and
> `mind_api/src/endpoints/aspirations_write.py` route a direct dict write
> through the same `validate()` (guard-330 — every write path calls its
> full-record validator). Regression-pinned by
> `core/scripts/tests/test_blocker_ref_write_path_normalization.py`.
>
> **Two gaps remain — do NOT assume a stored ref is canonical.** (1) A
> bare-STRING `blocker_ref` is still a live reader-supported shape and is
> deliberately out of scope here (tracked in g-115-3313). (2) Stored refs are
> not re-validated on read, and the live corpus predates this fix: 4 `type`
> values in use are absent from `BLOCKER_REF_TYPES` (`resource-contention`,
> `coordination`, `upstream_artifact_unpushed`, plus one ref with no `type` at
> all), so those refs would be REFUSED if rewritten today. Enum reconciliation
> + backfill of the untimed refs is tracked in **g-115-3543**.
>
> `quiescence-gate.py` C3 treats an ABSENT `expires_at` as disqualifying
> (absent is not "not yet expired" — guard-487: a suppression gate fails
> CLOSED), so quiescence stays denied until the backfill lands.

**`type` enum** (single source of truth: `BLOCKER_REF_TYPES` in
`core/scripts/gates/blocker_ref.py` — `aspirations.py` imports it, it does not
define it. That module's header lists the 5 sites a new type must be added to,
including its `BLOCKER_REF_TTL_HOURS` entry, which is keyed BY type):

| Type | Meaning | `external_id` conventions |
|------|---------|---------------------------|
| `infrastructure` | Named infra component down (cloud provider, remote storage, service endpoint) | component name + date, e.g. `remote-storage:2026-04-21` |
| `resource` | Shared resource contention (lock, quota, rate-limit) | resource identifier |
| `user_action` | Genuinely human-only action (see `.claude/rules/capability-before-user.md`) | pending-question ID, e.g. `pq-abc-123` |
| `credentials-required` | Missing credential the agent cannot provision | credential name |
| `security-trust` | Security review the agent cannot self-grant | review ticket / decision ID |
| `physical-hardware` | Hardware action (reboot, cable, hardware token) | hardware asset ID |
| `partner-response` | Waiting on another agent's reply on the board | board msg ID, e.g. `msg-20260421-...` |
| `external-service` | Scheduled external probe / API call | probe ID |

**Per-type TTL** (single source: `BLOCKER_REF_TTL_HOURS` in
`aspirations.py`). On expiry the blocker auto-converts to an Unblock goal
via `aspirations-precheck` Phase 0.5b re-probe, disqualifying quiescence:

| Type | TTL (hours) | Rationale |
|------|-------------|-----------|
| `partner-response` | 72 | Matches `handoff_aging.escalate_hours` in `aspirations.yaml` |
| `external-service` | 24 | External probe should be re-scheduled within a day |
| `user_action`, others | 120 | Matches existing `defer_reason_timeout_hours` |

**Callers that write `blocker_ref`**:
- `aspirations.py cmd_update_goal` — when `field == defer_reason` and value is non-null and non-structured-prefix. Pass `--blocker-ref '<json>'`. Minimum shape: `{"type":"<enum>","external_id":"<id>"}`; `state_hash`, `created_at`, `expires_at` auto-populate.
- `create-blocker.py` — emits `blocker_ref` on the `known_blockers[i]` WM entry AND on its JSON stdout. `--external-id` required for `partner-response` and `external-service` types; synthesized as `<failure_skill>:<goal_id>` for other types if not provided.

**Callers that read `blocker_ref`**:
- `goal-selector.py collect_blocked()` — surfaces `blocker_ref` (None if absent) on every blocked-entry dict. Infrastructure blocks prefer the `known_blockers[i].blocker_ref` over the goal's own.
- `quiescence-gate.py check` — iterates blocked entries and rejects eligibility when ANY entry has `blocker_ref == None` or `blocker_ref.expires_at` in the past. Invoked from `aspirations-all-blocked` Step B6.5 before the B7 backoff ladder; `verify-wake` counterpart runs from `aspirations` Phase -0.5e' after sleep to detect state drift.

**Structured-prefix bypass**: defer_reason values starting with
`STRUCTURED_DEFER_PREFIXES` (`precondition_unmet:`, `blocked_on_dependency`,
`Circuit breaker:`, `human_blocked:`) skip both the capability-gate AND the
blocker_ref gate. Those are machine-written state markers from the framework
itself, not narrative claims about external signals.

`human_blocked:` (added 2026-06-25 by g-115-1646) is the one member that is
NOT self-clearing, and it behaves differently enough to be worth stating
here: it marks a genuinely non-agent-provisionable human gate (an approval
click, outside counsel, a credential only a person can grant), so it is
exempt from the 120h defer fall-through in `collect_eligible` /
`collect_blocked`. It stays suppressed-from-selector AND counted-in-blocked,
which is what lets quiescence fire during a human-gated plateau instead of
the goal re-surfacing every iteration. User-surfacing is preserved by the
precheck `human_blocked` age-escalation, not by re-selection.

Do not read the bypass as a licence to route work to a human — it is the
opposite. `.claude/rules/capability-before-user.md` and
`.claude/rules/reclaim-routed-work.md` still govern whether the gate is real,
and a `human_blocked:` defer is subject to the same RULE-axis re-check as any
other routing-away decision: a standing grant can retire the reason while the
condition remains perfectly true.

(This paragraph is late by 36 days. `test_structured_prefixes_published`
exists specifically to force a docs update when the tuple changes, went red
the day `human_blocked:` landed, and nobody saw it — nothing ran that file.
See g-115-3748.)

**Override ledger**: `world/blocker-gate-overrides.jsonl` receives one record
per `--force-unstructured-defer` (fields: `timestamp`, `agent`, `source`,
`goal_id`, `defer_reason`, `justification`, `which_checks_bypassed`). Same
append-only shape as the existing `blocker-create-gate.py` and
`capability-gate.py` overrides — a single audit trail.

### Straggler-Aware Goal Reallocation

Goals targeted at a specific agent can be marked `reallocatable: true` to allow other agents
to pick them up if the targeted agent hasn't claimed them within `reallocation_hours` (default 8,
configurable in `aspirations.yaml` → `multi_agent.reallocation_hours`).

- `reallocatable`: `true`/`false` (default: `false`) — whether this goal can be picked up by
  non-targeted agents after the reallocation window.
- The window is measured from the goal's `created` timestamp (or parent aspiration's `created`).
- Once the window expires and the goal has no `claimed_by`, any agent can execute it.
- Based on ["Language Model Teams as Distributed Systems"](https://arxiv.org/abs/2603.12229)
  Finding 5: decentralized teams dynamically reallocate straggler work.

---

# Output-Passing Dependencies (`depends_on`)

For cross-agent workflows where a downstream goal needs the factual output of an
upstream goal, use `depends_on` alongside `blocked_by`. Based on arXiv 2603.28990:
downstream agents that see factual completed outputs outperform those seeing
intentions or status by +44%.

```yaml
depends_on:
  - goal_id: "g-005-01"
    expects: "List of discovered API endpoints"
  - goal_id: "g-005-02"
    expects: "Test coverage report for auth module"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| goal_id | string | yes | ID of the prerequisite goal (must also appear in `blocked_by`) |
| expects | string | no | Human-readable description of what output this goal needs from the dep |

**Rules:**
- Each `depends_on.goal_id` MUST also appear in `blocked_by` (structural consistency)
- When a dependency resolves, the verify skill reads the `handoff` board message for the
  completed goal and prepends the factual output to the dependent goal's description
  as a `## Predecessor Output` section
- See `aspirations-verify/SKILL.md` "Unblock Dependent Goals" for the output injection protocol

---

# Review Gate Fields

Async peer review for code-change goals. Set by the executing agent after Phase 5 verify,
picked up by the reviewing agent during idle/all-blocked board scans.

- `review_requested`: ISO 8601 timestamp or `null` — set when a completed world goal with
  code changes posts a `review-request` to the coordination board (Phase 5.7).
- `review_completed`: ISO 8601 timestamp or `null` — set when the reviewing agent finishes
  reviewing the experience trace and approves or flags issues (Step B0 board scan).
- Both fields are informational — goals are NOT blocked pending review. Review is async.
- Set via: `aspirations-update-goal.sh --source world <goal-id> review_requested <timestamp>`
- See `coordination.md` Review Gate section and `aspirations/SKILL.md` Phase 5.7 + Step B0.

# Self-Abstention Field

When an agent determines it cannot add genuine value to a goal (capability mismatch),
it records this via the `abstained_by` field. Based on arXiv 2603.28990: 8.6% voluntary
abstention rate in the top model improves overall system quality.

- `abstained_by`: string or `null` — agent name that abstained. The abstaining agent's
  goal-selector skips this goal; other agents see it normally.
  Expires after `abstention_timeout_hours` (default 72h) — script-enforced in goal-selector.py.
- `abstained_at`: ISO timestamp — when abstention was recorded. Required for expiry.
  If missing (legacy), abstention expires immediately (fail-open).
- `defer_reason_set_at`: ISO timestamp — when defer_reason was set. Required for expiry.
  defer_reason without deferred_until expires after `defer_reason_timeout_hours` (default 120h).
  If missing (legacy), deferral expires immediately (fail-open).
- `blocked_since`: ISO timestamp or `null` — when the goal's `blocked_by` list was first
  populated with unresolved dependencies. Required for dependency timeout expiry. Set
  automatically by `cmd_add`, `cmd_add_goal`, `cmd_update`, and `cmd_update_goal` when
  `blocked_by` is non-empty. If missing (legacy), dependency timeout expires immediately (fail-open).
  Cleared to `null` when `blocked_by` becomes empty.
- Set via: `aspirations-update-goal.sh <goal-id> abstained_by <agent_name>` + `abstained_at <timestamp>`
- See `aspirations-select/SKILL.md` Phase 2.55 for the abstention check protocol

---

# Intended-Agent Routing Hint (`intended_agent`)

Optional routing **hint** suggesting which agent should ideally claim a goal.
Distinct from `participants`:

- **`participants`** controls ELIGIBILITY — who is allowed to execute. Restrictive.
- **`intended_agent`** is a PREFERENCE — who the goal-routing taxonomy thinks should take it. Advisory; the goal-selector may use it to bias scoring without restricting access.

The two can disagree on purpose. A goal with `participants: ["agent"]` (any
agent eligible) AND `intended_agent: "zeta"` is open to all but routes to zeta
first. This separation lets work overflow naturally when the preferred agent
is busy (per `world/conventions/agent-lanes.md` "Open Lanes" overflow row)
without changing the eligibility schema.

## Field

```yaml
intended_agent: null              # default — no routing hint, current fall-through
intended_agent: "alpha"           # routes to alpha first (any in ACTIVE_AGENTS)
intended_agent: "bravo"
intended_agent: "zeta"
intended_agent: "either"          # explicitly ambiguous — selector ignores hint
```

## Valid values

Single source of truth: `VALID_INTENDED_AGENTS` in `core/scripts/aspirations.py`.
Currently `{alpha, bravo, zeta, either}`. Kept in lockstep with
`ACTIVE_AGENTS` in `core/scripts/capability-route-gate.py`. When a new
agent is added to the team, BOTH constants must update together.

## Population

- **Automatic (creation-side)**: `aspirations.py cmd_add_goal` invokes
  `capability-route-gate.py` (g-282-04) when the field is absent and stores
  the gate's `intended_agent` output. Gate is permissive — returns `"either"`
  when uncertain, so legacy/edge-case goals are unrouted.
- **Manual**: goal authors can set the field explicitly via the JSON payload
  passed to `aspirations-add-goal.sh`, or via
  `aspirations-update-goal.sh <goal-id> intended_agent <value>`.
- **Override**: when the field disagrees with the gate's classification, the
  author's manual value wins; the gate's suggestion is recorded for audit
  (g-282-07 wires the override-ledger).

## Selector behavior

The goal-selector (`goal-selector.py`) reads `intended_agent` and applies a
soft scoring bias when set (g-282-05 wires this in). Goals where
`intended_agent == AGENT_NAME` get a small positive bonus; goals where
`intended_agent` is set to a different active agent get a small penalty.
`"either"` and `null` contribute zero. The bias never reduces a goal to
zero candidacy — eligibility comes from `participants`, not from this hint.

## Validation

`validate_goal()` in `aspirations.py` rejects values outside
`VALID_INTENDED_AGENTS` (unless `null`). The check exits 1 with a clear
error — same pattern as `user_leg_scope` and `abstained_by` validation.

## Backward compatibility

Field is optional and defaults to `null`. Existing goals without
`intended_agent` continue to score and execute exactly as before. The
g-282-06 backfill goal will populate the field on existing pending goals
via the gate.

## Cross-references

- `world/conventions/agent-lanes.md` — the taxonomy this field implements
- `core/scripts/capability-route-gate.py` — the classifier that produces values
- `g-282-04` — creation-side wiring (aspirations.py add-goal invokes gate)
- `g-282-05` — selection-side wiring (goal-selector.py reads field)
- `g-282-06` — backfill of existing pending goals
- `g-282-07` — explicit `--cross-lane` override + audit ledger

---

# Inner Refinement (`inner_refinement`)

Optional Self-Refine inner loop (Madaan et al., *Self-Refine: Iterative
Refinement with Self-Feedback*, arXiv 2303.17651; BRD Layer-1 Gap 4,
g-306-10). When set on a goal, Phase 4 execution runs a bounded
generate -> same-LLM critique -> regenerate loop on the goal's primary
artifact before handing off to Phase 5 verify. Default OFF: a goal without
this block (or with it `null`) executes exactly as before.

## Field

```yaml
inner_refinement: null                       # default - OFF, no inner loop
inner_refinement:
  max_iters: 3                               # int in [1, 5]; the refinement pass cap
  satisficed_when: "all verification.outcomes met by the draft"  # non-empty stop predicate
```

## Semantics

When `inner_refinement` is set, after the primary action produces a first
draft of the goal's artifact, execution loops:

1. **Generate** - produce (or carry forward) the current draft.
2. **Critique** - the SAME LLM critiques the draft against the goal's
   `verification.outcomes` (and `verification.checks` where present),
   naming concrete gaps. No external grader; the model judges its own work.
3. **Regenerate** - revise the draft to close the named gaps.

The loop stops at the FIRST of:
- `satisficed_when` is met (the model judges the predicate true), OR
- `min(max_iters, INNER_REFINEMENT_MAX_ITERS_CAP)` passes are exhausted.

The clamp to `INNER_REFINEMENT_MAX_ITERS_CAP` (currently 5, defined in
`core/scripts/aspirations.py`) is the structural termination guarantee:
the loop can never run more than CAP passes regardless of the stored
`max_iters` value, so it always terminates.

## Worked example (one artifact)

Goal: "Draft the retry-policy paragraph for the deploy runbook" with
`verification.outcomes: ["names the backoff base", "names the max attempts",
"names the give-up action"]` and
`inner_refinement: {max_iters: 3, satisficed_when: "all three outcomes named"}`.

- Pass 1 draft: "Retries use exponential backoff." Critique: backoff base
  unstated; max attempts missing; give-up action missing. Not satisficed.
- Pass 2 draft: "Retries use exponential backoff (base 2s) up to 5 attempts."
  Critique: give-up action still missing. Not satisficed.
- Pass 3 draft: "...up to 5 attempts; on exhaustion the deploy is rolled
  back and an Unblock goal is filed." Critique: all three outcomes named.
  Satisficed -> stop (before max_iters reached).

The artifact improved monotonically across passes and the loop terminated on
the stop predicate, not the cap. Had the predicate never been met, the
`max_iters=3` clamp would have stopped it after pass 3 anyway - the
termination guarantee that the bounded test in
`core/scripts/tests/test_inner_refinement_validation.py` pins.

## Validation

`validate_goal()` in `core/scripts/aspirations.py` enforces, when the field
is present and non-null:
- `inner_refinement` is a dict (else ValueError)
- `max_iters` is an int (not bool) in `[1, INNER_REFINEMENT_MAX_ITERS_CAP]`
- `satisficed_when` is a non-empty string

This validation is **CLI-only by design** - it follows the same optional-field
pattern as `reallocatable`, `abstained_by`, and `intended_agent`, all of which
validate in the CLI `validate_goal` only. The daemon `_validate_goal`
(`mind_api/src/endpoints/aspirations_write.py`) deliberately validates a
minimal subset (id/status/recurring/interval) per guard-547; optional-field
validation is not duplicated there. Because the termination guarantee lives in
the execution-side clamp (not in validation), it holds regardless of which
write path created the goal.

## Backward compatibility

The field is optional and defaults to OFF. Goals without `inner_refinement`
(every existing goal) score and execute exactly as before - the Phase 4
execution path checks for the block and no-ops when it is absent or null.

## Cross-references

- `g-306-10` - this field's implementing goal (BRD Layer-1 Gap 4)
- Self-Refine, arXiv 2303.17651 - the generate/critique/regenerate method
- `core/scripts/aspirations.py` - `INNER_REFINEMENT_MAX_ITERS_CAP` + `validate_goal` block
- `.claude/skills/aspirations-execute/SKILL.md` Phase 4 - the execution loop (clamped to CAP)
- `core/scripts/tests/test_inner_refinement_validation.py` - bounded validation + termination test
- guard-547 - the CLI/daemon validation split this field's CLI-only validation respects

---

# Optional Outcome & User-Signal Fields

All optional. Absent fields have zero effect on scoring; the scorer skips the
corresponding dimension. Introduced to close three feedback-loop gaps
(Tranche A scaffolding — see `world/reasoning-bank.jsonl` rb-390):

1. **Work-mix skew** — no field telling the selector what kind of work a goal
   represents, so framework maintenance accumulates gravity unopposed.
2. **Process-vs-outcome divergence** — no pointer from a goal to the
   business-layer signal it should move.
3. **User signal blindness** — email replies, directives, and 48h silence on
   user-gated goals produce no reweighting.

## Goal-level fields

- `work_class`: one of `framework`, `strategic`, `hygiene`, `pm_analysis`,
  `research`. Populated by goal creators; used by the selector's planned
  `class_balance_bonus` dimension (Tranche C) to pull under-represented
  classes up when the last-N-goals distribution drifts from
  `aspirations.yaml → work_class_targets`. Existing goals default to
  `unclassified` and are excluded from the balance computation until
  reclassified.

- `user_thread_id`: **RETIRED 2026-04-24 (g-252-03 / commit 95e4df6)** — string or `null` — was a stable
  identifier for a conversation thread this goal belongs to. Path A
  scoring contribution removed from `goal-selector.py`; field still
  present in goal records for historical/audit purposes only. Decision
  to fully remove deferred to 2026-05-07 re-audit (g-252-05); if Path B
  (criterion 7d `user_signal_boost`) still works post-audit, this entry
  will be removed entirely. Original semantics: sources were email
  `Message-ID` prefix, pending-question `id`, `/respond` directive `id`;
  goals with the same `user_thread_id` were boosted together when the
  scanner detected a reply or new directive on that thread.

- `user_signal_last`: ISO timestamp — most recent user signal on this
  goal's thread. Updated by the scanner (`world/scripts/user-signal-scan.sh`),
  not by the goal author. Still actively read by Path B
  (`user_signal_boost` criterion 7d).

- `user_signal_kind`: **RETIRED 2026-04-24 (g-252-03 / commit 95e4df6)** — one of `reply`, `directive`,
  `silence_48h`, `override`, `null`. Path A scoring contribution removed
  from `goal-selector.py`; field still present in goal records for
  historical/audit purposes only. Decision to fully remove deferred to
  2026-05-07 re-audit. Original semantics: controlled the sign of the
  scorer boost — `reply` and `directive` pulled UP, `silence_48h` pushed
  DOWN while spawning an escalation branch.

- `outcome_signal_source`: string pointer to the business-layer number
  this goal claims to move. Format: `<kind>:<locator>` where `<kind>` is one
  of `git`, `ci`, `api`, `metric` and `<locator>` is a kind-specific path —
  e.g. `git:<owner>/<repo>`, `ci:workflow/<filename>`, `api:<service>/<endpoint>`,
  `metric:<metric_name>`. Domains supply their own concrete locators. Read by
  `agent-completion-report` when producing the Outcome Delta section.
  Aspirations whose ALL goals lack this pointer are capped at MEDIUM
  priority in Tranche C unless flagged `internal_infrastructure: true`
  on the aspiration.

## Aspiration-level fields

- `internal_infrastructure`: boolean — opt-out from the outcome-pointer
  requirement. Legitimate for framework-maintenance aspirations that
  have no product-layer signal. Must be explicitly set `true` — absence
  counts as opt-in to the requirement.

## Producer / consumer

| Field | Written by | Read by |
|-------|-----------|---------|
| `work_class` | goal authors (agents, user directive) | `goal-selector.py` class_balance_bonus (Tranche C) |
| `user_thread_id` | goal authors when goal originates from a user signal | _retired 2026-04-24 (g-252-03)_ — historical only |
| `user_signal_last` | `user-signal-scan.sh` | `goal-selector.py` user_signal_boost (Path B), `agent-completion-report` |
| `user_signal_kind` | `user-signal-scan.sh` | _retired 2026-04-24 (g-252-03)_ — historical only |
| `outcome_signal_source` | goal authors | `outcome-metrics-collect.sh`, `agent-completion-report` |
| `internal_infrastructure` | aspiration authors | `aspirations-precheck` outcome-pointer gate (Tranche C) |

## Set via

Per goal-schemas convention, all are writable via `aspirations-update-goal.sh`:

```
aspirations-update-goal.sh <goal-id> work_class strategic
aspirations-update-goal.sh <goal-id> user_thread_id "msg-abc-123"
aspirations-update-goal.sh <goal-id> outcome_signal_source "ci:workflow/deploy.yml"
```

Aspiration-level `internal_infrastructure` is set via
`aspirations-update.sh` with the full aspiration JSON.

## Tranche integration plan

These fields are **documented and writeable** as of Tranche A. The scorer
dimensions that read them (`class_balance_bonus`, `user_signal_boost`) land
in Tranche C. Writing these fields before Tranche C is safe — they are
preserved by the script layer but not yet consumed.
