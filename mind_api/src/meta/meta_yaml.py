"""Generic meta/ YAML store — parity with core/scripts/meta-yaml.py.

Daemonises the 4 meta-yaml subcommands (Batch 6). META-scoped. The single
most byte-compat-sensitive subsystem because read and set use DIFFERENT YAML
dumpers and there are three side-effect write targets on `set`.

  GET  /v1/meta/yaml/read    ?file=..&field=..&json=1
  POST /v1/meta/yaml/set     {file,dotpath,value,string?,reason?}
  POST /v1/meta/yaml/append  {file,dotpath,item}
  POST /v1/meta/yaml/log     (raw JSON body == the record; appended verbatim)

CRITICAL byte-compat traps:
  - `read` YAML output uses the DEFAULT yaml.Dumper (NOT CSafeDumper):
    yaml.dump(value, default_flow_style=False, allow_unicode=True,
    sort_keys=False). Scalars print via str()+"\\n"; --json -> json.dumps(
    ensure_ascii=False, default=str)+"\\n".
  - `set` writes the target file via locked_write_yaml (CSafeDumper + history +
    changelog), then RAW-appends a record to meta/meta-log.jsonl
    (json.dumps(record, ensure_ascii=False, default=str)+"\\n" — NO lock/
    history/changelog), then for STRATEGY files (and non-rollback reasons) fires
    two further locked CSafeDumper writes: a backpressure monitor
    (meta/backpressure.yaml) and a generation transition
    (meta/strategy-generations.yaml, only if already version-initialised). These
    inline side-effect helpers are lifted verbatim from meta-yaml.py — they are
    intentionally slightly different from meta-generations.py / meta-backpressure.py
    (lightweight 2-level snapshot, 2-key metrics).
  - `log` appends the caller's raw JSON VERBATIM (the CLI writes stdin bytes
    untouched after a json.loads validation) — so the daemon appends the raw
    request body, never a re-serialised form.
  - resolve_path / navigate sys.exit in the CLI; here they raise _MetaYamlError
    (a SystemExit inside a daemon thread is not acceptable) -> mapped to HTTP 400.
    Field-not-found -> 404. NUL-corrupted file (OneDrive guard) -> 500.

`set` produces NO stdout in the CLI; the daemon returns {"status":"set",
"meta_change_id":mc_id} for the wrapper's benefit — byte-compat for `set` is on
the WRITTEN artefacts (target file + meta-log record + backpressure +
generations), not on stdout. Bounds clamping (load_bounds) reads the REAL
core/config/meta.yaml via project_root.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402


class _MetaYamlError(Exception):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Path + IO helpers
# ---------------------------------------------------------------------------

def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip() or "system"


def _resolve_path(ctx, rel_path: str) -> Path:
    meta = ctx.paths.meta
    target = (meta / rel_path).resolve()
    if not target.is_relative_to(meta.resolve()):
        raise _MetaYamlError(400, "path_traversal",
                             "Path '{}' resolves outside meta/".format(rel_path))
    if target.suffix != ".yaml":
        raise _MetaYamlError(400, "bad_suffix",
                             "meta/ targets must end in .yaml — got '{}' "
                             "(resolved basename: {})".format(rel_path, target.name))
    return target


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if chr(0) in raw:
        raise _MetaYamlError(
            500, "nul_corruption",
            "{}: {} NUL byte(s) detected -- transient OneDrive sync corruption "
            "of a shared meta hot-file. NOT overwriting (read aborts, no clobber). "
            "(g-115-1271)".format(path.name, raw.count(chr(0))))
    data = yaml.safe_load(raw)
    return data if data is not None else {}


def _atomic_write_yaml(path: Path, data: Any) -> None:
    assert_not_cruft(path.parent, "mkdir (meta_yaml)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(path, _write, fallback_counter_key="daemon_meta_yaml_write")


def _persist(ctx, path: Path, data: Any) -> None:
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    assert_not_cruft(path.parent, "mkdir (meta_yaml)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(path):
        _validate_no_surrogates(data, path)
        history.snapshot(path, base_dir, agent)
        _atomic_write_yaml(path, data)
        changelog.append(base_dir, agent, path, "edit")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _navigate(data, dotpath):
    """Dot/bracket navigation; raises _MetaYamlError instead of sys.exit."""
    dotpath = re.sub(r"\[(\d+)\]", r".\1", dotpath).lstrip(".")
    parts = dotpath.split(".")
    current = data
    for i, part in enumerate(parts[:-1]):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except ValueError:
                raise _MetaYamlError(400, "navigate_error",
                                     "list index '{}' is not an integer at '{}'".format(
                                         part, ".".join(parts[:i + 1])))
            except IndexError:
                raise _MetaYamlError(400, "navigate_error",
                                     "list index {} out of range at '{}' (list len {})".format(
                                         part, ".".join(parts[:i + 1]), len(current)))
        elif isinstance(current, dict):
            if part not in current:
                current[part] = {}
            current = current[part]
        else:
            raise _MetaYamlError(400, "navigate_error",
                                 "Cannot navigate into {} at '{}'".format(
                                     type(current).__name__, ".".join(parts[:i + 1])))
    final_key = parts[-1]
    if isinstance(current, list):
        try:
            final_key = int(final_key)
        except ValueError:
            raise _MetaYamlError(400, "navigate_error",
                                 "list index '{}' is not an integer (target list at '{}')".format(
                                     final_key, ".".join(parts[:-1]) or "<root>"))
    return current, final_key


def _parse_value(raw):
    """Auto-detect type from string (verbatim from CLI)."""
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    stripped = raw.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


def _coerce_set_value(raw, string_flag):
    if string_flag:
        return raw if isinstance(raw, str) else str(raw)
    if isinstance(raw, str):
        return _parse_value(raw)
    return raw  # already-typed JSON value


def _load_bounds(ctx) -> Dict[str, Any]:
    meta_config = ctx.paths.project_root / "core" / "config" / "meta.yaml"
    if not meta_config.exists():
        return {}
    with open(meta_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return (config or {}).get("strategy_schemas", {})


def _validate_weight_bounds(file_rel, dotpath, value, bounds):
    """Clamp goal_selection weights / evolution trigger_sensitivity (stderr-only
    notice in CLI -> daemon silently clamps; the clamped value is what's stored)."""
    if "goal-selection-strategy" in file_rel and dotpath.startswith("weights."):
        wb = bounds.get("goal_selection", {}).get("weight_bounds", {})
        lo, hi = wb.get("min", 0.0), wb.get("max", 3.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(lo, min(hi, float(value)))
    if "evolution-strategy" in file_rel and "trigger_sensitivity" in dotpath:
        tb = bounds.get("evolution", {}).get("trigger_sensitivity_bounds", {})
        lo, hi = tb.get("min", 0.1), tb.get("max", 5.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(lo, min(hi, float(value)))
    return value


def _next_meta_change_id(ctx) -> str:
    log_path = ctx.paths.meta / "meta-log.jsonl"
    max_num = 0
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    mc_id = rec.get("meta_change_id", "")
                    if mc_id.startswith("mc-"):
                        max_num = max(max_num, int(mc_id[3:]))
                except (json.JSONDecodeError, ValueError):
                    pass
    return "mc-{:03d}".format(max_num + 1)


def _append_log(ctx, file_rel, dotpath, old_value, new_value, reason="") -> str:
    log_path = ctx.paths.meta / "meta-log.jsonl"
    mc_id = _next_meta_change_id(ctx)
    record = {
        "date": _now(), "meta_change_id": mc_id, "strategy_file": file_rel,
        "field": dotpath, "old_value": old_value, "new_value": new_value, "reason": reason,
    }
    assert_not_cruft(log_path.parent, "mkdir (meta_yaml append_log)")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return mc_id


def _create_backpressure_monitor(ctx, mc_id, file_rel, dotpath, old_value, new_value):
    """Lifted from meta-yaml.py (lightweight inline version; non-fatal)."""
    try:
        bp_path = ctx.paths.meta / "backpressure.yaml"
        vel_path = ctx.paths.meta / "improvement-velocity.yaml"
        baseline = 0.0
        if vel_path.exists():
            vel = _read_yaml(vel_path)
            entries = vel.get("entries", [])
            if entries:
                recent = entries[-min(10, len(entries)):]
                baseline = sum(e.get("learning_value", 0) for e in recent) / len(recent)
        bp = _read_yaml(bp_path)
        if "version" not in bp:
            bp = {"version": 1, "active_monitors": [], "rollback_history": []}
        config = _load_bounds(ctx).get("backpressure", {})
        max_monitors = config.get("max_active_monitors", 5)
        monitors = bp.get("active_monitors", [])
        if len(monitors) >= max_monitors:
            monitors.pop(0)
        monitors.append({
            "meta_change_id": mc_id, "strategy_file": file_rel, "field": dotpath,
            "old_value": old_value, "new_value": new_value,
            "baseline_imp_k": round(baseline, 4), "goals_since_change": 0,
            "imp_k_samples": [], "consecutive_below_baseline": 0,
            "consecutive_above_baseline": 0, "status": "monitoring", "created": _now(),
        })
        bp["active_monitors"] = monitors
        _persist(ctx, bp_path, bp)
    except Exception:  # noqa: BLE001 — non-fatal, matches CLI broad catch (stderr-only)
        pass


def _trigger_generation_transition(ctx):
    """Lifted from meta-yaml.py (inline lightweight version; non-fatal)."""
    try:
        gen_path = ctx.paths.meta / "strategy-generations.yaml"
        gen = _read_yaml(gen_path)
        if "version" not in gen:
            return  # not initialised
        generations = gen.get("generations", [])
        if generations and generations[-1].get("ended") is None:
            generations[-1]["ended"] = _now()
        new_num = gen.get("current_generation", 0) + 1
        snapshot = {}
        strategy_files = [
            "goal-selection-strategy.yaml", "reflection-strategy.yaml",
            "evolution-strategy.yaml", "aspiration-generation-strategy.yaml",
            "encoding-strategy.yaml", "skill-quality-strategy.yaml",
        ]
        for sf in strategy_files:
            sf_path = ctx.paths.meta / sf
            if sf_path.exists():
                sf_data = _read_yaml(sf_path)
                prefix = sf.replace(".yaml", "").replace("-", "_")
                if isinstance(sf_data, dict):
                    for k, v in sf_data.items():
                        if isinstance(v, (int, float, str, bool, type(None))):
                            snapshot["{}.{}".format(prefix, k)] = v
                        elif isinstance(v, dict):
                            for k2, v2 in v.items():
                                if isinstance(v2, (int, float, str, bool, type(None))):
                                    snapshot["{}.{}.{}".format(prefix, k, k2)] = v2
        generations.append({
            "generation": new_num, "started": _now(), "ended": None,
            "goals_completed": 0, "parameter_snapshot": snapshot,
            "metrics": {"avg_learning_value": 0.0, "total_learning_value": 0.0},
            "best_score": 0.0, "worst_score": 1.0,
        })
        gen["generations"] = generations
        gen["current_generation"] = new_num
        _persist(ctx, gen_path, gen)
    except Exception:  # noqa: BLE001 — non-fatal, matches CLI broad catch
        pass


def _truthy(v) -> bool:
    return v is not None and str(v).lower() not in ("", "0", "false", "no")


# ---------------------------------------------------------------------------
# GET /v1/meta/yaml/read
# ---------------------------------------------------------------------------

def read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    file_rel = ctx.query.get("file")
    if not file_rel:
        return Response.error(400, "missing_param", "file is required")
    field = ctx.query.get("field")
    as_json = _truthy(ctx.query.get("json"))
    try:
        path = _resolve_path(ctx, file_rel)
        data = _read_yaml(path)
        if field:
            parent, key = _navigate(data, field)
            try:
                value = parent[key]
            except (KeyError, IndexError, TypeError):
                return Response.error(404, "field_not_found",
                                      "Field '{}' not found".format(field))
            if as_json:
                body = json.dumps(value, ensure_ascii=False, default=str) + "\n"
                ct = "application/json"
            elif isinstance(value, (dict, list)):
                body = yaml.dump(value, default_flow_style=False,
                                 allow_unicode=True, sort_keys=False)
                ct = "text/plain"
            else:
                body = str(value) + "\n"
                ct = "text/plain"
        else:
            if as_json:
                body = json.dumps(data, ensure_ascii=False, default=str) + "\n"
                ct = "application/json"
            else:
                body = yaml.dump(data, default_flow_style=False,
                                 allow_unicode=True, sort_keys=False)
                ct = "text/plain"
    except _MetaYamlError as e:
        return Response.error(e.status, e.code, e.detail)
    return Response.text(body, content_type=ct)


# ---------------------------------------------------------------------------
# POST /v1/meta/yaml/set
# ---------------------------------------------------------------------------

def set_field(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    file_rel = body.get("file")
    dotpath = body.get("dotpath")
    if not file_rel or dotpath is None:
        return Response.error(400, "missing_param", "file and dotpath are required")
    if "value" not in body:
        return Response.error(400, "missing_param", "value is required")
    string_flag = bool(body.get("string"))
    reason = body.get("reason") or ""

    try:
        path = _resolve_path(ctx, file_rel)
        data = _read_yaml(path)
        value = _coerce_set_value(body["value"], string_flag)
        value = _validate_weight_bounds(file_rel, dotpath, value, _load_bounds(ctx))
        parent, key = _navigate(data, dotpath)
        try:
            old_value = parent[key]
        except (KeyError, IndexError, TypeError):
            old_value = None
        if isinstance(parent, list) and isinstance(key, int) and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value
        _persist(ctx, path, data)
    except _MetaYamlError as e:
        return Response.error(e.status, e.code, e.detail)

    mc_id = _append_log(ctx, file_rel, dotpath, old_value, value, reason)

    is_strategy_file = any(s in file_rel for s in [
        "goal-selection-strategy", "reflection-strategy", "evolution-strategy",
        "aspiration-generation-strategy", "encoding-strategy", "skill-quality-strategy",
    ])
    is_rollback = "BACKPRESSURE ROLLBACK" in reason
    if is_strategy_file and not is_rollback:
        _create_backpressure_monitor(ctx, mc_id, file_rel, dotpath, old_value, value)
        _trigger_generation_transition(ctx)

    return Response.text(
        json.dumps({"status": "set", "meta_change_id": mc_id}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/yaml/append   {file,dotpath,item}
# ---------------------------------------------------------------------------

def append_item(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    file_rel = body.get("file")
    dotpath = body.get("dotpath")
    if not file_rel or dotpath is None:
        return Response.error(400, "missing_param", "file and dotpath are required")
    if "item" not in body:
        return Response.error(400, "missing_param", "item is required")
    item = body["item"]

    try:
        path = _resolve_path(ctx, file_rel)
        data = _read_yaml(path)
        parent, key = _navigate(data, dotpath)
        arr = parent.get(key) if isinstance(parent, dict) else parent[key]
        if arr is None:
            parent[key] = []
            arr = parent[key]
        if not isinstance(arr, list):
            return Response.error(400, "not_a_list",
                                  "Field '{}' is {}, not a list".format(
                                      dotpath, type(arr).__name__))
        arr.append(item)
        _persist(ctx, path, data)
    except _MetaYamlError as e:
        return Response.error(e.status, e.code, e.detail)
    return Response.text(
        json.dumps({"status": "appended", "field": dotpath}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/yaml/log   (raw JSON body == the record, appended verbatim)
# ---------------------------------------------------------------------------

def log_record(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        raw = ctx.body.decode("utf-8").strip() if ctx.body else ""
    except AttributeError:
        return Response.error(400, "invalid_body", "request body must be text")
    if not raw:
        return Response.error(400, "missing_body", "expected a JSON record in the body")
    try:
        json.loads(raw)  # validate only — appended verbatim like the CLI's stdin
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_json", "body is not valid JSON: {}".format(e))
    log_path = ctx.paths.meta / "meta-log.jsonl"
    assert_not_cruft(log_path.parent, "mkdir (meta_yaml log endpoint)")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(raw + "\n")
    return Response.text(
        json.dumps({"status": "logged"}) + "\n", content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/meta/yaml/read")] = read
    routes[("POST", "/v1/meta/yaml/set")] = set_field
    routes[("POST", "/v1/meta/yaml/append")] = append_item
    routes[("POST", "/v1/meta/yaml/log")] = log_record
