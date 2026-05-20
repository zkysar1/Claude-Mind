# Encoding Protocol — Shared Digest (E16)

Single source of truth for the encoding mechanics shared between the two
encoding lanes:

- `.claude/skills/aspirations-state-update/SKILL.md` Step 8 — autonomous
  deep-outcome encoding (Phase 8 of the perpetual loop).
- `.claude/skills/encode-session/SKILL.md` Lane 1 — chat-mode encoding,
  invoked manually in assistant mode.

Both lanes write to the same stores via the same scripts (`tree-update.sh`,
`reasoning-bank-add.sh`, `guardrails-add.sh`, `pattern-signatures-add.sh`,
`experience-add.sh`, `decision-rules-append.sh`). The gate math, dedup
contracts, and propagation behavior are identical. The differences are
SCALE (Phase 8 emits a full precision manifest; chat-mode collapses to a
single Key Insight paragraph) and TIMING (Phase 8 runs per-goal; chat-mode
runs per-invocation), NOT mechanics.

When changing encoding behavior, edit this digest AND both consuming
skills together. Drift between the two skills is the failure mode this
digest exists to prevent.

## Section A — Encoding-Gate Scoring

Both lanes apply the same gate before writing. The formula SHAPE is:

```
encoding_score = (novelty             * w_novelty)
               + (outcome_impact      * w_outcome_impact)
               + (surprise            * w_surprise)
               + (goal_relevance      * w_goal_relevance)
               + (repetition_strength * w_repetition_strength)
```

All weights AND thresholds live in `core/config/memory-pipeline.yaml`
(`encoding_gate.score_formula`, `encode_threshold`, `skip_threshold`,
`review_range`). Read at call time — do NOT hardcode the numbers here or
in the consuming skills. Single source of truth.

Category-class multiplier (`memory-pipeline.yaml::category_class_multiplier`)
is applied AFTER precision bonus, BEFORE threshold compare. Lookup:
`goal.category → _tree.yaml node.domain_class`. Unknown category → 1.00.

Score components (0-1 each, scaled to threshold):
- novelty:        how new is this concept vs. existing tree coverage
- outcome_impact: did this change material behavior or just refine a label
- surprise:       gap between predicted and actual (use surprise_level/10
                  if a hypothesis is involved)
- goal_relevance: how directly does this serve the active goal/category
- repetition_strength: how many times has this signal recurred recently

## Section B — Curator Quality Gate

After the encoding score passes, the curator gate filters by quality.
Formula SHAPE:

```
curator_score = (coverage      * w_coverage)
              + (specificity   * w_specificity)
              + (actionability * w_actionability)
```

Investigation-aware reweighting applies when goal title starts with
Investigate/Research/Audit/Analyze/Diagnose/Trace/Review, OR
goal.category is in {"analysis", "diagnosis", "research"} — the
investigation weight set replaces the standard set.

All weights AND the pass_threshold live in
`core/config/memory-pipeline.yaml::curator_gate`. Read at call time.

Failure (curator_score < pass_threshold): write the candidate to
`curator_overflow` WM slot — consolidation re-considers it at session
end with full context.

## Section C — Tree Write Steps

```
1. PRECISION extract — scan execution/session content for VERBATIM values.
   Build manifest items: {type, label, value, unit, context}. Types:
   threshold, formula, constant, reference, measurement, config_value.
   Schema + heuristics: core/config/conventions/precision-encoding.md.

2. PRECISION compose — render manifest into Verified Values entries:
       - **{label}**: `{value}` {unit} — {context}

3. NARRATIVE compose — single paragraph of Key Insight from the encoded
   content. Avoid LLM-self-congratulation; describe WHAT the agent now
   knows, not HOW it figured it out.

4. CURATOR gate (Section B) — score the candidate. Below threshold,
   demote to overflow; do NOT proceed to step 5.

5. PRECISION audit — re-read the destination node. Verify each manifest
   item appears in Verified Values (no silent omissions during the
   compose step).

6. TREE write — Edit the node file. Update front matter:
       last_updated:         <today YYYY-MM-DD>
       last_update_trigger:  <"phase-8" | "encode-session" | other>

   The PostToolUse hook (T21 in encoding-triggers.md, via
   `tree-front-matter-sync.py`) atomically mirrors `.md` last_updated into
   BOTH `_tree.yaml::nodes[key].last_updated` AND the top-level
   `_tree.yaml::last_updated` on every Edit/Write of a tree node file.
   This is the single canonical sync — DO NOT add an explicit
   `tree-update.sh --set <key> last_updated <today>` register call after
   the Edit. Historical drift had both consuming skills doing that triple-
   write (Edit → T21 → explicit set); collapsed 2026-05-12 to single
   source of truth.

7. DECISION RULES — if a clear IF-THEN rule emerged from execution:
       echo '{"if": "<observable condition>", "then": "<specific action>"}' \
         | bash core/scripts/decision-rules-append.sh \
             --goal <goal-id-or-session-marker> \
             --node-path <node-md-path>
   Empty stdin is legitimate ("no rule emerged this pass") — bumps the
   staleness marker without writing.

8. CAPABILITY recalc — if the node's capability_level may have changed
   (new threshold crossed), run propagation:
       bash core/scripts/tree-propagate.sh <node.key>
   Emit a CAPABILITY UNLOCK log line when the level rises.
```

## Section D — Cross-Agent Coordination (T23)

Before writing to a tree node, check for in-flight encoding by another
agent:

```
Bash: board-read.sh --channel coordination --type encoding --since 30m --json
IF any message has tag matching node.key AND author != current agent:
    DEFER — queue to WM encoding_queue, skip the immediate write
ELSE:
    Bash: echo "Encoding: <node.key>" | board-post.sh --channel coordination \
        --type encoding --tags <node.key>
    PROCEED with steps 6-9 above
```

Consumer side: `/prime` Phase 2 step 5.5a (E11) surfaces pending encodings
to the OTHER agent so they don't queue a competing edit.

## Section E — Knowledge-Debt Consumption

Both lanes are consumers for `knowledge_debt` entries filed by:
- `/respond` Step 4.5 (E1) — tier-escalation debt
- `/respond` Step 6.5 (E2) — post-edit reconciliation debt
- `/respond` Step 7.5e (E3) — tool-result-surprise dual-write
- `/respond` Step 7.5f (E6) — review-finding debt
- aspirations-execute Phase 4.5 (E7) — probe-outcome surprise dual-write

Auto-resolve on read: if `node.last_updated >= debt.created` (date-only,
[:10] slice on both), the encoding pass that landed already covered the
debt — mark `resolved=true`. Filter resolved entries when writing back.

Cadence-driven inline resolution: HIGH-priority OR sessions_deferred >= 5
debts are tackled inline in the current pass (Lane 1.6 / Phase 8 second
pass). Lower-priority entries carry forward via `sessions_deferred + 1`,
dropped at 10 (configurable).

## Section F — Chunked Encoding (E13)

Long executions (>4000 chars output OR >30 min wall-clock) may contain
multiple distinct learnings. The chunk schema lets both lanes emit N
encoding payloads per single execution instead of bundling everything
into one Key Insight paragraph.

Chunk schema (appended to WM sensory_buffer):

```json
{
  "source_goal": "<goal_id-or-session-marker>",
  "chunk_idx": <0-based>,
  "chunk_total": <N>,
  "chunk_text": "<the segment's text, ≤2000 chars>",
  "content_type": "<finding | decision | code-change | observation>",
  "scores": {
    "novelty": <0-1>,
    "outcome_impact": <0-1>,
    "surprise": <0-1>,
    "goal_relevance": <0-1>,
    "repetition_strength": <0-1>
  },
  "target_article": "<node-key or null>",
  "replay_priority": "<replay-priority>"
}
```

Producer (aspirations-execute Phase 4.05 long-execution branch — or
encode-session Lane 1 when chat span exceeds threshold):

```
IF result_size_chars > 4000 OR phase_4_duration_sec > 1800:
    Segment result by natural boundaries (### headings, distinct tool-output
    blocks, paragraph breaks). Cap at 5 chunks.
    For each chunk:
        Score it independently per Section A's component definitions.
        Append to WM sensory_buffer using the chunk schema above.
```

Consumer (Phase 8 / encode-session Lane 1, on reading sensory_buffer):

```
For each chunk tagged with the current goal/session:
    Run Section A gate against chunk.scores
    IF passes: build a per-chunk precision payload, run Section B + C
               steps independently
    IF fails: drop (overflow queue handles re-consideration)

# Net effect: high-scoring chunks land as distinct tree updates; low-
# scoring chunks drop. The summarized bundle is replaced by N independent
# encoding decisions, each evaluated on its own merit.
```

## Section G — Mode + Trigger Coverage

| Lane | Trigger | Mode | Frequency |
|---|---|---|---|
| Phase 8 (T1) | Every deep-outcome goal completion | Au | Per-goal |
| Phase 8r (T2) | `achievedCount % 5 == 0` on recurring goal | Au | Per-5-routines |
| Consolidation (T4-T8) | Session end | Au | Per-session |
| /encode-session (T10) | User invocation | A, Au | On demand |
| /encode-session Lane 1.6 (E17 / T17) | Inside /encode-session | A, Au | On demand |

The two skills' DIFFERENT cadence is intentional: autonomous mode encodes
continuously inside the loop, chat-mode encodes on user invocation. Both
follow this digest's Sections A-F identically.

## Cross-references

- `core/config/memory-pipeline.yaml` — gate config (single source of truth
  for numeric thresholds; this digest cross-references but does not
  duplicate the numbers)
- `core/config/conventions/encoding-triggers.md` — trigger catalog
  (Txx active, Exx gaps)
- `core/config/conventions/precision-encoding.md` — precision manifest
  schema + extraction heuristics
- `core/config/conventions/learning-routing.md` — which store does this
  learning go to (T vs R vs G vs P vs E)
- `core/config/conventions/decision-rules.md` — Decision Rules format,
  dedup, and the staleness marker
