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
import re
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
}
MAP_SLOTS = {
    "active_context": {"summary": None, "experience_refs": [], "retrieval_manifest": None},
    "archived_context": {"summary": None, "experience_refs": []},
}
STRUCTURED_DICT_SLOTS = {"loop_state"}
CADENCE_TRACKER_PATTERNS = (
    re.compile(r"^last_.*_review$"),
    re.compile(r"^last_.*_at$"),
    re.compile(r"^last_.*_at_time$"),
    re.compile(r"^last_.*_tick$"),
    re.compile(r"^last_.*_check$"),
    re.compile(r"^last_.*_checkin$"),
    re.compile(r"^last_.*_scan$"),
    re.compile(r"^last_.*_fire$"),
)


def _is_cadence_tracker(slot_name: str) -> bool:
    return any(p.match(slot_name) for p in CADENCE_TRACKER_PATTERNS)


# --- Paths ----------------------------------------------------------------

def _wm_path(ctx) -> Path:
    # Per-Body WM routing (Phase 1A, ): route by the request's session
    # SID (X-Mind-Sid header). With no SID, or no body-manifest for it,
    # ctx.paths.wm_path returns the agent-wide WM (today's behavior) — so this
    # is backward-compatible and dormant until a 2nd Body exists.
    sid = (ctx.headers.get("x-ayoai-sid") or "").strip()
    return ctx.paths.wm_path(sid or None)


def _config_path(ctx) -> Path:
    return ctx.paths.project_root / "core" / "config" / "memory-pipeline.yaml"


def _tree_path(ctx) -> Path:
    return ctx.paths.world / "knowledge" / "tree" / "_tree.yaml"


def _gate_path(ctx) -> Path:
    return ctx.paths.project_root / "core" / "scripts" / "loop-state-merge-gate.py"


def _require_agent_header(ctx):
    from ..server import Response
    agent = (ctx.headers.get("x-ayoai-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for wm writes (g-115-957).")
    return None


# --- YAML I/O (verbatim from wm.py read_yaml / write_yaml) -----------------

def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _write_wm(path: Path, data: dict) -> None:
    """Verbatim wm.py write_yaml: default Dumper, atomic tmp+replace.

    DELIBERATELY a raw local write — do NOT route this through the backend
    (_atomic_write_with_fallback / get_backend().atomic_write), even though
    working-memory.yaml maps under the "agents" S3 root and so COULD be PUT to
    S3 directly. Excluded from the #38 own-cloud RMW conflict-retry on purpose
    (audited 2026-06-02):

      1. Per-agent single-writer. MACHINE_OWNED_AGENTS pins each agent to one
         machine and _wm_lock() serialises that machine's own threads, so there
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
                # 4: optimistic-concurrency on
                # slot_meta.loop_state.update_count closes the stale-lock-steal
                # race (9 mechanism B). The two CLI writers
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
                    # 8: write the preserved value when the gate floored
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
            if not isinstance(arr, list):
                return Response.error(400, "not_a_list",
                                      f"'{slot}' is {type(arr).__name__}, not a list")
            arr.append(item)
            # Enforce array limits.
            config = _read_config(ctx)
            pruning = _get_pruning_config(config)
            limits = pruning.get("array_limits", {})
            root_slot = slot.split(".")[0]
            limit = limits.get(root_slot)
            if limit and len(arr) > limit:
                arr.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
                while len(arr) > limit:
                    arr.pop(0)
            if not is_top:
                _update_modified(data, slot)
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "slot": slot})


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
            for slot_name, slot_val in existing_slots.items():
                if _is_cadence_tracker(slot_name) and slot_val is not None:
                    data["slots"][slot_name] = slot_val
                    if slot_name in existing_meta:
                        data["slot_meta"][slot_name] = existing_meta[slot_name]
                    cadence_preserved.append(slot_name)
            _write_wm(_wm_path(ctx), data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))
    return Response.json({"ok": True, "preserved_identity": sorted(preserved),
                          "preserved_cadence": len(cadence_preserved)})


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
