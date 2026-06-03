#!/usr/bin/env bash
# daemon-orphan-sweep.sh — User-invocable daemon health check + orphan reaper.
#
# Counts mind_api.src processes alive, cross-references against the published
# daemon.pid + daemon.parent.pid, identifies orphans (processes not in the
# pair), and optionally reaps them.
#
# Usage:
#   bash core/scripts/daemon-orphan-sweep.sh             # report only
#   bash core/scripts/daemon-orphan-sweep.sh --clean     # report + kill orphans
#   bash core/scripts/daemon-orphan-sweep.sh --strict    # exit 1 if orphans exist
#   bash core/scripts/daemon-orphan-sweep.sh --clean --strict
#
# Exit codes:
#   0 — healthy (exactly 1 daemon pair, no orphans) OR --clean swept successfully
#   1 — orphans found AND --strict (without --clean) OR --clean failed
#   2 — usage error
#
# Why this exists:  v3 added bulletproof in-flight prevention via
# _force_kill_tree + _sweep_orphan_daemons in mind-api-start.sh and
# _runtime.sh. This script is the user-facing companion:
#   - manual check ("am I leaking right now?")
#   - one-shot recovery if a regression slips through
#   - the entry point invoked by verify-learning's daemon-health check
#
# Cross-platform: full Windows logic via PowerShell + WMI; POSIX fallback
# uses pgrep. The POSIX path is mostly preventive — the orphan failure
# mode is Windows-specific (py.exe launcher layer + MSYS kill semantics).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

RT_DIR="$PROJECT_ROOT/mind_api/state"
PID_FILE="$RT_DIR/daemon.pid"
PORT_FILE="$RT_DIR/daemon.port"
PARENT_PID_FILE="$RT_DIR/daemon.parent.pid"

CLEAN=0
STRICT=0
QUIET=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        --strict) STRICT=1 ;;
        --quiet) QUIET=1 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "[orphan-sweep] ERROR: unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

_say() {
    [ "$QUIET" = "1" ] && return 0
    echo "$@"
}

_read_file() {
    [ -f "$1" ] || return 1
    local val
    val="$(cat "$1" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$val" ] && echo "$val"
}

# Read current expected daemon state.
current_child="$(_read_file "$PID_FILE" || echo "")"
current_parent="$(_read_file "$PARENT_PID_FILE" || echo "")"
current_port="$(_read_file "$PORT_FILE" || echo "")"

_say "═══ Daemon orphan sweep ══════════════════════════════════"
_say "  Published state:"
_say "    daemon.pid         = ${current_child:-<missing>}"
_say "    daemon.parent.pid  = ${current_parent:-<missing>}"
_say "    daemon.port        = ${current_port:-<missing>}"
_say ""

case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
    *) PLATFORM="posix" ;;
esac

if [ "$PLATFORM" = "posix" ]; then
    # POSIX: pgrep python processes running mind_api.src.
    mapfile -t pids < <(pgrep -f 'python.* -m mind_api\.src' 2>/dev/null)
    _say "  Alive mind_api.src processes (POSIX): ${#pids[@]}"
    orphans=()
    for p in "${pids[@]}"; do
        if [ "$p" = "$current_child" ] || [ "$p" = "$current_parent" ]; then
            _say "    KEEP  PID=$p (matches published state)"
        else
            _say "    ORPH  PID=$p (orphan)"
            orphans+=("$p")
        fi
    done
    orphan_count=${#orphans[@]}
    _say "  Orphans found: $orphan_count"
    if [ "$orphan_count" -gt 0 ] && [ "$CLEAN" = "1" ]; then
        _say "  Killing orphans..."
        killed=0; failed=0
        for p in "${orphans[@]}"; do
            if kill -KILL "$p" 2>/dev/null; then
                killed=$((killed + 1))
            else
                failed=$((failed + 1))
            fi
        done
        _say "    killed=$killed failed=$failed"
        [ "$failed" -gt 0 ] && exit 1
    fi
    if [ "$orphan_count" -gt 0 ] && [ "$STRICT" = "1" ] && [ "$CLEAN" = "0" ]; then
        exit 1
    fi
    exit 0
fi

# Windows: PowerShell + WMI.
keep_c="${current_child:-0}"
[ -z "$keep_c" ] && keep_c=0
keep_p="${current_parent:-0}"
[ -z "$keep_p" ] && keep_p=0

ps_script="
    \$keep_child = $keep_c
    \$keep_parent = $keep_p
    \$do_clean = \$$([ "$CLEAN" = "1" ] && echo "true" || echo "false")
    \$procs = Get-CimInstance Win32_Process -Filter \"Name='py.exe' OR Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { \$_.CommandLine -match 'mind_api\\.src' }
    \$total = (\$procs | Measure-Object).Count

    # If keep_parent is unset (=0) but keep_child is alive, derive its actual
    # parent process and treat that as legit. Handles the transition window
    # where an OLD daemon (pre-v3 fix) wrote daemon.pid but not
    # daemon.parent.pid — without this, the legit py.exe launcher would be
    # flagged ORPHAN and a --clean would orphan the live daemon python.exe.
    if (\$keep_parent -eq 0 -and \$keep_child -gt 0) {
        \$alive_child = \$procs | Where-Object { \$_.ProcessId -eq \$keep_child } | Select-Object -First 1
        if (\$alive_child) {
            \$derived_parent = Get-CimInstance Win32_Process -Filter \"ProcessId=\$(\$alive_child.ParentProcessId)\" -ErrorAction SilentlyContinue
            if (\$derived_parent -and \$derived_parent.CommandLine -match 'mind_api\\.src') {
                \$keep_parent = \$derived_parent.ProcessId
                Write-Output \"DERIVED_PARENT=\$keep_parent\"
            }
        }
    }

    \$alive = @()
    \$orphans = @()
    foreach (\$p in \$procs) {
        \$entry = [PSCustomObject]@{
            PID = \$p.ProcessId
            Name = \$p.Name
            PPID = \$p.ParentProcessId
            Start = \$p.CreationDate
        }
        \$alive += \$entry
        if (\$p.ProcessId -ne \$keep_child -and \$p.ProcessId -ne \$keep_parent) {
            \$orphans += \$entry
        }
    }
    Write-Output \"ALIVE_COUNT=\$total\"
    Write-Output \"ORPHAN_COUNT=\$(\$orphans.Count)\"
    foreach (\$a in \$alive) {
        \$flag = if (\$orphans -contains \$a) { 'ORPH' } else { 'KEEP' }
        Write-Output \"ALIVE \$flag PID=\$(\$a.PID) PPID=\$(\$a.PPID) NAME=\$(\$a.Name) START=\$(\$a.Start.ToString('MM-dd HH:mm:ss'))\"
    }
    if (\$do_clean -and \$orphans.Count -gt 0) {
        \$killed = 0
        \$already_gone = 0
        \$failed = 0
        foreach (\$o in \$orphans) {
            try {
                Stop-Process -Id \$o.PID -Force -ErrorAction Stop
                \$killed++
                Write-Output \"KILLED PID=\$(\$o.PID) NAME=\$(\$o.Name)\"
            } catch {
                # 'Cannot find a process' = success (cascade-killed when we
                # killed its parent moments earlier). Differentiate from
                # true failures.
                if (\$_.Exception.Message -match 'Cannot find a process') {
                    \$already_gone++
                    Write-Output \"CASCADE PID=\$(\$o.PID) NAME=\$(\$o.Name) (parent kill cascaded)\"
                } else {
                    \$failed++
                    Write-Output \"FAIL PID=\$(\$o.PID) ERR=\$(\$_.Exception.Message)\"
                }
            }
        }
        Write-Output \"KILLED_COUNT=\$killed\"
        Write-Output \"CASCADE_COUNT=\$already_gone\"
        Write-Output \"FAILED_COUNT=\$failed\"
    }
"

output="$(powershell.exe -NoProfile -Command "$ps_script" 2>&1)" || {
    echo "[orphan-sweep] ERROR: PowerShell invocation failed" >&2
    echo "$output" >&2
    exit 1
}

alive_count=0
orphan_count=0
killed_count=0
cascade_count=0
failed_count=0
derived_parent=""
while IFS= read -r line; do
    # PowerShell on Windows emits CRLF; strip trailing \r so integer
    # comparisons like `[ "$orphan_count" -gt 0 ]` don't blow up with
    # "integer expression expected: 2\r".
    line="${line%$'\r'}"
    case "$line" in
        ALIVE_COUNT=*)   alive_count="${line#ALIVE_COUNT=}" ;;
        ORPHAN_COUNT=*)  orphan_count="${line#ORPHAN_COUNT=}" ;;
        KILLED_COUNT=*)  killed_count="${line#KILLED_COUNT=}" ;;
        CASCADE_COUNT=*) cascade_count="${line#CASCADE_COUNT=}" ;;
        FAILED_COUNT=*)  failed_count="${line#FAILED_COUNT=}" ;;
        DERIVED_PARENT=*)
            derived_parent="${line#DERIVED_PARENT=}"
            _say "  NOTE: daemon.parent.pid missing; derived legit parent dynamically: PID=$derived_parent"
            _say "        (this is normal for daemons spawned before the g-115-764 v3 fix;"
            _say "         the next mind-api-start.sh --restart will populate daemon.parent.pid)"
            _say ""
            ;;
        ALIVE\ *)
            _say "    ${line#ALIVE }"
            ;;
        KILLED\ *)
            _say "    KILL  ${line#KILLED }"
            ;;
        CASCADE\ *)
            _say "    CASC  ${line#CASCADE }"
            ;;
        FAIL\ *)
            _say "    FAIL  ${line#FAIL }"
            ;;
    esac
done <<< "$output"

_say "  Alive mind_api.src processes: $alive_count"
_say "  Orphans (not in published state): $orphan_count"
if [ "$CLEAN" = "1" ]; then
    _say "  Killed: $killed_count   Cascade-killed: $cascade_count   Failed: $failed_count"
fi
_say "═════════════════════════════════════════════════════════"

# Exit code logic
if [ "$CLEAN" = "1" ]; then
    # --clean: success unless kills failed
    [ "$failed_count" -gt 0 ] && exit 1
    exit 0
fi
if [ "$STRICT" = "1" ] && [ "$orphan_count" -gt 0 ]; then
    exit 1
fi
exit 0
