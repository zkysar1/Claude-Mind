# Encoding Triggers

Authoritative catalog of every point where the agent WRITES knowledge into a
durable store: knowledge tree, reasoning bank, guardrails, pattern signatures,
experience archive, working memory, beliefs, journal, resource locators,
pipeline, message board, and spark questions. The single source of truth for
"where does encoding happen, where should it happen, in which mode, and to
which store."

For the WHAT-goes-WHERE routing question (which store fits this learning) see
`core/config/conventions/learning-routing.md`. For the symmetrical READ
surface see `core/config/conventions/retrieval-triggers.md`. For the encoding
gate math (score formula, thresholds, replay priority) see
`core/config/memory-pipeline.yaml`. This file documents the **encoding
trigger surface**: WHO writes, WHEN, in WHICH MODE, and to WHICH STORE.

## Stores (encoding side)

Abbreviations used in the matrix below:

| Code | Store | Path |
|------|-------|------|
| T | Knowledge tree | `world/knowledge/tree/_tree.yaml` + node `.md` files |
| R | Reasoning bank | `world/reasoning-bank.jsonl` |
| G | Guardrails | `world/guardrails.jsonl` |
| P | Pattern signatures | `world/pattern-signatures.jsonl` |
| E | Experience archive | `agents/<agent>/experience.jsonl` + `agents/<agent>/experience/` |
| B | Beliefs | `world/knowledge/beliefs.yaml` |
| Q | Pipeline (hypotheses) | `world/pipeline.jsonl` |
| J | Journal | `agents/<agent>/journal.jsonl` + `agents/<agent>/journal/` |
| L | Resource locators | `world/conventions/*.md` (locator lane) |
| WM | Working memory | `agents/<agent>/session/working-memory.yaml` |
| SK | Spark questions | `meta/spark-questions.jsonl` |
| BD | Message board | `world/board/*.jsonl` |
| SF | Self | `agents/<agent>/self.md` |

The unified encoding lane (Phase 8 + consolidation + `/encode-session`) writes
to T+R+G+P+E in one orchestration. Other stores have dedicated write paths.
WM is a scratchpad consulted by the unified lane at consolidation
(`encoding_queue`, `knowledge_debt`).

## Status legend

| Symbol | Meaning |
|--------|---------|
| ✓ | Fires today |
| ◐ | Partial — fires for some sub-paths, cadences, or conditions only |
| ✗ | Does not fire today |
| → R{N} | Implemented by improvement R{N} in this catalog |

## Mode legend

| Code | Mode | Loop running? |
|------|------|---------------|
| R | Reader (read-only) | No |
| A | Assistant (user-directed) | No |
| Au | Autonomous (perpetual loop) | Yes |

## Active triggers (what fires today)

| ID | Trigger | Status | Stores | Modes | Frequency | Implementation site |
|----|---------|--------|--------|-------|-----------|---------------------|
| T1 | State update — Phase 8 (deep-outcome goal) | ✓ | T + E + WM | Au | Every deep-outcome goal | `.claude/skills/aspirations-state-update/SKILL.md` Step 8 |
| T2 | Routine accumulation — Phase 8r | ✓ | T | Au | Every 5th routine execution of the same recurring goal (`achievedCount % 5 == 0`) | `aspirations-state-update/SKILL.md` Step 8r |
| T3 | Growth-trigger maintain | ✓ | T (structure) | Au | Conditional — when node crosses decompose/split threshold during Phase 8 | `aspirations-state-update/SKILL.md` Step 8 growth-triggers |
| T4 | Consolidation — encoding queue replay | ✓ | T + E | Au | Every session end (deferred items + standard-tier deferred) | `aspirations-consolidate/SKILL.md` Step 2 |
| T5 | Consolidation — knowledge-debt sweep | ✓ | T | Au | Every session end (when debt items exist) | `aspirations-consolidate/SKILL.md` Step 2.25 |
| T6 | Consolidation — wrong-conclusion encoding | ✓ | T | Au | Session end (when wrong conclusions detected) | `aspirations-consolidate/SKILL.md` Step 2.7 |
| T7 | Consolidation — experience distillation | ✓ | T | Au | Session end — both full + lean paths — when ≥3 experiences cluster on a node | `aspirations-consolidate/SKILL.md` Step 2.9 |
| T8 | Consolidation — `/tree maintain` ops | ✓ | T (structure) | Au | Every session end (stop-mode, standard, or backlog) | `aspirations-consolidate/SKILL.md` Step 6 |
| T9 | Precheck — tree-debt gate | ✓ | T (structure) | Au | When `force_tree_maintain` WM slot is set (candidates > 3× debt threshold) | `aspirations-precheck/SKILL.md` tree-debt gate |
| T10 | `/encode-session` Lane 1 | ◐ | T + R + G + P + E | A, Au | Explicit user invocation only — does NOT auto-fire | `.claude/skills/encode-session/SKILL.md` Lane 1 |
| T11 | `/respond` Step 5 — "Remember fact" directive | ✓ | T | A, Au | On user directive | `.claude/skills/respond/SKILL.md` Step 5 |
| T12 | `/respond` Step 6 — user-correction reconciliation | ✓ | T + R (retire) + G (retire) + B (weaken) + P (retire) + E | A, Au | On user correction (with G12/R15 broad re-retrieve) | `respond/SKILL.md` Step 6 |
| T13 | Phase 4.5 — post-execution reconciliation | ◐ | T or WM (knowledge_debt) | Au | After every goal with external_changes detected | `.claude/skills/aspirations-execute/SKILL.md` Phase 4.5 |
| T14 | `/reflect-tree-update` (Shared Tree Update Protocol) | ✓ | T (with upward propagation) | Au | After every reflection that writes to tree | `.claude/skills/reflect-tree-update/SKILL.md` |
| T15 | `/reflect-on-outcome` Step 8 — hypothesis reflection | ✓ | T (category insight) + R | Au | After every hypothesis resolution reflection | `.claude/skills/reflect-on-outcome/SKILL.md` Step 8 |
| T16 | `/reflect-on-self` — strategy compilation | ✓ | T (Decision Rules) + SF | Au | At reflection cadence (every N goals) | `.claude/skills/reflect-on-self/SKILL.md` Steps 3/3.5 |
| T17 | `/reflect-maintain` — active forgetting | ✓ | T (remove-child) | Au | At maintenance cadence | `.claude/skills/reflect-maintain/SKILL.md` Step 1d |
| T18 | `/research-topic` Step 3 — research-derived encoding | ✓ | T (edit or SPROUT) | A, Au | When `/research-topic` executes a knowledge goal | `.claude/skills/research-topic/SKILL.md` Step 3 |
| T19 | `/felt-sense-checkin` Phase 1 + 1b | ◐ | T + R + G + E | A, Au | Every 75 completed goals (cadence) OR user invocation. Loop required for cadence variant. | `.claude/skills/felt-sense-checkin/SKILL.md` Phase 1 + 1b |
| T20 | `/tree` sub-commands (add, edit, set, decompose, distill, maintain, reparent) | ✓ | T | A, Au | On invocation (skill or user) | `.claude/skills/tree/SKILL.md` |
| T21 | PostToolUse hook — mechanical front-matter sync | ✓ | T (mtime + `last_updated`) | R, A, Au | On every Edit/Write of any `knowledge/tree/*.md` file | `core/scripts/tree-sync-check.sh` + `tree-front-matter-sync.py` |
| T22 | Learning gate — Phase 9.5 retroactive encoding | ◐ | T + WM (knowledge_debt) | Au | When Phase 8 was skipped despite a deep-outcome goal | `.claude/skills/aspirations-learning-gate/SKILL.md` Phase 9.5 |
| T23 | Phase 8 coordination deferral | ✓ | WM (encoding_queue) | Au | When another agent recently encoded to the same target node | `aspirations-state-update/SKILL.md` Step 8 coordination |
| T24 | L1 pick logging (S9 — tree taxonomy review) | ✓ | meta/l1-pick-log.jsonl | R, A, Au | On every `tree.py` add-child / batch add-child / reparent — fires AUTOMATICALLY from inside the script. Logs L1 (depth==1 ancestor), target_node, decision_type, optional source/reason. Fail-open: log failure never blocks tree write. | `core/scripts/tree.py` `_log_l1_pick_for_key` (called from `cmd_add_child`, `cmd_batch` add-child branch, `cmd_reparent`). Companion stats: `tree-read.sh --stats --by-l1` (S1). Cadence consumer: `l1-skew-check.py --cadence` from aspirations-precheck Phase 0.5g (50 goals). Briefing consumer: `/fresh-eyes-tree` Phase 2.2 + `l1-emergence-detector.py` (S4/S6/S7). |

Reconciliation / knowledge-debt mechanisms (orchestrators of T1-T23):

| Mechanism | Where | When | What |
|-----------|-------|------|------|
| Knowledge debt creation | Phase 4.5 + `/respond` Step 6 | When reconciliation can't be inline | Append `{node_key, reason, source_goal, priority, created}` to WM `knowledge_debt` |
| Knowledge debt sweep | Consolidation Step 2.25 | Session-end | Sort by priority/age, edit tree nodes; carry forward or drop after 10 sessions |
| Knowledge debt auto-detect | Phase 8 | After tree encoding | If written node matches a debt entry, back-populate `closes_knowledge_debt` |
| Broad re-retrieve (G12/R15) | `/respond` Step 6.1a' | On user correction | Retire contradicting rb/guard/bel/sig entries across all stores |
| Encoding weight adaptation | Consolidation Step 2.6 | Session-end | Adjust `encoding_gate.score_formula` weights using experience utility |

## Gap triggers (what should fire but doesn't)

Each row names where encoding is missing today, the proposed Exx identifier
(stable for cross-reference), and the resolution. Status column shows the
implementation state: ✓=implemented, ✗=open, ◐=partial.

### Assistant-mode gaps (highest practical impact)

| Gap | Status | Trigger | What's missing | Resolution |
|-----|--------|---------|----------------|------------|
| E1 | ✓ R3 | `/respond` post-answer when Step 4 escalated to Tier 2/3 | Tier 2 (codebase) or Tier 3 (web) use is evidence the tree had a gap. Step 7.5 only encoded USER-message INSIGHT/FEEDBACK/HYPOTHESIS/OPS_GOTCHA — not "I learned this answering you." | `.claude/skills/respond/SKILL.md` Step 4.5 — Tier-Escalation Encoding Debt added between Step 4 and Step 4b. Self-classifies tier_used (tier-1/2/3/exhausted); on tier-2/3/exhausted appends a `knowledge_debt` entry to WM with priority by tier (tier-2 MEDIUM, tier-3/exhausted HIGH), `source_goal: respond-step-4.5`, `reason: tier-N-escalation: <topic>`. Reader mode skips. Fail-open: errors log to execution-diary but never block the response. Schema verified — both null and resolved node_key branches pass `_validate_knowledge_debt_entry`. Consumed by `/encode-session` Lane 1.6 (E17) or autonomous Phase 4.5 (T13). |
| E2 | ✓ R4 | `/respond` post-edit reconciliation for user-directed edits | Step 6 fires only on user *corrections of belief*. When user says "update probe-bridge.sh to port 28080" and agent edits it, that's a world-change but no tree reconciliation fires (assistant had no Phase 4.5 analog). | `.claude/skills/respond/SKILL.md` Step 6.5 — Post-Edit Tree Reconciliation added between Step 6 and Step 7. Self-reports edits this turn, filters out ephemeral/tree/board paths, flags likely-tree-documented paths (core/scripts, core/config, .claude/skills, .claude/rules, world/conventions, world/scripts), and files ONE `knowledge_debt` per flagged file (node_key=null, reason="post-edit reconciliation: <path>", source_goal: respond-step-6.5, priority: MEDIUM). Deferred node-mapping is the consumer's job (Lane 1.6 or Phase 4.5). Mode-gate skip list updated to include Step 6.5. Schema verified. |
| E3 | ✓ R9 | Tool-result-surprise encoding (assistant) | When agent runs Bash/Read/Grep mid-turn and the result contradicts the tree or prior expectation, no encoding lane fires. Step 7.5 surprise-detection is user-message-focused. | `.claude/skills/respond/SKILL.md` Sub-step 7.5e — Tool-Result Surprise added after 7.5d (Journal Entry). Scans this turn's Bash/Read/Grep results for contradictions against tree-documented values; scores via E7's rubric (port/URL=8, schema=7, missing field=6, silent empty=5); filters surprise<6 and dedups against 7.5a-d / Step 6.5 to avoid double-write; dual-writes to `knowledge_debt` (HIGH if surprise>=7, MEDIUM otherwise) AND `sensory_buffer` (replay_priority high_surprise). Reader mode skip; fail-open. Schema verified. |
| E4 | ✓ R5 | Mid-session cadence checkpoint in assistant | Long assistant sessions (2h+, 30+ turns) encoded only at user-invoked `/encode-session`. Sessions that ended abruptly lost all in-flight learning. | `.claude/skills/respond/SKILL.md` Step 7.6 — Mid-Session Cadence Nudge added between Step 7.5 and Persona Configuration. Counts substantive turns (any Step 5/6/6.5/7/7.5 firing OR Step 4 escalating past Tier 1) in WM scalar slot `assistant_turn_count`. At multiples of 10, surfaces a one-line nudge in the response showing turn count + queued knowledge_debt count, inviting `/encode-session`. Reader mode and autonomous-RUNNING mode skip. `/encode-session` Phase Final resets the counter to 0 after a flush, so cadence starts fresh after each encoding pass. Slot verified writable; eviction-safe because every substantive turn refreshes mtime. |
| E5 | ✓ R13 | Reader-mode observation queue | Reader produces in-context observations ("this node looks stale") that vanish at session end. Reader is read-only by contract, so writing isn't allowed — but a deferred-to-assistant queue could capture observations for later promotion. | `.claude/skills/respond/SKILL.md` Step 4b — Reader-Mode Observation Surfacing added (Approach A: conversational nudge, no infrastructure change). Mode-gated to reader; surfaces AT MOST ONE noticed staleness / contradiction per turn as a one-line note inviting the user to switch to assistant + `/encode-session`. Zero infrastructure change — reader's no-write contract preserved. |
| E6 | ✓ R10 | Code-review / audit findings (chat-mode) | "Review file X" or "audit SKILL.md Y" produces findings the agent describes inline. These vanish unless `/encode-session` is invoked. | `.claude/skills/respond/SKILL.md` Sub-step 7.5f — Agent-Produced Review Findings added after 7.5e. Heuristic detection (user-message keywords OR 3+ finding-style paragraphs OR 3+ `path:line` citations); caps at 5 findings per response; classifies each as bug/drift/code-smell/architecture/doc-gap; dedups against existing rb entries (strengthen on overlap); prescriptive findings ALSO file as guardrail; one knowledge_debt entry per unique file path mentioned (consumer: E2 / Lane 1.6). Reader mode skip; fail-open. Dedups against 7.5a to avoid double-encoding user-shared insights. |
| E17 | ✓ R2 | Knowledge-debt sweep in assistant mode (consumer side of T13/E1/E2) | `/respond` Step 6 writes `wm-append.sh knowledge_debt`, but `/encode-session` had no consumer for that array. In a pure assistant-only session, debt accumulated indefinitely (only autonomous `/aspirations-consolidate` Step 2.25 swept it). | `.claude/skills/encode-session/SKILL.md` Lane 1.6 — Knowledge-Debt Sweep added; mirrors `aspirations-consolidate` Step 2.25 adapted to chat-mode invocation. Auto-resolves debts whose target nodes were already updated (via `tree-read.sh --node` last_updated probe), inline-resolves HIGH-priority or aged debts using session content + canonical scripts, carries others forward with sessions_deferred counter, drops at 10-session ceiling. Writes array back via `wm-set.sh knowledge_debt` and triggers `wm-prune.sh` for age-based cleanup. |

### Autonomous-mode gaps (long-tail leaks)

| Gap | Status | Trigger | What's missing | Resolution |
|-----|--------|---------|----------------|------------|
| E7 | ✓ R6 | Probe-outcome encoding (drift detection) | Probes (`infra-health.sh`, `state-replay`, `efs-ssh.sh`, `aws-exec.sh`) run inside Phase 4. Surprising output stayed in experience archive only. Tree-encoding lottery via Phase 8. Exactly the rb-334/guard-308/rb-389 drift class. | `.claude/skills/aspirations-execute/SKILL.md` Phase 4.5 — Probe-Outcome Surprise Detection sub-section added. Runs IN ADDITION to external_changes + CORRECTED branches. Detects canonical-probe invocation in result text, finds candidate documenting tree nodes via `tree-find-node`, computes surprise per documented-field divergence (port/URL=8, schema=7, missing field=6, silent empty=5), dual-writes to `knowledge_debt` (HIGH priority, node_key=concrete) AND `sensory_buffer` (replay_priority=high_surprise). Schema verified on sensory_buffer. Fail-open. |
| E8 | ✓ R8 | Decision Rule `times_applied` tracking | Tree nodes have Decision Rules (per `decision-rules-append.sh`). When a goal USES one, no counter increments. Reasoning bank has `times_helpful` — tree Decision Rules don't. | `core/scripts/decision-rules-increment.{py,sh}` created. Appends `— applied: N (YYYY-MM-DD)` suffix to rule lines on first use; idempotently increments on subsequent calls (normalize-then-token-overlap match >=70%). `.claude/skills/aspirations-execute/SKILL.md` Phase 4.04 (E8) added between Phase 4 and Phase 4.05 — agent self-reports rules cited during execution, hook increments each. `.claude/skills/reflect-maintain/SKILL.md` Step 2.55 (E8) added — rules with `applied: 0` (or no suffix) AND age > 60 days are retired from the `## Decision Rules` section. Script tested end-to-end including idempotent re-increment. |
| E9 | ✓ R11 | Goal-skipped / goal-expired encoding | When a goal becomes irrelevant (skipped/expired), the REASON is sometimes tree-worthy ("X investigation moot because Y replaced it"). Today: status flips, no encoding. | `core/scripts/aspirations.py cmd_update_goal` — when `field == "status"` AND value in (skipped, expired), AFTER the lock releases, calls new helper `_emit_e9_skip_observation` which appends an encoding_observation to WM sensory_buffer (replay_priority routine_observations, surprise=0.4). Trivial goals (desc<40 + title<30 chars) skipped to avoid noise. Uses sys.executable + wm.py CLI directly (subprocess via bash wrapper breaks on Windows path mangling). Fail-open. Tested end-to-end. |
| E10 | ✓ R7 | Cross-reference confidence on high-surprise resolution | G3/R5 already broad-retrieves on `surprise_level >= 7`. The tree nodes cited in the hypothesis's `context_manifest.tree_nodes_read` should also get a "confidence-recalibration signal" stamped — not just the node Phase 8 picks. | `.claude/skills/review-hypotheses/SKILL.md` Step 3.5 (high-surprise branch) — after the broad re-retrieve, when outcome=CORRECTED AND context_consulted.tree_nodes_read is non-empty, iterates each cited node and decrements confidence by 0.05 (floor 0.0). Uses `tree-update.sh --batch` to set both `confidence` and `last_update_trigger = "surprise-recalibration"` atomically per node. Skip if read fails or already at floor. Fail-open: never blocks the atomic resolve in Step 4. |
| E11 | ✓ R12 | Cross-agent encoding visibility | T23 defers when another agent is encoding the same node, BUT the OTHER agent never sees the deferred queue. | `.claude/skills/prime/SKILL.md` Phase 2 step 5.5a — explicit `--type encoding` board read with author filter. Stash `pending_encodings = [{author, node_key from tags, started_minutes_ago, text}]`. Phase 4 PRIMED block renders a new "Pending encodings (cross-agent, 30m)" section listing each pending encoding (one line each). Omits the entire section when empty (no clutter). Time window matches T23's 30m deferral logic. T23 already posts board messages with type=encoding (line 295 of aspirations-state-update Step 8) — no producer change required. |
| E12 | ✓ R14 | Failed-retrieval as tree-debt signal | When Tier 1 retrieval returns empty for a category that obviously SHOULD have content (parent has ≥3 sibling leaves), that's a gap signal. Today: caller escalates silently, no debt logged. | `core/scripts/retrieve.py` — added `_detect_coverage_gap` helper + `meta.empty_with_populated_siblings` signal in result JSON. Fires only when tree_nodes is fully empty AND a length-≥5 query token appears in ≥3 OTHER nodes (cap sample at 5 keys). Signal carries `{query_category, populated_token, populated_node_count, sample_node_keys}`. `.claude/skills/respond/SKILL.md` Step 4.5 (consumer) — when meta.empty_with_populated_siblings is set, promotes priority to HIGH regardless of tier and includes E12 metadata in debt reason + structured `coverage_gap` field. wm-append schema accepts extra fields (not validated, free-form metadata). Tested via direct function call. |
| E13 | ✓ R17 | Mid-execution encoding for long goals | G4/R11 (Mid-Execution Drift Check) re-fires RETRIEVAL only. A 30+ min, 4000+ char execution probably contains 2-3 distinct learnings — all bundled into one Phase 8 encoding. | Shipped alongside E16. `core/config/encoding-protocol-digest.md` Section F defines the chunk schema (source_goal, chunk_idx, chunk_total, chunk_text<=2000 chars, content_type, scores, target_article, replay_priority). `.claude/skills/aspirations-execute/SKILL.md` Phase 4.05 — Chunked-Encoding Producer added under existing long-execution branch: splits result.text by natural boundaries (### headings, tool-output blocks, paragraph breaks), caps at 5 chunks, scores each independently, appends each as a sensory_buffer item. `.claude/skills/aspirations-state-update/SKILL.md` Step 8 — Chunked-Encoding Consumer added before single-bundle path: when sensory_buffer has chunks for the current goal.id, each chunk runs Section A/B/C of the digest independently and the chunked path REPLACES the single-bundle path. Fail-open at both producer and consumer. |
| E14 | ✓ R15 | Curriculum-stage transition encoding | When `curriculum-gates` promotes a stage, the newly-unlocked capabilities deserve a "what changed in capability boundaries" encoding. Today: gate state flips, nothing tree-side. | `.claude/skills/curriculum-gates/SKILL.md` Step 2 — post-promotion hook added inside the `promoted == true` branch. Extracts newly-unlocked capability keys (where value flipped false→true), composes a description paragraph ("Promoted from {from_name} to {to_name} on <today>. Newly unlocked: ..."), appends an encoding_observation to WM sensory_buffer (replay_priority=goal_completions, scores tuned for a rare-but-high-impact event: novelty=0.8, outcome_impact=0.7, surprise=0.2, goal_relevance=0.8). Does NOT presume a target node path — the encoding gate's category routing picks/creates the right node. Fail-open. Schema verified end-to-end. |

### Cross-mode / meta gaps

| Gap | Status | Trigger | What's missing | Resolution |
|-----|--------|---------|----------------|------------|
| E15 | ✓ R1 | No centralized encoding-triggers catalog | The retrieval side has `retrieval-triggers.md` with stable Gxx IDs. The encoding side had 23 triggers spread across 13 skill files with no index — making per-mode coverage impossible to audit. | This file (`core/config/conventions/encoding-triggers.md`). Mirror retrieval-triggers.md structure. Added cross-link in CLAUDE.md convention index. |
| E16 | ✓ R16 | `/encode-session` ↔ Phase 8 protocol drift risk | `/encode-session` is the chat-mode analog of Phase 8 + Phase 6.5. They share concept but not code. They will drift. | `core/config/encoding-protocol-digest.md` created (Option 1 — shared digest, NOT shared script). Sections A (encoding-gate scoring), B (curator quality gate), C (tree-write steps), D (cross-agent coordination T23), E (knowledge-debt consumption), F (chunked encoding chunk schema for E13), G (mode + trigger coverage). Both consuming skills (`encode-session/SKILL.md` Lane 1 header AND `aspirations-state-update/SKILL.md` Step 8 header) now reference the digest as the single source of truth and require editing it BEFORE editing the skill. Numeric thresholds remain in `memory-pipeline.yaml` (digest cross-references). Same pattern as existing digests (aspirations-loop-digest.md, etc). |

## Encoding flow notes

The encoding pipeline writes through three gates in sequence (see
`core/config/memory-pipeline.yaml` for math + tunable bounds):

1. **Encoding gate** — `(novelty * 0.30) + (outcome_impact * 0.25) + (surprise * 0.20) + (goal_relevance * 0.15) + (repetition_strength * 0.10)`. Encode if ≥ 0.40; skip if < 0.15; review range 0.15–0.40. Category-class multiplier applied AFTER the score, BEFORE threshold compare (framework-meta = 0.60, mixed/other = 1.00).
2. **Curator quality gate** — `(coverage * 0.40) + (specificity * 0.35) + (actionability * 0.25)` ≥ 0.45. Runs between encoding-decision and tree write. Fails: demote to overflow (consolidation gets a second chance).
3. **Tree write** — for nodes crossing growth thresholds (decompose, split, sprout), the `/tree maintain` ops fire automatically (T3).

Consolidation replay priority order (T4–T8):
`violations → context_gap_corrections → high_surprise → standard_deferred → high_outcome_impact → goal_completions → routine_observations`.

`max_replay_items: 10` per consolidation; `consolidation_budget` adjusts in `[5, 15]` based on violations + new_domains + surprise_gt7_count.

## Mode coverage matrix

| Mode | Automatic triggers | User-invoked triggers | Net coverage |
|------|---------------------|------------------------|--------------|
| R | T21 (mechanical only) + E5 (conversational nudge — no writes) | — | Reader is read-only by contract. T21 fires only because it sync-stamps any tree edit — reader doesn't typically produce them. E5 surfaces noticed staleness as a conversational nudge inviting the user to mode-switch — no infrastructure change. Substantive encoding deferred to assistant/autonomous. |
| A | T21 (mechanical), E1 (Step 4.5), E2 (Step 6.5), E4 (Step 7.6), E3 (Step 7.5e), E6 (Step 7.5f), T19 (cadence requires loop, so practically does NOT fire in pure assistant) | T10 `/encode-session`, T11 directive, T12 user-correction, T18 `/research-topic`, T20 `/tree`, T19 manual | 6 auto-fired per-turn + 6 user-driven. E1/E2/E3/E4/E5/E6 closed the per-turn drift class; E17 closes Lane 1.6 consumer for filed debt. |
| Au | T1, T2 (cadence), T3 (conditional), T4–T8 (session-end), T9 (conditional), T11, T12, T13, T14, T15, T16 (cadence), T17 (cadence), T18, T19 (cadence), T21, T22, T23, E7 (Phase 4.5), E8 (Phase 4.04), E9 (cmd_update_goal hook), E10 (review-hypotheses Step 3.5), E13 (Phase 4.05 chunked), E14 (curriculum-gates) | T10, T20 | 27 auto-fired (some user-message-triggered: T11 directive, T12 correction) + 2 user-invoked = 29 total. Dense coverage; E11/E12/E16 close cross-cutting gaps (cross-agent visibility, tree-debt-on-empty-retrieval, protocol-drift guard). |

## Cross-references

- `core/config/conventions/learning-routing.md` — store-routing decision tree
- `core/config/conventions/retrieval-triggers.md` — the symmetric READ catalog (Gxx IDs)
- `core/config/conventions/retrieval-escalation.md` — tier 1/2/3 escalation
- `core/config/memory-pipeline.yaml` — encoding gate math, curator gate, replay priorities
- `core/config/conventions/infrastructure.md` — knowledge reconciliation protocol
- `.claude/rules/knowledge-freshness.md` — the principle behind T12/T13/E2
- `.claude/rules/learning-philosophy.md` — "learning is the mission" mandate
- `.claude/skills/aspirations-state-update/SKILL.md` — T1, T2, T3, T23 implementation
- `.claude/skills/aspirations-consolidate/SKILL.md` — T4, T5, T6, T7, T8 implementation
- `.claude/skills/encode-session/SKILL.md` — T10 implementation (chat-mode analog)
- `.claude/skills/respond/SKILL.md` — T11, T12 implementation
- `.claude/skills/aspirations-execute/SKILL.md` — T13 implementation

## Maintenance

When adding a new encoding trigger (any new skill or phase that writes to
T/R/G/P/E/B/Q/J/L/WM via `tree-update.sh`, `reasoning-bank-add.sh`,
`guardrails-add.sh`, `pattern-signatures-add.sh`, `experience-add.sh`, etc.),
add a row to the **Active triggers** table with the next available `Tnn`
identifier (highest existing Tnn + 1; do NOT renumber existing rows). When
identifying a place where encoding SHOULD fire but doesn't, add a row to the
**Gap triggers** table with the next available `Enn` identifier (same rule:
highest existing Enn + 1, never renumber). Stable identifiers prevent
cross-reference drift in skills, rules, and reasoning-bank entries that cite
them. When a gap is closed, update its status column to `✓ R{N}` (next
available `Rnn` improvement number) — the row stays in the Gap triggers
table for historical traceability, the Resolution column documents what
landed.
