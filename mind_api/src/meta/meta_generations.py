"""Strategy generation tracking — parity with core/scripts/meta-generations.py.

Daemonises the 6 meta-generations subcommands (Batch 6). META-scoped
(meta/strategy-generations.yaml). No X-Mind-Agent gate; changelog attribution
defaults to "system".

  GET  /v1/meta/generation/snapshot                       read (NO ensure_state)
  POST /v1/meta/generation/close    {metrics?}            write
  POST /v1/meta/generation/open                           write
  POST /v1/meta/generation/update   {learning_value}      write
  GET  /v1/meta/generation/status                         read (lazy-init write)
  GET  /v1/meta/generation/history  ?top=N                read (lazy-init write)

WRITE mechanism: _fileops.locked_write_yaml (CSafeDumper + history snapshot +
changelog "edit", NO summary), base_dir = ctx.paths.meta. Replicated via the
daemon file_locks/history/changelog helpers.

FAITHFUL QUIRKS (refuted-then-replicated):
  - `snapshot` is the ONLY command that does NOT call ensure_state -> it is a
    pure read of the 6 STRATEGY_FILES, never writes.
  - `status`/`history` call ensure_state, which WRITES the init state if the
    file lacks "version" -> a one-time lazy-init write side-effect on a "read".
  - `update`'s auto-open path seeds metrics with TWO keys (avg/total
    learning_value); `open`'s metrics has FOUR keys (adds avg_goal_completion_
    rate + pipeline_accuracy). Not unified — replicated as-is.
  - peak update in `update` requires goals_completed >= 3; in `close` it does
    not. Both compare avg_learning_value > peak_score.
  - `close` with no/closed generation prints an {"error":...} JSON on stdout
    and exits 0 -> mapped to HTTP 200 with that exact body (NOT a 4xx). Bad
    --learning-value (float parse) crashes the CLI (exit 1) -> 400.

BYTE-COMPATIBILITY:
  - snapshot/status: json.dumps(obj, ensure_ascii=False, default=str)+"\n",
    timestamp-free.
  - close/open/update: json.dumps(obj) (default ensure_ascii=True)+"\n",
    timestamp-free (timestamps go to the file, not stdout).
  - history: json.dumps(list, ensure_ascii=False, default=str)+"\n" — carries
    started/ended timestamps -> the test normalises them.
  - The written YAML carries started/ended stamps -> the test normalises them.
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


# Keep in sync with core/scripts/meta-generations.py STRATEGY_FILES.
STRATEGY_FILES = [
    "goal-selection-strategy.yaml",
    "reflection-strategy.yaml",
    "evolution-strategy.yaml",
    "aspiration-generation-strategy.yaml",
    "encoding-strategy.yaml",
    "skill-quality-strategy.yaml",
]


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _gen_path(ctx) -> Path:
    return ctx.paths.meta / "strategy-generations.yaml"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Byte-identical to _fileops.locked_write_yaml's inner write (CSafeDumper)."""
    assert_not_cruft(path.parent, "mkdir (meta_generations)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_meta_generations_write")


def _persist(ctx, path: Path, data: Any) -> None:
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    assert_not_cruft(path.parent, "mkdir (meta_generations)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(path):
        _validate_no_surrogates(data, path)
        history.snapshot(path, base_dir, agent)
        _atomic_write_yaml(path, data)
        changelog.append(base_dir, agent, path, "edit")


def _ensure_state(ctx) -> Dict[str, Any]:
    """Ensure strategy-generations.yaml exists (writes init if version absent)."""
    path = _gen_path(ctx)
    data = _read_yaml(path)
    if "version" not in data:
        data = {
            "version": 1,
            "current_generation": 0,
            "generations": [],
            "peak_generation": None,
            "peak_score": 0.0,
        }
        _persist(ctx, path, data)
    return data


def _flatten(data, prefix, result, max_depth=2, depth=0):
    """Flatten a dict to dot-notation keys, limited depth (verbatim from CLI)."""
    if depth >= max_depth or not isinstance(data, dict):
        return
    for key, value in data.items():
        full_key = "{}.{}".format(prefix, key)
        if isinstance(value, (int, float, str, bool, type(None))):
            result[full_key] = value
        elif isinstance(value, dict) and depth < max_depth - 1:
            _flatten(value, full_key, result, max_depth, depth + 1)


def _capture_snapshot(ctx) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    for filename in STRATEGY_FILES:
        path = ctx.paths.meta / filename
        if not path.exists():
            continue
        data = _read_yaml(path)
        prefix = filename.replace(".yaml", "").replace("-", "_")
        _flatten(data, prefix, snapshot)
    return snapshot


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# GET /v1/meta/generation/snapshot  (pure read — NO ensure_state)
# ---------------------------------------------------------------------------

def snapshot(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    snap = _capture_snapshot(ctx)
    return Response.text(
        json.dumps(snap, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/generation/close  {metrics?}
# ---------------------------------------------------------------------------

def close(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")

    data = _ensure_state(ctx)
    generations = data.get("generations", [])

    if not generations:
        return Response.text(
            json.dumps({"error": "No active generation to close"}) + "\n",
            content_type="application/json")
    current = generations[-1]
    if current.get("ended") is not None:
        return Response.text(
            json.dumps({"error": "Current generation already closed"}) + "\n",
            content_type="application/json")

    current["ended"] = _now()

    metrics = body.get("metrics")
    if metrics:
        if not isinstance(metrics, dict):
            return Response.error(400, "invalid_param", "metrics must be a JSON object")
        current.setdefault("metrics", {}).update(metrics)

    avg_lv = current.get("metrics", {}).get("avg_learning_value", 0.0)
    if avg_lv > data.get("peak_score", 0.0):
        data["peak_generation"] = current["generation"]
        data["peak_score"] = avg_lv

    _persist(ctx, _gen_path(ctx), data)

    return Response.text(
        json.dumps({"status": "closed", "generation": current["generation"],
                    "goals_completed": current.get("goals_completed", 0),
                    "avg_learning_value": avg_lv}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/generation/open
# ---------------------------------------------------------------------------

def open_generation(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    data = _ensure_state(ctx)
    generations = data.get("generations", [])

    if generations and generations[-1].get("ended") is None:
        generations[-1]["ended"] = _now()

    new_gen_num = data.get("current_generation", 0) + 1
    snap = _capture_snapshot(ctx)

    new_gen = {
        "generation": new_gen_num,
        "started": _now(),
        "ended": None,
        "goals_completed": 0,
        "parameter_snapshot": snap,
        "metrics": {
            "avg_learning_value": 0.0,
            "total_learning_value": 0.0,
            "avg_goal_completion_rate": 0.0,
            "pipeline_accuracy": 0.0,
        },
        "best_score": 0.0,
        "worst_score": 1.0,
    }

    generations.append(new_gen)
    data["generations"] = generations
    data["current_generation"] = new_gen_num
    _persist(ctx, _gen_path(ctx), data)

    return Response.text(
        json.dumps({"status": "opened", "generation": new_gen_num,
                    "parameters_tracked": len(snap)}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/generation/update  {learning_value}
# ---------------------------------------------------------------------------

def update(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    lv_raw = body.get("learning_value")
    if lv_raw is None:
        return Response.error(400, "missing_param", "learning_value is required")
    try:
        learning_value = float(lv_raw)
    except (ValueError, TypeError):
        return Response.error(400, "invalid_param", "learning_value must be a float")

    data = _ensure_state(ctx)
    generations = data.get("generations", [])

    # Auto-open generation if none exists / current is closed (2-key metrics).
    if not generations or generations[-1].get("ended") is not None:
        new_num = data.get("current_generation", 0) + 1
        snap = _capture_snapshot(ctx)
        generations.append({
            "generation": new_num,
            "started": _now(),
            "ended": None,
            "goals_completed": 0,
            "parameter_snapshot": snap,
            "metrics": {"avg_learning_value": 0.0, "total_learning_value": 0.0},
            "best_score": 0.0,
            "worst_score": 1.0,
        })
        data["generations"] = generations
        data["current_generation"] = new_num

    current = generations[-1]
    current["goals_completed"] = current.get("goals_completed", 0) + 1

    metrics = current.setdefault("metrics", {})
    total = metrics.get("total_learning_value", 0.0) + learning_value
    metrics["total_learning_value"] = total
    metrics["avg_learning_value"] = round(total / current["goals_completed"], 4)

    current["best_score"] = max(current.get("best_score", 0.0), learning_value)
    current["worst_score"] = min(current.get("worst_score", 1.0), learning_value)

    if (metrics["avg_learning_value"] > data.get("peak_score", 0.0)
            and current["goals_completed"] >= 3):
        data["peak_generation"] = current["generation"]
        data["peak_score"] = metrics["avg_learning_value"]

    _persist(ctx, _gen_path(ctx), data)

    return Response.text(
        json.dumps({"status": "updated", "generation": current["generation"],
                    "goals_completed": current["goals_completed"],
                    "avg_learning_value": metrics["avg_learning_value"]}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/generation/status
# ---------------------------------------------------------------------------

def status(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    data = _ensure_state(ctx)
    generations = data.get("generations", [])
    current = generations[-1] if generations else None
    result = {
        "current_generation": data.get("current_generation", 0),
        "current_goals": current.get("goals_completed", 0) if current else 0,
        "current_avg_lv": (current.get("metrics", {}).get("avg_learning_value", 0.0)
                           if current else 0.0),
        "peak_generation": data.get("peak_generation"),
        "peak_score": data.get("peak_score", 0.0),
        "total_generations": len(generations),
    }
    return Response.text(
        json.dumps(result, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/generation/history  ?top=N
# ---------------------------------------------------------------------------

def history_cmd(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    data = _ensure_state(ctx)
    generations = data.get("generations", [])

    sorted_gens = sorted(
        generations,
        key=lambda g: g.get("metrics", {}).get("avg_learning_value", 0.0),
        reverse=True,
    )
    top_raw = ctx.query.get("top")
    if top_raw:
        try:
            top_n = int(top_raw)
        except (ValueError, TypeError):
            return Response.error(400, "invalid_param", "top must be an integer")
        sorted_gens = sorted_gens[:top_n]

    result = []
    for gen in sorted_gens:
        result.append({
            "generation": gen["generation"],
            "started": gen.get("started"),
            "ended": gen.get("ended"),
            "goals_completed": gen.get("goals_completed", 0),
            "avg_learning_value": gen.get("metrics", {}).get("avg_learning_value", 0.0),
            "best_score": gen.get("best_score", 0.0),
        })
    return Response.text(
        json.dumps(result, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/meta/generation/snapshot")] = snapshot
    routes[("POST", "/v1/meta/generation/close")] = close
    routes[("POST", "/v1/meta/generation/open")] = open_generation
    routes[("POST", "/v1/meta/generation/update")] = update
    routes[("GET", "/v1/meta/generation/status")] = status
    routes[("GET", "/v1/meta/generation/history")] = history_cmd
