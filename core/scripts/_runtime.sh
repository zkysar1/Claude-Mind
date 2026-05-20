#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Layer 1 — CLIENT: HTTP client lib. See core/BOUNDARY.md.
# Shared shell helper for daemon-aware wrappers.
#
# Every migrated wrapper sources this file. The contract:
#   1. rt_call <method> <path> [--query "k=v&..."] [--body @file|-data "..."] [--agent NAME]
#      - tries the daemon first
#      - auto-starts if "connection refused"
#      - falls back to direct python on persistent failure
#
# Wrappers that need byte-exact equivalence with the pre-migration CLI must
# implement their own fallback inline, because the daemon-side endpoint and
# the CLI may differ on edge cases (line endings, side-effects). rt_fallback
# is a sentinel exit code; callers branch on it.
#
# Why bash and not python: this code is sourced by ~200 thin wrappers. Adding
# a python helper would defeat the runtime's purpose by re-paying Python
# startup on every call. Pure bash + curl keeps the cold path at ~10ms.

# Idempotency guard — _paths.sh may already have been sourced by the caller.
# MUST gate on `agent_dir` function presence (Phase 2.5.C), NOT on PROJECT_ROOT —
# some callers (retrieve.sh) set PROJECT_ROOT directly without sourcing
# _paths.sh, which previously left agent_dir undefined when rt_session_mode
# tried to call it.
if ! declare -F agent_dir >/dev/null 2>&1; then
    _RT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # shellcheck disable=SC1091
    source "$_RT_SCRIPT_DIR/_paths.sh"
    unset _RT_SCRIPT_DIR
fi

RT_DIR="${RT_DIR:-$PROJECT_ROOT/mind_api/state}"
RT_PID_FILE="${RT_PID_FILE:-$RT_DIR/daemon.pid}"
RT_PORT_FILE="${RT_PORT_FILE:-$RT_DIR/daemon.port}"
RT_SPAWN_LOG="${RT_SPAWN_LOG:-$RT_DIR/spawn.log}"

# Tunables. Override per-invocation by exporting before sourcing.
# RT_CURL_TIMEOUT must cover two distinct worst-case wait sources:
#   1. Cold-cache loads — the tree endpoint's first request after a daemon
#      (re)start pays the OneDrive cost of reading every referenced .md
#      file (~5s on this machine). Subsequent calls are <250ms.
#   2. _atomic_write retry storms — OneDrive holds an exclusive lock on
#      world/*.jsonl while syncing, causing os.replace to fail with WinError 5.
#      _fileops._atomic_write retries 10× with exponential backoff capped at
#      5s/retry; the worst-case sum is ~22s before the in-place-rewrite
#      fallback fires. Raised from 15→45 after the 2026-05-20 false-down
#      incident where slow-due-to-write-contention was misread as
#      "daemon unreachable." See rt_no_daemon_error below for the paired
#      verify-before-report fix.
RT_CURL_TIMEOUT="${RT_CURL_TIMEOUT:-45}"     # seconds per request
RT_SPAWN_WAIT_MS="${RT_SPAWN_WAIT_MS:-5000}" # max wait for spawned daemon
RT_SPAWN_POLL_MS="${RT_SPAWN_POLL_MS:-50}"   # poll interval while waiting

# Internal: write a warning to stderr ONCE per wrapper invocation. Calling
# scripts may set RT_WARNED=1 to suppress duplicate warnings.
rt_warn() {
    if [ "${RT_WARNED:-0}" != "1" ]; then
        echo "[runtime] $*" >&2
        RT_WARNED=1
    fi
}

# Convention: value-arg parsing under set -u
# ------------------------------------------
# When a wrapper accepts `--flag VALUE` pairs and runs under `set -euo pipefail`
# (which all daemon-aware wrappers do), `$2` is "unbound variable" when the
# caller passes the flag without a value. That crashes the wrapper with a
# `set -u` error before argparse can produce its canonical "expected one
# argument" message, breaking equivalence with the pre-migration CLI.
#
# Standard pattern, applied across every PR 4/5/6 wrapper:
#
#   case "$1" in
#       --my-flag)
#           VAR="${2-}"                         # ${2-} = "" when $2 is unset
#           PASSTHROUGH+=("$1" "${2-}")          # preserve for daemon passthrough
#           shift $(( $# >= 2 ? 2 : 1 ));;       # shift 2 if available, else 1
#   esac
#
# For flags that take TWO values (rare — e.g. tree-read's --child-path,
# aspirations-query's --goal-field), the shift is three-tier:
#       shift $(( $# >= 3 ? 3 : ($# >= 2 ? 2 : 1) ))
#
# The daemon then surfaces a canonical error for the empty value instead
# of bash's "$2: unbound variable".

# rt_session_mode — read <agent>/session/agent-mode without shelling out.
# Prints the mode (reader/assistant/autonomous) or empty string when:
#   - MIND_AGENT is unset (background processes, daemon-internal callers)
#   - the file doesn't exist (UNINITIALIZED agent)
# Mirrors the inline read in session-mode-get.sh and the auto-inject block
# in retrieve.sh. Single source of truth so future mode-aware wrappers
# don't re-invent the read pattern.
rt_session_mode() {
    if [ -z "${MIND_AGENT:-}" ]; then
        return 0
    fi
    local mode_file="$(agent_dir "${MIND_AGENT}")/session/agent-mode"
    if [ -f "$mode_file" ]; then
        tr -d '[:space:]' < "$mode_file" 2>/dev/null || true
    fi
}

# rt_url_encode <value>
# Percent-encode a string for safe inclusion in a URL query parameter.
# Pure bash; no subprocess. Uses RFC 3986 unreserved characters.
rt_url_encode() {
    local LC_ALL=C
    local str="$1"
    local out=""
    local i ch
    for (( i=0; i<${#str}; i++ )); do
        ch="${str:i:1}"
        case "$ch" in
            [a-zA-Z0-9.~_-]) out+="$ch" ;;
            *) printf -v out '%s%%%02X' "$out" "'$ch" ;;
        esac
    done
    printf '%s' "$out"
}

# rt_port — print the daemon's port, or empty string if not running.
rt_port() {
    [ -f "$RT_PORT_FILE" ] || return 0
    tr -d '[:space:]' < "$RT_PORT_FILE"
}

# rt_base_url — return the daemon's base URL, or empty.
rt_base_url() {
    local port
    port="$(rt_port)"
    [ -n "$port" ] || return 0
    echo "http://127.0.0.1:${port}"
}

# rt_is_up — fast probe. Returns 0 if a daemon answers /v1/admin/health.
rt_is_up() {
    local base
    base="$(rt_base_url)"
    [ -n "$base" ] || return 1
    # --max-time 1: don't hang the wrapper if the daemon is wedged.
    # --fail: nonzero on HTTP 4xx/5xx, so a bad daemon counts as down.
    curl -s -f --max-time 1 "$base/v1/admin/health" >/dev/null 2>&1
}

# rt_check_staleness — warn ONCE per shell when the running daemon's git SHA
# differs from on-disk HEAD AND the daemon's CODE SURFACE actually changed
# between the two (core/scripts/mind-api-code-changed.sh — mind_api/src/** or
# core/scripts/_*.py). Closes the  incident class where an old
# daemon serves stale routes after a framework PR lands, WITHOUT the
# false-positive churn where a docs/world/meta-only commit moves HEAD but
# leaves the daemon's loaded code current (the user-flagged "restart for
# every change in the broad directory" anti-goal). Output is a single
# stderr line; never fails the caller. ESCALATION ( Change 1): on
# a genuine daemon-code mismatch ALSO sets RT_STALENESS_RESTART_PENDING=1 so
# the next rt_ensure_running call recycles the stale process (one-shot per
# shell via RT_STALENESS_RESTARTED). Skipped (fail-open) when:
#   - not in a git repo (rt_on_disk_sha returns empty)
#   - the daemon predates this check (response lacks git_head_sha field)
#   - the field is null (read_git_head_sha returned None at startup)
rt_check_staleness() {
    [ "${RT_STALENESS_WARNED:-0}" = "1" ] && return 0
    local base
    base="$(rt_base_url)"
    [ -n "$base" ] || return 0
    local on_disk
    on_disk="$(rt_on_disk_sha)" || return 0
    [ -n "$on_disk" ] || return 0
    local response
    response=$(curl -s -f --max-time 1 "$base/v1/admin/health" 2>/dev/null) || return 0
    # Field-presence check FIRST. Without it, the pure-bash extract below
    # silently turns a missing field into garbage (running_sha = "{ok:true")
    # and false-warns. Bash `${var#pattern}` returns var unchanged when
    # pattern does not match — same trap. Older daemons (pre-this-fix) hit
    # this path; treat them as "can't compare" and stay silent.
    case "$response" in
        *'"git_head_sha"'*) ;;
        *) return 0;;
    esac
    # Pure-bash JSON field extract — avoids spawning python on every wrapper call.
    local running_sha="${response#*\"git_head_sha\":}"
    running_sha="${running_sha%%,*}"
    running_sha="${running_sha%%\}*}"
    running_sha="${running_sha//\"/}"
    running_sha="${running_sha// /}"
    if [ -z "$running_sha" ] || [ "$running_sha" = "null" ]; then
        return 0
    fi
    if [ "$running_sha" != "$on_disk" ]; then
        # The cheap `!=` is the fast pre-filter: in the daemon-fresh steady
        # state running_sha == on_disk and we never reach here, so the
        # subprocess below is paid ONLY in the already-stale window — never
        # on the hot path. Now ask the PRECISE question: did the daemon's
        # code surface actually change between what it loaded (running_sha)
        # and HEAD? A docs/world/meta/*.sh-only commit moves HEAD but leaves
        # the daemon current — warning + recycling it then is exactly the
        # churn the user flagged. SINGLE SOURCE OF TRUTH for the boundary is
        # mind-api-code-changed.sh (also the post-commit hook's predicate); it
        # fails toward "changed" (exit 0) on any git error, so an
        # unresolvable running_sha still arms the restart (never serve stale
        # code). Do NOT inline the pathspec here — keep it in the one script.
        # CRITICAL — resolve the sibling script via BASH_SOURCE, NOT
        # PROJECT_ROOT. Callers can pre-export PROJECT_ROOT (test harnesses
        # do this to skip _paths.sh's agent-binding side effects) at values
        # other than the true repo root; depending on it here would break
        # silently — `if bash <wrong-path>; then` evaluates false (rc 127)
        # and the staleness arming is suppressed with no diagnostic. Same
        # pure-bash dirname idiom this file's top-of-source guard uses.
        if bash "${BASH_SOURCE[0]%/*}/mind-api-code-changed.sh" "$running_sha" >/dev/null 2>&1; then
            echo "[runtime] WARNING: daemon is running stale code (sha ${running_sha:0:8}, on-disk ${on_disk:0:8}). Auto-restart will fire on the next rt_ensure_running call (one-shot)." >&2
            export RT_STALENESS_WARNED=1
            #  Change 1: escalate from warn-only to actionable. The
            # sentinel is consumed by rt_ensure_running, which kills+respawns
            # the stale process so the retry hits fresh code. One-shot per shell.
            export RT_STALENESS_RESTART_PENDING=1
        fi
    fi
}

# rt_on_disk_sha — read git HEAD without shelling to `git`. Mirrors the
# resolution in mind_api/src/__init__.py:read_git_head_sha so wrapper and
# daemon use identical paths.
rt_on_disk_sha() {
    local git_path="$PROJECT_ROOT/.git"
    local gitdir
    if [ -f "$git_path" ]; then
        local content
        content="$(cat "$git_path" 2>/dev/null)"
        case "$content" in
            "gitdir:"*) gitdir="${content#gitdir:}"; gitdir="${gitdir# }";;
            *) return 1;;
        esac
    elif [ -d "$git_path" ]; then
        gitdir="$git_path"
    else
        return 1
    fi
    local head
    head="$(cat "$gitdir/HEAD" 2>/dev/null)" || return 1
    case "$head" in
        "ref: "*)
            local ref_path="$gitdir/${head#ref: }"
            if [ ! -f "$ref_path" ] && [ -f "$gitdir/commondir" ]; then
                local common
                common="$(cat "$gitdir/commondir" 2>/dev/null)"
                ref_path="$gitdir/$common/${head#ref: }"
            fi
            cat "$ref_path" 2>/dev/null | tr -d '[:space:]'
            ;;
        *)
            echo "$head" | tr -d '[:space:]'
            ;;
    esac
}

# rt_python_launcher — print the right python launcher for the current OS.
# Single source of truth for Windows-vs-POSIX launcher selection. On Windows
# Git Bash, `python3` may resolve to the Microsoft Store stub; `py -3` is
# the reliable entry point when available. Used by rt_spawn and by wrappers
# that need a short inline python call (e.g. JSON parse on response handling).
rt_python_launcher() {
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*)
            if command -v py >/dev/null 2>&1; then
                echo "py -3"
                return 0
            fi
            #  v3: never fall back to bare `python3` on Windows.
            # Git-bash python3 is frequently POSIX/MSYS-flavored; under it
            # _path_helpers.absolutize() returns a RELATIVE WORLD_DIR
            # (Path("C:/...") is a relative PosixPath there) and the daemon
            # mkdir-mirrors C<U+F03A>/Users/.../<WORLD_DIR> at cwd then
            # crash-loops. Fail loud — the caller logs + aborts the spawn.
            return 1
            ;;
    esac
    echo "python3"
}

# rt_spawn — start the daemon in the background. Same bash-native pattern on
# both POSIX and Windows (Git Bash): launch in a subshell with redirects and
# `&`, then disown so the daemon survives the wrapper exiting. Tested on
# Windows 10 + Git Bash 2.x; the `cmd /c start /B` indirection that earlier
# versions used did not always inherit cwd correctly under MSYS path
# translation, so we avoid it.
rt_spawn() {
    local stamp
    stamp="$(date +%Y-%m-%dT%H:%M:%S)"
    mkdir -p "$RT_DIR"

    # Resolve the launcher BEFORE the destructive rt_daemon_kill / the
    # "attempting" log — never kill a predecessor we cannot replace.
    local py_cmd
    py_cmd="$(rt_python_launcher)"
    if [ -z "$py_cmd" ]; then
        # : Windows host without `py`. Refusing POSIX python3 (it
        # would resolve a RELATIVE WORLD_DIR -> cwd-mirror the cruft tree +
        # crash-loop). Do NOT spawn. rt_wait_for_ready is the SINGLE source
        # of truth for readiness — it times out and the caller surfaces a
        # loud daemon-down error. Return 0 (best-effort): a non-zero return
        # here would trip `set -e` in any future bare caller. Mirrors the
        # rt_call:489 discipline — guard internally, never rely on an outer
        # `if` to suppress `set -e`. DO NOT change this to `return 1`.
        echo "[$stamp] rt_spawn — ABORT: no safe Python launcher (Windows host without 'py'). Daemon NOT started; caller reports daemon-down." >> "$RT_SPAWN_LOG"
        return 0
    fi

    # : clean-stop any live predecessor BEFORE spawning a
    # replacement. rt_spawn is only reached when the daemon is down, stale,
    # or unreachable — never when a healthy daemon is serving — so the PID in
    # RT_PID_FILE is always either dead (rt_daemon_kill silently no-ops) or a
    # hung orphan (the accumulation source  swept with a one-time
    # PowerShell census). Without this single chokepoint every down-path
    # spawn (rt_ensure_running, the  staleness restart) stacked a
    # new process on top of the old one (observed 170 started / 6 stopped).
    rt_daemon_kill
    echo "[$stamp] rt_spawn — attempting daemon start" >> "$RT_SPAWN_LOG"

    (
        cd "$PROJECT_ROOT" && \
        $py_cmd -m mind_api.src >> "$RT_SPAWN_LOG" 2>&1 &
        disown $! 2>/dev/null || true
    ) >/dev/null 2>&1
}

# rt_wait_for_ready — poll up to RT_SPAWN_WAIT_MS for the daemon to appear.
# Returns 0 if ready, 1 if timed out.
rt_wait_for_ready() {
    local waited=0
    local step="$RT_SPAWN_POLL_MS"
    # Pre-compute the sleep duration in seconds once — avoids spawning awk
    # ~20×/sec inside the poll loop (200-400ms wasted per second on Windows).
    local sleep_seconds
    printf -v sleep_seconds '%d.%03d' $((step / 1000)) $((step % 1000))
    while [ "$waited" -lt "$RT_SPAWN_WAIT_MS" ]; do
        if rt_is_up; then
            return 0
        fi
        sleep "$sleep_seconds"
        waited=$((waited + step))
    done
    return 1
}

# rt_acquire_spawn_lock / rt_release_spawn_lock — wrapper-side spawn mutex.
#
# The daemon's own _spawn_lock (mind_api/src/__main__.py) serializes the
# alive-check -> write_pid_and_port window AT BIND TIME but only AFTER
# multiple `python -m mind_api.src` processes have already been launched.
# Two concurrent rt_ensure_running calls (different Claude sessions) both
# see rt_is_up == false, both reach rt_spawn, both launch a daemon. The
# loser's _spawn_lock branch refuses ("daemon already running ... refusing
# to start") and exits — but the process started, bound briefly, and only
# the self-supersession poll (10s) converges. During that window the user
# sees N daemons on N ephemeral ports.
#
# This wrapper-side mutex prevents N rt_spawn launches in the first place.
# Uses set -o noclobber for atomic create-if-absent on POSIX + Git Bash;
# 45s stale TTL = daemon _spawn_lock TTL (30s) + spawn ready window (5s) +
# margin so a wedged spawn is recoverable. Stale-break is immediate-retry.
rt_acquire_spawn_lock() {
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

rt_release_spawn_lock() {
    rm -f "$RT_DIR/daemon.wrapper.lock" 2>/dev/null || true
}

# rt_daemon_kill — terminate the running daemon process TREE by PID
# ( Change 1; graceful-first + tree-reap ).
#
# daemon.pid holds the python.exe CHILD pid (os.getpid() inside
# mind_api.src). On Windows that child was launched by a `py -3 -m
# mind_api.src` py.exe PARENT. Windows has no POSIX process-group
# cascade: killing only the child orphans the py.exe parent, and that
# orphaning is the code root cause of the  114-process
# pileup. The force-reap below kills the WHOLE tree (parent + child),
# PID-reuse-guarded by a `mind_api.src` CommandLine match on BOTH so a
# recycled PID for an unrelated process is never force-killed.
#
# Graceful SIGTERM is attempted first. On POSIX it reaches the child,
# whose signal handler (__main__.py:135) runs the clean shutdown and
# logs the "stopped" lifecycle event — closing the started>>stopped
# asymmetry there. On Git Bash MSYS, `kill -TERM` does not reliably
# reach a detached native-Windows python.exe and never reaches the
# py.exe parent, so the Windows force-reap is authoritative and the
# daemon does NOT log "stopped" on Windows external kill
# (TerminateProcess bypasses the Python signal handler — a documented
# platform limitation, not a regression). Fail-open at every step; a
# missing or unkillable PID must not abort the caller.
rt_daemon_kill() {
    [ -f "$RT_PID_FILE" ] || return 0
    local pid
    pid="$(cat "$RT_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$pid" ] || return 0
    # Graceful first — POSIX child logs "stopped" and exits cleanly.
    kill -TERM "$pid" >/dev/null 2>&1 || true
    local waited=0
    while [ "$waited" -lt 30 ]; do          # ~3s graceful window
        kill -0 "$pid" >/dev/null 2>&1 || break
        sleep 0.1
        waited=$((waited + 1))
    done
    # Force-reap the process TREE: the python.exe child (= $pid) plus
    # its py.exe launcher parent, each PID-reuse-guarded by a
    # mind_api.src CommandLine match. Unconditional — it force-reaps a
    # survivor and is a silent no-op when SIGTERM already exited it.
    # POSIX uses kill -KILL on the child (no py.exe parent layer there).
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*)
            powershell.exe -NoProfile -Command "\$c=Get-CimInstance Win32_Process -Filter \"ProcessId=$pid\" -ErrorAction SilentlyContinue; if (\$c -and \$c.CommandLine -match 'mind_api\.src') { \$pp=\$c.ParentProcessId; \$p=Get-CimInstance Win32_Process -Filter \"ProcessId=\$pp\" -ErrorAction SilentlyContinue; if (\$p -and \$p.CommandLine -match 'mind_api\.src') { Stop-Process -Id \$pp -Force -ErrorAction SilentlyContinue }; Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1 || true
            ;;
        *) kill -KILL "$pid" >/dev/null 2>&1 || true;;
    esac
    rm -f "$RT_PID_FILE" 2>/dev/null || true
}

# rt_ensure_running — start the daemon if down. Returns 0 if (re)ready, 1 otherwise.
rt_ensure_running() {
    #  Change 1: stale-code auto-restart. If rt_check_staleness
    # flagged a SHA mismatch, the daemon is UP but serving stale code, so the
    # rt_is_up early-return below would mask the problem forever. Recycle the
    # process ONCE per shell (RT_STALENESS_RESTARTED), then fall through so
    # rt_wait_for_ready confirms the fresh daemon. Honors RT_NO_AUTOSPAWN so
    # health-probe / daemon-down test paths still observe a true down state.
    if [ "${RT_STALENESS_RESTART_PENDING:-0}" = "1" ] && \
       [ "${RT_STALENESS_RESTARTED:-0}" != "1" ] && \
       [ "${RT_NO_AUTOSPAWN:-0}" != "1" ]; then
        export RT_STALENESS_RESTARTED=1
        unset RT_STALENESS_RESTART_PENDING
        rt_warn "daemon running stale code — auto-restarting (g-115-745)"
        # : rt_spawn now clean-stops the stale predecessor itself
        # (single chokepoint). No explicit rt_daemon_kill here — coordinating
        # with  Change 1 so the restart reaps reliably on Windows.
        # Wrapper-side spawn mutex: another wrapper may already be doing the
        # stale-code restart; let them finish and reuse the fresh daemon.
        if rt_acquire_spawn_lock; then
            rt_spawn
            local rc=1
            if rt_wait_for_ready; then rc=0; fi
            rt_release_spawn_lock
            return $rc
        fi
        if rt_wait_for_ready && rt_is_up; then
            return 0
        fi
        return 1
    fi
    if rt_is_up; then
        return 0
    fi
    # Opt-in side-effect-free guard. Callers that must observe a genuine
    # daemon-down state without spawning a daemon (health probes, the
    # daemon-down test suite, CI gates) export RT_NO_AUTOSPAWN=1. Defaults
    # OFF — normal wrappers keep auto-spawning. Mirrored in rt_try_autospawn
    # so BOTH spawn entrypoints honor the same flag.
    if [ "${RT_NO_AUTOSPAWN:-0}" = "1" ]; then
        rt_warn "daemon down; auto-spawn suppressed (RT_NO_AUTOSPAWN=1)"
        return 1
    fi
    # Wrapper-side spawn mutex (). The daemon's own _spawn_lock
    # catches the race at bind time but only after multiple processes have
    # already been launched (10s self-supersession convergence window).
    # This mutex prevents the multi-daemon-launched state in the first place.
    if rt_acquire_spawn_lock; then
        # Re-check inside the lock — daemon may have come up while we waited.
        if rt_is_up; then
            rt_release_spawn_lock
            return 0
        fi
        rt_spawn
        local rc=1
        if rt_wait_for_ready; then rc=0; fi
        rt_release_spawn_lock
        return $rc
    fi
    # Lock held by another wrapper — wait for them to publish the daemon
    # instead of racing in with a parallel spawn.
    if rt_wait_for_ready; then
        return 0
    fi
    return 1
}

# rt_curl <method> <path> [--query "k=v&k2=v2"] [--body-string "..."] [--agent NAME] [--header "K: V"]...
# Prints the response body to stdout. Returns:
#   0   — daemon responded with HTTP 2xx
#   2   — daemon responded with HTTP 4xx/5xx (body printed to stderr)
#   3   — daemon unreachable, fallback warranted
#
# --header may be repeated; each appends one curl -H. Used by writer wrappers
# that need to forward override justifications as X-Mind-Override-* headers.
rt_curl() {
    local method="$1" path="$2"; shift 2
    local query="" body_string="" agent="${MIND_AGENT:-}"
    declare -a extra_headers=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --query) query="$2"; shift 2;;
            --body-string) body_string="$2"; shift 2;;
            --agent) agent="$2"; shift 2;;
            --header) extra_headers+=( -H "$2" ); shift 2;;
            *) echo "rt_curl: unknown flag: $1" >&2; return 2;;
        esac
    done

    local base
    base="$(rt_base_url)"
    if [ -z "$base" ]; then
        return 3
    fi

    local url="$base$path"
    [ -n "$query" ] && url="$url?$query"

    local headers=( -H "X-Runtime-Client: shell" )
    [ -n "$agent" ] && headers+=( -H "X-Mind-Agent: $agent" )
    headers+=( -H "X-Mind-Tenant: ${MIND_TENANT:-default}" )
    if [ ${#extra_headers[@]} -gt 0 ]; then
        headers+=( "${extra_headers[@]}" )
    fi

    # Capture body + status in ONE curl call without tempfiles. Avoids:
    #   - mktemp subprocess
    #   - cygpath subprocess (needed only when -o /tmp/... is used on Windows)
    #   - cat subprocess
    #   - rm subprocess
    # Cumulative savings on Windows: ~200-400ms per call.
    #
    # We use a marker (`\n\x1f__RT_STATUS__:`) that cannot appear in JSON or
    # human-text response bodies. Curl's -w expression prints the marker AFTER
    # the response body, all to stdout. We split in bash and emit only the body.
    local RT_MARKER=$'\n\x1f__RT_STATUS__:'

    local result curl_rc
    if [ -n "$body_string" ]; then
        result=$(curl -s --max-time "$RT_CURL_TIMEOUT" -X "$method" "${headers[@]}" \
            -d "$body_string" -w "${RT_MARKER}%{http_code}" "$url" 2>/dev/null) || curl_rc=$?
        curl_rc=${curl_rc:-0}
    else
        result=$(curl -s --max-time "$RT_CURL_TIMEOUT" -X "$method" "${headers[@]}" \
            -w "${RT_MARKER}%{http_code}" "$url" 2>/dev/null) || curl_rc=$?
        curl_rc=${curl_rc:-0}
    fi

    # No marker means curl failed before writing anything (connection refused,
    # DNS failure, timeout). $() strips trailing newlines, so the marker is
    # the reliable splitter — but only if it's present.
    if [ $curl_rc -ne 0 ] || [[ "$result" != *"${RT_MARKER}"* ]]; then
        return 3
    fi

    local status_code body
    status_code="${result##*${RT_MARKER}}"
    body="${result%${RT_MARKER}*}"

    if [ -z "$status_code" ] || [ "$status_code" = "000" ]; then
        return 3
    fi

    # Use printf %s rather than echo to avoid backslash interpretation in
    # response bodies that happen to contain escape sequences.
    if [ "$status_code" -ge 200 ] && [ "$status_code" -lt 300 ]; then
        printf '%s' "$body"
        return 0
    fi
    #  Change 2: routing-layer 404 ("no route for {method} {path}",
    # mind_api/src/server.py Response.error) means the endpoint does not exist
    # in the RUNNING daemon — typically because the route was added after the
    # daemon started (Mode 1 of ). Escalate rc 2->3 so rt_call takes
    # the auto-spawn/restart path instead of surfacing a hard 4xx to the
    # wrapper on the first call. Paired with Change 1: rt_call's rc==3 branch
    # runs rt_check_staleness, which sets RT_STALENESS_RESTART_PENDING, which
    # rt_ensure_running consumes to recycle the stale process.
    case "$body" in
        *"no route for "*) return 3;;
    esac
    printf '%s' "$body" >&2
    return 2
}

# rt_call <method> <path> [--query "..."] [--body-string "..."] [--agent NAME]
#
# Convenience wrapper: tries the daemon; auto-starts if down; returns the
# same exit codes as rt_curl. Wrappers usually call THIS and branch on
# exit 3 to invoke their own fallback.
#
# IMPORTANT: callers source this file under `set -euo pipefail`. A bare
# function call that returns nonzero would trip `set -e`. We guard the
# inner rt_curl calls with `|| true` so we can read $? without exiting.
rt_call() {
    local rc=0
    rt_curl "$@" || rc=$?
    if [ "$rc" -ne 3 ]; then
        # Daemon answered. If it's running stale code, rt_check_staleness
        # warns AND arms the auto-restart sentinel ( Change 1) so a
        # subsequent rt_ensure_running recycles the process.
        rt_check_staleness
        return "$rc"
    fi
    # rc==3: connection refused, no port, OR a routing-layer 404 escalated by
    # Change 2 (endpoint missing because the route post-dates the daemon).
    # Run rt_check_staleness FIRST so the Mode-1 case (daemon up but stale,
    # missing the new route) arms RT_STALENESS_RESTART_PENDING — otherwise
    # rt_ensure_running would no-op on the still-up stale daemon and the
    # retry below would re-hit the same 404. ( Change 1 glue.)
    rt_check_staleness
    # Try to spawn / recycle and retry once.
    if rt_ensure_running; then
        rc=0
        rt_curl "$@" || rc=$?
        return "$rc"
    fi
    rt_warn "daemon unreachable and auto-start failed"
    return 3
}

# DAEMON-ONLY (2026-05-14 cutover): print a loud error when the daemon is
# unreachable and the auto-spawn attempt also failed. Replaces _fallback_exec.
# See .claude/rules/no-python-cli-fallback.md.
#
# Verify-before-report (2026-05-20): rt_curl returns rc=3 on EITHER a true
# connection failure OR a request that exceeded RT_CURL_TIMEOUT. The wrapper's
# rt_try_autospawn then fails because the daemon's port is still bound (so a
# new spawn cannot bind). Without verification, BOTH paths surface the same
# "daemon is unreachable" message — even though in the timeout case the
# daemon is healthy and the original request was just slow (OneDrive
# write-lock contention is the canonical cause). This probe fixes the
# diagnostic: a fast /v1/admin/health hit distinguishes "truly down" from
# "slow but alive." Both paths still exit 1; only the message changes.
rt_no_daemon_error() {
    local cmd="${1:-<wrapper>}"

    # Fast health probe (1s max) before declaring unreachable. rt_is_up uses
    # the same --max-time 1 budget as elsewhere in this file.
    if rt_is_up; then
        local port
        port="$(rt_port)"
        echo "ERROR: daemon is REACHABLE but the request did not complete within RT_CURL_TIMEOUT=${RT_CURL_TIMEOUT}s." >&2
        echo "  Wrapper: $cmd" >&2
        echo "  Daemon health: OK (port ${port:-?})" >&2
        echo "  Likely cause: write contention (OneDrive sync lock on world/*.jsonl) or large/cold request." >&2
        echo "  The daemon's _atomic_write retries up to 10× with exponential backoff; worst case ~22s." >&2
        echo "  Recovery options:" >&2
        echo "    1. Retry — write contention is usually transient (<30s)." >&2
        echo "    2. Raise RT_CURL_TIMEOUT (current: ${RT_CURL_TIMEOUT}s) for this call if recurrent." >&2
        echo "    3. Inspect mind_api/state/spawn.log for _atomic_write retry storms." >&2
        echo "    4. Inspect meta/file-contention-telemetry.jsonl for the contention history." >&2
        exit 1
    fi

    echo "ERROR: daemon is unreachable and could not be auto-spawned." >&2
    echo "  Wrapper: $cmd" >&2
    echo "  Probe: GET ${MIND_RUNTIME_URL:-http://127.0.0.1:?}/v1/admin/health  failed" >&2
    echo "  Recovery options:" >&2
    echo "    1. Start the daemon manually: bash core/scripts/mind-api-start.sh" >&2
    echo "    2. Check mind_api/state/daemon.log for crash diagnostics" >&2
    echo "    3. See .claude/rules/no-python-cli-fallback.md for the rationale" >&2
    exit 1
}

# DAEMON-ONLY (2026-05-14 cutover): try to spawn the daemon, return rc=0 if
# it's now reachable, rc=1 if spawn failed. Used by wrappers on first rc=3.
# Single retry — if the second call still rc=3, wrapper calls rt_no_daemon_error.
rt_try_autospawn() {
    # Same opt-in guard as rt_ensure_running — the wrapper rc=3 branch calls
    # THIS function (which shells mind-api-start.sh), so it must short-circuit
    # too or RT_NO_AUTOSPAWN=1 callers still spawn a daemon via the script.
    if [ "${RT_NO_AUTOSPAWN:-0}" = "1" ]; then
        return 1
    fi
    local spawn_script="$PROJECT_ROOT/core/scripts/mind-api-start.sh"
    if [ ! -x "$spawn_script" ] && [ ! -f "$spawn_script" ]; then
        return 1
    fi
    bash "$spawn_script" >/dev/null 2>&1 || return 1
    rt_wait_for_ready
}

# Silent liveness probe — used by rt_try_autospawn. Hits the health endpoint
# and treats any HTTP response as alive.
rt_curl_silent_health() {
    local port
    port="$(rt_port)"
    [ -z "$port" ] && return 1
    curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/v1/admin/health" 2>/dev/null | grep -qE '^(200|400|404)$'
}
