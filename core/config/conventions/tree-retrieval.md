> For the full retrieval escalation policy (tree → codebase → web search),
> see `core/config/conventions/retrieval-escalation.md`. This file documents
> the Tier 1 (knowledge tree) retrieval engine specifically.

# Unified Retrieval Script Access

Context retrieval is implemented by `core/scripts/retrieve.sh` — a single script call that
reads ALL data stores, increments retrieval counters, and returns unified JSON.

| Script | Purpose | Stdin |
|--------|---------|-------|
| `retrieve.sh --category <cat> --depth <d>` | Load all context for a category | — |
| `retrieve.sh --category "cat1,cat2" --depth <d>` | Multi-category (comma-separated) | — |
| `retrieve.sh --supplementary-only --category <cat>` | Supplementary stores only (skip tree nodes) | — |

All depth levels return full results with `.md` content included. The `--depth` parameter
is accepted for backward compatibility but all levels return equivalent results.

`--supplementary-only` skips tree node matching entirely, returning only reasoning bank,
guardrails, pattern signatures, experiences, beliefs, and experiential index. Used by
the intelligent retrieval protocol in Phase 4, where the LLM reads `_tree.yaml` directly
and selects tree nodes via the Read tool.

Returns JSON with sections: `tree_nodes`, `reasoning_bank`, `guardrails`,
`pattern_signatures`, `experiences`, `beliefs`, `experiential_index`.

Each tree node entry includes `match_channel` (how it was matched) and `match_score`
(relevance score). Response `meta` includes `retrieval_channels` (list of channels used).

**Matching strategies** (applied in order, results merged):
1. **Exact key**: category string equals a node key
2. **Substring**: category appears in key/summary/topic (bidirectional)
3. **Entity index**: category matches a semantic entity in `_tree.yaml`
4. **Word-prefix**: hyphen-split words, prefix match (min 4 chars)
5. **Concept**: query tokens matched against `.md` front-matter `entities` fields

After matching: sibling inclusion (D3+ direct matches add siblings), parent inclusion
(matched L2+ nodes add their L1 parent), then scored by match quality (not depth-first).

Side effect: increments retrieval_count on all returned items.

---

# Memory Tree Script Access

The memory tree (`world/knowledge/tree/_tree.yaml`) is accessed via scripts for mechanical operations.
Use scripts for node lookup, path computation, ancestor walking, and field updates.
Direct `_tree.yaml` reads are still used for complex multi-node operations (SPLIT, DECOMPOSE)
and for semantic matching (choosing which node fits a category).

## Script-Based Access

| Script | Purpose | Stdin |
|--------|---------|-------|
| `load-tree-summary.sh` | Convention-style cached tree summary (gates re-reads via dedup tracker) | — |
| `tree-read.sh --node <key>` | Full node as JSON (defaults applied) | — |
| `tree-read.sh --path <key>` | File path string | — |
| `tree-read.sh --ancestors <key>` | Parent chain array (node → root) | — |
| `tree-read.sh --children <key>` | Immediate children as JSON array | — |
| `tree-read.sh --leaves` | All leaf nodes | — |
| `tree-read.sh --leaves-under <key>` | Leaf descendants of a subtree | — |
| `tree-read.sh --stats` | Node counts by depth, interior/leaf totals | — |
| `tree-read.sh --child-path <parent> <slug>` | Compute file path for new child | — |
| `tree-read.sh --validate` | Check parent-child consistency | — |
| `tree-read.sh --decompose-candidates` | Leaf nodes exceeding decompose_threshold (sorted by line count desc) | — |
| `tree-read.sh --redistribute-candidates` | Interior nodes with large bodies (sorted by line count desc) | — |
| `tree-update.sh --set <key> <field> <value>` | Update a single node field | — |
| `tree-update.sh --add-child <parent-key>` | Register child + update parent | JSON |
| `tree-update.sh --remove-child <parent> <child>` | Deregister child + update parent | — |
| `tree-update.sh --increment <key> <field>` | Atomic increment of numeric field | — |
| `tree-find-node.sh --text <text> [--top N] [--leaf-only]` | Find best-matching node(s) for text query | — |
| `tree-read.sh --summary` | Compact tree: keys, file paths, summaries, depth, capability, confidence, children | — |
| `tree-update.sh --batch` | Batch set/increment/add-child/remove-child/propagate (one parse/write cycle) | JSON |
| `tree-propagate.sh <node-key>` | Propagate confidence up parent chain, detect capability changes | — |

Scripts apply defaults for missing fields: `article_count` (0), `growth_state` ("stable"),
`node_type` ("leaf" if no children, "interior" if children exist).

All backed by `core/scripts/tree.py` (Python 3 + PyYAML).

### Batch Update

Single parse/write cycle for multiple operations. Validates all node keys before mutating.
Supports five operation types: `set`, `increment`, `add-child`, `remove-child`, `propagate`.
`propagate` ops always execute LAST, after all mutations, so they see updated child confidences.

```bash
# Simple set/increment (returns plain JSON array — backward compatible)
echo '{"operations": [
  {"op": "set", "key": "node-key", "field": "confidence", "value": 0.85},
  {"op": "increment", "key": "node-key", "field": "article_count"}
]}' | bash core/scripts/tree-update.sh --batch

# Full decompose (atomic — returns {"updated_nodes": [...], "propagate": [...]})
echo '{"operations": [
  {"op": "set", "key": "parent", "field": "node_type", "value": "interior"},
  {"op": "set", "key": "parent", "field": "article_count", "value": 0},
  {"op": "add-child", "key": "parent", "child": {"key": "child-1", "summary": "First child"}},
  {"op": "add-child", "key": "parent", "child": {"key": "child-2", "summary": "Second child"}},
  {"op": "propagate", "key": "parent"}
]}' | bash core/scripts/tree-update.sh --batch

# Remove child
echo '{"operations": [
  {"op": "remove-child", "key": "parent-key", "child_key": "child-key"},
  {"op": "propagate", "key": "parent-key"}
]}' | bash core/scripts/tree-update.sh --batch
```

Output: plain JSON array if no propagate ops (backward compat), or
`{"updated_nodes": [...], "propagate": [{source_node, ancestors_updated, capability_changes}]}` if propagate ops included.

`write_tree()` includes retry-with-backoff (5 attempts, exponential 50-800ms) for transient
`PermissionError`/`OSError` from OneDrive file sync locking.

### Propagate

Walks parent chain from node to root. For each ancestor: averages children's confidence,
updates `confidence` + `domain_confidence`, detects `capability_level` threshold crossings.

```bash
bash core/scripts/tree-propagate.sh <node-key>
# Returns: {source_node, ancestors_updated: [{key, old_confidence, new_confidence, capability_changed}],
#           capability_changes: [{key, old_level, new_level}]}
```

Capability thresholds read from `core/config/tree.yaml` `domain_health.competence_mapping`.
Stops propagation when confidence is unchanged. Body text updates (capability map tables)
remain the caller's responsibility — they require LLM reasoning.

### Find Node

Returns best-matching node(s) for a text query using substring, entity index, word-prefix,
and concept matching strategies.

```bash
bash core/scripts/tree-find-node.sh --text "authentication service" --top 3
bash core/scripts/tree-find-node.sh --text "deployment" --leaf-only --top 1
```

Returns JSON array: `[{key, score, file, depth, summary, node_type}]`.
`--leaf-only` filters to nodes with no children (most specific writable nodes).

---

# Category Suggestion Script Access

Category resolution maps free text to tree node keys. Used by goal creation,
goal selection fallback, and category backfill.

| Script | Purpose | Stdin |
|--------|---------|-------|
| `category-suggest.sh --text <text> [--top N]` | Return best-matching tree node key(s) for text | — |
| `category-backfill.sh [--dry-run]` | Assign categories to all goals missing them | — |

`category-suggest.sh` scores tree nodes against input text using:
1. Exact key substring match (+3)
2. Word overlap with key segments (+1/match)
3. Word overlap with summary (+0.5/match, capped at 3)
4. Word overlap with .md front-matter entities (+1.5/match)

Excludes D0/D1 structural nodes. Returns JSON array sorted by score descending.

All backed by `core/scripts/category-suggest.py` (Python 3, PyYAML).
