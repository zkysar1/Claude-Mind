#!/usr/bin/env python3
"""monitor-tick.py -- FW-1b demoted-probe runner ( / ).

Runs ENABLED pure-monitoring probes at their own interval_hours from
iteration-close.sh productivity-check (beside agent-watchdog --tick). Mirrors the
agent-watchdog --tick LOCAL-probe pattern: no daemon, no cloud cron (guard-441),
cross-platform pure file I/O, self-gating via a per-probe last-run marker in
<agent>/session/monitor-tick-state.json (the sibling of watchdog-prev-state.json).

  - NATURAL-GATE (guard-348): core/config/monitor-probes.yaml `enabled_probes` is
    the allowlist. EMPTY => inert (this script scans NOTHING and exits at once).
    The framework ships inert; probes migrate one id at a time (Phase-3 / Apply-3).
  - A clean probe (exit 0) records its last-run and emits NOTHING -- no goal slot.
  - A tripped probe (exit != 0) is converted to ONE deduped goal via
    monitor-finding-convert.py (origin_signal dedup, target_asp routing).

Design: FW-1b monitoring-sweep demotion (g-317-13 / asp-317), implemented in this module.

Importable seam (hermetic test): run_tick(registry_path, state_path, ...) takes an
injectable `probe_runner` (default subprocess) and `on_trip_fn` (default: the real
convert_finding via importlib). test_monitor_tick.py injects both -- no live daemon,
no real probe scripts.

Guards: guard-420 (datetime fromisoformat + Z-strip + tolerant), guard-645 (field
reads with defaults), guard-614 (JSON stdout), guard-759 (no /tmp; state under
<agent>/session/). FAIL-OPEN at every layer: a registry/probe/state/convert error
is logged to stderr and the tick continues; the CLI ALWAYS exits 0 so a tick fault
never aborts the productivity-check phase that invoked it.
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_PROBE_TIMEOUT = 60  # seconds per probe (bounded; a hung probe must not stall the tick)


def _now_iso():
    # Local system time (project convention: never UTC).
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _parse_ts(s):
    """guard-420: tolerant ISO parse (strip trailing Z, swallow malformed)."""
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "").strip())
    except Exception:
        return None


def load_registry(registry_path):
    """Parse monitor-probes.yaml -> (enabled_probes:list, probes_by_id:dict).
    Fail-open: a missing/unreadable/malformed registry yields ([], {}) (inert)."""
    try:
        import yaml
    except Exception as e:  # pragma: no cover
        print("[monitor-tick] WARN: pyyaml unavailable: %s" % e, file=sys.stderr)
        return [], {}
    try:
        raw = Path(registry_path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except FileNotFoundError:
        return [], {}
    except Exception as e:
        print("[monitor-tick] WARN: registry unreadable: %s" % e, file=sys.stderr)
        return [], {}
    enabled = data.get("enabled_probes") or []
    probes = {}
    for p in (data.get("probes") or []):
        if isinstance(p, dict) and p.get("id"):
            probes[str(p["id"])] = p
    return list(enabled), probes


def load_state(state_path):
    """Read the per-probe last-run state. Fail-open to empty (first tick)."""
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"probes": {}}
    except Exception as e:
        print("[monitor-tick] WARN: state unreadable, resetting: %s" % e, file=sys.stderr)
        return {"probes": {}}


def save_state(state_path, state):
    """Atomic write (.tmp + os.replace), guard-759 (under session dir, no /tmp)."""
    p = Path(state_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception as e:
        print("[monitor-tick] WARN: state write failed: %s" % e, file=sys.stderr)


def _due(probe, last_run_iso, now):
    """Due if never run OR elapsed >= interval_hours (guard-645 default interval)."""
    last = _parse_ts(last_run_iso)
    if last is None:
        return True
    try:
        interval_h = float(probe.get("interval_hours") or 24)
    except Exception:
        interval_h = 24.0
    return (now - last) >= dt.timedelta(hours=interval_h)


def _subprocess_runner(script_abs, args, timeout=DEFAULT_PROBE_TIMEOUT):
    """Default probe runner: exec the script, return (exit_code, combined_output).
    A probe exits 0 when clean and non-zero when it trips."""
    cmd = [str(script_abs)] + list(args or [])
    if str(script_abs).endswith(".sh"):
        cmd = ["bash"] + cmd
    elif str(script_abs).endswith(".py"):
        cmd = [sys.executable] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "probe timed out after %ss" % timeout
    except Exception as e:
        # A probe we cannot even launch is NOT a trip (avoid false findings on a
        # bad registry path); report rc=None so run_tick skips conversion.
        return None, "probe launch failed: %s" % e


def _default_on_trip(probe, evidence):
    """Load convert_finding from the hyphen-named converter module and file."""
    conv_path = SCRIPT_DIR / "monitor-finding-convert.py"
    spec = importlib.util.spec_from_file_location("monitor_finding_convert", conv_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = (probe.get("source") or "world")
    return mod.convert_finding(probe, evidence, source=source)


def run_tick(registry_path, state_path, *, now=None, project_root=None,
             probe_runner=None, on_trip_fn=None):
    """Run one monitor-tick. Returns a structured result (guard-614).

    Injection seams (test): `probe_runner(script_abs, args) -> (rc, output)` and
    `on_trip_fn(probe, evidence) -> result_dict`. Defaults exec real scripts /
    file via the daemon converter. Fail-open per probe.
    """
    now = now or dt.datetime.now()
    project_root = Path(project_root) if project_root else SCRIPT_DIR.parent.parent
    probe_runner = probe_runner or _subprocess_runner
    on_trip_fn = on_trip_fn or _default_on_trip

    result = {"inert": False, "due": [], "ran": [], "clean": [], "tripped": [],
              "filed": [], "deduped": [], "skipped_not_due": [], "errors": []}

    enabled, probes = load_registry(registry_path)
    if not enabled:
        result["inert"] = True
        return result  # NATURAL-GATE: empty allowlist -> scan nothing.

    state = load_state(state_path)
    pstate = state.setdefault("probes", {})

    for pid in enabled:
        pid = str(pid)
        probe = probes.get(pid)
        if not probe:
            result["errors"].append({"probe": pid, "error": "enabled but not in `probes:`"})
            continue
        prev = pstate.get(pid, {}) if isinstance(pstate.get(pid), dict) else {}
        if not _due(probe, prev.get("last_run"), now):
            result["skipped_not_due"].append(pid)
            continue
        result["due"].append(pid)

        script_rel = (probe.get("script") or "").strip()
        if not script_rel:
            result["errors"].append({"probe": pid, "error": "no script defined"})
            continue
        script_abs = (project_root / script_rel)
        rc, output = probe_runner(script_abs, probe.get("args") or [])
        result["ran"].append(pid)
        # Record last-run regardless of outcome so interval gating advances even
        # when the probe trips (prevents trip-every-tick from re-running each cycle).
        entry = {"last_run": now.replace(microsecond=0).isoformat()}

        if rc is None:
            # Launch failure: not a clean pass, not a trip. Log; do not convert.
            entry["last_outcome"] = "launch_error"
            result["errors"].append({"probe": pid, "error": output})
        elif rc == 0:
            entry["last_outcome"] = "clean"
            result["clean"].append(pid)
        else:
            entry["last_outcome"] = "tripped"
            result["tripped"].append(pid)
            try:
                conv = on_trip_fn(probe, output) or {}
            except Exception as e:
                conv = {"error": str(e)}
                result["errors"].append({"probe": pid, "error": "convert: %s" % e})
            if conv.get("filed"):
                gid = conv.get("goal_id")
                entry["last_goal_id"] = gid
                result["filed"].append({"probe": pid, "goal_id": gid})
            elif conv.get("deduped"):
                result["deduped"].append(pid)
        pstate[pid] = entry

    save_state(state_path, state)
    return result


def _resolve_state_path():
    """Live --tick: <agent>/session/monitor-tick-state.json via _paths."""
    try:
        from _paths import agent_state_dir, AGENT_NAME
        return Path(agent_state_dir(AGENT_NAME)) / "monitor-tick-state.json"
    except Exception:
        agent = os.environ.get("MIND_AGENT", "")
        root = SCRIPT_DIR.parent.parent
        return root / "agents" / agent / "session" / "monitor-tick-state.json"


def main(argv=None):
    ap = argparse.ArgumentParser(description="FW-1b monitor-tick: run enabled "
                                 "demoted probes at interval; clean=nothing, "
                                 "tripped=one deduped goal. (g-317-13)")
    ap.add_argument("--tick", action="store_true", help="run one tick and exit")
    ap.add_argument("--registry", default=str(SCRIPT_DIR.parent / "config" / "monitor-probes.yaml"))
    ap.add_argument("--state", default="")
    ap.add_argument("--json", action="store_true", help="print the tick result JSON")
    a = ap.parse_args(argv)

    state_path = a.state or str(_resolve_state_path())
    try:
        result = run_tick(a.registry, state_path)
    except Exception as e:
        # Belt-and-suspenders: run_tick is internally fail-open, but never let a
        # tick fault escape to abort the caller's productivity-check.
        print("[monitor-tick] WARN: tick failed: %s" % e, file=sys.stderr)
        result = {"inert": False, "errors": [{"error": str(e)}]}
    if a.json or not result.get("inert"):
        # Quiet on the common inert case unless --json; otherwise emit a terse summary.
        if a.json:
            print(json.dumps(result))
        elif result.get("filed") or result.get("tripped") or result.get("errors"):
            print("[monitor-tick] ran=%s clean=%s tripped=%s filed=%s deduped=%s" % (
                len(result.get("ran", [])), len(result.get("clean", [])),
                len(result.get("tripped", [])), len(result.get("filed", [])),
                len(result.get("deduped", []))))
    raise SystemExit(0)  # FAIL-OPEN: always 0.


if __name__ == "__main__":
    main()
