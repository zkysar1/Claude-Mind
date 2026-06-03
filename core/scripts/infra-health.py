#!/usr/bin/env python3
# domain-leak-exempt: code comments reference probe-bitnet-prod (an AyoAI deployment probe) as the canonical "probe with no current target" example for the no_target classification + streak-suppression logic. The framework reads component names dynamically; the bitnet-prod name only appears in comments documenting why the no_target branch exists.
"""Infrastructure health check and tracking.

Probes infrastructure components and records results in world/infra-health.yaml.
All shell scripts are thin wrappers around this.

Components are defined dynamically in world/infra-health.yaml. Domain-specific
probe scripts live in world/scripts/probe-{component}.sh and are discovered
automatically when a check is requested.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR
from _fileops import locked_modify_yaml, locked_append_jsonl
from _world_config import load_world_config as _load_world_config
HEALTH_FILE = WORLD_DIR / "infra-health.yaml"
ASPIRATIONS_CONFIG = PROJECT_ROOT / "core" / "config" / "aspirations.yaml"


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------

def load_health():
    """Lock-less read of world/infra-health.yaml. Returns dict or empty structure.

    READ-ONLY consumers only (cmd_status, cmd_stale, cmd_list, cmd_failing_streak).
    Writers MUST use locked_modify_yaml — see g-248-19 / rb-family locked-RMW
    pattern. An unlocked read-then-write sequence (the previous
    load_health → record_result → save_health flow) loses concurrent updates:
    two agents probing different components both read the same baseline,
    and the second write clobbers the first. Same bug class as g-240-52's
    team-state fix. The fix is the same shape: one lock guards the whole
    read-modify-write window.
    """
    if not HEALTH_FILE.exists():
        return {"components": {}}
    with open(HEALTH_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data else {"components": {}}


def _blank_component():
    return {
        "last_success": None,
        "last_failure": None,
        "last_failure_reason": None,
        "consecutive_failures": 0,
        "streak_started_at": None,
        "session_last_checked": None,
    }


# ---------------------------------------------------------------------------
# Credential helper
# ---------------------------------------------------------------------------

# Cache parsed .env.local — read once, reused across all get_credential() calls.
# Avoids spawning multiple bash subprocesses (which timeout on Windows due to
# slow bash startup in Python subprocess).
_ENV_CACHE = None


def _load_env_file():
    """Parse .env.local directly. Returns dict of KEY=VALUE pairs."""
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE

    _ENV_CACHE = {}
    env_file = PROJECT_ROOT / ".env.local"
    if not env_file.exists():
        return _ENV_CACHE

    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    _ENV_CACHE[k] = v
    return _ENV_CACHE


def get_credential(key):
    """Load a credential from .env.local. Returns value or None.

    Reads the file directly instead of spawning bash subprocesses,
    which avoids Windows-specific timeout issues where bash startup
    in Python subprocess takes >10s and causes all probes to report
    'no_credentials' despite credentials being configured.
    """
    env = _load_env_file()
    value = env.get(key)
    return value if value else None


# ---------------------------------------------------------------------------
# Plugin probe discovery
# ---------------------------------------------------------------------------

def _find_probe_script(component):
    """Check if world/scripts/probe-{component}.sh exists. Returns Path or None."""
    script = WORLD_DIR / "scripts" / f"probe-{component}.sh"
    if script.exists():
        return script
    return None


def _discover_probe_components():
    """Glob world/scripts/probe-*.sh and return the set of component names.

    Component name is the script stem after the 'probe-' prefix:
        world/scripts/probe-bridge.sh -> 'bridge'
        world/scripts/probe-bitnet-prod.sh -> 'bitnet-prod'

    Returns a set of strings; empty set when world/scripts/ is missing.
    rb-506 application: probe-script existence is the SSOT for which
    components exist; YAML-registration becomes a reflection of reality
    rather than a drift-prone manual list.
    """
    probe_dir = WORLD_DIR / "scripts"
    if not probe_dir.is_dir():
        return set()
    discovered = set()
    for script in probe_dir.glob("probe-*.sh"):
        stem = script.stem
        if stem.startswith("probe-"):
            name = stem[len("probe-"):]
            if name:
                discovered.add(name)
    return discovered


def _find_bash():
    """Find a bash binary that can handle Windows absolute paths.

    On Windows, Python may find MSYS2 bash (Git/usr/bin/bash.EXE) which
    cannot resolve Windows paths like C:/Users/... passed as arguments.
    Git Bash (Git/bin/bash.exe) handles both Windows and POSIX paths.
    Prefer bin/bash.exe when available; fall back to PATH bash on non-Windows.
    """
    if sys.platform == "win32":
        for candidate in [
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("C:/Program Files (x86)/Git/bin/bash.exe"),
        ]:
            if candidate.exists():
                return str(candidate)
    return "bash"


_BASH_CMD = _find_bash()


def _run_probe_script(script_path, component):
    """Run a domain-specific probe script and parse its JSON output.

    The script should output a JSON object with at least a "status" field.
    Valid status values: "ok", "failed", "no_credentials", "provisionable".
    """
    start = time.time()
    try:
        result = subprocess.run(
            [_BASH_CMD, str(script_path)],  # str() — Git Bash handles Windows paths
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=30,
        )
        latency_ms = int((time.time() - start) * 1000)

        if result.returncode == 0 and result.stdout.strip():
            try:
                probe_result = json.loads(result.stdout.strip())
                if "latency_ms" not in probe_result:
                    probe_result["latency_ms"] = latency_ms
                return probe_result
            except json.JSONDecodeError:
                return {
                    "status": "failed",
                    "error": f"Probe script returned invalid JSON: {result.stdout.strip()[:200]}",
                    "latency_ms": latency_ms,
                }

        # Exit code 2 = no_target convention: probe ran successfully but had
        # nothing to probe right now (e.g., bitnet-prod has no game-server EC2
        # running). DO NOT record this as a failure — there's no failure to
        # report. Documented in world/scripts/probe-bitnet-prod.sh header.
        # Without this, every probe firing while no game-server is up was
        # being recorded as "exit code 2" failure, accumulating spurious
        # consecutive_failures and triggering streak-bitnet-prod alerts
        # (user-reported 2026-05-05).
        if result.returncode == 2:
            summary = result.stdout.strip()[:200] or result.stderr.strip()[:200] or "no probe target"
            return {"status": "no_target", "summary": summary, "latency_ms": latency_ms}

        error = result.stderr.strip()[:200] or result.stdout.strip()[:200] or f"exit code {result.returncode}"
        return {"status": "failed", "error": error, "latency_ms": latency_ms}

    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": f"Probe script timed out (30s)"}
    except FileNotFoundError:
        return {"status": "failed", "error": "bash command not found"}


def probe_component(component):
    """Probe a component using its plugin script, or return no_probe if none exists."""
    script = _find_probe_script(component)
    if script:
        return _run_probe_script(script, component)

    return {
        "status": "no_probe",
        "error": "No probe defined for component. Add probe via world/scripts/ or domain conventions.",
    }


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------

def record_result(data, component, result):
    """Update infra-health.yaml for a component based on probe result."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    components = data.setdefault("components", {})
    entry = components.setdefault(component, _blank_component())

    entry["session_last_checked"] = _get_session_number()

    if result["status"] == "ok":
        entry["last_success"] = now
        entry["last_failure"] = None
        entry["last_failure_reason"] = None
        entry["consecutive_failures"] = 0
        entry["streak_started_at"] = None  # down-episode ended (g-249-16)
    elif result["status"] in ("failed", "provisionable"):
        prev_consecutive = entry.get("consecutive_failures") or 0
        entry["last_failure"] = now
        entry["last_failure_reason"] = result.get("error", "unknown")
        entry["consecutive_failures"] = prev_consecutive + 1
        # g-249-16: stamp the down-episode start on the 0->1 transition and
        # preserve it while the streak climbs, so per-episode alert dedup can
        # key on a stable identifier instead of the per-probe last_failure.
        if prev_consecutive == 0:
            entry["streak_started_at"] = now
    # "no_credentials" / "no_probe" / "no_target" — don't update success/failure,
    # just mark checked. "no_target" means the probe ran successfully but had
    # nothing to probe (e.g., bitnet-prod has no game-server EC2 running right
    # now). The probe-script convention is: exit code 2 from the script → harness
    # classifies as no_target. Documented in world/scripts/probe-bitnet-prod.sh.


def _get_session_number():
    """Read session number from aspirations-meta.json."""
    meta_file = WORLD_DIR / "aspirations-meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return meta.get("session_count", 0)
        except (json.JSONDecodeError, OSError):
            pass
    return None


# ---------------------------------------------------------------------------
# Component discovery
# ---------------------------------------------------------------------------

def _get_known_components():
    """Get the list of known components from world/infra-health.yaml.

    Returns the keys under 'components:' in the health file. If the file
    doesn't exist or has no components, returns an empty list.
    """
    data = load_health()
    return list(data.get("components", {}).keys())


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_check(args):
    """Probe a single component.

    Checking a component that isn't in infra-health.yaml auto-creates its entry.
    This is intentional — probing implies caring about the component.

    Concurrency (g-248-19, rb-family locked-RMW): probe runs OUTSIDE the lock
    (subprocess, up to 30s); only the record-result write is locked. Previous
    flow used unlocked load → mutate → save: two agents probing different
    components both read the same baseline, second write clobbered first.
    """
    component = args.component
    result = probe_component(component)
    result["component"] = component

    def _modifier(data):
        if data is None:
            data = {}
        record_result(data, component, result)
        return data

    locked_modify_yaml(HEALTH_FILE, _modifier, initial={"components": {}})
    print(json.dumps(result, ensure_ascii=False))


def cmd_check_all(args):
    """Probe all known components.

    Component set = union of YAML-registered (world/infra-health.yaml
    'components:' keys) and auto-discovered (world/scripts/probe-*.sh).
    Auto-discovery (rb-506) makes probe-script existence the SSOT and
    eliminates manual registration drift — new probe scripts are picked
    up without yaml updates; record_result auto-creates the entry on
    first probe via setdefault.

    Concurrency (g-248-19): same separation as cmd_check — probes run outside
    the lock (23+ components × up to 30s each = holding the lock for minutes
    would block every other writer). All results are batched into a single
    locked modifier that updates each component atomically.
    """
    # Lock-less enumeration of component list — probe targets, not write target.
    data = load_health()
    registered = set(data.get("components", {}).keys())
    discovered = _discover_probe_components()
    components = sorted(registered | discovered)
    results = []

    for component in components:
        result = probe_component(component)
        result["component"] = component
        results.append(result)

    # Single locked RMW applies all results atomically.
    def _modifier(data):
        if data is None:
            data = {}
        for result in results:
            record_result(data, result["component"], result)
        return data

    locked_modify_yaml(HEALTH_FILE, _modifier, initial={"components": {}})
    print(json.dumps(results, ensure_ascii=False))


def cmd_status(args):
    """Read current health state."""
    data = load_health()
    print(json.dumps(data, ensure_ascii=False, default=str))


def cmd_record(args):
    """Record a goal-execution outcome against a component without probing.

    Decoupled from cmd_check: probing measures component health NOW; record
    stores the outcome a goal just observed. A goal may succeed against a
    cached or degraded component — don't conflate the two signals.
    Referenced by world/conventions/post-execution.md Step 1.

    Concurrency (g-248-19): locked RMW, no external probe — lock window is
    just the dict mutation.
    """
    component = args.component
    # Synthesize a result dict matching record_result's expected schema.
    # success → status=ok (bumps last_success, clears failure fields)
    # failure → status=failed (bumps last_failure, increments consecutive_failures)
    if args.result == "success":
        result = {"status": "ok", "component": component, "summary": args.summary}
    else:
        result = {"status": "failed", "component": component, "error": args.summary}

    def _modifier(data):
        if data is None:
            data = {}
        record_result(data, component, result)
        return data

    locked_modify_yaml(HEALTH_FILE, _modifier, initial={"components": {}})
    print(json.dumps({"recorded": component, "result": args.result, "summary": args.summary}, ensure_ascii=False))


def cmd_stale(args):
    """List components not checked within N hours."""
    hours = args.hours
    data = load_health()
    cutoff = datetime.now() - timedelta(hours=hours)
    stale = []

    for component in data.get("components", {}).keys():
        entry = data.get("components", {}).get(component, _blank_component())
        last_success = entry.get("last_success")
        if last_success is None:
            stale.append({"component": component, "reason": "never checked"})
        else:
            try:
                last_dt = datetime.fromisoformat(last_success)
                if last_dt < cutoff:
                    stale.append({
                        "component": component,
                        "reason": f"last success {last_success}",
                        "hours_ago": round((datetime.now() - last_dt).total_seconds() / 3600, 1),
                    })
            except ValueError:
                stale.append({"component": component, "reason": f"unparseable timestamp: {last_success}"})

    print(json.dumps(stale, ensure_ascii=False))


def cmd_list(args):
    """List all known components from world/infra-health.yaml."""
    components = _get_known_components()
    for c in components:
        script = _find_probe_script(c)
        probe_status = "probe: " + str(script) if script else "no probe"
        print(f"  {c} ({probe_status})")
    if not components:
        print("  (no components defined in world/infra-health.yaml)")


# ---------------------------------------------------------------------------
# Failing-streak alert (g-249-02) + WM known_blocker sync (g-115-360)
# ---------------------------------------------------------------------------

def _load_component_categories():
    """Return component_categories mapping for infra-health alerts.

    Resolution order (per 2026-05-18 packaging plan Phase 2.7):
      1. `world/config/infra-health-categories.yaml` (key: component_categories)
         — host-deployment overlay, populated by domain operators.
      2. Legacy `infra_health.component_categories` in core/config/aspirations.yaml
         — back-compat for one cycle (core now ships an empty dict).

    Empty dict on missing/unreadable config — gate degrades to "blocker filed
    without affected_categories" which is harmless to goal-selector (the
    blocker still surfaces in WM but doesn't suppress any goals).
    """
    # Tier 1: world overlay (preferred). Module-level import: any failure
    # here would have crashed at infra-health.py import time, not silently
    # at first call (fresh-eyes review MEDIUM M2, 2026-05-18).
    overlay = _load_world_config(
        "infra-health-categories",
        default={"component_categories": {}},
    )
    cats = overlay.get("component_categories") or {}
    if isinstance(cats, dict) and cats:
        return cats

    # Tier 2: legacy core/config/aspirations.yaml (back-compat for one cycle).
    try:
        if ASPIRATIONS_CONFIG.exists():
            with open(ASPIRATIONS_CONFIG, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            ih = cfg.get("infra_health") or {}
            cats = ih.get("component_categories") or {}
            if isinstance(cats, dict):
                return cats
    except Exception:
        pass
    return {}


def _sync_known_blockers(alerts):
    """g-115-360: sync streak-* known_blocker entries into per-agent WM.

    Atomic read-modify-write via wm.py module functions under wm.wm_lock().
    Replaces the prior wm-read.sh + wm-set.sh subprocess pair, which had a
    race window between subprocess invocations: two concurrent infra-health
    runs (or one infra-health concurrent with any other wm-set writer) could
    both read the same baseline, compute their preserved lists independently,
    and the second writer clobber non-streak entries the first writer wrote.
    Single locked RMW closes the window. Fix per g-001-239 fresh-eyes finding;
    sister fix to g-001-237 (sig-004-auto-detect race).

    For each alerting component:
      - blocker_id = streak-<component>
      - affected_categories from component_categories config
      - resolution = null (active blocker)

    Removes any pre-existing streak-* entries before re-adding — single sync
    pass = idempotent. Recovery is automatic: when a component drops below
    threshold (no longer in alerts list), its streak-* entry is not re-added
    and is therefore cleared.

    Fail-open: any read or write failure logs to stderr and returns. The
    streak-alert JSON output remains the primary signal; the WM sync is an
    additive observability layer.
    """
    try:
        # Lazy import — wm.py is sibling script_root, AGENT_DIR resolution
        # only succeeds when AYOAI_AGENT is set in env. Importing at module
        # top would crash any infra-health invocation lacking the env var
        # (sometimes intentional, e.g., cross-agent diagnostic probes).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import wm  # type: ignore
    except Exception as e:
        print(f"[infra-health] WARN: wm module import failed ({e}) — skipping known_blocker sync",
              file=sys.stderr)
        return

    component_categories = _load_component_categories()

    try:
        with wm.wm_lock():
            data = wm.read_wm()
            if not data:
                print("[infra-health] WARN: working memory not initialized — skipping known_blocker sync",
                      file=sys.stderr)
                return
            parent, key, _is_top = wm.resolve_slot(data, "known_blockers")
            if parent is None:
                print("[infra-health] WARN: known_blockers slot unresolvable — skipping sync",
                      file=sys.stderr)
                return
            existing = parent.get(key) or []
            if not isinstance(existing, list):
                existing = []

            # Drop pre-existing streak-* entries (we re-derive from current alerts).
            preserved = [
                b for b in existing
                if not (isinstance(b, dict) and isinstance(b.get("blocker_id"), str)
                        and b["blocker_id"].startswith("streak-"))
            ]

            session_no = _get_session_number()
            for alert in alerts:
                component = alert.get("component")
                if not component:
                    continue
                preserved.append({
                    "blocker_id": f"streak-{component}",
                    "description": (
                        f"Streak alert: {alert.get('consecutive_failures')} consecutive "
                        f"probe failures. Last failure: {alert.get('last_failure_reason') or 'unknown'}"
                    ),
                    "affected_categories": component_categories.get(component, []),
                    "affected_skills": [],
                    "detected_session": session_no,
                    "resolution": None,
                    "source": "infra-health.streak-alert",
                })

            parent[key] = preserved
            wm.update_modified(data, "known_blockers")
            wm.write_wm(data)
    except Exception as e:
        print(f"[infra-health] WARN: known_blockers sync failed: {e}",
              file=sys.stderr)


def _load_streak_config():
    """Return (threshold, window_hours) from aspirations.yaml infra_health block.

    Fail-open: if the config file is unreadable or the block is missing, use
    defaults (threshold=3, window_hours=6) — the gate still runs, just with
    baseline parameters. This mirrors fresh-eyes-cadence-check's fail-open
    policy: a config read failure must never block a gate invocation.
    """
    threshold = 3
    window_hours = 6.0
    try:
        if ASPIRATIONS_CONFIG.exists():
            with open(ASPIRATIONS_CONFIG, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            ih = cfg.get("infra_health") or {}
            threshold = int(ih.get("failing_streak_threshold", threshold))
            window_hours = float(ih.get("window_hours", window_hours))
    except Exception:
        pass
    return threshold, window_hours


def cmd_failing_streak(args):
    """Report components whose consecutive_failures >= configured threshold.

    Reads world/infra-health.yaml, filters components where:
      - consecutive_failures >= failing_streak_threshold, AND
      - last_failure timestamp is within window_hours of now (recency filter:
        prevents historically-broken but now-known-blocked components from
        spamming after a human has filed a blocker and moved on).

    Prints JSON {components, threshold, window_hours, alert_count} to stdout.
    Exits 0 if alert_count == 0, exit 1 if alert_count > 0. The nonzero exit
    is the primary signal consumers gate on.

    --alert-file PATH (optional): if passed, appends one JSON line per
    alerting component to that file. Recurring-goal verification reads this
    file to route alerts through /notify-user exactly once per threshold
    crossing (dedup by (component, last_failure) key).
    """
    threshold, window_hours = _load_streak_config()
    if args.threshold is not None:
        threshold = args.threshold
    if args.window_hours is not None:
        window_hours = args.window_hours

    data = load_health()
    components = data.get("components") or {}
    now = datetime.now()
    window = timedelta(hours=window_hours)
    alerts = []
    for name, entry in components.items():
        consecutive = int(entry.get("consecutive_failures") or 0)
        if consecutive < threshold:
            continue
        last_failure = entry.get("last_failure")
        if last_failure:
            try:
                lf_dt = datetime.fromisoformat(last_failure)
                if (now - lf_dt) > window:
                    continue
            except (ValueError, TypeError):
                pass
        alerts.append({
            "component": name,
            "consecutive_failures": consecutive,
            "last_failure": last_failure,
            "last_failure_reason": entry.get("last_failure_reason"),
            "streak_started_at": entry.get("streak_started_at"),
            "human_gated": bool(entry.get("human_gated")),
        })

    output = {
        "threshold": threshold,
        "window_hours": window_hours,
        "alert_count": len(alerts),
        "components": alerts,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if args.alert_file and alerts:
        # g-001-223 (rb-685 family): use locked_append_jsonl instead of plain
        # open("a") so concurrent multi-agent invocations cannot interleave
        # mid-line on Windows NTFS (POSIX append atomicity does not apply).
        # Matches the locking discipline already used elsewhere in this file
        # via locked_modify_yaml.
        alert_path = Path(args.alert_file)
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        for a in alerts:
            locked_append_jsonl(str(alert_path), a)

    # g-115-360: sync streak-* known_blocker entries into per-agent WM so
    # goal-selector's blocker gate (precheck Phase 0.5b) can suppress
    # dependent goals. ALWAYS called — empty alerts list clears stale
    # streak-* entries (recovery path).
    if not args.no_sync_blockers:
        _sync_known_blockers(alerts)

    if alerts:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Infrastructure health check and tracking")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Probe a single component")
    p_check.add_argument("component", help="Component to probe (from world/infra-health.yaml or any name)")

    sub.add_parser("check-all", help="Probe all known components")

    sub.add_parser("status", help="Read current health state")

    p_stale = sub.add_parser("stale", help="List components not checked recently")
    p_stale.add_argument("--hours", type=float, default=2.0, help="Staleness threshold in hours (default: 2)")

    sub.add_parser("list", help="List known components and their probe status")

    p_record = sub.add_parser("record", help="Record goal-execution outcome without probing")
    p_record.add_argument("component", help="Component the outcome applies to")
    p_record.add_argument("result", choices=["success", "failure"], help="Outcome classification")
    p_record.add_argument("summary", help="One-line summary (stored in last_failure_reason on failure)")

    p_streak = sub.add_parser("streak-alert", help="Report components whose consecutive_failures >= threshold (nonzero exit = alerts pending)")
    p_streak.add_argument("--threshold", type=int, default=None, help="Override failing_streak_threshold (default: aspirations.yaml infra_health.failing_streak_threshold, fallback 3)")
    p_streak.add_argument("--window-hours", type=float, default=None, help="Override recency window (default: aspirations.yaml infra_health.window_hours, fallback 6)")
    p_streak.add_argument("--alert-file", type=str, default=None, help="Optional: append alerting components as JSONL for downstream notify-user dedup")
    p_streak.add_argument("--no-sync-blockers", action="store_true", help="Skip per-agent WM known_blocker sync (g-115-360). Use when running ad-hoc / outside an agent context.")

    return parser


DISPATCH = {
    "check": cmd_check,
    "check-all": cmd_check_all,
    "status": cmd_status,
    "stale": cmd_stale,
    "list": cmd_list,
    "record": cmd_record,
    "streak-alert": cmd_failing_streak,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
