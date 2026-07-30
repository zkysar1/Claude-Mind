#!/usr/bin/env bash
# peer-surface.sh -- compact cross-deployment peer summary for /prime Phase 2.
#
# Usage: bash core/scripts/peer-surface.sh [--window <dur>] [--json]
#   --window <dur>  board scan window (default 168h = 7d). A NARROW window can
#                   report "none" on a quiet week, which reads as "no channel";
#                   7d matches the window the convention itself measures over.
#   --json          machine-readable output (for tests / callers)
#
# FAIL-OPEN by contract: /prime is a hot path and this is an observability
# surface, never a gate. Every failure degrades to a pointer line and exit 0.
#
# Cost: ~1.5s end-to-end (measured cc-05 / Linux, warm daemon, 2026-07-30).
# Two bounded board reads dominate (coordination ~350ms, findings ~340ms); the
# rest is the team-state read plus three py -3 starts. Those two channels are
# where peer traffic actually lands -- measured over the same 7d window,
# coordination 19 / findings 17, and general / decisions / reasoning ZERO.
# Re-measure before quoting this number on another box or a cold daemon: the
# first board read after a daemon spawn measured 8.2s here.
#
# See core/config/conventions/cross-deployment-channel.md and peer_surface.py
# (which documents the three parse/predicate traps this surface encodes).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

WINDOW="168h"
WINDOW_LABEL="7d"
AS_JSON=""
while [ $# -gt 0 ]; do
    case "$1" in
        --window) WINDOW="${2:-168h}"; WINDOW_LABEL="$WINDOW"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --json)   AS_JSON="1"; shift ;;
        *)        shift ;;
    esac
done

cd "${PROJECT_ROOT:-$SCRIPT_DIR/../..}" 2>/dev/null || true

# ── Registry: one tiny committed YAML per known deployment ───────────────
# Read with grep rather than a YAML parser: these files are two-field and
# committed under core/, so they are always locally readable (no chicken-and-egg
# with the own-cloud store one of them configures).
REGISTRY="{}"
if [ -d core/config/environments ]; then
    REGISTRY=$(
        for f in core/config/environments/*.yaml; do
            [ -f "$f" ] || continue
            eid=$(grep -m1 '^environment_id:' "$f" 2>/dev/null | sed 's/^environment_id:[[:space:]]*//')
            bk=$(grep -m1 '^backend:' "$f" 2>/dev/null | sed 's/^backend:[[:space:]]*//')
            [ -n "$eid" ] && printf '%s\t%s\n' "$eid" "$bk"
        done | py -3 -c '
import sys, json
out = {}
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if parts and parts[0]:
        out[parts[0]] = parts[1] if len(parts) > 1 else ""
print(json.dumps(out))
' 2>/dev/null
    )
    [ -n "$REGISTRY" ] || REGISTRY="{}"
fi

# ── Self identity: env var wins, else .env.local ─────────────────────────
SELF_ENV="${ENVIRONMENT_ID:-}"
if [ -z "$SELF_ENV" ] && [ -f .env.local ]; then
    SELF_ENV=$(grep -m1 '^ENVIRONMENT_ID=' .env.local 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' \r')
fi

# ── Live local roster (team-state agent_status), for excluding local authors ──
# Fail-open to empty: an empty roster never inflates the peer count (attribution
# requires independent @env-id evidence), it only makes the excluded-author
# footnote noisier.
ROSTER=$(bash core/scripts/team-state-read.sh --json 2>/dev/null | py -3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(",".join(sorted((d.get("agent_status") or {}).keys())))
except Exception:
    print("")
' 2>/dev/null) || ROSTER=""

# ── Board scan: only the channels peer traffic actually lands on ─────────
BOARD=$(
    for ch in coordination findings; do
        bash core/scripts/board-read.sh --channel "$ch" --since "$WINDOW" --json 2>/dev/null
    done
)

printf '%s\n' "$BOARD" | \
    PEER_SELF_ENV="$SELF_ENV" PEER_REGISTRY="$REGISTRY" PEER_ROSTER="$ROSTER" \
    PEER_WINDOW="$WINDOW_LABEL" PEER_JSON="$AS_JSON" \
    py -3 core/scripts/peer_surface.py 2>/dev/null

# Never fail the caller: /prime treats this as observability, not a gate.
exit 0
