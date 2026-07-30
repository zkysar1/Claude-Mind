"""GET /v1/retrieve — full retrieve.py parity (read-only AND counter-bump).

Query parameters:
    category=<cat>            required (comma-separated for multi-category)
    depth=<shallow|medium|deep>   default `deep` (matches CLI default)
    supplementary_only=1      skip tree node matching
    full_content=1            include long-form body fields
    include_framework=1       include framework rules + conventions
    read_only=1               optional — absent ⇒ counter-bump path
    goal=<goal-id>            optional — scopes the retrieval-session manifest
    tree_nodes=<k1,k2>        optional — extra node keys recorded in manifest
    entry_type=<type>         optional — restrict reasoning_bank/meta_lessons to
                              records whose entry_type equals it (e.g. procedure)

Equivalence target: stdout JSON of
    py -3 core/scripts/retrieve.py --category <c> [--depth ...] [--read-only] \\
        [--goal G] [--tree-nodes k1,k2] [--full-content] \\
        [--include-framework] [--supplementary-only]

Counter-bump path (Decision #58 — supersedes #24 and extends #25):
  retrieve.py fans into seven backing stores; the non-read-only path bumps
  retrieval counters on five (tree, rb, guard, sigs, exp) and writes
  retrieval-session.json under AGENT_DIR. Decision #24 deferred daemonising
  this ("a strictly larger PR; keep the CLI byte-identical") and the wrapper
  fell through to `python3 retrieve.py`. The 2026-05-14 daemon-only cutover
  (commit 25d6520) then DELETED retrieve.py's argparse + main() + __main__
  — the very CLI #24 relied on — so every autonomous-mode retrieve (prime
  Phase 3, learning-gate, goal-execution) silently returned nothing from
  2026-05-14 until this fix. Both blockers #24 named are now resolved in
  production: `_fileops` locked writers run in-daemon (aspirations_write /
  pipeline_write), and per-request paths are handled by the #25 path-swap.
  This endpoint therefore serves BOTH paths; there is no direct-python
  fallback and no `read_only_required` 400 anymore.

Path-swap pattern (Decision #25, extended by #58):
  retrieve.py holds ten module-level globals derived from WORLD_DIR /
  AGENT_DIR at import time (TREE_PATH, RB_PATH, GUARD_PATH, SIGS_PATH,
  BELIEFS_PATH, EXP_PATH, EI_PATH, FRAMEWORK_WORLD_CONVENTIONS_DIR, plus
  WORLD_DIR + AGENT_DIR). The endpoint serialises swap + load_* + (for the
  counter-bump path) the session-manifest write + restore under one daemon-
  wide lock so concurrent requests for different agents cannot clobber each
  other's globals. #58 additionally swaps os.environ["MIND_AGENT"] inside
  the same lock: the counter-bump path calls _infer_in_flight_goal_id()
  (reads the env + WORLD_DIR/team-state.yaml) and _fileops save_history /
  append_changelog (_agent_name() = os.environ["MIND_AGENT"]) — without
  the env swap a bravo request would infer alpha's goal and attribute alpha
  in the changelog. retrieve.py's _log_retrieval_trace was also fixed to
  read the swappable module-global WORLD_DIR instead of re-importing _paths.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

# Make core/scripts/ importable so we can call retrieve.load_* directly.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Import the module ONCE at startup. The daemon will swap its path globals
# per request under `_swap_lock` below. Import-time side effects of
# retrieve.py: reconfigures stdout/stderr to utf-8 (harmless — the daemon
# already does this), imports tree_match + _rb_helpers + _fileops (all path-
# stable except for the WORLD_DIR-derived paths we already plan to swap).
import retrieve as _r  # noqa: E402
# Import build_concept_index from tree_match DIRECTLY so a future
# importlib.reload of this module can't capture our patched _r.build_concept_
# index as the "real" function (which would create infinite recursion on the
# first cache miss). tree_match.build_concept_index is never patched.
from tree_match import build_concept_index as _real_build_concept_index  # noqa: E402

from ..yaml_cache import cache as _yaml_cache
from ..jsonl_cache import cache as _jsonl_cache


# Serialises (snapshot, swap, call, restore) so concurrent requests for
# different agents do not race on retrieve's module globals. The whole call
# is held — load_* functions read TREE_PATH / RB_PATH / etc. as module
# globals on every call, so any mid-call swap would corrupt results.
_swap_lock = threading.Lock()


# --- Hot-path caches (installed once at module load) -----------------------
# Without these, retrieve takes ~7-10s per call on OneDrive worlds because:
#   1. read_yaml() reparses _tree.yaml (~270KB, 50-100ms) on every call.
#   2. build_concept_index() reads every node's .md file (~94 files, ~9-47s
#      cold, ~250ms warm) on every call.
# The CLI hides both costs behind Python-startup amortisation (one read per
# subprocess). The daemon can't — without caches, every endpoint call is
# essentially a cold CLI call.
#
# Strategy: patch retrieve's module-level names so its internal callers
# (load_tree_nodes / load_beliefs / load_experiential_index / _detect_
# coverage_gap) automatically pick up the cached versions. The patch is
# process-wide and monotonic (caches strictly add information; original
# behaviour is preserved when caches miss). No per-request undo needed.

# Each entry is (idx, anchor) where `anchor` is a strong reference to the
# nodes dict that produced this idx. Holding the dict strongly is LOAD-BEARING:
# without it, after yaml_cache invalidates a tree.yaml entry, the old dict
# becomes GC-eligible. CPython's small-object pool reuses freed addresses, so
# a new dict can land at the same memory address — at which point id(new_nodes)
# would collide with our cache key and return the STALE idx. Do not remove
# the anchor.
_concept_cache: dict = {}
_concept_cache_lock = threading.Lock()
_CONCEPT_CACHE_MAX = 8  # bounded; ~1 entry per agent


def _cached_build_concept_index(nodes):
    """id(nodes)-keyed wrapper. Misses naturally when yaml_cache reloads
    _tree.yaml (mtime change → new dict identity → cache miss → real rebuild).
    The anchor in the cache entry prevents id reuse from causing stale hits."""
    nodes_id = id(nodes)
    with _concept_cache_lock:
        entry = _concept_cache.get(nodes_id)
    if entry is not None:
        return entry[0]
    # Build outside the lock — file I/O can be slow on cold caches.
    idx = _real_build_concept_index(nodes)
    with _concept_cache_lock:
        _concept_cache[nodes_id] = (idx, nodes)  # anchor pins the dict
        if len(_concept_cache) > _CONCEPT_CACHE_MAX:
            _concept_cache.pop(next(iter(_concept_cache)))
    return idx


def _cached_read_yaml(path):
    """Daemon-cached replacement for retrieve.read_yaml. Routes through
    yaml_cache (mtime+size keyed). Returns {} for missing files to match
    the original read_yaml's contract.

    Mutation contract: yaml_cache returns the SHARED reference. retrieve's
    read_yaml callers (load_tree_nodes, load_beliefs, load_experiential_
    index, _detect_coverage_gap, _load_retrieval_cfg) read from the dict
    but do NOT mutate it — verified by audit before installing this patch.
    """
    data = _yaml_cache().get(path)
    return data if isinstance(data, dict) else {}


def _cached_read_jsonl(path):
    """Daemon-cached replacement for retrieve.read_jsonl. Routes through
    jsonl_cache (mtime+size keyed, ensure_local-backed for own-cloud freshness
    exactly like yaml_cache). Returns [] for missing files to match the
    original read_jsonl's contract.

    Mutation contract (audited g-333-01 before installing this patch):
    jsonl_cache returns the SHARED list+dicts. retrieve's read_jsonl callers
    (load_reasoning_bank :783, load_guardrails :836, load_pattern_signatures
    :876, load_experiences :1071) build NEW lists via comprehensions and route
    every counter bump through _locked_bump_jsonl — a SEPARATE locked file
    read-modify-write that re-reads fresh and discards the in-memory snapshot
    (see load_experiences' explicit comment). None mutate the cached list or
    its dicts in place, so sharing the cache copy is safe.

    Benefit profile (g-333-01): full on the read-only path (prime, reader mode,
    --read-only) where no bump fires; partial on the non-read-only path, because
    _locked_bump_jsonl rewrites a matched file's counters every call, changing
    its mtime and forcing the next LOAD read to reload. The bump-rewrite cost
    is out of this goal's scope (filed as a follow-up Idea).
    """
    data = _jsonl_cache().get(Path(path))
    return data if isinstance(data, list) else []


# Install the patches. From this point on, any code that calls
# `retrieve.build_concept_index(...)`, `retrieve.read_yaml(...)`, or
# `retrieve.read_jsonl(...)` — which includes all of retrieve's internal
# load_* functions (load_tree_nodes, load_reasoning_bank, load_guardrails,
# load_pattern_signatures, load_experiences) — gets the cached versions. The
# direct-python fallback path is unaffected because it imports retrieve fresh
# in its own process.
_r.build_concept_index = _cached_build_concept_index
_r.read_yaml = _cached_read_yaml
_r.read_jsonl = _cached_read_jsonl


def _flag(q, name: str) -> bool:
    v = q.get(name)
    if v is None:
        return False
    return v.lower() not in ("", "0", "false", "no")


def handle(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    category = (q.get("category") or "").strip()
    if not category:
        return Response.error(400, "missing_param",
                              "query parameter 'category' is required")

    depth = (q.get("depth") or "deep").lower()
    if depth not in ("shallow", "medium", "deep"):
        return Response.error(400, "invalid_param",
                              f"depth must be shallow/medium/deep, got {depth!r}")

    # source=agent semantics — retrieve always operates against the bound
    # agent. Require an explicit header (mirrors aspirations.read for source=
    # agent; without it the resolver picks "first available agent" which
    # would silently route to the wrong store). Required on BOTH paths now —
    # the counter-bump path needs it to swap MIND_AGENT env correctly.
    explicit_agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not explicit_agent:
        return Response.error(400, "agent_unset",
                              "X-Mind-Agent header required for /v1/retrieve")

    # read_only is now HONOURED, not required (Decision #58 supersedes #24).
    # Absent ⇒ counter-bump path: runs in-daemon under the same path-swap
    # lock as the read path, with MIND_AGENT env also swapped so
    # _infer_in_flight_goal_id() + _fileops attribution resolve to the
    # REQUESTING agent. The `read_only_required` 400 that used to live here
    # is gone.
    read_only = _flag(q, "read_only")
    goal = (q.get("goal") or "").strip() or None
    tree_nodes_param = (q.get("tree_nodes") or "").strip()
    # : optional reasoning-bank entry_type filter (e.g. "procedure").
    # Forwarded to _r.load_reasoning_bank; None => no filter (default).
    entry_type = (q.get("entry_type") or "").strip() or None

    # : optional bi-temporal point-in-time read. as_of=<ISO-8601> returns
    # the record VERSIONS that were valid at that instant (valid_from<=T<valid_to)
    # across RB/guardrails/patterns/beliefs — status-agnostic, no counter bump.
    # Validated here via the engine's own parser so a malformed value fails loud
    # (400) instead of silently treating every record as valid. None => default
    # current-version view. Forwarded to the load_* loaders below.
    as_of = (q.get("as_of") or "").strip() or None
    if as_of is not None and _r._parse_iso(as_of) is None:
        return Response.error(400, "invalid_param",
                              "as_of must be an ISO-8601 datetime "
                              "(e.g. 2026-06-19T01:00:00), got %r" % as_of)

    supplementary_only = _flag(q, "supplementary_only")
    full_content = _flag(q, "full_content")
    include_framework = _flag(q, "include_framework")

    categories = [c.strip() for c in category.split(",") if c.strip()]
    if not categories:
        return Response.error(400, "missing_param",
                              "category must contain at least one non-empty value")

    world = ctx.paths.world
    agent_dir = ctx.paths.agent

    coverage_gap = None
    with _swap_lock:
        # Snapshot the globals we will swap. CAPTURED INSIDE THE LOCK —
        # concurrency-critical. server.py runs a ThreadingHTTPServer, so if
        # this baseline were captured before acquiring the lock, a concurrent
        # request B could snapshot request A's ALREADY-SWAPPED paths/env
        # (while A holds the lock) as B's "baseline", then restore the module
        # globals to A's values in B's finally — persistently corrupting the
        # daemon's globals for every later request. With the lock held, every
        # prior holder has already restored in its own finally (which runs
        # before the lock is released), so these reads are guaranteed to see
        # the true daemon-startup baseline. Captured BEFORE the `try:` so the
        # `finally` always has `saved` + `_saved_env_agent` bound even if a
        # swap-in line below raises.
        #
        # Listed verbatim so a future reader can see the entire blast radius.
        # If retrieve.py grows another WORLD_DIR-derived module global, add it
        # to BOTH this dict AND the swap-in block below — otherwise the new
        # path is read from the daemon's startup-time value and serves stale
        # data.
        saved = {
            "WORLD_DIR": _r.WORLD_DIR,
            "AGENT_DIR": _r.AGENT_DIR,
            "TREE_PATH": _r.TREE_PATH,
            "RB_PATH": _r.RB_PATH,
            "GUARD_PATH": _r.GUARD_PATH,
            "SIGS_PATH": _r.SIGS_PATH,
            "BELIEFS_PATH": _r.BELIEFS_PATH,
            "EXP_PATH": _r.EXP_PATH,
            "EI_PATH": _r.EI_PATH,
            "FRAMEWORK_WORLD_CONVENTIONS_DIR": _r.FRAMEWORK_WORLD_CONVENTIONS_DIR,
        }
        # Decision #58: also swap MIND_AGENT env. _infer_in_flight_goal_id()
        # reads os.environ["MIND_AGENT"] + WORLD_DIR/team-state.yaml; _fileops
        # _agent_name() (save_history / append_changelog attribution on the
        # counter-bump path) = os.environ.get("MIND_AGENT","system"). The
        # daemon process env holds its STARTUP agent, not the requester's.
        # Saved-as-None means "was unset" → pop on restore (never write "").
        # Same lock-scope rule as `saved` above — capture inside the lock.
        _saved_env_agent = os.environ.get("MIND_AGENT")
        try:
            os.environ["MIND_AGENT"] = explicit_agent
            _r.WORLD_DIR = world
            _r.AGENT_DIR = agent_dir
            _r.TREE_PATH = world / "knowledge" / "tree" / "_tree.yaml"
            _r.RB_PATH = world / "reasoning-bank.jsonl"
            _r.GUARD_PATH = world / "guardrails.jsonl"
            _r.SIGS_PATH = world / "pattern-signatures.jsonl"
            _r.BELIEFS_PATH = world / "knowledge" / "beliefs.yaml"
            _r.EXP_PATH = (agent_dir / "experience.jsonl") if agent_dir else None
            _r.EI_PATH = (agent_dir / "experiential-index.yaml") if agent_dir else None
            _r.FRAMEWORK_WORLD_CONVENTIONS_DIR = world / "conventions"

            # effective_goal computed ONCE (mirrors retrieve.py main()
            #  / ). MUST be inside the swap — it reads the
            # swapped MIND_AGENT env + WORLD_DIR/team-state.yaml.
            effective_goal = goal or _r._infer_in_flight_goal_id()

            #  gate: no goal context (no ?goal and no in-flight goal
            # inferable from team-state) → auto-read-only, so we never bump
            # retrieval_count without a goal to classify it against
            # ("retrieved but never classified" drift). Symmetric with the
            # session-manifest gate below — bumps and manifest skip together.
            if not read_only and not effective_goal:
                read_only = True
                try:
                    _r.record_firing(
                        "retrieve.no_goal_gate",
                        context={"category": category, "depth": depth})
                except Exception:
                    pass

            # Load tree nodes unless supplementary_only. Returns ([], set())
            # in the supplementary-only branch — matches main() parity.
            if supplementary_only:
                tree_nodes, retrieval_channels = [], set()
            else:
                tree_nodes, retrieval_channels = _r.load_tree_nodes(
                    categories, depth, read_only=read_only)

            # Coverage-gap detection (E12) — only when tree returned empty.
            if not supplementary_only and not tree_nodes:
                coverage_gap = _r._detect_coverage_gap(categories)

            # read_only is now the post-gate value. When False, these load_*
            # calls run _locked_bump_jsonl / locked_modify_yaml against the
            # SWAPPED module-global paths (TREE_PATH/RB_PATH/...). Decision
            # #24's "wrong agent's WORLD_DIR baked in" fear is handled by the
            # path swap + the MIND_AGENT env swap above.
            reasoning_bank, meta_lessons = _r.load_reasoning_bank(
                categories, depth, read_only=read_only, entry_type=entry_type,
                as_of=as_of)
            guardrails = _r.load_guardrails(categories, depth,
                                            read_only=read_only, as_of=as_of)
            pattern_signatures = _r.load_pattern_signatures(
                categories, depth, read_only=read_only, as_of=as_of)
            experiences = _r.load_experiences(categories, depth,
                                              read_only=read_only)
            beliefs = _r.load_beliefs(categories, as_of=as_of)
            experiential_index = _r.load_experiential_index(categories)

            framework_rules = (_r.load_framework_rules(categories)
                               if include_framework else [])

            # Timestamp INSIDE the lock so meta + the session manifest are
            # consistent with what was just loaded.
            timestamp = _r.now_str()

            items_returned = {
                "tree_nodes": len(tree_nodes),
                "reasoning_bank": len(reasoning_bank),
                "meta_lessons": len(meta_lessons),
                "guardrails": len(guardrails),
                "pattern_signatures": len(pattern_signatures),
                "experiences": len(experiences),
                "beliefs": len(beliefs),
            }
            if include_framework:
                items_returned["framework_rules"] = len(framework_rules)

            # Counter-bump session manifest (Layer 1 utilization tracking),
            # ported from the deleted retrieve.py main(). ORDER IS LOAD-
            # BEARING: build this BEFORE _strip_long_form (which runs after
            # the lock) — strip nulls content/description in the SAME dicts,
            # halving the utilization-feedback token signal. Gate is
            # symmetric with : effective_goal AND not read_only AND
            # an agent dir to write under.
            if effective_goal and not read_only and agent_dir:
                tree_node_keys = []
                if tree_nodes_param:
                    tree_node_keys = [k.strip() for k in
                                      tree_nodes_param.split(",") if k.strip()]
                for tn in tree_nodes:
                    k = tn.get("key", "")
                    if k and k not in tree_node_keys:
                        tree_node_keys.append(k)

                tree_summary_by_key = {
                    tn.get("key", ""): tn.get("summary", "") or ""
                    for tn in tree_nodes if tn.get("key")}
                tree_nodes_detail = [{
                    "key": k,
                    "summary": tree_summary_by_key.get(k, ""),
                    "distinctive_tokens": _r._distinctive_tokens(
                        tree_summary_by_key.get(k, "")),
                } for k in tree_node_keys]

                def _times_active(rec):
                    u = (rec.get("utilization")
                         if isinstance(rec, dict) else None)
                    if isinstance(u, dict):
                        v = u.get("times_active", 0)
                        if isinstance(v, int) and not isinstance(v, bool):
                            return v
                    return 0

                supp_items = []
                supp_detail = []
                # MEMBERSHIP IS COMPUTED ONCE, BY THE LOADER ().
                # These four lists ARE the loaders' return sets: already filtered
                # by `_entry_matches` (strict category, THEN token-overlap
                # fallback), widened by the embedding blend, and capped. Do NOT
                # re-derive membership here — that is a second predicate free to
                # drift from the one that selected the return set, and it did.
                # The narrower `_entry_matches_category` dropped every entry that
                # arrived via the text fallback, so a free-text query bumped
                # retrieval_count on entries `utilization-feedback --helpful`
                # could never credit: denominator grew, numerator unreachable,
                # utility_ratio drifted to 0, record sank out of ranking and
                # never recovered. load_reasoning_bank's docstring states the
                # violated invariant verbatim ("the bump set MUST equal the
                # return set") and warns off exactly the
                # `is_universal_rb or _entry_matches_category` test that stood
                # here: that decides ELIGIBILITY, and the cap decides RETURN.
                # Note the `is_universal_rb` disjunct was also INERT — the domain
                # list is built with `not is_universal_rb(r)`, so it could never
                # fire, which is why the measured free-text case dropped 100% of
                # reasoning_bank and not merely the non-universal part.
                for item in reasoning_bank:
                    iid = item.get("id", "")
                    if not iid:
                        continue
                    supp_items.append({"id": iid, "type": "reasoning_bank"})
                    text = _r._item_text_for_tokens(item, "reasoning_bank")
                    supp_detail.append({
                        "id": iid, "type": "reasoning_bank",
                        "summary": text[:300],
                        "distinctive_tokens": _r._distinctive_tokens(text),
                        "times_active_at_retrieve": _times_active(item),
                    })
                for item in meta_lessons:
                    iid = item.get("id", "")
                    if not iid:
                        continue
                    # meta_lessons IS the universal partition — unconditional.
                    supp_items.append({"id": iid, "type": "meta_lesson"})
                    text = _r._item_text_for_tokens(item, "reasoning_bank")
                    supp_detail.append({
                        "id": iid, "type": "meta_lesson",
                        "summary": text[:300],
                        "distinctive_tokens": _r._distinctive_tokens(text),
                        "times_active_at_retrieve": _times_active(item),
                    })
                for item in guardrails:
                    iid = item.get("id", "")
                    if not iid:
                        continue
                    # No re-derivation — see the membership note above.
                    supp_items.append({"id": iid, "type": "guardrail"})
                    text = _r._item_text_for_tokens(item, "guardrail")
                    supp_detail.append({
                        "id": iid, "type": "guardrail",
                        "summary": text[:300],
                        "distinctive_tokens": _r._distinctive_tokens(text),
                        "times_active_at_retrieve": _times_active(item),
                    })
                for item in pattern_signatures:
                    iid = item.get("id", "")
                    if not iid:
                        continue
                    # No re-derivation — see the membership note above.
                    supp_items.append({"id": iid,
                                       "type": "pattern_signature"})
                    text = _r._item_text_for_tokens(item,
                                                    "pattern_signature")
                    supp_detail.append({
                        "id": iid, "type": "pattern_signature",
                        "summary": text[:300],
                        "distinctive_tokens": _r._distinctive_tokens(text),
                    })

                session_record = {
                    # v3 (): distinctive_tokens now keep identifier
                    # shape and rank structural tokens ahead of prose. v2 tokens
                    # were split on -/_ and prose-dominated; infer_feedback
                    # REFUSES v2 rather than classifying it, because v2's output
                    # was measured at 0.922 helpful/population with an unrelated
                    # cake recipe scoring 0.627 (). Bump this whenever
                    # the token contract changes — the consumer gates on it.
                    "schema_version": 3,
                    "goal_id": effective_goal,
                    "timestamp": timestamp,
                    "categories": categories,
                    "tree_nodes_loaded": tree_node_keys,
                    "supplementary_items": supp_items,
                    "tree_nodes_detail": tree_nodes_detail,
                    "supplementary_detail": supp_detail,
                    "counts": {
                        "tree_nodes": len(tree_node_keys),
                        "reasoning_bank": len(reasoning_bank),
                        "meta_lessons": len(meta_lessons),
                        "guardrails": len(guardrails),
                        "pattern_signatures": len(pattern_signatures),
                        "experiences": len(experiences),
                    },
                    "utilization_pending": True,
                    "utilization_completed_at": None,
                }
                try:
                    from _fileops import locked_write_json
                    # Phase 1D (): route the utilization manifest per-Body
                    # by the request SID (the unitKey), reducer-aware via the same
                    # forked-body-WM-file signal wm_path uses. A reducer/observer
                    # (no forked WM file) -> agent-wide session/retrieval-session.json,
                    # identical to pre-1D behavior; a forked worker Body -> its own
                    # sessions/<sid>/body-retrieval-session.json so concurrent
                    # Bodies don't clobber each other's utilization audit trail.
                    sid = (ctx.headers.get("x-mind-sid") or "").strip()
                    locked_write_json(
                        ctx.paths.retrieval_session_path(sid or None),
                        session_record)
                except Exception:
                    # Best-effort: a manifest write failure must not fail the
                    # retrieve — the data the caller asked for is ready. The
                    # CLI treated this write as fatal-on-error; the daemon
                    # serves many agents and must not 500 a good retrieval
                    # over a utilization-telemetry write.
                    pass

            # G3 telemetry — one line per invocation, fail-silent inside.
            # Inside the lock because (post retrieve.py #58 fix) it reads the
            # swapped module-global WORLD_DIR. retrieve.py main() called this
            # unconditionally on BOTH paths; the read-only daemon path
            # previously omitted it (a PR5 parity gap) — restored here.
            _r._log_retrieval_trace(
                category=category,
                depth=depth,
                read_only=read_only,
                items_returned=items_returned,
                effective_goal=effective_goal,
                supplementary_only=supplementary_only,
                include_framework=include_framework,
            )
        finally:
            # CRITICAL: restore on EVERY exit path, including exceptions.
            # Without this, alpha's paths/env leak into bravo's next call
            # (the daemon is long-lived; module globals + os.environ persist
            # across requests). Path globals first, then env — env saved-as-
            # None means "was unset" → pop, never set "".
            for k, v in saved.items():
                setattr(_r, k, v)
            if _saved_env_agent is None:
                os.environ.pop("MIND_AGENT", None)
            else:
                os.environ["MIND_AGENT"] = _saved_env_agent

    meta = {
        "category": category,
        "depth": depth,
        "read_only": read_only,
        "full_content": full_content,
        "timestamp": timestamp,
        "retrieval_channels": sorted(retrieval_channels),
        "items_returned": items_returned,
    }
    if coverage_gap:
        meta["empty_with_populated_siblings"] = coverage_gap

    result = {
        "meta": meta,
        "tree_nodes": tree_nodes,
        "reasoning_bank": reasoning_bank,
        "meta_lessons": meta_lessons,
        "guardrails": guardrails,
        "pattern_signatures": pattern_signatures,
        "experiences": experiences,
        "beliefs": beliefs,
        "experiential_index": experiential_index,
    }

    # Framework asymmetry: meta-marker AND list both included only when the
    # flag is on. Callers that don't opt in see no `framework_rules` key
    # and no `include_framework` meta marker. Mirrors retrieve.py main().
    if include_framework:
        meta["include_framework"] = True
        result["framework_rules"] = framework_rules

    # Strip long-form last. The session-manifest block above already ran
    # (inside the lock) on the un-stripped records — preserving the
    # utilization-feedback token signal exactly as retrieve.py main() did.
    if not full_content:
        _r._strip_long_form(result)

    # ensure_ascii=True + indent=2 mirrors retrieve.py (rb-597 hardening).
    # Output equivalence with the pre-cutover CLI is the acceptance test;
    # do not silently switch to ensure_ascii=False here.
    return Response.text(
        json.dumps(result, ensure_ascii=True, indent=2),
        content_type="application/json",
    )


def register(routes) -> None:
    routes[("GET", "/v1/retrieve")] = handle
