#!/usr/bin/env bash
# core/scripts/probe-ci.sh - domain-agnostic CI workflow health probe.
#
# For each given GitHub repo (optionally a specific workflow), reads the LATEST
# CI run's LIVE status/conclusion via `gh run list` and reports a three-way
# health verdict per repo plus an aggregate.
#
# Design constraints honored:
#   - guard-647: reads CURRENT state every call (a stored CI verdict is not the
#     current one) - no caching, no stored-value reuse.
#   - rb-611: parse-then-gate THREE-WAY (ok / failed / unverified). "cannot tell"
#     (no auth, no runs, API error) is never collapsed into "failed".
#   - domain-free (.claude/rules/domain-free-examples.md): repos are arguments,
#     never hardcoded. The concrete domain repo list lives in a domain shim
#     (e.g. world/scripts/probe-ci.sh), which wires this engine into infra-health
#     as the "ci" component.
#
# Part of the deploy-loop health probes ( / ).
#
# Usage:  probe-ci.sh [--stale-hours N] owner/repo[=WorkflowName] [owner/repo ...]
# Output: JSON {status, checked, summary, failed[], unverified[], stale[], workflows[]}
# Exit:   0 = JSON emitted (any verdict)   1 = usage error (no repos / no gh)
set -uo pipefail

STALE_HOURS=24
REPOS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --stale-hours) STALE_HOURS="${2:-24}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --stale-hours=*) STALE_HOURS="${1#*=}"; shift ;;
        --*) shift ;;
        *) REPOS+=("$1"); shift ;;
    esac
done

if [ "${#REPOS[@]}" -eq 0 ]; then
    echo '{"status":"unverified","reason":"no_repos","detail":"usage: probe-ci.sh [--stale-hours N] owner/repo[=Workflow] ..."}'
    exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
    echo '{"status":"unverified","reason":"no_auth","detail":"gh CLI not found on PATH"}'
    exit 1
fi

PROBE_REPOS=""
for r in "${REPOS[@]}"; do PROBE_REPOS="${PROBE_REPOS}${r}"$'\n'; done
export PROBE_REPOS
export PROBE_STALE_HOURS="$STALE_HOURS"

# Heredoc-quoted ('PYEOF') so bash performs NO expansion on the Python source
# (guard-165: values cross the boundary via env, never string interpolation).
py -3 <<'PYEOF'
import os, sys, json, subprocess
from datetime import datetime, timezone

repos = [r for r in os.environ.get("PROBE_REPOS", "").split("\n") if r.strip()]
try:
    stale_hours = float(os.environ.get("PROBE_STALE_HOURS", "24"))
except ValueError:
    stale_hours = 24.0
# Per-call gh timeout. gh is keyring-backed here (~5s warm, more on cold-start),
# so 12s tolerates a cold token read. Calls run in parallel below, so total
# wall-clock ~= the slowest single call, staying under infra-health's 30s probe
# budget regardless of repo count.
try:
    gh_timeout = float(os.environ.get("PROBE_GH_TIMEOUT", "12"))
except ValueError:
    gh_timeout = 12.0
now = datetime.now(timezone.utc)

FAIL_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure",
                    "action_required", "stale", "neutral"}
RUNNING_STATUS = {"in_progress", "queued", "requested", "waiting", "pending"}


def parse_created(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def probe_one(spec):
    repo, _, workflow = spec.partition("=")
    cmd = ["gh", "run", "list", "--repo", repo, "--limit", "1",
           "--json", "status,conclusion,createdAt,name,displayTitle"]
    if workflow:
        cmd += ["--workflow", workflow]
    entry = {"repo": repo, "workflow": workflow or None}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=gh_timeout)
    except subprocess.TimeoutExpired:
        entry.update(result="unverified", reason="api_error",
                     detail="gh run list timed out ({}s)".format(int(gh_timeout)))
        return entry
    except FileNotFoundError:
        entry.update(result="unverified", reason="no_auth", detail="gh not found")
        return entry
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        reason = "no_auth" if ("auth" in low or "logged in" in low or "gh auth login" in low) else "api_error"
        entry.update(result="unverified", reason=reason, detail=err[:160])
        return entry
    try:
        runs = json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        entry.update(result="unverified", reason="api_error", detail="invalid gh JSON")
        return entry
    if not runs:
        entry.update(result="unverified", reason="no_runs", detail="no CI runs for repo/workflow")
        return entry
    run = runs[0]
    concl = run.get("conclusion")
    rstatus = run.get("status")
    created = parse_created(run.get("createdAt"))
    age_hours = round((now - created).total_seconds() / 3600.0, 1) if created else None
    is_stale = bool(age_hours is not None and age_hours > stale_hours)
    if concl == "success":
        result = "ok"
    elif concl in FAIL_CONCLUSIONS:
        result = "failed"
    elif concl is None and rstatus in RUNNING_STATUS:
        result = "ok"  # in-flight run is not a failure
    else:
        result = "unverified"
    entry.update(result=result, run_status=rstatus, conclusion=concl,
                 age_hours=age_hours, stale=is_stale,
                 workflow_name=run.get("name"), title=run.get("displayTitle"))
    return entry


# Parallel probe: total wall-clock ~= slowest single gh call (not the sum),
# so N repos stay within infra-health's 30s budget. ex.map preserves order.
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=min(len(repos), 8)) as _ex:
    workflows = list(_ex.map(probe_one, repos))

failed = ["{} ({})".format(w["repo"], w.get("conclusion") or "?")
          for w in workflows if w.get("result") == "failed"]
unverified = ["{} ({})".format(w["repo"], w.get("reason") or "?")
              for w in workflows if w.get("result") == "unverified"]
stale = ["{} ({}h)".format(w["repo"], w.get("age_hours"))
         for w in workflows if w.get("stale")]

if failed:
    status = "failed"
elif unverified:
    status = "unverified"
else:
    status = "ok"

ok_n = sum(1 for w in workflows if w.get("result") == "ok")
summary = "{} ok, {} failed, {} unverified, {} stale (of {})".format(
    ok_n, len(failed), len(unverified), len(stale), len(workflows))

print(json.dumps({
    "status": status,
    "stale_hours": stale_hours,
    "checked": len(workflows),
    "summary": summary,
    "failed": failed,
    "unverified": unverified,
    "stale": stale,
    "workflows": workflows,
}))
PYEOF
