# Knowledge & Memory Tree Conventions

Reference documentation for memory tree structure, entity cross-links, reasoning bank, and tree conventions. Read on demand by `/research-topic`, `/reflect`, `/tree`, and `retrieve.sh`.

## Knowledge Articles (= Tree Nodes)

Knowledge articles ARE tree node `.md` files — there is no separate article storage. All knowledge lives in the memory tree under `world/knowledge/tree/`.

- YAML front matter minimal schema (all scoring/structural fields live in `_tree.yaml` only):
  - `topic` — display name of this node
  - `entities` — entity cross-links for retrieval concept matching
  - `temporal_validity` — staleness tracking (`first_observed`, `last_confirmed`, `observation_count`, `staleness_days`)
  - `sources` — research provenance (list of source strings)
  - `last_update_trigger` — what caused the last content change (`type`, `source`, `session`)
- Default staleness_days: 7 (domain-specific overrides allowed)
- Always include Key Insights section
- Fields that live ONLY in `_tree.yaml` (never in .md front matter): `confidence`, `capability_level`, `accuracy`, `sample_size`, `article_count`, `node_type`, `domain_confidence`, `depth`, `parent`, `last_updated`, `children`, `retrieval_count`, `last_retrieved`, `growth_state`

## Memory Tree (K=4 MAX, D=4 retrieval, D_max=20 structural)

The system's memory is organized as a **dynamic random tree** inspired by the *Random Tree Model of Meaningful Memory* (Zhong et al., PRL 2025). Each node compresses its descendants, producing sublinear memory growth. The tree grows organically via `/tree maintain` operations: DECOMPOSE, REDISTRIBUTE, SPLIT, SPROUT, MERGE, PRUNE.

**Dynamic tree index**: `world/knowledge/tree/_tree.yaml` — the living registry of all nodes. Skills read this to navigate; never hardcode category-to-path mappings. Framework config (K_max, D_max, thresholds): `core/config/tree.yaml`.

```
L0: _tree.yaml root node (virtual root — summary field)
 ├── L1: world/knowledge/tree/{domain}.md              (interior)
 │    ├── L2: world/knowledge/tree/{domain}/{topic}.md  (leaf or interior)
 │    │    ├── L3: .../{topic}/{subtopic}.md            (leaf or interior)
 │    │    │    ├── L4: .../{subtopic}/{detail}.md      (leaf or interior)
 │    │    │    │    ├── L5: .../{detail}/{sub-detail}.md
 │    │    │    │    └── L6: max structural depth
 │    │    │    └── L4: .../{subtopic}/{detail-2}.md
 │    │    └── L3: .../{topic}/{subtopic-2}.md
 │    └── L2: world/knowledge/tree/{domain}/{topic-2}.md
 ├── L1: world/knowledge/tree/execution.md              (what to DO)
 ├── L1: world/knowledge/tree/intelligence.md           (what we KNOW)
 ├── L1: world/knowledge/tree/performance.md            (how we're DOING)
 └── L1: world/knowledge/tree/system.md                 (HOW we work)

Directory nesting: interior nodes at depth N have a same-named directory
containing their children at depth N+1. Retrieval descends to D_retrieval=4.
Structural depth may extend to D_max=20 for fine-grained storage.
```

**Capability Levels** gate what operations are permitted per category AND what growth operations are allowed:
- `EXPLORE` (<0.30): research-only, no active hypotheses, no SPLIT
- `CALIBRATE` (0.30-0.60): hypotheses allowed, System 2 mandatory, SPROUT allowed
- `EXPLOIT` (0.60-0.80): full hypotheses, System 1 available, SPLIT/MERGE allowed
- `MASTER` (>0.80): fast evaluation, strategy teaching, all growth ops + skill forging

**Compression Protocol**: Findings flow upward through all levels with depth-dependent compression — deeper levels compress more aggressively per the Random Tree Model (Zhong et al., PRL 2025). A parent at depth D compresses its children's content at ratio `10 * 1.5^(D - 2)`: depth-2 parent compresses L3→L2 at ~10:1, depth-3 at ~15:1, depth-4 at ~22:1, depth-5 at ~34:1. Top levels use fixed ratios: L2→L1 ~25:1, L1→L0 ~50:1 (not formula-derived). Recall saturates at K^(D-1)=64 items per retrieval pass. Promotion triggers: child→parent on every article update (SPLIT when article_count > 3), parent→grandparent on capability level change, L1→L0 on domain-level strategy shift. Tree growth operations are defined in `/tree` skill (`/tree maintain` sub-command).

**Context Loading** (PageIndex-style vectorless retrieval): Two retrieval modes exist:

**1. Intelligent Retrieval (Phase 4 goal execution)**: The LLM reads `_tree.yaml` directly, reasons about which nodes are relevant to the current goal, reads those `.md` files via the Read tool, then calls `retrieve.sh --supplementary-only --category {cat}` to load reasoning bank, guardrails, pattern signatures, experiences, and beliefs. This gives the LLM full control over which tree nodes to load and why.

**2. Script-based Retrieval (other callers)**: Skills call `retrieve.sh --category {cat} --depth {d}` before their main work. All depth levels return full results with `.md` content. The script uses four matching strategies in order, deduplicating results:

1. **Exact key match** — category string matches a `_tree.yaml` node key directly
2. **Summary substring** — category appears in a node's `summary` field
3. **Path component** — category appears as a segment in the node's file path
4. **Concept match** — category matches `entities` listed in `.md` front matter of tree nodes

For nodes at depth 3+, matched nodes also pull in their siblings (same-parent nodes) to provide local context. Results are ranked by `match_score` (a numeric relevance rating) rather than depth-first tree order. Each returned tree node includes `match_channel` (which strategy matched it) and `match_score`. The output meta section includes `retrieval_channels` listing which strategies contributed.

**Multi-category**: Comma-separated categories are supported (e.g., `--category "api,infrastructure"`). Each category is matched independently; results are merged and deduplicated.

**`--supplementary-only`**: Skips tree node matching entirely, returning only supplementary stores (reasoning bank, guardrails, pattern signatures, experiences, beliefs, experiential index). Tree nodes array is empty. Used when the LLM has already loaded tree nodes via direct reads.

Returns all context as JSON. Fails open — if nothing relevant, proceed without context.

## Dynamic Tree Conventions

### Node Registry
- All memory tree nodes registered in `world/knowledge/tree/_tree.yaml`
- Skills read `_tree.yaml` to navigate — never hardcode category→path mappings
- `growth_state` values: `stable`, `growing`, `ready_to_split`, `ready_to_decompose`
- K_max=4 is a soft limit at all levels (can exceed when justified, log reason to tree_growth_log)

### Growth Operations
- DECOMPOSE: `.md` body > `decompose_threshold (80)` lines AND `depth < D_max` → break monolith into 2-4 child nodes
- SPLIT: `article_count > split_threshold (3)` → cluster into subtopics
- SPROUT: new content with no matching node → create new branch
- MERGE: `article_count <= merge_threshold (1)`, no children → absorb into sibling
- PRUNE: empty node, no sibling → archive
- All levels auto-create; L1 new domains logged prominently in journal and tree_growth_log
- Always log to `tree_growth_log` in `world/knowledge/tree/_tree.yaml`

### Interior vs Leaf Invariant
- **Interior nodes**: have children, contain summary/index only, `node_type: interior` in `_tree.yaml`
- **Leaf nodes**: no children, contain detailed content, `node_type: leaf` in `_tree.yaml`
- `node_type` lives exclusively in `_tree.yaml` — never in `.md` front matter
- When a leaf grows too large (> decompose_threshold lines) → DECOMPOSE → becomes interior, content distributed to new children
- **Directory convention**: when a leaf at `path/{node}.md` becomes interior, create a directory `path/{node}/` alongside it for its children. The `.md` file remains as the summary/index for that interior node.
- Every node is exactly one of interior or leaf — never both. The `node_type` field in `_tree.yaml` is the source of truth.

### Tree Node Files
- Path construction at any depth: `world/knowledge/tree/{L1}/{L2}/.../{node}.md`
- YAML front matter: minimal schema only — `topic`, `entities`, `temporal_validity`, `sources`, `last_update_trigger`
- All structural/scoring metadata (`parent`, `depth`, `node_type`, `confidence`, `capability_level`, `article_count`) lives in `_tree.yaml` only
- See `core/config/tree.yaml` `node_file_front_matter` template for the canonical minimal schema

### Provenance on Interior Node Summaries
- Interior node files (any depth) SHOULD include `last_update_trigger` in YAML front matter
- Updated incrementally as nodes are touched (no batch migration for existing files)
- Format:
  ```yaml
  last_update_trigger:
    type: "capability_change"  # or: reflection, consolidation, spark, tree_growth
    source: "description of what caused the update"
    session: 13
  ```
- Common trigger types: `capability_change` (level threshold crossed), `reflection` (from /reflect), `consolidation` (session-end encoding), `spark` (goal-level spark), `tree_growth` (DECOMPOSE/SPLIT/SPROUT/MERGE/PRUNE)

## Entity Cross-Links

- Entity index lives in `world/knowledge/tree/_tree.yaml` under `entity_index`
- Entity format: `{entity_name: {articles: [paths], tree_nodes: [node_ids], mention_count: N}}`
- Entity types: `person`, `organization`, `concept`, `metric`, `event` (informational, not enforced)
- Entities extracted during: `/research-topic` (Step 3), `/reflect` (Step 7.5b)
- Resolution: entity names are normalized to lowercase-kebab-case for matching
- Stale entity cleanup: entities with 0 articles after tree pruning are removed

## Reasoning Bank Retrieval Rules

- k-bounded retrieval: load at most k=2 reasoning bank entries per query (Phase 2a)
- k-bounded guardrails: load at most k=3 guardrails per query (Phase 2b)
- Ranking: success entries > failure entries, then by `utilization_score` descending
- `when_to_use` field: structured trigger conditions on reasoning bank entries
  - Format: `when_to_use: {conditions: ["condition1", "condition2"], category: "cat", confidence_range: [lo, hi]}`
  - Matched against current query context during Phase 2a filtering
  - Entries without `when_to_use` are matched by category only (backwards compatible)
- Reasoning bank entry types: `success`, `failure`, `contrastive`
  - `contrastive`: extracted from CONFIRMED/CORRECTED pairs in the same category — captures what distinguished success from failure

## Utilization Tracking (Reasoning Bank & Guardrails)

- Every reasoning bank entry and guardrail SHOULD have a `utilization` section:
  ```yaml
  utilization:
    retrieval_count: 0    # incremented each time loaded by retrieve.sh
    last_retrieved: null   # date of last retrieval
    times_helpful: 0       # incremented when context_quality rates this item's layer as most_valuable
    times_noise: 0         # incremented when context_quality rates this item's layer as least_valuable
    times_active: 0        # incremented when deliberation marks ACTIVE
    times_skipped: 0       # incremented when deliberation marks SKIPPED
    utilization_score: 0.0 # (times_helpful + times_active) / max(retrieval_count, 1)
  ```
- `utilization_score` recalculated by `/reflect` Step 7.7f after each outcome
- Entries with `utilization_score < 0.20` after 5+ retrievals are candidates for retirement

### Lifecycle Status Field

All knowledge artifacts carry a `status` field to track their lifecycle:

| Artifact Type | Valid Statuses | Default |
|---------------|---------------|---------|
| Strategies | `active`, `retired` | `active` |
| Pattern signatures | `active`, `retired`, `contradicted` | `active` |
| Guardrails | `active`, `retired` | `active` |
| Reasoning bank entries | `active`, `retired` | `active` |

**Backward compatibility**: Missing `status` field is treated as `active` everywhere.

**Retirement fields** (set when status changes to `retired` or `contradicted`):
- `retirement_date`: ISO 8601 date
- `retirement_reason`: brief explanation

**Strategies** additionally track usage:
- `times_applied`: incremented when loaded and marked ACTIVE in deliberation
- `last_applied`: date of last ACTIVE deliberation

Retirement is contextual, never forced. Items become candidates via spark questions (sq-c03, sq-c04) and memory curation (`/reflect --curate-memory`). The utilization thresholds above identify candidates; agent judgment decides actual retirement.

## Experience Archive (Full-Fidelity Interaction Traces)

Experiences store complete interaction traces, tool outputs, and evidence indexed by stable keys. They complement tree nodes (which compress detail) by preserving the full context of what the agent actually saw and did. Experiences answer "what have we tried, and did it work?" vs the tree's "what do we know?".

### Architecture

Experiences are **operational state** (lifecycle, retrieval stats, archival) not **reference knowledge**. A single experience may span multiple tree node topics. They live separately from the tree but cross-link to it.

- Experience records: `<agent>/experience.jsonl` (JSONL, script-accessed only)
- Full content files: `<agent>/experience/{id}.md` (markdown, read via Read tool)
- Experience archive: `<agent>/experience-archive.jsonl` (append-only)
- Experience metadata: `<agent>/experience-meta.json`

### JSONL Record Schema

Required: `id`, `type`, `created`, `category`, `summary`, `content_path`
Default: `goal_id` (null), `hypothesis_id` (null), `tree_nodes_related` ([]), `verbatim_anchors` ([]), `retrieval_stats` (zeros), `archived` (false), `archived_date` (null)

ID format: `exp-{source-id-or-slug}` (e.g., `exp-g001-05-research`, `exp-2026-03-10_api-response-latency`)

Valid types: `goal_execution`, `hypothesis_formation`, `research`, `reflection`, `user_correction`, `user_interaction`, `execution_reflection`

### Verbatim Anchors

Exact error messages, API responses, user corrections, and surprising findings stored as key-content pairs within the JSONL record:

```yaml
verbatim_anchors:
  - key: "error-msg"
    content: "exact text preserved verbatim"
  - key: "api-response"
    content: "raw response body"
```

Anchors prevent hallucination by storing exact content. Tree node articles and pipeline records can reference anchors: `"See exp-{id} anchor: error-msg"`.

### Retrieval Stats

Every experience record tracks its retrieval history:

```yaml
retrieval_stats:
  retrieval_count: 0      # incremented each time loaded by retrieve.sh
  times_useful: 0          # incremented when deliberation marks ACTIVE
  times_noise: 0           # incremented when deliberation marks SKIPPED
  utility_ratio: 0.0       # times_useful / max(retrieval_count, 1)
  last_retrieved: null      # date of last retrieval (resets relevance clock)
```

### Content Files

Full interaction trace stored at `content_path` (e.g., `<agent>/experience/exp-g001-05-research.md`). Created alongside the JSONL record. Contains complete reasoning traces, tool outputs, decisions, and outcomes. Read via standard `Read` tool when dereferencing.

### Cross-Linking to Tree Nodes

- Experience records include `tree_nodes_related: [node-key-1, node-key-2]`
- Tree node articles gain `experience_refs: [exp-id-1, exp-id-2]` in YAML front matter
- This bidirectional linking enables: "what experiences informed this knowledge?" and "what knowledge does this experience relate to?"

### Staleness & Archival

Experiences are records of what happened — the event doesn't become less true, but its relevance decays. Staleness triggers archival (move JSONL record to `experience-archive.jsonl`), not deletion. Content `.md` files stay in `<agent>/experience/` and remain dereferenceable.

Archive triggers (via `experience-archive.sh`, called during session-end consolidation):
- `created` > 30 days AND `retrieval_count == 0` → never used → archive
- `created` > 90 days AND `utility_ratio < 0.2` → consulted but not helpful → archive
- **Never archive** if `retrieval_count >= 5` AND `utility_ratio > 0.5` → actively valuable, keep live

Retrieval strengthening: Each retrieval updates `last_retrieved`. Frequently-used experiences stay live indefinitely.
