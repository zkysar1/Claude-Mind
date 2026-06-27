"""Cross-domain meta-strategy transfer — parity with core/scripts/meta-transfer.py.

Daemonises the 2 meta-transfer subcommands (Batch 6):

  POST /v1/meta/transfer-export  {output}            write (mixed scope)
  POST /v1/meta/transfer-import  {input, dry_run}     write (meta)

EXPORT (mixed scope, X-Mind-Agent REQUIRED): reads 4 META files
(goal-selection/reflection/encoding strategies + meta.yaml) and the bound
agent's self.md for provenance. Writes TWO targets with DIFFERENT mechanisms:
  - the bundle file (user-specified `output`): RAW open + yaml.dump with the
    DEFAULT yaml.Dumper (NOT CSafeDumper), NO lock/history/changelog.
  - meta/transfer/_index.yaml: locked_write_yaml (CSafeDumper + history +
    changelog "edit"). The dual-Dumper split is load-bearing for byte-compat.

IMPORT (META scope, no header): reads a bundle file, merges into up to 3
strategy files via locked_write_yaml each. Merge semantics vary by field:
  - goal_selection.weights: key-by-key, clamped [0,3], EXISTING keys only.
  - reflection.depth_allocation: full replacement.
  - selection_heuristics / trigger_overrides / priority_rules: list-append with
    a "source: transfer from <agent>" annotation.
The response "changes" count tracks ONLY weight merges + depth_allocation
replacement (list appends are NOT counted) — replicated for byte-compat.
dry_run -> preview only (no reads of strategy files, no writes). Missing input
bundle -> 404 (CLI would crash unhandled). Read-outside-lock TOCTOU mirrored.

stdout (export + import + dry_run): json.dumps(obj) default ensure_ascii=True,
no indent, + "\n". The bundle carries an "exported" datetime.now() stamp -> the
test normalises it; export/import stdout are otherwise timestamp-free.
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
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _atomic_write_yaml_csafe(path: Path, data: Any) -> None:
    assert_not_cruft(path.parent, "mkdir (meta_transfer)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(path, _write, fallback_counter_key="daemon_meta_transfer_write")


def _persist(ctx, path: Path, data: Any) -> None:
    """locked_write_yaml equivalent: CSafeDumper + history + changelog 'edit'."""
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    assert_not_cruft(path.parent, "mkdir (meta_transfer)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(path):
        _validate_no_surrogates(data, path)
        history.snapshot(path, base_dir, agent)
        _atomic_write_yaml_csafe(path, data)
        changelog.append(base_dir, agent, path, "edit")


# ---------------------------------------------------------------------------
# POST /v1/meta/transfer-export
# ---------------------------------------------------------------------------

def export(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    agent_hdr = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent_hdr:
        return Response.error(400, "missing_agent_header",
                              "X-Mind-Agent header required (export reads the agent self.md).")
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    output_raw = body.get("output")
    if not output_raw:
        return Response.error(400, "missing_param", "output is required")

    meta = ctx.paths.meta
    goal_sel = _read_yaml(meta / "goal-selection-strategy.yaml")
    reflection = _read_yaml(meta / "reflection-strategy.yaml")
    encoding = _read_yaml(meta / "encoding-strategy.yaml")
    meta_state = _read_yaml(meta / "meta.yaml")

    # Provenance from the bound agent's self.md (verbatim line-scan, not yaml).
    self_path = ctx.paths.agent / "self.md" if ctx.paths.agent else None
    self_name = "unknown"
    if self_path and self_path.exists():
        with open(self_path, "r", encoding="utf-8") as f:
            content = f.read()
        for line in content.split("\n"):
            if line.startswith("name:"):
                self_name = line.split(":", 1)[1].strip().strip('"')
                break

    bundle = {
        "exported": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source_agent": self_name,
        "total_goals_at_export": meta_state.get("total_meta_changes", 0),
        "strategies": {
            "goal_selection": {
                "weights": goal_sel.get("weights", {}),
                "selection_heuristics": goal_sel.get("selection_heuristics", []),
            },
            "reflection": {
                "depth_allocation": reflection.get("depth_allocation", {}),
                "trigger_overrides": reflection.get("trigger_overrides", []),
            },
            "encoding": {
                "priority_rules": encoding.get("priority_rules", []),
            },
        },
    }

    # Resolve output: relative -> meta/transfer/<name>; absolute honored.
    out_path = Path(output_raw)
    if not out_path.is_absolute():
        out_path = meta / "transfer" / output_raw
    assert_not_cruft(out_path.parent, "mkdir (meta_transfer bundle)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # RAW write, DEFAULT yaml.Dumper, NO lock/history/changelog (CLI line 80-81).
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(bundle, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Register in transfer index (locked CSafeDumper + history + changelog).
    index_path = meta / "transfer" / "_index.yaml"
    index = _read_yaml(index_path)
    bundles = index.get("bundles", [])
    bundles.append({"path": output_raw, "exported": bundle["exported"], "source": self_name})
    index["bundles"] = bundles
    _persist(ctx, index_path, index)

    return Response.text(
        json.dumps({"status": "exported", "path": output_raw,
                    "strategies": list(bundle["strategies"].keys())}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/transfer-import
# ---------------------------------------------------------------------------

def import_bundle(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    input_raw = body.get("input")
    if not input_raw:
        return Response.error(400, "missing_param", "input is required")

    in_path = Path(input_raw)
    if not in_path.is_absolute():
        in_path = ctx.paths.meta / "transfer" / input_raw
    if not in_path.exists():
        return Response.error(404, "not_found", "bundle not found: {}".format(input_raw))
    with open(in_path, "r", encoding="utf-8") as f:
        bundle = yaml.safe_load(f)

    strategies = (bundle or {}).get("strategies", {})
    changes = []

    dry_run = body.get("dry_run")
    if dry_run is not None and str(dry_run).lower() not in ("", "0", "false", "no"):
        for strategy_name, data in strategies.items():
            changes.append({"strategy": strategy_name,
                            "fields": list(data.keys()), "action": "would_merge"})
        return Response.text(
            json.dumps({"dry_run": True, "changes": changes}) + "\n",
            content_type="application/json")

    meta = ctx.paths.meta
    source = bundle.get("source_agent", "unknown")

    if "goal_selection" in strategies:
        gs = _read_yaml(meta / "goal-selection-strategy.yaml")
        imported = strategies["goal_selection"]
        if "weights" in imported:
            for k, v in imported["weights"].items():
                if k in gs.get("weights", {}):
                    gs["weights"][k] = max(0.0, min(3.0, float(v)))
                    changes.append({"field": "weights.{}".format(k),
                                    "value": gs["weights"][k], "source": "transfer"})
        if "selection_heuristics" in imported:
            existing = gs.get("selection_heuristics", [])
            for h in imported["selection_heuristics"]:
                h["source"] = "transfer from {}".format(source)
                existing.append(h)
            gs["selection_heuristics"] = existing
        _persist(ctx, meta / "goal-selection-strategy.yaml", gs)

    if "reflection" in strategies:
        ref = _read_yaml(meta / "reflection-strategy.yaml")
        imported = strategies["reflection"]
        if "depth_allocation" in imported:
            ref["depth_allocation"] = imported["depth_allocation"]
            changes.append({"field": "depth_allocation",
                            "value": ref["depth_allocation"], "source": "transfer"})
        if "trigger_overrides" in imported:
            existing = ref.get("trigger_overrides", [])
            for t in imported["trigger_overrides"]:
                t["source"] = "transfer from {}".format(source)
                existing.append(t)
            ref["trigger_overrides"] = existing
        _persist(ctx, meta / "reflection-strategy.yaml", ref)

    if "encoding" in strategies:
        enc = _read_yaml(meta / "encoding-strategy.yaml")
        imported = strategies["encoding"]
        if "priority_rules" in imported:
            existing = enc.get("priority_rules", [])
            for r in imported["priority_rules"]:
                r["source"] = "transfer from {}".format(source)
                existing.append(r)
            enc["priority_rules"] = existing
        _persist(ctx, meta / "encoding-strategy.yaml", enc)

    return Response.text(
        json.dumps({"status": "imported", "changes": len(changes), "details": changes}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/meta/transfer-export")] = export
    routes[("POST", "/v1/meta/transfer-import")] = import_bundle
