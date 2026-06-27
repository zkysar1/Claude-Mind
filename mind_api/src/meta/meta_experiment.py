"""A/B experiment lifecycle endpoints — parity with core/scripts/meta-experiment.py.

Daemonises the 4 meta-experiment subcommands (Batch 6). META-scoped
(meta/experiments/{active,completed}-experiments.yaml). No X-Mind-Agent gate
(agent-agnostic); changelog attribution uses the header defaulting to "system".

  POST /v1/meta/experiment/create   {strategy,field,baseline,variant}   write
  GET  /v1/meta/experiment/status?id=<id>                                read
  POST /v1/meta/experiment/resolve  {id}                                 write
  GET  /v1/meta/experiment/list?completed=1                              read

WRITE mechanism: _fileops.locked_write_yaml (CSafeDumper + history snapshot +
changelog "edit", NO summary). Replicated here via the daemon file_locks/
history/changelog helpers with base_dir = ctx.paths.meta (resolve_base_dir
returns META_DIR for meta/experiments/* in the CLI). create writes ONE file;
resolve writes BOTH active + completed in TWO separate locked cycles (NOT
atomic across files — faithful to CLI lines 156-157).

BYTE-COMPATIBILITY:
  - create stdout: json.dumps({"status":"created","id","strategy","field"})
    (default ensure_ascii=True, no indent) + "\n". Timestamp-free.
  - resolve stdout: json.dumps({"status":"resolved","id","outcome",
    "delta":round(delta,6)}) + "\n". Timestamp-free (delta is deterministic).
  - status/list stdout: json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    (ensure_ascii=False + default=str are both load-bearing).
  - The written YAML carries datetime.now() stamps (created/resolved) -> the
    test normalises them. Missing file -> empty list (status/list NOT 404).

sys.exit MAPPING: max_concurrent exceeded -> 409 (CLI exit 1, msg to stderr);
experiment-not-found (status/resolve) -> 404; bad float baseline/variant -> 400;
missing required create params -> 400. The PyYAML import guard is unreachable.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _active_path(ctx) -> Path:
    return ctx.paths.meta / "experiments" / "active-experiments.yaml"


def _completed_path(ctx) -> Path:
    return ctx.paths.meta / "experiments" / "completed-experiments.yaml"


def _config(ctx) -> Dict[str, Any]:
    return _read_yaml(ctx.paths.project_root / "core" / "config" / "meta.yaml")


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Byte-identical to _fileops.locked_write_yaml's inner write (CSafeDumper)."""
    assert_not_cruft(path.parent, "mkdir (meta_experiment)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_meta_experiment_write")


def _persist(ctx, path: Path, data: Any) -> None:
    """Locked CSafeDumper write + history snapshot + changelog 'edit' (no summary)."""
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    assert_not_cruft(path.parent, "mkdir (meta_experiment)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(path):
        _validate_no_surrogates(data, path)
        history.snapshot(path, base_dir, agent)
        _atomic_write_yaml(path, data)
        changelog.append(base_dir, agent, path, "edit")


def _next_id(experiments) -> str:
    max_num = 0
    for e in experiments:
        eid = e.get("id", "")
        if eid.startswith("exp-meta-"):
            try:
                max_num = max(max_num, int(eid.split("-")[-1]))
            except ValueError:
                pass
    return "exp-meta-{:03d}".format(max_num + 1)


# ---------------------------------------------------------------------------
# POST /v1/meta/experiment/create
# ---------------------------------------------------------------------------

def create(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")

    strategy = body.get("strategy")
    field = body.get("field")
    baseline = body.get("baseline")
    variant = body.get("variant")
    if strategy is None or field is None or baseline is None or variant is None:
        return Response.error(400, "missing_param",
                              "strategy, field, baseline, variant are required")
    try:
        baseline_value = float(baseline)
        variant_value = float(variant)
    except (ValueError, TypeError):
        return Response.error(400, "invalid_param", "baseline and variant must be floats")

    max_concurrent = _config(ctx).get("experiments", {}).get("max_concurrent", 1)
    active_path = _active_path(ctx)
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)

    assert_not_cruft(active_path.parent, "mkdir (meta_experiment)")
    active_path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(active_path):
        active = _read_yaml(active_path)
        experiments = active.get("experiments", [])
        if len(experiments) >= max_concurrent:
            return Response.error(409, "max_concurrent",
                                  "Max {} concurrent experiments".format(max_concurrent))
        exp_id = _next_id(experiments)
        experiment = {
            "id": exp_id,
            "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "strategy_file": strategy,
            "field": field,
            "baseline_value": baseline_value,
            "variant_value": variant_value,
            "status": "active",
            "phase": "baseline",
            "total_goals": 0,
            "metrics": {"baseline": [], "variant": []},
        }
        experiments.append(experiment)
        active["experiments"] = experiments
        _validate_no_surrogates(active, active_path)
        history.snapshot(active_path, base_dir, agent)
        _atomic_write_yaml(active_path, active)
        changelog.append(base_dir, agent, active_path, "edit")

    return Response.text(
        json.dumps({"status": "created", "id": exp_id,
                    "strategy": strategy, "field": field}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/experiment/status
# ---------------------------------------------------------------------------

def status(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    active = _read_yaml(_active_path(ctx))
    experiments = active.get("experiments", [])
    exp_id = ctx.query.get("id")
    if exp_id:
        for exp in experiments:
            if exp.get("id") == exp_id:
                return Response.text(
                    json.dumps(exp, ensure_ascii=False, default=str) + "\n",
                    content_type="application/json")
        return Response.error(404, "not_found", "Experiment {} not found".format(exp_id))
    return Response.text(
        json.dumps({"active_experiments": len(experiments), "experiments": experiments},
                   ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/experiment/resolve
# ---------------------------------------------------------------------------

def resolve(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    exp_id = (body.get("id") or "").strip()
    if not exp_id:
        return Response.error(400, "missing_param", "id is required")

    active = _read_yaml(_active_path(ctx))
    completed = _read_yaml(_completed_path(ctx))
    experiments = active.get("experiments", [])
    completed_list = completed.get("experiments", [])

    target = None
    remaining = []
    for exp in experiments:
        if exp.get("id") == exp_id:
            target = exp
        else:
            remaining.append(exp)
    if not target:
        return Response.error(404, "not_found", "Experiment {} not found".format(exp_id))

    baseline_metrics = target.get("metrics", {}).get("baseline", [])
    variant_metrics = target.get("metrics", {}).get("variant", [])
    if baseline_metrics and variant_metrics:
        delta = (sum(variant_metrics) / len(variant_metrics)
                 - sum(baseline_metrics) / len(baseline_metrics))
    else:
        delta = 0.0

    threshold = _config(ctx).get("experiments", {}).get("significance_threshold", 0.05)
    if delta > threshold:
        outcome = "adopted"
    elif delta < -threshold:
        outcome = "reverted"
    else:
        outcome = "inconclusive"

    target["resolved"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    target["outcome"] = outcome
    target["delta"] = round(delta, 6)
    target["status"] = "resolved"

    completed_list.append(target)
    completed["experiments"] = completed_list
    active["experiments"] = remaining

    # Two separate locked writes (NOT atomic across files — faithful to CLI).
    _persist(ctx, _active_path(ctx), active)
    _persist(ctx, _completed_path(ctx), completed)

    return Response.text(
        json.dumps({"status": "resolved", "id": exp_id,
                    "outcome": outcome, "delta": round(delta, 6)}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/experiment/list
# ---------------------------------------------------------------------------

def list_experiments(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    completed_flag = ctx.query.get("completed")
    use_completed = completed_flag is not None and str(completed_flag).lower() not in ("", "0", "false", "no")
    path = _completed_path(ctx) if use_completed else _active_path(ctx)
    data = _read_yaml(path)
    experiments = data.get("experiments", [])
    return Response.text(
        json.dumps({"count": len(experiments), "experiments": experiments},
                   ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/meta/experiment/create")] = create
    routes[("GET", "/v1/meta/experiment/status")] = status
    routes[("POST", "/v1/meta/experiment/resolve")] = resolve
    routes[("GET", "/v1/meta/experiment/list")] = list_experiments
