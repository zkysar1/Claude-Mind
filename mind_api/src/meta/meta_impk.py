"""Improvement-velocity (imp@k) endpoints — parity with core/scripts/meta-impk.py.

Daemonises the two imp@k subcommands (Batch 6):

  GET  /v1/meta/impk/compute   (cmd_compute)   ?window=<int>&metric=<str>
  POST /v1/meta/impk/snapshot  (cmd_snapshot)  ?goal_id=&learning_value=&category=&active_changes=

SCOPE: META. Both operate on META_DIR/improvement-velocity.yaml
(meta-impk.py:92/130). base_dir = ctx.paths.meta; resolve_base_dir returns
META_DIR for the CLI, so the write path snapshots + changelogs. No
X-Mind-Agent header REQUIRED (meta is agent-agnostic); attribution uses the
header when present, defaulting to "system" like _fileops._agent_name.

BYTE-COMPATIBILITY:
  - compute (read): json.dumps(result, ensure_ascii=False) + "\n"
    (meta-impk.py:125). The result dict's KEY ORDER is significant — replicate
    the two CLI dict literals exactly (insufficient_data branch vs computed
    branch, lines 97-103 / 117-123).
  - snapshot (write): improvement-velocity.yaml via _fileops.locked_write_yaml
    -> yaml.dump(data, handle, Dumper=yaml.CSafeDumper, default_flow_style=False,
    allow_unicode=True, sort_keys=False) (_fileops.py:1721) + save_history(...)
    [no summary] + append_changelog(..., "edit") [NO lines_changed -> 0, NO
    summary] (_fileops.py:1718/1728). The appended entry stamps date=now()
    (meta-impk.py:138), so the file is NOT byte-identical run-to-run; the test
    normalises the date. STDOUT: json.dumps({"status":"recorded","goal_id",
    "learning_value"}) + "\n" (meta-impk.py:162).

NUL-byte guard (g-115-1271, meta-impk.py:48): a transiently un-synced OneDrive
replica reads as NUL bytes; the CLI raises ValueError and the write ABORTS (no
clobber). Replicated in _read_yaml so the daemon snapshot path also refuses to
overwrite a corrupt replica.

sys.exit MAPPING: the only command-level sys.exit is the import-time PyYAML
guard (meta-impk.py:26), which cannot fire in the daemon (yaml is already
imported). Structural-validation failures (validate_velocity_structure) raise
ValueError -> propagate to the server's 500 handler, exactly as the CLI crashes
loudly with a traceback + non-zero exit and writes nothing.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip() or "system"


def _path(ctx) -> Path:
    return ctx.paths.meta / "improvement-velocity.yaml"


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Mirror meta-impk.py:read_yaml (line 31) incl. the NUL-byte guard."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if chr(0) in raw:
        raise ValueError(
            f"{path.name}: {raw.count(chr(0))} NUL byte(s) detected -- transient "
            f"OneDrive sync corruption of a shared meta hot-file. NOT overwriting "
            f"(read aborts, no clobber). (g-115-1271)")
    data = yaml.safe_load(raw)
    return data if data is not None else {}


def _validate_velocity_structure(data: Any, label: str = "") -> None:
    """Mirror meta-impk.py:validate_velocity_structure (line 66)."""
    prefix = f"[{label}] " if label else ""
    if not isinstance(data, dict):
        raise ValueError(f"{prefix}Expected dict, got {type(data).__name__}")
    allowed_keys = {"entries", "rolling_averages"}
    unexpected = set(data.keys()) - allowed_keys
    if unexpected:
        raise ValueError(f"{prefix}Unexpected top-level keys: {unexpected}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError(f"{prefix}'entries' is {type(entries).__name__}, expected list")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{prefix}entries[{i}] is {type(entry).__name__}, expected dict")
        if "goal_id" not in entry:
            raise ValueError(f"{prefix}entries[{i}] missing 'goal_id'")
        if "learning_value" not in entry:
            raise ValueError(f"{prefix}entries[{i}] missing 'learning_value'")
    ra = data.get("rolling_averages")
    if ra is not None and not isinstance(ra, dict):
        raise ValueError(f"{prefix}'rolling_averages' is {type(ra).__name__}, expected dict")


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Byte-identical to _fileops.locked_write_yaml's inner write (CSafeDumper)."""
    assert_not_cruft(path.parent, "mkdir (meta_impk)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_meta_impk_write")


# ---------------------------------------------------------------------------
# GET /v1/meta/impk/compute
# ---------------------------------------------------------------------------

def compute(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/meta/impk/compute?window=<int>&metric=<str>

    Mirrors meta-impk.py cmd_compute (line 90): imp@k over a rolling window.
    """
    from ..server import Response

    metric = ctx.query.get("metric")
    if not metric:
        return Response.error(400, "missing_param", "query parameter 'metric' required")
    window_raw = ctx.query.get("window")
    if window_raw is None or window_raw == "":
        return Response.error(400, "missing_param", "query parameter 'window' required")
    try:
        window = int(window_raw)
    except ValueError:
        return Response.error(400, "invalid_param", "window must be an integer")

    vel = _read_yaml(_path(ctx))
    entries = vel.get("entries", [])

    if len(entries) < window:
        result: Dict[str, Any] = {
            "metric": metric,
            "window": window,
            "imp_at_k": 0.0,
            "direction": "insufficient_data",
            "entries_available": len(entries),
        }
    else:
        recent = entries[-window:]
        older = (entries[-(window * 2):-window]
                 if len(entries) >= window * 2
                 else entries[:len(entries) - window])

        if older:
            recent_avg = sum(e.get("learning_value", 0) for e in recent) / len(recent)
            older_avg = sum(e.get("learning_value", 0) for e in older) / len(older)
            imp = (recent_avg - older_avg) / window
        else:
            recent_avg = sum(e.get("learning_value", 0) for e in recent) / len(recent)
            imp = recent_avg / window

        direction = ("improving" if imp > 0.001
                     else ("declining" if imp < -0.001 else "stable"))
        result = {
            "metric": metric,
            "window": window,
            "imp_at_k": round(imp, 6),
            "direction": direction,
            "recent_avg": round(recent_avg, 4),
        }

    return Response.text(json.dumps(result, ensure_ascii=False) + "\n",
                         content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/impk/snapshot
# ---------------------------------------------------------------------------

def snapshot(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/meta/impk/snapshot?goal_id=&learning_value=&category=&active_changes=

    Mirrors meta-impk.py cmd_snapshot (line 128): append a learning_value entry,
    recompute rolling averages, locked CSafeDumper write with history+changelog.
    """
    from ..server import Response

    goal_id = ctx.query.get("goal_id")
    if not goal_id:
        return Response.error(400, "missing_param", "query parameter 'goal_id' required")
    lv_raw = ctx.query.get("learning_value")
    if lv_raw is None or lv_raw == "":
        return Response.error(400, "missing_param", "query parameter 'learning_value' required")
    try:
        learning_value = float(lv_raw)
    except ValueError:
        return Response.error(400, "invalid_param", "learning_value must be a float")
    category = ctx.query.get("category", "")
    active_changes = ctx.query.get("active_changes", "")

    live_path = _path(ctx)
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)

    try:
        assert_not_cruft(live_path.parent, "mkdir (meta_impk)")
        live_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(live_path):
            vel = _read_yaml(live_path)
            _validate_velocity_structure(vel, "post-read")

            if "entries" not in vel:
                vel["entries"] = []

            entry: Dict[str, Any] = {
                "goal_id": goal_id,
                "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "learning_value": learning_value,
            }
            if category:
                entry["category"] = category
            if active_changes:
                entry["active_meta_changes"] = [
                    c.strip() for c in active_changes.split(",") if c.strip()]

            vel["entries"].append(entry)

            # Recompute rolling averages (meta-impk.py:149-156).
            entries = vel["entries"]
            for w in (5, 10, 20):
                key = f"window_{w}"
                if len(entries) >= w:
                    avg = sum(e.get("learning_value", 0) for e in entries[-w:]) / w
                    vel.setdefault("rolling_averages", {})[key] = round(avg, 4)
                else:
                    vel.setdefault("rolling_averages", {})[key] = 0.0

            _validate_velocity_structure(vel, "pre-write")
            _validate_no_surrogates(vel, live_path)
            history.snapshot(live_path, base_dir, agent)
            _atomic_write_yaml(live_path, vel)
            changelog.append(base_dir, agent, live_path, "edit")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.text(
        json.dumps({"status": "recorded", "goal_id": goal_id,
                    "learning_value": learning_value}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/meta/impk/compute")] = compute
    routes[("POST", "/v1/meta/impk/snapshot")] = snapshot
