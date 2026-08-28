#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# validate-paths.sh — L3 path-defense ()
#
# Runs at session start (invoked by /prime Phase 0.5 right after session-state-get).
# Verifies each path resolved from <agent>/local-paths.conf (WORLD, META) plus AGENT_DIR:
#   (a) exists, (b) is a directory, (c) is writable.
#
# Fail-open by design: prints WARN on any failure and exits 0. The priority is
# visibility — L1 ( write-time hook) and L2 (permission-time gate)
# remain the enforcement layers. Sub-1s budget: skips expensive I/O.
#
# Cross-reference: rb-223 (honor-system pattern), guard-142 (fail-open gates),
# .claude/rules/path-resolution.md (why the stale-META bug motivated this).

set -uo pipefail
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/_paths.sh"

_vp_warn() { printf '  WARN %s\n' "$*" >&2; }
_vp_info() { printf '  %s\n' "$*" >&2; }

printf '>> L3 PATH VALIDATOR (agent=%s)\n' "${AGENT_NAME:-<unset>}" >&2

_vp_fail=0
_vp_check() {
    # $1 = label, $2 = path
    local name="$1" path="$2"
    if [ -z "$path" ]; then
        _vp_warn "$name: NOT CONFIGURED"
        _vp_fail=$((_vp_fail+1))
        return
    fi
    _vp_info "$name: $path"
    if [ ! -e "$path" ]; then
        _vp_warn "  missing: path does not exist"
        _vp_fail=$((_vp_fail+1))
        return
    fi
    if [ ! -d "$path" ]; then
        _vp_warn "  not a directory"
        _vp_fail=$((_vp_fail+1))
        return
    fi
    local probe="$path/.paths-validator-probe-$$"
    if ! ( : > "$probe" ) 2>/dev/null; then
        _vp_warn "  not writable (L2 will enforce)"
        _vp_fail=$((_vp_fail+1))
        rm -f "$probe" 2>/dev/null
        return
    fi
    rm -f "$probe" 2>/dev/null
    _vp_info "  ok (exists, writable)"
}

_vp_check "WORLD_DIR" "${WORLD_DIR:-}"
_vp_check "META_DIR"  "${META_DIR:-}"
if [ -n "${AGENT_DIR:-}" ]; then
    _vp_check "AGENT_DIR" "$AGENT_DIR"
fi

# Stray repo-root world/|meta/ (2026-08-28 charter incident): a literal
# PROJECT_ROOT/world or /meta that is NOT the configured root holds files no
# other box, store, or git history can see. Script-lane writes bypass every
# write-time hook by design, so session-start detection is a load-bearing
# layer, not decoration.
for _vp_pair in "world:${WORLD_DIR:-}" "meta:${META_DIR:-}"; do
    _vp_sub="${_vp_pair%%:*}"; _vp_conf="${_vp_pair#*:}"
    _vp_stray="$PROJECT_ROOT/$_vp_sub"
    if [ -d "$_vp_stray" ] && [ "$_vp_stray" != "$_vp_conf" ]; then
        _vp_warn "STRAY $_vp_sub/ AT REPO ROOT: $_vp_stray is NOT the configured root ($_vp_conf) — its contents are invisible to the authoritative store and the fleet. Migrate + remove it."
        _vp_fail=$((_vp_fail+1))
    fi
done

if [ "$_vp_fail" -gt 0 ]; then
    printf '>> L3 PATH VALIDATOR: %d warning(s) — writes may land at wrong path\n' "$_vp_fail" >&2
else
    printf '>> L3 PATH VALIDATOR: ok\n' >&2
fi

exit 0  # fail-open (never blocks loop entry)
