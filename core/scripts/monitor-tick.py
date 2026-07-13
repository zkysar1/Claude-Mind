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


def _probe_env():
    """E3 (subprocess-env parity): the env a probe runs under.

    Start from the inherited env -- on the live iteration-close.sh path that
    already carries MIND_AGENT, MIND_SID, PATH, and any gh/aws auth (the
    bash-agent-inject prepend exported them into the shell that spawned this
    Python) -- and GUARANTEE MIND_AGENT even when monitor-tick is invoked
    OUTSIDE that path (a test, a cron, a bare CLI run), so gh/aws/agent-keyed
    probes behave identically regardless of how the tick was launched.
    Fail-open: never raises (a probe must run even if identity resolution errors).
    """
    env = os.environ.copy()
    if not env.get("MIND_AGENT"):
        try:
            from _paths import AGENT_NAME
            if AGENT_NAME:
                env["MIND_AGENT"] = AGENT_NAME
        except Exception:
            pass
    return env


def _subprocess_runner(script_abs, args, timeout=DEFAULT_PROBE_TIMEOUT, project_root=None):
    """Default probe runner: exec the script, return (exit_code, combined_output).
    A probe exits 0 when clean and non-zero when it trips.

    E3 (subprocess-env parity): the probe runs with (a) cwd=project_root so a
    probe that sources .env.local / _paths.sh or uses PROJECT_ROOT-relative
    paths resolves exactly as the loop's Bash path does, and (b) an env that
    guarantees MIND_AGENT (via _probe_env), so gh/aws probes behave identically
    to a hand-run of the same script from the loop.
    """
    cmd = [str(script_abs)] + list(args or [])
    if str(script_abs).endswith(".sh"):
        cmd = ["bash"] + cmd
    elif str(script_abs).endswith(".py"):
        cmd = [sys.executable] + cmd
    run_kwargs = {"capture_output": True, "text": True, "timeout": timeout,
                  "env": _probe_env()}
    if project_root:
        try:
            if Path(project_root).is_dir():
                run_kwargs["cwd"] = str(project_root)
        except Exception:
            pass  # fail-open: bad project_root -> inherit cwd (pre-E3 behavior)
    try:
        r = subprocess.run(cmd, **run_kwargs)
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


def _resolve_script_path(script_rel, project_root, world_dir=None, meta_dir=None):
    """E1: resolve a probe's `script` path to an absolute path.

    A registry `script` beginning with `world/` or `meta/` maps to the
    user-configured EXTERNAL path (WORLD_DIR / META_DIR), NOT
    project_root/world -- world/ and meta/ do not live under PROJECT_ROOT (the
    local repo holds only core/, .claude/, agents/; world/ and meta/ are
    external). Every other relative path (core/scripts/..., etc.) resolves under
    project_root as before; an absolute path is returned unchanged. Fail-open:
    an unset world_dir/meta_dir falls back to project_root/script_rel (the
    pre-E1 behavior), so a resolution gap only reproduces the old path -- never
    raises.
    """
    sr = (script_rel or "").strip()
    if not sr:
        return None
    p = Path(sr)
    if p.is_absolute():
        return p
    parts = sr.replace("\\", "/").split("/", 1)
    prefix = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if rest:
        if prefix == "world" and world_dir:
            return Path(world_dir) / rest
        if prefix == "meta" and meta_dir:
            return Path(meta_dir) / rest
    return Path(project_root) / sr


def run_tick(registry_path, state_path, *, now=None, project_root=None,
             probe_runner=None, on_trip_fn=None):
    """Run one monitor-tick. Returns a structured result (guard-614).

    Injection seams (test): `probe_runner(script_abs, args) -> (rc, output)` and
    `on_trip_fn(probe, evidence) -> result_dict`. Defaults exec real scripts /
    file via the daemon converter. Fail-open per probe.
    """
    now = now or dt.datetime.now()
    project_root = Path(project_root) if project_root else SCRIPT_DIR.parent.parent
    # E3: bind the default runner to project_root so probes run with cwd=root +
    # the agent env (parity with the loop's Bash path). An injected test runner
    # keeps the 2-arg (script_abs, args) seam untouched.
    if probe_runner is None:
        def probe_runner(script_abs, args):
            return _subprocess_runner(script_abs, args, project_root=project_root)
    on_trip_fn = on_trip_fn or _default_on_trip

    # E1: resolve the world/ and meta/ external base dirs ONCE per tick (a probe
    # `script` under those prefixes lives at the user-configured external path,
    # not project_root/world). Fail-open: unavailable _paths -> None ->
    # _resolve_script_path falls back to project_root/script_rel.
    world_dir = meta_dir = None
    try:
        import _paths
        world_dir = getattr(_paths, "WORLD_DIR", None)
        meta_dir = getattr(_paths, "META_DIR", None)
    except Exception:
        pass

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
        script_abs = _resolve_script_path(script_rel, project_root, world_dir, meta_dir)
        rc, output = probe_runner(script_abs, probe.get("args") or [])
        result["ran"].append(pid)
        # Record last-run regardless of outcome so interval gating advances even
        # when the probe trips (prevents trip-every-tick from re-running each cycle).
        entry = {"last_run": now.replace(microsecond=0).isoformat()}

        # E2 -- declarative exit-contract. A probe may override the default
        # "0 == clean, anything else == trip" via two optional registry fields:
        #   clean_exit_codes: list[int]  (default [0]) -- rc values meaning "clean"
        #   timeout_is_trip:  bool        (default false) -- when false, a probe
        #     killed at the timeout boundary (runner sentinel rc=124, the
        #     timeout(1)/curl convention) is a LOGGED ERROR, never a trip, so a
        #     slow/hung probe cannot manufacture a false finding. Set true only
        #     for a probe whose whole purpose is "did X finish in time."
        clean_codes = probe.get("clean_exit_codes")
        if not isinstance(clean_codes, list) or not clean_codes:
            clean_codes = [0]
        try:
            clean_codes = {int(c) for c in clean_codes}
        except Exception:
            clean_codes = {0}
        timeout_is_trip = bool(probe.get("timeout_is_trip", False))

        if rc is None:
            # Launch failure: not a clean pass, not a trip. Log; do not convert.
            entry["last_outcome"] = "launch_error"
            result["errors"].append({"probe": pid, "error": output})
        elif rc in clean_codes:
            entry["last_outcome"] = "clean"
            result["clean"].append(pid)
        elif rc == 124 and not timeout_is_trip:
            # Timeout sentinel -- logged error, NOT a trip (goal spec E2). A hung
            # probe records the outcome (so the interval gate still advances) but
            # files NOTHING. A probe that voluntarily exits 124 for a non-timeout
            # reason is treated as a timeout here unless it sets timeout_is_trip
            # or lists 124 in clean_exit_codes -- 124 is the timeout convention.
            entry["last_outcome"] = "timeout"
            result["errors"].append({"probe": pid, "error": output or "probe timed out"})
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
