#!/usr/bin/env bash
# mind-api-start.sh — Idempotent daemon spawn with crash recovery.
#
# Called from three sites:
#   1. SessionStart hook (sessionstart-orchestrator.sh Step 0)
#   2. /start <agent> skill (IDLE Step 3 / UNINITIALIZED Phase C / observer)
#   3. Wrapper auto-spawn on rc=3 (_runtime.sh rt_ensure_running)
#
# Behavior contract:
#   - Idempotent: if daemon is alive and responsive, exit 0 immediately.
#   - Crash recovery: stale PID file (PID dead) → clean up and respawn.
#   - Unresponsive recovery: PID alive but not responding to health probe
#     → SIGTERM → 5s wait → SIGKILL if still alive → clean up → respawn.
#   - Spawn: launch daemon detached, wait up to 10s for socket readiness.
#   - Exit 0 on success, exit 1 on spawn failure.
#
# This script MUST complete (exit) before the caller re-probes — it does
# NOT hold the foreground waiting on the daemon process.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# B16: RUNTIME_DIR override isolates the daemon's runtime files so a
# daemon-integration test can spawn a daemon without hijacking the live one's
# PROJECT_ROOT/mind_api/state. Unset (production default) -> the canonical path,
# byte-identical to prior behavior. Mirrors lifecycle.runtime_dir / owncloud_sync.
RT_DIR="${RUNTIME_DIR:-$PROJECT_ROOT/mind_api/state}"
PID_FILE="$RT_DIR/daemon.pid"
PORT_FILE="$RT_DIR/daemon.port"
PARENT_PID_FILE="$RT_DIR/daemon.parent.pid"
SPAWN_LOG="$RT_DIR/spawn.log"

# --restart forces a recycle even when the daemon is healthy-and-responsive.
# Plain (no-arg) mind-api-start.sh stays health-only idempotent (a responsive
# daemon exits 0 without restart) — guard-559 warns callers NOT to rely on it
# to clear a stale-but-200 daemon, which is exactly why the post-commit hook
# passes --restart after a daemon-code commit.
FORCE_RESTART=0
for arg in "$@"; do
    case "$arg" in
        --restart) FORCE_RESTART=1 ;;
        *) echo "[daemon-start] unknown arg: $arg (accepts: --restart)" >&2; exit 2 ;;
    esac
done

# ─── Helpers ──────────────────────────────────────────────────────────────────

_log() {
    local stamp
    stamp="$(date +%Y-%m-%dT%H:%M:%S)"
    echo "[$stamp] daemon-start: $*" >> "$SPAWN_LOG" 2>/dev/null || true
}

_is_pid_alive() {
    local pid="$1"
    [ "$pid" -gt 0 ] 2>/dev/null || return 1
    kill -0 "$pid" 2>/dev/null
}

_health_probe() {
    local port="$1"
    curl -s -f --max-time 2 "http://127.0.0.1:${port}/v1/admin/health" >/dev/null 2>&1
}

_read_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$pid" ] && echo "$pid"
}

_read_port() {
    [ -f "$PORT_FILE" ] || return 1
    local port
    port="$(cat "$PORT_FILE" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$port" ] && echo "$port"
}

#  v3: read the py.exe launcher PID written by the daemon at
# startup. Returns empty string when the file is absent (older daemon
# predating the v3 fix, daemon spawned without a launcher, foreground
# debugger run) — _force_kill_tree treats empty as "skip parent kill, child
# kill alone is best-effort."
_read_parent_pid() {
    [ -f "$PARENT_PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PARENT_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$pid" ] && echo "$pid"
}

_clean_runtime_files() {
    rm -f "$PID_FILE" "$PORT_FILE" "$PARENT_PID_FILE" 2>/dev/null || true
}

# _acquire_spawn_lock / _release_spawn_lock — wrapper-side spawn mutex.
# Mirrors _runtime.sh rt_acquire_spawn_lock / rt_release_spawn_lock; both
# call sites share the SAME lock file ($RT_DIR/daemon.wrapper.lock) so
# mind-api-start.sh and rt_ensure_running coordinate. Without this, two
# concurrent /start invocations or a /start racing a wrapper auto-spawn
# both pass the alive-check and both launch a daemon — the loser's
# _spawn_lock branch refuses at bind time but the process started, bound
# briefly, and only the 10s self-supersession poll converges.
#
# 45s stale TTL = daemon _spawn_lock TTL (30s) + spawn ready window (5s)
# + margin so a wedged spawn is recoverable. Stale-break is immediate-retry.
_acquire_spawn_lock() {
    local lock="$RT_DIR/daemon.wrapper.lock"
    mkdir -p "$RT_DIR" 2>/dev/null || true
    local stale_seconds=45
    local max_wait_ms=2000 step_ms=50 waited=0
    while [ "$waited" -lt "$max_wait_ms" ]; do
        if ( set -o noclobber; echo "$$:$(date +%s)" > "$lock" ) 2>/dev/null; then
            return 0
        fi
        if [ -f "$lock" ]; then
            local lock_mtime now age
            lock_mtime=$(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo 0)
            now=$(date +%s); age=$(( now - lock_mtime ))
            if [ "$age" -gt "$stale_seconds" ]; then
                rm -f "$lock" 2>/dev/null || true
                continue
            fi
        fi
        sleep 0.05
        waited=$(( waited + step_ms ))
    done
    return 1
}

_release_spawn_lock() {
    rm -f "$RT_DIR/daemon.wrapper.lock" 2>/dev/null || true
}

# _kill_escalate PID [PORT] — SIGTERM → short graceful wait → caller follows
# up with _force_kill_tree for guaranteed reap.
#
#  v3: the wait loop now uses HEALTH PROBE (curl) when PORT is
# available, falling back to _is_pid_alive only when no port is known. The
# original kill -0 loop false-negatives on MSYS Git Bash against detached
# native-Windows processes (the same fault that motivated 's
# idempotency-check fix; the wait loop was the second site that never got
# the same treatment), exiting at the first 100ms iteration and logging a
# misleading "PID X exited after SIGTERM" while the daemon was still serving.
#
# The graceful window is intentionally short (1.5s vs the old 5s). The
# daemon's signal handler runs in microseconds; if it hasn't gracefully
# exited inside 1.5s it isn't going to, and the unconditional Windows
# tree-kill that follows (in the caller) is the authoritative reap. Long
# SIGTERM windows just delay the spawn and prolong the wrapper-side lock.
_kill_escalate() {
    local pid="$1"
    local port="${2:-}"
    _log "sending SIGTERM to PID $pid (graceful window: ~1.5s wall, port=${port:-none})"
    kill -TERM "$pid" 2>/dev/null || true

    # Wall-clock-bounded wait: short-timeout curl probes capped at ~1.5s
    # total. The probe uses --max-time 0.3 (not 2) so a frozen daemon
    # holding the port doesn't blow past the documented budget. 5 iters
    # × (0.3s probe + 0.1s sleep) = 2s absolute worst case (typically <1s
    # when the daemon exits cleanly because the probe fails fast on
    # connection refused).
    local waited=0
    local max_iters=5
    while [ "$waited" -lt "$max_iters" ]; do
        if [ -n "$port" ]; then
            # Inline curl (NOT _health_probe — that uses --max-time 2 which
            # is appropriate for liveness checks but too slow for a tight
            # kill loop). A connection-refused returns near-instantly; only
            # a hung-but-bound daemon hits the 0.3s ceiling.
            if ! curl -s -f --max-time 0.3 "http://127.0.0.1:${port}/v1/admin/health" >/dev/null 2>&1; then
                _log "PID $pid no longer responding on port $port (graceful exit detected in iter $((waited + 1)))"
                return 0
            fi
        else
            if ! _is_pid_alive "$pid"; then
                _log "PID $pid no longer alive (kill -0; graceful exit detected in iter $((waited + 1)))"
                return 0
            fi
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
    _log "PID $pid still responding after ~1.5s — caller will force-kill the tree"
}

# _force_kill_tree CHILD_PID [PARENT_PID] — guaranteed reap of the daemon
# process tree on Windows.
#
#  v3 BULLETPROOF FIX. The previous tree-kill on Windows looked up
# the parent via Get-CimInstance Win32_Process -Filter "ProcessId=$child"
# .ParentProcessId. When the child had already exited from SIGTERM (which
# happens in ~17% of recycle cycles), Get-CimInstance returned null, the
# inner if-block was skipped, and the py.exe parent was orphaned. Empirical
# evidence: 35 orphan daemon pairs accumulated over 204 spawns in 37 hours
# (the snapshot the user surfaced 2026-05-22).
#
# v3 fix: kill BOTH PIDs by KNOWN values (read from daemon.parent.pid +
# daemon.pid). No lookup chain. PARENT_PID is optional (empty when no
# parent.pid file exists — older daemon, foreground debugger, etc.); in
# that case only the child is killed.
#
# Each Stop-Process is gated by a Get-CimInstance CommandLine sanity check
# against 'mind_api' (relaxed from 'mind_api\.src' so we still match the
# launcher's `py -3 -m mind_api.src` line under any future renaming). This
# guards against PID reuse: if the PID now names a different process, the
# CommandLine won't match and Stop-Process is skipped.
#
# Output is captured + logged (NOT silenced with >/dev/null). The previous
# code's blanket `>/dev/null 2>&1 || true` made every kill failure invisible,
# which is why this regression survived multiple "fix" attempts.
_force_kill_tree() {
    local child_pid="$1"
    local parent_pid="${2:-}"
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*) ;;
        *)
            # POSIX: just SIGKILL the child (no py.exe launcher layer).
            if [ -n "$child_pid" ] && _is_pid_alive "$child_pid"; then
                kill -KILL "$child_pid" 2>/dev/null || true
                _log "force-killed POSIX child PID $child_pid"
            fi
            return 0
            ;;
    esac

    # Normalize empty PIDs to 0 sentinel so PowerShell sees a number.
    # Kill-Guarded checks <=0 and skips.
    local pp="${parent_pid:-0}"
    [ -z "$pp" ] && pp=0
    local cp="${child_pid:-0}"
    [ -z "$cp" ] && cp=0

    # PowerShell script: kill each known PID with CommandLine sanity check,
    # return one structured line for spawn.log.
    local ps_script="
        \$results = @()
        function Kill-Guarded(\$pid_to_kill, \$label) {
            if (-not \$pid_to_kill -or \$pid_to_kill -le 0) {
                \$script:results += \"\$label=skip(no-pid)\"
                return
            }
            \$p = Get-CimInstance Win32_Process -Filter \"ProcessId=\$pid_to_kill\" -ErrorAction SilentlyContinue
            if (-not \$p) {
                \$script:results += \"\$label=skip(already-gone PID=\$pid_to_kill)\"
                return
            }
            if (\$p.CommandLine -notmatch 'mind_api') {
                \$script:results += \"\$label=skip(cmdline-mismatch PID=\$pid_to_kill name=\$(\$p.Name))\"
                return
            }
            try {
                Stop-Process -Id \$pid_to_kill -Force -ErrorAction Stop
                \$script:results += \"\$label=killed(PID=\$pid_to_kill name=\$(\$p.Name))\"
            } catch {
                \$script:results += \"\$label=fail(PID=\$pid_to_kill err=\$(\$_.Exception.Message))\"
            }
        }
        Kill-Guarded $pp 'parent'
        Kill-Guarded $cp 'child'
        \$results -join '; '
    "
    local result
    result="$(powershell.exe -NoProfile -Command "$ps_script" 2>&1)" || true
    _log "force-kill-tree child=$cp parent=$pp -> ${result:-<no-output>}"
}

# _sweep_orphan_daemons CURRENT_CHILD_PID CURRENT_PARENT_PID — belt-and-suspenders
# scan for any mind_api.src processes whose PID is NOT in the current
# {child, parent} pair, and kill them. Catches orphans from past spawns
# whose kill silently failed (the residual failure mode this whole patch
# addresses). Safe to run as a no-op when no orphans exist.
#
# CURRENT_CHILD_PID + CURRENT_PARENT_PID identify the daemon we want to
# KEEP alive (the one whose PIDs are in daemon.pid + daemon.parent.pid).
# Pass empty strings to "kill them all" — used when we are about to spawn
# a fresh daemon and there should be no surviving mind_api.src processes.
_sweep_orphan_daemons() {
    local keep_child="${1:-}"
    local keep_parent="${2:-}"
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*) ;;
        *)
            # POSIX implementation: pgrep for python processes whose cmdline
            # contains 'mind_api.src'. POSIX never had the orphan problem
            # (clean process group semantics) so this is preventive only.
            local pids
            pids="$(pgrep -f 'python.* -m mind_api\.src' 2>/dev/null | tr '\n' ' ')"
            local killed=0
            for p in $pids; do
                [ "$p" = "$keep_child" ] && continue
                [ "$p" = "$keep_parent" ] && continue
                kill -KILL "$p" 2>/dev/null && killed=$((killed + 1)) || true
            done
            [ "$killed" -gt 0 ] && _log "orphan-sweep POSIX killed $killed mind_api.src processes (kept child=${keep_child:-none} parent=${keep_parent:-none})"
            return 0
            ;;
    esac

    local keep_c="${keep_child:-0}"
    [ -z "$keep_c" ] && keep_c=0
    local keep_p="${keep_parent:-0}"
    [ -z "$keep_p" ] && keep_p=0

    local ps_script="
        \$keep_child = $keep_c
        \$keep_parent = $keep_p
        \$procs = Get-CimInstance Win32_Process -Filter \"Name='py.exe' OR Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { \$_.CommandLine -match 'mind_api\.src' }
        \$killed = @()
        \$kept = @()
        foreach (\$p in \$procs) {
            if (\$p.ProcessId -eq \$keep_child -or \$p.ProcessId -eq \$keep_parent) {
                \$kept += \"\$(\$p.Name)/\$(\$p.ProcessId)\"
                continue
            }
            try {
                Stop-Process -Id \$p.ProcessId -Force -ErrorAction Stop
                \$killed += \"\$(\$p.Name)/\$(\$p.ProcessId)\"
            } catch {
                \$killed += \"fail-\$(\$p.Name)/\$(\$p.ProcessId)\"
            }
        }
        if (\$killed.Count -gt 0) {
            Write-Output \"killed=\$(\$killed.Count) [\$(\$killed -join ',')] kept=[\$(\$kept -join ',')]\"
        } else {
            Write-Output \"clean (no orphans; alive=[\$(\$kept -join ',')])\"
        }
    "
    local result
    result="$(powershell.exe -NoProfile -Command "$ps_script" 2>&1)" || true
    # Always log; the "clean" line confirms the sweeper ran and saw a healthy
    # process state, which is itself valuable telemetry. Skip log when the
    # sweep saw nothing AND we have no current daemon yet (avoids noise on
    # the very first spawn after a clean repo checkout).
    if [ -n "$result" ]; then
        case "$result" in
            "clean"*)
                # Only log clean state when we have a current daemon — proves the
                # sweep matched our published PIDs. Otherwise skip.
                if [ "$keep_c" != "0" ] || [ "$keep_p" != "0" ]; then
                    _log "orphan-sweep: $result"
                fi
                ;;
            *)
                _log "orphan-sweep: $result"
                ;;
        esac
    fi
}

# _python_launcher — prefer py -3 on Windows (avoids MS Store stub), fall
# back to python3. Same logic as _runtime.sh rt_python_launcher.
_python_launcher() {
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*)
            if py -3 --version >/dev/null 2>&1; then
                echo "py -3"
            fi
            #  v3: NO bare-python3 fallback on Windows. A
            # POSIX/MSYS-flavored Git-bash python3 makes the daemon resolve
            # a RELATIVE WORLD_DIR and mkdir-mirror the cruft tree before
            # crash-looping. Empty output -> the `[ -z "$py_cmd" ]` guard
            # at the call site aborts loudly. Symmetric with
            # _runtime.sh rt_python_launcher.
            ;;
        *)
            if python3 --version >/dev/null 2>&1; then
                echo "python3"
            fi
            ;;
    esac
}

# ─── Main ─────────────────────────────────────────────────────────────────────

mkdir -p "$RT_DIR"

# Fast-path: skip the spawn mutex when the daemon is already alive and
# responsive AND no --restart was requested. Keeps the common path
# (SessionStart hook with a live daemon) lock-free.
#
#  fix: health-probe ALONE is the authoritative liveness signal
# here. POSIX `kill -0` false-negatives on MSYS Git Bash against detached
# native-Windows processes spawned via `py -3 -m mind_api.src` — every
# invocation was falling through to the slow path and recycling a healthy
# daemon (post-commit hook + SessionStart orchestrator hit this hundreds
# of times). If the daemon is responding to /health on the published port
# AND the PID/port files are current, it IS alive, regardless of kill -0.
existing_pid="$(_read_pid || echo "")"
existing_port="$(_read_port || echo "")"
if [ -n "$existing_pid" ] && [ -n "$existing_port" ] && \
   _health_probe "$existing_port" && \
   [ "$FORCE_RESTART" != "1" ]; then
    _log "daemon already running (fast-path PID=$existing_pid, port=$existing_port)"
    exit 0
fi

# ─── Shared-runtime claim gate () ───────────────────────────────────
# Placed AFTER the fast path deliberately: a test that merely pokes a HEALTHY
# daemon is harmless and must keep working (that is the common case and this
# gate must not touch it). Reaching HERE means we are about to _force_kill_tree
# the live daemon and write THIS process's pid/port into the shared
# PROJECT_ROOT/mind_api/state — repointing every agent on the box at whatever
# world the caller resolved. When the caller is pytest, that is never intended.
#
# Observed 2026-07-26: test_post_state_update_metric_gate_category.py builds a
# tmp world + tmp agent dir, touches a daemon wrapper, and the runtime recycled
# the live daemon out from under the running fleet.
#
# rt_spawn (_runtime.sh, ) already SCRUBS test env off the respawned
# daemon, but scrubbing is fail-OPEN: the shared port is still claimed and the
# live daemon still dies — only the replacement's environment is clean. That
# comment explicitly exempts this script as "a deliberate operator", which is
# false when the parent process is a test. This gate fails CLOSED instead, and
# does not depend on every future test author remembering a convention.
#
# Two sanctioned ways past it:
#   RUNTIME_DIR=<dir>  — the isolation override (B16 / lifecycle.runtime_dir).
#                        Spawning into your own runtime dir is never a hijack.
#   MIND_ALLOW_SHARED_DAEMON_FROM_TEST=1 — deliberate-operator opt-in for the
#                        one test that must use the shared dir
#                        (test_daemon_orphan_prevention.py counts system-wide
#                        mind_api.src processes, so RUNTIME_DIR isolation alone
#                        cannot make it safe — the marker plus this opt-in are).
# DELIBERATE trade-off: the gate does NOT first check whether a live daemon
# exists to protect. Consequence — on a box with NO daemon running, a test that
# relied on wrapper auto-spawn now FAILS instead of silently spawning. That is
# the intended direction: the failure is loud, names the fix (set RT_DIR), and
# costs one line in the test, whereas the alternative (refuse only when a live
# daemon would be killed) reopens the original harm on any box without the
# .mind-data/ tier — there, a test-spawned daemon can resolve the test's tmp
# world and then serve it to the whole fleet. Fail closed, per .
#
# Condition compares the dir ACTUALLY being claimed, not env-var names: the
# sibling gate in _runtime.sh must accept RT_DIR (what _daemon_fixture.py sets)
# as well as RUNTIME_DIR, and comparing the resolved path also catches
# RUNTIME_DIR pointed explicitly AT the shared dir. Keep both conditions in sync.
if [ -n "${PYTEST_CURRENT_TEST:-}" ] \
   && [ "$RT_DIR" = "$PROJECT_ROOT/mind_api/state" ] \
   && [ "${MIND_ALLOW_SHARED_DAEMON_FROM_TEST:-}" != "1" ]; then
    _log "REFUSED shared-runtime claim from pytest (${PYTEST_CURRENT_TEST})"
    {
        echo "[daemon-start] REFUSED: recycle/spawn requested from inside pytest"
        echo "[daemon-start]   PYTEST_CURRENT_TEST=${PYTEST_CURRENT_TEST}"
        echo "[daemon-start]   no runtime isolation -> would claim SHARED $RT_DIR"
        echo "[daemon-start] This would kill the live daemon and repoint every"
        echo "[daemon-start] agent on this box at the test's world resolution."
        echo "[daemon-start] Fix: set RUNTIME_DIR=<tmp dir> in the test env, or"
        echo "[daemon-start] MIND_ALLOW_SHARED_DAEMON_FROM_TEST=1 if the test"
        echo "[daemon-start] genuinely requires the shared runtime. (g-115-3329)"
    } >&2
    exit 1
fi

# Slow path: need to recycle and/or spawn. Acquire wrapper-side spawn
# mutex so a concurrent /start in another terminal (or a wrapper
# auto-spawn racing /start) doesn't also launch a daemon. The daemon's
# own _spawn_lock catches the race at bind time but only after multiple
# processes have already been launched (10s self-supersession convergence
# window). This mutex prevents the multi-daemon-launched state.
if ! _acquire_spawn_lock; then
    _log "another spawn holds the wrapper-side mutex; waiting for daemon to publish"
    waited=0; max_wait=100
    while [ "$waited" -lt "$max_wait" ]; do
        new_pid="$(_read_pid || echo "")"
        new_port="$(_read_port || echo "")"
        if [ -n "$new_pid" ] && [ -n "$new_port" ] && \
           _health_probe "$new_port"; then
            _log "daemon ready via concurrent spawn (PID=$new_pid port=$new_port)"
            exit 0
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
    echo "[daemon-start] ERROR: concurrent spawn did not publish a healthy daemon within 10s" >&2
    exit 1
fi
trap '_release_spawn_lock' EXIT

# Re-probe inside the lock — daemon may have come up while we waited.
#  fix: health-probe alone (kill -0 false-negatives on Windows).
existing_pid="$(_read_pid || echo "")"
existing_port="$(_read_port || echo "")"
if [ -n "$existing_pid" ] && [ -n "$existing_port" ] && \
   _health_probe "$existing_port" && \
   [ "$FORCE_RESTART" != "1" ]; then
    _log "daemon came up during lock wait (PID=$existing_pid, port=$existing_port)"
    exit 0
fi

#  v3: read parent_pid (py.exe launcher PID) for the kill
# path — see _force_kill_tree.
existing_parent_pid="$(_read_parent_pid || echo "")"

# 1. Check if daemon is already up and healthy.
#  fix: health-probe FIRST. POSIX `kill -0` false-negatives on
# MSYS Git Bash against detached native-Windows processes — concluding
# "stale PID, cleaning up" against a HEALTHY daemon was the root cause of
# the 112-restarts-in-24h pileup. Health-probe on the published port is
# the authoritative liveness signal; kill -0 only matters when we need
# to escalate a SIGTERM (the kill itself works fine — only `kill -0`
# false-negatives).
need_recycle=0
if [ -n "$existing_pid" ] && [ -n "$existing_port" ]; then
    if _health_probe "$existing_port"; then
        # Daemon is alive and responsive — verified by health probe,
        # regardless of what kill -0 says about the PID.
        if [ "$FORCE_RESTART" != "1" ]; then
            _log "daemon already running (PID=$existing_pid, port=$existing_port)"
            exit 0
        fi
        # --restart: healthy but a daemon-code commit landed, so the
        # in-memory code is stale. Recycle.
        #
        # Claim-liveness gate (, Layer B for guard-1151): a restart
        # of a HEALTHY daemon issued mid-goal is the redundant-restart shape —
        # canonical:  restarted 47 min after its claim was superseded
        # and released (2026-07-16, rb-3735). When the invoking agent has an
        # in_flight goal, verify the claim is still live before recycling.
        # ONLY this healthy+--restart branch is gated: the unhealthy/stale
        # recovery paths below never consult the (possibly down) daemon.
        # Fail-open on every probe error. Override: MIND_RESTART_FORCE_STALE_CLAIM=1.
        if [ -n "${MIND_AGENT:-}" ] && [ "${MIND_RESTART_FORCE_STALE_CLAIM:-0}" != "1" ]; then
            # grep -oE + head -n1 extracts exactly ONE goal-id even if the
            # transport ever emits a duplicated/concatenated body again (the
            # 2026-07-18 "" rt_call retry double-emit —
            # fixed at the source in _runtime.sh rt_call, defended here too).
            # null / absent field yields empty, caught by the -n test below.
            _clc_gid=$(bash "$SCRIPT_DIR/team-state-read.sh" --field "agent_status.${MIND_AGENT}.in_flight.goal_id" --json 2>/dev/null \
                | grep -oE 'g-[0-9]+-[0-9]+' | head -n1) || _clc_gid=""
            if [ -n "$_clc_gid" ]; then
                if ! bash "$SCRIPT_DIR/claim-liveness-check.sh" "$_clc_gid"; then
                    _log "REFUSED --restart: claim on in-flight goal $_clc_gid is STALE (superseded/released while executing — guard-1151). The daemon is HEALTHY; a recycle now is the redundant-restart shape. Re-read your goal + the coordination board. Override: MIND_RESTART_FORCE_STALE_CLAIM=1 bash core/scripts/mind-api-start.sh --restart"
                    exit 3
                fi
            fi
        fi
        _log "daemon healthy (PID=$existing_pid parent=${existing_parent_pid:-?}) but --restart requested; recycling for fresh code"
        need_recycle=1
    elif _is_pid_alive "$existing_pid"; then
        # Health probe failed but kill -0 says the PID is alive —
        # genuinely unresponsive daemon (not a Windows false-negative).
        _log "daemon PID $existing_pid alive but not responding on port $existing_port"
        need_recycle=1
    else
        # Health probe failed AND kill -0 says dead — actually stale.
        # (Windows false-negative would have been caught by the health
        # probe above; if /health doesn't respond AND kill -0 says dead,
        # the PID file is genuinely stale.)
        _log "stale PID file (PID=$existing_pid is dead and unresponsive), cleaning up"
        # Stale: no process to kill, just clean files (below).
    fi
elif [ -n "$existing_pid" ]; then
    # PID file but no port file — partial state.
    if _is_pid_alive "$existing_pid"; then
        _log "PID $existing_pid alive but no port file — killing orphan"
        need_recycle=1
    fi
fi

#  v3 BULLETPROOF KILL:
#   1. _kill_escalate sends SIGTERM with a SHORT graceful window. The wait
#      now uses health-probe (curl) when port is known — the  fix
#      applied to the second site (kill wait) that the original fix missed.
#   2. _force_kill_tree force-kills BOTH the child python.exe AND the py.exe
#      parent (read from daemon.parent.pid). No Win32 Get-CimInstance
#      .ParentProcessId lookup — that lookup silently no-op'd in ~17% of
#      recycles when the child had already exited, orphaning the py.exe
#      parent.
#   3. Each Stop-Process is CommandLine-guarded against 'mind_api' to defend
#      against Windows PID reuse.
#   4. Results are logged to spawn.log — no more silent >/dev/null 2>&1.
if [ "$need_recycle" = "1" ]; then
    _kill_escalate "$existing_pid" "$existing_port"
    _force_kill_tree "$existing_pid" "$existing_parent_pid"
fi

# Clean state files regardless of whether we killed. A stale PID file
# without a live process is the simplest case — no kill, just rm.
_clean_runtime_files

# DELIBERATELY no implicit `_sweep_orphan_daemons "" ""` call here. An
# empty-args sweep kills every mind_api.src process system-wide, which
# collides with OTHER repos running their own mind_api daemons (same
# cmdline, same regex — Win32_Process exposes no cwd/env discriminator).
# The `_force_kill_tree` above (kill by KNOWN PIDs from daemon.parent.pid
# + daemon.pid) is the authoritative reap and is repo-safe by construction.
# User-invoked `daemon-orphan-sweep.sh --clean` remains available for
# explicit cleanup if a regression slips through.

# 2. Spawn the daemon.
py_cmd="$(_python_launcher)"
if [ -z "$py_cmd" ]; then
    echo "[daemon-start] ERROR: no usable Python launcher (Windows: 'py' launcher not found — refusing POSIX python3 per g-115-733; POSIX: python3 not found)" >&2
    _log "ERROR: no usable Python launcher"
    exit 1
fi

_log "spawning daemon with: $py_cmd -m mind_api.src"

(
    cd "$PROJECT_ROOT" && \
    $py_cmd -m mind_api.src >> "$SPAWN_LOG" 2>&1 &
    disown $! 2>/dev/null || true
) >/dev/null 2>&1

# 3. Wait up to 10s for the daemon to become ready.
waited=0
max_wait=100  # 100 * 100ms = 10s
while [ "$waited" -lt "$max_wait" ]; do
    new_port="$(_read_port || echo "")"
    if [ -n "$new_port" ] && _health_probe "$new_port"; then
        _log "daemon ready (port=$new_port) after ~$((waited * 100))ms"
        exit 0
    fi
    sleep 0.1
    waited=$((waited + 1))
done

# 4. Timed out — daemon did not come up.
echo "[daemon-start] ERROR: daemon did not become ready within 10s" >&2
_log "ERROR: daemon did not become ready within 10s"
exit 1
