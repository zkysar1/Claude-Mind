---
name: prime
description: "Primes the agent's active context by loading accumulated knowledge: self.md identity, world/program.md shared purpose, all active guardrails, universal + active reasoning-bank entries, category-specific tree nodes (top in-progress and HIGH-priority pending goal categories), recent coordination-board messages, and cross-agent musings. Use whenever a session starts in any mode, when /boot hands off to the aspirations loop, or when switching agents — without priming, the agent answers domain questions from amnesia. Internal sub-skill."
user-invocable: false
triggers:
  - "/prime"
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [tree-retrieval, reasoning-guardrails, pattern-signatures]
minimum_mode: internal
revision_id: "skill-bootstrap-prime-eaa5b6"
previous_revision_id: null
---

# /prime — Context Priming Engine

Loads the agent's accumulated knowledge into active context so that conversations
and goal execution start with domain awareness rather than amnesia.

**Internal skill**: called by boot (RUNNING state) and session start protocol (any mode).
Not user-invocable — users enter persona to prime automatically.

**Key design**: Boot loads the MAP (indexes, summaries). Prime loads the TERRITORY
(actual tree node content, reasoning bank entries, guardrail details). Together they
give the agent full domain awareness.

## Sub-commands

```
/prime                    — Auto-detect context and prime broadly
/prime --category <cat>   — Prime a single category at deep depth
```

## Phase 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Phase 0.5: Agent Mode Detection

```
# Capture the state check's output FIRST so the IF clause below
# unambiguously refers to it (G3, 2026-05-20). The validate-paths.sh
# call between this and the IF emits multi-line "L3 PATH VALIDATOR"
# text — without naming the variable, a future editor could misread
# the IF as checking validate-paths' output.
Bash: state=$(bash core/scripts/session-state-get.sh); echo "state=$state"
# The trailing echo (H1, 2026-05-20) makes the captured value visible to the
# LLM in the Bash tool's stdout. Without it, the assignment writes to a bash
# subshell variable that dies when bash exits — zero stdout — and the IF
# clause below has no value to compare. Sibling skills use bare `var=$(cmd)`
# as pseudocode convention; /prime's NO_AGENT branch is critical enough to
# warrant explicit echo.

# L3 PATH DEFENSE (g-115-35) — verify local-paths.conf points at paths that
# exist and are writable BEFORE any tree/meta read or write. Fail-open:
# prints WARN on mismatch but never blocks loop entry. Enforcement belongs
# to L1 (write-time hook) and L2 (permission gate).
Bash: validate-paths.sh

IF state == "NO_AGENT":
  → World-only priming mode. Skip all agent-specific steps.
  → Bash: world-cat.sh program.md  # The Program — shared purpose
  → Bash: world-cat.sh knowledge/tree/_tree.yaml  # collective knowledge overview
  → Bash: guardrails-read.sh --active (shared safety rules)
  → Bash: reasoning-bank-read.sh --active (shared lessons)
  → Display:
    ═══ WORLD PRIME (no agent) ═══
    PROGRAM: [contents of world/program.md]
    KNOWLEDGE: [tree summary]
    GUARDRAILS: [count] active
    REASONING: [count] active
    ════════════════════════════════
  → Output: "Primed in world-only mode. No agent identity active."
  → DONE (skip all remaining phases)
```

## Phase 1: Detect Context & Build Category List

```
1. Reuse the `state` variable captured in Phase 0.5 (G3, 2026-05-20 —
   was a duplicate `session-state-get.sh` call; the value cannot change
   between Phase 0.5 and here, so re-reading was wasted work):
   - UNINITIALIZED: output "Nothing to prime — run /start first." → STOP
   - IDLE or RUNNING: PROCEED

2. IF --category <cat> argument provided:
     Set categories = [{name: <cat>, depth: "deep"}]
     SKIP to Phase 2

3. Read agents/<agent>/self.md → extract domain identity (for display in Phase 4)
   IF missing: self_summary = "Not configured"

4. Read agents/<agent>/profile.yaml → check focus field
   IF focus is set and non-null: add focus domain as Tier 1 category

5. Determine categories from aspirations and pipeline:
   Bash: load-aspirations-compact.sh → IF path returned: Read it
     (compact data has IDs, titles, statuses, priorities, categories — no descriptions/verification)
     Extract unique goal categories:
     - In-progress goal categories → Tier 1 (depth: medium)
     - HIGH priority pending goal categories → Tier 2 (depth: shallow)
     - Remaining goal categories → Tier 3 (skip)
   Bash: pipeline-read.sh --stage active → extract active hypothesis categories
     - Active hypothesis categories → Tier 2 (depth: shallow)

6. Deduplicate: if a category appears in multiple tiers, use the highest tier

7. Apply budget caps:
   - IDLE broad: max 3 categories at medium depth
   - RUNNING full: max 3 categories at medium + 2 at shallow
   - Targeted (--category): 1 category at deep (no cap)
```

## Phase 2: Load Domain-Agnostic Stores (Always)

These are small, always relevant, and not category-specific. Load unconditionally.

```
1. Read agents/<agent>/self.md → full content
   Display:
   ═══ SELF ══════════════════════════════════════
   {agents/<agent>/self.md body content after YAML front matter}

2. Bash: world-cat.sh program.md  # full content (if non-empty)
   IF non-empty:
     Display:
     ═══ THE PROGRAM ════════════════════════════════
     {world/program.md content}
   IF empty or missing: skip silently

3. Bash: guardrails-read.sh --active → ALL active guardrails
   IF count > 30: note overflow but still load all (guardrails are safety-critical)

4. Reasoning bank — load universal meta-lessons first, then all active entries.
   The universal layer guarantees cross-domain lessons (schema-probe-first,
   canonical-probe, cadence-observability) surface during priming regardless
   of the agent's current focus. See `memory-pipeline.yaml` → `reasoning_bank_routing`.
   - Bash: reasoning-bank-read.sh --universal → framework-* + applies_to in {any, framework} entries,
     sorted by utilization_score desc. Display count + top 5 titles.
   - Bash: reasoning-bank-read.sh --active → ALL active reasoning bank entries (superset).
     IF count > 30: note overflow but still load all

5. Bash: world-cat.sh knowledge/beliefs.yaml  # filter status in (active, weakened)
   IF file missing: beliefs = [] (skip silently)

5.4. Bash: `[ -f agents/<agent>/session/recovery-notice ] && cat agents/<agent>/session/recovery-notice && rm -f agents/<agent>/session/recovery-notice || true`
   → Crashed-runner auto-recovery notice written by `recovery-gate.sh` on
     SessionStart when ALL of: state=RUNNING, heartbeat stale, runner PID dead,
     no stop-requested, no active background job. The gate cleared session
     state to IDLE and recorded the cause; surface that cause in the PRIMED
     output (Phase 4) so the user knows what happened, then delete the file
     so subsequent /prime calls don't re-surface it.
   IF file missing: skip silently. (Stash captured contents for Phase 4.)

5.45. Bash: team-state-sync-blockers.sh
   → Drift guard for critical_blockers snapshot. Reads each
     critical_blockers[].goal_id and removes entries whose goal status
     (resolved across world + agent, live + archive aspiration files) is
     one of {completed, archived, skipped, expired}. Idempotent. Prevents
     both agents from inheriting a stale blocker list on prime and spinning
     up false "waiting on user" narratives.
     Fail-open: missing team-state.yaml skips silently.

5.5. Bash: team-state-read.sh --json
   → Live cross-agent snapshot. Stash partner.in_flight (goal_id, title,
     claimed_at, phase) and partner.last_active for Phase 4 display.
     The board claims (step 6 below) are the audit trail; this is the
     live snapshot — see coordination.md "Single source of truth".
   IF file missing: skip silently

5.55. Bash: bash core/scripts/mirror-health.sh
   → Own-cloud mirror-wedge visibility (g-115-2549, display-only). Exit 0
     (healthy): stash nothing. Exit 1 (WEDGED): stash the printed file list
     and surface a warning line in Phase 4 output — this box is serving
     stale world reads for those files until the g-115-2548
     /reconcile-owncloud-conflicts repair runs (the watchdog MirrorWedgeProbe
     files the Investigate goal; /prime only displays). Exit 2 (unknown —
     sweep not running / not own-cloud): stash the one-line reason, display
     at most one dim note. Advisory: never block priming on any exit code.

5.5a. Bash: board-read.sh --channel coordination --type encoding --since 30m --json --mark-read
   → E11: Cross-agent pending encodings. Output is JSONL (one object per
     line), each carrying keys: {id, author, timestamp, channel, type,
     text, reply_to, tags}. Filter further to author != current agent.
     `--mark-read` (g-304-03) appends a row to
     `world/board/coordination-reads.jsonl` for each displayed msg so future
     "did this agent see post X?" telemetry has signal.
     Stash:
       pending_encodings = [
         {
           author:        msg.author,
           node_key:      msg.tags[0]  (first tag — convention is target node),
           minutes_ago:   (now - msg.timestamp) in minutes,
           text:          msg.text
         }
         for each matching message
       ]
     Time window matches T23's coordination check (30m) — pending after
     that is staler than the deferral logic considers and shouldn't
     block prime. Phase 4 displays these so a fresh session knows not
     to immediately encode the same target.
   IF empty: stash pending_encodings = []. Phase 4 omits the section.

5.5b. Bash: bash core/scripts/insight-trigger-sweep.sh --dry-run --json
   → Surface findings-channel insight_triggers (posts tagged
     `requires_action_by:<agent>` + `action_type:<verb>`) that have aged
     past the 1h grace window but not yet converted to goals. The
     recurring sweep g-115-754 at 1h cadence handles conversion; this is
     the visibility surface for "what will fire next sweep tick".
     Read-only invocation — `--dry-run` never writes.
     Stash for Phase 4:
       pending_triggers = parse `pending` array from JSON output, sort by
       severity (invalidates > constrains > enables > informs), top 3.
       Each entry kept as {author, target, action, severity, msg_id, age_h}.
   IF empty: stash pending_triggers = []. Phase 4 omits the section.

6. Bash: board-read.sh --channel coordination --since 2h --json --mark-read
   → Recent coordination messages from other agents (what they're working on).
     `--mark-read` (g-304-03) records this agent saw each displayed coord post.
   IF no messages or board not initialized: skip silently
   Parse typed messages to build structured cross-agent state:
     - type=claim → "{author} is working on {goal_id from tags}"
     - type=complete → "{author} completed {goal_id from tags}"
     - type=handoff → "{author} completed {goal_id}: {text}" (factual output — high value)
     - type=blocked → "{author} is blocked: {text}"
     - type=encoding → "{author} encoding {node_path from tags}"
     - type=release → "{author} released {goal_id}: {text}" (includes failure reason)
     - Untyped/status → display as-is (backward-compatible)

7. Bash: board-read.sh --channel general --since 24h --tag forge --mark-read
   → Recent skill forge announcements from other agents.
     `--mark-read` (g-304-03) records this agent saw each displayed forge post.
   IF no messages or board not initialized: skip silently

8. Bash: board-read.sh --channel reasoning --since 24h --json --mark-read
   → Recent casual reasoning musings from all agents (cross-agent shared notebook).
     `--mark-read` (g-304-03) records this agent saw each displayed musing.
   IF no messages or channel not initialized: skip silently
   Parse entries and render in Phase 4 under "Recent musings (cross-agent)".
   Format per entry: `[HH:MM] <author>: <text>   [tags]`
   Principle: read-always, write-voluntary. See board.md "Casual Reasoning Channel".

9. Bash: wm-read.sh loop_state
   → Observability surface for the anti-drift counters mutated in aspirations
     loop Phase 4.1. See `core/config/aspirations-loop-digest.md` §Signal Mutation
     Blocks A/C: routine_streaks[goal_id] auto-flips outcome_class=deep at 5;
     session_signals.routine_streak_global auto-flips at 8.
   Stash for Phase 4:
     - global = loop_state.session_signals.routine_streak_global  (int, default 0)
     - per_goal = loop_state.routine_streaks                       (dict, default {})
     - total_routine = loop_state.session_signals.routine_count_total (int, default 0)
     - goals_completed = loop_state.goals_completed_this_session   (list, default [])
   IF wm-read returns "null" (no prior loop_state — IDLE session, fresh start, or
   first iteration): stash all zeros. Phase 4 will omit the Boredom line.

10. Bash: insights-read.sh --count
    Bash: insights-read.sh               # stdout is JSON array of unprocessed entries
    → Surface the agent's own unconsumed reasoning notes captured by
      `capture-insights.py` during goal execution / spark checks. Without this
      read they pile up indefinitely — 225 entries at time of writing, written
      but never loaded. Read-only: prime MUST NOT mark them processed (that's
      curation work for `/felt-sense-checkin` or explicit review).
    Parse:
      - unprocessed_count = int from `--count` (0 if empty)
      - entries           = JSON array from bare invocation (may be [])
    # insights-read.sh returns entries in FILE ORDER (append order, oldest
    # first). LLM MUST sort by timestamp desc before slicing — else Phase 4
    # surfaces the OLDEST 5 (worst signal for "what did I just notice").
    Stash for Phase 4:
      - insights_count = unprocessed_count
      - insights_latest = sort entries by `timestamp` desc, take first 5, each kept
        as {ts: entry.timestamp[:16].replace('T',' '), snippet: entry.content
        first 140 chars, single-lined, "…" suffix if truncated}
    IF unprocessed_count == 0 OR entries is empty: Phase 4 omits the insights block.
    IF count >= 50: display in Phase 4 gets a curation-debt suffix
      (" — consider /felt-sense-checkin to curate") to surface the backlog pressure.
```

## Phase 3: Load Category-Specific Knowledge

For each category from Phase 1 (in tier order, respecting budget):

```
1. Bash: session-mode-get.sh → if "reader", add --read-only flag below
   Bash: retrieve.sh --category {cat} --depth {tier_depth} [--read-only]
   → Returns JSON with: tree_nodes, reasoning_bank, guardrails,
     pattern_signatures, experiences, beliefs, experiential_index

   In reader mode: --read-only suppresses counter writes (side-effect-free).
   In assistant/autonomous: counters increment normally — primed knowledge
   IS retrieved knowledge, the spaced repetition signal is accurate.

2. From the result, extract and display:
   - Tree nodes loaded (count + capability levels)
   - Pattern signatures matched (count)
   - Experiences matched (count)

3. IF no categories were identified (empty list):
   Output: "No category-specific context to load."
   (Domain-agnostic stores from Phase 2 are still loaded)
```

## Phase 4: Output Priming Summary

```
═══ PRIMED ════════════════════════════════════
{IF recovery-notice was present in step 5.4, render this line FIRST inside the
 PRIMED block (above Self) so the user sees crash recovery before anything else):
"⚠ Recovered: {recovery-notice contents (one line)}"}
Self: {one-line Self summary from agents/<agent>/self.md}
Program: {one-line summary from world/program.md, or "not set"}
Focus: {focus directive from agents/<agent>/profile.yaml, or "none set"}
State: {IDLE | RUNNING}
Domains loaded:
  - {category}: {N} nodes at {depth}, capability: {level}
  - {category}: {N} nodes at {depth}, capability: {level}
Guardrails: {count} active
Reasoning: {count} entries
Patterns: {count} signatures
Beliefs: {count} active
{Boredom line — OMIT entire line when global == 0 AND per_goal has no non-zero entries
 (clean streak state, IDLE session, or first iteration). Otherwise render:
   top3 = top 3 per_goal entries by value desc where value > 0, joined as "{gid}={n}"
   ratio = f"{total_routine}/{len(goals_completed)} routine" if goals_completed else f"{total_routine} routine"
   IF global >= 4:
     "Boredom: ⚠ routine_streak_global={global} (auto-deep at 5) | per-goal: {top3 or 'none'} | session: {ratio} — pattern-matching risk"
   ELIF global > 0 OR top3:
     "Boredom: routine_streak_global={global} (auto-deep at 5) | per-goal: {top3 or 'none'} | session: {ratio}"
}
Partner ({partner-name}): {if in_flight: "in_flight {goal_id} '{title[:40]}' phase={phase} ({Nm/h} ago)" else: "no in_flight"} | last_active {Nm/h ago}
{Pending encodings (cross-agent) — OMIT entire block when pending_encodings is empty.
 Otherwise render:
   "Pending encodings (cross-agent, 30m):"
   one line per entry:
     "  {author} → {node_key} ({minutes_ago}m ago)"
 E11: signals that a partner recently posted encoding intent. Treat as a
 soft hint to avoid encoding to the same node this iteration — T23's own
 coordination check at write-time is the hard gate.}
{Pending insight triggers (cross-agent) — OMIT block when pending_triggers is empty.
 Otherwise render:
   "Pending insight triggers (cross-agent, next sweep tick):"
   one line per entry (top 3, severity-sorted):
     "  {author} → {target} {action} [{severity}] ({age_h}h ago, {msg_id})"
 Recurring goal g-115-754 (1h cadence) converts these on the next tick.
 Display is informational — agent need not act preemptively.}
Recent musings (cross-agent, 24h):
  [HH:MM] {author}: {text}   [{tags}]
  ... (up to last ~10 from world/board/reasoning.jsonl, omit block if empty)
{Recent insights block — OMIT entire block when insights_count == 0 OR insights_latest is empty.
 Otherwise render:
   "Recent insights (self, {insights_count} unprocessed{curation_suffix}):"
   where curation_suffix = " — consider /felt-sense-checkin to curate" when insights_count >= 50, else ""
   then one line per entry in insights_latest (top 5, newest first):
     "  [{ts}] {snippet}"
 Principle: prime is a read surface only — never flip processed=true here.
 Curation belongs to /felt-sense-checkin (cadence 75) or explicit user/agent action.}
═══════════════════════════════════════════════

IF IDLE state:
  "Context loaded. Ask me anything about {comma-separated domain list}."

IF RUNNING state:
  (no additional output — boot continues to next step)

IF no world/ data exists (fresh install, no aspirations):
  "Primed with empty state. Run /start to begin building knowledge."
Bash: echo "prime phase documented"
```

## Invocation Rules

- Does NOT require a session snapshot — reads data stores directly via scripts
- Does NOT modify agent-state, working-memory, handoff, or any state files
- When called from boot: runs after Step 2.5 (snapshot exists for navigation)
- For auto-continuation, boot passes `--category {goal_category}`

## Chaining

- **Called by**: `/boot` (Step 2.7 full, Step 8.5 continuation), session start protocol (reader/assistant modes)
- **Calls**: `retrieve.sh`, `guardrails-read.sh`, `reasoning-bank-read.sh`, `aspirations-read.sh` (read-only), `pipeline-read.sh` (read-only), `wm-read.sh` (loop_state observability — read-only), `insights-read.sh` (unprocessed self-insights — read-only, never marks processed)
- **Does NOT call**: `/boot`, `/aspirations`, `/respond`, or any other skill
- **Does NOT modify**: agent-state, working-memory, handoff, or any state files

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `retrieve.sh` or `board-read.sh` call. When /prime
is called from boot during RUNNING state, never end with a text summary — control
returns to boot which then hands off to /aspirations.
