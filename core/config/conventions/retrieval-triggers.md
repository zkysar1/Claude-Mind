# Retrieval Triggers

Authoritative catalog of every point where the agent loads accumulated knowledge
into active context. The single source of truth for "where does retrieval
happen, where should it happen, and which stores does each trigger touch."

For the escalation policy (tree → codebase → web) see
`core/config/conventions/retrieval-escalation.md`. For the engine details
(matchers, ranking, caps) see `core/config/conventions/tree-retrieval.md`.
This file documents the **trigger surface**: WHO fires retrieval, WHEN, and
WHY.

## Stores

Abbreviations used in the matrix below:

| Code | Store | Path |
|------|-------|------|
| T | Knowledge tree | `world/knowledge/tree/_tree.yaml` + node `.md` files |
| R | Reasoning bank | `world/reasoning-bank.jsonl` |
| G | Guardrails | `world/guardrails.jsonl` |
| P | Pattern signatures | `world/pattern-signatures.jsonl` |
| E | Experience archive | `agents/<agent>/experience.jsonl` + `agents/<agent>/experience/` |
| B | Beliefs | `world/knowledge/beliefs.yaml` |
| X | Experiential index | `agents/<agent>/experiential-index.yaml` |
| F | Framework rules + conventions | `.claude/rules/*.md`, `core/config/conventions/*.md` |
| C | Codebase | primary workspace (Grep/Glob/Read) |
| W | Web | WebSearch / WebFetch |

The unified retrieval script (`core/scripts/retrieve.sh`) reads T+R+G+P+E+B+X
in one call. F is loaded two ways: by exact convention key via
`core/scripts/load-conventions.sh` keyed to skill front matter, AND
content-searchable by topic via `retrieve.sh --include-framework`
(returned under the `framework_rules` key — closes G8 below). C and W
are escalated tiers (per retrieval-escalation.md).

## Status legend

| Symbol | Meaning |
|--------|---------|
| ✓ | Fires retrieval today |
| ◐ | Partial — fires for some sub-paths, not all |
| ✗ | Does not fire today |
| → R{N} | Implemented by improvement R{N} in this catalog |

## Active triggers (what fires today)

| Trigger | Status | Stores | Sync? | Implementation site |
|---------|--------|--------|-------|---------------------|
| Session start (prime) | ✓ | T+R+G+P+E+B+X + universal-RB | Sync | `.claude/skills/prime/SKILL.md` |
| Goal execution — Phase 4 Intelligent Retrieval | ✓ | T + R+G+P+E+B via `--supplementary-only` | Sync | `.claude/skills/aspirations-execute/SKILL.md`, `core/config/execute-protocol-digest.md` |
| User message — `/respond` Step 4 | ✓ | Tier 1: T+R+G+P+E; Tier 2: C; Tier 3: W (mode-gated) | Sync, escalated | `.claude/skills/respond/SKILL.md` |
| Decomposition — `/decompose` Step 3.5 | ✓ | T+R+G+P+E via `retrieve.sh --category {cat} --depth shallow` | Sync | `.claude/skills/decompose/SKILL.md` |
| Hypothesis resolve — `/review-hypotheses` Step 1.5 | ✓ | T+R+G+P+E+B per category, cached | Sync | `.claude/skills/review-hypotheses/SKILL.md` |
| Hypothesis reflection — `/reflect-on-outcome` Step 1 | ✓ | Experience via `experience_ref`; R/G category dedup | Sync | `.claude/skills/reflect-on-outcome/SKILL.md` |
| Replay — `/replay` Step 1.5, Step 5 | ✓ | T+R+G+P+E+B per category | Sync, cached | `.claude/skills/replay/SKILL.md` |
| State update — `/aspirations-state-update` Step 8r (every 5 routines) | ◐ | T via `tree-find-node` | Sync | `.claude/skills/aspirations-state-update/SKILL.md` |
| Learning gate — `/aspirations-learning-gate` Phase 9.5b | ◐ Audit; forces retroactive retrieve only when skipped entirely | T+R+G+P+E+B | Sync | `.claude/skills/aspirations-learning-gate/SKILL.md` |
| Encoding (chat) — `/encode-session` Lane 1 | ◐ Dedup-only retrieval (`tree-find-node` + RB/G category reads) | T+R+G | Sync | `.claude/skills/encode-session/SKILL.md` |
| Spark — `/aspirations-spark` sq-009 hypothesis gen | ✓ | T+R+G+P+E via `retrieve.sh --depth shallow` + pipeline-read | Sync | `.claude/skills/aspirations-spark/SKILL.md` |
| Spark — sq-c05 data acquisition | ◐ | T (entity_index scan) | Sync | same |
| Spark — sq-c04 contradiction check | ◐ | T (leaves under category) | Sync | same |
| Spark — sq-013 work discovery | ◐ | aspirations-read for dedup; no R/G consultation | Sync | same |
| Goal selection — `/aspirations-select` Phase 2.25 | ◐ | T via `load-tree-summary.sh` (selection-context only — NOT R/G/P/E) | Sync | `.claude/skills/aspirations-select/SKILL.md` — see Gap G1 |
| Before negation — exhaustive-search-before-negation | ✓ | All stores via 3+ query variations | Sync | `core/config/conventions/exhaustive-search-before-negation.md` |
| Before applying framework-file fix | ✓ | R+G via `retrieve.sh --category "<free-text>"` (token-overlap) | Sync | `.claude/rules/code-review-protocol.md` step 4 |
| Before discovering stable fact | ✓ | F (resource-locator convention files) | Sync | `.claude/rules/encode-stable-facts.md` |
| Boot — `/boot` Step 2.7 | ✓ | Delegates to `/prime` | Sync | `.claude/skills/boot/SKILL.md` |
| Knowledge reconciliation — Phase 4.5 | ◐ | T (identifies affected nodes from external_changes) | Sync | `.claude/skills/aspirations-execute/SKILL.md` Phase 4.5 |

## Gap triggers (what should fire but doesn't)

Each entry below names where retrieval is missing and the improvement that
adds it. The numbered Gxx labels are stable identifiers for cross-reference
from skills, rules, and reasoning-bank entries. Status column shows the
implementation state: ✓=implemented this session, ✗=open, ◐=partial.

| Gap | Status | Trigger | What was missing | Resolution |
|-----|--------|---------|------------------|------------|
| G1 | ✓ R8 | `aspirations-select` scoring | Cross-cutting R/G that should inform "is this the right goal now"; the selector read goal metadata only | `aspirations-select/SKILL.md` Phase 2.27 — Cross-Cutting Guardrail Probe added before Phase 2.5 metacognitive assessment |
| G2 | ✓ R6 | `aspirations-verify` Q1/Q2/Q3 escalation | Guardrails/R about verification anti-patterns; sets the LLM up to escalate blindly | `aspirations-verify/SKILL.md` — Pre-Escalation Retrieval added before Empty-Checks Escalation Protocol |
| G3 | ✓ R5 | Surprise → broad re-retrieve | `surprise_level >= 7` should pull category-broad B/R that may have been falsified by this resolution | `review-hypotheses/SKILL.md` Step 3.5 — Broad Re-Retrieve on High Surprise added before atomic move |
| G4 | ✓ R11 | Mid-execution drift | Long-running goals (>30 min wall-clock OR >4000 chars result) should re-retrieve fresh context | `aspirations-execute/SKILL.md` Phase 4.05 — Mid-Execution Drift Check added between Phase 4 and Phase 4-post |
| G5 | ✓ R7 | Blocker re-probe (`aspirations-precheck` Phase 0.5b) | R about prior probe attempts and canonical-probe pattern should inform probe shape | `aspirations-precheck/SKILL.md` Phase 0.5b — Pre-probe retrieval added inside FOR-EACH-blocker loop, before infra-health probe |
| G6 | ✓ R10 | Inbound board post / email | New external signal should trigger a delta-retrieve for relevant context before the agent acts | `access-email/SKILL.md` — Inbound Signal Retrieval section added; `aspirations-execute/SKILL.md` Phase 3.97 — Inbound Signal Sweep added |
| G7 | ✓ R9 | `create-aspiration` dedup | R/G that constrain or refine the new aspiration; today only title-matches existing aspirations | `create-aspiration/SKILL.md` Step 5.1.5 — Cross-store contradiction check added inside Step 5 |
| G8 | ✓ R13 | Framework rules + conventions not retrievable | F store is loaded by skill-frontmatter convention key, not searchable by content; the agent cannot retrieve a rule by topic | `core/scripts/retrieve.py` — `load_framework_rules()` walks `.claude/rules/*.md`, `core/config/conventions/*.md`, `world/conventions/*.md`; parses title + section headers + first 500 chars of body per file; reuses `_entry_matches_text` token-overlap. Opt-in via new `--include-framework` flag; result returned under new `framework_rules` key. Index rebuilt every call (corpus is ~94 files, body samples ~50KB total — O(ms) cost; no parallel YAML). **Wired (g-001-232, 2026-05-12)**: `.claude/skills/respond/SKILL.md` Tier 1 retrieve passes `--include-framework` for all user queries; e2e probe confirmed query "implementation discipline avoid scope creep" surfaces `.claude/rules/implementation-discipline.md`. |
| G9 | ✓ R3 | Supplementary-store matcher is bimodal with tree nodes | T uses token-overlap; R/G/P used `_entry_matches_category` (substring on category field only); free-text queries returned zero supplementary hits | `core/scripts/retrieve.py` — `_entry_matches_text` token-overlap fallback added; 3 loaders updated to use `_entry_matches` combined predicate. Verified: all 5 paraphrased queries now return rb=8-40, g=14-40. |
| G10 | ✓ R12 | "Retrieval influence:" not captured in journal | Phase 4 Step 5c output existed in protocol but never reached journal entries | `core/config/execute-protocol-digest.md` Step 5c — writes `retrieval_influence_last` to working memory; `core/scripts/iteration-close.sh` — reads slot and passes via new `--retrieval-influence` flag; `core/scripts/journal-append.sh` — accepts flag and emits "Retrieval influence:" line |
| G11 | ✓ R14 | `/respond` Step 5 directive writes | Self update / new aspiration / paused aspiration / remembered fact / recurring task all write to a store without first retrieving to check for contradictions, constraints, or duplicates. Non-autonomous-mode counterpart to G7. | `.claude/skills/respond/SKILL.md` Step 5.0 — Pre-Write Retrieval added between Mode Gate and directive routing. Reuses Step 4 retrieval when subject matches; re-retrieves for divergent subjects. Surfaces contradictions to user; user is authoritative. |
| G12 | ✓ R15 | `/respond` Step 6 Knowledge Freshness | User correction triggered `_tree.yaml` scan only — missed RB/guards/beliefs/patterns that depend on the falsified knowledge. Sister mechanism to G3 surprise→broad-retrieve. | `.claude/skills/respond/SKILL.md` Step 6.1.a' — Broad re-retrieve added; builds reconciliation_candidates set across all stores; sub-step e' marks contradicting rb/guard/bel/sig for review (retire via existing `*-update.sh` scripts). |
| G13 | ✓ R16 | `/encode-session` Lane 1 dedup quality | Lane 1 used `tree-find-node` (substring) + `reasoning-bank-read --category` (unfiltered category dump) — missed semantic matches and entries in sibling categories. | `.claude/skills/encode-session/SKILL.md` Lane 1.0 — Pre-Encoding Retrieval added; runs `retrieve.sh --depth medium --read-only` per topic and stashes snapshot. Sub-lanes 1.1/1.2/1.3 consult snapshot first; narrow tools are now fallback only. `--read-only` avoids distorting topic utility_ratio during chat-mode encoding. |

## Engine behavior notes

The matcher in `core/scripts/retrieve.py` works in two layers:

1. **Tree nodes** — multi-channel matching: Substring, Entity-index,
   Word-prefix, Concept (`.md` front-matter entities). TF-IDF cosine bonus,
   recency decay, MMR diversity rerank when top-K cap binds. Token-overlap
   works for any free-text query.
2. **Supplementary stores (R/G/P)** — single-channel matching:
   `_entry_matches_category` does bidirectional substring against the
   entry's `category` field only. Free-text queries that don't match a
   category exactly return zero supplementary hits even when title /
   content / tags / when_to_use would obviously match.

This bimodal behavior is **G9**. R3 adds a text-fallback layer that activates
when category matching returns empty — restoring symmetry between tree-node
and supplementary-store free-text retrieval.

## Cross-references

- `core/config/conventions/retrieval-escalation.md` — three-tier escalation policy
- `core/config/conventions/tree-retrieval.md` — engine details (matchers, caps, ranking)
- `core/config/conventions/exhaustive-search-before-negation.md` — protocol for negative conclusions
- `.claude/rules/retrieve-before-deciding.md` — decision tree for "should I retrieve now"
- `.claude/rules/code-review-protocol.md` — Step 4 pre-apply consultation
- `.claude/rules/verify-before-assuming.md` — multi-signal rule for negative claims
- `.claude/rules/encode-stable-facts.md` — retrieve-before-discovery for resource locators

## Maintenance

When adding a new retrieval trigger (any new skill or skill phase that calls
`retrieve.sh`, `tree-find-node.sh`, `reasoning-bank-read.sh`,
`guardrails-read.sh`, or similar), add a row to the **Active triggers**
table above. When identifying a place where retrieval should fire but
doesn't, add a row to the **Gap triggers** table with a stable Gxx
identifier. Stable identifiers prevent renumber-drift when the catalog
grows.
