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
    "last_goal_category",
}
SESSION_IDENTITY_FIELDS = {"session_start"}
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
                         "hyp_capture", "encoding_capture", "capture_consumed_hashes"}

#  — mirror of wm.py. Ordered tuple, deliberately NOT ARRAY_SLOTS: that
# set contains non-capture members, so it is the wrong thing to iterate when the
# question is "what did the worker capture".
CAPTURE_SLOTS = ("spark_capture", "exp_capture", "hyp_capture", "encoding_capture")


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
    if root_slot_for_validation == "knowledge_debt" and isinstance(item, dict):
        try:
            _validate_knowledge_debt_entry(ctx, item)
        except ValueError as e:
            return Response.error(400, "validation_failed", str(e))

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

    try:
        with _wm_lock(ctx):
            data = _read_yaml(_wm_path(ctx))
            if not data:
                return Response.error(400, "not_initialized",
                                      "working memory not initialized (run wm init)")
            parent, key, is_top = _resolve_slot(data, slot)
            if parent is None:
                return Response.error(400, "unresolvable_slot",
                                      f"cannot resolve path '{slot}'")
            arr = parent.get(key)
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
            # Scoped to this ONE slot + int/float; every other type mismatch
            # still refuses. TWIN of core/scripts/wm.py cmd_append — keep in
            # sync; this daemon copy is the LIVE path (guard-742).
            _healed_int = None
            if (is_top and key == "goals_completed_this_session"
                    and isinstance(arr, (int, float)) and not isinstance(arr, bool)):
                _healed_int = arr
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
                _ev = data.get("capture_evictions")
                if not isinstance(_ev, dict):
                    _ev = {}
                    data["capture_evictions"] = _ev
                _prev = _ev.get(root_slot)
                _ev[root_slot] = (_prev if isinstance(_prev, int) else 0) + _evicted
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
    out = {"ok": True, "slot": slot, "evicted": _evicted}
    if _healed_int is not None:
        # Surface the heal in the SAME response as the write, never silently:
        # a caller that reads `healed_from` non-null knows this Body's slot had
        # been collapsed to a counter and that a writer upstream still needs
        # finding (test fixtures were the measured writer —  class).
        out["healed_from"] = f"{type(_healed_int).__name__}:{_healed_int}"
        out["warning"] = ("goals_completed_this_session was an int (the loop_state "
                          "counter's name collided with the top-level hand-off LIST); "
                          "reset to [] before appending — find the writer")
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
                        slot_val.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
                        while len(slot_val) > limit:
                            removed = slot_val.pop(0)
                            report["pruned_items"].append({
                                "slot": slot_name,
                                "item_summary": str(removed.get("claim", removed.get("reason", "?")))[:80] if isinstance(removed, dict) else "?",
                                "reason": "array_limit",
                            })
                        if not dry_run:
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
    routes[("POST", "/v1/wm/prune")] = prune
    routes[("POST", "/v1/wm/init")] = init
    routes[("POST", "/v1/wm/reset")] = reset
    routes[("POST", "/v1/wm/clear-identity")] = clear_identity
    routes[("GET", "/v1/wm/ages")] = ages
