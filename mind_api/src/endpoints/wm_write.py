"""POST /v1/wm/{set,append,clear,prune,init,reset,clear-identity},
GET /v1/wm/ages — working-memory writes + age report.

Daemonises core/scripts/wm.py cmd_set / cmd_append / cmd_clear / cmd_prune /
cmd_init / cmd_reset / cmd_clear_identity / cmd_ages. Working memory is
PER-AGENT: ctx.paths.agent / session / working-memory.yaml.

BYTE-COMPATIBILITY: wm.py does NOT use _fileops.locked_modify_yaml — it has
its own write path (wm.py write_yaml): plain `yaml.dump(data, f,
default_flow_style=False, allow_unicode=True, sort_keys=False)` with the
DEFAULT Dumper (NOT CSafeDumper), atomic tmp(.yaml.tmp)+replace, and NO
history/changelog. _write_wm below replicates that EXACTLY. Using CSafeDumper
here would be a silent divergence — wm.py uses the default Dumper, so we must
too.

LOCKING: file_locks.locked(WM_PATH, stale_seconds=10) acquires
WM_PATH.with_suffix('.lock') — the SAME lock file wm.py's wm_lock() uses
(WM_LOCK_PATH = WM_PATH.with_suffix('.lock'), stale_seconds=10), so daemon
and any remaining CLI/direct writer serialise correctly during transition.

Constants + helpers (resolve_slot, gates, prune logic, _default_wm_data) are
lifted VERBATIM from wm.py; sys.exit() becomes HTTP 400. Paths resolve against
ctx (agent / project_root / world), never the wm.py module globals.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .. import file_locks
from ..agent_paths import assert_not_cruft

# --- Constants lifted verbatim from wm.py ---------------------------------

TOP_LEVEL_KEYS = {
    "encoding_queue", "session_id", "session_start",
    "goals_completed_this_session", "aspiration_touched_last",
    "last_goal_category", "capture_evictions",
}
SESSION_IDENTITY_FIELDS = {"session_start"}

# : TOP-LEVEL keys that must survive wm-reset but are NOT identity.
#
# Deliberately a SEPARATE constant from SESSION_IDENTITY_FIELDS, and that is the
# load-bearing part: `clear-identity` (below) NULLS every member of that set, so
# adding a key there to make it survive reset would make clear-identity destroy
# it instead — the opposite of the intent.
#
# RESET_SURVIVING_SLOTS cannot express this either: it is consulted ONLY inside
# `for slot_name, slot_val in existing_slots.items()`, and a top-level key never
# enters that loop. (Verified before this fix — it is the obvious move and it is
# a no-op.)
#
# capture_evictions is a top-level counter whose producer (wm-append eviction)
# and consumer (array_limits cap sizing) sit on opposite sides of the
# aspirations-consolidate Step-5 wm-reset, which fires MID-SESSION at every
# autocompact — so without this it reports a since-last-autocompact tally while
# being read as a lifetime one.
RESET_SURVIVING_TOP_LEVEL = {"capture_evictions"}
DEFAULT_SLOT_TYPES = [
    "active_constraints", "active_context", "active_hypothesis", "active_strategy",
    "archived_context", "cross_domain_transfer", "domain_data",
    "ephemeral_observation", "knowledge_debt", "known_blockers",
    "micro_hypotheses", "pending_resolutions", "recent_violations",
    "sensory_buffer", "session_goal", "conclusions",
]
ARRAY_SLOTS = {
    "knowledge_debt", "known_blockers", "micro_hypotheses",
    "recent_violations", "sensory_buffer", "conclusions",
    "spark_capture",  #  — see wm.py for why membership guards survival
    "exp_capture",    #  — experience sibling of spark_capture, same reason
    "hyp_capture",    #  — hypothesis-evidence sibling, same reason
    "encoding_capture",  #  — tree/domain-fact sibling, same reason. THIS
                         # copy is the live one: wrappers are daemon-only, so
                         # wm-append routes here and the scalar-eviction predicate
                         # at ~L640 reads THIS set. Editing wm.py alone changes
                         # nothing at runtime (guard-742 class); the parity test
                         # test_wm_reset_cadence.py is what makes that loud.
}
MAP_SLOTS = {
    "active_context": {"summary": None, "experience_refs": [], "retrieval_manifest": None},
    "archived_context": {"summary": None, "experience_refs": []},
}
STRUCTURED_DICT_SLOTS = {"loop_state"}
# Top-level slots that hold a LIST OF ROWS and nothing else. Deliberately NOT
# ARRAY_SLOTS (membership there governs reset/prune survival — ); this
# set only says a scalar written here is corruption, never data. Measured
# 2026-08-28 on a live deployment: `wm-set.sh goals_completed_this_session
# '<timestamp>'` (a Body reaching for a last_*-style stamp) left a STRING in the
# agent-wide WM, every per-session WM cloned from it inherited the string (36 of
# 51 sessions), and every worker close's Phase 4b hand-off append was refused
# `not_a_list` for 39 hours. set refuses the scalar naming the two commands that
# DO express the intent; append heals a scalar already on disk (see append_slot).
# TWIN in core/scripts/wm.py — keep in sync; this daemon copy is the LIVE path.
LIST_ROW_SLOTS = {"goals_completed_this_session"}
# Mirror of core/scripts/wm.py CADENCE_TRACKER_PATTERNS — keep in sync (parity
# asserted by test_wm_reset_cadence.py). THIS copy is the live one: wm-prune and
# wm-reset are daemon-only, so a wm.py-only edit changes nothing at runtime
# (guard-742 / guard-2552 class).
#
# Matches the CLASS — a slot recording when something last happened — rather than
# the eight `^last_.*_<verb>$` suffix verbs this enumerated until 
# (2026-08-18). That list left 9 of 17 live stamps unprotected, including all six
# `*_last_dispatch` consumption-aware stamps that stale-sentinel-canary uses as
# its only bypass discriminator. Full reasoning + the measurement: wm.py.
#
# EVERY PATTERN MUST BE ^-ANCHORED — `_is_cadence_tracker` uses `p.match()`, so a
# bare infix `_last_` would compile, read correctly, and match nothing.
CADENCE_TRACKER_PATTERNS = (
    re.compile(r"^last_"),
    re.compile(r"^.*_last_"),
)
# Mirror of core/scripts/wm.py RESET_SURVIVING_SLOTS — keep in sync (parity
# asserted by test_wm_reset_cadence.py). Slots whose writer and reader sit on
# opposite sides of the aspirations-consolidate Step-5 wm-reset boundary
# (Step 0.65 writes journal_cluster_summaries; Step 9 consumes it one-shot for
# handoff key_outcomes). ()
# spark_capture: body-merge generalize-down writes it at consolidate Step -1;
# aspirations-spark Phase 6.5 consumes it. wm-reset at Step 5 sits between them.
# ()
# exp_capture: same transport, same Step-5 boundary; consumed by the
# retrospective, which calls the existing experience writers. ()
# hyp_capture: same transport, same Step-5 boundary; consumed by the
# /review-hypotheses resolution protocol as EVIDENCE INPUT, never as a second
# resolver. Losing it here would not merely drop a payload — it would let a
# hypothesis resolve without the worker's evidence while every signal reads
# success. ()
# encoding_capture: same transport, same Step-5 boundary; INTENDED consumer is
# tree encoding at aspirations-state-update Step 8. MISSING here for its whole
# life until  (2026-08-12) — it was registered in ARRAY_SLOTS in both
# files and in neither reset set, so a reset destroyed it while its three
# siblings survived. Note the parity test could not catch that: both copies
# agreed on the same wrong set, and the survive-assertion exercised one
# representative member. ()
#
# ┌─ HISTORICAL as of 2026-08-22 () — THE CONSUMER NOW EXISTS. ──────┐
# │ Read the paragraph below as the record of a FIXED defect, not as current  │
# │ state. worker_retrospective.py RUN_LANES includes "encoding" (L133) and    │
# │ `_lane_encoding` (L666) is dispatched at L788, so 's reducer half │
# │ landed. Header placed ABOVE the narrative per guard-4079 (a reader's entry │
# │ point is the top of the block). Twins corrected in the same change:        │
# │ core/scripts/wm.py, iteration-close.sh, body-merge.sh.                     │
# └───────────────────────────────────────────────────────────────────────────┘
# THAT CONSUMER DOES NOT EXIST — measured 2026-08-15 (alpha worker, cc-07); this
# line read "consumed by tree encoding at ... Step 8" here and in the wm.py twin
# until then. 0 mentions in aspirations-state-update/SKILL.md, no bridge to
# encoding_queue, and 132 live entries sitting unread in the reducer WM. The
# three siblings above all have real drains; this one has none. Full census,
# measurement and the do-NOT-retire caveat: see the matching block in
# core/scripts/wm.py. Building the consumer is  (HIGH, pending) —
# half-shipped: worker/producer half landed, reducer half did not.
# capture_consumed_hashes () — the durable consumed-watermark for the
# four capture lanes, written by capture_fast_lane._merge_flagged at MERGE time.
# It must survive the reset for the same reason the lanes do, plus one more: the
# watermark exists BECAUSE consumption destroys the live dedup basis, so a
# watermark that is itself wiped restores the original bug while looking fixed.
RESET_SURVIVING_SLOTS = {"journal_cluster_summaries", "spark_capture", "exp_capture",
                         "hyp_capture", "encoding_capture", "capture_consumed_hashes",
                         # : a pinned user-interrupt task is a STANDING HUMAN
                         # OBLIGATION, not session bookkeeping. THIS copy is the live
                         # runtime path (POST /v1/wm/reset), so a wm.py-only edit would
                         # leave the pin droppable by the reset that actually runs.
                         "interrupt_task_open"}

#  — mirror of wm.py. Ordered tuple, deliberately NOT ARRAY_SLOTS: that
# set contains non-capture members, so it is the wrong thing to iterate when the
# question is "what did the worker capture".
CAPTURE_SLOTS = ("spark_capture", "exp_capture", "hyp_capture", "encoding_capture")

#  — mirror of wm.py APPEND_CREATABLE_EXTRA. THIS copy is the LIVE
# one: wm-append.sh is daemon-only, so it routes here and the refusal below is
# what actually runs; the wm.py twin exists for parity and is pinned by
# test_wm_reset_cadence.py. The full rationale — why the seam is lane CREATION
# rather than every append, why it is not keyed on capture-lane shape, and the
# ten orphan lanes measured on cc-08 2026-08-19 — lives on the wm.py definition.
APPEND_CREATABLE_EXTRA = {"notification_log", "proactive_escalation_log"}


def _append_creatable_slots() -> set:
    """Root slot names an append may CREATE: every static registry, unioned."""
    return (set(ARRAY_SLOTS) | set(CAPTURE_SLOTS) | set(DEFAULT_SLOT_TYPES)
            | set(MAP_SLOTS) | set(TOP_LEVEL_KEYS) | set(RESET_SURVIVING_SLOTS)
            | set(STRUCTURED_DICT_SLOTS) | set(APPEND_CREATABLE_EXTRA))


def _unknown_lane_refusal(slot: str, lane_exists: bool):
    """Reason to refuse this append, or None to allow it. Mirror of
    wm.py::unknown_lane_refusal — keep BYTE-equivalent in behaviour. A
    leading-dash root is refused ALWAYS; an unregistered root is refused only
    when the append would CREATE the lane."""
    root = str(slot).split(".")[0]
    if root.startswith("-"):
        return (f"slot name {root!r} starts with '-', which is a command-line "
                f"FLAG, not a slot. The wm-append/wm-clear/wm-set arg loops are "
                f"bare catch-alls where the LAST positional wins, so a trailing "
                f"flag silently becomes the slot name. Re-run with the slot as "
                f"the only positional argument.")
    if lane_exists:
        return None
    creatable = _append_creatable_slots()
    if root in creatable:
        return None
    # Shared `_`-delimited TOKENS as well as substring — see the wm.py twin for
    # why the substring test alone misses `experience_capture` -> `exp_capture`.
    tokens = {t for t in root.split("_") if t}
    near = sorted(n for n in creatable
                  if (root in n or n in root
                      or tokens & {t for t in n.split("_") if t}))[:6]
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    return (f"slot {root!r} is not a registered append target and does not "
            f"exist, so this append would mint a new lane that no consumer "
            f"reads and no carrier mirrors.{hint} If the lane is genuinely new, "
            f"register it in APPEND_CREATABLE_EXTRA (wm.py AND the daemon twin "
            f"in mind_api/src/endpoints/wm_write.py).")


def _eviction_sort_key(x):
    """Evict UNFLAGGED entries before load-bearing ones; oldest-first within
    each class. Byte-identical mirror of wm.py::_eviction_sort_key — the full
    rationale and the 237-entry measurement live on that definition."""
    if not isinstance(x, dict):
        return (0, "0000")
    return (1 if x.get("load_bearing") else 0, x.get("_item_ts", "0000"))


def _is_flagged(x) -> bool:
    """Flag half of _eviction_sort_key as a predicate. Mirror of
    wm.py::_is_flagged — non-dicts sort as UNFLAGGED there, so they count as
    unflagged here too, or the floor below indexes into the wrong entry."""
    return isinstance(x, dict) and bool(x.get("load_bearing"))


# Mirror of wm.py::UNFLAGGED_FLOOR_RATIO / _unflagged_floor (). Kept a
# constant rather than config for the same reason the sort key is duplicated
# rather than imported: two copies of a policy that read one config key is a
# worse failure mode than two copies of a number.
UNFLAGGED_FLOOR_RATIO = 0.2


def _unflagged_floor(limit: int) -> int:
    """Slots of `limit` reserved for UNFLAGGED entries. Zero below limit=2 (a
    one-item lane cannot reserve a share of itself); capped at limit-1 so the
    reservation can never starve flagged entries completely."""
    if not limit or limit < 2:
        return 0
    return min(limit - 1, max(1, int(limit * UNFLAGGED_FLOOR_RATIO)))


def _record_capture_evictions(data, slot_name, n) -> None:
    """Add `n` to the persisted `capture_evictions[slot_name]` tally.

    TOP-LEVEL, not slot_meta, and that is load-bearing: body-merge merges
    slot_meta REDUCER-WINS, so a counter there is discarded at generalize-down
    (g-306-289). Shared by the append and prune paths — prune used to record
    its evictions ONLY into the transient response `report`, so every capture
    entry it destroyed was invisible to the one counter built to measure
    capture loss, and to the reset-survival fix that counter received
    (g-306-355). A blind lane in the counter silently undercounts any future
    cap sizing that reads it.
    """
    if not n:
        return
    ev = data.get("capture_evictions")
    if not isinstance(ev, dict):
        ev = {}
        data["capture_evictions"] = ev
    prev = ev.get(slot_name)
    ev[slot_name] = (prev if isinstance(prev, int) else 0) + n


def _is_cadence_tracker(slot_name: str) -> bool:
    """Mirror of core/scripts/wm.py::_is_cadence_tracker — keep BYTE-equivalent.

    TOP_LEVEL_KEYS excluded first (g-115-6697): they hold session VALUES, not
    cadence bookkeeping, and exactly one — `last_goal_category` — matches the
    `^last_` class pattern while being the deliberate negative control in
    test-wm-prune-cadence-protection.sh. NOTE the parity test compares the
    PATTERNS tuple only, so a divergence in this FUNCTION body is NOT caught.
    """
    if slot_name in TOP_LEVEL_KEYS:
        return False
    return any(p.match(slot_name) for p in CADENCE_TRACKER_PATTERNS)


# --- Paths ----------------------------------------------------------------

def _wm_path(ctx) -> Path:
    # Per-Body WM routing (Phase 1A, ): route by the request's session
    # SID (X-Mind-Sid header). With no SID, or no body-manifest for it,
    # ctx.paths.wm_path returns the agent-wide WM (today's behavior) — so this
    # is backward-compatible and dormant until a 2nd Body exists.
    sid = (ctx.headers.get("x-mind-sid") or "").strip()
    return ctx.paths.wm_path(sid or None)


def _config_path(ctx) -> Path:
    return ctx.paths.project_root / "core" / "config" / "memory-pipeline.yaml"


def _tree_path(ctx) -> Path:
    return ctx.paths.world / "knowledge" / "tree" / "_tree.yaml"


def _gate_path(ctx) -> Path:
    return ctx.paths.project_root / "core" / "scripts" / "loop-state-merge-gate.py"


def _require_agent_header(ctx):
    from ..server import Response
    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for wm writes (g-115-957).")
    return None


# --- YAML I/O (verbatim from wm.py read_yaml / write_yaml) -----------------

def _read_yaml(path: Path) -> dict:
    """Mirror of wm.py read_yaml, incl. the  all-null detective:
    a non-empty file of pure 0x00 bytes is the NTFS
    metadata-journaled-but-data-not-flushed crash signature — WARN and read
    as empty (the loop's Phase -1 all-slots-null wm-init path self-heals)."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if raw and not raw.strip(b"\x00"):
        print(f"[wm_write] WARN: {path} is all-null content ({len(raw)} bytes) — "
              f"post-crash corruption signature (g-001-44); treating as empty.",
              file=sys.stderr)
        return {}
    data = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    return data if data is not None else {}


def _write_wm(path: Path, data: dict) -> None:
    """Verbatim wm.py write_yaml: default Dumper, atomic tmp+replace.

    DELIBERATELY a raw local write — do NOT route this through the backend
    (_atomic_write_with_fallback / get_backend().atomic_write), even though
    working-memory.yaml maps under the "agents" S3 root and so COULD be PUT to
    S3 directly. Excluded from the #38 own-cloud RMW conflict-retry on purpose
    (audited 2026-06-02):

      1. Per-agent single-writer. The live DDB runner claim pins each agent to
         one machine and _wm_lock() serialises that machine's own threads, so there
         is no concurrent multi-machine writer to conflict with. #38's
         conflict-retry targets SHARED world/meta files written by every agent;
         working memory is not one of them.
      2. Hot path. WM is read-modify-written many times per loop iteration
         (every slot set/append). An immediate S3 PUT per write would put S3
         latency on the system's hottest write path for a re-derivable file.
      3. Tier-model integrity. WM reaches S3 via the owncloud SWEEP per its
         session-manifest continuity tier (with the sweep's own conflict
         handling), and pull_continuity restores it on a machine move. An
         endpoint-side PUT would bypass that tier decision.

    The repo cache is off OneDrive, so the bare tmp.replace needs no os.replace
    retry/fallback here. If WM ever becomes shared multi-writer, that is a tier
    change to reconsider — not a one-line write-path swap."""
    assert_not_cruft(path.parent, "mkdir (wm_write)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        # : fsync before rename — a bare tmp.replace survives a crash
        # in metadata only (NTFS all-0x00 signature). Local durability concern;
        # the raw-local-write rationale above is unaffected.
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _read_config(ctx) -> dict:
    return _read_yaml(_config_path(ctx))


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# --- Slot resolution + meta (verbatim from wm.py) --------------------------

def _default_wm_data(ctx) -> dict:
    config = _read_config(ctx)
    wm_config = config.get("working_memory", {})
    slot_types = wm_config.get("slot_types", DEFAULT_SLOT_TYPES)
    slots = {}
    slot_meta = {}
    for st in slot_types:
        if st in ARRAY_SLOTS:
            slots[st] = []
        elif st in MAP_SLOTS:
            slots[st] = dict(MAP_SLOTS[st])
        else:
            slots[st] = None
        slot_meta[st] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    return {
        "encoding_queue": [],
        "session_id": None,
        "session_start": None,
        "goals_completed_this_session": [],
        "aspiration_touched_last": "",
        "last_goal_category": "",
        "slots": slots,
        "slot_meta": slot_meta,
    }


def _resolve_slot(data, slot_path):
    parts = slot_path.split(".")
    root_key = parts[0]
    if root_key in TOP_LEVEL_KEYS:
        current = data
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None, None, True
        return current, parts[-1], True
    else:
        slots = data.get("slots", {})
        if len(parts) == 1:
            return slots, root_key, False
        current = slots
        for part in parts[:-1]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, dict):
                current[part] = {}
                current = current[part]
            else:
                return None, None, False
        return current, parts[-1], False


def _get_slot_meta(data, slot_name):
    meta = data.setdefault("slot_meta", {})
    root = slot_name.split(".")[0]
    if root not in meta:
        meta[root] = {"updated_at": None, "accessed_at": None, "update_count": 0}
    return meta[root]


def _update_modified(data, slot_name):
    m = _get_slot_meta(data, slot_name)
    m["updated_at"] = _now_iso()
    m["update_count"] = m.get("update_count", 0) + 1


def _get_pruning_config(config):
    defaults = {
        "stale_threshold_minutes": 30,
        "evict_threshold_minutes": 120,
        "array_limits": {
            "encoding_queue": 20, "sensory_buffer": 20, "micro_hypotheses": 30,
            "knowledge_debt": 15, "known_blockers": 10, "recent_violations": 5,
        },
        "item_stale_minutes": {
            "micro_hypotheses": 180, "sensory_buffer": 60, "ephemeral_observation": 60,
        },
        "protected_slots": ["known_blockers", "knowledge_debt"],
    }
    return config.get("working_memory_pruning", defaults)


# TWIN of core/scripts/wm.py::_normalize_spark_capture_entry (,
# guard-742 twin discipline — this is the LIVE daemon path; the CLI copy is
# the fallback, so a fix applied to only one of them is inert in production).
# Rationale, measurement and the guard-1565/guard-3970 reasoning for why this
# normalizes at the WRITER instead of adding a reader-side fallback chain live
# in the wm.py copy — read it there before changing either.
_SPARK_CAPTURE_META_KEYS = frozenset({
    "goal_id", "category", "load_bearing", "sq_trigger", "_item_ts",
})
_SPARK_CAPTURE_MIN_CONTENT = 40


def _normalize_spark_capture_entry(item):
    """Promote an improvised content key into `observation`; return that key
    or None. Normalizes rather than raising — a worker mid-close cannot
    recover from a rejected append, and losing the observation is the exact
    failure this prevents."""
    if not isinstance(item, dict):
        return None
    if str(item.get("observation") or "").strip():
        return None
    best_key, best_val = None, ""
    for k, v in item.items():
        if k == "observation" or k in _SPARK_CAPTURE_META_KEYS:
            continue
        if isinstance(v, str) and len(v.strip()) > len(best_val):
            best_key, best_val = k, v.strip()
    if best_key is None or len(best_val) < _SPARK_CAPTURE_MIN_CONTENT:
        return None
    item["observation"] = best_val
    item["observation_normalized_from"] = best_key
    return best_key


def _validate_knowledge_debt_entry(ctx, item) -> None:
    """wm.py _validate_knowledge_debt_entry — raises ValueError (caller -> 400)."""
    priority = item.get("priority")
    node_key = item.get("node_key")
    reason = item.get("reason")
    if priority == "housekeeping":
        if not reason:
            raise ValueError("knowledge_debt housekeeping entry requires 'reason'")
        return
    if node_key is None:
        if not reason:
            raise ValueError("knowledge_debt entry with node_key=null requires 'reason'")
        return
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError(
            f"knowledge_debt node_key must be non-empty string or null, got {node_key!r}")
    tree_path = _tree_path(ctx)
    from storage_backend import get_backend
    get_backend().ensure_local(tree_path)  # own-cloud read-path fix: materialize S3-only file before local read; no-op on LocalBackend and out-of-root paths
    try:
        with open(tree_path, encoding="utf-8") as f:
            tree = yaml.safe_load(f) or {}
    except OSError as e:
        raise ValueError(f"cannot read {tree_path} for knowledge_debt validation: {e}")
    nodes = tree.get("nodes", {})
    if node_key not in nodes:
        raise ValueError(
            f"knowledge_debt node_key '{node_key}' does not resolve to a tree node "
            f"(valid: real node_key, priority='housekeeping'+reason, or node_key=null+reason)")


def _loop_state_merge_check(ctx, value, on_disk, override):
    """Load + run loop-state-merge-gate.check(). Fail-open (wm.py:441-444)."""
    try:
        gate_path = _gate_path(ctx)
        spec = importlib.util.spec_from_file_location("loop_state_merge_gate", gate_path)
        gate_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_mod)
        return gate_mod.check(value, on_disk, override=override)
    except (ImportError, OSError, AttributeError):
        return {"would_block": False, "reason": "gate load failed", "missing_subkeys": []}


def _carrier_mod(ctx):
    """core/scripts/body_capture_carrier.py, or None ().

    Lazily file-loaded rather than imported, for the same reason
    `_loop_state_merge_check` above does it: this package's relative imports
    make a top-level `import` of a core/scripts module fail. Cached in
    sys.modules by the module itself, so repeated appends pay a dict lookup.

    Returning None on any failure is the contract — the carrier is an
    accelerator in front of a merge that happens anyway, so a missing helper
    must degrade to today's behaviour rather than fail a WM write.
    """
    try:
        mod_path = (ctx.paths.project_root / "core" / "scripts"
                    / "body_capture_carrier.py")
        cached = sys.modules.get("body_capture_carrier")
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(
            "body_capture_carrier", mod_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["body_capture_carrier"] = mod
        spec.loader.exec_module(mod)
        return mod
    except (ImportError, OSError, AttributeError):
        return None


def _wm_lock(ctx):
    return file_locks.locked(_wm_path(ctx), stale_seconds=10)


# ---------------------------------------------------------------------------
# POST /v1/wm/set
# ---------------------------------------------------------------------------

def set_slot(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/set?slot=&override_merge_gate=  body: value (JSON or scalar)."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    slot = (ctx.query.get("slot") or "").strip()
    if not slot:
        return Response.error(400, "missing_param", "query parameter 'slot' required")

    raw = (ctx.body or b"").decode("utf-8").strip()
    if not raw:
        return Response.error(400, "empty_body", "value required in request body")

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        if raw == "null":
            value = None
        elif raw == "true":
            value = True
        elif raw == "false":
            value = False
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw

    if slot in STRUCTURED_DICT_SLOTS and value is not None and not isinstance(value, dict):
        return Response.error(
            400, "structured_dict_required",
            f"structured-dict slot '{slot}' refuses non-dict-or-null write "
            f"(got {type(value).__name__})")
    if slot in LIST_ROW_SLOTS and value is not None and not isinstance(value, list):
        return Response.error(
            400, "not_a_list_value",
            f"'{slot}' holds a LIST of hand-off rows; a {type(value).__name__} there is "
            f"corruption every reader drops. Append a row: echo '<json-row>' | "
            f"wm-append.sh {slot}; reset it: printf '%s' '[]' | wm-set.sh {slot}; "
            f"a timestamp belongs in a last_* slot")

    override = ctx.query.get("override_merge_gate")

    try:
        with _wm_lock(ctx):
            if slot == "loop_state":
                # : optimistic-concurrency on
                # slot_meta.loop_state.update_count closes the stale-lock-steal
                # race ( mechanism B). The two CLI writers
                # (loop-state-bump-counters.py, recurring-loop-state-mutate.py)
                # use the SAME _fileops.loop_state_cas_retry helper. If a >10s
                # stall lets a peer stale-break this lock and write, the token
                # re-read catches it and the cycle re-reads fresh + re-runs the
                # merge-gate + set on the peer's landed loop_state. The
                # byte-compat _write_wm (default Dumper) is preserved unchanged.
                from _fileops import loop_state_cas_retry

                err_box = {}

                def _read():
                    data = _read_yaml(_wm_path(ctx))
                    return data if data else _default_wm_data(ctx)  #  self-heal

                def _mutate(data):
                    # Returns True to commit; False (with err_box set) to abort
                    # the write and surface an error response to the caller.
                    parent, key, is_top = _resolve_slot(data, slot)
                    if parent is None:
                        err_box["resp"] = Response.error(
                            400, "unresolvable_slot",
                            f"cannot resolve path '{slot}'")
                        return False
                    on_disk = parent.get(key)
                    gate = _loop_state_merge_check(ctx, value, on_disk, override)
                    if gate.get("would_block"):
                        err_box["resp"] = Response.error(
                            400, "merge_gate_blocked", gate.get("reason", ""))
                        return False
                    # : write the preserved value when the gate floored
                    # stale-lower monotonic counters. CAS re-runs this _mutate on a
                    # stale-steal with FRESH on_disk, so the re-run gate sees the
                    # peer's higher counter and floors `value` to it — the
                    # Mechanism C backstop for the daemon full-slot path (CAS alone
                    # retries the same value; it does not floor it).
                    write_value = (
                        gate.get("preserved_value", value)
                        if gate.get("counters_preserved") else value
                    )
                    parent[key] = write_value
                    # loop_state is never a TOP_LEVEL_KEYS slot, so meta always
                    # advances (guard-540) — update_count is the CAS token.
                    _update_modified(data, slot)
                    return True

                def _write(data):
                    _write_wm(_wm_path(ctx), data)

                loop_state_cas_retry(_read, _mutate, _write, slot="loop_state")
                if err_box:
                    return err_box["resp"]
            else:
                data = _read_yaml(_wm_path(ctx))
                if not data:
                    data = _default_wm_data(ctx)  #  self-heal (set only)
                parent, key, is_top = _resolve_slot(data, slot)
                if parent is None:
                    return Response.error(400, "unresolvable_slot",
                                          f"cannot resolve path '{slot}'")
                parent[key] = value
                if not is_top:
                    _update_modified(data, slot)
                _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "slot": slot})


# ---------------------------------------------------------------------------
# POST /v1/wm/append
# ---------------------------------------------------------------------------

def append_slot(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/append?slot=  body: JSON item."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    slot = (ctx.query.get("slot") or "").strip()
    if not slot:
        return Response.error(400, "missing_param", "query parameter 'slot' required")

    raw = (ctx.body or b"").decode("utf-8").strip()
    if not raw:
        return Response.error(400, "empty_body", "JSON item required in request body")
    try:
        item = json.loads(raw)
    except json.JSONDecodeError as e:
        return Response.error(400, "invalid_body", f"body must be JSON: {e}")

    if isinstance(item, dict):
        item["_item_ts"] = _now_iso()

    root_slot_for_validation = slot.split(".")[0]

    # : a capture lane takes ONE OBJECT PER APPEND. Any other
    # top-level JSON shape is refused here, at the write, because every
    # downstream consumer drops it SILENTLY: worker_retrospective.index_captures
    # does `if not isinstance(entry, dict): continue` BEFORE bucketing by
    # goal_id, so a non-dict never enters a goal's bucket and therefore never
    # reaches the per-goal accounting that would have surfaced it (no_prose_key,
    # the rc=-1 all-malformed path, _slot_goal_ids). All three of those are real
    # guard-4044 instrumentation; they simply sit downstream of the drop.
    # Measured 2026-08-27 (alpha, cc-04): encoding_capture held 1091 entries, 7
    # of them non-dict -- 5 single-element ARRAYS wrapping a perfectly good dict
    # ( x3, , ) and 2 bare STRINGS, one of which was a
    # 4233-char narrative for . Every one carried a joinable goal_id or
    # real prose; none was ever counted.
    #
    # The refusal names EVERY shape it now catches, not just the array that
    # motivated it (guard-2680) -- the string case is in the measured data and a
    # list-only check would have left it open. Container type is not element
    # shape (guard-4813). Refused at rc=400 rather than coerced: `[{...}]` is
    # unambiguous to unwrap but `[a, b]` is two entries, and silently picking one
    # is the same class of silent-success this refusal exists to end.
    if root_slot_for_validation in CAPTURE_SLOTS and not isinstance(item, dict):
        return Response.error(
            400, "validation_failed",
            f"capture slot {root_slot_for_validation!r} takes one JSON OBJECT "
            f"per append; got a top-level {type(item).__name__}. Every non-dict "
            f"entry is dropped silently by worker_retrospective.index_captures "
            f"before it is bucketed by goal_id, so it is never counted and no "
            f"consumer reads it. Pass one object per append"
            + (" (send each element of the array as its own append)"
               if isinstance(item, list) else
               " (wrap the text in an object, e.g. "
               '{\"goal_id\": \"...\", \"observation\": \"...\"})'
               if isinstance(item, str) else "")
            + ".")

    if root_slot_for_validation == "knowledge_debt" and isinstance(item, dict):
        try:
            _validate_knowledge_debt_entry(ctx, item)
        except ValueError as e:
            return Response.error(400, "validation_failed", str(e))

    # : promote improvised content keys into `observation` BEFORE the
    # entry is persisted, so every downstream reader sees the canonical shape.
    if root_slot_for_validation == "spark_capture" and isinstance(item, dict):
        _normalize_spark_capture_entry(item)

    # : initialized OUTSIDE the try so every return path can report it.
    # A name defined only inside the array branch would NameError on a scalar
    # append — a failure landing in the one place nobody is watching.
    _evicted = 0
    # : same reason, and the same trap — the post-lock push below reads
    # this on EVERY append, including the scalar and error paths that never
    # reach the branch that sets it.
    _carrier_path = None
    # : same NameError trap as the two above — the response builder reads
    # both on EVERY append, including the scalar and upsert paths that never reach
    # the eviction branch.
    # : same trap, third instance. `placement` reports WHICH of the two
    # physical locations this slot occupies — the YAML top level, or under the
    # `slots:` mapping. It is DERIVED from _resolve_slot's own routing decision a
    # few lines below, never from a restated copy of TOP_LEVEL_KEYS: a second copy
    # of that set would drift, and the drift would be invisible because both
    # answers still look like valid placements. None means the resolver never ran
    # (an early error return) — a different fact from either placement.
    _placement = None

    try:
        with _wm_lock(ctx):
            data = _read_yaml(_wm_path(ctx))
            if not data:
                return Response.error(400, "not_initialized",
                                      "working memory not initialized (run wm init)")
            parent, key, is_top = _resolve_slot(data, slot)
            # : capture the routing decision at the one place it is
            # made. `is_top` IS the answer the caller cannot otherwise see.
            _placement = "top-level" if is_top else "slots"
            if parent is None:
                return Response.error(400, "unresolvable_slot",
                                      f"cannot resolve path '{slot}'")
            arr = parent.get(key)
            # : refuse BEFORE minting the lane below. THIS is the LIVE
            # copy; the wm.py twin exists for parity (guard-742).
            _refusal = _unknown_lane_refusal(slot, arr is not None)
            if _refusal:
                return Response.error(400, "unknown_slot", _refusal)
            if arr is None:
                parent[key] = []
                arr = parent[key]
            # SELF-HEAL the int-in-LIST-slot collision (2026-08-16, worker-loop
            # Phase 4b outage). The TOP-LEVEL `goals_completed_this_session` is
            # canonically a LIST of hand-off rows (default [] in _default_wm);
            # the same NAME is an int COUNTER under loop_state and an int
            # PARAMETER to consolidate. A writer that collapses the two leaves
            # an int here, and an int carries no rows — so it is ALWAYS
            # corruption, never data. Measured on 3 of 3 forked Bodies checked
            # (alpha cc-07, alpha+foxtrot cc-08): every Phase 4b append was
            # refused `not_a_list`, body-merge's list/int type-mismatch branch
            # then dropped the Body's contribution silently, merged_goal_ids
            # stayed [], and the retrospective lane wired the same day never
            # fired (msg-20260816-194710-alpha-5109). One stray int disabled the
            # lane for the Body's whole life. Heal to [] and CONTINUE, loudly:
            # the refusal protected nothing (the int is not a value anyone
            # reads), while the heal restores the lane on the next append.
            # Widened from int/float to ANY scalar 2026-08-30: a STRING got in
            # the same way (`wm-set.sh goals_completed_this_session '<timestamp>'`,
            # 2026-08-28, live deployment), 36 of 51 cloned sessions inherited
            # it, and the int-only heal left every worker close refused for 39
            # hours — a string carries no rows either. Scoped to LIST_ROW_SLOTS;
            # every other slot's type mismatch still refuses. TWIN of
            # core/scripts/wm.py cmd_append — keep in sync; this daemon copy is
            # the LIVE path (guard-742).
            _healed_scalar = None
            if is_top and key in LIST_ROW_SLOTS and arr is not None and not isinstance(arr, list):
                _healed_scalar = arr
                parent[key] = []
                arr = parent[key]
            if not isinstance(arr, list):
                return Response.error(400, "not_a_list",
                                      f"'{slot}' is {type(arr).__name__}, not a list")
            # knowledge_debt UPSERT-BY-node_key (, 2026-08-06).
            # TWIN of core/scripts/wm.py cmd_append — keep both in sync; this
            # daemon copy is the LIVE path (wm-append.sh is daemon-routed, so
            # the CLI edit alone changes nothing: guard-742). Measured before
            # the fix: 10 entries for 5 distinct node_keys across 3 scans,
            # limit 15. The reflect tree-lint re-flags the TOP 5 by
            # retrieval_count every maintain pass and that set is stable by
            # construction, so blind appends saturate the slot with re-records
            # and the FIFO eviction below then drops genuine debt from the
            # other writers within ~3 scans. Upsert (not skip) because the
            # newer scan carries fresher counts; _item_ts is preserved so a
            # node that keeps re-flagging cannot indefinitely renew its own
            # eviction priority over older, never-serviced debt.
            # SELF-HEALING: collapse ALL matches, not just the first — a
            # replace-first-and-break prevents NEW duplicates but never
            # converges a slot that ALREADY holds them. TWIN of core/scripts/
            # wm.py cmd_append; keep in sync.
            _upserted = False
            if root_slot_for_validation == "knowledge_debt" and isinstance(item, dict):
                _nk = item.get("node_key")
                if _nk:
                    _matches = [_i for _i, _e in enumerate(arr)
                                if isinstance(_e, dict) and _e.get("node_key") == _nk]
                    if _matches:
                        _oldest = min(
                            (arr[_i].get("_item_ts") for _i in _matches
                             if arr[_i].get("_item_ts")),
                            default=item.get("_item_ts"),
                        )
                        item["_item_ts"] = _oldest
                        arr[_matches[0]] = item
                        for _i in reversed(_matches[1:]):
                            arr.pop(_i)
                        _upserted = True
            if not _upserted:
                arr.append(item)
            # Enforce array limits.
            config = _read_config(ctx)
            pruning = _get_pruning_config(config)
            limits = pruning.get("array_limits", {})
            root_slot = slot.split(".")[0]
            limit = limits.get(root_slot)
            if limit and len(arr) > limit:
                # : evict UNFLAGGED before load-bearing. Byte-identical
                # mirror of wm.py::_eviction_sort_key — this is the LIVE copy
                # (wrappers are daemon-only), so the wm.py edit alone would be
                # inert at runtime (the  bug class). Rationale for the
                # ordering, and the 237-entry measurement behind it, live on the
                # wm.py definition; keep the two in sync.
                # : the entry THIS call just added must never be its
                # own eviction victim. MEASURED on the live append path (alpha
                # worker Body, cc-07, 2026-08-17) against spark_capture at cap
                # 50 with 50/50 load_bearing: an UNFLAGGED append returned rc=0
                # and the entry was simply ABSENT afterwards, while a FLAGGED
                # append in the same probe SURVIVED (that flagged control is
                # what proves the write path was live and the absence real).
                # Cause: the sort puts unflagged first and pop(0) takes it, so
                # in a lane saturated at 100% flagged the newcomer sorts to
                # index 0 and destroys itself. The lane was therefore
                # selectively deaf to exactly the entries an honest reporter
                # marks routine, and the saturation was self-reinforcing --
                # only flagged entries survived, so the rate stayed 100%, which
                # is how load_bearing became a constant and stopped carrying
                # the triage signal it exists for.
                # An append that is silently undone is not a cap; it is a write
                # failure that reports success. When the lane is 100% flagged
                # the key has no variance and cannot prioritise anything, so
                # falling through to the oldest peer is both the only remaining
                # order and the right one. Priority is otherwise untouched:
                # unflagged peers are still evicted before flagged ones.
                # TWIN of core/scripts/wm.py cmd_append -- keep in sync; this
                # daemon copy is the LIVE path (guard-742/547).
                # : the guard above is PER-CALL and therefore could not
                # bound the lane. It protects `item` only while THIS call runs;
                # on the next append the previous newcomer is an unprotected
                # unflagged peer that sorts first and is popped. N consecutive
                # unflagged appends into a saturated lane kept exactly ONE — 90%
                # loss at N=10, measured deterministically in , and
                # invisible to every single-call test. The fix is a PER-WINDOW
                # reserved floor: a property of the LANE, so it survives across
                # calls. Both protections stay; they are scoped to different
                # units (guard-4236) and neither substitutes for the other.
                # Above the floor the priority key is untouched. Below it the
                # oldest FLAGGED entry is evicted instead — the stated cost,
                # since a lane at 100% flagged has no variance left in the key.
                # : this stays INLINE on purpose. Extracting it behind a
                # helper was tried and reverted — test_capture_fast_lane.py
                # ::test_daemon_eviction_key_is_defined_and_actually_called
                # asserts `key=_eviction_sort_key` appears inside append_slot
                # precisely so a mirrored-but-uncalled helper cannot pass a
                # definition check while changing nothing at runtime (the
                #  class). One level of indirection defeats that
                # textual guard, and the guard is worth more than the tidiness.
                arr.sort(key=_eviction_sort_key)
                _floor = _unflagged_floor(limit)
                # Sorted (flag, ts) => unflagged are the PREFIX, so this count is
                # also the index of the oldest FLAGGED entry.
                _n_unflagged = sum(1 for x in arr if not _is_flagged(x))
                while len(arr) > limit:
                    _victim = (_n_unflagged
                               if (len(arr) - _n_unflagged) > limit - _floor
                               else 0)
                    if arr[_victim] is item and len(arr) > 1:
                        _victim = (_victim + 1 if _victim + 1 < len(arr)
                                   else _victim - 1)
                    if _victim < _n_unflagged:
                        _n_unflagged -= 1
                    arr.pop(_victim)
                    _evicted += 1
                # : this eviction used to be entirely silent — rc=0,
                # {"ok": true}, nothing recorded. Measured on one worker Body:
                # 215 capture entries destroyed (exp_capture 115, spark_capture
                # 86, hyp_capture 14), which is the ONLY channel a worker's
                # execution learning has, since it skips every reducer-only phase.
                #
                # The counter is a TOP-LEVEL key, NOT slot_meta, and that choice is
                # load-bearing rather than stylistic: body-merge.py merges slot_meta
                # REDUCER-WINS (only Body-only metas are added), so a counter there
                # is discarded at generalize-down — the same silent loss one layer
                # up. Top-level keys route through _merge_value, where a nested int
                # gets the 3-way-delta SUM, so counts aggregate across Bodies.
                _record_capture_evictions(data, root_slot, _evicted)
            if not is_top:
                _update_modified(data, slot)
            _write_wm(_wm_path(ctx), data)
            # : mirror a LOAD-BEARING capture into this Body's
            # session/-rooted carrier so capture_fast_lane can see it from
            # ANOTHER box. sessions/ is sync-excluded and machine-local, so
            # without this the lane can only ever reach same-box Bodies — the
            # one case it was not built for. TWIN of core/scripts/wm.py
            # cmd_append; this daemon copy is the LIVE path (wm-append.sh is
            # daemon-routed, so the wm.py edit alone is inert: guard-742).
            # Local append only, INSIDE the lock so carrier order matches WM
            # order; the store push happens after the lock is released.
            if (root_slot_for_validation in CAPTURE_SLOTS
                    and isinstance(item, dict) and item.get("load_bearing")):
                _cm = _carrier_mod(ctx)
                if _cm is not None:
                    _carrier_path = _cm.record_local(
                        _wm_path(ctx), root_slot_for_validation, item)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Push OUTSIDE the lock, deliberately: the WM lock is stale_seconds=10 and
    # this is a network round trip, so holding it here would let a slow store
    # look like a crashed writer and hand the lock to a second writer. A failed
    # push is self-healing — the next append re-pushes the WHOLE carrier.
    if _carrier_path is not None:
        _cm = _carrier_mod(ctx)
        if _cm is not None:
            _cm.push(_carrier_path)

    # : `evicted` is always present (0 on the common path) so a caller
    # can branch on it without a key-existence check, and so a non-zero value is
    # visible in the same breath as the write that caused it.
    # : `placement` is always present, for the same reason `evicted`
    # is — a caller must be able to branch on it without a key-existence
    # check, and the write that CAUSED a placement is the only moment the
    # answer is free. Measured both directions in one session: reading the
    # wrong level returns a clean, plausible 0 that is byte-identical to
    # "this slot is empty", so a caller who guesses wrong gets no error.
    out = {"ok": True, "slot": slot, "evicted": _evicted,
           "placement": _placement}
    if _healed_scalar is not None:
        # Surface the heal in the SAME response as the write, never silently:
        # a caller that reads `healed_from` non-null knows this Body's slot had
        # been collapsed to a scalar and that a writer upstream still needs
        # finding (test fixtures were the measured writer —  class;
        # a Body's wm-set of a timestamp was the 2026-08-28 one).
        out["healed_from"] = f"{type(_healed_scalar).__name__}:{_healed_scalar}"
        out["warning"] = (f"goals_completed_this_session was a {type(_healed_scalar).__name__} "
                          "(a counter's name or a last_*-style stamp written into the "
                          "top-level hand-off LIST); reset to [] before appending — find the writer")
    return Response.json(out)


# ---------------------------------------------------------------------------
# POST /v1/wm/clear
# ---------------------------------------------------------------------------

def clear_slot(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/clear?slot= — null a scalar or [] an array slot."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    slot = (ctx.query.get("slot") or "").strip()
    if not slot:
        return Response.error(400, "missing_param", "query parameter 'slot' required")

    try:
        with _wm_lock(ctx):
            data = _read_yaml(_wm_path(ctx))
            if not data:
                return Response.error(400, "not_initialized",
                                      "working memory not initialized")
            parent, key, is_top = _resolve_slot(data, slot)
            if parent is None:
                return Response.error(400, "unresolvable_slot",
                                      f"cannot resolve path '{slot}'")
            current_val = parent.get(key) if isinstance(parent, dict) else None
            root_slot = slot.split(".")[0]
            if isinstance(current_val, list) or root_slot in ARRAY_SLOTS:
                parent[key] = []
            else:
                parent[key] = None
            if not is_top:
                _update_modified(data, slot)
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "slot": slot})


# ---------------------------------------------------------------------------
# POST /v1/wm/drain-goals
# ---------------------------------------------------------------------------

def drain_goals(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/drain-goals?slot=  body: JSON array of goal ids.

    Subtractive drain of a capture lane: removes every entry whose `goal_id` is
    in the posted set and keeps everything else. g-115-7366 — `exp_capture` and
    `encoding_capture` had readers and NO drain site anywhere in the tree. The
    cap then decides only the SYMPTOM: a capped lane parks at cap and evicts its
    oldest entry silently on every append, an uncapped one grows without bound
    (measured 2026-08-24: spark 50/50, exp 20/20, hyp 10/10, encoding 931).

    WHY A HANDLER AND NOT A READ-FILTER-`set` IN THE CALLER (guard-3881). A
    caller that reads the slot, decides outside the lock, then POSTs the whole
    surviving list to /v1/wm/set performs a full-slot OVERWRITE of a stale
    snapshot: anything appended between its read and its write is destroyed.
    guard-3881 requires the candidate predicate to be re-asserted INSIDE the
    write lock, and guard-364's "hold the lock across the read-modify-write" is
    explicitly not enough. This file already carries the proof, in `set_slot`'s
    loop_state branch, whose CAS exists because "a >10s stall lets a peer
    stale-break this lock and write" — the WM lock is stale-breakable, so even a
    correctly-held lock does not make an overwrite safe.

    Applying the filter HERE makes the operation a genuine SUBTRACTION rather
    than an overwrite, which is what removes the hazard rather than narrowing
    its window: an entry this handler never saw is either not in the drained set
    (and survives, because the surviving list is derived from data read under
    THIS lock) or is in it (and removing it is correct anyway). No CAS is needed
    because the operation is idempotent and order-independent — which the
    loop_state counter merge is not.

    Scoped to CAPTURE_SLOTS on purpose: this is the only destructive goal-keyed
    primitive in the WM surface, and a lane outside the capture set should widen
    it deliberately rather than inherit it.

    Deliberately has NO wm.py CLI twin, unlike the other eight handlers. The
    wrappers are daemon-only (`.claude/rules/no-python-cli-fallback.md`), so a
    twin would have zero callers, and `core/config/verification-checklist.md`
    pins wm.py at nine subcommands. The asymmetry is a choice, not drift.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    slot = (ctx.query.get("slot") or "").strip()
    if not slot:
        return Response.error(400, "missing_param", "query parameter 'slot' required")
    if slot not in CAPTURE_SLOTS:
        return Response.error(
            400, "slot_not_drainable",
            f"drain-goals is scoped to capture lanes {list(CAPTURE_SLOTS)}; "
            f"refused '{slot}'")

    raw = (ctx.body or b"").decode("utf-8").strip()
    if not raw:
        return Response.error(400, "empty_body", "JSON array of goal ids required")
    try:
        posted = json.loads(raw)
    except json.JSONDecodeError as e:
        return Response.error(400, "invalid_json", str(e))
    if not isinstance(posted, list) or not posted:
        return Response.error(
            400, "empty_goal_ids",
            "a non-empty JSON array of goal ids is required — a drain with no "
            "ids is a caller defect, not a silent no-op")
    ids = {g for g in posted if isinstance(g, str) and g}
    if not ids:
        return Response.error(
            400, "empty_goal_ids",
            "no usable goal ids in the posted array (expected non-empty strings)")

    try:
        with _wm_lock(ctx):
            data = _read_yaml(_wm_path(ctx))
            if not data:
                return Response.error(400, "not_initialized",
                                      "working memory not initialized")
            parent, key, _is_top = _resolve_slot(data, slot)
            if parent is None:
                return Response.error(400, "unresolvable_slot",
                                      f"cannot resolve path '{slot}'")
            current = parent.get(key) if isinstance(parent, dict) else None
            if current is None:
                return Response.json({"ok": True, "slot": slot,
                                      "removed": 0, "kept": 0})
            if not isinstance(current, list):
                return Response.error(
                    400, "slot_not_a_list",
                    f"slot '{slot}' holds {type(current).__name__}, not a list")
            # Non-dict entries, and entries carrying no goal_id, are KEPT: an
            # entry the classifier cannot classify must not be destroyed by it.
            keep = [e for e in current
                    if not (isinstance(e, dict) and e.get("goal_id") in ids)]
            removed = len(current) - len(keep)
            if not removed:
                return Response.json({"ok": True, "slot": slot,
                                      "removed": 0, "kept": len(keep)})
            parent[key] = keep
            # Capture lanes are never TOP_LEVEL_KEYS, so meta always advances
            # (guard-540). wm-prune reads slot_meta.updated_at for staleness; a
            # drain that left it untouched would age the lane from its last
            # APPEND and could evict survivors it just spared.
            _update_modified(data, slot)
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "slot": slot,
                          "removed": removed, "kept": len(keep)})


# ---------------------------------------------------------------------------
# POST /v1/wm/prune
# ---------------------------------------------------------------------------

def prune(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/prune?dry_run=1 — mid-session pruning by config thresholds."""
    from ..server import Response
    from ._jsonl_common import flag as _flag

    err = _require_agent_header(ctx)
    if err:
        return err
    dry_run = _flag(ctx.query, "dry_run")

    try:
        with _wm_lock(ctx):
            data = _read_yaml(_wm_path(ctx))
            if not data:
                return Response.error(400, "not_initialized",
                                      "working memory not initialized")
            config = _read_config(ctx)
            pruning = _get_pruning_config(config)
            now = datetime.now()
            meta = data.get("slot_meta", {})
            slots = data.get("slots", {})
            protected = set(pruning.get("protected_slots", []))
            report = {"pruned_items": [], "stale_slots": [], "evicted_slots": []}
            stale_mins = pruning.get("stale_threshold_minutes", 30)
            evict_mins = pruning.get("evict_threshold_minutes", 120)
            item_stale = pruning.get("item_stale_minutes", {})
            limits = pruning.get("array_limits", {})

            for slot_name, slot_val in list(slots.items()):
                m = meta.get(slot_name, {})
                updated_str = m.get("updated_at")
                mins_since = None
                if updated_str:
                    try:
                        dt = datetime.fromisoformat(updated_str)
                        mins_since = (now - dt).total_seconds() / 60
                    except (ValueError, TypeError):
                        pass
                if mins_since is not None and mins_since > stale_mins:
                    report["stale_slots"].append(
                        {"slot": slot_name, "minutes_stale": int(mins_since)})
                if (slot_name not in protected
                        and slot_name not in ARRAY_SLOTS
                        and slot_name not in MAP_SLOTS
                        and not _is_cadence_tracker(slot_name)
                        and slot_val is not None
                        and mins_since is not None
                        and mins_since > evict_mins):
                    if not dry_run:
                        slots[slot_name] = None
                        _update_modified(data, slot_name)
                    report["evicted_slots"].append(
                        {"slot": slot_name, "minutes_stale": int(mins_since)})
                if isinstance(slot_val, list) and slot_name in item_stale:
                    max_age = item_stale[slot_name]
                    to_remove = []
                    for i, item in enumerate(slot_val):
                        if not isinstance(item, dict):
                            continue
                        ts = item.get("_item_ts")
                        if not ts:
                            continue
                        try:
                            dt = datetime.fromisoformat(ts)
                            age_mins = (now - dt).total_seconds() / 60
                        except (ValueError, TypeError):
                            continue
                        if age_mins > max_age:
                            if slot_name in protected:
                                if slot_name == "known_blockers" and item.get("resolution") is not None:
                                    to_remove.append(i)
                                elif slot_name == "knowledge_debt" and item.get("resolved"):
                                    to_remove.append(i)
                            else:
                                if slot_name == "micro_hypotheses" and item.get("outcome") is not None:
                                    to_remove.append(i)
                                elif slot_name != "micro_hypotheses":
                                    to_remove.append(i)
                    for i in reversed(to_remove):
                        removed = slot_val.pop(i)
                        report["pruned_items"].append({
                            "slot": slot_name,
                            "item_summary": str(removed.get("claim", removed.get("reason", removed.get("observation", "?"))))[:80],
                            "reason": "item_stale",
                        })
                    if to_remove and not dry_run:
                        _update_modified(data, slot_name)
                if isinstance(slot_val, list) and slot_name in limits:
                    limit = limits[slot_name]
                    if len(slot_val) > limit:
                        # : DELIBERATELY pure FIFO — do NOT "unify" this
                        # with _enforce_slot_limit. That was tried and MEASURED
                        # here, and it is strictly worse for the payloads this
                        # lane exists to protect. The shared policy evicts
                        # UNFLAGGED first, but for a Body that has not CLOSED
                        # `load_bearing` is the ONLY delivery channel (the fast
                        # lane mirrors flagged entries out; unflagged ones are
                        # reachable only at generalize-down), so unflagged ==
                        # UNDELIVERED and flagged == a redundant second copy.
                        # At limit=10, floor=2, counting undelivered entries
                        # lost (FIFO vs shared policy): 8F+4U 0 vs 2; 4F+8U
                        # 0 vs 2; 2F+10U 0 vs 2; 10F+2U 0 vs 0; unflagged-older
                        # 2 vs 2. Never better, worse in 4 of 5. FIFO is
                        # flag-NEUTRAL and evicts by age alone, which protects
                        # the newest (still-undelivered) captures for free.
                        _n = 0
                        slot_val.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
                        while len(slot_val) > limit:
                            removed = slot_val.pop(0)
                            _n += 1
                            report["pruned_items"].append({
                                "slot": slot_name,
                                "item_summary": str(removed.get("claim", removed.get("reason", "?")))[:80] if isinstance(removed, dict) else "?",
                                "reason": "array_limit",
                            })
                        # Persist the tally. This path recorded evictions ONLY
                        # into the transient `report` above, so everything it
                        # destroyed was invisible to capture_evictions — the
                        # counter built to make exactly this loss measurable.
                        if not dry_run:
                            _record_capture_evictions(data, slot_name, _n)
                            _update_modified(data, slot_name)

            eq = data.get("encoding_queue", [])
            eq_limit = limits.get("encoding_queue", 20)
            if isinstance(eq, list) and len(eq) > eq_limit:
                eq.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
                while len(eq) > eq_limit:
                    removed = eq.pop(0)
                    report["pruned_items"].append({
                        "slot": "encoding_queue",
                        "item_summary": str(removed.get("observation", "?"))[:80] if isinstance(removed, dict) else "?",
                        "reason": "array_limit",
                    })

            if not dry_run:
                _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "dry_run": dry_run, "report": report})


# ---------------------------------------------------------------------------
# POST /v1/wm/init
# ---------------------------------------------------------------------------

def init(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/init — initialize working memory from template."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    try:
        data = _default_wm_data(ctx)
        slot_count = len(data.get("slots", {}))
        with _wm_lock(ctx):
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return Response.json({"ok": True, "slots": slot_count})


# ---------------------------------------------------------------------------
# POST /v1/wm/reset
# ---------------------------------------------------------------------------

def reset(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/reset — reset to template, preserve identity + cadence slots."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    try:
        data = _default_wm_data(ctx)
        with _wm_lock(ctx):
            existing = _read_yaml(_wm_path(ctx))
            preserved = []
            for k in SESSION_IDENTITY_FIELDS:
                v = existing.get(k)
                if v is not None:
                    data[k] = v
                    preserved.append(k)
            # : top-level non-identity survivors. Reported separately
            # from `preserved` so the response never implies these are identity
            # fields — clear-identity must keep ignoring them.
            preserved_top_level = []
            for k in RESET_SURVIVING_TOP_LEVEL:
                v = existing.get(k)
                if v is not None:
                    data[k] = v
                    preserved_top_level.append(k)
            existing_slots = existing.get("slots", {})
            existing_meta = existing.get("slot_meta", {})
            cadence_preserved = []
            surviving_preserved = []
            for slot_name, slot_val in existing_slots.items():
                is_cadence = _is_cadence_tracker(slot_name)
                if (is_cadence or slot_name in RESET_SURVIVING_SLOTS) and slot_val is not None:
                    data["slots"][slot_name] = slot_val
                    if slot_name in existing_meta:
                        data["slot_meta"][slot_name] = existing_meta[slot_name]
                    (cadence_preserved if is_cadence else surviving_preserved).append(slot_name)
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return Response.json({"ok": True, "preserved_identity": sorted(preserved),
                          "preserved_top_level": sorted(preserved_top_level),
                          "preserved_cadence": len(cadence_preserved),
                          "preserved_surviving": sorted(surviving_preserved)})


# ---------------------------------------------------------------------------
# POST /v1/wm/clear-identity
# ---------------------------------------------------------------------------

def clear_identity(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/wm/clear-identity — null SESSION_IDENTITY_FIELDS."""
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
    try:
        with _wm_lock(ctx):
            data = _read_yaml(_wm_path(ctx))
            if not data:
                return Response.json({"ok": True, "cleared": [],
                                      "detail": "not initialized; nothing to clear"})
            cleared = []
            for k in SESSION_IDENTITY_FIELDS:
                if data.get(k) is not None:
                    data[k] = None
                    cleared.append(k)
            if not cleared:
                return Response.json({"ok": True, "cleared": [],
                                      "detail": "already clear"})
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return Response.json({"ok": True, "cleared": sorted(cleared)})


# ---------------------------------------------------------------------------
# GET /v1/wm/ages
# ---------------------------------------------------------------------------

def ages(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/wm/ages — slot age report (read-only)."""
    from ..server import Response

    data = _read_yaml(_wm_path(ctx))
    if not data:
        return Response.json({})
    now = datetime.now()
    meta = data.get("slot_meta", {})
    slots = data.get("slots", {})
    result = {}
    for slot_name in slots:
        m = meta.get(slot_name, {})
        updated = m.get("updated_at")
        accessed = m.get("accessed_at")
        update_count = m.get("update_count", 0)
        mins_since_update = None
        mins_since_access = None
        if updated:
            try:
                mins_since_update = int((now - datetime.fromisoformat(updated)).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
        if accessed:
            try:
                mins_since_access = int((now - datetime.fromisoformat(accessed)).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
        slot_val = slots.get(slot_name)
        item_count = len(slot_val) if isinstance(slot_val, list) else None
        result[slot_name] = {
            "minutes_since_update": mins_since_update,
            "minutes_since_access": mins_since_access,
            "update_count": update_count,
            "item_count": item_count,
        }
    return Response.json(result)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/wm/set")] = set_slot
    routes[("POST", "/v1/wm/append")] = append_slot
    routes[("POST", "/v1/wm/clear")] = clear_slot
    routes[("POST", "/v1/wm/drain-goals")] = drain_goals
    routes[("POST", "/v1/wm/prune")] = prune
    routes[("POST", "/v1/wm/init")] = init
    routes[("POST", "/v1/wm/reset")] = reset
    routes[("POST", "/v1/wm/clear-identity")] = clear_identity
    routes[("GET", "/v1/wm/ages")] = ages
