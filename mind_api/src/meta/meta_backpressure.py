"""Backpressure gate — parity with core/scripts/meta-backpressure.py.

Daemonises the 7 meta-backpressure subcommands (Batch 6). META-scoped writes
to meta/backpressure.yaml via locked CSafeDumper + history + changelog.

  POST /v1/meta/backpressure/monitor           {change_id,file,field,old,new,baseline}
  POST /v1/meta/backpressure/check             {learning_value}
  POST /v1/meta/backpressure/graduate          {change_id}
  GET  /v1/meta/backpressure/status
  GET  /v1/meta/backpressure/cooldown-check    ?window=N
  POST /v1/meta/backpressure/evolution-monitor {monitor_kind,revision_id,file_path,
                                                 history_snapshot,baseline_vector,agent?}
  POST /v1/meta/backpressure/evolution-check

CONFIG: load_config / load_evolution_config read ctx.paths.project_root/core/
config/meta.yaml (CONFIG_DIR), falling back to hardcoded defaults if absent.
check / cooldown-check read meta/improvement-velocity.yaml for total goal count.

FAITHFUL QUIRKS:
  - `graduate` on a NOT-FOUND change_id prints {"error":...} and returns BEFORE
    write_yaml — it must NOT persist the post-loop state. ensure_state's lazy
    init may still have written once; no SECOND write occurs. Mapped to HTTP 200
    (CLI exit 0). (BLOCKING quirk #1)
  - `evolution-monitor` is META-scoped only (writes solely meta/backpressure.yaml
    at create time; reads no world/agent files). The `agent` field is provenance
    from body/header, NOT a path-resolution dependency. (BLOCKING quirk #2)
  - `evolution-check` re-samples each evolution monitor by spawning
    evolution-snapshot-metrics.py (intentionally returns rc=64 -> empty vector ->
    monitor skipped, no counter change). Its rollback/graduation execution path
    (history.py restore + world evolution-stream append + board post + email) is
    therefore DEAD until the F3 snapshot registry lands; replicated faithfully
    for completeness but only the empty path is exercised (matching the CLI).
  - cmd_check skips monitors whose monitor_kind is an EVOLUTION_KIND (disjoint
    schema; checked by evolution-check, not here) — without the skip, KeyError on
    goals_since_change (g-115-1277). evolution-check symmetrically skips
    non-evolution monitors.
  - bad float (check learning_value / monitor baseline) -> 400; bad
    baseline-vector JSON (evolution-monitor) -> 400 (CLI sys.exit 2).

BYTE-COMPATIBILITY:
  - monitor/graduate/evolution-monitor: json.dumps(obj) (default ascii)+"\n",
    timestamp-free. Eviction notices go to stderr -> never affect stdout.
  - check/status/cooldown-check/evolution-check: json.dumps(obj,
    ensure_ascii=False, default=str)+"\n".
  - The written backpressure.yaml carries `created`/`*_at` stamps -> the test
    normalises them.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402


EVOLUTION_KINDS = {"self_evolution", "program_evolution", "skill_evolution", "rule_evolution"}

_KIND_TO_SNAPSHOT = {
    "self_evolution": "agent_self",
    "program_evolution": "program",
    "skill_evolution": "skill_edit",
    "rule_evolution": "rule_edit",
}
_KIND_TO_STREAM = {
    "self_evolution": "self-evolution.jsonl",
    "program_evolution": "program-evolution.jsonl",
    "skill_evolution": "skill-evolution.jsonl",
    "rule_evolution": "rule-evolution.jsonl",
}
_FILE_KIND_TO_ROLLBACK_BOARD_TYPE = {
    "agent_self": "self-rollback",
    "program": "program-rollback",
    "skill_edit": "skill-rollback",
    "rule_edit": "rule-rollback",
}

_BACKPRESSURE_DEFAULTS = {
    "regression_window": 5,
    "graduation_window": 15,
    "baseline_tolerance": -0.10,
    "max_active_monitors": 5,
}


# ---------------------------------------------------------------------------
# Path + IO helpers
# ---------------------------------------------------------------------------

def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _bp_path(ctx) -> Path:
    return ctx.paths.meta / "backpressure.yaml"


def _scripts_dir(ctx) -> Path:
    return ctx.paths.project_root / "core" / "scripts"


def _config_meta(ctx) -> Path:
    return ctx.paths.project_root / "core" / "config" / "meta.yaml"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _atomic_write_yaml(path: Path, data: Any) -> None:
    assert_not_cruft(path.parent, "mkdir (meta_backpressure)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_meta_backpressure_write")


def _persist(ctx, path: Path, data: Any) -> None:
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    assert_not_cruft(path.parent, "mkdir (meta_backpressure)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(path):
        _validate_no_surrogates(data, path)
        history.snapshot(path, base_dir, agent)
        _atomic_write_yaml(path, data)
        changelog.append(base_dir, agent, path, "edit")


def _ensure_state(ctx) -> Dict[str, Any]:
    path = _bp_path(ctx)
    data = _read_yaml(path)
    if "version" not in data:
        data = {"version": 1, "active_monitors": [], "rollback_history": []}
        _persist(ctx, path, data)
    return data


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


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
    except (ValueError, TypeError):
        pass
    try:
        return float(raw)
    except (ValueError, TypeError):
        pass
    return raw


def _coerce_value(raw):
    """Daemon body values arrive already-typed; strings go through _parse_value
    so JSON `"5"` and bare `5` both land as the CLI's --old/--new would."""
    if isinstance(raw, str):
        return _parse_value(raw)
    return raw


def _load_config(ctx) -> Dict[str, Any]:
    meta_config = _config_meta(ctx)
    if not meta_config.exists():
        return dict(_BACKPRESSURE_DEFAULTS)
    with open(meta_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return config.get("strategy_schemas", {}).get("backpressure", dict(_BACKPRESSURE_DEFAULTS))


def _load_evolution_config(ctx) -> Dict[str, Any]:
    meta_config = _config_meta(ctx)
    defaults = {
        "max_active_monitors": 15,
        "aggregation": {
            "worst_case_floor": 0.15, "majority_threshold": 0.10,
            "majority_count_self": 8, "majority_count_program": 11,
            "majority_count_skill": 6, "majority_count_rule": 5,
        },
        "per_kind": {
            "self_evolution": {"regression_window": 8, "graduation_window": 25, "signal_count": 15},
            "program_evolution": {"regression_window": 6, "graduation_window": 50, "signal_count": 21},
            "skill_evolution": {"regression_window": 6, "graduation_window": 15, "signal_count": 10},
            "rule_evolution": {"regression_window": 6, "graduation_window": 20, "signal_count": 9},
        },
        "dead_end_threshold": 3, "dead_end_window_days": 60,
    }
    if not meta_config.exists():
        return defaults
    with open(meta_config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("strategy_schemas", {}).get("self_program_backpressure", defaults)


# ---------------------------------------------------------------------------
# POST /v1/meta/backpressure/monitor
# ---------------------------------------------------------------------------

def _body(ctx):
    return json.loads(ctx.body.decode("utf-8")) if ctx.body else {}


def monitor(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = _body(ctx)
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    for req in ("change_id", "file", "field", "old", "new", "baseline"):
        if body.get(req) is None:
            return Response.error(400, "missing_param", "{} is required".format(req))
    try:
        baseline = float(body["baseline"])
    except (ValueError, TypeError):
        return Response.error(400, "invalid_param", "baseline must be a float")

    config = _load_config(ctx)
    data = _ensure_state(ctx)
    monitors = data.get("active_monitors", [])

    if len(monitors) >= config.get("max_active_monitors", 5):
        monitors.pop(0)  # eviction notice is stderr-only in CLI -> stdout unaffected

    monitors.append({
        "meta_change_id": body["change_id"],
        "strategy_file": body["file"],
        "field": body["field"],
        "old_value": _coerce_value(body["old"]),
        "new_value": _coerce_value(body["new"]),
        "baseline_imp_k": baseline,
        "goals_since_change": 0,
        "imp_k_samples": [],
        "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": _now(),
    })
    data["active_monitors"] = monitors
    _persist(ctx, _bp_path(ctx), data)
    return Response.text(
        json.dumps({"status": "created", "meta_change_id": body["change_id"]}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/backpressure/check
# ---------------------------------------------------------------------------

def _check_dead_end_candidates(data, newly_rolled_fields):
    history_rb = data.get("rollback_history", [])
    field_rollbacks = Counter()
    for rb in history_rb:
        field_rollbacks["{}:{}".format(rb["strategy_file"], rb["field"])] += 1
    candidates = []
    for key in newly_rolled_fields:
        count = field_rollbacks.get(key, 0)
        if count >= 2:
            file_part, field_part = key.split(":", 1)
            evidence = [rb["meta_change_id"] for rb in history_rb
                        if "{}:{}".format(rb["strategy_file"], rb["field"]) == key]
            failed_values = [rb.get("failed_value") for rb in history_rb
                             if "{}:{}".format(rb["strategy_file"], rb["field"]) == key
                             and rb.get("failed_value") is not None]
            candidates.append({"strategy_file": file_part, "field": field_part,
                               "rollback_count": count, "evidence": evidence,
                               "failed_values": failed_values})
    return candidates


def check(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = _body(ctx)
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

    config = _load_config(ctx)
    data = _ensure_state(ctx)
    regression_window = config.get("regression_window", 5)
    graduation_window = config.get("graduation_window", 15)
    baseline_tolerance = config.get("baseline_tolerance", -0.10)
    audit_only_fields = config.get("audit_only_fields", {}) or {}

    rollback_actions: List[Any] = []
    graduated: List[Any] = []
    audit_only_skipped: List[Any] = []
    monitors = data.get("active_monitors", [])

    for mon in monitors:
        if mon["status"] != "monitoring":
            continue
        if mon.get("monitor_kind") in EVOLUTION_KINDS:
            continue

        mon["goals_since_change"] += 1
        mon["imp_k_samples"].append(learning_value)
        baseline = mon["baseline_imp_k"]
        threshold = baseline + baseline_tolerance
        if learning_value < threshold:
            mon["consecutive_below_baseline"] += 1
            mon["consecutive_above_baseline"] = 0
        else:
            mon["consecutive_below_baseline"] = 0
            mon["consecutive_above_baseline"] += 1

        if mon["consecutive_below_baseline"] >= regression_window:
            file_audit = audit_only_fields.get(mon["strategy_file"], []) or []
            if mon["field"] in file_audit:
                mon["status"] = "audit_only_skipped"
                skip = {
                    "meta_change_id": mon["meta_change_id"],
                    "strategy_file": mon["strategy_file"], "field": mon["field"],
                    "would_have_rolled_back_to": mon["old_value"],
                    "failed_value": mon["new_value"], "skipped_at": _now(),
                    "reason": ("audit_only_field — backpressure refused to roll back "
                               "append-only audit log "
                               "({}::{}); see meta.yaml backpressure.audit_only_fields "
                               "and rb-504 / g-115-204".format(
                                   mon["strategy_file"], mon["field"])),
                    "imp_k_at_check": learning_value,
                    "goals_measured": mon["goals_since_change"],
                }
                audit_only_skipped.append(skip)
                data.setdefault("audit_only_skips", []).append(skip)
            else:
                mon["status"] = "rolled_back"
                vel = _read_yaml(ctx.paths.meta / "improvement-velocity.yaml")
                total_goals_now = len(vel.get("entries", []))
                rollback = {
                    "meta_change_id": mon["meta_change_id"],
                    "strategy_file": mon["strategy_file"], "field": mon["field"],
                    "rollback_to": mon["old_value"], "failed_value": mon["new_value"],
                    "rolled_back_at": _now(),
                    "reason": "{} consecutive goals below baseline ({:.4f} + tolerance {})".format(
                        mon["consecutive_below_baseline"], baseline, baseline_tolerance),
                    "imp_k_at_rollback": learning_value,
                    "goals_measured": mon["goals_since_change"],
                    "total_goals_at_rollback": total_goals_now,
                }
                rollback_actions.append(rollback)
                data.setdefault("rollback_history", []).append(rollback)
        elif mon["consecutive_above_baseline"] >= graduation_window:
            mon["status"] = "graduated"
            graduated.append(mon["meta_change_id"])

    data["active_monitors"] = [m for m in monitors if m["status"] == "monitoring"]
    _persist(ctx, _bp_path(ctx), data)

    newly_rolled_fields = {"{}:{}".format(a["strategy_file"], a["field"])
                           for a in rollback_actions}
    dead_end_candidates = (_check_dead_end_candidates(data, newly_rolled_fields)
                           if newly_rolled_fields else [])

    result = {
        "rollback_actions": rollback_actions, "graduated": graduated,
        "dead_end_candidates": dead_end_candidates,
        "audit_only_skipped": audit_only_skipped,
        "active_monitors_count": len(data["active_monitors"]),
    }
    return Response.text(
        json.dumps(result, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/backpressure/graduate
# ---------------------------------------------------------------------------

def graduate(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = _body(ctx)
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    change_id = body.get("change_id")
    if not change_id:
        return Response.error(400, "missing_param", "change_id is required")

    data = _ensure_state(ctx)
    monitors = data.get("active_monitors", [])
    found = False
    for mon in monitors:
        if mon["meta_change_id"] == change_id:
            mon["status"] = "graduated"
            found = True
            break

    if not found:
        # BLOCKING quirk #1: return BEFORE write_yaml — must NOT persist.
        return Response.text(
            json.dumps({"error": "Monitor {} not found".format(change_id)}) + "\n",
            content_type="application/json")

    data["active_monitors"] = [m for m in monitors if m["status"] == "monitoring"]
    _persist(ctx, _bp_path(ctx), data)
    return Response.text(
        json.dumps({"status": "graduated", "meta_change_id": change_id}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/backpressure/status
# ---------------------------------------------------------------------------

def status(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    data = _ensure_state(ctx)
    result = {
        "active_monitors": data.get("active_monitors", []),
        "rollback_history": data.get("rollback_history", []),
        "audit_only_skips": data.get("audit_only_skips", []),
        "active_count": len(data.get("active_monitors", [])),
        "total_rollbacks": len(data.get("rollback_history", [])),
        "total_audit_only_skips": len(data.get("audit_only_skips", [])),
    }
    return Response.text(
        json.dumps(result, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/backpressure/cooldown-check
# ---------------------------------------------------------------------------

def cooldown_check(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    data = _ensure_state(ctx)
    history_rb = data.get("rollback_history", [])
    window_raw = ctx.query.get("window")
    try:
        cooldown_window = int(window_raw) if window_raw else 20
    except (ValueError, TypeError):
        return Response.error(400, "invalid_param", "window must be an integer")

    vel = _read_yaml(ctx.paths.meta / "improvement-velocity.yaml")
    total_goals = len(vel.get("entries", []))

    in_cooldown = []
    for rb in history_rb:
        rollback_at = rb.get("total_goals_at_rollback")
        if rollback_at is None:
            continue
        goals_since_rollback = total_goals - rollback_at
        if goals_since_rollback < cooldown_window:
            in_cooldown.append({
                "strategy_file": rb["strategy_file"], "field": rb["field"],
                "rolled_back_at": rb["rolled_back_at"],
                "goals_since_rollback": goals_since_rollback,
            })
    result = {"in_cooldown": in_cooldown, "cooldown_window": cooldown_window}
    return Response.text(
        json.dumps(result, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Evolution aggregation helpers (verbatim, ctx-free pure functions)
# ---------------------------------------------------------------------------

def _majority_count(kind, ev_cfg):
    agg = ev_cfg.get("aggregation", {})
    return {
        "self_evolution": agg.get("majority_count_self", 8),
        "program_evolution": agg.get("majority_count_program", 11),
        "skill_evolution": agg.get("majority_count_skill", 6),
        "rule_evolution": agg.get("majority_count_rule", 5),
    }.get(kind, 999)


def _aggregate_vote(baseline, current, kind, ev_cfg):
    agg = ev_cfg.get("aggregation", {})
    worst_floor = float(agg.get("worst_case_floor", 0.15))
    majority_threshold = float(agg.get("majority_threshold", 0.10))
    majority_count = _majority_count(kind, ev_cfg)
    worst_drop = 0.0
    worst_signal = None
    majority_below = 0
    for name, b in (baseline or {}).items():
        try:
            b_val = float(b)
            c_val = float(current.get(name, b_val))
        except (TypeError, ValueError):
            continue
        drop = b_val - c_val
        if drop > worst_drop:
            worst_drop = drop
            worst_signal = name
        if drop >= majority_threshold:
            majority_below += 1
    is_below = worst_drop >= worst_floor or majority_below >= majority_count
    return {"vote": "below" if is_below else "above", "worst_signal": worst_signal,
            "worst_drop": round(worst_drop, 4), "majority_below": majority_below}


def _sample_vector(ctx, kind, file_path, agent):
    snap_file_kind = _KIND_TO_SNAPSHOT.get(kind)
    if not snap_file_kind:
        return {}
    script = _scripts_dir(ctx) / "evolution-snapshot-metrics.py"
    cmd = ["py", "-3", str(script), "--file-kind", snap_file_kind]
    if file_path:
        cmd += ["--file-path", file_path]
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if result.returncode == 64 or result.returncode != 0:
        # rc=64: snapshot-metrics intentionally unimplemented (F3 pending).
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _post_rollback_board(ctx, rollback_entry):
    file_kind = rollback_entry.get("file_kind", "")
    board_type = _FILE_KIND_TO_ROLLBACK_BOARD_TYPE.get(file_kind, "evolution-rollback")
    file_path = rollback_entry.get("file_path") or "?"
    revision_id = rollback_entry.get("revision_id") or "?"
    agent = rollback_entry.get("agent") or os.environ.get("MIND_AGENT", "")
    reasoning = (rollback_entry.get("reasoning") or "").strip()
    body = ("**{}** — `{}`\n\n- rollback revision: `{}`\n- previous revision: `{}`\n"
            "- agent: {}\n\n{}".format(board_type, file_path, revision_id,
                                       rollback_entry.get("previous_revision_id"),
                                       agent, reasoning))
    tags = ",".join(t for t in (agent, Path(file_path).stem, "rollback") if t)
    bp_script = str(_scripts_dir(ctx) / "board.py")
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(
            ["py", "-3", bp_script, "post", "--channel", "decisions",
             "--type", board_type, "--tags", tags],
            input=body, capture_output=True, text=True, env=env, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    msg_id = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else None
    if not msg_id or not msg_id.startswith("msg-"):
        return None
    return msg_id


def _email_rollback(ctx, rollback_entry):
    file_kind = rollback_entry.get("file_kind", "")
    file_path = rollback_entry.get("file_path") or "?"
    revision_id = rollback_entry.get("revision_id") or "?"
    agent = rollback_entry.get("agent") or os.environ.get("MIND_AGENT", "")
    reasoning = (rollback_entry.get("reasoning") or "").strip()
    subject = "Auto-rollback: {} {}".format(file_kind, file_path)
    body = ("Backpressure auto-rolled back a recent edit to {}.\n\nRollback revision: {}\n"
            "Previous revision: {}\nAgent: {}\n\n{}\n\nThis is a post-rollback "
            "notification. The original edit is preserved in the event stream for "
            "audit; the file has been restored to its pre-edit state from "
            ".history/.".format(file_path, revision_id,
                                rollback_entry.get("previous_revision_id"), agent, reasoning))
    if os.environ.get("MIND_EVOLUTION_NOTIFY_DRYRUN", "").strip() in ("1", "true", "yes"):
        return "dry-run:rollback:{}".format(revision_id)
    payload = {"InfoType": "Self-Program-Evolution-Rollback", "Title": subject,
               "InfoMessage": subject, "Body": body}
    email_script = (ctx.paths.world / "scripts" / "email-send.sh").as_posix()
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        from _runtime_bash import bash_cmd  # Windows-safe bash resolution ()
        result = subprocess.run(bash_cmd(email_script), input=json.dumps(payload),
                                capture_output=True, text=True, env=env, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return "email:rollback:{}".format(revision_id)


def _evolution_rollback(ctx, mon, ev_cfg, current_vector, vote):
    history_snapshot = mon.get("history_snapshot")
    file_path = mon.get("file_path")
    kind = mon["monitor_kind"]
    rolled_back = False
    rollback_error = None
    if history_snapshot and file_path:
        snapshot_path = Path(history_snapshot)
        version_name = snapshot_path.name
        history_script = _scripts_dir(ctx) / "history.py"
        try:
            result = subprocess.run(
                ["py", "-3", str(history_script), "restore", file_path, version_name],
                capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                rolled_back = True
            else:
                rollback_error = result.stderr.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            rollback_error = str(exc)
    else:
        rollback_error = "history_snapshot or file_path missing on monitor"

    rollback = {
        "revision_id": mon.get("revision_id"), "monitor_kind": kind, "file_path": file_path,
        "agent": mon.get("agent"), "history_snapshot": history_snapshot,
        "rolled_back": rolled_back, "rollback_error": rollback_error,
        "rolled_back_at": _now(), "worst_signal": vote.get("worst_signal"),
        "worst_drop": vote.get("worst_drop"), "majority_below": vote.get("majority_below"),
        "consecutive_below": mon.get("consecutive_below_baseline", 0),
        "current_vector": current_vector, "baseline_vector": mon.get("baseline"),
    }
    stream_name = _KIND_TO_STREAM.get(kind)
    if stream_name and rolled_back:
        try:
            from _fileops import locked_append_jsonl
            entry = {
                "revision_id": "{}-rollback".format(mon.get("revision_id")),
                "previous_revision_id": mon.get("revision_id"), "ts": rollback["rolled_back_at"],
                "file_kind": _KIND_TO_SNAPSHOT.get(kind), "file_path": file_path,
                "agent": mon.get("agent"), "change_class": "rollback",
                "before_hash": None, "after_hash": None,
                "history_snapshot": history_snapshot, "signal_source": "backpressure-rollback",
                "signal_evidence": [{"type": "metric_vector",
                                     "worst_signal": vote.get("worst_signal"),
                                     "worst_drop": vote.get("worst_drop"),
                                     "majority_below": vote.get("majority_below")}],
                "reasoning": ("Auto-rollback: {} consecutive degraded windows; worst signal "
                              "{} dropped {}; {} signals below threshold.".format(
                                  mon.get("consecutive_below_baseline"),
                                  vote.get("worst_signal"), vote.get("worst_drop"),
                                  vote.get("majority_below"))),
                "status": "final",
            }
            locked_append_jsonl(ctx.paths.world / stream_name, entry)
            rollback["board_post_id"] = _post_rollback_board(ctx, entry)
            rollback["user_notify_ref"] = _email_rollback(ctx, entry)
        except Exception as exc:  # noqa: BLE001 — faithful to CLI broad catch
            rollback["stream_append_error"] = str(exc)
    return rollback


# ---------------------------------------------------------------------------
# POST /v1/meta/backpressure/evolution-monitor  (META-scoped, quirk #2)
# ---------------------------------------------------------------------------

def evolution_monitor(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = _body(ctx)
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    kind = body.get("monitor_kind")
    if kind not in EVOLUTION_KINDS:
        return Response.error(400, "invalid_param",
                              "monitor_kind must be one of {}".format(sorted(EVOLUTION_KINDS)))
    for req in ("revision_id", "file_path", "history_snapshot"):
        if body.get(req) is None:
            return Response.error(400, "missing_param", "{} is required".format(req))
    baseline_raw = body.get("baseline_vector")
    if baseline_raw is None:
        return Response.error(400, "missing_param", "baseline_vector is required")
    # baseline_vector may arrive as a JSON string (CLI parity) or an object.
    if isinstance(baseline_raw, str):
        try:
            baseline = json.loads(baseline_raw) if baseline_raw else {}
        except json.JSONDecodeError as exc:
            return Response.error(400, "invalid_param",
                                  "baseline_vector must be JSON: {}".format(exc))
    elif isinstance(baseline_raw, dict):
        baseline = baseline_raw
    else:
        return Response.error(400, "invalid_param", "baseline_vector must be a JSON object")

    agent = body.get("agent")
    if agent is None:
        agent = (ctx.headers.get("x-mind-agent") or "").strip() or None

    ev_cfg = _load_evolution_config(ctx)
    data = _ensure_state(ctx)
    monitors = data.get("active_monitors", [])
    cap = ev_cfg.get("max_active_monitors", 15)
    if len(monitors) >= cap:
        for i, m in enumerate(monitors):
            if m.get("monitor_kind") in EVOLUTION_KINDS:
                monitors.pop(i)  # eviction notice is stderr-only
                break

    monitors.append({
        "monitor_kind": kind, "revision_id": body["revision_id"],
        "file_path": body["file_path"], "agent": agent,
        "history_snapshot": body["history_snapshot"], "baseline": baseline,
        "metric_samples": [], "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0, "status": "monitoring", "created": _now(),
    })
    data["active_monitors"] = monitors
    _persist(ctx, _bp_path(ctx), data)
    return Response.text(
        json.dumps({"status": "created", "monitor_kind": kind,
                    "revision_id": body["revision_id"], "signal_count": len(baseline)}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/backpressure/evolution-check
# ---------------------------------------------------------------------------

def evolution_check(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    ev_cfg = _load_evolution_config(ctx)
    data = _ensure_state(ctx)
    monitors = data.get("active_monitors", [])
    per_kind = ev_cfg.get("per_kind", {})
    rollback_actions = []
    graduated = []

    for mon in monitors:
        kind = mon.get("monitor_kind")
        if kind not in EVOLUTION_KINDS:
            continue
        if mon.get("status") != "monitoring":
            continue
        kind_cfg = per_kind.get(kind, {})
        regression_window = kind_cfg.get("regression_window", 6)
        graduation_window = kind_cfg.get("graduation_window", 15)

        current = _sample_vector(ctx, kind, mon.get("file_path"), mon.get("agent"))
        if not current:
            continue  # sampling failed (rc=64) — do NOT count against the monitor
        mon["metric_samples"].append({"ts": _now(), "vector": current})
        keep = graduation_window + regression_window + 5
        mon["metric_samples"] = mon["metric_samples"][-keep:]

        vote = _aggregate_vote(mon.get("baseline", {}), current, kind, ev_cfg)
        if vote["vote"] == "below":
            mon["consecutive_below_baseline"] += 1
            mon["consecutive_above_baseline"] = 0
        else:
            mon["consecutive_above_baseline"] += 1
            mon["consecutive_below_baseline"] = 0

        if mon["consecutive_below_baseline"] >= regression_window:
            mon["status"] = "rolled_back"
            rb = _evolution_rollback(ctx, mon, ev_cfg, current, vote)
            rollback_actions.append(rb)
            data.setdefault("rollback_history", []).append(rb)
        elif mon["consecutive_above_baseline"] >= graduation_window:
            mon["status"] = "graduated"
            graduated.append({"revision_id": mon.get("revision_id"), "monitor_kind": kind,
                              "file_path": mon.get("file_path"),
                              "samples_collected": len(mon["metric_samples"])})

    data["active_monitors"] = [m for m in monitors
                               if m.get("status") == "monitoring"
                               or m.get("monitor_kind") not in EVOLUTION_KINDS]
    _persist(ctx, _bp_path(ctx), data)

    return Response.text(
        json.dumps({"rollback_actions": rollback_actions, "graduated": graduated,
                    "active_monitors_count": len([m for m in data["active_monitors"]
                                                  if m.get("monitor_kind") in EVOLUTION_KINDS])},
                   ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/meta/backpressure/monitor")] = monitor
    routes[("POST", "/v1/meta/backpressure/check")] = check
    routes[("POST", "/v1/meta/backpressure/graduate")] = graduate
    routes[("GET", "/v1/meta/backpressure/status")] = status
    routes[("GET", "/v1/meta/backpressure/cooldown-check")] = cooldown_check
    routes[("POST", "/v1/meta/backpressure/evolution-monitor")] = evolution_monitor
    routes[("POST", "/v1/meta/backpressure/evolution-check")] = evolution_check
