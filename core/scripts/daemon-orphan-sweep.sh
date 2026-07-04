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
#   bash core/scripts/daemon-orphan-sweep.sh --keep-repo <path>   # extra repo to protect
#   bash core/scripts/daemon-orphan-sweep.sh --print-keepset      # show protected PIDs, no scan/kill
#
# CROSS-REPO SAFE (): --clean only reaps mind_api.src processes that are
# NOT in ANY live deployment's published daemon pair. The keep-set is auto-built
# from this repo PLUS every sibling deployment found under the deployments' parent
# dir (default: dirname PROJECT_ROOT; override via ORPHAN_SWEEP_DEPLOY_PARENT) PLUS
# any --keep-repo paths. So --clean run from one Mind repo never kills a sibling
# repo's live daemon — it is no longer a multi-deployment footgun.
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

# Honor RUNTIME_DIR (B16) so a sweep run against an isolated runtime dir reads
# the right local state — mirrors mind-api-start.sh:30 + lifecycle.runtime_dir.
RT_DIR="${RUNTIME_DIR:-$PROJECT_ROOT/mind_api/state}"
PID_FILE="$RT_DIR/daemon.pid"
PORT_FILE="$RT_DIR/daemon.port"
PARENT_PID_FILE="$RT_DIR/daemon.parent.pid"

CLEAN=0
STRICT=0
QUIET=0
PRINT_KEEPSET=0
KEEP_REPOS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1 ;;
        --strict) STRICT=1 ;;
        --quiet) QUIET=1 ;;
        --print-keepset) PRINT_KEEPSET=1 ;;
        --keep-repo)
            shift
            [ $# -gt 0 ] || { echo "[orphan-sweep] ERROR: --keep-repo needs a path" >&2; exit 2; }
            KEEP_REPOS+=("$1")
            ;;
        --keep-repo=*) KEEP_REPOS+=("${1#--keep-repo=}") ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "[orphan-sweep] ERROR: unknown arg: $1" >&2
            exit 2
            ;;
    esac
    shift
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

# ─────────────────────────────────────────────────────────────────────────
# Cross-repo-safe keep-set (). The PowerShell/pgrep scan below is
# SYSTEM-WIDE (Win32_Process / pgrep expose no cwd/env discriminator), so a
# keep-set of ONLY this repo's pair would flag a SIBLING Mind deployment's live
# daemon as an orphan and --clean would kill it (the cross-repo footgun the
# promotion teardown hit 2026-06-28). Build the keep-set from EVERY reachable
# live deployment's published pair: this repo + auto-discovered siblings under
# the deployments' parent dir + any explicit --keep-repo paths. A deployment
# whose daemon.pid has vanished (the teardown-orphan failure mode) is correctly
# absent from the keep-set and stays sweepable.
KEEP_PIDS=()        # all protected PIDs (children + parents)
KEEP_CHILDREN=()    # child PIDs only — parents are derived on Windows if missing

_collect_pair() {
    # $1 = a mind_api/state dir. Appends its child (+ parent) PIDs to the keep-set.
    local sdir="$1" c p
    c="$(_read_file "$sdir/daemon.pid" || echo "")"
    p="$(_read_file "$sdir/daemon.parent.pid" || echo "")"
    if [ -n "$c" ]; then KEEP_PIDS+=("$c"); KEEP_CHILDREN+=("$c"); fi
    if [ -n "$p" ]; then KEEP_PIDS+=("$p"); fi
}

# 1. This repo's pair (honors RUNTIME_DIR via RT_DIR).
_collect_pair "$RT_DIR"

# 2. Auto-discovered sibling deployments. Default parent = dirname PROJECT_ROOT;
#    overridable via ORPHAN_SWEEP_DEPLOY_PARENT (test seam + non-default layouts).
#    Glob both layouts: <repo>/mind_api/state/ and <repo>/.mind-data/mind_api/state/.
DEPLOY_PARENT="${ORPHAN_SWEEP_DEPLOY_PARENT:-$(dirname "$PROJECT_ROOT")}"
for _pidf in "$DEPLOY_PARENT"/*/mind_api/state/daemon.pid \
             "$DEPLOY_PARENT"/*/.mind-data/mind_api/state/daemon.pid; do
    [ -f "$_pidf" ] || continue              # unmatched glob expands to literal — skip
    _sdir="$(dirname "$_pidf")"
    [ "$_sdir" = "$RT_DIR" ] && continue      # this repo, already collected
    _collect_pair "$_sdir"
done

# 3. Explicit --keep-repo paths (both layouts).
for _repo in "${KEEP_REPOS[@]:-}"; do
    [ -n "$_repo" ] || continue
    [ -f "$_repo/mind_api/state/daemon.pid" ] && _collect_pair "$_repo/mind_api/state"
    [ -f "$_repo/.mind-data/mind_api/state/daemon.pid" ] && _collect_pair "$_repo/.mind-data/mind_api/state"
done

# De-duplicate (a repo can appear via both the glob and --keep-repo).
_dedup() { printf '%s\n' "$@" | awk 'NF && !seen[$0]++' | tr '\n' ' '; }
# shellcheck disable=SC2207
KEEP_PIDS=($(_dedup "${KEEP_PIDS[@]:-}"))
# shellcheck disable=SC2207
KEEP_CHILDREN=($(_dedup "${KEEP_CHILDREN[@]:-}"))

if [ "$PRINT_KEEPSET" = "1" ]; then
    # Debug/inspection + hermetic test seam: print the protected set and exit
    # WITHOUT scanning processes or killing anything.
    echo "KEEPSET_PIDS=$(IFS=,; echo "${KEEP_PIDS[*]:-}")"
    echo "KEEPSET_CHILDREN=$(IFS=,; echo "${KEEP_CHILDREN[*]:-}")"
    echo "DEPLOY_PARENT=$DEPLOY_PARENT"
    exit 0
fi

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
    _in_keepset() { local x="$1" k; for k in "${KEEP_PIDS[@]:-}"; do [ "$x" = "$k" ] && return 0; done; return 1; }
    orphans=()
    for p in "${pids[@]}"; do
        if _in_keepset "$p"; then
            _say "    KEEP  PID=$p (live deployment pair)"
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
# Serialize the cross-repo keep-set into PowerShell array literals.
# Empty -> @() (no protected pair found; every mind_api.src proc is an orphan).
keep_pids_ps="$(IFS=,; echo "${KEEP_PIDS[*]:-}")"
keep_children_ps="$(IFS=,; echo "${KEEP_CHILDREN[*]:-}")"

ps_script="
    \$keep_pids = @($keep_pids_ps)
    \$keep_children = @($keep_children_ps)
    \$do_clean = \$$([ "$CLEAN" = "1" ] && echo "true" || echo "false")
    \$procs = Get-CimInstance Win32_Process -Filter \"Name='py.exe' OR Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { \$_.CommandLine -match 'mind_api\\.src' }
    \$total = (\$procs | Measure-Object).Count

    # Derive the live parent for every protected child whose parent PID is not
    # already in the keep-set. Handles (a) the pre-v3 transition window where a
    # daemon wrote daemon.pid but not daemon.parent.pid, and (b) a sibling repo
    # whose daemon.parent.pid file is missing — without this, the live py.exe
    # launcher of a protected child would be flagged ORPHAN and --clean would
    # orphan its python.exe. Generalized from the prior single-local-child
    # derive to the whole cross-repo keep-set ().
    foreach (\$kc in \$keep_children) {
        \$alive_child = \$procs | Where-Object { \$_.ProcessId -eq \$kc } | Select-Object -First 1
        if (\$alive_child) {
            \$kppid = \$alive_child.ParentProcessId
            if (\$keep_pids -notcontains \$kppid) {
                \$derived_parent = Get-CimInstance Win32_Process -Filter \"ProcessId=\$kppid\" -ErrorAction SilentlyContinue
                if (\$derived_parent -and \$derived_parent.CommandLine -match 'mind_api\\.src') {
                    \$keep_pids += \$kppid
                    Write-Output \"DERIVED_PARENT=\$kppid\"
                }
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
        if (\$keep_pids -notcontains \$p.ProcessId) {
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
