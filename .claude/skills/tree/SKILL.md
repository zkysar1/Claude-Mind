---
name: tree
description: "Knowledge-tree operations entry point dispatching to sub-commands: read (node content), find (search), add (new node), edit (existing node), set (field update), decompose (split large node), split-overcap (verbatim split of a node too big to Read — MUST use for crit3/oversized nodes, never hand-split), distill (summarize), maintain (rebalance), stats (counts & health), validate (schema check). Use whenever the agent needs to read, modify, reorganize, or audit the world knowledge tree — the user says \"add a node about X\", \"update the Y node\", \"reorganize this category\", a Read of a tree node truncates, or reflection/encoding produces a tree write. Canonical entry point; never edit _tree.yaml or node .md files directly."
type: system
user-invocable: false
triggers: [knowledge-tree, tree-maintain, tree-decompose, tree-distill, tree-split, tree-split-overcap, over-cap-node, tree-node, tree-read, tree-add, tree-edit, tree-validate]
reads:
  - world/knowledge/tree/_tree.yaml
  - core/config/memory-pipeline.yaml
  - core/config/tree.yaml
writes:
  - world/knowledge/tree/_tree.yaml
  - world/knowledge/tree/**/*.md
  - world/knowledge/archive/*.md
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
minimum_mode: reader
revision_id: "skill-bootstrap-tree-cd036b"
previous_revision_id: null
---

# /tree — Knowledge Tree Operations

System skill (NOT user-invocable) that provides all knowledge tree operations as
discoverable sub-commands. Replaces the former `/tree-growth` skill and extends it
with granular read/write operations that any skill can call.

Supports recursive knowledge tree at arbitrary depth up to D_max=20.

## Mode Gate for Write Operations
For sub-commands: add, edit, set, decompose, split-overcap, distill, maintain, reparent
Bash: `session-mode-get.sh`
- If mode is `reader`: output "Tree write operations require assistant mode. Run `/start --mode assistant` to enable." STOP.
- If mode is `assistant` or `autonomous`: PROCEED.
Read-only sub-commands (read, find, stats, validate) work in all modes.

## Sub-Commands

```
/tree read <key>                    — Read node content + _tree.yaml metadata
/tree find <query>                  — Find best-matching nodes (wraps tree-find-node.sh)
/tree add <parent> <key> <summary>  — Create a new child node (atomic: _tree.yaml + .md)
/tree edit <key>                    — Read node for editing, update _tree.yaml after
/tree set <key> <field> <value>     — Update _tree.yaml metadata field
/tree decompose <key>               — Break a large node into children
/tree split-overcap <key> --boundaries "..." — Verbatim split of a node too big to Read
/tree distill <key>                 — Extract actionable kernel, archive narrative
/tree maintain                      — Full batch maintenance (DECOMPOSE, REDISTRIBUTE, DISTILL, SPLIT, SPROUT, MERGE, PRUNE, RETIRE)
/tree reparent <node> <new-parent>   — Move node (and subtree) to a new parent
/tree stats                         — Tree health overview
/tree validate                      — Consistency check
```

## File Path Convention

Node files live at paths derived from their ancestry in `_tree.yaml`:

```
L1:  world/knowledge/tree/{L1-domain}.md
L2:  world/knowledge/tree/{L1}/{L2-topic}.md
L3:  world/knowledge/tree/{L1}/{L2-topic}/{L3-subtopic}.md
L4:  world/knowledge/tree/{L1}/{L2-topic}/{L3}/{L4-detail}.md
...
LN:  world/knowledge/tree/{L1}/{L2}/.../{LN-detail}.md   (up to D_max=20)
```

Path construction rule: compute the child file path from the parent's `file` field
in `_tree.yaml`. For a parent with `file: world/knowledge/tree/a/b/c.md`, a new child
`d` gets `file: world/knowledge/tree/a/b/c/d.md` (strip `.md` from parent path, use
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
# 1. Get parent metadata (for file path computation in step 3)
parent_node=$(bash core/scripts/tree-read.sh --node <parent>)
# Strip .md from parent file, use as directory, append {key}.md
child_path = parent_node.file with .md stripped + "/" + key + ".md"

# 2. Create the .md file with YAML front matter.
#    Creator owns the initial mechanical fields (last_updated, trigger.session,
#    trigger.source, trigger.type). Layer A's hook keeps them fresh on
#    subsequent edits but does NOT initialize them — the file isn't yet
#    registered in _tree.yaml when the Write fires, so Layer A early-exits.
Write {child_path}:
---
topic: <key in title case>
last_updated: '<today>'
poignancy: <1-10>   # g-306-26: LLM-rated importance (readability copy; the
                    # scoring value is the index poignancy set in step 3 — keep identical)
last_update_trigger:
  type: tree_growth
  source: "/tree add"
  session: {current_session}   # the FULL $MIND_SID, not a truncated prefix —
                               # tree-edit-since.py:97 compares this EXACTLY against
                               # MIND_SID to decide "did I encode this, or did a
                               # partner?". A shortened value silently attributes your
                               # own node to another session: --tree-updated is then
                               # IGNORED at state-update close and the node reads as a
                               # partner's in every later attribution audit. Measured
                               # 2026-07-30 (g-335-546) — an 8-char prefix here cost
                               # the iteration its tree-encoding credit.
---
# {Topic Title}

{summary}

# 3. Register child + propagate (atomic single write).
#    --encoding-source attributes this write to the /tree add path in the
#    L1 pick log (S9). Auto-logging fires regardless; this enriches the
#    entry. Pass --encoding-reason "<short why>" when the call site has
#    semantic context worth capturing (a one-line "why this L1").
#    poignancy (g-306-26 producer, BRD Gap 1a / Generative Agents 2304.03442):
#    include an LLM-rated 1-10 importance value in the child so the new node's
#    INDEX poignancy is set AT CREATION (the g-306-08 retrieve blend reads the
#    index value; absent → null and the blend no-ops). Keep it identical to the
#    front-matter poignancy from step 2. Rubric: 1-3 routine · 4-6 useful ·
#    7-8 pivotal · 9-10 mission-altering — how durable + impactful for FUTURE retrieval.
echo '{"operations": [
  {"op": "add-child", "key": "<parent>", "child": {"key": "<key>", "summary": "<summary>", "poignancy": <1-10>}},
  {"op": "propagate", "key": "<parent>"}
]}' | bash core/scripts/tree-update.sh --batch --encoding-source tree-add
```

## Sub-Command: /tree edit <key>

Read a node for editing, then perform the SEMANTIC re-evaluation that
Layer A cannot do mechanically.

Three-layer consistency model:
- **Layer A** (`tree-sync-check.sh` PostToolUse hook → `tree-front-matter-sync.py`)
  auto-fixes mechanical fields on EVERY tree-node Edit/Write, regardless of
  whether `/tree edit` was the entry point: front-matter `last_updated`,
  `last_update_trigger.session`, `last_update_trigger.source` (filled from
  in-flight goal if missing), and `_tree.yaml.<key>.last_updated`.
- **Layer B** (this Step 4) handles semantic fields that need LLM judgment:
  `_tree.yaml.summary`, front-matter `topic`, `last_update_trigger.type`.
- **Layer C** (`guard-503`) reminds direct-Edit callers (those who skip
  `/tree edit`) to run the semantic walk anyway when the body change was
  material.

```
# 1. Get node metadata
node=$(bash core/scripts/tree-read.sh --node <key>)

# 2. Read the .md file for editing
Read {node.file}

# 3. (Caller makes edits via Edit tool)
#    Layer A's PostToolUse hook fires automatically here, mutating
#    last_updated + trigger.session + trigger.source. Do NOT manually
#    re-stamp those fields — the hook owns them.

# 4. Layer B: SEMANTIC re-evaluation. Walk these three questions before
#    declaring the edit complete. Each is LLM-judgment that no script can
#    replace. Skip any question whose answer is genuinely "no change" —
#    but answer all three.

#    Q1 — TOPIC DRIFT
#      Re-read the body's H1 and the first paragraph.
#      Does front-matter `topic` still describe the same thing?
#      If body's scope shifted (e.g., was about "X retry logic", now covers
#      "X retry + Y fallback"), Edit the .md file's front-matter topic line
#      to match. Layer A will not touch topic.

#    Q2 — SUMMARY DRIFT
#      Read the current _tree.yaml summary:
#        bash core/scripts/tree-read.sh --node <key>
#      The `summary` field is what `retrieve.sh` shows in previews and
#      what the agent matches against during retrieval. If body added,
#      removed, or replaced a major topic, the summary likely no longer
#      describes the gist. Decision rule: would a future reader retrieving
#      by this summary be misled about what they'll find? If yes, refresh:
#        bash core/scripts/tree-update.sh --set <key> summary "<new one-line>"
#      Keep summaries dense and specific — they drive retrieval ranking.

#    Q3 — EDIT CATEGORIZATION
#      Set front-matter `last_update_trigger.type`. Layer A leaves this
#      blank because it cannot infer WHAT KIND of edit you made.
#      Common values:
#        - knowledge_reconciliation — corrected facts based on new evidence
#        - direct_correction       — typo, polish, formatting
#        - decompose               — split sections out into children
#        - distill                 — extracted kernel, archived narrative
#        - tree_growth             — added new content to a growing area
#        - audit_followup          — encoded an audit finding
#      This field drives session_artifacts_count.py — getting it right
#      matters for the productivity gate (maintenance vs real encoding).
#      Edit the .md file's front-matter to add/update the type line.

# 5. Check structural growth trigger (K=D=4 retrieval-locality contract, g-306-13)
# Growth is STRUCTURAL now, not line-count (outcome 3 — the old
# `line_count > decompose_threshold` trigger is retired). Flag a node for
# regroup when its direct children exceed K_max; retrieval-subtree leaf overflow
# (> K_max^(D_retrieval-1), derived from config) is computed at maintain time by
# `tree-read.sh --decompose-candidates` / `--validate`.
Read core/config/tree.yaml for K_max
If child_count > K_max:
  bash core/scripts/tree-update.sh --set <key> growth_state ready_to_decompose

# 6. Propagate if confidence changed
bash core/scripts/tree-propagate.sh <key>
```

## Sub-Command: /tree set <key> <field> <value>

Update a single `_tree.yaml` metadata field.

```
bash core/scripts/tree-update.sh --set <key> <field> <value>
```

### Durable per-node judgment markers

Three fields record a decision so it stops being re-derived every sweep. All are
plain `--set` writes; JSON arrays store as real YAML lists.

| Field | Suppresses | Set it when |
|---|---|---|
| `maintain_exempt` (list) | `distill` and/or `redistribute` candidacy | the node is coherent / intentionally wide — see the coherence gates in DISTILL and REGROUP |
| `decompose_exempt` (bool) | decompose candidacy | the node coheres as ONE document (rb-94) |
| `saturated_topics` (list of phrases) | **goal FILING on those topics** | the node's frontier is measured out — another measurement would reproduce an already-encoded number |

`saturated_topics` is the odd one out and the reason it is worth naming here:
the other two suppress TREE MAINTENANCE, this one suppresses WORK ORIGINATION.
It is read by `core/scripts/gates/goal_duplication.py`'s
`saturated_frontier` check, which runs as step 7 of every goal filing, so the
declaration reaches an agent BEFORE they file rather than only after they open
the node.

```
bash core/scripts/tree-update.sh --set <key> saturated_topics \
  '["cognitive load","primitives amortization"]'
```

Write each phrase as the distinctive words a matching goal would contain; ALL of
a phrase's non-stopword tokens must appear in the goal's title+description, AND
the goal must carry a measurement verb (measure / quantify / audit / baseline /
benchmark), before the gate refuses. Prefer several narrow phrases over one
broad one — a phrase whose only distinctive token is generic (`environment`)
will fire on unrelated work.

Do NOT pair this with another prose warning in the node body. Five agents
re-measured `multi-env-cognitive-load-baseline.md` past four escalating body
warnings, because a warning in the destination is reachable only by someone who
already opened the destination. That is the failure this field replaces
(g-115-4703, guard-2437).

## Sub-Command: /tree decompose <key>

Break a single large node into children. Same as DECOMPOSE operation but targeted.

```
# 1. Read the node
node=$(bash core/scripts/tree-read.sh --node <key>)
Read {node.file}

# 1.5. COHERENCE GATE (rb-94: triage by coherence, not size). The size
#      threshold over-flags long-form domain nodes. Before clustering, judge:
#      do the ## sections cohere as ONE document (catalog with catalog-wide
#      governance, single-source analysis, thesis, design spec with shared
#      components, living list), or are they multiple INDEPENDENT topics that
#      would each retrieve on their own? If COHERENT, do NOT decompose —
#      mechanical splitting fragments retrieval and orphans cross-cutting
#      sections. Record the judgment so the node never re-surfaces as a
#      false-positive candidate:
#        bash core/scripts/tree-update.sh --set <key> decompose_exempt true
#      and STOP (get_decompose_candidates now skips decompose_exempt nodes).
#      Only proceed to step 2+ when the node is genuinely INCOHERENT.

# 2. Depth guard: abort if depth + 1 > D_max (6)
IF node.depth + 1 > 6: abort "Cannot decompose — at max depth"

# 3. Identify ## sections, cluster into 2-4 groups
# 4. For each cluster:
#    a. Choose kebab-case child name
#    b. Compute child path from parent file field (strip .md, use as dir)
#    c. Create child .md with front matter
#    d. Move relevant ## sections into child
# 5-8. Atomically convert parent + register children + propagate (ONE batch call):
# ⚠ THE node_type SET BELOW LOOKS REDUNDANT AND IS LOAD-BEARING — DO NOT DELETE.
# batch add-child already auto-flips a leaf parent to interior on its own
# (tree.py cmd_batch, g-115-1437, pinned by
# test_batch_add_child_flips_parent_leaf_to_interior), so this op changes no
# state and reads like dead code a cleanup should remove. It is what
# core/scripts/_growth_log.py RECOGNIZES as "this batch is a decompose"
# (g-115-3210) — an explicit set-interior plus add-child on the same key. The
# auto-flip is invisible to that check because it mutates the tree, not the
# ops list. Remove this line and DECOMPOSE rows silently stop being written,
# exactly as they silently were not written for 3.7 months before g-115-3210.
# Pinned by test_growth_log.py::test_producer_batch_shape_still_carries_the_signature.
echo '{"operations": [
  {"op": "set", "key": "<key>", "field": "node_type", "value": "interior"},
  {"op": "set", "key": "<key>", "field": "article_count", "value": 0},
  {"op": "add-child", "key": "<key>", "child": {"key": "<child-1>", "summary": "<summary-1>"}},
  {"op": "add-child", "key": "<key>", "child": {"key": "<child-2>", "summary": "<summary-2>"}},
  {"op": "propagate", "key": "<key>"}
]}' | bash core/scripts/tree-update.sh --batch
# 9. Log: NOTHING TO DO — the batch above writes the tree_growth_log DECOMPOSE
#    row itself (core/scripts/_growth_log.py, g-115-3210). The batch's own
#    shape IS the signature: `set <key> node_type interior` + add-child on that
#    same key. Do NOT hand-append a row; you would duplicate the script's.
#    This step used to read "Append to tree_growth_log: {op: DECOMPOSE, ...}"
#    and was honour-system — it produced 8 rows on 2026-04-04 and nothing for
#    the next 3.7 months, while decomposes kept happening.
```

## Sub-Command: /tree distill <key>

Extract actionable kernel from a node, archive narrative content. Keeps Decision Rules
and Verified Values (the content that drives decisions); archives everything else.
Use when a node has accumulated narrative that inflates context without improving decisions.

```
# 1. Read the node
node=$(bash core/scripts/tree-read.sh --node <key>)
Read {node.file}

# 1.5. COHERENCE GATE (rb-94 / guard-896 / g-115-1534: triage by coherence,
#      not utility). util_ratio over-flags coherent low-RETRIEVAL nodes the same
#      way article_count=0 over-flagged content-bearing nodes (g-115-1534).
#      Before archiving anything, body-read the node and judge: do the ##
#      sections cohere as ONE tight document (clean Decision Rules + Verified
#      Values, narrative that supplies load-bearing context) flagged purely by
#      LOW RETRIEVAL? Low retrieval signals under-instrumentation / niche value,
#      NOT bloat — distilling it DESTROYS context. If COHERENT, do NOT distill —
#      record the durable judgment so it never re-surfaces as a utility candidate
#      (get_distill_candidates skips "distill" in maintain_exempt — crit1/crit2 only):
#        bash core/scripts/tree-update.sh --set <key> maintain_exempt '["distill"]'
#      (merge, don't clobber: if the node already carries a maintain_exempt list,
#       set the UNION, e.g. '["redistribute","distill"]')
#      and STOP. Only proceed to step 2+ when the node has genuine NARRATIVE
#      BLOAT (sections that inflate context without improving decisions).
#      Default to COHERENT when ambiguous — destroying context is worse than
#      leaving a slightly-long coherent node. EXCEPTION: the oversized-append-grown
#      structural trigger (trigger=oversized_append_grown / crit3, g-115-1570) is
#      NOT exempt — maintain_exempt:["distill"] suppresses only the utility
#      triggers, never crit3. But crit3 does NOT go straight to the rollup:
#      it takes the SHAPE FORK at 1.6 FIRST.

# 1.6. SHAPE FORK (guard-2109 / rb-6055 — MANDATORY before ANY destructive
#      reduction on a crit3 candidate). The crit3 trigger fires on SIZE and
#      never tests SHAPE, so it collects two shapes that keep-newest-N damages.
#      Measured: 3 of 3 crit3 nodes hand-examined in ONE maintain run were three
#      DIFFERENT shapes — the mixed case is the normal case, not the edge case.
#      Enumerate heading structure and per-section line spans FIRST — use RANGED
#      Reads (offset/limit); a full Read truncates exactly the node this fork is
#      for — then route on shape, never on size:
#        grep -n '^#\{1,3\} ' {node.file}     # headings + line numbers => spans
#
#        (a) APPEND-GROWN SERIES — dated sections, newer supersedes older,
#            roughly UNIFORM section size.
#            -> keep-newest-N is correct. Proceed to step 2.
#
#        (b) CATALOG — sections are orthogonal and PERMANENT (per-lane,
#            per-component, per-failure-mode) and early sections are REFERENCED
#            BY later ones. keep-newest-N DELETES the foundational entries, and
#            the loss is unrecoverable in place (node .md bodies are written
#            UNFENCED). Correct operation is a SPLIT on the natural partition
#            key, keeping index + governance sections in the parent:
#            -> take the SPLIT-OVERCAP PATH FORK (§ split-overcap step 0 —
#               boundaries are INPUT, never inferred) and STOP. Do NOT distill.
#
#        (c) INVERTED-BLOAT SERIES — genuinely a series, but the NEWEST sections
#            are far LARGER than the old ones, so the cheap dense history is all
#            that keep-newest-N removes while nearly all the cost is retained.
#            -> NO CUT. Report the finding as per-entry size discipline and route
#               it to the node's active writer (board findings post). STOP.
#
#      SOLE-WRITER RULE: a per-agent series shard has ONE owner. Never rewrite
#      another agent's sole-writer node wholesale — route the finding to its
#      writer instead. This holds even when the shape is (a) and the rollup
#      would otherwise be correct.
#
#      Default when ambiguous: treat as (b)/(c) and do NOT cut. A missed rollup
#      costs one cycle; a wrong one destroys referenced content with no fence
#      under it to recover from.
#
#      WHY THIS LIVES IN THE PSEUDOCODE and not only in the guardrail: the same
#      lesson was already carried behaviorally and a near-repeat still occurred
#      one day later on a different agent, because the pseudocode kept inviting
#      the error. A guardrail cannot outvote the instrument it guards.

# 2. Archive full original content
mkdir -p "$WORLD_DIR/knowledge/archive"    # WORLD_DIR from _paths.sh — NEVER use bare world/
Write $WORLD_DIR/knowledge/archive/{key}-{date}.md:
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

## Sub-Command: /tree split-overcap <key> --boundaries "{##-heading}|{##-heading}|..."

Verbatim split of a node TOO BIG TO READ (the Read tool truncates it). The
mechanical sibling of `/tree decompose`: decompose is SEMANTIC clustering of a
readable node; split-overcap moves sections VERBATIM because you cannot
re-organize what you cannot fully read. Forged from gap-051 (g-115-3994) after
three hand-runs (directive-lane-compliance 07-29, directive-lane-series 07-30
x2); both predicted silent failure modes fired live in g-115-4069 and are
script- or checklist-blocked below.

```
# 0. PATH FORK (load-bearing — it decided the third live run; not hedging).
#    Boundaries are INPUT, never inferred. The mechanics generalize; the
#    BOUNDARY choice does not. If the node has NO natural partition key
#    (## sections per agent / per component), or cross-cutting content sits
#    NESTED inside one partition (an h3 fleet-level section inside one
#    agent's h2 — splitting on the h2 boundary would bury it in one shard),
#    this is a DISTILL, not a split — use /tree distill and STOP.
#    Decompose's coherence gate (rb-94) applies here unchanged.

# 1. MEASURE via a full Read's truncation notice — NEVER a byte/char
#    heuristic (guard-1478, guard-1422 clause 3). Normal trigger: crit3
#    oversized_append_grown from /tree maintain.

# 2. BACK UP — your OWN sha1-verified copy. Do NOT assume .history backs a
#    tree node (measured absent for tree nodes in the g-115-4069 run).
cp {node.file} agents/{agent}/temp/{key}-presplit-$(date +%Y%m%dT%H%M%S).md
sha1sum {node.file} {backup}   # must match before any surgery

# 3. SPLIT on the given boundaries, VERBATIM. Use RANGED Reads
#    (offset/limit) to move content the cap hides; moved sections must sum
#    to the original line count.

# 4. WRITE children (front matter each; kebab-case keys under the parent's
#    directory).

# 5. INTEGRITY DIFF (MANDATORY — silent failure mode #1: skipping it loses
#    rows with no error). MUST use the companion verifier, never an ad-hoc
#    identifier/header check — an identifier-level diff measured 0 orphans
#    of 18 while the content-level check found 2 whole sections missing
#    (g-115-4069):
py -3 core/scripts/tree-split-verify.py --original {backup} \
    --outputs {child1} {child2} ... {stub-or-index} [{archive}]
#    Decoration-tolerant, prints denominators ("lines=N missing=M").
#    exit 1 = content lost: STOP, restore from backup, re-split.
#    exit 2 (VACUOUS, denominator 0) is NOT a pass — fix the inputs.

# 6. REGISTER children in _tree.yaml with ABSOLUTE paths — a relative path
#    raises 'not under any configured root' from the own-cloud lock backend
#    (guard-1331). Use the decompose batch shape (ONE tree-update.sh --batch
#    with set node_type interior + add-child per child + propagate) — that
#    exact shape IS the growth-log DECOMPOSE signature (g-115-3210).

# 7. STUB + PARENT RECONCILE (MANDATORY — silent failure mode #2: skipping
#    it leaves the parent instructing agents to append to the file just
#    emptied, which re-grows it within one fire; measured live).
#    a. Leave a stub/index at the old key so an existing --node read does
#       not return nothing.
#    b. Re-evaluate EVERY IMPERATIVE carried into the stub against the
#       post-split topology: verbatim is the right default for CONTENT and
#       the WRONG default for INSTRUCTIONS. Strike stale imperatives
#       ("append HERE") with a dated SUPERSEDED note — never delete
#       (provenance survives).
#    c. Update front matter (last_updated + last_update_trigger) on the
#       stub; the verifier deliberately excludes front matter + the H1
#       title, so THIS step owns their correctness.
```

## Sub-Command: /tree maintain

Full batch maintenance — performs ALL tree growth operations. This is the same behavior
as the former `/tree-growth` skill.

### Operations (in order)

> **Retrieval-locality contract — K_max=40 (user directive 2026-07-14,
> supersedes the K=4 half of g-306-13 / board decision
> msg-20260619-075228-bravo-086; see decisions-board amendment of
> 2026-07-14).** DECOMPOSE and REGROUP remain STRUCTURAL, not line-count
> (outcome 3 — the `decompose_threshold` line trigger is retired). The
> original K=4 bound (Zhong et al., PRL 134:237402, 2025: recall saturates
> at `K_max^(D_retrieval-1)` leaves under a retrieval entry point) modeled
> hierarchical-descent retrieval; this deployment's `retrieve.sh`
> flat-scores nodes, the fleet never actioned a count-based regroup in
> practice (observed max fan-out 26 vs the 4-cap), and wide-shallow shapes
> shorten file paths (Windows MAXPATH). K_max and the derived leaf cap live
> in `core/config/tree.yaml` (single source of truth — tree.py derives
> `K_max^(D_retrieval-1)`; do NOT hardcode either number here). With
> K_max=40 the count trigger is effectively dormant: regroup/decompose fire
> on **semantic incoherence** (a hub whose children span unrelated topics),
> judged opportunistically when maintain is already touching the subtree.
> Both operations introduce intermediate category nodes and REPARENT
> existing children under them (moving nodes, preserving content + history)
> — they do NOT split `.md` sections. CAUTION when reparenting: git-tracked
> framework files (e.g. `.claude/rules/path-resolution.md`) hardcode some
> node paths — `tree-inbound-ref-fix.py` repairs tree-node bodies only;
> grep `.claude/` for any moved path.

#### 1. DECOMPOSE — Restore retrieval locality (subtree leaf overflow)

**Trigger** (STRUCTURAL): a non-root node whose retrieval subtree holds more
than the leaf cap (`K_max^(D_retrieval-1)`, derived from config) leaves. Reported by
`tree-read.sh --decompose-candidates` with `reason: leaf_overflow`,
`recommended_action: decompose`, plus `subtree_leaves` / `depth` / `child_count`.

**Steps**:
1. Scan: `Bash: tree-read.sh --decompose-candidates`
2. For each candidate (largest `subtree_leaves` first), restore the derived leaf-cap bound
   by introducing intermediate category nodes that partition the children into
   <=K_max balanced semantic groups:
   a. Read the node and its children to find the natural clustering.
   a.5. COHERENCE GATE (rb-94: triage by coherence, not size). Before
        clustering, judge whether the node's content coheres as ONE document
        (catalog with catalog-wide governance, single-source analysis, thesis,
        design spec with shared components, living list) or contains multiple
        INDEPENDENT topics. If COHERENT, do NOT decompose — set
        `bash core/scripts/tree-update.sh --set <key> decompose_exempt true`
        and SKIP to the next candidate (the node is permanently exempted from
        decompose candidacy; this is the durable fix for the long-form
        false-positive churn rb-94 diagnosed). Only continue to b. when the
        node is genuinely INCOHERENT. Default to COHERENT when ambiguous —
        fragmenting a coherent doc is worse than leaving a large coherent one.
   b. Choose <=K_max kebab-case intermediate category names.
   c. Create the intermediate nodes as children of `<key>` (add-child), then
      REPARENT the existing children under the appropriate category via
      `bash core/scripts/tree-update.sh` reparent ops. Use `--encoding-source
      tree-maintain-decompose` so the L1 pick log (S9) attributes the new
      intermediates to structural maintenance, not agent-judgment SPROUTs.
   c2. **Repair inbound cross-refs (g-115-1830)**: after this candidate's
      reparents AND their physical file moves are applied (all bodies now
      readable at their new paths), rewrite any OTHER node's backtick-quoted
      body ref that pointed at a moved child's OLD path — else it silently
      dangles until a later `/tree validate` flags it (g-115-1419), and every
      retrieval through it misses in the meantime. Collect this candidate's
      reparent `file_moves` into one JSON array and apply:
      `echo '[{"old":"world/...","new":"world/..."}, ...]' | py -3 core/scripts/tree-inbound-ref-fix.py --apply`.
      The output lists every ref rewritten (node + old_ref -> new_ref) — surface
      it in the maintenance log. Backtick-scoped, form-preserving, idempotent;
      never mutates the moved node itself, only inbound refs.
   d. **Depth guard / grandfather**: skip any reparent that would push a child
      past `D_max` (20). If the subtree is already deep (grandfathered
      depth-5..8) and cannot be regrouped without exceeding the ceiling, LEAVE
      it — report-only, do not force-migrate. For a node that is intentionally
      broad by design (not just deep), record a durable `maintain_exempt`
      `["decompose"]` judgment so it stops re-surfacing every sweep — see the
      Durable exemption note (d2) under REGROUP below.
   e. Propagate confidence upward (`propagate` op).
   f. Append to `tree_growth_log`: `{op: DECOMPOSE, node, intermediates, date, reason: leaf_overflow}`
   Cap: process up to `config.max_decompose_per_invocation` candidates (default
   50, largest first — single source of truth core/config/tree.yaml).

#### 1.5. REGROUP — Cap fan-out at K_max (formerly REDISTRIBUTE)

**Trigger** (STRUCTURAL): an interior node with more than `K_max` (40, per core/config/tree.yaml) children.
Reported by `tree-read.sh --redistribute-candidates` with `reason: k_overflow`,
`recommended_action: regroup`, plus `child_count` / `children` / `depth`.

**Steps**:
1. Scan: `Bash: tree-read.sh --redistribute-candidates`
2. For each candidate (largest `child_count` first):
   a. Cluster the >K_max children into <=K_max semantic groups (balanced
      regrouping on overflow).
   b. Create one intermediate category node per group as a child of `<key>`.
   c. REPARENT each existing child under its group's intermediate node
      (`tree-update.sh` reparent ops — moves nodes, preserves content/history).
   c2. **Repair inbound cross-refs (g-115-1830)**: after this candidate's
      reparents AND their physical file moves are applied (all bodies now
      readable at their new paths), rewrite any OTHER node's backtick-quoted
      body ref that pointed at a moved child's OLD path — else it silently
      dangles until a later `/tree validate` flags it (g-115-1419). Collect this
      candidate's reparent `file_moves` into one JSON array and apply:
      `echo '[{"old":"world/...","new":"world/..."}, ...]' | py -3 core/scripts/tree-inbound-ref-fix.py --apply`.
      The output lists every ref rewritten (node + old_ref -> new_ref) — surface
      it in the maintenance log. Backtick-scoped, form-preserving, idempotent;
      never mutates the moved node itself, only inbound refs. (This is the
      systemic auto-fix for the exact breakage the g-115-398 regroup hit — 8
      inbound refs across 7 nodes, then fixed by hand.)
   d. **Depth guard / grandfather**: skip reparents that would exceed `D_max`
      (20); leave grandfathered deep subtrees in place rather than force-migrate.
   d2. **Durable exemption (coherence judgment, g-115-1648)**: if a candidate is
      intentionally wide — a coherent interior hub (overview, chronological
      build/research log) or a grandfathered taxonomy cut that would be
      FRAGMENTED by a regroup — record that one-time judgment so it stops
      re-surfacing every sweep instead of re-deciding "leave it" each pass:
      `bash core/scripts/tree-update.sh update --set <key> maintain_exempt '["redistribute"]'`.
      `get_redistribute_candidates` then skips it (skip_reason:
      `redistribute_exempt`). Use sparingly — exemption is a durable assertion
      that the node's width is correct by design, NOT a way to silence backlog.
      To later re-include the node, set `maintain_exempt '[]'`. Symmetric
      `"decompose"` exemption applies to the DECOMPOSE step above.
   e. Replace the parent body with a brief routing summary; propagate.
   f. Log to tree_growth_log: `{op: REGROUP, node, groups, date, reason: k_overflow}`
   Cap: process up to `config.max_redistribute_per_invocation` candidates (default 5).

#### 1.75. DISTILL — Concentrate low-utility nodes to actionable kernel

**Trigger**: leaf node where:
- `utility_ratio < distill_utility_threshold` (0.3) AND `retrieval_count >= distill_min_retrievals` (5)
- OR: `line_count > distill_line_threshold` (50) AND `utility_ratio < distill_line_utility_threshold` (0.5)
- OR: READ-CAP arms — `est_tokens >= distill_token_ratio * distill_token_cap`
  (0.8 × 25000). These split on whether the node is append-grown:
  `trigger == oversized_append_grown` (>= `distill_refresh_min_sections` dated
  sections) and `trigger == oversized_not_append_grown` (fewer). BOTH are
  returned by `--distill-candidates`; only the FIRST is a distill. See step 2.a0.

Thresholds from `core/config/tree.yaml` `pruning` section.

**Steps**:
1. Scan candidates: `Bash: tree-read.sh --distill-candidates`
2. For each candidate (largest line_count first, up to `max_distill_per_invocation`):
   a0. ACTION ROUTING (g-115-4058) — read the candidate's `recommended_action`
      FIRST, before the coherence gate. This list is NOT all distills: a node
      with `trigger == oversized_not_append_grown` carries
      `recommended_action: regroup`, because it is too big to Read but has too
      few dated sections for the rb-2085 archive + keep-newest-N rollup to have
      anything to roll up. For those, do NOT invoke `/tree distill` — route them
      to the SPLIT-OVERCAP PATH FORK (§ split-overcap step 0) and SKIP to the
      next candidate. Only `recommended_action: distill` proceeds to 2.a below.

      DO NOT route these to REGROUP (§1.5) — that arm is DORMANT BY DESIGN and
      cannot fire on them (g-115-4147, measured 2026-07-30 alpha cc-04). REGROUP
      triggers on `child_count > K_max`, and `core/config/tree.yaml:9` raised
      K_max 4 -> 40 by user directive 2026-07-14, stating verbatim: "count-based
      DECOMPOSE/REGROUP pressure is effectively retired; regroup on SEMANTIC
      incoherence only." Measured consequence: `tree-read.sh
      --redistribute-candidates` returns [] against the whole 1299-node tree,
      and all four live arm members report `child_count: None` — two are LEAVES,
      which have no children to regroup at all, so the old routing was
      categorically wrong for them rather than merely unmet. The cost was not
      theoretical: the arm holds the two MOST-RETRIEVED nodes in the tree
      (framework-guardrails-and-gates, 438 retrievals / 30,633 est. tokens;
      product-world-model, 247 / 29,535), both past the ~25k Read cap — so the
      read-cap arm was silently exempting exactly the nodes the fleet reads most.

      FIRST CHECK est_tokens >= 26413 — this arm's trigger is PROACTIVE and
      splitting an under-cap node is a net harm (guard-2006 / rb-5894). The arm
      fires at `token_trigger` = 0.8 * 25000 = 20000, i.e. deliberately BEFORE the
      cap, which is free for the distill arm (an early rollup is non-destructive)
      and NOT free here: structural surgery on an already-readable node fragments
      coherent content and buys nothing. est_tokens = chars/2.3 is the LOW end of
      the measured 2.31-2.43 band, so it runs high by up to ~5.7% and a node
      reported at 100-106% of cap may be UNDER it. 26413 is that band's floor
      (25000 * 2.43 / 2.3) — at or above it the node is over cap at ANY ratio.
        - est_tokens >= 26413        -> over cap for certain: take the FORK below.
        - est_tokens <  25000        -> readable in one Read: the arm's "too big to
          Read" premise is FALSE for it. Do NOT do structural surgery — fall
          through to the 2.a coherence gate and let it distill on merit or mark
          `maintain_exempt`.
        - 25000 <= est_tokens < 26413 -> UNKNOWN, not over. Re-measure with a real
          tokenizer, or treat as under (conservative for a destructive-shaped op).
      Measured 2026-07-30: of the 4 live arm members, checker-input-assumption-
      defects (21680 est / 50156 chars) is UNDER cap and must NOT be split; the
      other three (27820 / 29535 / 30633) clear 26413 and take the fork.

      THE FORK IS THE ROUTE — do not shorten this to "call split-overcap". Its
      step 0 is load-bearing: BOUNDARIES ARE INPUT, NEVER INFERRED. Enumerate the
      node's `##` headings with RANGED Reads (offset/limit — the cap hides them
      from a full Read), then branch:
        - a natural partition key exists (## sections per agent / per component)
          -> `/tree split-overcap <key> --boundaries "..."`
        - NO natural partition key, OR cross-cutting content sits NESTED inside
          one partition -> that is a DISTILL, not a split: `/tree distill`, STOP.
      A leaf with 0 children normally takes this second branch. Skipping the fork
      and splitting on an inferred boundary buries cross-cutting content in one
      shard, which the fork exists to prevent.
      Why this step is load-bearing: the read-cap size test fires INDEPENDENTLY
      of append-grown-ness (that decoupling is the whole of g-115-4058), so
      without this routing the nodes in the regroup arm would each
      receive the destructive-shaped procedure that is wrong for them — the
      false-positive class the conjunction originally existed to prevent. A
      branched action that no caller reads changes no behavior.
   a. COHERENCE GATE (rb-94 / guard-896): body-read the candidate FIRST. If it
      coheres as one tight document flagged purely by low retrieval (niche, not
      bloated), set `maintain_exempt` to include "distill" and SKIP — do NOT
      distill (low util = under-instrumented, not bloat; the g-115-1534 over-flag
      class). The `/tree distill` sub-command enforces the same gate at step 1.5,
      and get_distill_candidates then skips the node's utility triggers durably.
      EXCEPTION: a candidate with `trigger == oversized_append_grown` (crit3) is
      NOT exempt from the COHERENCE gate — it is still too big to Read. But do
      NOT read that as "the rollup fires": crit3 takes the SHAPE FORK (§ distill
      step 1.6) first, and the rollup is non-destructive ONLY for shape (a), an
      append-grown series. For a CATALOG it deletes foundational sections that
      later ones reference, unrecoverably (guard-2109 / rb-6055). Route via 2.b
      and let the fork decide; never pre-commit to the rollup here.
   b. Otherwise invoke `/tree distill <key>`.
3. Log to tree_growth_log

Note: DISTILL candidates require utility data from Phase 4.26. New nodes with zero
retrievals are not eligible. This operation only fires after sufficient sessions
of utility tracking have accumulated signal.

#### 2. SPLIT — Decompose overloaded nodes

**Trigger**: `node.article_count > config.split_threshold` (currently 3)

**Steps**:
1. Find nodes where `growth_state: ready_to_split`
2. Depth guard: abort if `parent.depth + 1 > D_max` (20)
3. Read all articles, cluster into 2-4 groups
4. Create child nodes (compute paths from parent `file` field)
5. Ensure `min_articles_per_child` (2) is met
6. Convert parent to interior, register children
7. Verify parent `child_count <= K_max` (40, per core/config/tree.yaml)
8. Log to tree_growth_log

#### 3. SPROUT — Add a new node for unmapped content

**Trigger**: `unmapped_categories` has entries

**Steps**:
1. Check `_tree.yaml` `unmapped_categories`
2. Find best parent at any depth
3. Depth guard: abort if `parent.depth + 1 > D_max` (20)
4. Compute child file path from parent's `file` field
5. Create node, register via `tree-update.sh --add-child --encoding-source tree-maintain-sprout` (the --encoding-source flag attributes the new node to the SPROUT path in `meta/l1-pick-log.jsonl` — S9; pass --encoding-reason "<why this L1>" to enrich the entry)
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
AND the node `.md` body is a STUB — fewer than `prune_stub_line_threshold`
(config, default 10) non-blank body lines, excluding YAML front matter.

> **`article_count == 0` is NOT an emptiness signal.** `article_count` counts
> *attached articles*, not node body content — a leaf can hold 13-86 lines of
> real `.md` body and still report `article_count == 0` (the field was simply
> never incremented on an under-instrumented node). Pruning such a node
> destroys real knowledge. The g-115-398 sweep flagged 25 such nodes; ALL were
> content-bearing (g-115-1534; exp-g-115-1367: "article_count=0 is NORMAL —
> it counts attached articles, not node body"). The body-read gate below is
> mandatory (verify-before-assuming) — never prune on `article_count == 0` alone.

**Steps**:
1. Find empty childless nodes (skip L1 domains, skip `growth_state: growing`)
2. **Body-read gate (mandatory)**: read the candidate `.md` and count non-blank
   body lines (exclude YAML front matter). If `>= prune_stub_line_threshold`
   (10) the node is content-bearing, NOT empty — SKIP prune. (The
   under-instrumented `article_count` is a data-hygiene concern, not grounds
   for deletion; leave the node in place.)
3. Archive topic file to `world/knowledge/archive/`
4. Remove from `_tree.yaml`, log to tree_growth_log

#### 5.5. RETIRE — Remove never-consulted dead nodes

**Trigger**: leaf node where ALL of the following are true:
- `retrieval_count == 0`
- Node has existed for 5+ sessions (`retire_sessions_unused` from config)
- `growth_state` is not `growing`
- NOT an L1 domain node (depth > 1)
- `.md` body is a STUB — fewer than `prune_stub_line_threshold` (config,
  default 10) non-blank body lines (excl. front matter). A content-bearing
  leaf that was never retrieved is a retrieval-INDEXING gap, not dead
  knowledge — route it to REVIEW, never auto-RETIRE (g-115-1534).

**Steps**:
1. Find candidates: leaf nodes with `retrieval_count == 0`, created before session N-5
   (check `tree_growth_log` for creation date, or node's `.md` front matter `last_update_trigger.session`)
2. **Body-read gate (mandatory)**: read the candidate `.md`; if non-blank body
   lines `>= prune_stub_line_threshold` (10) the node holds real content — do
   NOT retire. Flag in tree_growth_log: `{op: REVIEW, node, date, reason:
   "content-bearing but never retrieved — retrieval-indexing gap"}` and move on.
3. Archive `.md` content to `world/knowledge/archive/{key}-retired-{date}.md`
4. Remove node: `bash core/scripts/tree-update.sh --remove-child <parent> <key>`
5. Log to tree_growth_log: `{op: RETIRE, node, date, reason: "never retrieved in N sessions"}`

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

#### 9. Record Maintenance

Always invoked — records that `/tree maintain` ran to completion. Writes
`maintenance.last_maintain_at` so callers (aspirations precheck, reports,
consolidation audits) have a verifiable cadence signal. Cleared state
(`last_backlog_clear_at`) is auto-computed by tree.py from post-run debt
vs `tree_debt_check.debt_threshold` — the caller does not pass it.

**Instrumentation (required)**: As you perform actions in steps 1–5.5,
maintain an in-memory counter dict tracking what you actioned and what you
reasoned-past. Shape:

```
{
  "mode": "standard" | "backlog",
  "started_at": "<ISO timestamp captured at step 1 start>",
  "decompose":    {"actioned": N, "skipped_by_reason": {"coherent_concept": N, ...}, "children_created": N},
  "distill":      {"actioned": N, "skipped_by_reason": {"still_useful": N, ...}, "bytes_removed": N},
  "redistribute": {"actioned": N, "skipped_by_reason": {...}},
  "split":        {"actioned": N, "skipped_by_reason": {...}},
  "sprout":       {"actioned": N},
  "merge":        {"actioned": N, "skipped_by_reason": {...}},
  "prune":        {"actioned": N},
  "retire":       {"actioned": N, "skipped_by_reason": {...}}
}
```

`skipped_by_reason` keys are free-form strings chosen by the LLM
(e.g. `coherent_concept`, `still_useful`, `risk_of_fragmenting`). Use
concise, reusable reasons so aggregates stay readable. Python-side pre-filter
skip counts are computed automatically by tree.py (`has_children`, `no_file`,
`depth_at_dmax`, `file_not_found`, `read_error`, `below_line_threshold`,
`insufficient_retrievals`, `utility_above_threshold`) — do NOT include those
in your LLM-reported dict.

Pipe the counter dict as stdin JSON with `--with-run-record`. Wrapping the
JSON in a HEREDOC keeps special characters safe in bash:

```
bash core/scripts/tree-update.sh --record-maintenance --with-run-record <<'EOF'
{"mode":"standard","started_at":"...","decompose":{...},"distill":{...},...}
EOF
```

The script appends a full run record to `world/tree-maintenance-log.jsonl`
(merging pre-filter skip counts + llm-reported counts + post-run debt) AND
writes the `maintenance` cadence block as today.

Running without `--with-run-record` is still supported (legacy callers) and
skips the JSONL append. New maintain invocations should always include it.

`_tree.yaml.maintenance.last_maintain_at` is the SINGLE source of truth for
tree-maintenance cadence. Callers (Phase 8.8) read it via `tree-read.sh
--maintenance`. Do NOT add a working-memory mirror — the dual-source pattern
introduces divergence risk across crash boundaries with no compensating benefit.
The JSONL log is instrumentation, not an alternate cadence source.

### Completion Criteria (maintain only)

1. All DECOMPOSE candidates processed (or cap reached with remainder logged)
2. All REDISTRIBUTE candidates processed (or cap reached)
3. All SPLIT, SPROUT, MERGE, PRUNE operations evaluated AND executed where triggered
4. Validation passes
5. Tree node count changed if DECOMPOSE or SPLIT candidates existed
6. `maintenance.last_maintain_at` updated via `tree-update.sh --record-maintenance`

**Failure states** (do NOT mark goal completed):
- Candidates identified but not acted on
- "Will decompose later" / "next cycle" deferral
- Report-only output with no file mutations

### /tree maintain --backlog

Backlog cleanup mode for trees with accumulated structural debt.
Same operations as `/tree maintain` but with elevated caps. Standard caps live
in `core/config/tree.yaml` (single source of truth). Backlog mode multiplies
each standard cap by 1.5 (ceil), bounded by the corresponding `modifiable.max`
ceiling in `tree.yaml`:

- DECOMPOSE: `min(ceil(max_decompose_per_invocation * 1.5),
  modifiable.max_decompose_per_invocation.max)`
- REDISTRIBUTE: `min(ceil(max_redistribute_per_invocation * 1.5),
  modifiable.max_redistribute_per_invocation.max)`
- DISTILL: `min(ceil(max_distill_per_invocation * 1.5),
  modifiable.max_distill_per_invocation.max)`

Other differences from standard mode:
- Processes ALL candidates regardless of `growth_state` field value
  (standard mode requires `growth_state: ready_to_decompose` or checks threshold;
  backlog mode checks threshold directly for every leaf node)
- Processes candidates largest-first (DECOMPOSE by `subtree_leaves`, REGROUP by `child_count`)

**When to use**: Auto-invoked by aspirations loop Phase 8.7 and by
consolidation Step 6 when
`(decompose_candidates + distill_candidates) > tree_debt_check.debt_threshold * 3`.
Can also be run manually after deploying threshold changes.

**Steps**:
1. `Bash: tree-read.sh --decompose-candidates` (structural: subtree-leaves > leaf_cap)
2. `Bash: tree-read.sh --redistribute-candidates` (structural: children > K_max)
3. Compute elevated caps from `core/config/tree.yaml` standard × 1.5 (ceil),
   bounded by the `modifiable.max` ceiling for each parameter.
4. Process DECOMPOSE candidates (up to elevated cap, largest first) —
   same steps as standard DECOMPOSE.
5. Process REDISTRIBUTE candidates (up to elevated cap, largest first) —
   same steps as standard REDISTRIBUTE.
6. Process DISTILL candidates (up to elevated cap, largest first).
7. Run SPLIT, SPROUT, MERGE, PRUNE, RETIRE with normal caps.
8. Log: "BACKLOG CLEANUP: {decomposed} decompositions, {redistributed} redistributions, {distilled} distillations, {total_new_nodes} new nodes created"
9. `Bash: tree-read.sh --validate` — verify structural integrity after bulk changes.
10. Record maintenance with `--backlog-mode --with-run-record`. Pipe the
    instrumentation counter dict (same schema as standard step 9, with
    `"mode":"backlog"`) on stdin. tree.py auto-computes whether backlog drained
    below `tree_debt_check.debt_threshold` and sets `last_backlog_clear_at`
    accordingly, AND appends a run record to `world/tree-maintenance-log.jsonl`.
    ```
    bash core/scripts/tree-update.sh --record-maintenance --backlog-mode --with-run-record <<'EOF'
    {"mode":"backlog","started_at":"...","decompose":{...},"distill":{...},...}
    EOF
    ```
    (Same single-source-of-truth rule as standard step 9 — no WM mirror.
    The JSONL log is instrumentation for drain-rate diagnostics.)

After backlog cleanup, subsequent `/tree maintain` calls use standard caps.

### /tree maintain --stop-mode

Stop-mode maintenance for session-end consolidation. Same operations as standard
`/tree maintain` but with a small fixed budget from `core/config/tree.yaml`
`stop_mode_caps` section. Designed to drain ~20% of standing structural debt per
/stop invocation while keeping /stop under 3 minutes wall-clock.

The three-tier cap relationship: standard 50 / backlog 75 / stop 5. Read
`stop_mode_caps` in `tree.yaml` for the full ratio across all op types.

Caps (from `stop_mode_caps` in `core/config/tree.yaml`):
- DECOMPOSE: `stop_mode_caps.max_decompose_per_invocation` (default 5)
- REDISTRIBUTE: `stop_mode_caps.max_redistribute_per_invocation` (default 1)
- DISTILL: `stop_mode_caps.max_distill_per_invocation` (default 2)
- SPLIT: `stop_mode_caps.max_split_per_invocation` (default 1)
- SPROUT: `stop_mode_caps.max_sprout_per_invocation` (default 0 — do NOT create new debt during /stop)
- MERGE: `stop_mode_caps.max_merge_per_invocation` (default 1)
- PRUNE: `stop_mode_caps.max_prune_per_invocation` (default 1)
- RETIRE: `stop_mode_caps.max_retire_per_invocation` (default 1)

Other behavior identical to standard mode (processes candidates largest-first).

**When to use**: Invoked from `core/config/consolidation-housekeeping.md` Step 6
when stop_mode is active. NOT auto-elevated to backlog mode (the whole point is
small budget, not big drain). NEVER invoked from a normal iteration close — that
path uses standard or backlog caps based on the debt threshold.

**Why this exists** (Magic Wand #4 — see `core/config/modes/autonomous.md`
"Stop-mode does work"): pre-2026-05-08, stop_mode skipped /tree maintain
entirely under tight context, leaving decompose candidates to accumulate for
weeks. The fix isn't "skip more to save time" — it's "do a small budget per
stop so the queue drains over many stops." Sibling principle: pending-questions
sweep (Lane B), structural-progress productivity axis (Lane D #1).

**Steps**:
1. `Bash: tree-read.sh --decompose-candidates`
2. `Bash: tree-read.sh --redistribute-candidates`
3. Read `stop_mode_caps` from `core/config/tree.yaml`.
4. Process DECOMPOSE candidates (up to stop cap, largest first).
5. Process REDISTRIBUTE candidates (up to stop cap, largest first).
6. Process DISTILL candidates (up to stop cap, largest first).
7. Run SPLIT, MERGE, PRUNE, RETIRE with stop caps. SKIP SPROUT (cap=0 means
   no new node creation during /stop — bias toward draining, not adding).
8. Record maintenance with `--stop-mode --with-run-record`. Pipe the
   instrumentation counter dict (same schema as standard, with `"mode":"stop"`)
   on stdin. Sets `maintenance.last_stop_mode_at` for cadence visibility.
   ```
   bash core/scripts/tree-update.sh --record-maintenance --stop-mode --with-run-record <<'EOF'
   {"mode":"stop","started_at":"...","decompose":{...},"distill":{...},...}
   EOF
   ```
   The JSONL log entry at `world/tree-maintenance-log.jsonl` records `mode: "stop"`
   and the per-op `actioned` counts. The Lane D #1 productivity-gate structural
   axis reads these entries to credit /stop-time drain as legitimate work.

## Sub-Command: /tree reparent <node> <new-parent>

Move a node (and its entire subtree) to a new parent. Updates all YAML metadata
atomically. Reports physical file moves for the agent to execute.

```
result=$(bash core/scripts/tree-update.sh --reparent <node> <new-parent>)
# Returns JSON:
# {
#   "reparented": "<node>",
#   "old_parent": "<old-parent-key>",
#   "new_parent": "<new-parent-key>",
#   "new_depth": N,
#   "file_moves": [{"key": "<node>", "old": "world/...", "new": "world/..."}, ...],
#   "old_chain_propagation": {...},
#   "new_chain_propagation": {...}
# }

# The command handles atomically:
# - Remove node from old parent's children, add to new parent's children
# - Update node's parent field
# - Recursively recompute file paths and depths for entire subtree
# - Update node_type (old parent → leaf if childless, new parent → interior if was leaf)
# - Propagate confidence up both old and new parent chains

# Guards (rejects with exit 1):
# - Cannot reparent root
# - Cannot reparent a node to itself
# - Cannot reparent to a descendant (circular)
# - Cannot reparent to current parent (no-op)
# - Cannot reparent if subtree would exceed D_max

# AFTER the command succeeds, execute physical file moves.
# file_moves[0] is always the reparented node itself.
# For interior nodes: moving the directory implicitly moves all descendants,
# so only the top-level node needs explicit handling.
top = result.file_moves[0]
old_abs = resolve_file_path(top.old)
new_abs = resolve_file_path(top.new)
mkdir -p $(dirname new_abs)
mv old_abs new_abs
# If node is interior, move the directory (carries all children)
old_dir = old_abs without .md
new_dir = new_abs without .md
IF old_dir exists as directory:
    mv old_dir new_dir
# Verify: remaining file_moves entries should now resolve to existing files
# (moved implicitly by the directory move)

# --- Repair inbound cross-references (g-115-1830) ---
# The reparent updated _tree.yaml + moved the file, but any OTHER node whose BODY
# hardcodes a backtick-quoted ref to this node's OLD path now dangles — a later
# `/tree validate` would flag it as a g-115-1419 warning, and until then every
# retrieval through that ref misses. Now that the physical move is applied and
# every body is readable at its current path, repair those inbound refs old->new.
# The reparent `result` already carries file_moves; pipe it straight in.
echo "$result" | py -3 core/scripts/tree-inbound-ref-fix.py --apply
# Output JSON lists every ref rewritten (node + old_ref -> new_ref) + fixed count.
# Surface it. Backtick-scoped (bare-prose mentions untouched), form-preserving
# (world/knowledge/tree/ prefix or L1-first kept), and idempotent — safe to re-run.
```

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
  Bash: echo "Tree validation PASSED"
ELSE:
  Bash: echo "Tree validation FAILED: {errors}"
  Attempt auto-repair for: mismatched child_count, broken parent refs
```

## Constraints

- `K_max` (40, per core/config/tree.yaml): soft limit at all levels (can exceed when justified, log reason)
- `D_max` (20): hard limit — never create nodes beyond depth 20
- Never prune L1 domain nodes
- `tree_growth_log` is written BY THE SCRIPTS, not by hand (g-115-3210). The
  write paths append DECOMPOSE (batch: interior-flip + add-child), PRUNE
  (remove-child), REPARENT, L1_ADD and L1_RENAME. Ordinary child-add is
  deliberately NOT logged — it would bury the structural signal the log exists
  to carry. Do not hand-append rows; if an op you think belongs is missing,
  add it to `core/scripts/_growth_log.py` so BOTH the CLI and the daemon get
  it. (This line previously read "Always append ... for every structural
  change" — an instruction nothing enforced, which is why the log froze for
  3.7 months.)
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
- **Reads**: `world/knowledge/tree/_tree.yaml`, node `.md` files, `core/config/memory-pipeline.yaml`, `core/config/tree.yaml`
- **Writes**: `world/knowledge/tree/_tree.yaml`, node `.md` files, `world/knowledge/archived/`
- **Does NOT call**: any other skills (pure tree operations)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last tree-script call (`tree-find-node.sh`, `tree-add.sh`,
`tree-set.sh`, etc.) or the Edit/Write of a node `.md` file. Never end with a text
summary — the tree mutation IS the output.
