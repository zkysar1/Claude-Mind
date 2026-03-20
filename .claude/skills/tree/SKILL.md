---
name: tree
description: "Knowledge tree operations — read, find, add, edit, set, decompose, distill, maintain, stats, validate"
type: system
user-invocable: false
triggers: []
reads:
  - mind/knowledge/tree/_tree.yaml
  - core/config/memory-pipeline.yaml
  - core/config/tree.yaml
writes:
  - mind/knowledge/tree/_tree.yaml
  - mind/knowledge/tree/**/*.md
  - mind/knowledge/archive/*.md
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [tree-retrieval]
---

# /tree — Knowledge Tree Operations

System skill (NOT user-invocable) that provides all knowledge tree operations as
discoverable sub-commands. Replaces the former `/tree-growth` skill and extends it
with granular read/write operations that any skill can call.

Supports recursive knowledge tree at arbitrary depth up to D_max=6.

## Sub-Commands

```
/tree read <key>                    — Read node content + _tree.yaml metadata
/tree find <query>                  — Find best-matching nodes (wraps tree-find-node.sh)
/tree add <parent> <key> <summary>  — Create a new child node (atomic: _tree.yaml + .md)
/tree edit <key>                    — Read node for editing, update _tree.yaml after
/tree set <key> <field> <value>     — Update _tree.yaml metadata field
/tree decompose <key>               — Break a large node into children
/tree distill <key>                 — Extract actionable kernel, archive narrative
/tree maintain                      — Full batch maintenance (DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE)
/tree stats                         — Tree health overview
/tree validate                      — Consistency check
```

## File Path Convention

Node files live at paths derived from their ancestry in `_tree.yaml`:

```
L1: mind/knowledge/tree/{L1-domain}.md
L2: mind/knowledge/tree/{L1}/{L2-topic}.md
L3: mind/knowledge/tree/{L1}/{L2-topic}/{L3-subtopic}.md
L4: mind/knowledge/tree/{L1}/{L2-topic}/{L3-subtopic}/{L4-detail}.md
L5: mind/knowledge/tree/{L1}/{L2-topic}/{L3}/{L4}/{L5-detail}.md
L6: mind/knowledge/tree/{L1}/{L2-topic}/{L3}/{L4}/{L5}/{L6-detail}.md
```

Path construction rule: compute the child file path from the parent's `file` field
in `_tree.yaml`. For a parent with `file: mind/knowledge/tree/a/b/c.md`, a new child
`d` gets `file: mind/knowledge/tree/a/b/c/d.md` (strip `.md` from parent path, use
as directory, append `{child-slug}.md`).

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Sub-Command: /tree read <key>

Read a node's content and metadata.

```
# Get metadata from _tree.yaml
node=$(bash core/scripts/tree-read.sh --node <key>)
# Returns: {key, summary, file, depth, node_type, capability_level, confidence, article_count, child_count, children, growth_state}

# Read the .md file
Read {node.file}

# Return both metadata and content
```

## Sub-Command: /tree find <query>

Find best-matching nodes by semantic text search.

```
results=$(bash core/scripts/tree-find-node.sh --text "<query>" --top 3)
# Returns: [{key, score, file, depth, summary, node_type}, ...]
# Use --leaf-only to restrict to leaf nodes
# Use --top N to limit results (default 3)
```

## Sub-Command: /tree add <parent> <key> <summary>

Atomic node creation: registers in `_tree.yaml` AND creates the `.md` file.

```
# 1. Register child in _tree.yaml
echo '{"key":"<key>","summary":"<summary>"}' | bash core/scripts/tree-update.sh --add-child <parent>
# This sets: depth, parent, node_type=leaf, capability_level, children=[], article_count=0

# 2. Compute file path from parent's file field
parent_node=$(bash core/scripts/tree-read.sh --node <parent>)
# Strip .md from parent file, use as directory, append {key}.md
child_path = parent_node.file with .md stripped + "/" + key + ".md"

# 3. Create the .md file with YAML front matter
Write {child_path}:
---
topic: <key in title case>
last_update_trigger:
  type: tree_growth
  source: "/tree add"
  session: {current_session}
---
# {Topic Title}

{summary}

# 4. Propagate confidence upward
bash core/scripts/tree-propagate.sh <parent>
```

## Sub-Command: /tree edit <key>

Read a node for editing, then sync `_tree.yaml` metadata after changes.

```
# 1. Get node metadata
node=$(bash core/scripts/tree-read.sh --node <key>)

# 2. Read the .md file for editing
Read {node.file}

# 3. (Caller makes edits via Edit tool)

# 4. After editing, update _tree.yaml metadata
bash core/scripts/tree-update.sh --set <key> last_updated "$(date +%Y-%m-%d)"

# 5. Check growth triggers
Read core/config/tree.yaml for decompose_threshold
line_count = count lines in node .md body (excluding YAML front matter)
If line_count > decompose_threshold AND depth < D_max:
  bash core/scripts/tree-update.sh --set <key> growth_state ready_to_decompose

# 6. Propagate if confidence changed
bash core/scripts/tree-propagate.sh <key>
```

## Sub-Command: /tree set <key> <field> <value>

Update a single `_tree.yaml` metadata field.

```
bash core/scripts/tree-update.sh --set <key> <field> <value>
```

## Sub-Command: /tree decompose <key>

Break a single large node into children. Same as DECOMPOSE operation but targeted.

```
# 1. Read the node
node=$(bash core/scripts/tree-read.sh --node <key>)
Read {node.file}

# 2. Depth guard: abort if depth + 1 > D_max (6)
IF node.depth + 1 > 6: abort "Cannot decompose — at max depth"

# 3. Identify ## sections, cluster into 2-4 groups
# 4. For each cluster:
#    a. Choose kebab-case child name
#    b. Compute child path from parent file field
#    c. Create child .md with front matter
#    d. Move relevant ## sections into child
# 5. Convert parent to interior:
bash core/scripts/tree-update.sh --set <key> node_type interior
bash core/scripts/tree-update.sh --set <key> article_count 0
# 6. Register children:
echo '{"key":"<child-key>","summary":"<summary>"}' | bash core/scripts/tree-update.sh --add-child <key>
# 7. Log:
#    Append to tree_growth_log: {op: DECOMPOSE, node: <key>, children: [...], date, reason}
# 8. Propagate:
bash core/scripts/tree-propagate.sh <key>
```

## Sub-Command: /tree distill <key>

Extract actionable kernel from a node, archive narrative content. Keeps Decision Rules
and Verified Values (the content that drives decisions); archives everything else.
Use when a node has accumulated narrative that inflates context without improving decisions.

```
# 1. Read the node
node=$(bash core/scripts/tree-read.sh --node <key>)
Read {node.file}

# 2. Archive full original content
mkdir -p mind/knowledge/archive
Write mind/knowledge/archive/{key}-{date}.md:
  ---
  distilled_from: <key>
  distilled_date: <date>
  original_line_count: <N>
  original_file: <node.file>
  ---
  {full original .md content}

# 3. Extract actionable kernel
# Keep: YAML front matter, ## Decision Rules, ## Verified Values sections (verbatim)
# Replace: all other ## sections with a 2-3 sentence summary paragraph

# 4. Write distilled content back (Edit, not Write)
Edit {node.file} — replace full body with:
  {front matter unchanged}
  # {Topic Title}
  {2-3 sentence summary of what was removed}
  ## Decision Rules
  {preserved verbatim, or omit if none existed}
  ## Verified Values
  {preserved verbatim, or omit if none existed}

# 5. Update metadata
bash core/scripts/tree-update.sh --set <key> growth_state distilled

# 6. Log
Append to tree_growth_log: {op: DISTILL, node: <key>, date, original_lines, distilled_lines, archived_to}

# 7. Propagate (confidence unchanged, but log the event)
bash core/scripts/tree-propagate.sh <key>
```

## Sub-Command: /tree maintain

Full batch maintenance — performs ALL tree growth operations. This is the same behavior
as the former `/tree-growth` skill.

### Operations (in order)

#### 1. DECOMPOSE — Break apart monolithic leaf nodes

**Trigger**: leaf node where ALL of the following are true:
- `.md` body > `decompose_threshold` (80 lines)
- `depth < D_max` (6)

article_count is NOT a gating condition — if a node is too big, decompose it
regardless of how many articles it has.

If `growth_state` field is missing from the node, check `decompose_threshold` directly
(do not require `growth_state: ready_to_decompose`).

**Steps**:
1. Scan all leaf nodes for decompose triggers:
   Bash: `tree-read.sh --decompose-candidates`
2. For each monolithic leaf:
   a. Read the `.md` file, identify all `##` sections
   b. Cluster `##` sections semantically into 2-4 groups
   c. Compute child directory from parent's file path: strip `.md` extension, use as
      directory name
   d. Create child leaf files in the new subdirectory, one per cluster:
      - Each child gets minimal YAML front matter: `topic`, `last_update_trigger`
        (node_type, depth, parent, capability_level set via `tree-update.sh --add-child`)
      - Move the relevant `##` sections into each child file
   e. Convert parent to interior node:
      bash core/scripts/tree-update.sh --set <parent-key> node_type interior
      bash core/scripts/tree-update.sh --set <parent-key> article_count 0
   f. Register children in `_tree.yaml` using `tree-update.sh --add-child`
   g. Append to `tree_growth_log`: `{op: DECOMPOSE, node, children, date, reason}`
   Cap: process up to `config.max_decompose_per_invocation` candidates (default 7, largest first).

#### 1.5. REDISTRIBUTE — Move interior node body content into children

**Trigger**: interior node (has children) where `.md` body > `decompose_threshold` (80 lines).

**Steps**:
1. Scan interior nodes for large bodies:
   Bash: `tree-read.sh --redistribute-candidates`
2. For each candidate:
   a. Read the interior node's `.md` file, identify all `##` sections
   b. Read each child node's `.md` file to understand its scope
   c. For each `##` section in the parent:
      - Semantic match to existing child? Move content there.
      - No match and depth < D_max? Create new child leaf.
      - No match and depth >= D_max? Leave in parent.
   d. Replace parent body with brief summary (3-5 lines)
   e. Update `_tree.yaml`, log to tree_growth_log
   Cap: process up to `config.max_redistribute_per_invocation` candidates (default 5).

#### 1.75. DISTILL — Concentrate low-utility nodes to actionable kernel

**Trigger**: leaf node where:
- `utility_ratio < distill_utility_threshold` (0.3) AND `retrieval_count >= distill_min_retrievals` (5)
- OR: `line_count > distill_line_threshold` (50) AND `utility_ratio < distill_line_utility_threshold` (0.5)

Thresholds from `core/config/tree.yaml` `pruning` section.

**Steps**:
1. Scan candidates: `Bash: tree-read.sh --distill-candidates`
2. For each candidate (largest line_count first, up to `max_distill_per_invocation`):
   Invoke `/tree distill <key>`
3. Log to tree_growth_log

Note: DISTILL candidates require utility data from Phase 4.26. New nodes with zero
retrievals are not eligible. This operation only fires after sufficient sessions
of utility tracking have accumulated signal.

#### 2. SPLIT — Decompose overloaded nodes

**Trigger**: `node.article_count > config.split_threshold` (currently 3)

**Steps**:
1. Find nodes where `growth_state: ready_to_split`
2. Depth guard: abort if `parent.depth + 1 > D_max` (6)
3. Read all articles, cluster into 2-4 groups
4. Create child nodes (compute paths from parent `file` field)
5. Ensure `min_articles_per_child` (2) is met
6. Convert parent to interior, register children
7. Verify parent `child_count <= K_max` (4)
8. Log to tree_growth_log

#### 3. SPROUT — Add a new node for unmapped content

**Trigger**: `unmapped_categories` has entries

**Steps**:
1. Check `_tree.yaml` `unmapped_categories`
2. Find best parent at any depth
3. Depth guard: abort if `parent.depth + 1 > D_max` (6)
4. Compute child file path from parent's `file` field
5. Create node, register via `tree-update.sh --add-child`
6. Log to tree_growth_log. K_max is soft limit.

#### 4. MERGE — Absorb sparse nodes into siblings

**Trigger**: `node.article_count <= merge_threshold` (1) AND no children

**Steps**:
1. Find sparse childless nodes (skip `growth_state: growing`)
2. Move articles to most similar sibling
3. Remove merged node, delete topic file
4. Log to tree_growth_log

#### 5. PRUNE — Remove empty dead-end nodes

**Trigger**: `article_count == 0` AND `children` empty AND no merge candidate

**Steps**:
1. Find empty childless nodes (skip L1 domains, skip `growth_state: growing`)
2. Archive topic file to `mind/knowledge/archive/`
3. Remove from `_tree.yaml`, log to tree_growth_log

#### 5.5. RETIRE — Remove never-consulted dead nodes

**Trigger**: leaf node where ALL of the following are true:
- `retrieval_count == 0`
- Node has existed for 5+ sessions (`retire_sessions_unused` from config)
- `growth_state` is not `growing`
- NOT an L1 domain node (depth > 1)

**Steps**:
1. Find candidates: leaf nodes with `retrieval_count == 0`, created before session N-5
   (check `tree_growth_log` for creation date, or node's `.md` front matter `last_update_trigger.session`)
2. Archive `.md` content to `mind/knowledge/archive/{key}-retired-{date}.md`
3. Remove node: `bash core/scripts/tree-update.sh --remove-child <parent> <key>`
4. Log to tree_growth_log: `{op: RETIRE, node, date, reason: "never retrieved in N sessions"}`

**Interior node review**: If ALL children of an interior node have `utility_ratio < 0.3`:
- Flag in tree_growth_log: `{op: REVIEW, node, date, reason: "all children low utility"}`
- Do NOT auto-retire interior nodes — the agent assesses whether the branch is dead or underutilized

#### 6. Compress

For any modified nodes, update topic file summaries.
Target compression ratio by depth: `compression_ratio = 10 * 1.5^(depth - 2)`

#### 7. Propagate parents

For any modified node, recursively propagate changes upward via:
```
bash core/scripts/tree-propagate.sh <node-key>
```
Then update parent .md files (capability map tables, summaries).

#### 8. Validate

```
validation=$(bash core/scripts/tree-read.sh --validate)
```
If validation fails: log errors, attempt auto-repair for common issues.

### Completion Criteria (maintain only)

1. All DECOMPOSE candidates processed (or cap reached with remainder logged)
2. All REDISTRIBUTE candidates processed (or cap reached)
3. All SPLIT, SPROUT, MERGE, PRUNE operations evaluated AND executed where triggered
4. Validation passes
5. Tree node count changed if DECOMPOSE or SPLIT candidates existed

**Failure states** (do NOT mark goal completed):
- Candidates identified but not acted on
- "Will decompose later" / "next cycle" deferral
- Report-only output with no file mutations

## Sub-Command: /tree stats

Return tree health overview.

```
stats=$(bash core/scripts/tree-read.sh --stats)
# Returns: {total_nodes, by_depth, interior_count, leaf_count, ...}

Output:
  ## Tree Health
  - Total nodes: {stats.total_nodes}
  - By depth: {stats.by_depth}
  - Interior: {stats.interior_count}, Leaf: {stats.leaf_count}
  - Nodes ready to split: {list}
  - Nodes at risk of merge: {list}
  - Unmapped categories: {count}
```

## Sub-Command: /tree validate

Run consistency check.

```
validation=$(bash core/scripts/tree-read.sh --validate)
# Returns: {valid: true/false, errors: [...], warnings: [...]}

IF validation.valid:
  Output: "Tree validation PASSED"
ELSE:
  Output: "Tree validation FAILED: {errors}"
  Attempt auto-repair for: mismatched child_count, broken parent refs
```

## Constraints

- `K_max` (4): soft limit at all levels (can exceed when justified, log reason)
- `D_max` (6): hard limit — never create nodes beyond depth 6
- Never prune L1 domain nodes
- Always append to `tree_growth_log` for every structural change
- Always verify `min_articles_per_child` (2) before creating split children
- Nodes in `growth_state: growing` are protected from MERGE and PRUNE
- Path construction always computed from parent `file` field — never hardcoded

## Output Format (for /tree maintain)

```
## Tree Growth Report — {date}

### Operations Executed
- DECOMPOSE: {parent} → {children} ({N} sections redistributed)
- REDISTRIBUTE: {parent} → content moved to {children_updated}, {new_children} new children
- SPLIT: {parent} → {children} ({N} articles redistributed)
- SPROUT: {node} created under {parent} ({N} articles)
- MERGE: {node} absorbed into {sibling} ({N} articles moved)
- PRUNE: {node} archived (empty leaf)

### Tree Health
{/tree stats output}

### Growth Log (last 5 entries)
{from tree_growth_log}
```

## Chaining

- **Called by**: any skill that needs tree operations, `/reflect-tree-update` (via sub-commands), `/aspirations` consolidation (via `/tree maintain`)
- **Reads**: `mind/knowledge/tree/_tree.yaml`, node `.md` files, `core/config/memory-pipeline.yaml`, `core/config/tree.yaml`
- **Writes**: `mind/knowledge/tree/_tree.yaml`, node `.md` files, `mind/knowledge/archived/`
- **Does NOT call**: any other skills (pure tree operations)
