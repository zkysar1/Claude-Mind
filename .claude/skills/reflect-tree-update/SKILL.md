---
name: reflect-tree-update
description: "Implements the Shared Tree Update Protocol used by every reflection path: identifies the affected knowledge-tree node, updates the target's content and metadata, propagates signals upward through parent nodes, and logs capability events. Use whenever any reflection or encoding writes to a tree node and parents must reflect the change (confidence rollup, capability-level propagation). Called automatically by /reflect-on-outcome (Hypothesis mode) and /reflect-on-self (Patterns mode)."
user-invocable: false
parent-skill: reflect
triggers:
  - "/reflect-tree-update"
conventions: [tree-retrieval]
minimum_mode: assistant
revision_id: "skill-bootstrap-reflect-tree-update-7eae29"
previous_revision_id: null
---

# /reflect-tree-update — Tree Update Protocol

This sub-skill implements the shared Tree Update Protocol used by `/reflect-on-outcome` (Hypothesis mode) and `/reflect-on-self` (Patterns mode). It is invoked after any reflection that updates memory tree nodes, to propagate new insights upward through the tree hierarchy. After EVERY reflection, update the memory tree to propagate new insights upward.

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Step 1: Identify Affected Node (Dynamic Lookup)

Use dynamic lookup to find the right node — never hardcode category->path:
```
node=$(bash core/scripts/tree-find-node.sh --text "{hypothesis.category}" --leaf-only --top 1)
# Returns: {key, score, file, depth, summary, node_type}
target_file = node.file
```
Also update performance nodes as needed:
- Pattern/violation discovery → `bash core/scripts/tree-find-node.sh --text "pattern-extraction" --top 1`
- Calibration insight → `bash core/scripts/tree-find-node.sh --text "calibration-analysis" --top 1`
- Accuracy change → `bash core/scripts/tree-find-node.sh --text "hypothesis-accuracy" --top 1`

## Step 2: Update Target Node (Depth-Generic)

Behavior depends on whether the target node is a leaf or interior node:

**If target is a LEAF node AND insight is substantial (>1 sentence) AND depth < D_max (20):**
1. Create a NEW child .md file under the leaf's directory
2. Register child and convert former leaf to interior:
   echo '{"key":"<child-key>","summary":"<summary>"}' | bash core/scripts/tree-update.sh --add-child <parent-key> --encoding-source reflect-tree-update
   # --encoding-source attributes the new child to the Shared Tree Update Protocol
   # in meta/l1-pick-log.jsonl (S9). Auto-logging fires regardless; this enriches
   # the entry. Pass --encoding-reason "<why this L1>" when there's a 1-line
   # semantic justification worth capturing.
   # The add-child enforces a pre-create dedup gate (Phase 2 curation).
   # On exit 3 (summary overlap with an existing sibling under the new parent),
   # read the rejected JSON from stderr for `sibling_key` and UPDATE that sibling
   # in place with the new insight — do NOT retry with a different key.
   # Exit 2 (exact key match) signals the new-child name collides with a sibling.
   bash core/scripts/tree-update.sh --set <former-leaf-key> node_type interior
3. Move the original leaf content into a child if needed, then add the new child
4. Invoke `/tree validate` to check the structural change

**If target is a LEAF node AND insight is brief (1 sentence or less):**
1. Read the leaf's .md file (use `Edit` to update — never `Write` on existing node files)
2. EXTRACT PRECISION: Scan insight for exact values (numbers, thresholds, code refs,
   formulas, error codes, config values). Build precision items:
     {type, label, value (VERBATIM), unit, context}
   See core/config/conventions/precision-encoding.md for schema and extraction heuristics.
3. IF precision items found:
     Append to "## Verified Values" section (create if missing):
       For each item: - **{label}**: `{value}` {unit} — {context}
4. Add compressed qualitative insight to "Key Insights" section (1-3 sentences)
5. CONSISTENCY SCAN: Re-read the FULL node. If your edit changed a factual claim
   (number, threshold, axis count, status, architecture detail), search the entire
   article for other references to the OLD value. Long articles accumulate references
   to the same fact across multiple sections. Use Edit with replace_all where the
   old string is unambiguous, or fix each occurrence individually. Do not leave
   stale values in historical context — add "(now {new_value})" annotations instead.
6. Recalculate accuracy, sample_size, and confidence, then batch update:
   echo '{"operations": [
     {"op": "set", "key": "<node-key>", "field": "accuracy", "value": <new-value>},
     {"op": "set", "key": "<node-key>", "field": "sample_size", "value": <new-value>},
     {"op": "set", "key": "<node-key>", "field": "confidence", "value": <new-value>},
     {"op": "increment", "key": "<node-key>", "field": "article_count"}
   ]}' | bash core/scripts/tree-update.sh --batch
   (confidence formula: base_accuracy - max(0, (20 - sample_size) / 20) * 0.3)
7. Check growth triggers:
   Read core/config/tree.yaml for decompose_threshold, split_threshold
   line_count = count lines in node .md body (excluding YAML front matter)
   If line_count > decompose_threshold AND depth < D_max:
     bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_decompose
     Invoke `/tree maintain`
   Elif article_count crossed split_threshold:
     bash core/scripts/tree-update.sh --set <node-key> growth_state ready_to_split
     Invoke `/tree maintain`
8. Check if `capability_level` threshold crossed:
   - EXPLORE (<0.30) → CALIBRATE (0.30-0.60) → EXPLOIT (0.60-0.80) → MASTER (>0.80)

**If target is an INTERIOR node:**
1. Navigate to the most specific child matching the insight's topic
2. Recurse: apply this same Step 2 logic to that child node
3. Continue recursing until a leaf is reached

## Step 3: Propagate Upward (Recursive Parent Chain)

If capability_level changed OR major pattern discovered:
```
result=$(bash core/scripts/tree-propagate.sh <node-key>)
# Returns: {source_node, ancestors_updated, capability_changes}
IF result.capability_changes is non-empty:
  For each changed ancestor: Read ancestor.file (.md), update capability map table in body text
If propagation reaches root and triggers domain-level shift:
  bash core/scripts/tree-update.sh --set root summary "<updated>"
```

## Step 4: Log Capability Events

If a capability level changed:
```
Bash: echo '{"date":"<today>","event":"capability_unlock","details":"{topic} upgraded from {old_level} to {new_level}"}' | bash core/scripts/evolution-log-append.sh
```

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is `evolution-log-append.sh` (capability unlock) or the last
`tree-set.sh` propagation write. Never end with a text summary of the update.
