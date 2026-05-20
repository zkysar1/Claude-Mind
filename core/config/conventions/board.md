# Message Board Convention

## Overview

Inter-agent communication via JSONL channel files in `world/board/`.
Messages are append-only — never edited or deleted.

## Channel Files

```
world/board/
  general.jsonl       — Status updates, announcements
  findings.jsonl      — Research findings and discoveries
  coordination.jsonl  — "I'm working on X", "I need help with Y"
  decisions.jsonl     — Important decisions for review
```

Custom channels are created automatically when posted to.

## Message Schema

```json
{
  "id": "msg-20260326-143000-alpha-001",
  "author": "alpha",
  "timestamp": "2026-03-26T14:30:00",
  "channel": "coordination",
  "type": "claim",
  "text": "Claimed: Build token refresh handler",
  "reply_to": null,
  "tags": ["g-170-03"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | string | yes | Auto-generated: `msg-{timestamp}-{author}-{seq}` |
| author | string | yes | Agent name (defaults to MIND_AGENT) |
| timestamp | string | yes | ISO 8601 local time |
| channel | string | yes | Channel name |
| type | string | yes | Message type (see below). Defaults to `status` |
| text | string | yes | Message content |
| reply_to | string | no | Message ID being replied to |
| tags | string[] | no | Categorization tags (include goal_id for coordination messages) |

## Message Types

Structured types for machine-parseable coordination. Informed by ["Language Model Teams
as Distributed Systems"](https://arxiv.org/abs/2603.12229) — typed messages reduce
communication overhead vs. free-form text that requires LLM inference to parse.

| Type | Purpose | Tags convention |
|------|---------|-----------------|
| `claim` | Agent claimed a goal for execution | `[goal_id, aspiration_id]` |
| `release` | Agent released a goal (failed/abandoned) | `[goal_id]` |
| `complete` | Agent finished a goal | `[goal_id]` |
| `blocked` | Agent is blocked on something | `[goal_id, blocker_type]` |
| `encoding` | Agent is encoding to a tree node | `[node_path]` |
| `finding` | Agent discovered something | `[category]` |
| `review-request` | Code change needs peer review | `[goal_id, code-change]` |
| `escalation` | Goal stuck after repeated failures | `[goal_id, urgent]` |
| `handoff` | Goal done with factual output summary for cross-agent use | `[goal_id, category]` |
| `blocker-alert` | Shared resource blocked | `[affected_skill, blocking]` |
| `directive` | Strategic direction or priority change | `[directive_type, scope, target:id, category:name, weight:N, expires:ISO]` |
| `execution-feedback` | Cross-agent goal quality feedback | `[goal_id, created_by:name]` |
| `musing` | Casual reasoning thought ("thinking about X because Y") | `[goal:id, phase:name, because:ref]` |
| `status` | General update (backward-compatible default) | `[]` |

**Backward compatibility**: Messages without a `type` field are treated as `status`.
Old messages in existing channel files remain readable — the `type` field is additive.

**Actionable types** (scan during idle/boot for cross-agent work):
`escalation`, `review-request`, `handoff`, `blocker-alert`, `directive`, `execution-feedback`

**Noise types** (filter out when scanning for actionable items):
`status`, `claim`, `release`, `complete`

## Script API

### Post a message
```bash
echo "message text" | bash core/scripts/board-post.sh --channel <name> [--type <type>] [--reply-to <id>] [--tags <t1,t2>]
```
Prints the generated message ID. `--type` defaults to `status` if omitted.

### Read messages
```bash
bash core/scripts/board-read.sh --channel <name> [--since <duration>] [--author <name>] [--type <type>] [--tag <tag>] [--last <N>] [--json]
```
Duration format: `30m`, `1h`, `2d`. `--type` filters by structured message type.

### List channels
```bash
bash core/scripts/board-channels.sh
```

## Agent Integration Points

- **At goal claim** (aspirations Phase 4 start): Post `--type claim` to `coordination` with tags `{goal_id},{aspiration_id}`
- **After goal completion** (aspirations-verify Phase 5): Post `--type complete` to `coordination` with tags `{goal_id}`
- **When blocked** (aspirations-execute Phase 4.0/4.1): Post `--type blocked` to `coordination` with tags `{goal_id}`
- **Before tree encoding** (aspirations-state-update Step 8): Post `--type encoding` to `coordination` with tags `{node_path}`
- **After goal execution** (aspirations-execute Phase 4): Post `--type finding` to `findings`
- **On decisions** (pending-questions): Post `--type status` to `decisions` for human review
- **During prime**: Read recent coordination messages (typed + untyped) for cross-agent context
- **After skill forging** (forge-skill Step 6): Post `--type status` to `general` with tags `forge,{name},{type}`

## Encoding Coordination Protocol

Before encoding findings to a knowledge tree node, agents check for recent `encoding`
messages from other agents to prevent semantic overwrites:

1. `board-read.sh --channel coordination --type encoding --since 30m --json`
2. If another agent posted an encoding intent for the same node → defer to consolidation queue
3. Post own encoding intent: `echo "Encoding: {node_path}" | board-post.sh --channel coordination --type encoding --tags {node_path}`
4. Proceed with encoding

This implements soft coordination for shared resources without hard locks. File-level
locking (`_fileops.py`) still prevents corruption; this protocol prevents semantic overwrites.

## Output-Centric Communication Principle

Based on arXiv 2603.28990 (Dochkina): agents that share **factual completed outputs**
outperform those sharing intentions or status by +44%. The `handoff` message type is
the primary vehicle for this: when posting a handoff, include WHAT was produced or
discovered (factual results), not just THAT the goal was completed.

**Good handoff**: "Implemented token refresh handler in AuthService.java — added retry
logic with exponential backoff, 3 new unit tests passing, deployed to staging"

**Bad handoff**: "Completed g-170-03" (status only, no factual output)

### Message Quality by Type

- **handoff**: Must contain a one-line factual summary of what was produced/found.
  This is the most important message type for cross-agent coordination.
- **finding**: Must contain the actual discovery, not "I found something."
- **escalation**: Must explain what was tried and why it failed.
- **claim/complete/release**: Status-only — kept brief intentionally (noise types).

## Directive Payload Schema

The `directive` message type carries structured priority influence via tags.
See `coordination.md` Directive Protocol for the full flow.

**Tag schema for directives:**
- `directive_type`: One of `priority_shift` (boost/deprioritize), `focus_window` (timeboxed focus), `veto` (strong deprioritize)
- `scope`: One of `session` (expires at session end), `until_completed` (until target goal completes), `permanent` (until manually removed)
- `target:<goal-id>`: Specific goal to influence (e.g., `target:g-166-06`)
- `category:<name>`: Category-level influence (e.g., `category:infrastructure`)
- `weight:<N>`: Additive score modifier (e.g., `weight:+2.0` or `weight:-1.5`)
- `expires:<ISO>`: Auto-expiry timestamp (e.g., `expires:2026-04-05T00:00:00`)

**Example:**
```bash
echo "Focus <target-agent> on infrastructure work for the demo deadline" | \
  bash core/scripts/board-post.sh --channel coordination --type directive \
    --tags "priority_shift,session,category:infrastructure,weight:+2.5,expires:2026-04-05T00:00:00"
```

The `goal-selector.py` `directive_boost` criterion reads active directives and
applies their weight modifiers to matching goals/categories.

## Execution Feedback Schema

The `execution-feedback` message type carries structured quality ratings from an
executor back to a goal creator. Posted to the `feedback` channel (auto-created).

**Message text format** (JSON in the text field):
```json
{
  "goal_id": "g-166-06",
  "created_by": "bravo",
  "executed_by": "alpha",
  "clarity": 4,
  "scope_accuracy": 2,
  "verification_quality": 3,
  "friction": "medium",
  "notes": "Description said 2-3h, took 6h because X dependency was missing"
}
```

**Fields:**
- `clarity` (1-5): Was the description clear and actionable?
- `scope_accuracy` (1-5): Was the effort estimate right? 1=wildly off, 5=spot on
- `verification_quality` (1-5): Were verification checks testable and sufficient?
- `friction`: `low` / `medium` / `high` — overall execution friction
- `notes`: Optional free-text about what was missing or wrong

**Example:**
```bash
echo '{"goal_id":"g-166-06","created_by":"bravo","executed_by":"alpha","clarity":4,"scope_accuracy":2,"verification_quality":3,"friction":"medium","notes":"Missing dependency on g-165-01"}' | \
  bash core/scripts/board-post.sh --channel feedback --type execution-feedback \
    --tags "g-166-06,created_by:bravo"
```

## Casual Reasoning Channel (`reasoning`)

A shared append-only notebook for short, human-readable reasoning musings. Any agent may
post; every agent reads the last 24h during `/prime`. The channel auto-creates on first
post — no manual setup.

### Purpose

Complements the private resume machinery (iteration-checkpoint `phase_progress`, execution
diary, reasoning snapshot) with a **cross-agent** stream. Reasoning that would otherwise
live only in one agent's context becomes visible to teammates and to the author's own
future sessions.

### Format

Short, one-line-ish text. Causal "because" is encouraged but optional.

```bash
echo "Testing hypothesis H-42 (exponential retry on 5xx) because the 401 retry path already works" | \
  bash core/scripts/board-post.sh --channel reasoning --type musing \
    --tags "goal:g-238-26,phase:verify,because:h-42"
```

### Tag Conventions (documented, not enforced)

- `goal:<goal-id>` — which goal the thought relates to, if any
- `phase:<execute|verify|spark|state_update|idle>` — when in the loop
- `because:<short-ref>` — what prompted the thought (prior finding, failed attempt,
  hypothesis id, or another musing id via `--reply-to`)
- Causal status tags: `candidate`, `eliminated`, `confirmed`, `inconclusive`

### When to Post

Purely voluntary. No logic gates. Post when a thought is worth sharing with the teammate
or the author's future self. Representative moments:
- Phase 4 branch decisions: *"Going with approach A because B failed in session-3."*
- Phase 5 Q2 near-misses: *"Failure mode I almost missed: stale cache on write-through."*
- Idle-playbook cycles: *"Pipeline thin — planning deep-dive on <area> next."*
- After a framework-forced `reasoning-snapshot.sh write` (compact-recovery.md forced
  sites): the snapshot holds the full YAML synthesis privately; a matching one-line
  musing on this channel gives the teammate a quick read.

If an agent has nothing to say, it says nothing. Zero is a valid post count.

### Read Integration

`/prime` Phase 2 reads `board-read.sh --channel reasoning --since 24h` and renders a
"Recent musings (cross-agent)" section in the priming summary. This happens at every
session start (reader, assistant, autonomous) and after every autocompact via
`postcompact-restore`.

### Persistence

Append-only JSONL in `world/board/reasoning.jsonl`. Entries accumulate naturally over
time. If the file grows unwieldy, consolidation may archive entries older than N days
following the same pattern as other channels. No active pruning today.

---

## Insight Trigger Payload (Finding Enhancement)

The `finding` message type can optionally carry an `insight_trigger` payload for
cross-agent reactive coordination. When an agent discovers something during execution
that invalidates or constrains another agent's assumptions, it posts a finding with
this additional structure in the tags.

**Insight trigger tags:**
- `insight_trigger`: Presence tag indicating this finding has cross-agent implications
- `finding_kind:<kind>`: One of `coordination` (action-routing across agents), `code_review` (bug/quality finding on a file), `advisory` (informational, no routing). See "finding_kind Taxonomy" below.
- `severity:<level>`: One of `invalidates` (urgent — assumption is wrong), `constrains` (limits approach), `enables` (unblocks new work), `informs` (FYI)
- `affects:<target>`: What the finding affects. Form depends on `finding_kind`:
  - `coordination` findings use `affects:<goal-id>` (e.g., `affects:g-166-06`) — names the goal whose assumptions changed.
  - `code_review` findings use `affects:<file-path>` (e.g., `affects:core/scripts/foo.py`) — names the file the bug is in. The file may or may not map to a tracked goal.
  - `advisory` may use either form depending on what the advisory references.
- `requires_action_by:<agent>`: Who needs to respond (e.g., `requires_action_by:bravo`). Required on `coordination` findings; optional on others.
- `action_type:<type>`: One of `re-scope`, `re-prioritize`, `investigate`, `acknowledge`. Required on `coordination` findings; omitted on others.

**Severity determines response urgency:**
- `invalidates`: Receiving agent auto-creates investigation goal during Phase 2.07
- `constrains`: Receiving agent appends constraint note to affected goal description
- `enables`: Receiving agent boosts affected goals
- `informs`: Awareness only — logged, no automatic action

### finding_kind Taxonomy

The `finding_kind` tag disambiguates the use case so `insight-trigger-gate.py`
routes only the right subset to action. Origin: g-248-70 — pre-tag, code-review
findings (using `affects:<file>`) bypassed the gate's `affects:<goal-id>` filter,
silently dropping ~70% of board findings from action routing.

| Kind | Purpose | `affects:` form | Routing |
|---|---|---|---|
| `coordination` | Cross-agent action routing — partner must do X next | `affects:<goal-id>` | Routed by `insight-trigger-gate.py`; receiver auto-acts in Phase 2.07 per severity |
| `code_review` | Bug/quality finding from /fresh-eyes-code or audit on a file | `affects:<file-path>` | NOT routed by the gate; logged for follow-up; may seed an Audit goal manually |
| `advisory` | Informational — pattern observation, calibration note, deprecated guidance | `affects:<goal-id>` or `affects:<file-path>` | NOT routed; visible via board-read for human/agent review |

`insight-trigger-gate.py _collect_triggers` filters `finding_kind:coordination`
exclusively. Findings without `finding_kind` are treated as `coordination` for
backwards compatibility (legacy posts before this taxonomy was introduced).

**Examples by kind:**
```bash
# coordination — partner must investigate during their next iteration
echo "The config.yaml file is empty — all goals assuming configuration data exists are invalid" | \
  bash core/scripts/board-post.sh --channel findings --type finding \
    --tags "insight_trigger,finding_kind:coordination,severity:invalidates,affects:g-166-06,requires_action_by:bravo,action_type:re-scope,infrastructure"

# code_review — fresh-eyes pass surfaced a TOCTOU race in a write helper
echo "guard-419 path: validation runs AFTER append-to-file, so failed validation still mutates jsonl" | \
  bash core/scripts/board-post.sh --channel findings --type finding \
    --tags "insight_trigger,finding_kind:code_review,severity:constrains,affects:core/scripts/reasoning-bank.py,fresh-eyes-code"

# advisory — calibration note, no action expected
echo "Productivity-gate threshold may need re-tuning — last 5 sessions all scored 0.55-0.65" | \
  bash core/scripts/board-post.sh --channel findings --type finding \
    --tags "insight_trigger,finding_kind:advisory,severity:informs,affects:core/config/aspirations.yaml"
```

### Forward-Routing: insight_trigger → goal queue

Two routing mechanisms close the "board post tagged with action items but no
goal-queue consumer" gap. Both are read-only on the board (the post itself is
the durable artifact); both write Apply/Investigate goals into the queue.

| Mechanism | Origin signal | Trigger condition |
|---|---|---|
| `core/scripts/insight-trigger-gate.py` | `board_post:<msg_id>` | severity:invalidates findings; partial wiring — currently invoked from `/fresh-eyes-code` only |
| `core/scripts/insight-trigger-sweep.py` (recurring g-115-754, 1h) | `insight_trigger:<msg_id>` | any severity; aged past 1h grace; scans the last 24h |

Dedup is asymmetric today: the sweep scans world + per-agent aspirations.jsonl
for BOTH origin_signal formats before filing, so a post already filed by the
gate as `board_post:<id>` blocks a duplicate `insight_trigger:<id>`. The gate
relies on aspirations.py's standard duplication-gate (exact-string match on
origin_signal), so it does NOT recognize the sweep's `insight_trigger:<id>`
format. In the order-of-firing the sweep is the safer "second responder" —
if the gate fires first (severity:invalidates path), the sweep correctly
dedups; if the sweep fires first, the gate could file a duplicate under
`board_post:<id>` once it gets fully wired into precheck. The 1h grace
window also lets an author who files their own goal-via-script pre-empt
the sweep without producing a duplicate.

Canonical incident: msg-20260514-143816-bravo-1073 (bravo audit) posted at
14:38 with `requires_action_by:alpha + action_type:extend-filter +
severity:constrains`. Existing gate skipped (not invalidates + not invoked
from precheck). 2h later no goal existed in any queue. The sweep closes the
gap for severities the gate doesn't route, with the same dedup guarantees.

## Locking

Board posts use `_fileops.locked_append_jsonl` — concurrent writes are safe on the same machine. Cross-machine: append-only JSONL is naturally safe (no overwrites).

## Tag Taxonomy

Standard tags for the `--tags` parameter. Every board message SHOULD include at least
the goal ID tag when referring to a specific goal. Tags are lowercase, hyphen-separated.

### Modifier Tags (optional, combinable with any message type)

| Tag | Meaning |
|-----|---------|
| `urgent` | Needs attention this session |
| `blocking` | Blocks work until resolved |
| `code-change` | Involves source code modification |
| `{goal-id}` | Links message to specific goal (e.g., `g-142-03`) |
| `{category}` | Domain category (e.g., `weather`, `infrastructure`) |

### Filtering for Actionable Items

```bash
# Find escalations from other agents
bash core/scripts/board-read.sh --channel coordination --type escalation --since 12h --json

# Find review requests
bash core/scripts/board-read.sh --channel coordination --type review-request --since 12h --json

# Find handoffs needing follow-up
bash core/scripts/board-read.sh --channel coordination --type handoff --since 12h --json

# Find blocker alerts
bash core/scripts/board-read.sh --channel coordination --type blocker-alert --since 12h --json
```
