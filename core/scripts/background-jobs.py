#!/usr/bin/env python3
"""Background job tracker for <agent>/session/background-jobs.yaml.

Tracks long-running external OS processes (hours+) so the aspirations loop
can monitor them via recurring goals and collect results on completion.

Complements pending-agents.py (which tracks short-lived Claude Code sub-agents).
Together they form a complete "background work" subsystem:
  pending-agents.py  = Claude Code sub-agents (~10 min, timeout-based staleness)
  background-jobs.py = External OS processes (hours+, PID-based liveness)

The completion_check field makes this domain-agnostic: the framework checks PID
liveness (universal) and delegates "is the job really done?" to whatever command
was registered at launch time.

Subcommands:
  register   — Add a job entry
  deregister — Remove by job_id (deletes file if list empty)
  check      — Check a specific job: PID alive? If dead, run completion_check
  list       — Print all registered jobs
  has-pending — Exit 0 if any jobs exist, exit 1 otherwise
  clear      — Delete file entirely
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import AGENT_DIR, PROJECT_ROOT

JOBS_PATH = AGENT_DIR / "session" / "background-jobs.yaml"


def log(msg):
    print(f"[background-jobs] {msg}", file=sys.stderr)


def read_data():
    """Read background-jobs.yaml, return dict with 'jobs' list."""
    if not JOBS_PATH.exists():
        return {"jobs": [], "last_updated": None}
    try:
        data = yaml.safe_load(JOBS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"jobs": [], "last_updated": None}
    if "jobs" not in data or not isinstance(data["jobs"], list):
        data["jobs"] = []
    return data


def write_data(data):
    """Atomic write to background-jobs.yaml."""
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    tmp = JOBS_PATH.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(JOBS_PATH))


def delete_file():
    """Remove the tracking file entirely."""
    JOBS_PATH.unlink(missing_ok=True)


def pid_alive(pid):
    """Check if a process with the given PID is running."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_bash():
    """Get the correct bash executable path.

    Uses AYOAI_SHELL (set by background-jobs.sh) to ensure we use the same
    shell that invoked us — avoids WSL bash on Windows where Git Bash is intended.
    """
    return os.environ.get("AYOAI_SHELL", "bash")


def run_completion_check(cmd):
    """Run the registered completion_check command.

    Returns (exit_code, stdout). The command runs relative to PROJECT_ROOT.
    Exit codes: 0 = completed, 1 = still running, 2 = failed, other = unknown.

    The command is passed as a single string to bash -c (not split into args)
    because it may contain relative paths that need the cwd context.
    """
    if not cmd:
        return (2, '{"status":"failed","reason":"no_completion_check"}')
    try:
        bash = _get_bash()
        result = subprocess.run(
            [bash, "-c", cmd],
            capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT),
        )
        return (result.returncode, result.stdout.strip())
    except subprocess.TimeoutExpired:
        return (2, '{"status":"failed","reason":"completion_check_timeout"}')
    except Exception as e:
        return (2, json.dumps({"status": "failed", "reason": str(e)}))


def check_job(job):
    """Determine the current status of a job. Returns a status dict."""
    launched_at = job.get("launched_at", "")
    try:
        launch_time = datetime.fromisoformat(launched_at)
        elapsed_hours = (datetime.now() - launch_time).total_seconds() / 3600
    except (ValueError, TypeError):
        elapsed_hours = -1

    pid = job.get("pid")
    is_alive = pid_alive(pid)

    result = {
        "job_id": job.get("job_id"),
        "type": job.get("type"),
        "pid": pid,
        "pid_alive": is_alive,
        "elapsed_hours": round(elapsed_hours, 2),
        "goal_id": job.get("goal_id"),
        "monitor_goal_id": job.get("monitor_goal_id"),
    }

    if is_alive:
        result["status"] = "running"
    else:
        # PID is dead — run completion check to determine outcome
        cmd = job.get("completion_check", "")
        exit_code, output = run_completion_check(cmd)
        if exit_code == 0:
            result["status"] = "completed"
        elif exit_code == 2:
            result["status"] = "failed"
        else:
            result["status"] = "unknown"
        result["check_output"] = output

    return result


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_register(args):
    """Add a job entry to the tracking file."""
    data = read_data()
    # Prevent duplicate registration
    for job in data["jobs"]:
        if job.get("job_id") == args.id:
            log(f"already registered: {args.id}")
            return
    entry = {
        "job_id": args.id,
        "type": args.type,
        "goal_id": args.goal,
        "pid": args.pid,
        "launched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "monitor_goal_id": args.monitor_goal,
        "completion_check": args.completion_check,
    }
    if args.metadata:
        entry["metadata"] = json.loads(args.metadata)
    data["jobs"].append(entry)
    write_data(data)
    log(f"registered: {args.id} (type={args.type}, goal={args.goal}, pid={args.pid})")


def cmd_deregister(args):
    """Remove a job by job_id. Delete file if list becomes empty."""
    data = read_data()
    before = len(data["jobs"])
    data["jobs"] = [j for j in data["jobs"] if j.get("job_id") != args.id]
    after = len(data["jobs"])
    if before == after:
        log(f"not found: {args.id}")
        return
    if not data["jobs"]:
        delete_file()
        log(f"deregistered: {args.id} (no jobs remaining, file deleted)")
    else:
        write_data(data)
        log(f"deregistered: {args.id} ({after} remaining)")


def cmd_check(args):
    """Check the status of a specific job."""
    data = read_data()
    job = next((j for j in data["jobs"] if j.get("job_id") == args.id), None)
    if not job:
        print(json.dumps({"job_id": args.id, "status": "not_found"}, indent=2))
        sys.exit(1)
    result = check_job(job)
    print(json.dumps(result, indent=2))


def cmd_list(args):
    """Print all registered jobs."""
    data = read_data()
    jobs = data.get("jobs", [])
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        if not jobs:
            print("No background jobs.")
            return
        for j in jobs:
            elapsed = ""
            try:
                launch_time = datetime.fromisoformat(j.get("launched_at", ""))
                hours = (datetime.now() - launch_time).total_seconds() / 3600
                elapsed = f" ({hours:.1f}h ago)"
            except (ValueError, TypeError):
                pass
            alive = pid_alive(j.get("pid"))
            status_str = "RUNNING" if alive else "STOPPED"
            print(f"  {j.get('job_id', '?')} | type={j.get('type', '?')} | "
                  f"goal={j.get('goal_id', '?')} | pid={j.get('pid', '?')} "
                  f"[{status_str}]{elapsed}")


def cmd_has_pending(args):
    """Check for registered jobs. Exit 0 if any, exit 1 if none."""
    data = read_data()
    jobs = data.get("jobs", [])
    if jobs:
        sys.exit(0)
    else:
        sys.exit(1)


def cmd_clear(args):
    """Delete the tracking file entirely."""
    if JOBS_PATH.exists():
        delete_file()
        log("cleared all background jobs")
    else:
        log("no background jobs file to clear")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Background external job tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    # register
    reg = sub.add_parser("register", help="Register a background job")
    reg.add_argument("--id", required=True, help="Job identifier (e.g., proc-1711234567)")
    reg.add_argument("--type", required=True, help="Job type (e.g., processor)")
    reg.add_argument("--goal", required=True, help="Goal ID this job serves")
    reg.add_argument("--pid", type=int, required=True, help="OS process ID")
    reg.add_argument("--monitor-goal", default="", help="ID of the recurring monitor goal")
    reg.add_argument("--completion-check", default="",
                     help="Command to run when PID is dead to verify completion "
                          "(exit 0=completed, 2=failed). Resolved relative to project root.")
    reg.add_argument("--metadata", default=None,
                     help="JSON string of domain-specific metadata")

    # deregister
    dereg = sub.add_parser("deregister", help="Remove a job by ID")
    dereg.add_argument("--id", required=True, help="Job identifier to remove")

    # check
    chk = sub.add_parser("check", help="Check status of a specific job")
    chk.add_argument("--id", required=True, help="Job identifier to check")

    # list
    lst = sub.add_parser("list", help="List all registered jobs")
    lst.add_argument("--json", action="store_true", help="Output as JSON")

    # has-pending
    sub.add_parser("has-pending", help="Exit 0 if any jobs exist, exit 1 otherwise")

    # clear
    sub.add_parser("clear", help="Delete tracking file entirely")

    return parser


DISPATCH = {
    "register": cmd_register,
    "deregister": cmd_deregister,
    "check": cmd_check,
    "list": cmd_list,
    "has-pending": cmd_has_pending,
    "clear": cmd_clear,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    fn = DISPATCH.get(args.command)
    if fn is None:
        parser.error(f"Unknown command: {args.command}")
    fn(args)


if __name__ == "__main__":
    main()
