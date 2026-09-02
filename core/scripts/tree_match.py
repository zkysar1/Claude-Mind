#!/usr/bin/env python3
# domain-leak-exempt: tokenizer comments cite "how does NPC selection work" as a
# concrete query example to anchor the unsplit-token bug discussion. The term
# is comment-prose illustrating a regression, not framework-domain coupling.
"""Shared matching module for knowledge tree node lookup.

Extracted from retrieve.py so that both retrieve.py (full retrieval) and
tree.py (lightweight find) can reuse the same matching logic.

Functions:
    find_nodes(text, nodes, entity_index, top, leaf_only)
        - High-level: match + score + limit. No sibling/parent inclusion.
    build_concept_index(nodes)
        - Build entity-term -> [node_keys] from .md front matter.
    _match_nodes(category, nodes, entity_index, concept_index)
        - Run 4 matching strategies for a single category.
    _include_siblings / _include_parents / _compute_match_score / _score_and_limit
        - Retrieval helpers (used by retrieve.py for full retrieval).
    parse_front_matter(md_path)
        - Extract YAML front matter from a .md file.
"""

import math
import re
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import _longpath_safe, resolve_file_path

# Match channel scores for ranking (higher = more relevant)
CHANNEL_SCORES = {
    "exact_key": 4.0,
    "substring": 3.0,
    "entity_index": 2.5,
    # g-306-83: semantic eligibility via the persisted embedding index
    # (retrieve.load_tree_nodes, flag-gated embedding_tree_channel_enabled).
    # Scored like entity_index: a strong semantic hit is comparable evidence
    # to an entity-term hit; the cosine BONUS in the scorer differentiates
    # strong vs marginal semantic matches on top of this base.
    "embedding": 2.5,
    "concept": 2.0,
    "word_prefix": 1.5,
    "sibling": 1.0,
    "parent": 0.5,
}

CAPABILITY_BONUS = {
    "MASTER": 0.5,
    "EXPLOIT": 0.3,
    "CALIBRATE": 0.1,
}

# Cosine-similarity bonus weight, applied on top of channel + depth +
# confidence + capability bonuses. cosine ∈ [0, 1] → bonus ∈ [0, COSINE_BONUS_WEIGHT].
# Tuned 2.0 so a fully-aligned query/node pair adds enough to outrank a
# generic-token match (e.g. word_prefix 1.5 + d3 1.5 = 3.0 base) while
# not overwhelming the depth/confidence signals on partial-cosine matches.
# Weight is the SCORING-LAYER tunable; the IDF math itself in tree_idf.py
# is parameter-free.
COSINE_BONUS_WEIGHT = 2.0

# Recency bonus parameters (P2 #10, 2026-05-10). One-sided positive bonus
# based on `last_updated` — recently-edited nodes get a small score boost,
# stable old knowledge gets ZERO bonus (no penalty). Asymmetric on purpose:
# a stale-but-correct node should not be demoted just for being stable.
# Decay: bonus = MAX_BONUS * exp(-age_days / TAU_DAYS).
#   age=0   → 0.50 bonus (full)
#   age=30  → 0.18 bonus
#   age=90  → 0.025 bonus
#   age=130 → 0.007 bonus (~zero, matches HISTORICAL_ANCHOR positioning)
#   missing last_updated → 0 bonus (legacy nodes pre-backfill)
# Source field is `last_updated` (content freshness), NOT `last_retrieved`
# (interest signal — would create runaway feedback if amplified by scoring).
RECENCY_MAX_BONUS = 0.5
RECENCY_TAU_DAYS = 30

# M-4 rehearsal-weighted decay defaults (overridden by tree.yaml retrieval: section).
# Frequently-retrieved nodes get a longer TAU so their recency bonus persists.
# Uses retrieval_count (monotonic counter, safe) -- NOT last_retrieved (incremented
# by scoring via _locked_bump_jsonl, which would create runaway feedback).
REHEARSAL_TAU_BASE = 30
REHEARSAL_TAU_MAX = 120
REHEARSAL_TAU_SCALE = 5.0
REHEARSAL_NEUTRAL_BELOW = 5

# M-5 provenance-based confidence modulation.
# Weights applied to the confidence term only (NOT the full score).
# ProvenanceTag values align with perception-module.md Section 1.
PROVENANCE_WEIGHTS = {
    "DIRECT": 1.0,
    "INFERRED": 0.7,
    "SYNTHESIZED": 0.8,
    "HEARSAY": 0.5,
}
DEFAULT_PROVENANCE_WEIGHT = 0.9  # Legacy nodes without provenance field


def safe_num(value, default, *, rid="", field=""):
    """Coerce a record-sourced numeric field to float; on failure warn (naming
    the record and field) and return ``default`` — a malformed record costs
    ITSELF a scoring term, never the whole query (g-357-49).

    A hand-edited front matter or store record can carry a quoted number, a
    list, or arbitrary text where the scorer expects a number. Raw arithmetic
    on such a value crashes the entire retrieval ("can't multiply sequence by
    non-int of type float" — observed live on a peer deployment, deterministic
    for any query whose candidate set included the one malformed record).
    Shared by tree_match and retrieve.py scoring (retrieve already imports
    from this module; the reverse import would cycle).
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            sys.stderr.write(
                f"[retrieve] WARN: non-numeric {field or 'field'}="
                f"{value!r} on record {rid or '?'} — scored as {default} "
                f"(g-357-49)\n")
        except Exception:
            pass
        return default

# MMR (Maximal Marginal Relevance) parameters (P2 #11, 2026-05-10).
# Re-ranks the top-K to favor diversity when multiple high-relevance results
# come from the same subtree (e.g., 5 siblings under one parent crowding out
# alternatives). Both terms are normalized to [0, 1] by max-relevance scaling
# before combining:
#   MMR(i) = λ * (score_i / max_score) - (1-λ) * max_j∈S path_sim(i, j)
# λ=1 → pure relevance (no diversity); λ=0 → pure diversity.
# Tuned 0.7: high-relevance items still dominate, but a top-relevance sibling
# can lose its slot to a mid-relevance different-branch alternative when the
# slot would otherwise be its 3rd or 4th sibling.
MMR_LAMBDA = 0.7


def _recency_bonus(node, cfg=None):
    """Exponential-decay recency bonus from ``last_updated``, with rehearsal-
    adaptive TAU.  Frequently-retrieved nodes (high ``retrieval_count``) get a
    longer TAU, meaning their recency bonus persists longer.

    Missing ``last_updated`` -> 0.  Future-dated entries clamped to age=0.

    M-4: TAU = clamp(base + retrieval_count / scale, base, max).
    Below ``rehearsal_neutral_below`` retrievals TAU stays at base (no
    rehearsal effect for under-retrieved nodes).

    Source field is ``last_updated`` (content freshness), NOT ``last_retrieved``
    (interest signal -- would create runaway feedback if amplified by scoring).
    ``retrieval_count`` is safe: it is a monotonic counter that scoring does not
    increment (only ``_locked_bump_jsonl`` in retrieve.py increments it, outside
    the scoring path).
    """
    lu = node.get("last_updated")
    if not lu:
        return 0.0
    try:
        d = datetime.strptime(str(lu)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0.0
    age_days = (date.today() - d).days
    if age_days < 0:
        age_days = 0

    # Rehearsal-adaptive TAU (safe_num: a non-numeric retrieval_count must not
    # crash the rc/neutral comparison below — g-357-49)
    rc = safe_num(node.get("retrieval_count", 0) or 0, 0.0,
                  rid=node.get("key", ""), field="retrieval_count")
    if cfg:
        tau_base = float(cfg.get("rehearsal_tau_base", REHEARSAL_TAU_BASE))
        tau_max = float(cfg.get("rehearsal_tau_max", REHEARSAL_TAU_MAX))
        tau_scale = float(cfg.get("rehearsal_tau_scale", REHEARSAL_TAU_SCALE))
        neutral = int(cfg.get("rehearsal_neutral_below", REHEARSAL_NEUTRAL_BELOW))
    else:
        tau_base = float(REHEARSAL_TAU_BASE)
        tau_max = float(REHEARSAL_TAU_MAX)
        tau_scale = float(REHEARSAL_TAU_SCALE)
        neutral = REHEARSAL_NEUTRAL_BELOW

    if rc < neutral:
        tau = tau_base
    else:
        tau = min(tau_base + rc / tau_scale, tau_max)

    return RECENCY_MAX_BONUS * math.exp(-age_days / tau)


def _provenance_weight(node):
    """Return the provenance-based weight for the confidence term.

    Reads ``node['provenance']``; missing/unknown -> DEFAULT_PROVENANCE_WEIGHT.
    Case-insensitive lookup.  M-5: modulates the confidence contribution in
    ``_compute_match_score`` so DIRECT observations rank above INFERRED or
    HEARSAY at equal raw confidence.
    """
    prov = node.get("provenance")
    if not prov:
        return DEFAULT_PROVENANCE_WEIGHT
    return PROVENANCE_WEIGHTS.get(str(prov).upper(), DEFAULT_PROVENANCE_WEIGHT)


# ---------------------------------------------------------------------------
# Front matter parsing
# ---------------------------------------------------------------------------

def parse_front_matter(md_path):
    """Extract YAML front matter from a .md file. Returns dict or {}."""
    p = Path(md_path)
    # own-cloud read-path fix (2026-07-02): materialize an S3-only node .md on a
    # fresh box BEFORE the exists() gate. Node bodies under world/knowledge/tree/
    # are synced; on a blank cache they'd read as absent, so the concept index
    # would build from empty `entities` (degraded retrieval). Lowest-level reader
    # -> covers every caller (build_concept_index et al.). Lazy, fail-open import;
    # no-op on LocalBackend and for out-of-root paths (keystone).
    try:
        from storage_backend import get_backend
        get_backend().ensure_local(p)
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error (g-306-218)
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("ensure_local", p, e)
        except Exception:
            pass
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1])
        return fm if isinstance(fm, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Concept index from .md front matter entities
# ---------------------------------------------------------------------------

def _norm_separators(s):
    """Collapse any run of non-alphanumeric characters to a single hyphen.

    Node keys are lowercase kebab-case by framework convention, so this is
    near-identity for keys and does the real work on the QUERY side: it lets
    "test coverage illusions", "test_coverage_illusions" and
    "Test Coverage Illusions" all resolve to the key test-coverage-illusions.

    Uses the same `[a-z0-9]+` tokenizer Strategy 3 adopted on 2026-05-09, so
    the two strategies agree on what counts as a separator. Order-preserving
    by construction — this makes separators irrelevant, never token order.
    Callers must pass an already-lowercased string.
    """
    return "-".join(re.findall(r'[a-z0-9]+', s))


# Below this many nodes a bulk listing costs more than the HEADs it saves.
# Derived from the measurement in `_prefetch_tree_root` below: the listing is
# 3 requests / 0.9s and a HEAD is ~0.094s (135.1s / 1436), so break-even is
# ~10 nodes. 50 is a deliberate margin — every real caller passes the whole
# tree (1437 nodes), and the small-`nodes` case is `find_nodes_by_text`'s
# `leaf_only` filter, which must not pay a full-tree listing to save a handful
# of HEADs.
_PREFETCH_MIN_NODES = 50


def _node_path(virtual_path, world_root=None):
    """Resolve a VIRTUAL-prefixed tree path ("world/knowledge/tree/...").

    ``world_root`` is the per-request world: a daemon endpoint's
    ``ctx.paths.world``, or the swapped-in ``retrieve.WORLD_DIR``. When given,
    the path derives from IT -- the same ``root / virtual[6:]`` join that
    ``resolve_file_path`` performs, so the spelling `_prefetch_tree_root`
    depends on is unchanged. When omitted, fall back to the module-level
    resolver, which is correct in a fresh CLI process where ``_paths.WORLD_DIR``
    was bound from this agent's conf.

    Why the parameter exists (g-367-08): a long-lived daemon binds
    ``_paths.WORLD_DIR`` ONCE at import. A daemon that started before its
    world was configured holds None forever; one that started bound to agent A
    serves agent B's requests against A's files. Neither is visible from the
    config, which is correct on disk. The request context is the only root
    that is right per call.
    """
    if world_root is not None and virtual_path.startswith("world/"):
        return _longpath_safe(Path(world_root) / virtual_path[6:])
    return resolve_file_path(virtual_path)


def _prefetch_tree_root(node_count, world_root=None):
    """One bulk listing that warms the freshness cache for every node body the
    loop below is about to read. Optimization ONLY — never changes what a read
    returns, and never raises.

    WHY: `parse_front_matter` calls `get_backend().ensure_local(p)` per node
    (the own-cloud read-path fix, 2026-07-02). A tree walk touches each file
    ONCE, so every file misses `_refresh`'s per-file TTL cache and pays a HEAD.
    `prefetch` gets the same ETag token from a paginated listing at ~1 request
    per 1000 keys, and warms an entry only where the listing PROVES the local
    copy current (md5 == ETag) — see OwnCloudBackend.prefetch.

    MEASURED here, on this call site, cold in-process caches, live store
    (zeta, hostname cc-02, uname -r 6.8.0-137-generic, 1437 nodes / _tree.yaml
    1,486,779 B):
        without: 1436 HEAD          , 135.1s, 3036 terms
        with   : 3 LIST + 1 HEAD    ,   1.8s, 3036 terms
        -> 359x fewer requests, 75x faster, INDEX BYTE-IDENTICAL
    Verify a change here by COUNTING head_object calls, never by reading
    `stats["warmed"]`: warmed=N with N HEADs still paid is a reachable state
    that reads as total success (g-115-6671).

    PATH SPELLING IS LOAD-BEARING and is why the root is resolved through the
    same `resolve_file_path` the loop uses. `prefetch` warms
    `_cache_check[str(root / rel)]` while `_refresh` reads
    `_cache_check[str(self._local(path))]`, and `_local` is identity — neither
    side normalizes. Both spellings here come from `resolve_file_path`, so they
    agree (measured True above). If the loop's path derivation ever changes,
    re-measure the HEAD count rather than trusting `warmed`.

    LocalBackend.prefetch is a documented no-op that walks nothing, so this
    costs a dict return on the default backend.
    """
    if node_count < _PREFETCH_MIN_NODES:
        return
    # Lazy, fail-open — same posture as parse_front_matter's ensure_local
    # below: an optimization must never break retrieval.
    try:
        from storage_backend import get_backend
        get_backend().prefetch(_node_path("world/knowledge/tree", world_root))
    except Exception as e:
        try:  # report, never raise (g-306-218)
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error(
                "prefetch", "world/knowledge/tree", e)
        except Exception:
            pass


def build_concept_index(nodes, world_root=None):
    """Build entity-term -> [node_keys] index from .md front matter entities.

    Reads the `entities` list from each node's .md front matter.
    Returns dict mapping lowercase entity terms to lists of node keys.

    ``world_root``: the per-request world root (see ``_node_path``). Daemon
    callers MUST pass it; the CLI may omit it.
    """
    index = {}
    _prefetch_tree_root(len(nodes), world_root)
    for key, node in nodes.items():
        file_path = node.get("file")
        if not file_path:
            continue
        # `file` in _tree.yaml is VIRTUAL-prefixed ("world/knowledge/tree/...").
        # It must go through the canonical resolver: under .mind-data storage
        # WORLD_DIR is PROJECT_ROOT/.mind-data/world, so PROJECT_ROOT/world/
        # does not exist and a bare join resolved 0 of 1363 node bodies —
        # parse_front_matter's exists() gate then returned {} for every node and
        # this index built from nothing, silently disabling Strategy 4 (the only
        # token-level natural-language path into the tree). guard-132; the same
        # resolver tree.py has used all along. Do NOT reintroduce PROJECT_ROOT
        # here — the failure is silent, because an empty index and a genuinely
        # entity-less corpus produce identical output.
        md_path = _node_path(file_path, world_root)
        fm = parse_front_matter(md_path)

        # confidence/capability_level are NOT read from .md — they live
        # exclusively in _tree.yaml (split-by-nature schema). Do not re-add
        # a backfill here; it reintroduces the dual-source drift bug.
        entities = fm.get("entities", [])
        if not isinstance(entities, list):
            continue
        for entity in entities:
            term = str(entity).lower().strip()
            if term:
                index.setdefault(term, []).append(key)
    return index


# ---------------------------------------------------------------------------
# Matching engine (composable helpers)
# ---------------------------------------------------------------------------

def _match_nodes(category, nodes, entity_index, concept_index):
    """Run all 4 matching strategies for a single category.

    Returns (matched_list, match_channels_dict).
    matched_list: [(key, node), ...]
    match_channels_dict: {key: channel_name}
    """
    cat_lower = category.lower()
    matched = []
    matched_keys = set()
    channels = {}

    # g-306-182 (2026-08-04): the exact_key test was LITERAL equality, so the
    # natural-language spelling of a node key could not earn the channel —
    # "test coverage illusions" missed the node keyed test-coverage-illusions
    # and fell through to word_prefix, costing 4.0 -> 1.5 (measured: rank #1 at
    # 6.80 vs rank #7 at 4.30 on the live tree). Natural language is the query
    # shape the framework MANDATES (code-review-protocol.md step 4 requires two
    # free-text queries), so the miss lands on the dominant caller.
    #
    # Normalizing to a common separator form resolves it the same way Strategy 3
    # was resolved on 2026-05-09 (P0 #2) — same `[a-z0-9]+` tokenizer, so the two
    # strategies now agree on what a separator is. This is deliberately ORDER-
    # PRESERVING: it makes separators irrelevant, NOT token order, so
    # "illusions coverage test" still does not match test-coverage-illusions.
    cat_norm = _norm_separators(cat_lower)

    # Strategy 1: Substring — category in key/summary/topic (bidirectional)
    for key, node in nodes.items():
        key_lower = key.lower()
        summary = str(node.get("summary", "")).lower()
        topic = str(node.get("topic", key)).lower()

        if key_lower == cat_lower or _norm_separators(key_lower) == cat_norm:
            # Exact key match (strongest)
            matched.append((key, node))
            matched_keys.add(key)
            channels[key] = "exact_key"
        elif (cat_lower in key_lower or cat_lower in summary or
              cat_lower in topic or key_lower in cat_lower):
            matched.append((key, node))
            matched_keys.add(key)
            channels[key] = "substring"

    # Strategy 2: Entity index — _tree.yaml entity_index lookup
    if cat_lower in entity_index:
        for en in entity_index[cat_lower].get("tree_nodes", []):
            if en in nodes and en not in matched_keys:
                matched.append((en, nodes[en]))
                matched_keys.add(en)
                channels[en] = "entity_index"

    # Strategy 3: Word-prefix — tokenize on word boundaries (any non-alphanumeric
    # separator: hyphen, space, underscore, slash, etc.), prefix match (min 4 chars).
    # 2026-05-09 (P0 #2 from knowledge-system audit): pre-fix used `split("-")`
    # which only split on hyphens. Space-separated multi-word queries
    # ("memory consolidation", "framework architecture") and natural-language
    # questions ("how does NPC selection work") became one giant unsplit token
    # that never prefix-matched anything — those queries returned 0 tree nodes.
    # Aligned with Strategy 4's tokenizer (`re.findall`) for consistency.
    # Node KEYS are still hyphen-separated (the framework convention) so we
    # split them with the same regex — pure hyphen-split was a happy accident.
    cat_words_4 = {w for w in re.findall(r'[a-z0-9]+', cat_lower) if len(w) >= 4}
    if cat_words_4:
        for key, node in nodes.items():
            if key in matched_keys:
                continue
            key_words = {w for w in re.findall(r'[a-z0-9]+', key.lower()) if len(w) >= 4}
            if any(cw.startswith(kw) or kw.startswith(cw)
                   for cw in cat_words_4 for kw in key_words):
                matched.append((key, node))
                matched_keys.add(key)
                channels[key] = "word_prefix"

    # Strategy 4: Concept — match query tokens against .md front-matter entities
    cat_tokens = {w for w in re.findall(r'[a-z0-9]+', cat_lower) if len(w) >= 3}
    if cat_tokens:
        for term, term_nodes in concept_index.items():
            # Check if any query token matches this entity term
            hit = False
            for token in cat_tokens:
                if token == term or token.startswith(term) or term.startswith(token):
                    hit = True
                    break
            if hit:
                for nk in term_nodes:
                    if nk in nodes and nk not in matched_keys:
                        matched.append((nk, nodes[nk]))
                        matched_keys.add(nk)
                        channels[nk] = "concept"

    return matched, matched_keys, channels


def _include_siblings(matched, matched_keys, channels, nodes):
    """Add siblings of D3+ direct matches for related context.

    Only applies to direct matches (not parent/sibling-included) at depth >= 3.
    Avoids pulling all 21 L2 children of intelligence when a single L2 matches.
    """
    direct_channels = {"exact_key", "substring", "entity_index", "concept", "word_prefix"}
    sibling_keys = set()
    for key, node in list(matched):
        depth = node.get("depth", 0)
        if depth >= 3 and channels.get(key) in direct_channels:
            parent_key = node.get("parent")
            if parent_key and parent_key in nodes:
                for sibling in nodes[parent_key].get("children", []):
                    if sibling != key and sibling not in matched_keys:
                        sibling_keys.add(sibling)

    for sk in sibling_keys:
        if sk in nodes:
            matched.append((sk, nodes[sk]))
            matched_keys.add(sk)
            channels[sk] = "sibling"

    return matched, matched_keys, channels


def _include_parents(matched, matched_keys, channels, nodes):
    """Add parent nodes for context when we matched L2+ nodes."""
    parent_keys = set()
    for key, node in matched:
        parent = node.get("parent")
        if parent and parent not in matched_keys:
            parent_keys.add(parent)

    for pk in parent_keys:
        if pk in nodes:
            matched.append((pk, nodes[pk]))
            matched_keys.add(pk)
            channels[pk] = "parent"

    return matched, matched_keys, channels


def _path_chain(nodes, key, max_hops=20):
    """Walk parent pointers from key to root. Returns the list of keys in
    root-first order. Cycle-safe via visited set; max_hops bounds runaway
    in malformed trees."""
    chain = []
    visited = set()
    current = key
    hops = 0
    while current and current not in visited and hops < max_hops:
        visited.add(current)
        chain.append(current)
        current = nodes.get(current, {}).get("parent")
        hops += 1
    chain.reverse()
    return chain


def _path_similarity(chain_a, chain_b):
    """Common-ancestor-prefix length / max chain length. Range [0, 1].

    Two siblings (share all ancestors except themselves) at depth 5:
        common=5, max_len=6, sim ≈ 0.83
    Cousins (different L4 parents) at depth 6:
        common=4, max_len=7, sim ≈ 0.57
    Different L1 branches:
        common=1 (root), max_len=N, sim ≈ 1/N (low)

    Pre-tokenized chains expected so callers can cache `_path_chain` per
    candidate (called O(K) times during MMR; without cache it's O(K^2)).
    """
    common = 0
    for a, b in zip(chain_a, chain_b):
        if a == b:
            common += 1
        else:
            break
    longest = max(len(chain_a), len(chain_b), 1)
    return common / longest


def _mmr_rerank(scored, all_nodes, limit, lambda_=None):
    """Re-rank a sorted-by-relevance list using normalized MMR for diversity.

    `scored` is a list of tuples whose first element is the node key and
    third element is the (relevance) score — matches both the lightweight
    `_score_and_limit` shape (key, node, score, channel) and retrieve.py's
    `_score_weight_limit` shape (key, node, effective, channel, base, util_w).
    Returns at most `limit` items in MMR-selected order.

    Normalization: each candidate's relevance is divided by max_score so
    both MMR terms live in [0, 1]. Without normalization the relevance
    term (3-7 typical) would swamp the path-similarity term (always [0, 1])
    and MMR would degenerate to pure relevance.

    No-op when len(scored) <= limit — returns the input unchanged. The
    cost path only fires when the cap actually binds.
    """
    if not scored:
        return []
    if len(scored) <= limit:
        return list(scored)
    if lambda_ is None:
        lambda_ = MMR_LAMBDA

    max_score = scored[0][2] if scored[0][2] > 0 else 1.0
    chains = {item[0]: _path_chain(all_nodes, item[0]) for item in scored}

    selected = [scored[0]]
    remaining = list(scored[1:])

    while len(selected) < limit and remaining:
        best_idx = -1
        best_mmr = -float("inf")
        for i, cand in enumerate(remaining):
            cand_key = cand[0]
            cand_score = cand[2]
            normalized_rel = cand_score / max_score
            cand_chain = chains[cand_key]
            max_sim = max(
                _path_similarity(cand_chain, chains[sel[0]])
                for sel in selected
            )
            mmr = lambda_ * normalized_rel - (1 - lambda_) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        if best_idx < 0:
            break
        selected.append(remaining.pop(best_idx))

    return selected


def _compute_match_score(key, node, channel, cfg=None):
    """Score a matched node by match quality. Higher = more relevant.

    ``cfg`` is the optional retrieval config dict (from tree.yaml retrieval:
    section).  When provided, it is threaded to ``_recency_bonus`` for M-4
    rehearsal-adaptive TAU.  When ``None`` (lightweight lookups via
    ``_score_and_limit``), hardcoded module-level defaults are used.
    """
    score = CHANNEL_SCORES.get(channel, 0.5)

    # Depth bonus: deeper = more specific knowledge (inverted from old depth-first)
    depth = safe_num(node.get("depth", 0), 0.0, rid=key, field="depth")
    if depth >= 3:
        score += 1.5
    elif depth == 2:
        score += 1.0
    elif depth == 1:
        score += 0.3

    # Confidence (0-1 range), modulated by provenance weight (M-5).
    # DIRECT observations get full confidence credit; INFERRED/SYNTHESIZED/
    # HEARSAY get progressively less.  Legacy nodes without provenance tag
    # get DEFAULT_PROVENANCE_WEIGHT (0.9).  YAML null -> Python None; guard.
    # safe_num (g-357-49): a non-numeric confidence (quoted string, list) was
    # `sequence * float` here — one malformed record crashed the whole query.
    score += safe_num(node.get("confidence") or 0, 0.0,
                      rid=key, field="confidence") * _provenance_weight(node)

    # Capability bonus (str() guard: an unhashable capability_level must cost
    # only its own bonus, not the query — same g-357-49 class)
    _cl = node.get("capability_level", "")
    score += CAPABILITY_BONUS.get(_cl if isinstance(_cl, str) else str(_cl), 0)

    # Recency bonus (asymmetric -- recently-updated content gets a boost,
    # stable old content gets no penalty). See _recency_bonus.
    score += _recency_bonus(node, cfg=cfg)

    return score


def _score_and_limit(matched, channels, limit, query_text="", all_nodes=None):
    """Score all matched nodes, sort by match quality, apply limit.

    When `query_text` and `all_nodes` are both provided, augment each base
    score with a TF-IDF cosine-similarity bonus computed against the full
    `all_nodes` corpus (NOT just `matched` — IDF needs the whole corpus to
    weight tokens correctly). Callers that only need channel-based scoring
    (legacy lightweight lookups) pass neither and get identical pre-IDF
    behavior. See COSINE_BONUS_WEIGHT for the tunable.
    """
    idf_index = None
    q_vm = None
    if query_text and all_nodes:
        from tree_idf import build_index, query_vector
        idf_index = build_index(all_nodes)
        q_vm = query_vector(query_text, idf_index["idf"])

    if idf_index is not None:
        from tree_idf import cosine

    scored = []
    for key, node in matched:
        channel = channels.get(key, "parent")
        ms = _compute_match_score(key, node, channel)
        if idf_index is not None:
            d_vm = idf_index["vectors"].get(key, ({}, 0.0))
            ms += COSINE_BONUS_WEIGHT * cosine(q_vm, d_vm)
        scored.append((key, node, ms, channel))

    scored.sort(key=lambda x: -x[2])
    if all_nodes and len(scored) > limit:
        return _mmr_rerank(scored, all_nodes, limit)
    return scored[:limit]


# ---------------------------------------------------------------------------
# High-level find function (no sibling/parent inclusion)
# ---------------------------------------------------------------------------

def find_nodes(text, nodes, entity_index, top=3, leaf_only=False):
    """Find best-matching tree nodes for a text query.

    Unlike full retrieval, this does NOT include siblings/parents and does NOT
    read .md content or update retrieval counters. Designed for lightweight
    node lookup (e.g., tree-find-node.sh, tree.py read --find).

    Args:
        text: query string
        nodes: dict of node_key -> node_dict from _tree.yaml
        entity_index: entity_index dict from _tree.yaml
        top: max results to return (default 3)
        leaf_only: if True, filter to leaf nodes (empty children) before scoring

    Returns:
        list of dicts: [{key, score, file, depth, summary, node_type}, ...]
    """
    # Optionally filter to leaves
    if leaf_only:
        nodes = {k: v for k, v in nodes.items() if not v.get("children")}

    # Build concept index and run matching
    concept_index = build_concept_index(nodes)
    matched, matched_keys, channels = _match_nodes(
        text, nodes, entity_index, concept_index
    )

    # Score and limit (no sibling/parent inclusion). Pass query+nodes so the
    # cosine bonus is layered onto channel scores — see _score_and_limit.
    scored = _score_and_limit(matched, channels, top,
                              query_text=text, all_nodes=nodes)

    # Build lightweight result dicts
    results = []
    for key, node, match_score, channel in scored:
        children = node.get("children", [])
        results.append({
            "key": key,
            "score": round(match_score, 2),
            "file": node.get("file", ""),
            "depth": node.get("depth", 0),
            "summary": node.get("summary", ""),
            "node_type": node.get("node_type", "leaf" if not children else "interior"),
        })

    return results
