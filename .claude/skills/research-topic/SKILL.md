---
name: research-topic
description: "Web research engine — acquires knowledge from external sources and writes findings to memory tree nodes"
user-invocable: false
triggers:
  - "/research-topic"
parameters:
  - name: topic
    description: "Topic to research (required)"
    required: true
  - name: depth
    description: "Research depth: quick, standard, deep (default: standard)"
    required: false
  - name: target-node
    description: "Tree node key to update (optional — auto-detected if omitted)"
    required: false
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [tree-retrieval, experience]
minimum_mode: assistant
---

# /research-topic — Web Research Engine

Researches topics using WebSearch/WebFetch and writes findings directly to memory tree nodes. Called by /aspirations for knowledge goals, /reflect for gap-filling, and /replay for domain transfer.

## Parameters

- `<topic>` (required) — Topic to research
- `--depth quick|standard|deep` — Research intensity (default: standard)
- `--target-node <key>` — Tree node key to update (auto-detected if omitted)

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Scope

Before any external research, determine what we already know and what gaps remain.

```
Bash: retrieve.sh --category {topic-category} --depth shallow
# Returns tree_nodes, experiences, reasoning_bank, guardrails, etc.
# Check if tree nodes already cover this topic (avoid redundant research)
# Check if experiences show prior research attempts

1. If target-node provided: use that node
   Else: node=$(bash core/scripts/tree-find-node.sh --text "{topic}" --leaf-only --top 1)
   # Returns: {key, score, file, depth, summary, node_type}
2. If matching node found (node.score > 0):
     - Read its .md file
     - Note what's already documented, identify gaps
     - Research focus = gaps only (never re-research known content)
3. If no matching node found:
     - Identify best parent for a new node (SPROUT)
     - Compute path: bash core/scripts/tree-read.sh --child-path <parent> <topic-slug>
     - Research focus = broad (building initial understanding)
```

## Step 2: Research

Depth adapts to the goal's needs. The caller sets depth based on effort assessment.

### Quick (1-2 queries, 0-1 fetch)
```
Search: "{topic} {specific_gap}" — single focused query
Optional: WebFetch top result if it looks authoritative
Good for: filling a specific gap, fact-checking, single data points
```

### Standard (2-3 queries, 2-3 fetches)
```
Search 1: "{topic}" — broad overview
Search 2: "{topic} {current_year}" — recent developments
Search 3: "{topic} [angle based on identified gaps]" — targeted gap-fill
WebFetch top 2-3 most relevant results
Good for: building initial understanding, moderate gap-filling
```

### Deep (3-4 queries, 5+ fetches)
```
Search 1: "{topic}" — broad overview
Search 2: "{topic} {current_year}" — recent developments
Search 3: "{topic} analysis" — analytical perspectives
Search 4: "{topic} [domain-specific angle]" — quantitative or technical depth
WebFetch top 5+ results with cross-referencing:
  - Triangulate claims across multiple sources
  - Note where sources agree (high confidence) vs disagree (flag contradiction)
  - Prefer: official docs > academic > well-sourced blogs > forums
Good for: mastering a topic, resolving contradictions, deep expertise building
```

### Novelty & Contradiction Check

Compare all findings against existing node content:
- **Genuinely new** → include in findings
- **Contradicts existing** → flag both perspectives, note the contradiction
- **Reinforces existing** → note reinforcement (increases confidence)
- **Opens new questions** → collect for return to caller

### Source Tracking

For every source consulted, record: `{url, title, accessed: today}`

## Step 3: Write to Tree Node

### If updating existing node:
```
Edit the node's .md file (NEVER Write — file already exists):
  - Add new findings to body (Key Insights or relevant section)
  - Update YAML front matter (content-provenance fields only):
    - sources: append new sources
    - last_update_trigger: {type: "research", source: "<topic>", session: N}
    - temporal_validity.last_confirmed: today
  - Update _tree.yaml via batch:
    echo '{"operations": [
      {"op": "set", "key": "<node-key>", "field": "confidence", "value": <new-value>},
      {"op": "set", "key": "<node-key>", "field": "last_updated", "value": "<today>"},
      {"op": "increment", "key": "<node-key>", "field": "article_count"}
    ]}' | bash core/scripts/tree-update.sh --batch
  - Update entities list if new entities discovered

After writing content, check growth triggers:
  Read core/config/tree.yaml for decompose_threshold, split_threshold
  line_count = count lines in node .md body (excluding YAML front matter)
  If line_count > decompose_threshold AND depth < D_max:
    bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_decompose
    Invoke /tree maintain
  Elif article_count > split_threshold:
    bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_split
    Invoke /tree maintain
```

### If creating new node (SPROUT):
```
1. Register in _tree.yaml (sets parent, depth, capability_level, confidence):
   bash core/scripts/tree-update.sh --add-child <parent-key> < JSON
   JSON: {"key": "<topic-slug>", "file": "world/knowledge/tree/<parent>/<topic-slug>.md",
          "depth": <parent_depth+1>, "summary": "<one-line summary>", "article_count": 1}
2. Create directory if needed (mkdir -p)
3. Write new .md file with front matter:
   ---
   topic: <topic-slug>
   sources:
     - {url: "...", title: "...", accessed: "YYYY-MM-DD"}
   entities: [...]
   last_update_trigger: {type: "research", source: "<topic>", session: N}
   temporal_validity:
     first_observed: "YYYY-MM-DD"
     last_confirmed: "YYYY-MM-DD"
     observation_count: 1
   ---
   # <Topic Title>
   <overview>
   ## Key Insights
   <findings>
   ## Sources
   <source list>
4. Set last_updated in _tree.yaml:
   bash core/scripts/tree-update.sh --set <node-key> last_updated <today>
```

### Archive Research Results as Experience
```
experience_id = "exp-research-{topic-slug}-{date}"
Write <agent>/experience/{experience_id}.md with:
    - Complete web fetch results and source evaluations
    - Key findings with full context (not just compressed insights)
    - Source URLs, reliability assessments, contradictions found
    - What was novel vs what confirmed existing knowledge
echo '<experience-json>' | bash core/scripts/experience-add.sh
Experience JSON:
    id: "{experience_id}"
    type: "research"
    created: "{ISO timestamp}"
    category: "{topic-category}"
    summary: "Research on {topic}: {key finding summary}"
    goal_id: "{goal.id if available}"
    tree_nodes_related: ["{target-node-key}"]
    verbatim_anchors:
        - key: "key-finding-1"
          content: "exact quote or data point from source"
        - key: "key-finding-2"
          content: "exact quote or data point from source"
    content_path: "<agent>/experience/{experience_id}.md"

# Add experience_refs to tree node front matter
Read target tree node .md file
Add experience_id to experience_refs list in YAML front matter (create field if missing)
Edit the tree node to include the updated front matter
```

### Entity Extraction
```
Extract named entities from findings (people, orgs, concepts, metrics, events)
Normalize to lowercase-kebab-case
Bash: world-cat.sh knowledge/tree/_tree.yaml  # entity_index
  If entity exists: add this node's path, increment mention_count
  If entity is new AND total_entities < max_entities: create entry
  If entity is new AND over cap: skip
Update node front matter entities list
Write updated entity_index back to _tree.yaml
```

### Return to Caller
```
Return: {node_key, node_path, new_questions: [...], contradictions: [...]}
Caller handles: tree propagation, journal entry, spark check, tree maintenance triggers
```

## Chaining

| Direction | Skill | How |
|-----------|-------|-----|
| Called by | `/aspirations` | Knowledge goals, gap analysis fallback |
| Called by | `/reflect` | Knowledge gap identified during reflection |
| Called by | `/replay` | Domain transfer generates research question |
| Outputs | Tree node | New or updated `.md` file + `_tree.yaml` updates |
| Calls | `/tree maintain` | Invokes when growth triggers fire (decompose_threshold or split_threshold exceeded) |
| Does NOT do | Journal, spark, propagation | Caller (aspirations) handles all downstream |
