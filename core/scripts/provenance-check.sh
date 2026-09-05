#!/usr/bin/env bash
# provenance-check.sh — was this URL / file path / tree-node key actually
# retrieved in THIS session? ()
#
#   bash core/scripts/provenance-check.sh <url-or-path-or-node-key>
#   bash core/scripts/provenance-check.sh --session-id <sid> --quiet <query>
#
# Exit 0 = retrieved this session (prints when and how, one line per hit).
# Exit 1 = no record of it this session.
#
# The point is to make a citation FALSIFIABLE. An agent can emit a plausible URL
# from parametric memory; this answers from what the tools actually fetched.
#
# READ THE NEGATIVE CORRECTLY (guard-4407, and verify-before-assuming.md rule 1):
# exit 1 means "no tool-fetch record in this session's manifest". It does NOT
# mean the URL is fabricated. The manifest is fed by PostToolUse hooks bound to
# the Read / WebFetch / WebSearch TOOLS, plus two scripts that record from the
# INSIDE and are therefore immune to that tool-binding (`tree-read.sh --node` and
# `retrieve.sh`, both on rc=0 only — ). A page pulled with `curl` in a
# Bash call, or a file read with `cat`, is still invisible by construction — as is
# anything retrieved BEFORE the session's most recent manifest reset. Exit 1 is a
# prompt to go verify, never by itself evidence of invention.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

sid_arg=()
quiet_arg=()
query=""

while [ $# -gt 0 ]; do
    case "$1" in
        --session-id)
            [ $# -ge 2 ] || { echo "provenance-check: --session-id needs a value" >&2; exit 2; }
            sid_arg=(--session-id "$2"); shift 2 ;;
        --quiet|-q)
            quiet_arg=(--quiet); shift ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        --)
            shift; query="${1:-}"; break ;;
        *)
            query="$1"; shift ;;
    esac
done

if [ -z "$query" ]; then
    echo "usage: provenance-check.sh [--session-id <sid>] [--quiet] <url-or-path-or-node-key>" >&2
    exit 2
fi

# Exit code is the answer, so it must pass through untouched — no trailing pipe
# or echo may replace it (guard-1150).
exec python3 "$CORE_ROOT/scripts/context-reads.py" provenance-check \
    "${sid_arg[@]}" "${quiet_arg[@]}" "$query"
