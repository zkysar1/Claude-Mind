# Goal Scoring Script Access

Goal selection scoring is implemented by `core/scripts/goal-selector.py` with exploration noise.
The script computes 16 deterministic criteria plus 1 stochastic criterion (`exploration_noise`)
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
  preconditions:   # What must be true before execution (checked in COLLECT)
    - "Root domain node exists (from g-001-00 or bootstrap)"

# LEGACY format (still accepted, auto-mapped):
desiredEndState: "At least one L2 node exists..."   # → verification.outcomes[0]
completion_check:                                    # → verification.checks[0]
  type: file_check
  target: world/knowledge/tree/_tree.yaml
  condition: "Has at least one L2 node registered"
```

`verification.outcomes` = what success looks like (for spark checks, aspiration assessment).
`verification.checks` = how to verify it (for Phase 5 completion, Phase 0 auto-detect).
`verification.preconditions` = what must be true before execution (checked in Phase 2 COLLECT).
`verification.verification_hint` = advisory text suggesting what machine checks to consider when
creating a goal. Present on templates with `checks: []`. Read during goal creation to prompt
the agent to populate `checks` with concrete machine-verifiable conditions. Not enforced —
Phase 5 Verification Escalation handles empty-checks goals structurally.

---

# Recurring Goal Fields

Goals that re-fire on a schedule use these fields:

- `recurring`: `true`/`false` — whether the goal repeats. Set to `false` to permanently stop.
- `interval_hours`: positive number — hours between executions (e.g., 0.25, 4, 8, 24). Default: 24.
- `remind_days`: DEPRECATED — converted to `interval_hours * 24` for backward compatibility. Use `interval_hours` for new goals.
- `lastAchievedAt`: `YYYY-MM-DDTHH:MM:SS` — full ISO 8601 timestamp of last completion. Legacy `YYYY-MM-DD` format is accepted (assumes start of day).
- `achievedCount`: integer — total times completed
- `currentStreak`: integer — consecutive on-time completions. Resets to 1 when `hours_since(lastAchievedAt) > 2 * interval_hours` at completion time (missed interval). First completion always starts at 1.
- `longestStreak`: integer — best streak ever

To permanently stop a recurring goal: set `recurring: false` via `aspirations-update-goal.sh <goal-id> recurring false`.

Phase 0 Recurring Goal Checks resets completed recurring goals to `pending` after `interval_hours` elapses. Phase 7 skips "aspiration fully complete" for aspirations where ALL goals are recurring (perpetual aspirations).

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
- Set via: `aspirations-update-goal.sh <goal-id> abstained_by <agent_name>` + `abstained_at <timestamp>`
- See `aspirations-select/SKILL.md` Phase 2.55 for the abstention check protocol
