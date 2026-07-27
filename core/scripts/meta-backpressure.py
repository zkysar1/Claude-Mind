#!/usr/bin/env python3
"""Backpressure gate for meta-strategy changes.

Monitors meta-strategy changes and auto-reverts when performance regresses.
Inspired by AutoContext's backpressure gate system.

Subcommands:
  monitor   — create a new monitor for a meta-strategy change
  check     — check all active monitors against a new learning_value
  graduate  — stop monitoring a change (performance sustained)
  status    — list all active monitors and rollback history
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR, CONFIG_DIR, WORLD_DIR, PROJECT_ROOT
from _runtime_bash import bash_cmd  # : Windows-safe bash resolution


def read_yaml(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write YAML with locking and history."""
    from _fileops import locked_write_yaml
    locked_write_yaml(path, data)


def load_config():
    """Load backpressure config from core/config/meta.yaml."""
    meta_config = CONFIG_DIR / "meta.yaml"
    if not meta_config.exists():
        return {
            "regression_window": 5,
            "graduation_window": 15,
            "baseline_tolerance": -0.10,
            "max_active_monitors": 5,
        }
    with open(meta_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("strategy_schemas", {}).get("backpressure", {
        "regression_window": 5,
        "graduation_window": 15,
        "baseline_tolerance": -0.10,
        "max_active_monitors": 5,
    })


BP_PATH = META_DIR / "backpressure.yaml"


def ensure_state():
    """Ensure backpressure.yaml exists with valid structure."""
    data = read_yaml(BP_PATH)
    if "version" not in data:
        data = {
            "version": 1,
            "active_monitors": [],
            "rollback_history": [],
        }
        write_yaml(BP_PATH, data)
    return data


def cmd_monitor(args):
    """Create a new monitor for a meta-strategy change."""
    config = load_config()
    data = ensure_state()

    monitors = data.get("active_monitors", [])

    # Enforce max_active_monitors
    if len(monitors) >= config.get("max_active_monitors", 5):
        evicted_id = monitors[0].get("meta_change_id", "?")
        monitors.pop(0)
        print(f"BACKPRESSURE: Evicted oldest monitor {evicted_id} (cap reached)", file=sys.stderr)

    monitor = {
        "meta_change_id": args.change_id,
        "strategy_file": args.file,
        "field": args.field,
        "old_value": _parse_value(args.old),
        "new_value": _parse_value(args.new),
        "baseline_imp_k": float(args.baseline),
        "goals_since_change": 0,
        "imp_k_samples": [],
        "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    monitors.append(monitor)
    data["active_monitors"] = monitors
    write_yaml(BP_PATH, data)

    print(json.dumps({"status": "created", "meta_change_id": args.change_id}))


def cmd_check(args):
    """Check all active monitors against a new learning_value.

    Returns JSON with any rollback actions needed.
    """
    config = load_config()
    data = ensure_state()

    learning_value = float(args.learning_value)
    regression_window = config.get("regression_window", 5)
    graduation_window = config.get("graduation_window", 15)
    baseline_tolerance = config.get("baseline_tolerance", -0.10)

    rollback_actions = []
    graduated = []
    audit_only_skipped = []
    monitors = data.get("active_monitors", [])

    #  / rb-504: per-strategy-file allowlist of append-only audit
    # fields. cmd_check refuses to produce rollback_actions for these — the
    # rollback would erase observability without changing system behavior.
    # See core/config/meta.yaml backpressure.audit_only_fields. Missing or
    # empty config → empty dict → legacy unconditional-rollback behavior.
    audit_only_fields = config.get("audit_only_fields", {}) or {}

    for monitor in monitors:
        if monitor["status"] != "monitoring":
            continue
        # Evolution monitors (skill/self/program/rule) share active_monitors but
        # carry a disjoint schema (monitor_kind/revision_id/metric_samples, NOT
        # goals_since_change/imp_k_samples/strategy_file). They are checked by
        # cmd_evolution_check, NOT here. Without this skip, cmd_check raises
        # KeyError on the first evolution monitor (). Symmetric to the
        # "if kind not in EVOLUTION_KINDS: continue" filter in cmd_evolution_check.
        if monitor.get("monitor_kind") in EVOLUTION_KINDS:
            continue

        monitor["goals_since_change"] += 1
        monitor["imp_k_samples"].append(learning_value)

        baseline = monitor["baseline_imp_k"]
        threshold = baseline + baseline_tolerance

        if learning_value < threshold:
            monitor["consecutive_below_baseline"] += 1
            monitor["consecutive_above_baseline"] = 0
        else:
            monitor["consecutive_below_baseline"] = 0
            monitor["consecutive_above_baseline"] += 1

        # Check for regression → rollback
        if monitor["consecutive_below_baseline"] >= regression_window:
            file_audit = audit_only_fields.get(monitor["strategy_file"], []) or []
            if monitor["field"] in file_audit:
                # Audit-only field: skip rollback, mark monitor closed, record
                # the skip for parity with rollback_history. Removed from the
                # active list in the post-loop filter so the same monitor does
                # not re-trigger the skip path on every subsequent check call.
                monitor["status"] = "audit_only_skipped"
                skip = {
                    "meta_change_id": monitor["meta_change_id"],
                    "strategy_file": monitor["strategy_file"],
                    "field": monitor["field"],
                    "would_have_rolled_back_to": monitor["old_value"],
                    "failed_value": monitor["new_value"],
                    "skipped_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "reason": (
                        f"audit_only_field — backpressure refused to roll back "
                        f"append-only audit log "
                        f"({monitor['strategy_file']}::{monitor['field']}); "
                        f"see meta.yaml backpressure.audit_only_fields and "
                        f"rb-504 / g-115-204"
                    ),
                    "imp_k_at_check": learning_value,
                    "goals_measured": monitor["goals_since_change"],
                }
                audit_only_skipped.append(skip)
                data.setdefault("audit_only_skips", []).append(skip)
            else:
                monitor["status"] = "rolled_back"
                # Count total goals for cooldown tracking
                vel = read_yaml(META_DIR / "improvement-velocity.yaml")
                total_goals_now = len(vel.get("entries", []))
                rollback = {
                    "meta_change_id": monitor["meta_change_id"],
                    "strategy_file": monitor["strategy_file"],
                    "field": monitor["field"],
                    "rollback_to": monitor["old_value"],
                    "failed_value": monitor["new_value"],  # the value that caused regression
                    "rolled_back_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "reason": f"{monitor['consecutive_below_baseline']} consecutive goals below baseline ({baseline:.4f} + tolerance {baseline_tolerance})",
                    "imp_k_at_rollback": learning_value,
                    "goals_measured": monitor["goals_since_change"],
                    "total_goals_at_rollback": total_goals_now,
                }
                rollback_actions.append(rollback)
                data.setdefault("rollback_history", []).append(rollback)

        # Check for graduation → sustained improvement
        elif monitor["consecutive_above_baseline"] >= graduation_window:
            monitor["status"] = "graduated"
            graduated.append(monitor["meta_change_id"])

    # Remove rolled-back, audit-only-skipped, and graduated monitors from active list
    data["active_monitors"] = [m for m in monitors if m["status"] == "monitoring"]
    write_yaml(BP_PATH, data)

    # Only report dead end candidates for fields that were JUST rolled back in
    # this check run. Avoids repeated registration on every subsequent call.
    # audit_only_skipped fields intentionally excluded — they are not failures.
    newly_rolled_fields = {f"{a['strategy_file']}:{a['field']}" for a in rollback_actions}
    dead_end_candidates = _check_dead_end_candidates(data, newly_rolled_fields) if newly_rolled_fields else []

    result = {
        "rollback_actions": rollback_actions,
        "graduated": graduated,
        "dead_end_candidates": dead_end_candidates,
        "audit_only_skipped": audit_only_skipped,
        "active_monitors_count": len(data["active_monitors"]),
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def _check_dead_end_candidates(data, newly_rolled_fields):
    """Check if a newly-rolled-back field has been rolled back 2+ times total."""
    history = data.get("rollback_history", [])
    from collections import Counter
    field_rollbacks = Counter()
    for rb in history:
        key = f"{rb['strategy_file']}:{rb['field']}"
        field_rollbacks[key] += 1

    candidates = []
    for key in newly_rolled_fields:
        count = field_rollbacks.get(key, 0)
        if count >= 2:
            file_part, field_part = key.split(":", 1)
            evidence = [rb["meta_change_id"] for rb in history
                        if f"{rb['strategy_file']}:{rb['field']}" == key]
            failed_values = [rb.get("failed_value") for rb in history
                             if f"{rb['strategy_file']}:{rb['field']}" == key
                             and rb.get("failed_value") is not None]
            candidates.append({
                "strategy_file": file_part,
                "field": field_part,
                "rollback_count": count,
                "evidence": evidence,
                "failed_values": failed_values,
            })
    return candidates


def cmd_graduate(args):
    """Graduate a monitor — stop monitoring."""
    data = ensure_state()
    monitors = data.get("active_monitors", [])

    found = False
    for monitor in monitors:
        # active_monitors mixes weight-monitors (meta_change_id) with evolution
        # monitors (monitor_kind/revision_id, NO meta_change_id). .get() skips
        # the latter, mirroring cmd_check's EVOLUTION_KINDS skip ();
        # graduate was the sibling that fix missed — an unguarded subscript
        # KeyError'd the whole endpoint on any co-resident evolution monitor,
        # breaking graduate fleet-wide ().
        if monitor.get("meta_change_id") == args.change_id:
            monitor["status"] = "graduated"
            found = True
            break

    if not found:
        print(json.dumps({"error": f"Monitor {args.change_id} not found"}))
        return

    data["active_monitors"] = [m for m in monitors if m["status"] == "monitoring"]
    write_yaml(BP_PATH, data)
    print(json.dumps({"status": "graduated", "meta_change_id": args.change_id}))


def cmd_status(args):
    """List all active monitors, rollback history, and audit-only skips."""
    data = ensure_state()
    result = {
        "active_monitors": data.get("active_monitors", []),
        "rollback_history": data.get("rollback_history", []),
        "audit_only_skips": data.get("audit_only_skips", []),
        "active_count": len(data.get("active_monitors", [])),
        "total_rollbacks": len(data.get("rollback_history", [])),
        "total_audit_only_skips": len(data.get("audit_only_skips", [])),
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


def cmd_cooldown_check(args):
    """Check if a field is in cooldown (rolled back within last N goals)."""
    data = ensure_state()
    history = data.get("rollback_history", [])
    cooldown_window = int(args.window) if args.window else 20

    vel = read_yaml(META_DIR / "improvement-velocity.yaml")
    total_goals = len(vel.get("entries", []))

    in_cooldown = []
    for rb in history:
        rollback_at = rb.get("total_goals_at_rollback")
        if rollback_at is None:
            continue  # legacy record without tracking — skip
        goals_since_rollback = total_goals - rollback_at
        if goals_since_rollback < cooldown_window:
            in_cooldown.append({
                "strategy_file": rb["strategy_file"],
                "field": rb["field"],
                "rolled_back_at": rb["rolled_back_at"],
                "goals_since_rollback": goals_since_rollback,
            })

    result = {
        "in_cooldown": in_cooldown,
        "cooldown_window": cooldown_window,
    }
    print(json.dumps(result, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Evolution-monitor dispatch (per world/conventions/self-program-evolution.md)
# ---------------------------------------------------------------------------

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


def load_evolution_config():
    """Load self_program_backpressure config block. Falls back to defaults if missing."""
    meta_config = CONFIG_DIR / "meta.yaml"
    defaults = {
        "max_active_monitors": 15,
        "aggregation": {
            "worst_case_floor": 0.15,
            "majority_threshold": 0.10,
            "majority_count_self": 8,
            "majority_count_program": 11,
            "majority_count_skill": 6,
            "majority_count_rule": 5,
        },
        "per_kind": {
            "self_evolution": {"regression_window": 8, "graduation_window": 25, "signal_count": 15},
            "program_evolution": {"regression_window": 6, "graduation_window": 50, "signal_count": 21},
            "skill_evolution": {"regression_window": 6, "graduation_window": 15, "signal_count": 10},
            "rule_evolution": {"regression_window": 6, "graduation_window": 20, "signal_count": 9},
        },
        "dead_end_threshold": 3,
        "dead_end_window_days": 60,
    }
    if not meta_config.exists():
        return defaults
    with open(meta_config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("strategy_schemas", {}).get("self_program_backpressure", defaults)


def _majority_count(kind, ev_cfg):
    """Look up majority_count for this monitor_kind."""
    agg = ev_cfg.get("aggregation", {})
    if kind == "self_evolution":
        return agg.get("majority_count_self", 8)
    if kind == "program_evolution":
        return agg.get("majority_count_program", 11)
    if kind == "skill_evolution":
        return agg.get("majority_count_skill", 6)
    if kind == "rule_evolution":
        return agg.get("majority_count_rule", 5)
    return 999  # unreachable; degrades to "never vote regression"


def _aggregate_vote(baseline, current, kind, ev_cfg):
    """Apply §7.2 + §14.4 aggregation: worst-case OR majority test, binary outcome.

    Binary per bible §7.2 ("Above tolerance × 25 → graduated") — an iteration is
    either below tolerance (regression vote) or above (graduation progress). No
    gray-zone "neutral" state; a single intermediate drop must not stall the
    graduation counter indefinitely.

    Returns dict {"vote": "below"|"above", "worst_signal": str|None,
    "worst_drop": float, "majority_below": int}.
    """
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
    vote = "below" if is_below else "above"

    return {
        "vote": vote,
        "worst_signal": worst_signal,
        "worst_drop": round(worst_drop, 4),
        "majority_below": majority_below,
    }


def _sample_vector(kind, file_path, agent):
    """Invoke evolution-snapshot-metrics.py and return parsed JSON dict."""
    import subprocess
    snap_file_kind = _KIND_TO_SNAPSHOT.get(kind)
    if not snap_file_kind:
        return {}
    script = Path(__file__).parent / "evolution-snapshot-metrics.py"
    cmd = ["py", "-3", str(script), "--file-kind", snap_file_kind]
    if file_path:
        cmd += ["--file-path", file_path]
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: snapshot subprocess failed: {exc}", file=sys.stderr)
        return {}
    if result.returncode == 64:
        # F3: snapshot-metrics is an INTENTIONAL unimplemented placeholder
        # (sentinel rc=64). Known disabled state — return empty vector
        # quietly (no monitor), not a runtime error.
        print("INFO: backpressure vector unavailable — snapshot-metrics "
              "intentionally unimplemented (F3 design goal pending).",
              file=sys.stderr)
        return {}
    if result.returncode != 0:
        print(f"WARN: snapshot script exited {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"WARN: snapshot JSON decode failed: {exc}", file=sys.stderr)
        return {}


def _evolution_rollback(monitor, ev_cfg, current_vector, vote):
    """Execute rollback: restore history snapshot + append rollback entry to stream.

    Returns rollback record dict.
    """
    import subprocess
    history_snapshot = monitor.get("history_snapshot")
    file_path = monitor.get("file_path")
    kind = monitor["monitor_kind"]
    rolled_back = False
    rollback_error = None

    if history_snapshot and file_path:
        snapshot_path = Path(history_snapshot)
        version_name = snapshot_path.name
        history_script = Path(__file__).parent / "history.py"
        try:
            result = subprocess.run(
                ["py", "-3", str(history_script), "restore", file_path, version_name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                rolled_back = True
            else:
                rollback_error = result.stderr.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            rollback_error = str(exc)
    else:
        rollback_error = "history_snapshot or file_path missing on monitor"

    rollback = {
        "revision_id": monitor.get("revision_id"),
        "monitor_kind": kind,
        "file_path": file_path,
        "agent": monitor.get("agent"),
        "history_snapshot": history_snapshot,
        "rolled_back": rolled_back,
        "rollback_error": rollback_error,
        "rolled_back_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "worst_signal": vote.get("worst_signal"),
        "worst_drop": vote.get("worst_drop"),
        "majority_below": vote.get("majority_below"),
        "consecutive_below": monitor.get("consecutive_below_baseline", 0),
        "current_vector": current_vector,
        "baseline_vector": monitor.get("baseline"),
    }

    stream_name = _KIND_TO_STREAM.get(kind)
    if stream_name and rolled_back:
        try:
            from _fileops import locked_append_jsonl
            entry = {
                "revision_id": f"{monitor.get('revision_id')}-rollback",
                "previous_revision_id": monitor.get("revision_id"),
                "ts": rollback["rolled_back_at"],
                "file_kind": _KIND_TO_SNAPSHOT.get(kind),
                "file_path": file_path,
                "agent": monitor.get("agent"),
                "change_class": "rollback",
                "before_hash": None,
                "after_hash": None,
                "history_snapshot": history_snapshot,
                "signal_source": "backpressure-rollback",
                "signal_evidence": [{
                    "type": "metric_vector",
                    "worst_signal": vote.get("worst_signal"),
                    "worst_drop": vote.get("worst_drop"),
                    "majority_below": vote.get("majority_below"),
                }],
                "reasoning": (
                    f"Auto-rollback: {monitor.get('consecutive_below_baseline')} consecutive "
                    f"degraded windows; worst signal {vote.get('worst_signal')} dropped "
                    f"{vote.get('worst_drop')}; {vote.get('majority_below')} signals below threshold."
                ),
                "status": "final",
            }
            locked_append_jsonl(WORLD_DIR / stream_name, entry)
            # Phase 5.3: board post + email notification on rollback
            rollback["board_post_id"] = _post_rollback_board(entry)
            rollback["user_notify_ref"] = _email_rollback(entry)
        except Exception as exc:
            rollback["stream_append_error"] = str(exc)

    return rollback


def _post_rollback_board(rollback_entry):
    """Post a decisions-channel entry for an auto-rollback. Returns msg ID or None."""
    import subprocess
    file_kind = rollback_entry.get("file_kind", "")
    board_type = _FILE_KIND_TO_ROLLBACK_BOARD_TYPE.get(file_kind, "evolution-rollback")
    file_path = rollback_entry.get("file_path") or "?"
    revision_id = rollback_entry.get("revision_id") or "?"
    agent = rollback_entry.get("agent") or os.environ.get("MIND_AGENT", "")
    reasoning = (rollback_entry.get("reasoning") or "").strip()

    body = (
        f"**{board_type}** — `{file_path}`\n\n"
        f"- rollback revision: `{revision_id}`\n"
        f"- previous revision: `{rollback_entry.get('previous_revision_id')}`\n"
        f"- agent: {agent}\n\n"
        f"{reasoning}"
    )
    tags = ",".join(t for t in (agent, Path(file_path).stem, "rollback") if t)
    # Call board.py directly (rb-370 — bypass bash shim on Windows).
    bp_script = str(Path(__file__).parent / "board.py")
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(
            ["py", "-3", bp_script, "post", "--channel", "decisions",
             "--type", board_type, "--tags", tags],
            input=body, capture_output=True, text=True, env=env, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: rollback board-post failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"WARN: rollback board-post returned {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    msg_id = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else None
    if not msg_id or not msg_id.startswith("msg-"):
        return None
    return msg_id


def _email_rollback(rollback_entry):
    """Send user-notification email about an auto-rollback. Returns notify ref or None.

    Honors MIND_EVOLUTION_NOTIFY_DRYRUN=1 (skips subprocess, returns dry-run sentinel).
    """
    import subprocess
    import json as _json
    file_kind = rollback_entry.get("file_kind", "")
    file_path = rollback_entry.get("file_path") or "?"
    revision_id = rollback_entry.get("revision_id") or "?"
    agent = rollback_entry.get("agent") or os.environ.get("MIND_AGENT", "")
    reasoning = (rollback_entry.get("reasoning") or "").strip()

    subject = f"Auto-rollback: {file_kind} {file_path}"
    body = (
        f"Backpressure auto-rolled back a recent edit to {file_path}.\n\n"
        f"Rollback revision: {revision_id}\n"
        f"Previous revision: {rollback_entry.get('previous_revision_id')}\n"
        f"Agent: {agent}\n\n"
        f"{reasoning}\n\n"
        f"This is a post-rollback notification. The original edit is preserved in the event "
        f"stream for audit; the file has been restored to its pre-edit state from .history/."
    )

    if os.environ.get("MIND_EVOLUTION_NOTIFY_DRYRUN", "").strip() in ("1", "true", "yes"):
        print(f"NOTIFY DRY-RUN: would email rollback ({subject})", file=sys.stderr)
        return f"dry-run:rollback:{revision_id}"

    payload = {
        "InfoType": "Self-Program-Evolution-Rollback",
        "Title": subject,
        "InfoMessage": subject,
        "Body": body,
        # Provenance stamp — email-send.sh refuses payloads without it ().
        "XPayloadProvenance": "meta-backpressure/v1",
    }
    email_script = (WORLD_DIR / "scripts" / "email-send.sh").as_posix()
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(
            bash_cmd(email_script),
            input=_json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: rollback email-send failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"WARN: rollback email-send returned {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return f"email:rollback:{revision_id}"


def cmd_evolution_monitor(args):
    """Create a new evolution monitor (Self/Program/Skill/Rule edit).

    Stores in the SAME meta/backpressure.yaml file as meta_strategy monitors,
    keyed by revision_id (unique by construction).
    """
    ev_cfg = load_evolution_config()
    data = ensure_state()
    monitors = data.get("active_monitors", [])

    # Enforce max_active_monitors (raised to 15 per D4)
    cap = ev_cfg.get("max_active_monitors", 15)
    if len(monitors) >= cap:
        # Evict oldest evolution monitor (preserve meta_strategy monitors)
        for i, m in enumerate(monitors):
            if m.get("monitor_kind") in EVOLUTION_KINDS:
                evicted_id = m.get("revision_id", "?")
                monitors.pop(i)
                print(f"BACKPRESSURE: Evicted oldest evolution monitor {evicted_id} (cap reached)",
                      file=sys.stderr)
                break

    try:
        baseline = json.loads(args.baseline_vector) if args.baseline_vector else {}
    except json.JSONDecodeError as exc:
        print(f"ERROR: --baseline-vector must be JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    monitor = {
        "monitor_kind": args.monitor_kind,
        "revision_id": args.revision_id,
        "file_path": args.file_path,
        "agent": args.agent,
        "history_snapshot": args.history_snapshot,
        "baseline": baseline,
        "metric_samples": [],
        "consecutive_below_baseline": 0,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    monitors.append(monitor)
    data["active_monitors"] = monitors
    write_yaml(BP_PATH, data)

    print(json.dumps({
        "status": "created",
        "monitor_kind": args.monitor_kind,
        "revision_id": args.revision_id,
        "signal_count": len(baseline),
    }))


def cmd_evolution_check(args):
    """Re-sample metric vector for all active evolution monitors and update counters.

    Fires rollback when consecutive_below_baseline >= regression_window.
    Fires graduation when consecutive_above_baseline >= graduation_window.

    Returns JSON with rollback_actions, graduated, and active_monitors_count.
    """
    ev_cfg = load_evolution_config()
    data = ensure_state()
    monitors = data.get("active_monitors", [])
    per_kind = ev_cfg.get("per_kind", {})

    rollback_actions = []
    graduated = []

    for monitor in monitors:
        kind = monitor.get("monitor_kind")
        if kind not in EVOLUTION_KINDS:
            continue
        if monitor.get("status") != "monitoring":
            continue

        kind_cfg = per_kind.get(kind, {})
        regression_window = kind_cfg.get("regression_window", 6)
        graduation_window = kind_cfg.get("graduation_window", 15)

        # Sample current vector
        current = _sample_vector(kind, monitor.get("file_path"), monitor.get("agent"))
        if not current:
            # Sampling failed — skip this iteration; do NOT count against the monitor.
            continue
        monitor["metric_samples"].append({
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "vector": current,
        })
        # Keep only last graduation_window+regression_window samples (prevent unbounded growth)
        keep = graduation_window + regression_window + 5
        monitor["metric_samples"] = monitor["metric_samples"][-keep:]

        vote = _aggregate_vote(monitor.get("baseline", {}), current, kind, ev_cfg)
        if vote["vote"] == "below":
            monitor["consecutive_below_baseline"] += 1
            monitor["consecutive_above_baseline"] = 0
        else:  # "above" — binary opposite of "below" per §7.2
            monitor["consecutive_above_baseline"] += 1
            monitor["consecutive_below_baseline"] = 0

        if monitor["consecutive_below_baseline"] >= regression_window:
            monitor["status"] = "rolled_back"
            rollback = _evolution_rollback(monitor, ev_cfg, current, vote)
            rollback_actions.append(rollback)
            data.setdefault("rollback_history", []).append(rollback)
        elif monitor["consecutive_above_baseline"] >= graduation_window:
            monitor["status"] = "graduated"
            graduated.append({
                "revision_id": monitor.get("revision_id"),
                "monitor_kind": kind,
                "file_path": monitor.get("file_path"),
                "samples_collected": len(monitor["metric_samples"]),
            })

    # Drop non-monitoring entries (rolled_back, graduated)
    data["active_monitors"] = [m for m in monitors
                               if m.get("status") == "monitoring"
                               or m.get("monitor_kind") not in EVOLUTION_KINDS]
    write_yaml(BP_PATH, data)

    print(json.dumps({
        "rollback_actions": rollback_actions,
        "graduated": graduated,
        "active_monitors_count": len([m for m in data["active_monitors"]
                                       if m.get("monitor_kind") in EVOLUTION_KINDS]),
    }, ensure_ascii=False, default=str))


def _parse_value(raw):
    """Auto-detect type from string."""
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


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Backpressure gate for meta-strategy changes")
    sub = parser.add_subparsers(dest="command", required=True)

    p_mon = sub.add_parser("monitor", help="Create a new monitor")
    p_mon.add_argument("--change-id", required=True, help="Meta change ID (mc-NNN)")
    p_mon.add_argument("--file", required=True, help="Strategy file (relative to meta/)")
    p_mon.add_argument("--field", required=True, help="Dot-notation field path")
    p_mon.add_argument("--old", required=True, help="Old value before change")
    p_mon.add_argument("--new", required=True, help="New value after change")
    p_mon.add_argument("--baseline", required=True, help="Current imp@k baseline")

    p_check = sub.add_parser("check", help="Check monitors against a learning value")
    p_check.add_argument("--learning-value", required=True, help="Learning value from Step 8.8")

    p_grad = sub.add_parser("graduate", help="Graduate a monitor")
    p_grad.add_argument("--change-id", required=True, help="Meta change ID to graduate")

    sub.add_parser("status", help="List monitors and rollback history")

    p_cool = sub.add_parser("cooldown-check", help="Check if fields are in cooldown")
    p_cool.add_argument("--window", default="20", help="Cooldown window in goals (default: 20)")

    # Evolution-monitor commands (per world/conventions/self-program-evolution.md)
    p_evmon = sub.add_parser("evolution-monitor",
                              help="Create a monitor for Self/Program/Skill/Rule edit")
    p_evmon.add_argument("--monitor-kind", required=True,
                          choices=sorted(EVOLUTION_KINDS),
                          help="Edit kind to monitor")
    p_evmon.add_argument("--revision-id", required=True,
                          help="Unique revision ID from <kind>-evolution.jsonl")
    p_evmon.add_argument("--file-path", required=True,
                          help="Edited file path (relative or absolute)")
    p_evmon.add_argument("--agent", default=os.environ.get("MIND_AGENT", "").strip() or None,
                          help="Editing agent (defaults to MIND_AGENT env)")
    p_evmon.add_argument("--history-snapshot", required=True,
                          help="Absolute path to .history/ snapshot file for rollback")
    p_evmon.add_argument("--baseline-vector", required=True,
                          help="JSON dict of signal_name → 0..1 float (from evolution-snapshot-metrics.py)")

    sub.add_parser("evolution-check",
                    help="Re-sample evolution monitors and update counters (fires rollback/graduation)")

    return parser


DISPATCH = {
    "monitor": cmd_monitor,
    "check": cmd_check,
    "graduate": cmd_graduate,
    "status": cmd_status,
    "cooldown-check": cmd_cooldown_check,
    "evolution-monitor": cmd_evolution_monitor,
    "evolution-check": cmd_evolution_check,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
