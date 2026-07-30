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

# --claim may appear in ANY position, and a flag must never be mistaken for a
# slot name. Positional-only detection (`[[ "${2:-}" == "--claim" ]]`) had a
# silent footgun found by fresh-eyes-code on this very commit: invoking
# `<script> --claim` with the slot omitted left $2 empty, so claim mode was NOT
# entered and the call fell through to a REAL end-of-ritual stamp against a WM
# slot literally named "--claim" — the opposite of what the caller asked for,
# with no error. Scan for the flag, and refuse to treat any leading `--token`
# as a slot.
CLAIM_MODE=0
for _arg in "$@"; do
    [[ "$_arg" == "--claim" ]] && CLAIM_MODE=1
done
[[ "$SLOT_NAME" == --* ]] && SLOT_NAME="last_fresh_eyes_review"

# Does this slot's config block declare `team_aware: true` (tree/program —
# shared resources with ONE time series)? Used by BOTH the ritual-START claim
# just below and the ritual-END shared stamp at the bottom of this script.
_slot_is_team_aware() {
    SLOT_NAME="$SLOT_NAME" SCRIPT_DIR="$SCRIPT_DIR" python3 -c '
import os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import yaml
import _paths
cfg = yaml.safe_load((_paths.CONFIG_DIR / "aspirations.yaml").read_text(encoding="utf-8")) or {}
slot = os.environ["SLOT_NAME"]
hit = any(
    isinstance(b, dict) and b.get("wm_slot") == slot and b.get("team_aware")
    for b in cfg.values()
)
print("1" if hit else "0")
' || echo 0
}

# ── Ritual-START claim (, 2026-07-28) ────────────────────────────
# The shared stamp at the bottom of this script is written at ritual END, but
# the cadence gate READS it at ritual START — so agents entering that window
# together all read the same pre-fire value and all pass (check-then-act).
# Measured before this mode existed: 4 duplicate rituals across ~34 fires made
# AFTER the team gate landed (2026-06-15, 06-19, and BOTH slots on 07-19; gaps
# 2m48s-9m52s). A stamp that does not exist yet cannot suppress anything, so
# this writes a short-TTL in-flight claim the gate can see IMMEDIATELY and the
# second agent noops instead of re-running the entire ritual.
#
# Sole-writer invariant (guard-718) preserved: the action-performer writes the
# per-agent slot AND every shared key; the gate only ever reads. The claim
# lives in a SEPARATE key, never inside the cadence stamp — writing the real
# stamp early would corrupt the cadence basis if the ritual then died, costing
# a whole cadence (100-200 world goals = days). Best-effort by design: a failed
# claim WARNs and exits 0, because degraded suppression must never block the
# ritual it is protecting.
if [[ "$CLAIM_MODE" == "1" ]]; then
    if [[ "$(_slot_is_team_aware)" != "1" ]]; then
        echo "fresh-eyes-record-tick: slot '$SLOT_NAME' is not team-aware; no shared claim needed"
        exit 0
    fi
    now=$(date +%Y-%m-%dT%H:%M:%S)
    agent_name="${MIND_AGENT:-unknown}"
    claim_payload=$(printf '{"claimed_by":"%s","claimed_at":"%s"}' "$agent_name" "$now")
    if bash "$SCRIPT_DIR/team-state-update.sh" \
            --field "shared_cadences.${SLOT_NAME}__inflight_claim" \
            --value "$claim_payload" >/dev/null; then
        echo "fresh-eyes-record-tick: claimed ${SLOT_NAME}__inflight_claim for $agent_name at $now (TTL 30m)"
    else
        echo "fresh-eyes-record-tick: WARN — shared claim write did not succeed; ritual proceeds with duplicate suppression degraded" >&2
    fi
    exit 0
fi

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

# Value-match assertion (2026-06-07): a non-null readback does NOT prove the
# write landed. A wm-set that returns 0 without persisting leaves the slot at
# its OLD non-null value, which the empty/"null" check above false-passes as
# success — observed when this wrapper printed "wrote ...=<new>" while the slot
# kept an older count, re-firing the cadence gate every iteration. Confirm the
# count just written is the count now on disk; if not, the write did not
# persist — fail loud per this script's contract (the caller's cadence re-fire
# is the retry). Pure-bash extraction keeps the wrapper IRREDUCIBLY LOCAL.
if [[ "$readback" =~ \"goals_count_at_last_fire\"[[:space:]]*:[[:space:]]*([0-9]+) ]]; then
    readback_count="${BASH_REMATCH[1]}"
else
    readback_count=""
fi
if [[ "$readback_count" != "$current" ]]; then
    echo "fresh-eyes-record-tick: ERROR — readback for '$SLOT_NAME' has count='$readback_count' not the just-written '$current' (write did not persist; stale value retained)" >&2
    exit 1
fi

echo "fresh-eyes-record-tick: wrote $SLOT_NAME goals_count_at_last_fire=$current at $now"

#  team-aware shared stamp: when the slot belongs to a config block
# declaring `team_aware: true` (tree/program — shared resources with ONE time
# series), also stamp world/team-state.yaml shared_cadences.<slot> so OTHER
# agents' cadence gates see this fire. Stamp carries the WORLD-ONLY count —
# agent-inclusive counts are not cross-agent comparable (see
# fresh-eyes-cadence-check.py count_completed_goals docstring).
# Best-effort by design: the per-agent WM write above is this script's
# contract; a team-state write failure (daemon down) WARNs and exits 0 —
# the next teammate fire re-stamps, and the read-side gate fails open.
team_aware=$(_slot_is_team_aware)

if [[ "$team_aware" == "1" ]]; then
    world_current=$(bash "$SCRIPT_DIR/fresh-eyes-cadence-check.sh" --print-current --world-only || echo "")
    if [[ -z "$world_current" ]] || ! [[ "$world_current" =~ ^[0-9]+$ ]]; then
        echo "fresh-eyes-record-tick: WARN — world-only count unavailable ('$world_current'); skipping team stamp for '$SLOT_NAME' (per-agent stamp landed)" >&2
    else
        agent_name="${MIND_AGENT:-unknown}"
        team_payload=$(printf '{"timestamp":"%s","world_goals_count_at_last_fire":%s,"fired_by":"%s"}' "$now" "$world_current" "$agent_name")
        if bash "$SCRIPT_DIR/team-state-update.sh" --field "shared_cadences.$SLOT_NAME" --value "$team_payload" >/dev/null; then
            echo "fresh-eyes-record-tick: team stamp shared_cadences.$SLOT_NAME world_count=$world_current fired_by=$agent_name"
        else
            echo "fresh-eyes-record-tick: WARN — team-state stamp write failed for '$SLOT_NAME' (per-agent stamp landed; next team fire re-stamps)" >&2
        fi
    fi
fi
