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
# s4 (lodestar own-cloud): route store-file reads through the active backend so
# own-cloud materializes the current S3 object into the local cache before the
# raw read. On the default LocalBackend, ensure_local() is identity and refresh()
# is a no-op (zero added I/O) — the local read path is byte-for-byte unchanged.
from storage_backend import get_backend
# g-358-05: the reader seam for the segmented content stores. Top-level import is
# safe HERE (unlike in mind_api/src/world/reasoning_bank.py, where it had to be
# lazy) because this module already imports `_paths` at :80, so the WORLD_DIR
# resolution it triggers is already paid. `_store_paths` below never lets its
# module-level WORLD_DIR reach a read — see that docstring.
from _utilization_store import store_paths as _seg_store_paths
from _utilization_store import dedup_by_id as _dedup_by_id
# g-358-05 reader seam for the reasoning-bank/guardrails counter split. Reader-only
# and a no-op until the writer lands (see _utilization_store's module docstring).
# PER-STORE (`load_counters`), not merged: every _sort_by_utility call site is
# single-kind. A concurrent implementation imported `load_all_counters` here for a
# merged read; both landed in one merge, so this import block briefly bound
# `utilization_of` twice. One binding, one seam.
from _utilization_store import (load_counters as _load_counters,
                                utilization_of as _utilization_of)

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
#
# EXP_PATH is LIVE-ONLY, and that is an UNINTENDED reachability gap — NOT
# deliberate active forgetting (g-115-4617, measured 2026-08-04, echo, hostname
# cc-03, uname -r 6.8.0-136-generic). 1,722 records sit in experience-archive.jsonl
# that retrieve cannot surface. Recorded here so the next reader does not
# re-derive it; the evidence, in the order it settles the question:
#   - The sweep that fills the archive (experience.py cmd_archive_sweep) applies
#     (1) age>=30d AND retrieval_count==0, and (2) age>=90d AND utility_ratio<0.2,
#     with a "never archive high-value" guard requiring rc>=5 AND ur>=0.5.
#   - utility_ratio is 0.0 on 4,174 of 4,175 fleet records (and on 604 of 604 of
#     echo's own, which are ground truth rather than a mirror read) because its
#     recompute is UNREACHABLE DEAD CODE: experience.py cmd_update_field rejects
#     any dotted field name at L665, and the recompute at L687 fires only on a
#     dotted field name. So rule (2) degenerates to a PURE 90-DAY AGE CAP and the
#     protection guard is structurally unreachable — 0 of 4,175 records qualify,
#     and 174 archived records had rc>=5 (max 34). Tracked by g-115-4969; the
#     class is guard-893's under-recorded-utilization trap.
#   - Its stated purpose is performance, not curation: g-001-06, the recurring goal
#     that drives it, reads "Keeps live JSONL files small and fast."
#   - No consumer compensates — experiential-index.yaml holds 8 entries fleet-wide.
#   - The archive is a ONE-WAY DOOR: retrieval_count is bumped only on live
#     records, so rule (1)'s own criterion is self-fulfilling once a record lands
#     there (guard-731: never retire on retrieval_count==0 alone).
# DO NOT widen scope here alone. DO NOT INLINE — the twin at
# mind_api/src/endpoints/retrieve.py re-binds _r.EXP_PATH to the same live file
# per request, so a one-sided change fixes nothing (guard-130).
#
# COST NOW MEASURED, and BOTH halves of the prior "not free" claim were wrong
# (g-115-4970, 2026-08-12, zeta, hostname cc-02, uname -r 6.8.0-137-generic;
# counts are a mutable-data snapshot — re-measure before relying on them,
# guard-1876):
#   - SCAN: EXP_PATH is per-AGENT, so the fleet-wide "+70%" is the wrong
#     denominator. zeta = 726 live / 283 archive: +39% records, +24% bytes,
#     +3.7ms to parse the archive. The +3.7ms is a STANDALONE json.loads loop
#     over the file, NOT an in-situ measurement of this module's read path —
#     so treat it as an order-of-magnitude figure, not a regression baseline.
#     Against a real warm retrieve.sh call (657ms medium / 741ms deep, same
#     box, measured end-to-end) that is ~0.5%; even off by 10x it is ~5%.
#     Too small to justify a conditional either way.
#   - WRITE AMPLIFICATION: structurally absent, not merely small. The bump is
#     _locked_bump_jsonl(EXP_PATH, ...) — a HARDCODED path that re-reads that
#     one file whole under lock and no-ops on ids absent from it. Merging
#     archive records into `matching` CANNOT write to the archive. Verified by
#     reading the call site (load_experiences) and the helper (L241).
#   - REACH: the sort is stable and archive retrieval_count is 0 on 273/283, so
#     archive records land BELOW live ones automatically — tail-fill only, never
#     displacing a proven record. 67 of 139 categories change at deep; 45 return
#     ZERO today (61 records, incl. product categories lodestar-mycelium-gateway
#     and pearl-plan). 6 saturated categories hold 68% of live records and are
#     provably inert — the scan is paid there and buys nothing.
# DECISION: widen READ-ONLY, unconditionally (shape b). Rejected: (a) replace
# live with live+archive — displaces proven records; (c) bump on archive hit —
# re-arms the one-way door guard-731 warns about, and tail-fill already reaches
# them; (d) un-archive on retrieval — mutates two stores on a READ path, and
# rc==0 mostly means "never queried", not "proved unuseful". A lazy load (parse
# the archive only when live < limit) is a single-use branch guarding 0.5%.
# TO IMPLEMENT: derive the archive path from EXP_PATH at CALL time inside
# load_experiences — do NOT add a second module-level constant. The twin
# re-binds _r.EXP_PATH per request and calls this module's function, so a
# derived path follows the re-bind for free and cannot drift (guard-130);
# a second constant would need its own re-bind. Both comments move together
# (guard-2323). Tracked by g-115-6084.
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

def _goal_id_is_terminal(goal_id):
    """True only when the local store POSITIVELY shows goal_id in a terminal state.

    g-115-5887. Deliberately reads world/aspirations.jsonl directly, WITHOUT
    get_backend().ensure_local(): this runs on every --goal-less retrieve, and a
    per-call S3 materialize of a ~35MB store would put a network round-trip on
    the retrieval hot path. Measured 2026-08-13 (alpha, hostname cc-04, uname -r
    6.8.0-137-generic, own-cloud): 34,966,142 bytes / 33 aspirations / 7,105
    goals; 51ms substring-prefiltered scan against a 0.53s warm retrieve (4.21s
    cold) — ~10% warm, ~1% cold, so no staleness/time heuristic is needed to
    afford it.

    The staleness that read accepts fails in the SAFE direction, which is the
    whole reason it is allowed. A stale local copy can only UNDER-report
    terminality (an older snapshot is strictly less likely to show a goal
    completed), so the worst case degrades to the exact pre-fix behaviour —
    return the gid — and never rejects a live goal. That asymmetry is why
    guard-980 ("read the store of record, not the local cache") does not bind
    here: guard-980 governs concluding ABSENCE, where a cold cache manufactures
    a false negative with real consequences. Every error path returns False for
    the same reason: a read failure must never suppress inference.

    Evicted goals are handled too. aspirations-evict-completed.py removes aged
    terminal goals from the live `goals` list, so a long-stale in_flight naming
    an evicted goal would otherwise read as "not found" -> not terminal — which
    is precisely the aged case this check exists for. The archived_census
    carries their ids, so they are consulted as well.
    """
    if not goal_id or WORLD_DIR is None:
        return False
    try:
        from _goal_census import TERMINAL_STATUSES, census_evicted_ids
        path = WORLD_DIR / "aspirations.jsonl"
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                # Cheap prefilter: the id must appear textually in the
                # aspiration's line for the goal (or its census tombstone) to
                # live there. A line may mention the id only as a REFERENCE in
                # some other goal's prose, so a match here is not a hit —
                # never return from this branch, fall through to the next line.
                if goal_id not in line:
                    continue
                asp = json.loads(line)
                for goal in (asp.get("goals") or []):
                    if goal.get("id") == goal_id:
                        return goal.get("status") in TERMINAL_STATUSES
                for status, ids in census_evicted_ids(asp).items():
                    if goal_id in ids:
                        return status in TERMINAL_STATUSES
    except Exception:
        return False
    return False


def _body_role():
    """`worker` | `reducer` | `unknown` — which Body this retrieval is FOR.

    DERIVED INLINE rather than imported from `worker_retrospective.body_role`,
    which is a documented convention and not laziness: that function's own
    docstring records the same predicate as "derived independently at
    journal-append.sh, stop-hook.sh, post-recovery-edit-gate.py,
    worker_reducer_liveness and reducer_self_fence — DELIBERATELY NOT SHARED, so
    no module can quietly change another's meaning of 'which Body is this'".
    This is the sixth site. Two further reasons specific to HERE:

      * CONTEXT (guard-2485). `body_role()` resolves the agent dir through
        `agent_dir(agent)`. Inside the daemon that is WRONG: the endpoint swaps
        `_r.AGENT_DIR` per request under its lock, so the requesting agent's dir
        is the module global — not whatever `agent_dir()` derives from process
        state. Reading AGENT_DIR is the only form that is correct on both the
        CLI and the daemon path.
      * SIGNATURE. `body_role()` defaults its sid from `os.environ["MIND_SID"]`
        and the daemon process env holds the DAEMON's sid, not the caller's.
        The endpoint now swaps MIND_SID from the `x-ayoai-sid` header exactly
        as Decision #58 already swaps MIND_AGENT for this very function; that
        swap is what makes the env read correct here.

    Three-way ON PURPOSE (guard-2913 — an unevaluated check is not a passed
    one). The caller folds `unknown` into the reducer branch, but it does so
    explicitly at the call site rather than here, so the fold stays visible.
    """
    sid = os.environ.get("MIND_SID", "")
    if not sid or AGENT_DIR is None:
        return "unknown"
    try:
        if (AGENT_DIR / "sessions" / sid / "working-memory.yaml").exists():
            return "worker"
    except OSError:
        return "unknown"
    return "reducer"


def _infer_in_flight_goal_id():
    """Infer the agent's current in-flight goal_id from team-state.yaml.

    Returns None if no MIND_AGENT binding, team-state missing, or no in_flight
    entry. Called when --goal is absent so utilization-feedback still fires —
    skills pass only --category (not --goal), which before this inference left
    retrieval-session.json unwritten and distill candidates with times_helpful=0
    AND times_noise=0. See g-115-137.

    Also returns None when the named goal is already TERMINAL (g-115-5887). The
    row is stamped inside aspirations-claim.sh, which ONLY world-source goals
    invoke (guard-2835), so a run of agent-queue goals leaves it naming a
    finished goal for an unbounded period — measured at 80 minutes stale, which
    then mis-stamped a manifest and made utilization-feedback refuse with
    goal_mismatch. This is a READ-SIDE validation: it never writes or clears the
    row, so the evidence g-306-233 still needs stays intact (guard-2835 rule 4
    and guard-2260 forbid remedying a stale row with a background sweep).
    """
    agent = os.environ.get("MIND_AGENT")
    if not agent or WORLD_DIR is None:
        return None
    ts_path = WORLD_DIR / "team-state.yaml"
    # s4: materialize via the backend so own-cloud reads the current S3 object,
    # not a stale local cache. team-state WRITES already route through the
    # backend (_fileops.locked_modify_yaml), so this read must match. Identity
    # on LocalBackend; best-effort — a genuinely missing file still returns None.
    ts_path = Path(get_backend().ensure_local(ts_path))
    # g-328-27 sharding: the agent's live status is its ROW file
    # (world/team-state/agents/<agent>.yaml); the core file only carries a
    # pre-migration residual. Materialize the row via the backend too, then
    # read row-first with core fallback (newest-wins).
    from _team_state import read_agent_row, row_path as _ts_row_path
    try:
        get_backend().ensure_local(_ts_row_path(WORLD_DIR, agent))
    except Exception as e:
        # best-effort — a missing row falls back to the core residual
        try:  # report, never raise — see note_swallowed_backend_error (g-306-218)
            from storage_backend import note_swallowed_backend_error
            # Recomputed rather than hoisted: _ts_row_path() is currently INSIDE
            # the guarded block, so lifting it out would let it raise. The nested
            # try covers the recompute.
            note_swallowed_backend_error(
                "ensure_local", _ts_row_path(WORLD_DIR, agent), e)
        except Exception:
            pass
    status = read_agent_row(WORLD_DIR, agent, core_path=ts_path) or {}
    inflight = status.get("in_flight")
    if not inflight or not isinstance(inflight, dict):
        return None
    gid = inflight.get("goal_id")
    if not (isinstance(gid, str) and gid):
        return None
    # g-115-5887: a non-empty-string check ALONE was the defect. Falling through
    # to None here routes the caller to the existing no-goal path (manifest not
    # goal-stamped) rather than to a WRONG goal, which is the strictly safer of
    # the two failure modes: an unstamped manifest loses attribution, a
    # mis-stamped one makes consumers act on another goal's identity.
    if _goal_id_is_terminal(gid):
        return None
    # g-115-6748: the in_flight row is AGENT-keyed with NO sid — worker-loop
    # Phase 4a states the sharing outright ("a worker and its reducer share one
    # row") — so on a WORKER Body this row names the REDUCER's goal, running on
    # another box. Measured 2026-08-19 (alpha worker, cc-07, SID d1aec55b): a
    # worker executing g-115-6653 stamped its manifest with g-363-20, the
    # reducer's goal on cc-04. That is MISATTRIBUTION, strictly worse than the
    # missing attribution the g-115-5887 note above prefers, and it was harmless
    # only while the consumers were blind — fixing their path is what arms it.
    #
    # Gating on a sid comparison CANNOT work and is the tempting wrong fix: the
    # row carries {goal_id, title, claimed_at, phase} and no sid at all, so any
    # `claimed_by_sid == MIND_SID` test compares against a field that does not
    # exist and silently never fires. On a worker the inference is unsound in
    # PRINCIPLE, not merely stale, so the fix is not to infer.
    #
    # Returning None routes the caller to the no-goal path — the same fail-safe
    # the terminal-goal check above already chose. Downstream that also trips
    # the g-304-01 auto-read-only gate, so a --goal-less retrieval on a worker
    # stops bumping utilization counters as well as not stamping the manifest.
    # That is the intended trade and worth stating: a lost count beats a count
    # attributed to another Body's goal.
    #
    # FAIL OPEN. Only "worker" suppresses. "unknown" (no MIND_SID, no AGENT_DIR,
    # OSError) falls through WITH "reducer" to the current behaviour, because
    # unknown fires whenever MIND_SID is unset — folding it into the worker
    # branch would disable goal-stamping fleet-wide and regress g-115-137, the
    # fix this inference exists to serve.
    if _body_role() == "worker":
        return None
    return gid

# ---------------------------------------------------------------------------
# Helpers: file I/O (same patterns as experience.py, pipeline.py)
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read JSONL file, return list of dicts. Returns [] if missing/empty."""
    # s4: materialize from the active backend (own-cloud: pull the current S3
    # object into the local cache; LocalBackend: identity, no I/O) before the
    # raw read, so own-cloud never reads a missing/stale local cache.
    p = Path(get_backend().ensure_local(path))
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items

def _store_paths(path, kind):
    """Ordered content-store paths for a legacy store file, oldest-first (g-358-05).

    `path` is the LEGACY store path (RB_PATH / GUARD_PATH) and the enumeration
    base is derived from ITS parent — never from the module-level WORLD_DIR.
    That is the load-bearing detail on the daemon path: the retrieve endpoint
    monkeypatches `_r.RB_PATH` / `_r.GUARD_PATH` to the per-request world, so a
    WORLD_DIR-derived base would silently read the DAEMON's world on every
    request instead of the caller's. `_seg_store_paths` takes `world_dir`
    explicitly for exactly this reason.

    The legacy path is pinned unconditionally rather than left to the seam's
    enumeration. `store_paths` includes it only when it can SEE it — locally or
    in the backend listing — and on a cold own-cloud box neither is guaranteed;
    it also returns [] outright when the base is unresolvable. Either miss would
    read as an EMPTY store, which for guardrails means "no guardrails apply" and
    for the reasoning bank means "no prior reasoning exists". That is the worst
    available failure direction (it reads as a clean all-clear), and it is the
    one direction this pin makes impossible. Duplication is impossible in the
    other direction too: `store_paths` yields the legacy name at most once and
    only ever prepends it, so the membership test below is exact.
    """
    try:
        paths = list(_seg_store_paths(kind, path.parent))
    except Exception:
        # Fail toward the legacy read, never toward an empty store — see above.
        return [path]
    if path not in paths:
        paths.insert(0, path)
    return paths

def _read_store(path, kind):
    """Read a content store as its ordered segment set (g-358-05), with ids
    appearing in more than one segment collapsed NEWEST-WINS.

    Byte-identical to `read_jsonl(path)` until a writer emits segments, since
    `_store_paths` resolves to the legacy file alone today — the dedup is inert
    while no id can repeat.

    THE DEDUP IS HERE AND IN THE DAEMON'S `reasoning_bank._load` BOTH, through
    one shared helper rather than two implementations: these are the CLI and
    daemon halves of the same read, and a stale-wins fix applied to only one of
    them leaves half the fleet reading a retired guardrail as active.

    `read_jsonl` is called as a MODULE GLOBAL on purpose: the daemon retrieve
    endpoint patches `_r.read_jsonl` with a jsonl_cache-backed version, and a
    reference captured at def time would not see the patch. Both shapes call
    `ensure_local` first, so the seam's read-through-the-backend contract holds
    on the CLI and daemon paths alike.
    """
    items = []
    for p in _store_paths(path, kind):
        items.extend(read_jsonl(p))
    return _dedup_by_id(items)

def read_yaml(path):
    """Read YAML file, return dict. Returns {} if missing/empty."""
    # s4: materialize via the backend before the raw read (see read_jsonl).
    # Identity on LocalBackend. NB: the daemon retrieve endpoint patches
    # `_r.read_yaml` to a yaml_cache-backed version, so on the daemon path this
    # body runs only as the fallback; own-cloud freshness for the cached daemon
    # path is wired when yaml_cache becomes backend-aware (s5).
    p = Path(get_backend().ensure_local(path))
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}

def _locked_bump_jsonl(path, should_bump_fn, counter_path=("utilization", "retrieval_count"),
                      timestamp_path=("utilization", "last_retrieved"), kind=None):
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
        kind: Optional sidecar spool kind ("reasoning-bank" / "guardrails").
            When set AND UTILIZATION_COUNTERS_SPOOLED is on, the bump is
            spool-routed (see below) instead of rewriting the store. None
            (pattern-signatures, experience) always takes the legacy RMW.

    Returns the (possibly bumped) records list. Returns the original (un-bumped)
    snapshot when the file does not exist — callers should handle empty.
    """
    from _fileops import (acquire_lock, release_lock, save_history,
                          append_changelog, resolve_base_dir, _agent_name,
                          _validate_no_surrogates, _atomic_write_with_fallback,
                          _rmw_with_conflict_retry)
    p = Path(path)
    # s4: materialize from the backend before the pre-lock existence check so
    # own-cloud does not skip the bump for a file that exists in S3 but is not
    # yet in the local cache. Self-contained (does not rely on a caller having
    # read_jsonl'd first). Identity on LocalBackend.
    get_backend().ensure_local(p)
    if not p.exists():
        return []

    # g-358-22: spool-route the retrieval bump for sidecar-covered kinds. The
    # legacy path below is a per-call full-store RMW — history snapshot + fenced
    # whole-object PUT + changelog row — repeated once per store per retrieval
    # call, which survived the g-358-05 flip as its dominant residual churn
    # (measured 2026-08-20: an 83-PUT burst of ~3.7MB objects in 10 minutes
    # where the only diff was utilization.retrieval_count/last_retrieved).
    # Same flag, spool, and fallback idiom as the store endpoint's increment
    # branch (mind_api/src/endpoints/store.py). The read here is LOCAL-only
    # (no refresh, no lock — nothing races an O_APPEND delta), and world_dir
    # is derived from the store path itself because the daemon swaps RB_PATH/
    # GUARD_PATH per request — ambient env may name a different agent's world.
    # last_retrieved is NOT written here: the spool line's `ts` carries it,
    # and utilization-flush.py stamps the sidecar (sidecar wins wholesale in
    # utilization_of, so a store-side stamp would be invisible anyway).
    if kind is not None:
        try:
            import _utilization_store as _us
            if _us.spooled_enabled() and kind in _us.KINDS:
                records = []
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped:
                            records.append(json.loads(stripped))
                failed_ids = set()
                for rec in records:
                    if should_bump_fn(rec) and not _us.record_increment(
                            kind, rec.get("id"), counter_path[-1],
                            world_dir=p.parent):
                        failed_ids.add(rec.get("id"))
                if not failed_ids:
                    return records
                # record_increment returns False rather than raising, so the
                # rare failed appends fall through to the legacy RMW below —
                # narrowed to ONLY the failed ids, or the spooled majority
                # would double-count. Never let the cheap path lose a counter.
                _spooled_ok_fn = should_bump_fn
                should_bump_fn = (lambda rec: rec.get("id") in failed_ids
                                  and _spooled_ok_fn(rec))
        except Exception:
            pass    # any import/flag fault -> full legacy path, bump preserved
    base_dir = resolve_base_dir(p)
    lock_path = p.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # Stash of the most recent in-cycle read, for the degraded return path
        # below (g-115-2301): index 0 holds the last records list _cycle read.
        last_read = [[]]

        def _cycle():
            # s4: force-fresh the local cache from the backend AFTER acquiring
            # the lock and BEFORE the read — own-cloud lost-update prevention
            # (fix #2) and records the If-Match fence etag for the atomic_write
            # below. No-op on LocalBackend. Mirrors _fileops.locked_modify_jsonl.
            # Re-runs on every conflict retry so each attempt re-fences on the
            # latest remote state (g-115-2301).
            get_backend().refresh(p)
            # Read inside the lock — captures the post-writer state, not
            # whatever was on disk before another agent's locked append landed.
            records = []
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        records.append(json.loads(stripped))
            last_read[0] = records

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

        # g-115-2301: the bump was a SINGLE-SHOT fenced write — on own-cloud,
        # hot shared stores (world/reasoning-bank.jsonl etc., written by every
        # agent's spark/increment paths) advance the fence between refresh and
        # PUT often enough that whole retrievals 409'd on a telemetry write
        # (observed cc-05 2026-07-16: 4 conflicts across 2 windows; sibling
        # writers via locked_rmw/locked_modify_yaml already retry). Wrap the
        # cycle in the same bounded retry, and on exhaustion DEGRADE instead of
        # raising: counter telemetry is subordinate to retrieval availability —
        # a persistent per-object fence wedge (rb-2639/rb-3080 class) must not
        # make retrieval unavailable. Returns the last-read records (bumps not
        # persisted). conflict_error is () on LocalBackend, so this except arm
        # is unreachable there (empty-tuple except catches nothing).
        try:
            return _rmw_with_conflict_retry(p, _cycle)
        except get_backend().conflict_error:
            print(f"[retrieve] counter-bump on {p.name} dropped after "
                  f"conflict retries exhausted — returning records un-bumped "
                  f"(telemetry lost for this call only; g-115-2301)",
                  file=sys.stderr)
            return last_read[0]
    finally:
        release_lock(lock_path)

def today_str():
    return date.today().isoformat()

def now_str():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

# ---------------------------------------------------------------------------
# Bi-temporal reader (g-306-36, BRD Gap 5 — consumes the g-306-35 writer fields)
#
# The writer path (g-306-35) stamps valid_from / valid_to on RB, guardrails,
# beliefs, and tree records. Falsification is close-old (set valid_to=now) +
# insert-new (valid_from=now), so a logically-evolving record accumulates a
# version history of half-open [valid_from, valid_to) intervals. This reader
# answers "what was the version valid at instant T?" — the point-in-time query
# rb-335 mandates (without it the writer fields are dead weight).
#
# Lower-bound precedence: valid_from is the canonical bi-temporal field, but
# records that predate g-306-35 carry no valid_from. `created` (RB/guardrails)
# and `last_observed` (beliefs) are transaction-time proxies that give every
# legacy record a real temporal floor — without the fallback, a legacy record
# would read as "-inf lower bound" and wrongly surface in an as-of query for a
# time BEFORE it was even written.
# ---------------------------------------------------------------------------

_VALID_LOWER_FIELDS = ("valid_from", "created", "last_observed")


def _parse_iso(value):
    """Parse an ISO-8601 datetime string; return None on any non-string or
    unparseable value (callers treat None as 'unbounded on this edge')."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _valid_at(record, as_of_dt):
    """Bi-temporal validity predicate (g-306-36): is `record` the version that
    was valid at instant `as_of_dt`? Half-open interval [lower, upper):

      lower = first parseable of valid_from / created / last_observed
              (None => -inf: record has no temporal floor, always-valid lower)
      upper = valid_to  (None => +inf: this IS the current, still-open version)

    Returns True iff lower <= as_of_dt < upper. The half-open upper bound makes
    a close-old/insert-new pair non-overlapping at the cut instant: the closed
    version (valid_to=T) is valid up to but NOT including T; the new version
    (valid_from=T) is valid from T onward — exactly one is valid at any instant.
    """
    lower = None
    for field in _VALID_LOWER_FIELDS:
        lower = _parse_iso(record.get(field))
        if lower is not None:
            break
    if lower is not None and as_of_dt < lower:
        return False
    upper = _parse_iso(record.get("valid_to"))
    if upper is not None and as_of_dt >= upper:
        return False
    return True


def _as_of_dt_or_raise(as_of):
    """Parse an as_of CLI/endpoint argument to a datetime, raising ValueError on
    a malformed value so the caller surfaces a clear error rather than silently
    treating every record as valid. None passes through (the default,
    current-version path)."""
    if as_of is None:
        return None
    dt = _parse_iso(as_of)
    if dt is None:
        raise ValueError(
            f"Invalid as_of: {as_of!r} (expected ISO-8601 datetime, "
            "e.g. 2026-06-19T01:00:00)")
    return dt

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

    # g-306-83: flag-gated tree embedding channel — semantic eligibility.
    # Nodes whose query-cosine clears the tree-lane floor join the matched
    # set on the 'embedding' channel even when all four token strategies
    # missed them. Runs BEFORE sibling/parent inclusion so semantic matches
    # are first-class (they expand at deep like any other match). The same
    # score map feeds _score_weight_limit below, replacing the TF-IDF
    # cosine bonus with true embedding cosine for this request.
    tree_emb = _tree_embedding_scores(categories, nodes)
    if tree_emb:
        # g-306-92: tree-lane-specific floor. embedding_min_cosine is SHARED
        # with the supplementary rb/guardrail lane and was tuned on that lane's
        # evidence, never on tree-lane data; lowering it would silently change
        # reasoning-bank behaviour. embedding_tree_min_cosine overrides it for
        # THIS lane only and falls back to the shared value when unset, so
        # deleting the config key restores the prior behaviour exactly.
        _cfg = _load_retrieval_config()
        try:
            _min_cos = float(_cfg.get(
                "embedding_tree_min_cosine",
                _cfg.get("embedding_min_cosine", 0.35)))
        except (TypeError, ValueError):
            _min_cos = 0.35
        for _k, _score in tree_emb.items():
            if _score >= _min_cos and _k not in all_matched and _k in nodes:
                all_matched[_k] = nodes[_k]
                all_channels[_k] = "embedding"
                all_matched_keys.add(_k)

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
                                 query_text=query_text, all_nodes=nodes,
                                 emb_scores=tree_emb)

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

def _embedding_blend(matched, active, categories, exclude=None):
    """g-306-77 part b2 — flag-gated embedding-cosine hybrid for the
    supplementary stores (reasoning bank + guardrails; the two corpora
    embedding-index-build.py persists).

    Fixes the two weaknesses the g-306-77 A/B exposed (delta msg-2771,
    hit@3 67% vs 13%, MRR .512 vs .129): the token predicate is BINARY
    (a semantically-relevant entry sharing <2 long tokens never becomes
    eligible), and the final order is utility, not relevance (a relevant
    entry loses its cap slot to high-utility off-topic entries).

    Two moves, both flag-gated by `embedding_blend_enabled` (DEFAULT OFF —
    byte-identical ranking when off, same contract as the poignancy/PPR
    blends):
      1. WIDEN — active entries whose cosine >= embedding_min_cosine join
         the candidate set even when `_entry_matches` said no.
      2. RE-RANK — candidates sort by cosine desc. Entries absent from the
         index (added since the last `embedding-index-build.py --update`)
         sort AT the threshold, keeping their pre-existing utility order
         among themselves (stable sort; `matched` arrives utility-sorted).
         They earned eligibility via tokens — never buried below every
         indexed record, never boosted above real semantic hits.

    The caller applies the depth cap AFTER this returns, so semantic adds
    compete for cap slots by relevance — and the bump-set == return-set
    invariant (see load_reasoning_bank docstring) is untouched because the
    bump code reads the post-cap list exactly as before.

    Graceful degradation is structural: flag off, empty/missing index,
    model unavailable, or ANY exception from the helper → `matched` is
    returned unchanged. cosine_scores() itself never raises
    (_embedding_retrieval.py contract); the try/except here additionally
    covers the import on a box without numpy.

    `exclude` filters the widen pass (load_reasoning_bank passes
    is_universal_rb — the universal partition has its own cap and must not
    be double-returned via the domain list).
    """
    cfg = _load_retrieval_config()
    if not cfg.get("embedding_blend_enabled", False):
        _BLEND_STATS["supplementary_blend_status"] = "off"
        return matched
    try:
        from _embedding_retrieval import cosine_scores
        query = " ".join(c for c in categories
                         if isinstance(c, str) and c).strip()
        scores = cosine_scores(query) if query else {}
    except Exception:
        scores = {}
    if not scores:
        # Flag ON but nothing scored: absent/corrupt index, unloadable
        # encoder, or an empty query. Distinguish index-absent — it is the
        # one state a box can FIX (build the index) and the one that hid
        # for 25 days (g-115-6860).
        status = "no_scores"
        try:
            from _embedding_retrieval import index_available
            if not index_available():
                status = "no_scores:index_absent"
        except Exception:
            pass
        _BLEND_STATS["supplementary_blend_status"] = status
        global _BLEND_DEGRADED_WARNED
        if not _BLEND_DEGRADED_WARNED:
            _BLEND_DEGRADED_WARNED = True
            print("[retrieve] embedding blend DEGRADED (%s): "
                  "embedding_blend_enabled=true but no cosine scores — "
                  "supplementary retrieval is serving the token-overlap "
                  "baseline. If the index is absent, provision per "
                  "g-115-3115 then: python3 core/scripts/"
                  "embedding-index-build.py --build (g-115-6860)"
                  % status, file=sys.stderr)
        return matched
    _BLEND_STATS["supplementary_blend_status"] = "served"
    try:
        min_cos = float(cfg.get("embedding_min_cosine", 0.35))
    except (TypeError, ValueError):
        min_cos = 0.35
    matched_ids = {r.get("id") for r in matched}
    widened = list(matched)
    for r in active:
        rid = r.get("id")
        if not rid or rid in matched_ids:
            continue
        if exclude is not None and exclude(r):
            continue
        if scores.get(rid, 0.0) >= min_cos:
            widened.append(r)
    widened.sort(key=lambda r: -scores.get(r.get("id"), min_cos))
    return widened

# g-115-4039 — per-request carrier for the universal pull-slot outcome.
#
# The producer (_universal_relevance_split, inside load_reasoning_bank) and the
# consumer (_log_retrieval_trace) sit in different call frames with no shared
# argument path, and the daemon endpoint calls them separately. A module global
# is the same channel WORLD_DIR already uses here, and it is safe for the same
# documented reason: mind_api/src/endpoints/retrieve.py runs BOTH the retrieval
# and the trace write inside `_swap_lock`, so requests are serialized. Under the
# CLI there is one request per process.
#
# Consumed with POP semantics, never plain read: a request that does not reach
# the split (supplementary-only, as_of, an early return) must not inherit the
# PREVIOUS request's numbers. Clearing on read makes a stale carry-over
# impossible rather than merely unlikely.
_UNIVERSAL_SPLIT_STATS: "dict" = {}

# g-115-6860 — per-request carrier for the SUPPLEMENTARY (domain-RB +
# guardrail) blend outcome. Same channel + pop semantics as
# _UNIVERSAL_SPLIT_STATS above (g-115-4039): producer _embedding_blend,
# consumer _log_retrieval_trace, cleared unconditionally in the loaders.
# Motivating incident: embedding_blend_enabled=true fleet-wide since 07-25
# while this box had NO index — cosine_scores returned {} and the blend's
# degraded branch is byte-identical to "no semantic hits", so 25 days of
# queries served the token-overlap baseline (the 13%-hit@3 arm of the
# g-306-77 A/B) with zero telemetry. g-115-4039 gave the UNIVERSAL lane a
# status field; this is the same visibility for the domain/guardrail lane.
# Values: "off" | "served" | "no_scores" | "no_scores:index_absent".
_BLEND_STATS: "dict" = {}
# One-time-per-process stderr warning flag for the degraded case (mirrors the
# g-115-3387 encoder-fallback diagnostic: soft must not mean SILENT).
_BLEND_DEGRADED_WARNED = False


def _universal_relevance_split(universal_sorted, categories, stats=None):
    """g-306-86 — split the universal-RB cap between the utilization push
    floor and query-relevance pulls, flag-gated by `embedding_blend_enabled`.

    Motivating finding (g-306-77 b2 acceptance A/B, 2026-07-10): the
    universal partition returned the SAME top-UNIVERSAL_RB_CAP(5)-by-
    utilization entries for ANY query — 9/9 direct paraphrases of
    universal lessons (rb-629 silent-loop-death, rb-2859 archive-before-
    delete, ...) came back unranked in BOTH A/B arms. Query relevance
    played no role in the lane where the framework's hard-won meta-lessons
    live.

    Contract:
      - Input is the FULL universal list already ordered by
        sort_universal_rbs (utilization desc) — the caller's pre-cap list.
      - Flag off / no scores / no qualifying picks → EXACTLY
        universal_sorted[:UNIVERSAL_RB_CAP], today's behavior.
      - Flag on: the top (CAP - universal_relevance_slots) by utilization
        are ALWAYS returned first (the push floor — the "always surface
        meta-lessons" guarantee, narrowed but never removed). The remaining
        slots go to the highest-cosine entries from the rest of the
        universal list that clear embedding_min_cosine, in cosine order.
        Unfilled pull slots BACKFILL by utilization order, so the total is
        min(CAP, len(universal_sorted)) in every branch — the cap never
        shrinks because a query had no semantic relatives.
      - Bump-set == return-set holds downstream for free: the caller bumps
        exactly the list this returns.
      - prime's boot display is NOT this lane — it reads
        `reasoning-bank-read.sh --universal` (a separate reader); only
        retrieve's meta_lessons output changes here.
    """
    def _rec(status, picked=0, backfilled=0, slots_n=0):
        """Record the pull-slot outcome into the caller's `stats` dict.

        g-115-4039: the trace previously carried only COUNTS of returned items,
        so a lane where both pull slots were filled by cosine and one where
        cosine picked NOTHING and utilization backfilled every slot both emitted
        n_reasoning_bank=5 — the cosine feature silently not running was
        invisible to every metric (same shape as guard-1977: a check that
        declines to run is indistinguishable from one that ran and passed).

        `status` is FOUR-valued on purpose. A bare picked=0 count would collapse
        three genuinely different conditions into one number and rebuild the
        very defect this instruments: `off` (feature flag disabled), `no_slots`
        (configured to 0 pull slots), `no_scores` (enabled, but the embedding
        index returned nothing — a broken/missing index, NOT an abstention), and
        `ran` (cosine actually scored candidates). Only under `ran` does
        backfilled measure true abstention; treating the other three as
        abstention would inflate the fleet-wide rate with configuration and
        infrastructure states.
        """
        if stats is None:
            return
        stats["universal_cosine_status"] = status
        stats["n_universal_cosine_picked"] = picked
        stats["n_universal_backfilled"] = backfilled
        stats["n_universal_pull_slots"] = slots_n

    cfg = _load_retrieval_config()
    if not cfg.get("embedding_blend_enabled", False):
        _rec("off")
        return universal_sorted[:UNIVERSAL_RB_CAP]
    try:
        slots = int(cfg.get("universal_relevance_slots", 2))
    except (TypeError, ValueError):
        slots = 2
    slots = max(0, min(slots, UNIVERSAL_RB_CAP))
    if slots == 0:
        _rec("no_slots")
        return universal_sorted[:UNIVERSAL_RB_CAP]
    try:
        from _embedding_retrieval import cosine_scores
        query = " ".join(c for c in categories
                         if isinstance(c, str) and c).strip()
        scores = cosine_scores(query) if query else {}
    except Exception:
        scores = {}
    if not scores:
        _rec("no_scores", slots_n=slots)
        return universal_sorted[:UNIVERSAL_RB_CAP]
    try:
        min_cos = float(cfg.get("embedding_min_cosine", 0.35))
    except (TypeError, ValueError):
        min_cos = 0.35
    floor_n = UNIVERSAL_RB_CAP - slots
    out = list(universal_sorted[:floor_n])
    rest = universal_sorted[floor_n:]
    pulls = [r for r in rest if scores.get(r.get("id"), 0.0) >= min_cos]
    pulls.sort(key=lambda r: -scores.get(r.get("id"), 0.0))
    out.extend(pulls[:slots])
    cosine_picked = len(pulls[:slots])
    # Count what the backfill loop ACTUALLY appends rather than inferring it from
    # (slots - cosine_picked): when `rest` is shorter than the remaining slots the
    # loop cannot fill them, so the inferred figure would over-report backfill for
    # a small corpus. Measure the append, do not derive it.
    before_backfill = len(out)
    if len(out) < UNIVERSAL_RB_CAP:
        picked = {id(r) for r in out}
        for r in rest:
            if id(r) not in picked:
                out.append(r)
                picked.add(id(r))
            if len(out) >= UNIVERSAL_RB_CAP:
                break
    _rec("ran", picked=cosine_picked,
         backfilled=len(out) - before_backfill, slots_n=slots)
    return out

def _tree_doc_id_for(node):
    """This node's embedding-index doc id: 'tree:' + tree-root-relative path
    (no .md), derived from the `file` field. MUST mirror embedding-index-
    build.py tree_doc_id — the write-side of this join. Basename keys are
    deliberately NOT the join key (g-306-45: a basename join against a
    path-keyed store matched zero real records and shipped silently inert)."""
    f = str((node or {}).get("file") or "").replace("\\", "/")
    marker = "knowledge/tree/"
    i = f.find(marker)
    if i < 0:
        return None
    rel = f[i + len(marker):]
    if rel.endswith(".md"):
        rel = rel[:-3]
    return ("tree:" + rel) if rel else None

def _tree_embedding_scores(categories, nodes):
    """g-306-83 — flag-gated tree-lane cosine scores, joined back to the
    caller's BASENAME namespace.

    Returns {basename_key: cosine} for every node present in the persisted
    index, or {} when `embedding_tree_channel_enabled` is off, the index is
    absent/degraded, or anything fails (structural graceful degradation —
    same contract as _embedding_blend). The caller uses the map twice:
    eligibility (nodes above embedding_min_cosine join the matched set on
    the 'embedding' channel) and ranking (_score_weight_limit swaps the
    TF-IDF cosine bonus for these scores when present).
    """
    cfg = _load_retrieval_config()
    if not cfg.get("embedding_tree_channel_enabled", False):
        return {}
    try:
        from _embedding_retrieval import cosine_scores
        query = " ".join(c for c in categories
                         if isinstance(c, str) and c).strip()
        raw = cosine_scores(query) if query else {}
    except Exception:
        raw = {}
    if not raw:
        return {}
    out = {}
    for key, node in (nodes or {}).items():
        did = _tree_doc_id_for(node)
        if did is not None and did in raw:
            out[key] = raw[did]
    return out

def _sort_by_utility(entries, counters=None):
    """In-place sort by utilization.utilization_score desc, provenance weight
    desc (M-5), then created desc.

    Generic counterpart to `sort_universal_rbs` — applies to any record with
    the standard `utilization` sub-object schema (RB, guardrails, pattern
    signatures). M-5 adds provenance as a secondary sort key so DIRECT-provenance
    entries surface above HEARSAY at equal (poignancy-weighted) utility. Tie-break
    by `created` ensures fresh entries surface above older ones at equal
    utility + provenance. Mutates and returns the list.

    Poignancy blend (g-306-08): when enabled, utilization_score is MULTIPLIED by
    the poignancy factor (1.0 .. poignancy_weight_max). Multiplicative (not
    additive) is load-bearing: utilization_score values are tiny (p75 ~ 0.007 on
    the live corpus) while an additive bonus of up to 0.5 would dwarf them and
    let poignancy DOMINATE utilization — the g-306-08 A/B caught exactly that.
    Multiplicative is scale-invariant and bounded: a record can be displaced only
    by one within poignancy_weight_max x of its utilization, never by an
    arbitrarily-lower-utility record (the "no known-good knowledge hidden"
    property). The poignancy factor is a tertiary key so it still orders the
    large utilization_score==0 mass (where util*factor==0 for all). Flag off or
    null poignancy -> factor 1.0, so ordering is identical to pre-g-306-08 by
    default; records without a poignancy field (guardrails, pattern signatures)
    are unaffected.

    `counters` (g-358-05) is the utilization sidecar map for the ONE store these
    entries came from; None keeps the embedded-field reading. No kind dispatch
    is needed here even though this sorter serves three stores, because every
    call site is SINGLE-KIND — load_reasoning_bank passes rb counters,
    load_guardrails passes guardrails counters, and load_pattern_signatures
    passes None (pattern-signatures has no sidecar: _utilization_store.KINDS is
    ('reasoning-bank', 'guardrails'), so asking for one would raise). Sharing
    one map across a call's two sorts is why it is a PARAMETER rather than a
    load inside this function: load_reasoning_bank sorts twice (domain here,
    universal via sort_universal_rbs) and must not pay the read twice.

    A concurrent implementation of g-358-05 loaded a kind-MERGED map inside this
    function instead, on the premise that the lists arriving here are mixed. They
    are not — all three call sites are single-kind, which is what makes the
    per-store parameter both correct and more precise. Its measurement is worth
    keeping though: while no sidecar exists a load is 2 is_file() checks (11us,
    measured 2026-08-17) and `utilization_of` falls through to the embedded
    field, so ordering is byte-identical to pre-seam behaviour. WHEN THE WRITER
    LANDS each load becomes a real read of a small object — that is the point at
    which to decide on caching, deliberately not now (nothing to cache yet, and a
    cache would need an invalidation story).
    """
    cfg = _load_retrieval_config()
    blend = cfg.get("poignancy_blend_enabled", False)

    # M-5 provenance weights — duplicated from tree_match.PROVENANCE_WEIGHTS to
    # avoid import-cycle risk (retrieve.py already imports from tree_match;
    # tree_match must not import from retrieve). The enum is stable (M-1).
    _PROV_WEIGHTS = {
        "DIRECT": 1.0, "INFERRED": 0.7,
        "SYNTHESIZED": 0.8, "HEARSAY": 0.5,
    }
    _PROV_DEFAULT = 0.9

    def _prov_w(entry):
        prov = entry.get("provenance")
        if not prov:
            return _PROV_DEFAULT
        return _PROV_WEIGHTS.get(str(prov).upper(), _PROV_DEFAULT)

    def _key(r):
        # No `or {}` guard: utilization_of documents that it returns {} rather
        # than None precisely so callers can .get() directly (see its docstring).
        util = _utilization_of(r, counters).get("utilization_score", 0) or 0
        # M-5: provenance is the secondary key (a trust signal — DIRECT over
        # HEARSAY at equal utility). When the poignancy blend is on, the
        # poignancy factor stays a lower-priority key so it still orders the
        # large util*pf==0 mass within equal provenance.
        if blend:
            pf = _poignancy_weight(r, cfg)
            return (util * pf, _prov_w(r), pf, r.get("created", "") or "")
        return (util, _prov_w(r), r.get("created", "") or "")

    entries.sort(key=_key, reverse=True)
    return entries

def load_reasoning_bank(categories, depth="medium", read_only=False, entry_type=None,
                        as_of=None):
    """Load active reasoning bank entries, partitioned into domain + universal.

    entry_type (g-306-11): when non-null, restrict the candidate set to records
    whose `entry_type` field equals it (e.g. "procedure"). The filter is applied
    to `active` BEFORE partition/sort/cap/bump, so the bump-set==return-set
    invariant below still holds and non-matching entries' retrieval_count is
    never polluted. None (the default) = no filter — byte-identical to the
    pre-g-306-11 behavior; existing callers need no change.

    as_of (g-306-36): when non-null (an ISO-8601 instant T), switch from the
    "current active records" view to the BI-TEMPORAL point-in-time view —
    return the record VERSIONS that were valid at T (`_valid_at`), regardless
    of current `status`. The status filter is DROPPED on this path on purpose:
    a record that was active at T but has since been falsified (status retired,
    valid_to=T2) must still surface for "what was believed at T". as_of also
    forces NO counter bump — a historical read is observational and must not
    inflate the retrieval_count that ranks CURRENT records. None (the default)
    = exact pre-g-306-36 current-version behavior.

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
    as_of_dt = _as_of_dt_or_raise(as_of)
    records = _read_store(RB_PATH, "reasoning-bank")
    # g-306-36: as_of set => point-in-time validity filter (versions valid at T,
    # status-agnostic). as_of None => current-active view (byte-identical path).
    if as_of_dt is None:
        active = [r for r in records if r.get("status") == "active"]
    else:
        active = [r for r in records if _valid_at(r, as_of_dt)]
    # g-306-11: optional entry_type filter (e.g. "procedure"). Applied here,
    # before partition/sort/cap/bump, so both partitions and the bump-set are
    # restricted consistently. None => no-op (default).
    if entry_type is not None:
        active = [r for r in active if r.get("entry_type") == entry_type]
    universal = [r for r in active if is_universal_rb(r)]
    domain = [r for r in active if not is_universal_rb(r)
              and _entry_matches(r, categories)]
    # Sidecar counters loaded ONCE and shared by BOTH sorts below (g-358-05):
    # this lane ranks twice — domain here, universal at sort_universal_rbs —
    # and they are the same store, so a second read would be pure waste.
    # Free today (absent sidecar returns {} immediately); measured cost once
    # the writer lands is ~25 ms against this lane's ~100 ms store read.
    _rb_counters = _load_counters("reasoning-bank")
    _sort_by_utility(domain, _rb_counters)
    # g-306-77 b2: flag-gated embedding hybrid (widen + cosine re-rank) BEFORE
    # the cap, so semantic matches compete for slots by relevance. Skipped on
    # as_of reads — blending a historical view against the current-corpus
    # index would rank yesterday's records by today's semantics.
    if as_of_dt is None:
        domain = _embedding_blend(domain, active, categories,
                                  exclude=is_universal_rb)
    domain = domain[:cap]
    sort_universal_rbs(universal, _rb_counters)
    # g-306-86: flag-gated relevance split of the universal cap. as_of reads
    # keep the pure utilization slice — same historical-view reasoning as the
    # domain-lane blend above.
    # Clear UNCONDITIONALLY, before the as_of branch. This must not sit inside
    # the `is None` arm: the consumer pops on the way out, so the carrier is
    # normally empty by the time the next request arrives — but a request that
    # populates it and then RAISES before _log_retrieval_trace (anything between
    # endpoints/retrieve.py:339 and :534) leaves it dirty, and the next request
    # that reaches the trace write WITHOUT running the split then inherits the
    # previous request's numbers. Measured leak: an as_of read emitted
    # status="ran" with a prior request's picked/backfilled counts, and because
    # the rate contract selects exactly status=="ran" rows, the contaminated row
    # is COUNTED — corrupting the metric this instrument exists to produce.
    # Every request that can reach the trace write passes through here
    # (endpoints/retrieve.py:339 is unconditional), so clearing here closes the
    # window entirely. Pop-on-read protects the request AFTER the consumer;
    # this protects the request after a FAILED one. (g-115-4039 fresh-eyes;
    # guard-1663 — never let a process-global carry across owners.)
    _UNIVERSAL_SPLIT_STATS.clear()
    # g-115-6860: same unconditional-clear reasoning for the supplementary
    # blend-status carrier (written by _embedding_blend on both the RB and
    # guardrail calls; load_guardrails clears too for guardrail-only paths).
    _BLEND_STATS.clear()
    # g-306-86 (cont.): as_of reads never run the blend, so there is no pull-slot
    # outcome to report. Leaving the carrier empty (rather than writing zeros)
    # keeps "the lane did not run" distinct from "the lane ran and picked none" —
    # the same conflation the four-valued status exists to prevent.
    if as_of_dt is None:
        universal = _universal_relevance_split(
            universal, categories, stats=_UNIVERSAL_SPLIT_STATS)
    else:
        universal = universal[:UNIVERSAL_RB_CAP]

    # g-306-36: never bump on a point-in-time (as_of) read — it is observational
    # history, not current usage, and would inflate the counters that rank
    # current records (and could touch retired/closed versions).
    if not read_only and as_of_dt is None:
        bump_ids = {r["id"] for r in domain} | {r["id"] for r in universal}

        def _should_bump(rec):
            return (rec.get("id") in bump_ids
                    and rec.get("status") == "active")

        _locked_bump_jsonl(RB_PATH, _should_bump, kind="reasoning-bank")

    return domain, universal

def load_guardrails(categories, depth="medium", read_only=False, as_of=None):
    """Load active guardrails matching the requested categories.

    Filtered by `_entry_matches` (strict category, then token-overlap fallback), sorted by
    `utilization.utilization_score` desc then `created` desc, capped at
    SUPPLEMENTARY_CAPS[depth].

    as_of (g-306-36): point-in-time validity filter — see load_reasoning_bank.
    Non-null as_of returns the guardrail VERSIONS valid at T (status-agnostic,
    no counter bump). None = current-active view (byte-identical path).

    INVARIANT (utility_ratio alignment): bump fires only on the records
    actually returned. Mirrored by utilization-feedback.py
    increment_supplementary which targets `session.supplementary_items`.
    See load_reasoning_bank docstring for the rationale and incident history.
    Concurrent `guardrails-add.sh` writes are protected by the lock.
    """
    cap = SUPPLEMENTARY_CAPS.get(depth, SUPPLEMENTARY_CAPS["medium"])
    as_of_dt = _as_of_dt_or_raise(as_of)
    # g-115-6860: unconditional carrier clear for guardrail-only request
    # paths (load_reasoning_bank has the mirror clear) — as_of requests
    # skip the blend, so the carrier must be empty rather than stale.
    _BLEND_STATS.clear()
    records = _read_store(GUARD_PATH, "guardrails")
    if as_of_dt is None:
        active = [r for r in records if r.get("status") == "active"]
    else:
        active = [r for r in records if _valid_at(r, as_of_dt)]
    filtered = [r for r in active if _entry_matches(r, categories)]
    _sort_by_utility(filtered, _load_counters("guardrails"))
    # g-306-77 b2: flag-gated embedding hybrid — see load_reasoning_bank.
    if as_of_dt is None:
        filtered = _embedding_blend(filtered, active, categories)
    filtered = filtered[:cap]

    if not read_only and as_of_dt is None:
        bump_ids = {r["id"] for r in filtered}

        def _should_bump(rec):
            return (rec.get("id") in bump_ids
                    and rec.get("status") == "active")

        _locked_bump_jsonl(GUARD_PATH, _should_bump, kind="guardrails")

    return filtered

def load_pattern_signatures(categories, depth="medium", read_only=False, as_of=None):
    """Load active pattern signatures matching the requested categories.

    Filtered by `_entry_matches` (strict category, then token-overlap fallback), sorted by utilization, capped at
    SUPPLEMENTARY_CAPS[depth]. Pattern signatures are tiny (~5 active today)
    so the cap rarely binds — the filter is what matters when the corpus grows.

    as_of (g-306-36): point-in-time validity filter — see load_reasoning_bank.
    Pattern signatures carry no explicit valid_from/valid_to yet (out of the
    g-306-35 writer scope), but `_valid_at` falls back to `created`, so an as_of
    query still returns a COHERENT point-in-time view (patterns that existed at
    T) alongside the as_of-filtered RB/guardrails — not current patterns mixed
    with historical RB. None = current-active view (byte-identical path).

    INVARIANT (utility_ratio alignment): bump fires only on returned records.
    See load_reasoning_bank docstring. Concurrent `pattern-signatures-add.sh`
    writes are protected by the lock.
    """
    cap = SUPPLEMENTARY_CAPS.get(depth, SUPPLEMENTARY_CAPS["medium"])
    as_of_dt = _as_of_dt_or_raise(as_of)
    records = read_jsonl(SIGS_PATH)
    if as_of_dt is None:
        active = [r for r in records if r.get("status") == "active"]
    else:
        active = [r for r in records if _valid_at(r, as_of_dt)]
    filtered = [r for r in active if _entry_matches(r, categories)]
    # No counters arg (g-358-05): pattern-signatures has NO sidecar —
    # _utilization_store.KINDS is ('reasoning-bank', 'guardrails') and
    # _check_kind would raise. This lane keeps reading the embedded field, which
    # is correct rather than a gap: nothing splits these counters out.
    _sort_by_utility(filtered)
    filtered = filtered[:cap]

    if not read_only and as_of_dt is None:
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

def load_beliefs(categories, as_of=None):
    """Load active/weakened beliefs. Returns list of belief dicts.

    as_of (g-306-36): when non-null, return the belief VERSIONS valid at the
    instant T (`_valid_at`, status-agnostic) instead of the current
    active/weakened set — "what did I believe at T". Beliefs carry valid_from /
    valid_to (g-306-35 stamping) with last_observed as the legacy floor. None =
    current view (byte-identical path).
    """
    beliefs_data = read_yaml(BELIEFS_PATH)
    if not beliefs_data:
        return []

    beliefs_list = beliefs_data.get("beliefs", [])
    if not isinstance(beliefs_list, list):
        return []

    as_of_dt = _as_of_dt_or_raise(as_of)
    if as_of_dt is None:
        return [
            b for b in beliefs_list
            if b.get("status") in ("active", "weakened")
        ]
    return [b for b in beliefs_list if _valid_at(b, as_of_dt)]

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
    # Cosine slot reservation (g-306-93). The semantic cosine bonus is ADDITIVE
    # (at most COSINE_BONUS_WEIGHT=2.0 of a ~4.5-5.4 base, ~25%) while
    # utility_weight and the MMR path-similarity penalty act on the WHOLE base,
    # so a node can hold the highest cosine of any node for a query and still
    # never be returned. Measured 2026-07-26 on the 12-query tree-embed harness:
    # `server-lifecycle` scored cosine 0.5653 — the top cosine for its query —
    # and was dropped, while 27 SUB-FLOOR nodes were returned (base 4.481 ->
    # utility_weight 0.705 -> pre-MMR rank 21 of 61 -> MMR dropped it as
    # path-redundant with higher-ranked sibling server/session nodes).
    # Reserving the top-N floor-clearing nodes by cosine guarantees the
    # strongest semantic matches survive, without touching how the other
    # (limit - N) slots are ranked. 0 disables (byte-identical to pre-g-306-93).
    # Only active on the real-embedding path; the TF-IDF fallback is untouched.
    "cosine_reserved_slots": 3,
    # Poignancy blend (g-306-08, BRD Gap 1a). DEFAULT OFF — mirrors
    # core/config/tree.yaml retrieval:. When false, _poignancy_weight() returns
    # 1.0 for every record and ranking is identical to pre-g-306-08.
    "poignancy_blend_enabled": False,
    "poignancy_weight_min": 1.0,
    "poignancy_weight_max": 1.5,
    # Poignancy assumed for a null/unparseable rating (g-115-6387). In RAW
    # poignancy units (1-10) so it feeds the same linear map as a real rating —
    # mirroring utility_weight_center, which is likewise expressed in the input's
    # own units. DEFAULT 1.0 IS DELIBERATELY THE PRE-FIX BEHAVIOUR, not the
    # measured corpus mean: an absent key must degrade to today (factor == min)
    # rather than to a value this file cannot verify against the live corpus. The
    # measured value ships in core/config/tree.yaml, where the re-derivation
    # instruction lives next to it.
    "poignancy_weight_center": 1.0,
    # PPR blend (g-306-44, BRD Gap 1b+1c; HippoRAG 2405.14831). DEFAULT OFF —
    # mirrors the poignancy blend above. When false, _ppr_weight() returns 1.0
    # for every node AND _score_weight_limit skips the PPR pass entirely, so
    # ranking is byte-identical to pre-g-306-44. When true, seeds Personalized
    # PageRank from the top-N baseline (token-overlap) matches over the Mind
    # knowledge-graph and applies a boost-only graph-proximity factor, surfacing
    # multi-hop-relevant records a pure-lexical match misses.
    "ppr_blend_enabled": False,
    "ppr_weight_min": 1.0,
    "ppr_weight_max": 1.5,
    # Normalized PPR score assumed for a candidate absent from the knowledge
    # graph (g-115-6387). Same defect shape as poignancy_weight_center, but NO
    # measured value ships: a PPR score is normalized per-query, so its mean is
    # not a static corpus property the way poignancy's is. 0.0 reproduces the
    # pre-fix factor (== ppr_weight_min) exactly. See _ppr_weight's docstring for
    # why the per-query median is the likely right answer if the blend is enabled.
    "ppr_weight_center": 0.0,
    "ppr_seed_top_n": 5,
    # Embedding-cosine hybrid for the supplementary stores (g-306-77 part b2;
    # index built by embedding-index-build.py, queried via _embedding_retrieval).
    # DEFAULT OFF — mirrors the two blends above: when false, _embedding_blend
    # returns its input unchanged and ranking is byte-identical to pre-b2.
    "embedding_blend_enabled": False,
    "embedding_min_cosine": 0.35,
    # g-306-86: universal-RB cap slots reassigned from utilization order to
    # query-cosine order when the blend is ON. 0 disables the split even
    # with the blend enabled; clamped to [0, UNIVERSAL_RB_CAP].
    "universal_relevance_slots": 2,
    # g-306-82: builder-side model choice (embedding-index-build.py). Query
    # side follows the built index's meta.json, never this key.
    "embedding_model_name": "all-MiniLM-L6-v2",
    # g-306-83: tree-lane embedding channel (semantic eligibility in
    # load_tree_nodes + embedding cosine replacing the TF-IDF bonus in
    # _score_weight_limit). Separate flag from the supplementary blend so
    # each lane enables on its own A/B evidence.
    "embedding_tree_channel_enabled": False,
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
    """Clamp(`1.0 + (utility_ratio - center)`, min, max); neutral 1.0 for underretrieved nodes.

    CENTERED on the corpus mean utility_ratio (g-306-95), not on a bare 0.5. The
    old `0.5 + utility_ratio` inverted this function's own stated intent: measured
    over 1254 live nodes, utility_ratio averages ~0.206 among the 1180 nodes that
    reach this path, so the average PROVEN node scored 0.706 while every unmeasured
    node returned the neutral 1.0 — a ~41% ranking edge for having no track record,
    stepped off a cliff at retrieval_count 5 rather than ramped.

    Centering fixes the semantics rather than the symptom: an average node now lands
    ON the neutral 1.0, so the two early-return 1.0s below stop meaning "best
    possible" and start meaning "unmeasured, assume average" — which is what they
    were always intended to mean. The slope is unchanged (1:1 in utility_ratio);
    this is purely a shift of the neutral point, so relative ordering among measured
    nodes is identical and only measured-vs-unmeasured ordering changes.

    `utility_weight_center` is a measured constant, not a tuning knob — see
    core/config/tree.yaml for the derivation and when to re-derive it. It defaults
    to 0.0 here, which reproduces a 1.0-centered (uncentered) weight rather than
    silently restoring the old inverted 0.5 base, so a missing key degrades to
    "no centering" instead of to the bug.
    """
    cfg = cfg or _load_retrieval_config()
    rc = node.get("retrieval_count", 0) or 0
    if rc < cfg["utility_weight_neutral_below_retrievals"]:
        return 1.0
    # Path-c no-feedback-signal exemption (origin/design g-115-1284, guard-393).
    # A node with zero feedback of ANY kind is UNMEASURED, not unhelpful:
    # times_inferred_helpful is starved (no realistic auto-increment path) while
    # times_noise auto-accrues, so without this guard _utility_weight penalizes the
    # absent positive signal as negative (utility_ratio -> 0, w -> 0.5 floor). Extends
    # the "can't punish what hasn't had a chance" principle (the rc check above) from
    # retrieval-count to feedback-signal. Any times_noise keeps the penalty (real
    # negative signal); self-correcting -- junk accrues noise -> re-penalized,
    # valuable-but-uncited stays neutral -> fair chance to be retrieved + attested.
    if (node.get("times_helpful", 0) or 0) == 0 \
       and (node.get("times_inferred_helpful", 0) or 0) == 0 \
       and (node.get("times_noise", 0) or 0) == 0:
        return 1.0
    ur = node.get("utility_ratio", 0) or 0
    center = float(cfg.get("utility_weight_center", 0.0) or 0.0)
    w = 1.0 + (float(ur) - center)
    lo = float(cfg["utility_weight_min"])
    hi = float(cfg["utility_weight_max"])
    if w < lo:
        return lo
    if w > hi:
        return hi
    return w

def _poignancy_weight(record, cfg=None):
    """Map a record's poignancy (1-10) to a multiplicative score factor.

    Boost-only, null-safe, flag-gated (g-306-08, BRD Gap 1a; Generative Agents
    2304.03442). Returns 1.0 — a no-op factor — when the blend flag is off OR
    the record carries no poignancy. When enabled and poignancy is set, maps
    poignancy linearly from [1, 10] onto [poignancy_weight_min,
    poignancy_weight_max]. With the default min of 1.0 the factor is always
    >= 1.0, so the blend can only PROMOTE high-poignancy records — it never
    demotes anything below its current effective score, which is what makes the
    "no known-good knowledge hidden" A/B criterion hold by construction.

    `record` is any dict carrying an optional top-level `poignancy` field
    (a tree-node `_tree.yaml` entry OR a reasoning-bank record). Missing, None,
    or unparseable poignancy is treated as `poignancy_weight_center` — the
    corpus-mean rating — so legacy records are null-safe with no backfill.

    NULL IS CENTERED, NOT FLOORED (g-115-6387, 2026-08-16). This function used to
    `return 1.0` for a null rating and call that "neutral". 1.0 is
    poignancy_weight_min — the BOTTOM of the output range, not its middle — so an
    unrated record was ranked as if it were the least significant thing in the
    corpus, and could never be promoted at any k. It is the g-306-95 defect (see
    _utility_weight, whose docstring explains the same centering) mirrored into
    the sibling factor: unmeasured must read "assume average", and this read
    "assume worst". Measured on cc-07 before the fix, over 7604 active rb records
    (74.1% rated): an unrated record entered the top-k zero times at k=20/50/100/
    200, while 100%/82%/78%/71% of ALL demotions were unrated records against a
    25.9% corpus rate.

    The fix substitutes the null INPUT and leaves the rated mapping alone, so:
    (a) a real rating produces the identical factor it did before, and (b) every
    factor stays within [lo, hi] and therefore >= lo, preserving the boost-only
    property the A/B harness's displacement bound depends on. The clamp runs
    AFTER the substitution, so even a mis-configured out-of-range center cannot
    push the factor outside [lo, hi].

    The default center is 1.0 — the pre-fix behaviour — so an absent config key
    degrades to today rather than to an unverified value. The measured constant
    ships in core/config/tree.yaml alongside its re-derivation instruction.
    """
    cfg = cfg or _load_retrieval_config()
    if not cfg.get("poignancy_blend_enabled", False):
        return 1.0
    lo = float(cfg.get("poignancy_weight_min", 1.0))
    hi = float(cfg.get("poignancy_weight_max", 1.5))
    center = float(cfg.get("poignancy_weight_center", 1.0) or 1.0)
    p = record.get("poignancy")
    if p is None:
        p = center
    else:
        try:
            p = float(p)
        except (TypeError, ValueError):
            p = center
    if p < 1.0:
        p = 1.0
    elif p > 10.0:
        p = 10.0
    # p=1 -> lo, p=10 -> hi (linear interpolation).
    return lo + (p - 1.0) / 9.0 * (hi - lo)

_PPR_MODULE_CACHE = None

def _load_ppr_module():
    """importlib-load the hyphen-named knowledge-graph-ppr.py once per process.

    Returns the module, or None if it cannot be loaded (fail-open: a missing or
    broken PPR module just removes the blend, it never breaks retrieval). The
    False sentinel records a prior failed attempt so we do not re-pay the import
    cost on every call when the module is genuinely absent.
    """
    global _PPR_MODULE_CACHE
    if _PPR_MODULE_CACHE is not None:
        return _PPR_MODULE_CACHE or None
    try:
        import importlib.util
        ppr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "knowledge-graph-ppr.py")
        spec = importlib.util.spec_from_file_location("knowledge_graph_ppr",
                                                      ppr_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PPR_MODULE_CACHE = mod
        return mod
    except Exception:
        _PPR_MODULE_CACHE = False  # tried + failed; do not retry this process
        return None

def _compute_ppr_scores(seed_keys, cfg=None):
    """Seed Personalized PageRank from graph-node keys; return {graph_key: norm}.

    `norm` is the PPR score divided by the maximum in the ranking, so it lies in
    [0, 1] and is directly consumable by _ppr_weight. Returns {} (-> _ppr_weight
    no-ops to 1.0 everywhere) when the blend flag is off, there are no seeds, the
    PPR module/graph is unavailable, or the ranking is empty. Fail-open at every
    layer: a PPR failure never breaks retrieval — it removes the boost and leaves
    the baseline ranking unchanged (g-306-44).
    """
    cfg = cfg or _load_retrieval_config()
    if not cfg.get("ppr_blend_enabled", False) or not seed_keys:
        return {}
    mod = _load_ppr_module()
    if mod is None:
        return {}
    try:
        ranked, _meta = mod.compute(list(seed_keys), exclude_pseudo=False)
    except Exception:
        return {}
    if not ranked:
        return {}
    max_score = ranked[0][1] or 0.0
    if max_score <= 0:
        return {}
    return {node: (score / max_score) for node, score in ranked}

def _ppr_weight(graph_key, ppr_scores, cfg=None):
    """Map a node's normalized PPR score to a multiplicative boost factor.

    Boost-only, null-safe, flag-gated (g-306-44, BRD Gap 1b+1c; HippoRAG
    2405.14831). Mirrors _poignancy_weight: returns 1.0 (no-op) when the blend
    flag is off, when ppr_scores is empty, or when this node is absent from the
    PPR ranking. When enabled, maps the node's normalized PPR score (in [0, 1])
    linearly onto [ppr_weight_min, ppr_weight_max]. With the default min of 1.0
    the factor is always >= 1.0, so the blend can only PROMOTE graph-proximate
    records -- never demotes -- preserving the no-regression A/B criterion by
    construction (the same property the poignancy blend relies on).

    NULL CENTERING (g-115-6387, 2026-08-16) -- SHAPE FIXED, VALUE DELIBERATELY
    LEFT AT THE PRE-FIX DEFAULT. This function inherited _poignancy_weight's
    "null -> 1.0" and therefore its defect: 1.0 is ppr_weight_min, the FLOOR, so
    an unscored candidate was ranked as if the graph held the WORST possible
    opinion of it rather than none at all.

    That the null really is "unmeasured" was verified, not assumed from the code
    shape: personalized_pagerank() initializes its rank vector over every node in
    the adjacency and returns a distribution over all of them, so a merely
    graph-DISTANT node gets a small NONZERO score. A None therefore means the
    candidate is absent from the knowledge graph altogether -- never built, or a
    key-resolution miss in _resolve_ppr_key (the rb-245 key-format class) -- which
    is exactly the unmeasured case.

    BUT THE POIGNANCY REMEDY DOES NOT TRANSFER, and this is why no measured value
    ships here. Poignancy's center is a static corpus property (one mean over all
    rated records, re-derivable on a cadence). A PPR score is normalized by THAT
    QUERY's maximum in _compute_ppr_scores, so its distribution is per-query and
    no static constant can be its mean. The likely correct fix is a per-query
    statistic -- the median of ppr_scores.values(), which is already in hand at
    this call -- but that is an unmeasured design choice for a blend that is
    DEFAULT OFF, so it is not made here. The seam exists; the value stays 0.0,
    which reproduces the pre-fix factor (lo) exactly and changes nothing today.
    """
    cfg = cfg or _load_retrieval_config()
    if not cfg.get("ppr_blend_enabled", False):
        return 1.0
    if not ppr_scores:
        return 1.0
    lo = float(cfg.get("ppr_weight_min", 1.0))
    hi = float(cfg.get("ppr_weight_max", 1.5))
    center = float(cfg.get("ppr_weight_center", 0.0) or 0.0)
    score = ppr_scores.get(graph_key)
    if score is None:
        score = center
    score = min(1.0, max(0.0, float(score)))
    return lo + score * (hi - lo)

def _graph_node_key_candidates(key, node):
    """Knowledge-graph node ids ("node:<...>") a retrieval candidate may map to.

    knowledge-graph-build.py keys a tree node by its front-matter `key` when
    present, else by the tree-root-relative POSIX path (no .md suffix).
    retrieve.load_tree_nodes keys the SAME node by BASENAME, so the naive
    "node:"+key the PPR blend first shipped with matched ZERO graph nodes on real
    data -- the blend was silently inert until g-306-45's multi-hop validation
    found it (graph stores node:execution/.../framework-patterns; the blend seeded
    node:framework-patterns). Recover the build's path form from the candidate's
    `file` field and return it FIRST, then the basename form as a fallback (covers
    synthetic test nodes that carry no `file`, and the minority of nodes the build
    keyed by an explicit front-matter `key`). The caller picks whichever form is
    actually present in the graph/PPR ranking.
    """
    out = []
    f = str((node or {}).get("file") or "").replace("\\", "/")
    marker = "/knowledge/tree/"
    i = f.find(marker)
    if i >= 0:
        rel = f[i + len(marker):]
        if rel.endswith(".md"):
            rel = rel[:-3]
        if rel:
            out.append("node:" + rel)
    bk = "node:" + key
    if bk not in out:
        out.append(bk)
    return out

def _resolve_ppr_key(key, node, ppr_scores):
    """Pick this candidate's graph-node id that is present in the PPR ranking,
    preferring the path-derived form (g-306-45). Falls back to the first
    candidate when none is in the ranking (the weight then no-ops to 1.0)."""
    cands = _graph_node_key_candidates(key, node)
    for cand in cands:
        if cand in ppr_scores:
            return cand
    return cands[0]

def _score_weight_limit(matched, channels, limit,
                        query_text="", all_nodes=None, emb_scores=None):
    """Score each matched node, apply utility weighting, sort, limit.
    Replaces tree_match._score_and_limit for full retrieval; the shared
    helper stays unchanged for lightweight lookups (/tree find, etc.).

    When `query_text` and `all_nodes` are both provided, augments each base
    score with a TF-IDF cosine-similarity bonus computed against the full
    corpus. The cosine signal helps specific multi-token matches outrank
    generic-token parents (the audit-driven NOISY-leaf fix). Cosine bonus
    is added to `base` before the utility weight multiplies, so a noisy
    node's low utility_weight still drags down its effective score.

    g-306-83: when `emb_scores` (basename-keyed embedding cosines from
    _tree_embedding_scores) is non-empty, it REPLACES the TF-IDF bonus —
    same COSINE_BONUS_WEIGHT, real semantic cosine instead of token IDF,
    and the tree_idf index build is skipped entirely. Nodes absent from
    the embedding index contribute 0 bonus (their channel/depth/confidence
    signals still rank them). Empty/None emb_scores → the TF-IDF path,
    byte-identical to pre-g-306-83.
    """
    cfg = _load_retrieval_config()

    use_emb = bool(emb_scores)
    idf_index = None
    q_vm = None
    if query_text and all_nodes and not use_emb:
        from tree_idf import build_index, query_vector
        idf_index = build_index(all_nodes)
        q_vm = query_vector(query_text, idf_index["idf"])

    if idf_index is not None:
        from tree_idf import cosine

    scored = []
    for key, node in matched:
        channel = channels.get(key, "parent")
        base = _compute_match_score(key, node, channel)
        if use_emb:
            base += COSINE_BONUS_WEIGHT * float(emb_scores.get(key, 0.0))
        elif idf_index is not None:
            d_vm = idf_index["vectors"].get(key, ({}, 0.0))
            base += COSINE_BONUS_WEIGHT * cosine(q_vm, d_vm)
        w = _utility_weight(node, cfg)
        # Poignancy blend (g-306-08): third multiplicative factor, 1.0 (no-op)
        # when the blend flag is off or the node carries no poignancy.
        p = _poignancy_weight(node, cfg)
        effective = base * w * p
        scored.append((key, node, effective, channel, base, w))
    scored.sort(key=lambda x: -x[2])

    # PPR blend (g-306-44): seed Personalized PageRank from the top-N baseline
    # (token-overlap) matches and apply a boost-only graph-proximity factor, so
    # records reachable in 1-2 hops from the recognized query entities surface
    # above lexically-unrelated ones (HippoRAG 2405.14831). Skipped entirely when
    # the flag is off -> ranking byte-identical to baseline (zero-cost no-op).
    # Tree-node graph keys are "node:<key>" in the knowledge-graph build namespace.
    if cfg.get("ppr_blend_enabled", False) and scored:
        top_n = int(cfg.get("ppr_seed_top_n", 5) or 5)
        # Seed from the top-N baseline matches, mapping each to its knowledge-graph
        # node id (path-derived via _graph_node_key_candidates) rather than the
        # naive "node:"+basename, which matched NOTHING in the graph -- the blend
        # was inert on real data until g-306-45 (graph keys are node:<relpath>).
        seed_keys = []
        for entry in scored[:top_n]:
            seed_keys.extend(_graph_node_key_candidates(entry[0], entry[1]))
        ppr_scores = _compute_ppr_scores(seed_keys, cfg)
        if ppr_scores:
            rescored = [
                (key, node,
                 eff * _ppr_weight(_resolve_ppr_key(key, node, ppr_scores),
                                   ppr_scores, cfg),
                 channel, base, w)
                for (key, node, eff, channel, base, w) in scored
            ]
            rescored.sort(key=lambda x: -x[2])
            scored = rescored

    if all_nodes and len(scored) > limit:
        # Cosine slot reservation (g-306-93). Pull the top-N floor-clearing
        # nodes by SEMANTIC cosine out of the pool, fill the remaining slots
        # with the unchanged MMR pass, then re-sort the union by effective
        # score. Reserved nodes are GUARANTEED a slot but are NOT promoted —
        # they land in their natural effective-score position, so this fixes
        # exclusion without distorting the returned ORDER.
        reserved = []
        n_reserve = int(cfg.get("cosine_reserved_slots", 0) or 0)
        if use_emb and n_reserve > 0 and limit > 1:
            floor = float(cfg.get("embedding_tree_min_cosine",
                                  cfg.get("embedding_min_cosine", 0.35)))
            eligible = [e for e in scored
                        if float(emb_scores.get(e[0], 0.0)) >= floor]
            eligible.sort(key=lambda e: -float(emb_scores.get(e[0], 0.0)))
            # Never reserve every slot — MMR must keep meaningful authority.
            reserved = eligible[:min(n_reserve, limit - 1)]
        if reserved:
            reserved_keys = {e[0] for e in reserved}
            rest = [e for e in scored if e[0] not in reserved_keys]
            filled = _mmr_rerank(rest, all_nodes, limit - len(reserved))
            merged = reserved + list(filled)
            merged.sort(key=lambda x: -x[2])
            return merged
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

# Identifier-preserving tokenizer (g-115-3144). The previous `[a-z0-9]+` SPLIT
# on `-` and `_`, so `movement-navigation` became {movement, navigation} and no
# token could ever carry structural shape — which made rb-1729's "token SHAPE
# ([-_0-9]-bearing identifier) is the discriminator, not generic prose vocab"
# rule structurally INAPPLICABLE downstream. Keeping identifiers whole is the
# precondition for that rule to mean anything.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_STRUCTURAL_RE = re.compile(r"[-_0-9]")


def _is_structural_token(t):
    """rb-1729 discriminator: a [-_0-9]-bearing identifier, not prose vocab.

    `rb-1729`, `movement-navigation`, `loop_state`, `g-115-3144` qualify;
    `architecture`, `framework`, `never`, `first` do not.
    """
    return len(t) > 3 and bool(_STRUCTURAL_RE.search(t))


def _distinctive_tokens(text):
    """Extract distinctive tokens: identifiers kept whole, structural ones first.

    Two properties matter to the consumer (utilization-feedback.infer_feedback):

    1. Identifier shape survives (see `_TOKEN_RE` above).
    2. Structural tokens are ranked AHEAD of prose BEFORE the cap. The old
       version took the first 40 survivors in DOCUMENT ORDER, so the cap kept
       whatever happened to appear early — usually prose. Ranking first means
       the cap keeps the informative tokens.

    WHY (g-115-3134, measured): the old output was not distinctive at all. With
    a 31-word stopword list and no rarity test it emitted `all, must, never,
    two, first, when, they, only, same, where, even, full, like, across`, and
    the consumer needed only ONE of those to appear anywhere in a multi-KB goal
    description. Measured over 6 real manifests (485 items): mean
    helpful/population 0.922. An unrelated CAKE RECIPE scored 0.627 against the
    same manifests — 68% of every "helpful" verdict was reproducible by
    topically-unrelated text. The name `_distinctive_tokens` described an
    intent the implementation never had.
    """
    if not text:
        return []
    seen = []
    seen_set = set()
    for raw in _TOKEN_RE.findall(str(text).lower()):
        t = raw.strip("-_")
        if len(t) < 3 or t in _UTIL_STOPWORDS or t in seen_set:
            continue
        seen.append(t)
        seen_set.add(t)
    # Structural first, then prose, THEN cap — order matters (property 2).
    ordered = [t for t in seen if _is_structural_token(t)]
    ordered += [t for t in seen if not _is_structural_token(t)]
    return ordered[:_MAX_DISTINCTIVE_TOKENS]

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

    Replaces the affected ROWS with stripped COPIES; it never mutates a record
    in place. Callers wanting full bodies should pass `--full-content`.

    *** THE COPY IS LOAD-BEARING (g-115-3387, 2026-07-27). *** These rows are
    the SHARED jsonl-cache dicts -- `mind_api/src/jsonl_cache.py:53` warns in
    capitals that "the returned list and its dicts are the SHARED cache copy
    ... copy.deepcopy first if you need to modify". The previous in-place form
    (`r["content"] = None`) did exactly what that forbids, so a single
    metadata-only retrieval PERMANENTLY NULLED `content` on the daemon's cached
    reasoning-bank records -- for every later caller, including ones that
    explicitly passed `--full-content`, until the cache reloaded on mtime/TTL.

    Metadata-only is the DEFAULT, so in practice nearly every retrieval was
    poisoning the cache for the entries it touched. MEASURED: rb-3698 carries
    1243 chars in the store; after one default retrieval touched it, a DIFFERENT
    query with `--full-content` returned content length 0. Consumers that need
    the lesson text -- the code-review-protocol pre-apply consultation,
    encode-session dedup, /respond -- silently saw title-only entries and could
    not tell the difference from an entry that genuinely had no content.
    """
    for bucket in ("reasoning_bank", "meta_lessons"):
        rows = result.get(bucket) or []
        stripped = []
        for r in rows:
            # Keep title + when_to_use (both short, highly discriminative).
            # Drop content (multi-paragraph lesson text) + description
            # (redundant long-form when present). Copy-on-write: only rows
            # that actually carry a long-form field pay for a dict copy.
            if ("content" in r) or r.get("description"):
                r = dict(r)
                if "content" in r:
                    r["content"] = None
                if r.get("description"):
                    r["description"] = None
            stripped.append(r)
        if rows:
            result[bucket] = stripped
    sigs = result.get("pattern_signatures") or []
    if sigs:
        out = []
        for p in sigs:
            desc = p.get("description")
            if isinstance(desc, str) and len(desc) > 240:
                # Signature descriptions occasionally balloon; truncate rather
                # than null so title-less sigs retain a handle. 240 ≈ one tweet.
                p = dict(p)
                p["description"] = desc[:240].rstrip() + "…"
            out.append(p)
        result["pattern_signatures"] = out
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
      agent             — MIND_AGENT or "unknown"
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

    Universal pull-slot fields (g-115-4039) — PRESENT ONLY when the universal
    relevance split ran on this request (absent on supplementary-only and as_of
    reads). Before these, every count in this record was a count of RETURNED
    items, so a lane where cosine filled both pull slots and one where cosine
    picked nothing and utilization backfilled every slot were byte-identical:
    both emitted n_reasoning_bank=5. The cosine path silently not running was
    unmeasurable fleet-wide.
      universal_cosine_status    — off | no_slots | no_scores | ran

    Supplementary blend field (g-115-6860) — PRESENT ONLY when _embedding_blend
    ran on this request (absent on as_of reads). The domain-RB/guardrail twin
    of universal_cosine_status: without it, flag-ON with no index was
    byte-identical to "no semantic hits" and a box served the token baseline
    for 25 days unmeasured.
      supplementary_blend_status — off | served | no_scores | no_scores:index_absent
      n_universal_pull_slots     — configured pull slots for this request
      n_universal_cosine_picked  — slots filled by cosine (>= min_cosine)
      n_universal_backfilled     — slots filled by utilization-order backfill

    ABSTENTION RATE = n_universal_backfilled / n_universal_pull_slots, computed
    ONLY over rows with universal_cosine_status == "ran". The other three
    statuses are configuration or infrastructure states, not abstentions —
    folding them in would report a disabled flag or a missing embedding index as
    a cosine miss.

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
            "agent": os.environ.get("MIND_AGENT", "unknown"),
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
        # Dropped-key detection (g-115-3416). The n_* keys above are a FIXED
        # allowlist over items_returned, so a store lane added to items_returned
        # without a matching n_<store> here vanishes from the trace with no
        # error — and this trace is what the retrieval audits count. The source
        # is internal, so this is producer/consumer version skew rather than a
        # caller-contract break; it is still silent, which is the defect.
        # Report, do not reject (rb-538 / guard-527). Cheap: a set difference.
        _carried = {k[2:] for k in record if k.startswith("n_")}
        # str(k), not k. This block sits INSIDE the enclosing
        # `try/except Exception: return`, so a raise here silently discards the
        # WHOLE trace row — the observability addition suppressing the
        # observability it is attached to. Measured: one non-string key in
        # items_returned made `sorted()` raise and the row vanish, on an input
        # that wrote fine before this block existed.
        _dropped = sorted(str(k) for k in items_returned if k not in _carried)
        if _dropped:
            print(
                "WARN: retrieve trace dropped %d unrecognized items_returned "
                "key(s): %s. _log_retrieval_trace carries a fixed n_<store> "
                "allowlist; these counts were NOT written to "
                "retrieval-trace.jsonl. Add n_<store> to the record if the lane "
                "should be audited." % (len(_dropped), ", ".join(_dropped)),
                file=sys.stderr,
            )
        # g-115-4039 — universal pull-slot outcome, POPPED (not read) so a
        # request that never reached the split cannot inherit the previous
        # request's numbers. Absent keys mean "the blend lane did not run on
        # this request", which is deliberately DIFFERENT from status="ran" with
        # zero picks; a consumer computing the fleet-wide abstention rate must
        # filter to status == "ran" or it will count configuration and
        # missing-index states as abstentions.
        if _UNIVERSAL_SPLIT_STATS:
            record.update(_UNIVERSAL_SPLIT_STATS)
            _UNIVERSAL_SPLIT_STATS.clear()
        # g-115-6860 — supplementary blend outcome, same pop semantics.
        # Absent key = the blend lane did not run on this request (as_of /
        # early return); "no_scores:index_absent" = flag ON but this box has
        # no built index — the degraded state that hid for 25 days.
        if _BLEND_STATS:
            record.update(_BLEND_STATS)
            _BLEND_STATS.clear()
        # Same best-effort append pattern as _record_fallback_hit. Single-line
        # JSON under PIPE_BUF (4 KB) is single-write atomic on most filesystems
        # — torn-line risk is observability-grade, not durable-state-grade.
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return

# ---------------------------------------------------------------------------
