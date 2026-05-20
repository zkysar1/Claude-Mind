#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# fresh-eyes-record-tick.sh — Atomically record the fresh-eyes review tick.
#
# Replaces the fragile &&-chained inline block that was in
# /fresh-eyes-review SKILL.md Phase 8. The old block chained four bash
# commands with && and terminated in a board-post — if any step in the
# chain failed (including the optional board-post), the stamp write
# could be skipped silently. Evidence: fresh-eyes-2026-04-20 fired but
# left last_fresh_eyes_review null, causing the next aspirations-loop
# iteration's cadence gate to re-fire at goal count 1848.
#
# Contract:
#   - Reads current completed-goals count from fresh-eyes-cadence-check.sh --print-current
#   - Writes {timestamp, goals_count_at_last_fire} to wm.last_fresh_eyes_review
#   - Reads the slot back and verifies non-null payload
#   - Exit 0 on verified write; exit 1 with diagnostic on any failure
#
# Board-post is NOT this script's responsibility — it lives as a
# separate, best-effort Bash step in the skill (with `|| true`).
# Single responsibility: the stamp write must land, or this script
# must fail loud so the caller can retry.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

# Optional positional arg: WM slot name. Defaults to last_fresh_eyes_review
# (backward compat with the original fresh-eyes-review ritual). Sibling rituals
# pass their own slot: e.g., `fresh-eyes-record-tick.sh last_fresh_eyes_program_review`.
SLOT_NAME="${1:-last_fresh_eyes_review}"

current=$(bash "$SCRIPT_DIR/fresh-eyes-cadence-check.sh" --print-current)
if [[ -z "$current" ]] || ! [[ "$current" =~ ^[0-9]+$ ]]; then
    echo "fresh-eyes-record-tick: ERROR — --print-current returned non-numeric: '$current'" >&2
    exit 1
fi

now=$(date +%Y-%m-%dT%H:%M:%S)
payload=$(printf '{"timestamp":"%s","goals_count_at_last_fire":%s}' "$now" "$current")

if ! echo "$payload" | bash "$SCRIPT_DIR/wm-set.sh" "$SLOT_NAME" >/dev/null; then
    # Error text intentionally avoids the substring pattern the
    # signal-lifecycle-gate regex matches (`<script> <word> <word>`) in
    # quoted error prose, so write-failure messages don't false-flag as
    # positional-arg misuse (rb-349 family — prose-filter false positives).
    echo "fresh-eyes-record-tick: ERROR — write to '$SLOT_NAME' slot did not succeed" >&2
    exit 1
fi

# Readback verification — the load-bearing assertion.
# wm-read.sh returns literal "null" when the slot is unset.
readback=$(bash "$SCRIPT_DIR/wm-read.sh" "$SLOT_NAME" --json)
if [[ -z "$readback" ]] || [[ "$readback" == "null" ]]; then
    echo "fresh-eyes-record-tick: ERROR — readback for '$SLOT_NAME' returned '$readback' after write (silent write failure)" >&2
    exit 1
fi

echo "fresh-eyes-record-tick: wrote $SLOT_NAME goals_count_at_last_fire=$current at $now"
