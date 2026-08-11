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

from _paths import AGENT_DIR, PROJECT_ROOT, assert_agent_dir

# : fail loud at import time if MIND_AGENT unset; replaces the
# opaque `None / "session"` TypeError class the next line would otherwise raise.
assert_agent_dir("background-jobs")

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
    """Check if a process with the given PID is running.

    On Windows, ``os.kill(pid, 0)`` can return a false negative for processes
    that exist but do not grant the caller PROCESS_TERMINATE access (journal
    2026-04-07 documented a Processor run falsely reporting pid_alive=false).
    When os.kill reports dead on Windows, double-check via WMI Win32_Process
    before trusting the result.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        if os.name == "nt":
            return _win_pid_exists(pid)
        return False


def _win_pid_exists(pid):
    """WMI-based PID liveness fallback for Windows. Returns True if the PID
    is currently a live Win32_Process, False otherwise (including on any
    query failure — better to under-report than kill a running job)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (Get-CimInstance Win32_Process -Filter 'ProcessId={pid}') "
             f"{{ 'ALIVE' }} else {{ 'DEAD' }}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "ALIVE" in result.stdout
    except Exception:
        return False


def _get_bash():
    """Get the correct bash executable path.

    Uses MIND_SHELL (set by background-jobs.sh) to ensure we use the same
    shell that invoked us — avoids WSL bash on Windows where Git Bash is intended.
    """
    from _runtime_bash import BASH  # rb-1472: bin-first, honors MIND_SHELL, clean-PATH-safe
    return BASH


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
            encoding="utf-8", errors="replace",
            cwd=str(PROJECT_ROOT),
        )
        return (result.returncode, result.stdout.strip())
    except subprocess.TimeoutExpired:
        return (2, '{"status":"failed","reason":"completion_check_timeout"}')
    except Exception as e:
        return (2, json.dumps({"status": "failed", "reason": str(e)}))


def check_output_artifacts(artifacts):
    """Validate declared output artifacts after a job is otherwise marked completed.

    Each artifact is {path, min_bytes (default 1), format (optional: json|jsonl)}.
    Returns a list of failure dicts — empty list means all artifacts passed.

    This is the generalized 0-byte defense: trusting a completion_check exit code
    alone is insufficient because an external command can exit 0 while its declared
    output is missing, empty, or unparseable (see rb-061, rb-085, guard-156, rb-247).
    """
    failures = []
    for art in artifacts or []:
        path_str = art.get("path")
        if not path_str:
            failures.append({"path": "", "reason": "artifact_missing_path"})
            continue
        p = Path(path_str)
        if not p.exists():
            failures.append({"path": str(p), "reason": "missing"})
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            failures.append({"path": str(p), "reason": f"stat_failed: {e}"})
            continue
        min_bytes = art.get("min_bytes", 1)
        if size < min_bytes:
            failures.append({"path": str(p), "reason": f"too_small ({size}b < {min_bytes}b)"})
            continue
        fmt = art.get("format")
        if fmt == "json":
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                failures.append({"path": str(p), "reason": f"json_parse: {e}"})
        elif fmt == "jsonl":
            try:
                with p.open(encoding="utf-8") as f:
                    first = f.readline()
                if not first.strip():
                    failures.append({"path": str(p), "reason": "jsonl_empty_first_line"})
                else:
                    json.loads(first)
            except Exception as e:
                failures.append({"path": str(p), "reason": f"jsonl_parse: {e}"})
        # Any other (or missing) format: size check alone is sufficient.
    return failures


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
        elif exit_code == 1:
            # 1 = still running per run_completion_check's contract. Reached when
            # the registered PID reads dead but the domain completion_check (the
            # authoritative arbiter) reports the job alive -- e.g. a Windows/MSYS
            # PID-namespace mismatch where the PID file holds an MSYS bash PID
            # that WMI/os.kill cannot see (). Without this branch the
            # "still running" signal fell through to "unknown", which MONITOR-
            # style consumers treat like "failed" -- false-failing a healthy
            # long-running job.
            result["status"] = "running"
        elif exit_code == 2:
            result["status"] = "failed"
        else:
            result["status"] = "unknown"
        result["check_output"] = output

        # Output-sanity gate: if the completion_check claims success, verify
        # declared output_artifacts are actually present, non-trivial, and
        # parseable. Any failure overrides status to "failed".
        # CRITICAL — DO NOT MOVE: the gate MUST run after run_completion_check
        # and MUST only act on status == "completed". Running it earlier
        # (before PID-dead is confirmed) or on other statuses produces
        # false failures on still-running jobs and conflates two distinct
        # failure modes (runtime crash vs. 0-byte output after clean exit).
        if result["status"] == "completed":
            artifacts = job.get("output_artifacts") or []
            if artifacts:
                failures = check_output_artifacts(artifacts)
                if failures:
                    result["status"] = "failed"
                    result["output_check_failures"] = failures

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
        # Owning BODY (). This store is agent-wide (session/ singular,
        # one file per mind on the box) and carried no body key, so a WORKER
        # body's job made the REDUCER's stop-hook Gate 2.6 ALLOW a turn-end it
        # would otherwise BLOCK. MIND_SID is always injected into Bash tool
        # calls (bash-agent-inject.py) and every register call site is an
        # EXECUTE-phase skill, so the env read resolves in production; the
        # explicit flag exists for tests and for callers that know better.
        # Empty when unresolvable — see cmd_has_pending for why that is safe.
        "owner_sid": (getattr(args, "body_sid", "") or os.environ.get("MIND_SID", "") or ""),
    }
    if args.metadata:
        entry["metadata"] = json.loads(args.metadata)
    if args.output_artifacts:
        parsed = json.loads(args.output_artifacts)
        if not isinstance(parsed, list):
            raise ValueError("--output-artifacts must be a JSON array of artifact specs")
        entry["output_artifacts"] = parsed
    data["jobs"].append(entry)
    write_data(data)
    log(f"registered: {args.id} (type={args.type}, goal={args.goal}, pid={args.pid})")
    # Un-gateable registration warning (). cmd_has_pending counts a job
    # as pending only when the PID is alive AND at least one completion mechanism
    # exists. That strictness is deliberate (the anti-zombie rule — see its
    # docstring), so this is NOT an error: standalone infrastructure legitimately
    # registers without one. But without the warning the caller gets a plain
    # success message for a registration that is structurally incapable of the one
    # thing it was registered for, believes turn-end is permitted, and busy-spins
    # against a Gate 2.6 BLOCK for the length of the external wait
    # (~20 turns over 32min — run-full-suite-after-deep-code.md). Say so at the
    # only moment the caller is still in a position to fix it.
    if not (entry["monitor_goal_id"] or "") and not (entry["completion_check"] or ""):
        log(f"WARN: {args.id} has neither --monitor-goal nor --completion-check — "
            "this job will NOT satisfy `has-pending` / stop-hook Gate 2.6, so it "
            "cannot gate turn-end. Pass --completion-check or --monitor-goal if "
            "you registered it to hold the turn open.")


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
    """Check for registered jobs that are actually pending.

    A job counts as "pending" only when BOTH:
      - PID is alive (pid_alive returns True), AND
      - At least one completion mechanism is registered:
        monitor_goal_id non-empty OR completion_check non-empty.

    Why strict: stop-hook.sh:167-170 and recovery-gate.sh Cond 4 BOTH consume
    this gate. A "pending" verdict ALLOWs stop-hook turn-end (alpha 2026-05-14
    failure mode) and SUPPRESSES recovery-gate.

    Dead-PID registrations are by definition no longer running — the job is
    over (or never cleaned up). They must NOT gate stop-hook decisions.

    Empty-monitor + empty-completion-check registrations have no completion
    signal the framework can observe. They cannot legitimately gate stop-hook
    decisions because gating depends on the eventual completion, which can
    never fire. (Canonical zombie: alpha roblox-bridge-29208 registered
    2026-05-13T17:40 with both fields empty; gated 17:56 + 00:45 ALLOWs
    that killed alpha's loop after each iteration.)

    Exit 0 = at least one pending job, Exit 1 = none.

    BODY FILTER (--body-sid, g-306-135). OPT-IN by design: with no --body-sid
    the behaviour is byte-identical to before, because the other consumer of
    this gate — recovery-gate.sh Cond 4 — probes CROSS-AGENT
    (`MIND_AGENT="$agent" background-jobs.sh has-pending`) and asks an
    agent-wide question: "is this whole mind legitimately busy?" Making the
    filter a default would answer a different question there and, since Cond 4
    passes when has-pending exits 1, would make zombie-recovery MORE likely to
    fire on an agent that is genuinely working.

    When --body-sid IS passed (stop-hook Gates 2.5/2.6), a job counts only if
    its owner_sid matches EXACTLY. A job with a missing/empty owner_sid — one
    registered before this change — therefore does NOT gate. That direction is
    deliberate: rb-605 (anticipation gates fail OPEN) and this gate's own
    comment both say an error must resolve to "no pending jobs" so the BLOCK
    proceeds and the loop stays alive. Filtering must never turn an unknown
    owner into an ALLOW, because an ALLOW is what removes the text-death net.
    """
    data = read_data()
    # THREE-WAY, not two: the flag's ABSENCE and an EMPTY value mean opposite
    # things, so the default is None rather than "".
    #   None  -> caller did not ask to filter (recovery-gate) -> agent-wide.
    #   ""    -> caller asked to filter but could not resolve its own identity.
    #            That is the error case: nothing is "mine", so exit 1 and let the
    #            BLOCK proceed. Collapsing it into the None branch would let an
    #            identity-less caller ALLOW, and matching it against a legacy
    #            record's empty owner_sid would do the same by string equality.
    #   <sid> -> exact-match filter; a record with an empty owner_sid can never
    #            match a non-empty sid, so legacy records correctly do not gate.
    body_sid = getattr(args, "body_sid", None)
    if body_sid is not None and not body_sid:
        sys.exit(1)
    for job in data.get("jobs", []):
        if body_sid is not None and (job.get("owner_sid") or "") != body_sid:
            continue
        if not pid_alive(job.get("pid")):
            continue
        if (job.get("monitor_goal_id") or "") or (job.get("completion_check") or ""):
            sys.exit(0)
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
    reg.add_argument("--goal", default="standalone",
                     help="Goal ID this job serves. Defaults to 'standalone' for "
                          "infrastructure spawns (roblox-bridge, llama-server) that "
                          "run outside a goal context.")
    reg.add_argument("--pid", type=int, required=True, help="OS process ID")
    reg.add_argument("--monitor-goal", default="", help="ID of the recurring monitor goal")
    reg.add_argument("--completion-check", default="",
                     help="Command to run when PID is dead to verify completion "
                          "(exit 0=completed, 2=failed). Resolved relative to project root.")
    reg.add_argument("--metadata", default=None,
                     help="JSON string of domain-specific metadata")
    reg.add_argument("--output-artifacts", default=None,
                     help="JSON array of artifact specs to validate when the job "
                          "completes. Each spec: {path, min_bytes, format}. "
                          "format is optional (json|jsonl); omit for size-only. "
                          "Any missing/too-small/unparseable artifact flips status "
                          "from 'completed' to 'failed' with output_check_failures.")
    reg.add_argument("--body-sid", default="",
                     help="Owning body's session id. Defaults to $MIND_SID.")

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
    hp = sub.add_parser("has-pending", help="Exit 0 if any jobs exist, exit 1 otherwise")
    hp.add_argument("--body-sid", default=None,
                    help="Count only jobs owned by this body's session id. "
                         "Omit entirely for agent-wide (legacy) behaviour; an "
                         "EMPTY value means the caller has no identity and "
                         "nothing counts as pending.")

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
