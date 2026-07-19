# Exhaustive Knowledge Search Before Negative Conclusions

**Reinforces**: `.claude/rules/verify-before-assuming.md` (multi-signal requirement)

## Principle

Before concluding that something "isn't built," "doesn't exist," "isn't possible,"
or "can't be done" — the agent MUST exhaustively search the knowledge tree. A single
category search or a single query is insufficient. Absence of evidence in one location
is not evidence of absence.

## Protocol

When forming a negative conclusion about capabilities, features, or possibilities:

1. **Multi-query search**: Use `tree-find-node.sh --text` with at least 3 different
   query variations. Rephrase the concept using synonyms, related terms, and
   alternative framings.
   ```bash
   bash core/scripts/tree-find-node.sh --text "llama server CUDA" --top 3
   bash core/scripts/tree-find-node.sh --text "GPU inference binary" --top 3
   bash core/scripts/tree-find-node.sh --text "external model server" --top 3
   ```

2. **Cross-category check**: Don't search only the obvious category. If looking for
   whether something exists in "performance," also check "infrastructure,"
   "architecture," and any other category that could plausibly contain the information.

3. **Read matching nodes**: For each matching node returned, READ the full `.md` file.
   Summaries in `_tree.yaml` are compressed — the detail is in the articles.

4. **Check adjacent systems**: If the knowledge tree doesn't have it, check:
   - Reasoning bank (`reasoning-bank-read.sh --search "query"`)
   - Guardrails (`guardrails-read.sh --search "query"`)
   - Pattern signatures (`pattern-read.sh --search "query"`)
   - Experience archive (`experience-read.sh --search "query"`)

5. **Retrieval escalation**: Follow `core/config/conventions/retrieval-escalation.md`
   — Tier 1 (tree) → Tier 2 (codebase) → Tier 3 (web search) before concluding
   something doesn't exist.

6. **Search the GOAL QUEUE — not just the code** (added 2026-07-14, zeta). A large
   class of invocations lives in a **recurring goal's `description`**, never in
   `.claude/skills/`, `core/config/`, or `core/scripts/*.sh`. Grepping only the code
   surfaces and concluding "nothing invokes this / this detector has no consumer" is a
   **structurally guaranteed false negative** for every goal-queue-driven sweep.

   Before any "nothing runs X / X has no consumer / X was built and abandoned" claim:
   ```bash
   # search DESCRIPTIONS, not just titles — the invocation lives in the body
   py -3 -c "import json,sys; sys.path.insert(0,'core/scripts'); from _paths import WORLD_DIR; import os; \
   [print(g['id'],'|',g.get('title','')[:60]) for r in (json.loads(l) for l in open(os.path.join(str(WORLD_DIR),'aspirations.jsonl'),encoding='utf-8') if l.strip()) \
    for g in (r.get('goals') or []) if '<script-or-tool-name>' in json.dumps(g)]"
   ```
   **`aspirations-query.sh --contains` is NOT sufficient** — it matches TITLES only, so it
   returns empty for a script named solely in a description. An empty result from it is a
   silent-failure negative (`verify-before-assuming` rule 4: a silently-empty command is
   ZERO signals, not one), *not* a confirming second signal. Two weak signals drawn from
   the same blind spot are not two signals.

   Canonical incident (2026-07-14, zeta fresh-eyes): grepped skills/config/scripts for
   `fixture-leak-scan.py`, found nothing, and wrote "a working detector with ZERO
   consumers" into a briefing. It is in fact wired to **g-115-1651** (recurring, 24h) via
   that goal's Step 2, and had run **that same morning**. The empty
   `--contains "fixture-leak"` query was read as corroboration; it only ever searched
   titles. Caught by the goal-duplication gate, not by the agent.

## Trigger Phrases

Apply this protocol whenever the agent is about to output or act on:
- "This isn't built yet"
- "There's no way to..."
- "This can't be done because..."
- "No existing implementation for..."
- "We'd need to build..."
- "Not possible with current..."
- "Doesn't support..."
- "Nothing invokes this" / "this has no consumer" / "this was built and never wired"
  (→ rule 6 above: search the goal queue's DESCRIPTIONS before asserting)

## Anti-Patterns

- Searching one node and concluding a feature doesn't exist
- Checking `_tree.yaml` summaries without reading the actual `.md` files
- Assuming something isn't built because the agent doesn't remember building it
- Concluding "not possible" from a single tool/library limitation without checking
  if alternative approaches exist in the knowledge base
- Declaring "we'd need to build X" without searching for existing implementations

## Relationship to Verify Before Assuming

This convention is the knowledge-tree-specific implementation of the "Verify Before
Assuming" rule. While that rule requires 2+ independent verification signals for any
negative conclusion, THIS convention specifies exactly how to verify within the
knowledge system:

- 3+ query variations (not just one search)
- Cross-category (not just the obvious node)
- Full article reads (not just summaries)
- Adjacent data stores (reasoning bank, guardrails, etc.)
- Retrieval escalation (tree → codebase → web)

## Motivation

Session 18 (2026-04-01): During LLM benchmarking, the agent assumed that GPU
inference for Qwen3.5 was impossible based on a single signal (pip wheel limitation).
A more thorough search would have revealed that pre-built llama-server.exe binaries
with CUDA support existed — solving the problem without any code changes. The cost
of the missed search was multiple hours of wasted effort and a premature negative
conclusion that blocked progress.
