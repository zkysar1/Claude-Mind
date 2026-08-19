# Retrieval Escalation Convention

Three-tier escalation policy for knowledge retrieval. Every retrieval attempt
follows this sequence, with a sufficiency check between each tier. Stop at
the first tier that provides sufficient knowledge.

## The Three Tiers

### Tier 1: Knowledge Tree (Always First)

Use existing retrieval mechanisms:
- `retrieve.sh --category {topic} --depth medium` for unified search
- Or intelligent retrieval protocol (read tree summary, reason about nodes,
  read `.md` files, load supplementary stores)

This is the primary knowledge source. Most questions should be answerable here.

**Sufficiency check**: Can I answer/execute confidently with what the tree returned?
- **YES** → stop, use tree knowledge
- **PARTIAL** → note what's missing, proceed to Tier 2
- **NO** (empty or irrelevant results) → proceed to Tier 2

### Tier 2: Codebase Exploration (Verify/Supplement)

When the tree doesn't have enough, search the actual codebase for evidence.
The primary workspace is defined in `agents/<agent>/self.md` (e.g., a repo path).
If no workspace is configured, skip Tier 2.

**Techniques**:
- **Grep**: search for patterns, function names, configuration values, error messages
- **Glob**: find files by name pattern (e.g., `**/*.yaml`, `**/auth/*.py`)
- **Read**: examine specific files identified by Grep/Glob

**When to use Tier 2**:
- Tree has partial knowledge but needs verification against actual code
- Question is about implementation details, code structure, or current state
- Tree returned nothing but the topic is clearly about the codebase
- Goal requires understanding how code actually works (not just documented theory)

**When to SKIP Tier 2**:
- Topic is purely conceptual (not about any codebase)
- Tree answer is confident and complete
- Topic is about external systems/services with no local code

**Sufficiency check**: Can I answer/execute with tree + codebase findings?
- **YES** → stop, use combined knowledge
- **NO** → proceed to Tier 3 (if mode permits)

### Tier 3: Web Search (External Knowledge)

When tree + codebase aren't enough, search the web for external knowledge.

**Techniques**:
- **WebSearch**: targeted queries for specific gaps
- **WebFetch**: retrieve authoritative sources identified by search

**When to use Tier 3**:
- Topic involves external APIs, services, or technologies not in tree/codebase
- Information may be outdated and needs current data
- Tree + codebase provide no relevant results
- Goal explicitly requires research (skill: `/research-topic`)

**When to SKIP Tier 3**:
- Mode is `reader` (no web access)
- Tree + codebase fully answered the question
- Topic is purely internal (private codebase knowledge)

### Tier 2.5: Peer Worlds (Cross-Deployment, READ-ONLY)

**All three tiers above are single-world by construction.** `retrieve.py` reads
`WORLD_PATH` and nothing else — it has zero references to `ENVIRONMENT_ID`,
`COMMONS_POLICY`, or any peer, and it never reads board channels (its one
`board` occurrence is a comment). So an answer that lives in a peer Mind
deployment is invisible to Tiers 1-3 even when every deployment is perfectly
healthy. That is a substrate gap, not a diligence gap: no amount of Tier-1/2
rigor can close it, which is what makes it different from every other
"exhaustive search" failure mode.

```bash
bash core/scripts/peer-retrieve.sh <query terms...>        # add --json for the raw object
```

**When to use Tier 2.5**: before reporting that something does not exist,
before answering a question whose subject another deployment owns, and any
time the user's question spans the fleet rather than this world. It is cheap
(filesystem reads) and read-only in every mode.

**Read the exit code — it is the whole point.**

| rc | meaning |
|----|---------|
| 0 | every registered world was fully read; an `empty` here is an EARNED negative |
| 2 | usage error (empty query, or `--limit` below 1) — nothing was searched, so this is not a result at all |
| 3 | **PARTIAL** — at least one world could not be fully read, so absence there is NOT evidence of absence |

Each world reports two orthogonal fields, and conflating them is the failure
this tier exists to prevent:

- `status` — `hit` \| `empty` \| `unreachable` (did I find anything?)
- `completeness` — `complete` \| `partial` (did I see everything?)

A world can be `hit` **and** `partial` at once — that is the common case for a
peer whose published output reached our board while its own store stays
unreachable. `status: empty` is the ONLY value that licenses a negative
conclusion, and it is unreachable-free by construction.

**Two lanes per peer.** The `direct` lane reads the peer's world store when a
`peer_world_path` is configured; the `channel` lane finds peer-authored content
(`<agent>@<env-id>`) that already arrived on THIS world's board. The channel
lane is always reachable but shows only what the peer chose to publish — it can
never stand in for the direct lane, which is why an unreachable direct lane
keeps the world `partial` even when the channel returns hits.

**READ-ONLY, never import/merge.** Nothing retrieved is written into this
world's stores, and every record keeps its `origin_env`. Rationale against
`world-contract.md` G1-G5 lives in `peer_retrieve.py`'s module docstring. The
reader never imports `_fileops`, so a peer read cannot inherit this world's
storage backend (guard-955 / rb-2983 class) — `assert_no_fileops()` proves it
at runtime rather than asserting it in prose.

## Mode Gates

| Mode       | Tier 1 (Tree) | Tier 2 (Codebase) | Tier 2.5 (Peer worlds) | Tier 3 (Web) |
|------------|---------------|---------------------|------------------------|--------------|
| reader     | Yes           | Yes                 | Yes                    | No           |
| assistant  | Yes           | Yes                 | Yes                    | Yes          |
| autonomous | Yes           | Yes                 | Yes                    | Yes          |

Tier 2 (Grep/Glob/Read) is inherently read-only — safe in all modes.
Tier 2.5 (`peer-retrieve.sh`) is read-only by construction — safe in all modes.
Tier 3 (WebSearch/WebFetch) requires assistant or autonomous mode.

## Sufficiency Evaluation Criteria

Between each tier, evaluate:
1. **Coverage**: Does retrieved knowledge address the core question/goal?
2. **Confidence**: Is the information reliable enough to act on?
3. **Specificity**: Is it detailed enough for the task at hand?
4. **Recency**: Is the information current enough? (Stale data may trigger Tier 3.)

A "sufficient" result meets criteria 1-3. Criterion 4 specifically triggers Tier 3.

## Anti-Patterns

- DO NOT escalate to Tier 2 when the tree clearly has the answer
- DO NOT escalate to Tier 3 for internal/private codebase topics
- DO NOT run all three tiers "just in case" — stop when sufficient
- DO NOT use Tier 3 for topics where the tree was recently updated (check `last_updated`)
- Tier 2 searches should be **targeted** (specific Grep queries), not exhaustive

## Retrieval Manifest Metadata

After escalation completes, the retrieval manifest should include:
- `tiers_used`: list of tiers attempted (e.g., `[1]`, `[1, 2]`, `[1, 2, 3]`)
- `tier_results`: per-tier summary of findings
- `escalation_reasons`: why each additional tier was needed
- `sufficient`: whether the combined result was sufficient

This metadata feeds into the retrieval gate (Phase 9.5b) for escalation quality checks.

## Consumers

This convention is referenced by:
- `/respond` Step 4 (Knowledge Search)
- `/aspirations-execute` Phase 4 Step 5 (Sufficiency Evaluation)
- `/aspirations-learning-gate` Phase 9.5b (Retrieval Gate — escalation quality check)
