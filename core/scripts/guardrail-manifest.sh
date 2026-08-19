#!/usr/bin/env bash
# Emit the guardrail index as a grouped ID MANIFEST — 100% id coverage, no rule text.
#
# Replaces `guardrails-read.sh --summary` at the two hot-path callers that need
# id COVERAGE rather than rule TEXT (prime, worker-loop). Measured 2026-08-19:
# 467,777 B -> 52,258 B over the same 4,099 records (88.8% smaller). Rule text is
# re-fetched on demand via the already-documented expand path:
#   guardrails-read.sh --id guard-NNN      (one rule, in full)
#   guardrails-read.sh --category <cat>    (a whole lane)
# Rationale + the measurement that rejected per-line compression: guardrail_manifest.py
#
# Usage: guardrail-manifest.sh [--stats] [--assert-total N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# Capture into a variable, THEN check rc. Piping guardrails-read.sh directly into
# the transformer would replace its exit code with the pipe's (guard-1150/guard-696),
# turning a daemon failure into a confident empty manifest.
#
# NO `2>&1` HERE, AND DO NOT ADD ONE (guard-1963 / guard-659 / guard-1675). $SUMMARY
# is a PAYLOAD another program parses, so merging stderr into it poisons the parse.
# This is not theoretical: _runtime.sh:259 emits `[runtime] WARNING: daemon is running
# stale code ...` to stderr while the call still returns rc=0 — a state that arises
# immediately after every framework commit. Measured 2026-08-19 with a positive control
# (zeta, cc-02,  fresh-eyes): with 2>&1 the warning lands first, the
# transformer sees a leading unparseable line with no record above it, and REFUSES with
# rc=2 — so prime and worker-loop get NO guardrail index at all. Worse than the byte
# cost this script exists to remove.
#
# `2>/dev/null` is NOT the alternative — that is the trap guard-659's own refinement
# names: it keeps the payload clean by making a LOUD REFUSAL INVISIBLE. Leave stderr
# alone. Diagnostics reach the caller's terminal unmerged, and the rc check below is
# what decides.
SUMMARY=""
if ! SUMMARY="$(bash "$SCRIPT_DIR/guardrails-read.sh" --summary)"; then
    echo "[guardrail-manifest] guardrails-read.sh --summary FAILED — not emitting a manifest. Its own diagnostics are on stderr above, unmerged." >&2
    exit 1
fi

if [ -z "$SUMMARY" ]; then
    echo "[guardrail-manifest] guardrails-read.sh --summary returned EMPTY with rc=0 — refusing to emit an empty manifest (an empty index reads as healthy downstream)." >&2
    exit 1
fi

printf '%s\n' "$SUMMARY" | python3 "$SCRIPT_DIR/guardrail_manifest.py" "$@"
