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
| `retrieve.sh --category <cat> --full-content` | Include long-form bodies (opt-in) | — |

`--depth` controls TWO axes (both depth-aware since 2026-05-09):

1. **Tree-node sibling/parent expansion** — `deep` (default) runs sibling
   and parent inclusion after the direct-match phase; `shallow` and `medium`
   return only direct matches (functionally equivalent to each other; the
   third level is preserved for forward-compatibility). Tree-node body
   content is NEVER returned inline regardless of depth — the LLM reads
   `entry["file"]` via the Read tool.
2. **Supplementary-store cap** — reasoning_bank, guardrails, and
   pattern_signatures are filtered by `_entry_matches_category`, sorted by
   `utilization.utilization_score` desc (then `created` desc), and capped at
   `SUPPLEMENTARY_CAPS = {shallow:20, medium:40, deep:80}`. Pre-2026-05-09
   these stores returned ALL active records (280-705 RB / 328 guardrails per
   call) regardless of category — the audit found ~75% had
   utilization_score=0. Counter-bump gating was already category-filtered
   since 2026-04-23; the result-filter brings the returned set into
   alignment.

Experiences are governed by `EXP_LIMITS = {shallow:10, medium:15, deep:25}`
(unchanged). Beliefs and experiential_index remain unfiltered/uncapped —
beliefs are tiny and experiential_index is already category-keyed at the
file level.

Universal RBs (framework-* category OR `applies_to: any`) are surfaced as
`meta_lessons` and capped separately at `UNIVERSAL_RB_CAP=5`; the
SUPPLEMENTARY_CAPS depth cap does not apply to them.

**Default is metadata-only (2026-04-23).** Long-form body fields in the
supplementary stores — reasoning bank `content`, meta lesson `content`,
pattern signature long `description` — are nulled in the output JSON.
Discriminative fields (`title`, `summary`, `when_to_use`, `trigger_condition`,
`category`, `tags`, `utilization`, `confidence`, `capability_level`,
`match_channel`, `match_score`) are preserved so the LLM can triage without
seeing full bodies. Pass `--full-content` to opt in when deliberate deep-reading
of supplementary stores is the already-made decision (e.g. execution-time
intelligent retrieval after triage has picked a target). Guardrail `rule` is
preserved in both modes because rules are short AND are the actionable content.
Paper-Idea-1 cross-pollination from Recursive Language Models (arXiv
2512.24601) — metadata-only history.

**Tree node bodies are never returned inline, in either mode** (2026-04-23
intentional separation). Retrieve returns the tree INDEX: `key`, `file`,
`summary`, scores, utilization. The LLM reads `entry["file"]` via the Read
tool when it decides a specific node body matters. This separation keeps one
code path (retrieve = index, Read = body), avoids duplicating work the LLM
already does, and aligns with the metadata-only history pattern. Previous
versions attempted inline tree-body reads via a PROJECT_ROOT-join that was
wrong for externally-configured world paths and has been removed.

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

After matching, sibling and parent inclusion runs ONLY when `--depth deep`
was requested. Gating added 2026-04-23 after a diagnostic showed sibling
and parent channels contributed the bulk of "retrieved but never helpful"
entries at shallow / medium depth (sparse categories returning thin
results IS honest signal — do not pad). At deep:

- **Sibling inclusion** (`_include_siblings` in `tree_match.py`): direct-match
  nodes (`exact_key`, `substring`, `entity_index`, `word_prefix`, `concept`)
  at depth ≥ 3 add their siblings. Parent- or sibling-included matches do
  NOT recursively pull more siblings.
- **Parent inclusion** (`_include_parents`): every matched node adds its
  immediate parent (root is the only node with no parent).

Then scored by match quality (not depth-first).

**TF-IDF cosine bonus** (2026-05-10): on top of the channel score, each
matched node gets a `COSINE_BONUS_WEIGHT * cosine_similarity(query, node)`
addition, where the cosine is computed against `_tree.yaml`-wide TF-IDF
vectors (key + summary as document text). This rewards rare-token overlap
and downweights common-token overlap — directly addressing the audit's
NOISY-leaf finding (generic-token parents like `intelligence-pipeline`
were ranking against specific leaves on shared common tokens). With
`COSINE_BONUS_WEIGHT=2.0` a fully-aligned query/node pair adds 2.0 atop
typical channel base scores in [3, 7]; partial matches scale linearly.
The IDF index is rebuilt per retrieval call (~110ms over 985 nodes —
no on-disk cache to invalidate). Implementation: `core/scripts/tree_idf.py`.

**Recency bonus** (2026-05-10): each node gets an additional one-sided
exponential-decay bonus from `last_updated`:
`RECENCY_MAX_BONUS * exp(-age_days / RECENCY_TAU_DAYS)`. A node updated
today gets `+0.5`; at age=30d (TAU) bonus ≈ 0.18; at 5×TAU it's
essentially zero. Missing/null/malformed `last_updated` → bonus 0 (no
penalty for legacy nodes). Future-dated entries (clock skew) clamp to
age=0. Constants: `RECENCY_MAX_BONUS=0.5`, `RECENCY_TAU_DAYS=30`.
Implementation: `_recency_bonus` in `core/scripts/tree_match.py`. Backfill
of legacy missing fields: `core/scripts/backfill-tree-node-fields.py`.

**MMR diversity rerank** (2026-05-10): when the top-K cap binds (i.e.,
more candidates were scored than the depth limit allows), the final
selection runs Maximal Marginal Relevance to demote sibling clustering:
`MMR(i) = λ * (score_i / max_score) - (1-λ) * max_j∈S path_sim(i, j)`.
Path similarity is shared-ancestor-prefix length / max chain length.
Both terms normalized to [0,1] so they're comparable; without the
relevance normalization the score term (3-7 typical) would swamp path-sim
(always [0,1]) and MMR would degenerate to pure relevance. `MMR_LAMBDA=0.7`:
high-relevance items still dominate, but a top-relevance sibling can lose
its slot to a mid-relevance different-branch alternative when the slot
would otherwise be its 3rd or 4th sibling. **No-op when no overflow** —
if `len(scored) <= limit` the function returns the input unchanged,
so MMR cost only fires when the cap actually binds.
Implementation: `_mmr_rerank` in `core/scripts/tree_match.py`.

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
