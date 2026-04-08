#!/usr/bin/env python3
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
HEALTH_FILE = WORLD_DIR / "infra-health.yaml"


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------

def load_health():
    """Load world/infra-health.yaml. Returns dict or empty structure."""
    if not HEALTH_FILE.exists():
        return {"components": {}}
    with open(HEALTH_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data else {"components": {}}


def save_health(data):
    """Write world/infra-health.yaml atomically."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEALTH_FILE.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(HEALTH_FILE)


def _blank_component():
    return {
        "last_success": None,
        "last_failure": None,
        "last_failure_reason": None,
        "consecutive_failures": 0,
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
    elif result["status"] in ("failed", "provisionable"):
        entry["last_failure"] = now
        entry["last_failure_reason"] = result.get("error", "unknown")
        entry["consecutive_failures"] = (entry.get("consecutive_failures") or 0) + 1
    # "no_credentials" / "no_probe" — don't update success/failure, just mark checked


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
    """
    component = args.component
    result = probe_component(component)
    result["component"] = component

    # Auto-record
    data = load_health()
    record_result(data, component, result)
    save_health(data)

    print(json.dumps(result, ensure_ascii=False))


def cmd_check_all(args):
    """Probe all known components."""
    data = load_health()
    components = list(data.get("components", {}).keys())
    results = []

    for component in components:
        result = probe_component(component)
        result["component"] = component
        record_result(data, component, result)
        results.append(result)

    save_health(data)
    print(json.dumps(results, ensure_ascii=False))


def cmd_status(args):
    """Read current health state."""
    data = load_health()
    print(json.dumps(data, ensure_ascii=False, default=str))


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

    return parser


DISPATCH = {
    "check": cmd_check,
    "check-all": cmd_check_all,
    "status": cmd_status,
    "stale": cmd_stale,
    "list": cmd_list,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
