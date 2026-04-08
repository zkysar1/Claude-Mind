---
name: tree-reader
description: "Read-only knowledge tree viewer — portable, no scripts needed"
type: system
user-invocable: true
minimum_mode: reader
reads:
  - world/knowledge/tree/_tree.yaml
  - world/knowledge/tree/**/*.md
writes: []
---

# /tree-reader — Read-Only Knowledge Tree Viewer

Portable read-only viewer for a knowledge tree. Uses ONLY built-in tools
(Read, Grep, Glob) — no Python, no scripts, no external dependencies.

## Setup

**In an Ayoai-Mind project** (with `local-paths.conf`): works automatically.

**Standalone** (shared with another agent):
1. Copy this directory to `.claude/skills/tree-reader/`
2. Uncomment and set the TREE_DIR line in Step 0 below
3. Done. Use any sub-command.

## Sub-Commands

```
/tree-reader read <key>      — Read node metadata + .md content
/tree-reader find <query>    — Search nodes by keyword
/tree-reader stats           — Tree health overview
/tree-reader browse [key]    — List children (root if no key)
/tree-reader path <key>      — Show ancestry chain to root
```

## Step 0: Resolve TREE_DIR

Run this before every sub-command to set TREE_DIR.

**Standalone mode** — uncomment ONE line and set your path:
```
# TREE_DIR = "C:/Users/Shared/my-world/knowledge/tree"
# TREE_DIR = "/mnt/sharepoint/knowledge/tree"
```

**Dynamic mode** (default) — resolve from project config:
```
1. Read: {AYOAI_AGENT}/local-paths.conf
   (if AYOAI_AGENT not set: Glob for */local-paths.conf → use first match)
2. Extract the value after "WORLD_PATH=" on the matching line
3. TREE_DIR = {WORLD_PATH}/knowledge/tree
```

### File Path Resolution

Node `file:` values in `_tree.yaml` use the virtual prefix `world/knowledge/tree/`.
To get the real path, replace that prefix with `{TREE_DIR}/`.

Example: `file: "world/knowledge/tree/intelligence/ayoai-core-engine.md"`
becomes `{TREE_DIR}/intelligence/ayoai-core-engine.md`.

## _tree.yaml Structure

The index file has a header (`last_updated`, `tree_growth_log`, etc.) followed
by a `nodes:` section. All nodes are in a **flat dictionary** at 2-space indent
under `nodes:`, regardless of tree depth. Properties are at 4-space indent.

```yaml
nodes:
  node-key:                          # ← 2-space indent (the key to grep for)
    file: world/knowledge/tree/...   # ← 4-space indent (properties)
    depth: 3
    parent: parent-key
    children: [child-a, child-b]     # may span multiple lines
    child_count: 2
    summary: Brief description...
    confidence: 0.85
    capability_level: EXPLOIT        # EXPLORE | CALIBRATE | EXPLOIT
    growth_state: stable
```

## Sub-Command: /tree-reader read <key>

```
1. Grep: pattern="^  {key}:" path="{TREE_DIR}/_tree.yaml"
   → Get the line number
   IF no match → "Node '{key}' not found." STOP

2. Read: path="{TREE_DIR}/_tree.yaml" offset={line} limit=30
   → Parse metadata: file, depth, parent, children, summary,
     confidence, capability_level, growth_state

3. Resolve the file path (replace virtual prefix with TREE_DIR)
   Read: the resolved .md file path

4. Display:
   === NODE: {key} ==========================================
   Depth: {depth} | Parent: {parent} | Confidence: {confidence}
   Capability: {capability_level} | Growth: {growth_state}
   Children: {children or "none (leaf node)"}
   Summary: {summary}
   --- Content ---------------------------------------------
   {.md file content}
   ==========================================================
```

## Sub-Command: /tree-reader find <query>

```
1. Search node keys:
   Grep: pattern="^  [a-z0-9-]*{query}[a-z0-9-]*:" path="{TREE_DIR}/_tree.yaml" -i
   → Nodes whose KEY contains the query term

2. Search summaries:
   Grep: pattern="summary:.*{query}" path="{TREE_DIR}/_tree.yaml" -i -B 12
   → For each match, find the node key: the nearest preceding line
     at 2-space indent matching "^  [a-z][a-z0-9-]*:"

3. Combine results. Rank: exact key match > partial key > summary match.
   Deduplicate by node key.

4. For top 5 results, read each node's line range (offset/limit=25)
   to get summary, depth, capability_level.

5. Display:
   === SEARCH: "{query}" ({N} results) ======================
   1. {key} (depth {d}, {capability_level}, confidence {c})
      {summary}
   2. ...
   ==========================================================
```

## Sub-Command: /tree-reader stats

```
1. Glob: pattern="**/*.md" path="{TREE_DIR}"
   → Count article files (exclude _tree.yaml, _summary.json,
     tree-growth-log.jsonl from the count)

2. Grep: pattern="    depth:" path="{TREE_DIR}/_tree.yaml" output_mode="content"
   → Count occurrences of each depth value to build a histogram

3. Grep: pattern="    capability_level:" path="{TREE_DIR}/_tree.yaml" output_mode="content"
   → Count EXPLORE, CALIBRATE, EXPLOIT

4. Grep: pattern="    growth_state:" path="{TREE_DIR}/_tree.yaml" output_mode="content"
   → Count each growth state (stable, ready_to_decompose, etc.)

5. Display:
   === TREE STATS ============================================
   Total nodes: {count from depth lines, excluding root}
   Article files: {.md file count}

   Depth distribution:
     L0 (root): {n}  L1: {n}  L2: {n}  L3: {n}  L4: {n}  L5: {n}

   Capability levels:
     EXPLORE: {n}  CALIBRATE: {n}  EXPLOIT: {n}  unset: {n}

   Growth states:
     stable: {n}  ready_to_decompose: {n}  ...
   ==========================================================
```

## Sub-Command: /tree-reader browse [key]

```
IF key provided:
  1. Grep: pattern="^  {key}:" path="{TREE_DIR}/_tree.yaml"
     → Read offset/limit=30 → extract children list and summary
     IF no children → "{key} is a leaf node." STOP

ELSE (no key — show root):
  1. Grep: pattern="^  root:" path="{TREE_DIR}/_tree.yaml"
     → Read offset/limit=30 → extract children list

2. For each child in the children list:
   Grep: pattern="^  {child}:" → Read offset/limit=12
   → Extract summary, depth, capability_level, child_count

3. Display:
   === CHILDREN OF: {key or "root"} ==========================
   {child} ({capability_level}, {child_count} children)
     {summary}
   ...
   ==========================================================
```

## Sub-Command: /tree-reader path <key>

```
1. Grep: pattern="^  {key}:" path="{TREE_DIR}/_tree.yaml"
   → Read offset/limit=25 → extract parent field
   IF no match → "Node '{key}' not found." STOP

2. Build chain = [{key}]
   Set current_parent = parent value

3. While current_parent is not "root" and not null:
   Grep: pattern="^  {current_parent}:" → Read offset/limit=10
   → Extract next parent
   Prepend current_parent to chain

4. Display:
   root > {L1} > {L2} > ... > {key}
```
