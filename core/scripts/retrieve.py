#!/usr/bin/env python3
"""Unified retrieval engine — single script call replaces the 5-phase retrieval protocol.

Reads ALL relevant data stores (tree nodes, reasoning bank, guardrails, pattern
signatures, experiences, beliefs, experiential index), increments retrieval
counters, and returns a single JSON blob to stdout.

Usage:
    retrieve.sh --category <cat> --depth shallow|medium|deep   # metadata-only (default)
    retrieve.sh --category <cat> --full-content                # opt-in full bodies
    retrieve.sh --category "cat1,cat2" --depth medium          # multi-category
    retrieve.sh --supplementary-only --category <cat>          # skip tree nodes

DEFAULT IS METADATA-ONLY (Paper-Idea-1, 2026-04-23). Returns node keys,
summaries, match scores, utility counters — but NULLS the long-form body
text in supplementary stores (reasoning bank `content`, meta lesson `content`,
pattern signature long `description`). Forces the LLM to triage before
deep-reading. Request supplementary bodies explicitly with `--full-content`
when the triage decision is already made.

Tree node `.md` BODIES ARE NEVER RETURNED INLINE, in either mode. Retrieve
is the tree index; the LLM uses the Read tool on `entry["file"]` for any node
body it actually needs. Guardrail `rule` is preserved in both modes because
rules are short AND are the actionable content.

`--depth` controls TWO things, both depth-aware since 2026-05-09:
  1. Sibling/parent inclusion on tree-node matching: `deep` (default) adds
     D3+ direct-match siblings + matched-node parents after the direct-match
     phase. `shallow` and `medium` skip both — thin results from sparse
     categories are honest signal, not padding (gated 2026-04-23 after
     diagnostic showed parent/sibling channels contributed most retrieved-
     but-never-helpful entries; see the inline comment at the depth==deep
     branch).
  2. Supplementary-store cap: SUPPLEMENTARY_CAPS = {shallow:20, medium:40,
     deep:80} bounds reasoning_bank, guardrails, pattern_signatures
     output AFTER category filtering and utility sorting. Pre-2026-05-09
     these stores returned ALL active records (280-705 RB / 328 guardrails
     per call) regardless of category — the audit found ~75% had
     utilization_score=0. Counter-bump gating was already category-filtered
     since 2026-04-23; the result-filter (P0 #1) brings the returned set
     into alignment.

Experiences are governed by EXP_LIMITS (10/15/25). Beliefs and
experiential_index remain unfiltered/uncapped — beliefs are tiny and
experiential_index is already category-keyed at the file level.

Use --supplementary-only to skip tree node matching and only load reasoning bank,
guardrails, pattern signatures, experiences, beliefs, and experiential index.

Matching strategies (applied in order, results merged):
  1. Substring: category appears in key/summary/topic (bidirectional)
  2. Entity index: category matches a semantic entity in _tree.yaml
  3. Word-prefix: hyphen-split words, prefix match (min 4 chars)
  4. Concept: .md front-matter entities matched against query tokens

Results are scored by match quality (not depth-first), so specific deep nodes
rank above generic parents when they match directly. Sibling inclusion adds
related D3+ nodes for context.
"""

import json
import os
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

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR, CONFIG_DIR
from _rb_helpers import is_universal_rb, sort_universal_rbs
from trigger_firings import record_firing  # g-304-07 telemetry — fail-open inside

# Universal meta-lessons cap in retrieve output — prevents framework-category
# entries from flooding domain retrieval. Tuned: 5 is enough to surface the
# top-utility meta-lessons without dominating the reasoning_bank result.
UNIVERSAL_RB_CAP = 5

# Collective domain stores (world/)
TREE_PATH = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
RB_PATH = WORLD_DIR / "reasoning-bank.jsonl"
GUARD_PATH = WORLD_DIR / "guardrails.jsonl"
SIGS_PATH = WORLD_DIR / "pattern-signatures.jsonl"
BELIEFS_PATH = WORLD_DIR / "knowledge" / "beliefs.yaml"

# Per-agent stores (agent directory)
EXP_PATH = AGENT_DIR / "experience.jsonl" if AGENT_DIR else None
EI_PATH = AGENT_DIR / "experiential-index.yaml" if AGENT_DIR else None

# Depth-differentiated limits (reintroduced 2026-04-23 after unification proved
# too broad). The 50/50/50 unification assumed "retrieval intelligence is in the
# LLM, not here" — but empirically the LLM couldn't triage 50+ tree nodes plus
# the full reasoning-bank and guardrails dumps per prime, collapsing positive
# feedback: 94% of rb and 100% of guardrails stayed at times_helpful=0.
# Tighter limits on shallow/medium force the scorer to surface only the best
# matches; deep stays wide for full-context exploration (reflection, research).
# See g-242-05/06 diagnostics + 2026-04-23 joint feedback-pipeline diagnosis.
DEPTH_LIMITS = {"shallow": 15, "medium": 30, "deep": 50}
EXP_LIMITS = {"shallow": 10, "medium": 15, "deep": 25}

# Supplementary-store result caps (2026-05-09: P0 #1 from knowledge-system audit).
# Pre-fix, load_reasoning_bank / load_guardrails / load_pattern_signatures
# returned ALL active records regardless of category — every retrieval flooded
# the LLM with 280-705 RB entries + 328 guardrails. The audit found ~75% of
# those entries had utilization_score=0 (never helped a single decision after
# being retrieved). Counter-bump gating already filters by category since
# 2026-04-23, but the RETURNED set was not. This cap closes that gap:
# entries are filtered by `_entry_matches_category`, sorted by utility, and
# capped here. Deep stays generous for full-context exploration; shallow stays
# tight for quick lookups. Universal RBs are partitioned out before this cap
# applies (UNIVERSAL_RB_CAP=5 governs them).
SUPPLEMENTARY_CAPS = {"shallow": 20, "medium": 40, "deep": 80}

# Matching engine imported from shared module
from tree_match import (
    build_concept_index, _match_nodes, _include_siblings,
    _include_parents, _score_and_limit, _compute_match_score, CHANNEL_SCORES,
    COSINE_BONUS_WEIGHT, _mmr_rerank,
)

def _infer_in_flight_goal_id():
    """Infer the agent's current in-flight goal_id from team-state.yaml.

    Returns None if no AYOAI_AGENT binding, team-state missing, or no in_flight
    entry. Called when --goal is absent so utilization-feedback still fires —
    skills pass only --category (not --goal), which before this inference left
    retrieval-session.json unwritten and distill candidates with times_helpful=0
    AND times_noise=0. See g-115-137.
    """
    agent = os.environ.get("AYOAI_AGENT")
    if not agent or WORLD_DIR is None:
        return None
    ts_path = WORLD_DIR / "team-state.yaml"
    if not ts_path.exists():
        return None
    try:
        with open(ts_path, "r", encoding="utf-8") as f:
            ts = yaml.safe_load(f) or {}
    except Exception:
        return None
    status = (ts.get("agent_status") or {}).get(agent) or {}
    inflight = status.get("in_flight")
    if not inflight or not isinstance(inflight, dict):
        return None
    gid = inflight.get("goal_id")
    return gid if isinstance(gid, str) and gid else None

# ---------------------------------------------------------------------------
# Helpers: file I/O (same patterns as experience.py, pipeline.py)
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read JSONL file, return list of dicts. Returns [] if missing/empty."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items

def read_yaml(path):
    """Read YAML file, return dict. Returns {} if missing/empty."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}

def _locked_bump_jsonl(path, should_bump_fn, counter_path=("utilization", "retrieval_count"),
                      timestamp_path=("utilization", "last_retrieved")):
    """Read JSONL under lock, bump retrieval counters on matching records, write back.

    Closes the read-modify-write race against `*-add.sh` / `reasoning-bank.py`
    writers that ALSO lock this path. Without the shared lock, retrieve.py
    would read a snapshot, bump counters, and overwrite later writes from
    other agents (rb-add, guardrails-add, pattern-signatures-add, experience
    archival) — those writes would be silently lost.

    Args:
        path: Path to the JSONL file.
        should_bump_fn: Callable receiving each record dict; True → bump.
        counter_path: Tuple of nested dict keys identifying the counter field.
            Default ("utilization", "retrieval_count") matches reasoning-bank,
            guardrails, and pattern-signatures. Pass
            ("retrieval_stats", "retrieval_count") for experience.jsonl.
        timestamp_path: Tuple identifying the last_retrieved field. Same pair
            as counter_path with `_count` → `_retrieved` rename.

    Returns the (possibly bumped) records list. Returns the original (un-bumped)
    snapshot when the file does not exist — callers should handle empty.
    """
    from _fileops import (acquire_lock, release_lock, save_history,
                          append_changelog, resolve_base_dir, _agent_name,
                          _validate_no_surrogates, _atomic_write_with_fallback)
    p = Path(path)
    if not p.exists():
        return []
    base_dir = resolve_base_dir(p)
    lock_path = p.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # Read inside the lock — captures the post-writer state, not whatever
        # was on disk before another agent's locked append landed.
        records = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    records.append(json.loads(stripped))

        today = today_str()
        modified = False
        for rec in records:
            if not should_bump_fn(rec):
                continue
            # Walk counter_path / timestamp_path setting intermediate dicts.
            # setdefault chain mirrors the pre-lock pattern (`util = rec.setdefault("utilization", {})`).
            target = rec
            for k in counter_path[:-1]:
                target = target.setdefault(k, {})
            target[counter_path[-1]] = target.get(counter_path[-1], 0) + 1
            target = rec
            for k in timestamp_path[:-1]:
                target = target.setdefault(k, {})
            target[timestamp_path[-1]] = today
            modified = True

        if not modified:
            return records

        # g-276-03 mirror: validate post-modify, pre-write. The walk is cheap
        # and short-circuits on the kill-switch. Aligns retrieve.py writes
        # with the surrogate-gate discipline the rest of _fileops uses.
        for item in records:
            _validate_no_surrogates(item, p)

        agent = _agent_name()
        if base_dir:
            save_history(p, base_dir, agent)

        def _write(handle):
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=True) + "\n")
        _atomic_write_with_fallback(
            p, _write, fallback_counter_key="retrieve_locked_bump_jsonl")

        if base_dir:
            append_changelog(base_dir, agent, p, "edit",
                             lines_changed=len(records))
        return records
    finally:
        release_lock(lock_path)

def today_str():
    return date.today().isoformat()

def now_str():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

# ---------------------------------------------------------------------------
# Tree node loading (main entry point for tree retrieval)
# ---------------------------------------------------------------------------

def load_tree_nodes(categories, depth, read_only=False):
    """Load matching tree nodes for one or more categories.

    Args:
        categories: list of category strings (supports multi-category)
        depth: "shallow", "medium", or "deep"
        read_only: if True, skip retrieval counter increments

    Returns list of index entries (key, file, summary, scores, match metadata —
    no inline body content; LLM reads node .md via Read tool after triage).
    """
    if not TREE_PATH.exists():
        return [], set()

    tree = read_yaml(TREE_PATH)
    nodes = tree.get("nodes", {})
    if not nodes:
        return [], set()

    limit = DEPTH_LIMITS.get(depth, 50)

    # Build concept index once (shared across multi-category)
    concept_index = build_concept_index(nodes)
    entity_index = tree.get("entity_index", {})

    # Match across all categories, merge with dedup (keep best channel)
    all_matched = {}  # key -> node
    all_channels = {}  # key -> best channel
    all_matched_keys = set()

    for cat in categories:
        cat_matched, cat_keys, cat_channels = _match_nodes(
            cat, nodes, entity_index, concept_index
        )
        for key, node in cat_matched:
            if key not in all_matched:
                all_matched[key] = node
                all_channels[key] = cat_channels.get(key, "substring")
                all_matched_keys.add(key)
            else:
                # Keep the higher-scoring channel
                existing = CHANNEL_SCORES.get(all_channels.get(key, ""), 0)
                new = CHANNEL_SCORES.get(cat_channels.get(key, ""), 0)
                if new > existing:
                    all_channels[key] = cat_channels[key]

    # Convert to list form for sibling/parent inclusion
    matched = [(k, v) for k, v in all_matched.items()]

    # Sibling/parent inclusion broadens the match set with peripherally-related
    # nodes — useful at `deep` for full-context exploration, too noisy at
    # `shallow`/`medium` where the LLM needs specific matches. 2026-04-23: gated
    # by depth after diagnostic showed sibling/parent channels contributed most
    # of the "retrieved but never helpful" entries. Sparse categories returning
    # thin results at shallow/medium IS honest signal — do not pad.
    if depth == "deep":
        matched, all_matched_keys, all_channels = _include_siblings(
            matched, all_matched_keys, all_channels, nodes
        )
        matched, all_matched_keys, all_channels = _include_parents(
            matched, all_matched_keys, all_channels, nodes
        )

    # Score, apply utility weighting, and limit (Phase 1.5 of curation plan).
    # Reweights base match score by each node's utility_ratio so proven-helpful
    # nodes outrank zero-utility ones; new nodes get neutral weight 1.0.
    # Joined-categories query feeds the TF-IDF cosine bonus so multi-token
    # specific matches outrank generic-token parents (NOISY-leaf fix).
    query_text = " ".join(c for c in categories if c)
    scored = _score_weight_limit(matched, all_channels, limit,
                                 query_text=query_text, all_nodes=nodes)

    # Build results with match metadata (tree bodies never inline — see below).
    # Snapshot match metadata from `node` (the unlocked-read view); the
    # retrieval_count bump runs in a SECOND pass under lock so concurrent
    # tree.py writes (decompose, propagate, reflect-tree-update) cannot lose
    # our increments. Without this split, alpha's autonomous loop would
    # silently drop counter bumps every time /tree maintain or /reflect-tree
    # ran in the same iteration window.
    results = []
    matched_keys_to_bump = []
    retrieval_channels_used = set()

    for key, node, effective_score, channel, base_score, util_weight in scored:
        entry = {
            "key": key,
            "file": node.get("file", ""),
            "summary": node.get("summary", ""),
            "depth": node.get("depth", 0),
            "confidence": node.get("confidence", 0),
            "capability_level": node.get("capability_level", ""),
            "match_channel": channel,
            "match_score": round(base_score, 2),
            "utility_weight": round(util_weight, 3),
            "effective_score": round(effective_score, 2),
        }

        # DO NOT re-add an md_path.read_text() here. Retrieve returns the tree
        # INDEX; the LLM uses the Read tool on entry["file"] for bodies. A
        # PROJECT_ROOT-join would be wrong for external world paths, and it
        # duplicates the LLM's existing post-triage workflow. See
        # tree-retrieval.md "Tree node bodies are never returned inline".

        retrieval_channels_used.add(channel)
        results.append(entry)
        if not read_only:
            matched_keys_to_bump.append(key)

    # Write back tree with retrieval_count increments — under lock to avoid
    # racing tree.py's `write_tree()` (decompose, propagate, batch ops) which
    # acquires the same `<tree_path>.lock`. The modifier re-reads the tree
    # inside the lock, so bumps land on top of any concurrent structural
    # write rather than overwriting it.
    if matched_keys_to_bump:
        from _fileops import locked_modify_yaml
        today = today_str()

        def _bump_counters(data):
            data_nodes = (data or {}).get("nodes", {})
            for k in matched_keys_to_bump:
                n = data_nodes.get(k)
                if not n:
                    # Node may have been removed (PRUNE/RETIRE/MERGE) between
                    # the unlocked match and the locked bump. Drop silently —
                    # the retrieval was logged in `results`; the counter is
                    # incidental on a node that no longer exists.
                    continue
                n["retrieval_count"] = n.get("retrieval_count", 0) + 1
                n["last_retrieved"] = today
            data["last_updated"] = today
            return data

        locked_modify_yaml(TREE_PATH, _bump_counters)

    return results, retrieval_channels_used

# ---------------------------------------------------------------------------
# E12: Coverage-gap detection. Fires when load_tree_nodes returns empty for
# a query whose distinctive tokens appear scattered across 3+ other nodes.
# That pattern means "the topic is covered, just not as a dedicated node" —
# a signal to file knowledge_debt rather than a true "doesn't exist" miss.
# Heuristic: only length-≥5 tokens count (stopword/short-token filter); a
# token "hits" a node if it appears in node.key OR node.summary. Result is
# {query_category, populated_token, populated_node_count, sample_node_keys}.
# Returns None when no hit threshold reached.
# ---------------------------------------------------------------------------

_E12_TOKEN_RE = re.compile(r"[a-z0-9]+")
_E12_HIT_THRESHOLD = 3
_E12_MIN_TOKEN_LEN = 5

def _detect_coverage_gap(categories):
    """Return coverage-gap dict or None. See module-level comment above."""
    if not TREE_PATH.exists():
        return None
    tree = read_yaml(TREE_PATH)
    nodes = (tree or {}).get("nodes", {})
    if not nodes:
        return None
    for cat in categories:
        if not isinstance(cat, str) or not cat:
            continue
        tokens = [t for t in _E12_TOKEN_RE.findall(cat.lower())
                  if len(t) >= _E12_MIN_TOKEN_LEN]
        if not tokens:
            continue
        # Per-token hit count + sample keys for the highest-hit token only
        best = None  # (token, count, sample_keys)
        for tok in tokens:
            hit_keys = []
            for key, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                summary = (node.get("summary") or "").lower()
                if tok in key.lower() or tok in summary:
                    hit_keys.append(key)
                    if len(hit_keys) >= 5:
                        break  # cap sample size
            count = len(hit_keys)
            if count >= _E12_HIT_THRESHOLD and (best is None or count > best[1]):
                best = (tok, count, hit_keys)
        if best:
            return {
                "query_category": cat,
                "populated_token": best[0],
                "populated_node_count": best[1],
                "sample_node_keys": best[2],
            }
    return None

# ---------------------------------------------------------------------------
# Supporting data loaders. Filter active records by category match, sort by
# utility, cap at SUPPLEMENTARY_CAPS[depth]. Counter-bump logic is independent
# (locked via _locked_bump_jsonl) and uses the same category predicate so the
# returned set is a subset of the bumped set — utility_ratio invariant holds.
# ---------------------------------------------------------------------------

def _entry_matches_category(entry, categories):
    """Return True if an rb/guardrail/pattern-signature entry's category field
    intersects any requested category. Bidirectional substring match — e.g.
    "npc-intelligence-evaluation" matches a "npc-intelligence" query.

    Untagged entries and empty category lists match by default (fail-open):
    this is a counter-bump signal, not a safety gate.
    """
    entry_cat = (entry.get("category") or "").lower()
    if not categories or not entry_cat:
        return True
    for c in categories:
        cl = (c or "").lower()
        if cl and (cl in entry_cat or entry_cat in cl):
            return True
    return False

# Token splitter for the text-fallback. Pulled out of the loop body so the
# regex compiles once per Python process instead of once per entry × category.
_TEXT_FALLBACK_TOKEN_RE = re.compile(r"[a-z0-9]+")

def _entry_matches_text(entry, categories):
    """Token-overlap fallback for supplementary stores when category match fails.

    Matches free-text queries against entry title, content/rule/summary, tags,
    and when_to_use fields. Symmetry counterpart to the tree-node
    Substring/Word-prefix/Concept channels — without this, supplementary
    stores were invisible to free-text queries that did not match an exact
    category key (see core/config/conventions/retrieval-triggers.md G9).

    Match rule (single, not OR'd): a query is a hit if ≥2 distinct tokens
    of length ≥5 from the query appear in the corpus.

    The earlier draft also accepted single ≥5-char tokens (rule_a), but
    measurement on the live world store showed that rule matched 300/688
    RB entries for stopword-heavy queries like "before declaring something
    doesn't exist" — common English words like "before", "exist", "doesn"
    each hit ~half the corpus. SUPPLEMENTARY_CAPS then sorted those 300
    by utility and returned 40 — the most-cited entries regardless of
    topical relevance. Two distinct length-≥5 tokens is the threshold
    where noise drops to manageable levels while canonical entries
    (rb-774, guard-165, guard-346, guard-147) still surface for their
    motivating queries. See 2026-05-12 fresh-eyes review.

    Added 2026-05-12 for retrieval-triggers.md G9 / R3. The
    `_entry_matches_category` strict-only matcher remains the primary
    predicate; this fallback only fires when strict match returns False.
    """
    if not categories:
        return False
    # Build a token corpus from the entry's text fields.
    parts = []
    for field in ("title", "content", "rule", "summary"):
        v = entry.get(field)
        if isinstance(v, str):
            parts.append(v)
    tags = entry.get("tags")
    if isinstance(tags, list):
        parts.extend(t for t in tags if isinstance(t, str))
    when = entry.get("when_to_use")
    if isinstance(when, dict):
        cond = when.get("conditions")
        if isinstance(cond, list):
            parts.extend(s for s in cond if isinstance(s, str))
        elif isinstance(cond, str):
            parts.append(cond)
    if not parts:
        return False
    corpus = " ".join(parts).lower()
    if not corpus:
        return False
    corpus_tokens = set(_TEXT_FALLBACK_TOKEN_RE.findall(corpus))
    if not corpus_tokens:
        return False
    for q in categories:
        if not isinstance(q, str) or not q:
            continue
        q_tokens = set(_TEXT_FALLBACK_TOKEN_RE.findall(q.lower()))
        if not q_tokens:
            continue
        matched = sum(1 for t in q_tokens if len(t) >= 5 and t in corpus_tokens)
        if matched >= 2:
            return True
    return False

def _entry_matches(entry, categories):
    """Combined supplementary-store predicate: strict category match first,
    token-overlap fallback second. Used by load_reasoning_bank,
    load_guardrails, and load_pattern_signatures.

    The strict-only matcher `_entry_matches_category` remains callable
    independently for code paths that need exact-category semantics.

    Added 2026-05-12 for retrieval-triggers.md R3.
    """
    if _entry_matches_category(entry, categories):
        return True
    return _entry_matches_text(entry, categories)

def _sort_by_utility(entries):
    """In-place sort by utilization.utilization_score desc, then created desc.

    Generic counterpart to `sort_universal_rbs` — applies to any record with
    the standard `utilization` sub-object schema (RB, guardrails, pattern
    signatures). Tie-break by `created` ensures fresh entries surface above
    older ones at equal utility. Mutates and returns the list.
    """
    entries.sort(
        key=lambda r: (
            (r.get("utilization") or {}).get("utilization_score", 0) or 0,
            r.get("created", "") or "",
        ),
        reverse=True,
    )
    return entries

def load_reasoning_bank(categories, depth="medium", read_only=False):
    """Load active reasoning bank entries, partitioned into domain + universal.

    Universal entries (framework-* category OR applies_to in {any, framework})
    are always surfaced as meta_lessons, capped at UNIVERSAL_RB_CAP, ordered by
    utilization_score desc then recency. Domain entries are filtered by
    `_entry_matches` (strict category, then token-overlap fallback), sorted by
    `utilization.utilization_score` desc then `created` desc, and capped at
    SUPPLEMENTARY_CAPS[depth].

    INVARIANT (utility_ratio alignment, 2026-05-09 fresh-eyes-fix): the bump
    set MUST equal the return set. retrieval_count is bumped ONLY on the
    records actually returned (post-filter, post-sort, post-cap). Mirror in
    utilization-feedback.py increment_supplementary: helpful++ fires only on
    `session.supplementary_items`, which is built from the return set. If
    bump and return diverge, `helpful/rc` underestimates true helpfulness for
    bumped-but-cap-rejected records — utility_ratio drifts toward 0, the
    record sinks in ranking, never gets returned, never recovers. That was
    the post-P0 #1 / pre-fresh-eyes bug. The fresh-eyes-fix realigns them.

    DO NOT bump unconditionally on `is_universal_rb(rec) or
    _entry_matches_category(rec, categories)` — that is the predicate that
    decides ELIGIBILITY, but the cap is what decides RETURN. Bump on RETURN.

    Counter writes route through `_locked_bump_jsonl` so the locked
    read-modify-write does not clobber concurrent `reasoning-bank-add.sh`
    writes from the partner agent. The two-phase pattern (snapshot read for
    ranking, locked bump for the return-set IDs) has a small TOCTOU window
    — a record added between snapshot and lock won't be bumped this call,
    next call picks it up. Acceptable.
    """
    cap = SUPPLEMENTARY_CAPS.get(depth, SUPPLEMENTARY_CAPS["medium"])
    records = read_jsonl(RB_PATH)
    active = [r for r in records if r.get("status") == "active"]
    universal = [r for r in active if is_universal_rb(r)]
    domain = [r for r in active if not is_universal_rb(r)
              and _entry_matches(r, categories)]
    _sort_by_utility(domain)
    domain = domain[:cap]
    sort_universal_rbs(universal)
    universal = universal[:UNIVERSAL_RB_CAP]

    if not read_only:
        bump_ids = {r["id"] for r in domain} | {r["id"] for r in universal}

        def _should_bump(rec):
            return (rec.get("id") in bump_ids
                    and rec.get("status") == "active")

        _locked_bump_jsonl(RB_PATH, _should_bump)

    return domain, universal

def load_guardrails(categories, depth="medium", read_only=False):
    """Load active guardrails matching the requested categories.

    Filtered by `_entry_matches` (strict category, then token-overlap fallback), sorted by
    `utilization.utilization_score` desc then `created` desc, capped at
    SUPPLEMENTARY_CAPS[depth].

    INVARIANT (utility_ratio alignment): bump fires only on the records
    actually returned. Mirrored by utilization-feedback.py
    increment_supplementary which targets `session.supplementary_items`.
    See load_reasoning_bank docstring for the rationale and incident history.
    Concurrent `guardrails-add.sh` writes are protected by the lock.
    """
    cap = SUPPLEMENTARY_CAPS.get(depth, SUPPLEMENTARY_CAPS["medium"])
    records = read_jsonl(GUARD_PATH)
    active = [r for r in records if r.get("status") == "active"]
    filtered = [r for r in active if _entry_matches(r, categories)]
    _sort_by_utility(filtered)
    filtered = filtered[:cap]

    if not read_only:
        bump_ids = {r["id"] for r in filtered}

        def _should_bump(rec):
            return (rec.get("id") in bump_ids
                    and rec.get("status") == "active")

        _locked_bump_jsonl(GUARD_PATH, _should_bump)

    return filtered

def load_pattern_signatures(categories, depth="medium", read_only=False):
    """Load active pattern signatures matching the requested categories.

    Filtered by `_entry_matches` (strict category, then token-overlap fallback), sorted by utilization, capped at
    SUPPLEMENTARY_CAPS[depth]. Pattern signatures are tiny (~5 active today)
    so the cap rarely binds — the filter is what matters when the corpus grows.

    INVARIANT (utility_ratio alignment): bump fires only on returned records.
    See load_reasoning_bank docstring. Concurrent `pattern-signatures-add.sh`
    writes are protected by the lock.
    """
    cap = SUPPLEMENTARY_CAPS.get(depth, SUPPLEMENTARY_CAPS["medium"])
    records = read_jsonl(SIGS_PATH)
    active = [r for r in records if r.get("status") == "active"]
    filtered = [r for r in active if _entry_matches(r, categories)]
    _sort_by_utility(filtered)
    filtered = filtered[:cap]

    if not read_only:
        bump_ids = {r["id"] for r in filtered}

        def _should_bump(rec):
            return (rec.get("id") in bump_ids
                    and rec.get("status") == "active")

        _locked_bump_jsonl(SIGS_PATH, _should_bump)

    return filtered

# ---------------------------------------------------------------------------
# Framework rules + conventions (G8, 2026-05-12 — retrieval-triggers.md).
#
# Until this loader landed, the F store (.claude/rules/*.md,
# core/config/conventions/*.md, world/conventions/*.md) was reachable only
# by exact convention key (`load-conventions.sh <name>` → returns path if
# not yet in context). Agents could read a rule they already knew the name
# of, but could NOT retrieve "the rule that covers X" by topic. G8 in
# core/config/conventions/retrieval-triggers.md flagged this as the last
# remaining trigger gap; this loader closes it.
#
# Single source of truth: the index is rebuilt on every retrieve.sh call.
# Corpus is ~94 files today and the body sample per file is capped at 500
# chars — reading all three globs costs O(ms), negligible against the
# tree-match + JSONL reads this script already does. No parallel YAML, no
# cache state to invalidate. If the corpus grows past ~500 files this can
# swap to an mtime-keyed cache; rebuild-every-call is the simplest correct
# form at the current size.
# ---------------------------------------------------------------------------

FRAMEWORK_RULES_DIR = PROJECT_ROOT / ".claude" / "rules"
FRAMEWORK_CORE_CONVENTIONS_DIR = CONFIG_DIR / "conventions"
# WORLD_DIR is always a Path (never None) thanks to the fallback chain in
# _paths.py; the `.exists()` check below in `_framework_file_sources` handles
# fresh worlds where the conventions subdir is absent.
FRAMEWORK_WORLD_CONVENTIONS_DIR = WORLD_DIR / "conventions"

# Tier ordering. Rules apply across every domain, core conventions are the
# next-broadest scope, world conventions are domain-specific. Sorting by
# tier surfaces higher-leverage hits first when a query matches multiple
# files.
_FRAMEWORK_TIER_RANK = {"rule": 0, "core-convention": 1, "world-convention": 2}

# Body sample size. Enough text for token-overlap matching AND for the LLM
# to decide whether to Read the full file, without bloating retrieve output.
_FRAMEWORK_BODY_SAMPLE_CHARS = 500

# Cap on returned framework rule entries. Corpus is ~94 files today; 15 is
# tight enough to keep the result focused but loose enough that genuine hits
# on multi-token queries surface. Symmetric in spirit with SUPPLEMENTARY_CAPS
# shallow=20 but tighter because the corpus is smaller.
FRAMEWORK_RULES_CAP = 15

_YAML_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
# Header regex applied line-by-line (NOT against the full body) so we can skip
# lines inside fenced code blocks — `# foo` and `## foo` lines inside ``` fences
# are example code, not document structure. Audited 2026-05-12: 138 spurious
# header captures (board.md / coordination.md / agent-spawning.md /
# rationale-extraction.md etc.) would polute the matcher corpus without the
# fence skip. Indented code blocks (4-space prefix) naturally fail this regex
# because `#` would no longer be at column 0.
_FRAMEWORK_HEADER_RE = re.compile(r"^#{2,4}\s+(.+)$")

def _framework_file_sources():
    """Yield (path, source_tier) for every framework rule + convention markdown file.

    Three roots, fixed order: .claude/rules > core/config/conventions >
    world/conventions. Order within each tier is glob-sorted for stability.
    World conventions are skipped silently when WORLD_DIR/conventions is
    missing (fresh world or world-only prime path).
    """
    if FRAMEWORK_RULES_DIR.exists():
        for p in sorted(FRAMEWORK_RULES_DIR.glob("*.md")):
            yield p, "rule"
    if FRAMEWORK_CORE_CONVENTIONS_DIR.exists():
        for p in sorted(FRAMEWORK_CORE_CONVENTIONS_DIR.glob("*.md")):
            yield p, "core-convention"
    if FRAMEWORK_WORLD_CONVENTIONS_DIR.exists():
        for p in sorted(FRAMEWORK_WORLD_CONVENTIONS_DIR.glob("*.md")):
            yield p, "world-convention"

def _build_framework_index():
    """Build framework rule + convention index entries from disk.

    Field names are chosen for compatibility with `_entry_matches_text`
    (which scans `title`, `content`, `tags`, `summary`, `when_to_use`).
    Semantics map to the user-facing spec:
      title       — H1 of the file, or filename stem fallback
      content     — first 500 chars of body, post-frontmatter (the
                    "body sample" the LLM uses to decide whether to Read)
      tags        — all H2/H3/H4 header lines (section names contribute
                    discriminative tokens, e.g. "Anti-patterns",
                    "Multi-signal requirement", "Pre-Completion Review")
      path        — repo-relative path for display; absolute when the
                    file lives outside PROJECT_ROOT (world conventions
                    on an external drive)
      source_tier — rule / core-convention / world-convention
    """
    entries = []
    for path, tier in _framework_file_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            # Unreadable file — skip silently rather than fail the whole
            # retrieve call. The index is opportunistic; one bad file
            # should not block every other framework rule from surfacing.
            continue
        # Strip optional YAML front matter. Most rule/convention files
        # have none; a few use `domain-leak-exempt:` markers etc.
        fm = _YAML_FRONTMATTER_RE.match(text)
        body = text[fm.end():] if fm else text
        # Title: first H1 outside any fenced block, fallback to filename
        # stem. Section headers (H2/H3/H4) are also collected as
        # token-overlap fodder. Single pass over the body:
        #   - track ``` fences so example `# foo` / `## foo` inside code
        #     blocks don't get captured as real document structure
        #   - first non-fenced `# ` line wins as title; subsequent `# `
        #     lines (rare) are ignored
        title = path.stem.replace("-", " ").replace("_", " ").title()
        headers = []
        in_fence = False
        found_h1 = False
        for line in body.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            stripped = line.strip()
            if not found_h1 and stripped.startswith("# "):
                title = stripped[2:].strip()
                found_h1 = True
                continue
            m = _FRAMEWORK_HEADER_RE.match(line)
            if m:
                headers.append(m.group(1).strip())
        # Body sample.
        sample = body[:_FRAMEWORK_BODY_SAMPLE_CHARS]
        # Display path — repo-relative when possible, absolute otherwise.
        # Normalize to forward slashes for consistency with tree_node `file`
        # paths (which are canonical forward-slash because they're stored in
        # YAML). A caller substring-matching `rules/verify-before-assuming`
        # should hit on every platform, not just POSIX.
        try:
            rel = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(path).replace("\\", "/")
        entries.append({
            "path": rel,
            "title": title,
            "tags": headers,
            "content": sample,
            "source_tier": tier,
        })
    return entries

def load_framework_rules(categories):
    """Return framework rule + convention entries matching the requested categories.

    Reuses `_entry_matches_text` (token-overlap) so free-text queries find
    framework rules on the same surface as reasoning bank / guardrails.
    No side effects — no counter writes, no JSONL bumps, no cache state.
    Sorted by tier (rule > core-convention > world-convention) then path
    for stability; capped at FRAMEWORK_RULES_CAP.

    Returns [] when categories is empty/falsy — symmetric with
    `_entry_matches_text`, which requires at least one query token.
    """
    if not categories:
        return []
    entries = _build_framework_index()
    matches = [e for e in entries if _entry_matches_text(e, categories)]
    matches.sort(key=lambda e: (_FRAMEWORK_TIER_RANK.get(e["source_tier"], 99),
                                e["path"]))
    return matches[:FRAMEWORK_RULES_CAP]

def load_experiences(categories, depth, read_only=False):
    """Load top N experiences matching any category. Increment retrieval counters unless read_only.

    Counter writes route through `_locked_bump_jsonl` (with the
    `retrieval_stats.*` field path — experiences nest counters there rather
    than under `utilization`) so concurrent experience-archive writes from
    aspirations-execute are not clobbered."""
    if not EXP_PATH:
        return []
    records = read_jsonl(EXP_PATH)
    limit = EXP_LIMITS.get(depth, 5)

    # Filter by any category match + not archived
    matching = []
    for r in records:
        if r.get("archived", False):
            continue
        exp_cat = r.get("category", "").lower()
        if any(c.lower() in exp_cat for c in categories):
            matching.append(r)

    # Sort by retrieval_count descending (most-proven first)
    matching.sort(
        key=lambda r: r.get("retrieval_stats", {}).get("retrieval_count", 0),
        reverse=True,
    )

    selected = matching[:limit]

    if not read_only and selected:
        selected_ids = {r["id"] for r in selected}

        def _should_bump(rec):
            return rec.get("id") in selected_ids

        # `selected` was computed from the unlocked snapshot; the locked write
        # re-reads, bumps the same IDs (when still present), and persists.
        # We discard the locked-read return value because `selected` is the
        # caller's contract — keeping it stable preserves the existing
        # "top-N most-proven" semantic the LLM relies on.
        _locked_bump_jsonl(
            EXP_PATH,
            _should_bump,
            counter_path=("retrieval_stats", "retrieval_count"),
            timestamp_path=("retrieval_stats", "last_retrieved"),
        )

    return selected

def load_beliefs(categories):
    """Load active/weakened beliefs. Returns list of belief dicts."""
    beliefs_data = read_yaml(BELIEFS_PATH)
    if not beliefs_data:
        return []

    beliefs_list = beliefs_data.get("beliefs", [])
    if not isinstance(beliefs_list, list):
        return []

    return [
        b for b in beliefs_list
        if b.get("status") in ("active", "weakened")
    ]

def load_experiential_index(categories):
    """Load experiential index entries for categories."""
    if not EI_PATH:
        return {}
    ei = read_yaml(EI_PATH)
    if not ei:
        return {}

    by_cat = ei.get("by_category", {})
    merged = {}

    for cat in categories:
        cat_lower = cat.lower()
        # Try exact match first, then substring
        if cat_lower in by_cat:
            merged.update(by_cat[cat_lower])
            continue
        for key, val in by_cat.items():
            if cat_lower in key.lower() or key.lower() in cat_lower:
                merged.update(val)
                break

    return merged

# ---------------------------------------------------------------------------
# Utility-weighted retrieval ranking (Phase 1.5 of cognitive-core curation plan).
# Reweights match scores by each node's utility_ratio so proven-helpful nodes
# outrank zero-utility ones at retrieval time. New nodes (retrieval_count below
# a neutral threshold) keep weight 1.0 — can't punish what hasn't had a chance.
# Bad nodes drop out of top-K → retrieval_count stops climbing → existing
# `retrieval_count == 0 for N sessions` RETIRE rule fires naturally. Self-healing.
# ---------------------------------------------------------------------------

_TREE_CONFIG_PATH = CONFIG_DIR / "tree.yaml"

_DEFAULT_RETRIEVAL_CFG = {
    "utility_weight_min": 0.5,
    "utility_weight_max": 1.5,
    "utility_weight_neutral_below_retrievals": 5,
}

_RETRIEVAL_CFG_CACHE = None

def _load_retrieval_config():
    """Read retrieval: section of tree.yaml once per process."""
    global _RETRIEVAL_CFG_CACHE
    if _RETRIEVAL_CFG_CACHE is not None:
        return _RETRIEVAL_CFG_CACHE
    merged = dict(_DEFAULT_RETRIEVAL_CFG)
    try:
        import yaml as _yaml
        with open(_TREE_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
        merged.update(cfg.get("retrieval", {}) or {})
    except Exception:
        pass
    _RETRIEVAL_CFG_CACHE = merged
    return merged

def _utility_weight(node, cfg=None):
    """Clamp(`0.5 + utility_ratio`, min, max); neutral 1.0 for underretrieved nodes."""
    cfg = cfg or _load_retrieval_config()
    rc = node.get("retrieval_count", 0) or 0
    if rc < cfg["utility_weight_neutral_below_retrievals"]:
        return 1.0
    ur = node.get("utility_ratio", 0) or 0
    w = 0.5 + float(ur)
    lo = float(cfg["utility_weight_min"])
    hi = float(cfg["utility_weight_max"])
    if w < lo:
        return lo
    if w > hi:
        return hi
    return w

def _score_weight_limit(matched, channels, limit,
                        query_text="", all_nodes=None):
    """Score each matched node, apply utility weighting, sort, limit.
    Replaces tree_match._score_and_limit for full retrieval; the shared
    helper stays unchanged for lightweight lookups (/tree find, etc.).

    When `query_text` and `all_nodes` are both provided, augments each base
    score with a TF-IDF cosine-similarity bonus computed against the full
    corpus. The cosine signal helps specific multi-token matches outrank
    generic-token parents (the audit-driven NOISY-leaf fix). Cosine bonus
    is added to `base` before the utility weight multiplies, so a noisy
    node's low utility_weight still drags down its effective score.
    """
    cfg = _load_retrieval_config()

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
        base = _compute_match_score(key, node, channel)
        if idf_index is not None:
            d_vm = idf_index["vectors"].get(key, ({}, 0.0))
            base += COSINE_BONUS_WEIGHT * cosine(q_vm, d_vm)
        w = _utility_weight(node, cfg)
        effective = base * w
        scored.append((key, node, effective, channel, base, w))
    scored.sort(key=lambda x: -x[2])
    if all_nodes and len(scored) > limit:
        return _mmr_rerank(scored, all_nodes, limit)
    return scored[:limit]

# ---------------------------------------------------------------------------
# Token extraction for utilization-feedback `--infer` mode (Phase 1 curation).
# Produces a domain-agnostic set of distinctive tokens per retrieved item so
# the feedback heuristic can match against goal result_text/diary without any
# domain knowledge. Stopword set mirrors tree-dedup-check.py for consistency.
# ---------------------------------------------------------------------------

_UTIL_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "will", "with", "but", "not",
    "been", "being", "via", "over", "into", "than", "then",
])

_MAX_DISTINCTIVE_TOKENS = 40  # cap per item to keep session file small

def _distinctive_tokens(text):
    """Extract lowercase word-chars, filter stopwords and <3 chars, dedupe, cap."""
    if not text:
        return []
    tokens = re.findall(r"[a-z0-9]+", str(text).lower())
    seen = []
    seen_set = set()
    for t in tokens:
        if len(t) < 3 or t in _UTIL_STOPWORDS or t in seen_set:
            continue
        seen.append(t)
        seen_set.add(t)
        if len(seen) >= _MAX_DISTINCTIVE_TOKENS:
            break
    return seen

def _strip_long_form(result):
    """Strip long-form body fields from supplementary stores for metadata-only mode.

    Scope: reasoning_bank + meta_lessons (`content`, `description`) and pattern
    signature long `description`. Tree nodes are NOT touched here — they never
    carry inline body content (see `load_tree_nodes` note; tree bodies are
    always loaded via the Read tool after triage). Guardrail `rule` is preserved
    because rules are short AND ARE the actionable content. Experiences are
    preserved (already bounded by EXP_LIMITS + retrieval_count sort).

    Preserves every discriminative field the LLM needs to decide whether to
    load deeper: title, summary, when_to_use, trigger_condition, category,
    tags, utilization counters, confidence, capability_level, match_channel,
    match_score.

    Mutates result in place and returns it for chaining. Reversible only via
    re-fetch — callers wanting full bodies should pass `--full-content`.
    """
    for bucket in ("reasoning_bank", "meta_lessons"):
        for r in result.get(bucket, []) or []:
            # Keep title + when_to_use (both short, highly discriminative).
            # Drop content (multi-paragraph lesson text) + description
            # (redundant long-form when present).
            if "content" in r:
                r["content"] = None
            if "description" in r and r.get("description"):
                r["description"] = None
    for p in result.get("pattern_signatures", []) or []:
        desc = p.get("description")
        if isinstance(desc, str) and len(desc) > 240:
            # Signature descriptions occasionally balloon; truncate rather than
            # null so title-less sigs retain a handle. 240 ≈ one tweet.
            p["description"] = desc[:240].rstrip() + "…"
    return result

def _item_text_for_tokens(item, item_type):
    """Best text representation of a supplementary item for token extraction."""
    if not isinstance(item, dict):
        return ""
    if item_type == "reasoning_bank":
        return " ".join(filter(None, [
            item.get("title", ""), item.get("content", ""), item.get("description", ""),
        ]))
    if item_type == "guardrail":
        return " ".join(filter(None, [
            item.get("rule", ""), item.get("trigger_condition", ""),
        ]))
    if item_type == "pattern_signature":
        return " ".join(filter(None, [
            item.get("description", ""), item.get("title", ""), item.get("name", ""),
        ]))
    return item.get("summary", "") or ""

# ---------------------------------------------------------------------------
# G3 — Retrieval tier tracking (world/conventions/self-program-evolution.md)
# ---------------------------------------------------------------------------
# Appends one line per retrieve.py invocation to world/retrieval-trace.jsonl.
# tier_satisfied: 1 if Tier 1 (tree-node) retrieval returned non-empty; 0 if
# empty. Tier 2 (codebase grep) and Tier 3 (web search) live at the LLM layer
# and require separate logging — out of scope for this script.
#
# Fail-silent: best-effort observability. Never crashes retrieve.py if the
# JSONL write fails (disk full, permission denied, OneDrive sync lock).
# Same pattern as `_record_fallback_hit` in _fileops.py.

def _log_retrieval_trace(category, depth, read_only, items_returned,
                         effective_goal, supplementary_only,
                         include_framework):
    """Append retrieval telemetry to world/retrieval-trace.jsonl.

    Schema:
      ts                — local ISO 8601
      agent             — AYOAI_AGENT or "unknown"
      goal_id           — args.goal or inferred in-flight, else null
      category          — query category (comma-separated multi)
      depth             — shallow|medium|deep
      tier_satisfied    — 1 if tree_nodes > 0 (or supplementary returned anything
                          in --supplementary-only mode), 0 if empty (caller
                          should escalate to Tier 2/3 LLM-side)
      n_tree_nodes      — count
      n_reasoning_bank  — count
      n_guardrails      — count
      read_only         — bool (read-only retrieval skips rc bumps)
      supplementary_only — bool
      include_framework — bool

    Signal #10 of the Self/Program evolution metric vector (§7.1) reads this
    file to compute "retrieval tier success rate" — higher Tier-1-satisfied
    fraction = more knowledge is encoded into the tree (good).
    """
    try:
        # Decision #58: use the module-global WORLD_DIR (the daemon retrieve
        # endpoint swaps `_r.WORLD_DIR` per request under _swap_lock). Do NOT
        # re-import from _paths here — under the long-lived daemon that
        # captures the STARTUP world, not the requesting agent's, writing the
        # trace to the wrong world. The bare name resolves to the swappable
        # module global, exactly like TREE_PATH/RB_PATH in load_tree_nodes.
        if WORLD_DIR is None:
            return
        trace_path = WORLD_DIR / "retrieval-trace.jsonl"
        # supplementary_only mode: tier satisfied = any supplementary store
        # returned anything (rb, guardrails, patterns, exp). Otherwise: tree_nodes.
        if supplementary_only:
            satisfied = int(
                items_returned.get("reasoning_bank", 0)
                + items_returned.get("guardrails", 0)
                + items_returned.get("pattern_signatures", 0)
                + items_returned.get("experiences", 0) > 0
            )
        else:
            satisfied = 1 if items_returned.get("tree_nodes", 0) > 0 else 0
        from datetime import datetime
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": os.environ.get("AYOAI_AGENT", "unknown"),
            "goal_id": effective_goal or None,
            "category": category,
            "depth": depth,
            "tier_satisfied": satisfied,
            "n_tree_nodes": items_returned.get("tree_nodes", 0),
            "n_reasoning_bank": items_returned.get("reasoning_bank", 0),
            "n_guardrails": items_returned.get("guardrails", 0),
            "n_pattern_signatures": items_returned.get("pattern_signatures", 0),
            "n_experiences": items_returned.get("experiences", 0),
            "read_only": bool(read_only),
            "supplementary_only": bool(supplementary_only),
            "include_framework": bool(include_framework),
        }
        # Same best-effort append pattern as _record_fallback_hit. Single-line
        # JSON under PIPE_BUF (4 KB) is single-write atomic on most filesystems
        # — torn-line risk is observability-grade, not durable-state-grade.
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return

# ---------------------------------------------------------------------------
