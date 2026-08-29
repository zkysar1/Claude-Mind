---
name: prime
description: "Primes the agent's active context by loading accumulated knowledge: self.md identity, world/program.md shared purpose, a bounded 100%-coverage guardrail index (expanded in full on demand), a bounded recency slice of the reasoning bank, category-specific tree nodes (top in-progress and HIGH-priority pending goal categories), recent coordination-board messages, and cross-agent musings. Use whenever a session starts in any mode, when /boot hands off to the aspirations loop, or when switching agents — without priming, the agent answers domain questions from amnesia. Internal sub-skill."
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

# LANE-PIN REVIEW SURFACE (g-115-5901) — sibling of the L3 defense above: a
# fail-open startup advisory that prints and never blocks. A lane pin is a
# durable USER-DIRECTED restriction on this agent's entire work surface; its
# `review_by` column says when a human should LOOK at it again, not when it
# stops binding. Past that date this prints "still enforced — confirm or
# retire" and keeps printing every startup until a human edits the registry row.
#
# The pin is NOT weakened by a lapsed date and the claim gate never consults
# this — a hard expiry would silently hand the agent back a surface the user
# deliberately took away. Before this existed the `expires` column was parsed
# into a field NO caller anywhere in the tree ever read, so a pin could sit
# unexamined forever and the dead field terminated the audit that would have
# found it. Silent when no pin covers this agent, or when none is past due.
Bash: lane-pin-review.sh

IF state == "NO_AGENT":
  → World-only priming mode. Skip all agent-specific steps.
  → Bash: world-cat.sh program.md  # The Program — shared purpose
  → Bash: world-cat.sh knowledge/tree/_tree.yaml  # collective knowledge overview
  → Bash: guardrail-manifest.sh (shared safety rules — id manifest, 100%
    coverage; same budget + expansion rules as Phase 2 item 3)
  → Bash: reasoning-bank-read.sh --recent (shared lessons — bounded; see Phase 2 item 4)
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

Self, the Program, and beliefs are small and load unconditionally. The two JSONL
stores — guardrails and the reasoning bank — are not: each loads to an explicit
token budget below, as a bounded index plus on-demand expansion, never in full.

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

# Rationale (WHY bounded, and why not a ranked slice): core/config/rationale/prime-store-load-budget.md

3. Guardrails — bounded INDEX, then expand on demand. Budget ~40k tokens.
   `--active` is not an option: measured 2026-07-27 at 1398 records / 2.66 MB
   (~665k tokens), larger than the whole context window — so the former
   unconditional-load instruction could never have executed.
   - Bash: guardrail-manifest.sh → ids grouped by category, covering 100% of
     active guardrails, prefixed (since 2026-08-20, g-115-6965) by every
     severity=CRITICAL rule IN FULL. Measured 2026-08-19 (zeta, cc-02): id
     manifest **52 KB / 4099 records / ~21k tokens**; ~70 KB / ~28k with the
     CRITICAL section — inside the budget. It replaced `--summary` (468 KB,
     ~3.2x over): that breach was count-driven and per-line compression was
     measured EXHAUSTED, while the safety floor the callers state is id
     coverage, not text coverage (g-115-6703; evidence in the transformer
     docstring). Growth is COUNT-only (~41 records/day ≈ +0.4 KB/day) —
     re-breach needs ~4x today's count. `--summary` remains correct for
     callers that want the truncated text.
   - The id rollup carries NO truncated rule text, so guard-1421 holds by
     construction: nothing is half-shown (CRITICAL entries arrive whole).
   - Before acting inside a guardrail's trigger zone, expand it IN FULL:
     `guardrails-read.sh --id guard-NNN`, or `--category <cat>` for a whole lane.
   - The always-load core is identified by the explicit `severity` marker —
     NEVER by a utilization count (guard-841 / rb-1824: times_active is
     cumulative, so passive always-on rails read healthy-HIGH and a count
     threshold would drop exactly the wrong entries). Canonical case: UPPER
     (g-115-3573). The manifest REALIZES the tier (g-115-6965): every
     severity=CRITICAL rule rides above the rollup whole (15 of 4,198
     active, 2026-08-20; the fetch degrades fail-open to the plain
     manifest). Severity stays ADDITIVE, never a selector — the id manifest
     remains the 100%-coverage floor; never substitute a ranked slice.
   - The admission rule for the CRITICAL always-load tier (both clauses: the
     harm outlives the loop, AND the trigger zone is not self-announcing) is
     `core/config/rationale/prime-store-load-budget.md` → "The CRITICAL
     admission rule". Read it before rating any guardrail's severity; do not
     re-derive a bar per rater.

4. Reasoning bank — bounded recency index; relevance arrives in Phase 3.
   Budget ~9k tokens. Neither `--active` nor `--universal` is an option:
   `--universal` measured 4123 records / 10.8 MB (~2.7M tokens) — 79% of the
   store, so it was never a budget.
   - Bash: reasoning-bank-read.sh --recent → **10 entries, 42 KB (~12k tokens)**.
     RE-MEASURED 2026-08-11 (foxtrot, LAPTOP-3IOFCNEO). This line read "498
     entries, 37 KB (~9k tokens)" until then — off by 50x on the count, and the
     shape is what changed, not just the number: 498 entries in 37 KB is ~76 B
     each (a shallow id+title INDEX), while 10 in 42 KB is ~4.3 KB each (FULL
     bodies). Same token cost, ~98% less recall breadth. Believed intended (the
     bounding work in g-115-3407); the figure simply was not updated with it.
     `--recent N` widens it (verified 3→3, 25→25). A mistyped flag is now
     REFUSED loudly at exit 2 (g-115-4428, fixed 2026-08-20 — formerly
     swallowed rc=0), and a BOUNDED-LOAD stderr warning fires past 8192 B/entry.
   - Category-relevant entries load in Phase 3 via
     `retrieve.sh --category {cat} --depth {tier_depth}` (DEPTH_LIMITS:
     shallow 15 / medium 30 / deep 50) — the existing budgeted path.
   - Expand on demand: `--id`, `--tag`, `--category`.
   - This does NOT preserve the old universal-set guarantee (every framework/any
     lesson present at prime time); that guarantee was unbuyable at ~2.7M tokens.
     Cross-domain lessons now arrive via the recency window plus Phase 3
     category retrieval. See `memory-pipeline.yaml` → `reasoning_bank_routing`.
   - `--universal --summary` does NOT compose: `--universal` wins silently and
     returns the full ~2.7M-token load, not an index (verified 2026-07-27).

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
     recurring sweep g-115-754 at 6h cadence handles conversion; this is
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
     signals.routine_streak_global auto-flips at 5 (`recurring.routine_streak_global_ceiling`,
     core/config/aspirations.yaml — NOT 8, corrected 2026-07-29 against the config + script default).
   Stash for Phase 4:
     # Sub-slot is `signals` on disk, NOT `session_signals` (that is the orchestrator's
     # restored variable name). Reading `session_signals` returns absent → silent all-zeros.
     - global = loop_state.signals.routine_streak_global           (int, default 0)
     - per_goal = loop_state.routine_streaks                       (dict, default {})
     - total_routine = loop_state.signals.routine_count_total      (int, default 0)
     # NOT goals_completed_this_session — that key EXISTS but is an INT counter, so
     # len() on it raises TypeError; goals_completed is an int too. The list is
     # counted_goals_this_session. Measured 2026-07-29.
     - goals_completed = loop_state.counted_goals_this_session     (list, default [])
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

11. Bash: peer-surface.sh
    → THIS WORLD IS NOT ALONE. Peer deployments are registered in
      `core/config/environments/*.yaml`, and one of them (`zds-mind`) has been
      posting to this world's board continuously since 2026-06-02. Every other
      store /prime loads describes this world only, so without this read the
      PRIMED summary is a complete-looking picture of a world the agent has no
      reason to believe has neighbours — and an agent that primes into a
      solitary world never thinks to cross (g-115-3927).
    Emits 2-4 lines: peer count + each peer's storage backend, inbound volume
      over 7d broken down by deployment and channel, and the pointer to
      `peer-board-post.sh` + `core/config/conventions/cross-deployment-channel.md`.
    Cost ~1.5s warm (two bounded board reads). FAIL-OPEN by contract — it exits
      0 on every error path and never gates loop entry.
    Use the emitted lines VERBATIM — do NOT re-derive these counts inline. The
      script exists because all three ways to get this wrong return a plausible
      number instead of an error: `board-read.sh --json` emits JSONL and not an
      array (a `json.load` returns zero rows, which reads as "no peers"); the
      `cross-deployment` tag has ZERO installed base, so filtering by it reports
      an empty channel over real traffic; and "author not in the local roster"
      over-counts, because some local posts carry a goal-title fragment in the
      author field. See `core/scripts/peer_surface.py` for the measurements.
    Stash for Phase 4: peer_lines = the script's stdout lines.
    IF the script emits nothing at all: Phase 4 omits the block.
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
{Peers block (step 11) — render peer_lines VERBATIM, one per line, unindented.
 OMIT the block ONLY when step 11 emitted nothing at all. Do NOT omit it on a
 quiet window: the script deliberately prints "channel is live but quiet this
 window" rather than a zero, because a suppressed line and an absent channel
 are indistinguishable to the reader — and that ambiguity is the whole defect
 this block exists to close. Sits directly under Partner by design: Partner is
 the local fleet, Peers is the same question asked across deployments.
 Typical shape:
   "Peers: 3 registered (claude-mind:local, local:local, zds-mind:local) | self=ayoai-mind:own-cloud"
   "Inbound (7d): 34 posts from zds-mind [coordination 17, findings 17]"
   "Cross via core/scripts/peer-board-post.sh (convention: core/config/conventions/cross-deployment-channel.md)"}
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
