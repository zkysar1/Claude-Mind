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

RT_DIR="$PROJECT_ROOT/mind_api/state"
PID_FILE="$RT_DIR/daemon.pid"
PORT_FILE="$RT_DIR/daemon.port"
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

_clean_runtime_files() {
    rm -f "$PID_FILE" "$PORT_FILE" 2>/dev/null || true
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

# _kill_escalate PID — SIGTERM → 5s wait → SIGKILL if still alive.
_kill_escalate() {
    local pid="$1"
    _log "sending SIGTERM to PID $pid"
    kill -TERM "$pid" 2>/dev/null || true

    # Wait up to 5 seconds for graceful exit.
    local waited=0
    while [ "$waited" -lt 50 ]; do
        if ! _is_pid_alive "$pid"; then
            _log "PID $pid exited after SIGTERM"
            return 0
        fi
        sleep 0.1
        waited=$((waited + 1))
    done

    # Still alive — escalate to SIGKILL.
    _log "PID $pid still alive after 5s, sending SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 0.2
    if _is_pid_alive "$pid"; then
        _log "WARNING: PID $pid survived SIGKILL"
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
existing_pid="$(_read_pid || echo "")"
existing_port="$(_read_port || echo "")"
if [ -n "$existing_pid" ] && [ -n "$existing_port" ] && \
   _is_pid_alive "$existing_pid" && _health_probe "$existing_port" && \
   [ "$FORCE_RESTART" != "1" ]; then
    _log "daemon already running (fast-path PID=$existing_pid, port=$existing_port)"
    exit 0
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
           _is_pid_alive "$new_pid" && _health_probe "$new_port"; then
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
existing_pid="$(_read_pid || echo "")"
existing_port="$(_read_port || echo "")"
if [ -n "$existing_pid" ] && [ -n "$existing_port" ] && \
   _is_pid_alive "$existing_pid" && _health_probe "$existing_port" && \
   [ "$FORCE_RESTART" != "1" ]; then
    _log "daemon came up during lock wait (PID=$existing_pid, port=$existing_port)"
    exit 0
fi

# 1. Check if daemon is already up and healthy.
if [ -n "$existing_pid" ] && [ -n "$existing_port" ]; then
    if _is_pid_alive "$existing_pid"; then
        if _health_probe "$existing_port"; then
            if [ "$FORCE_RESTART" != "1" ]; then
                # Daemon is alive and responsive — nothing to do.
                _log "daemon already running (PID=$existing_pid, port=$existing_port)"
                exit 0
            fi
            # --restart: healthy but a daemon-code commit landed, so the
            # in-memory code is stale. Recycle. (Falls through to the
            # process-tree reap + spawn below — same path as the
            # unresponsive case.)
            _log "daemon healthy (PID=$existing_pid) but --restart requested; recycling for fresh code"
            _kill_escalate "$existing_pid"
            _clean_runtime_files
        else
            # PID alive but health probe failed — unresponsive daemon.
            _log "daemon PID $existing_pid alive but not responding on port $existing_port"
            _kill_escalate "$existing_pid"
            _clean_runtime_files
        fi
    else
        # PID file present but process is dead — stale PID.
        _log "stale PID file (PID=$existing_pid is dead), cleaning up"
        _clean_runtime_files
    fi
elif [ -n "$existing_pid" ]; then
    # PID file but no port file — partial state.
    if _is_pid_alive "$existing_pid"; then
        _log "PID $existing_pid alive but no port file — killing orphan"
        _kill_escalate "$existing_pid"
    fi
    _clean_runtime_files
fi

# : clean-stop guard mirroring _runtime.sh rt_daemon_kill's
# process-TREE reap. The _is_pid_alive recovery gates above use POSIX
# `kill -0`, which false-negatives on a detached native-Windows
# `py -m mind_api.src` under MSYS — a live orphan slips past as "dead",
# _kill_escalate is never called, and we would spawn a replacement on
# top of it (observed leak: 170 started / 6 stopped, 17 orphans).
# existing_pid is the python.exe CHILD; killing only it orphans the
# py.exe launcher PARENT (Windows has no POSIX process-group cascade) —
# the root cause of the  114-process pileup. Reap the WHOLE
# tree (parent + child), PID-reuse-guarded by a `mind_api.src`
# CommandLine match on BOTH so a recycled PID is never force-killed.
# By the time we reach here the captured `existing_pid` is never a
# live healthy daemon: either the health check `exit 0`'d (no --restart,
# healthy) and we never got here, OR it was already _kill_escalate'd
# above (unresponsive, OR --restart recycling a healthy one). So
# force-stopping the captured predecessor tree cannot kill a serving daemon.
if [ -n "$existing_pid" ]; then
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*)
            powershell.exe -NoProfile -Command "\$c=Get-CimInstance Win32_Process -Filter \"ProcessId=$existing_pid\" -ErrorAction SilentlyContinue; if (\$c -and \$c.CommandLine -match 'mind_api\.src') { \$pp=\$c.ParentProcessId; \$p=Get-CimInstance Win32_Process -Filter \"ProcessId=\$pp\" -ErrorAction SilentlyContinue; if (\$p -and \$p.CommandLine -match 'mind_api\.src') { Stop-Process -Id \$pp -Force -ErrorAction SilentlyContinue }; Stop-Process -Id $existing_pid -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1 || true
            ;;
    esac
fi

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
