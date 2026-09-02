#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-update-goal — daemon-aware wrapper (PR 7f → 7j).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse args (positional goal_id, field, value + override flags)
#   3. JSON-encode value via py -3 (mirrors aspirations.py parse_value)
#   4. POST /v1/aspirations/update-goal with overrides mapped to headers
#   5. On 200, print `goal` field from response to stdout (matches legacy
#      `json.dumps(goal, indent=2, ensure_ascii=False)`)
#
# As of PR 7j the daemon handles every field write end-to-end,
# INCLUDING Layer-D auto-
# Unblock filing on defer-time capability blocks. The old capability_blocked
# fallback was retired — the daemon now files the Unblock atomically with
# the refusal under the same aspirations.jsonl lock and surfaces
# `filed_unblock_id` in the 400 response body.
#
# ── WRITING A NARRATIVE FIELD? USE goal-field-append.sh INSTEAD ────────────
# This wrapper REPLACES the field. For the prose fields that accumulate across
# sessions and contributors — progress_note, outcome_note, description — a
# plain set is destructive by default, and the loss is invisible at the call
# site: the write returns rc=0 with the full record echoed either way.
#
#   bash core/scripts/goal-field-append.sh [--source world|agent] \
#        <goal-id> <field> <marker> [--value-file <path> | --value-stdin]
#
# That script exists for exactly this and carries what a hand-rolled
# read-modify-write cannot: a CAS conflict check (the value moving under you
# between read and write), post-state verification (post_len + delta +
# confirm_read:agreed), and marker-keyed idempotency so a retry cannot
# double-paste. Note rc=0 with `changed:false` means NOTHING was written —
# never read the exit code as "landed" (guard-3381).
#
# The field-shrink guard below does NOT cover this: it fires on the SHRINK
# direction past a 25% floor, so a same-size or modestly-smaller replacement —
# the ordinary addendum-clobber — passes it silently (guard-5228, which
# measured four clobbers in one session).
#
# Do NOT "fix" this by adding an --append flag here. That was considered and
# rejected (guard-2525 / guard-2460 / guard-1047 / guard-1488): this wrapper
# hand-rolls its parser, and a distinct script name cannot be swallowed by a
# flag-parsing arm. Rationale lives in goal-field-append.sh's own header.
#
# HARD LIMIT: if the field has a concurrent CROSS-BOX writer, neither this
# wrapper nor goal-field-append.sh can win — the loss happens at the sync
# merge, between the read and the write, and local .history never captures a
# pre-image. Move the content to an append-only channel (a board post) and
# reference it from the field instead (guard-5234).
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Normalize --goal/--goal-id flag aliases → positional goal id (rewrites $@).
# SSOT for the dual-accept goal-id contract; verify-learning enforces that this
# wrapper sources the normalizer (12-wrapper coverage grep). Restored 2026-05-29
# — dropped by a prior daemon cutover, which silently broke dual-accept and the
# verify-learning normalizer-coverage check.
GOAL_NORMALIZE_TARGET=positional source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"

# Shared unknown-flag refusal (). Sourced BEFORE _runtime.sh so the
# refusal is cheap and cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
# ONE literal, referenced by BOTH the --help arm and the refusal (
# fresh-eyes F-002). These were two copies until the review: the helper's own
# comment asserted they came from one, which was simply false, and two strings
# that must agree are the drift surface the refusal exists to remove.
_ACCEPTED_FLAGS="--source --force-defer --override-agent-match --override-uncommitted --cross-lane --override-missing-artifact --override-residual --override-shrink --blocker-ref --force-unstructured-defer --override-blocker-gate --allow-new-field --value-stdin --outcome-note --outcome-note-file"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
FORCE_DEFER=""
OVERRIDE_UNCOMMITTED=""
OVERRIDE_MISSING_ARTIFACT=""
OVERRIDE_RESIDUAL=""
OVERRIDE_SHRINK=""
BLOCKER_REF=""
FORCE_UNSTRUCTURED_DEFER=""
OVERRIDE_BLOCKER_GATE=""
ALLOW_NEW_FIELD=""
CROSS_LANE=""
VALUE_STDIN=""
OUTCOME_NOTE=""
OUTCOME_NOTE_FILE=""
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()
declare -a POSITIONALS=()

# Value-arg pattern: "${2-}" + safe shift handle the no-value case under
# set -u (see _runtime.sh "Convention: value-arg parsing under set -u").
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --force-defer)
            FORCE_DEFER="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-agent-match)
            # : wrong-context flag on the defer path (it is the
            # CREATE_BLOCKER bypass). Enumerated here so the flag+value pair is
            # CONSUMED as a pair and the daemon can redirect the caller to
            # --force-defer. Does NOT honor the defer bypass.
            # Comment corrected : the original said this kept argparse
            # able to recognize the flag "instead of the bare -* fallback dropping
            # the value into POSITIONALS". There is no argparse — the 2026-05-14
            # cutover deleted it — and the bare -* arm no longer drops anything;
            # it refuses (below). The ENUMERATION is still load-bearing, because
            # without it this flag's VALUE would be the token that gets refused.
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-uncommitted)
            OVERRIDE_UNCOMMITTED="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --cross-lane)
            # : bypass the cross-lane TAKEOVER guard
            # (status->in-progress / claimed_by on another agent's goal).
            CROSS_LANE="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-missing-artifact)
            OVERRIDE_MISSING_ARTIFACT="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-residual)
            # : bypass the Layer-B residual-work completion gate
            # (outcome_note names undone work, no live carrier cited).
            OVERRIDE_RESIDUAL="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-shrink)
            # : bypass the field-shrink guard (a description /
            # outcome_note write dropping to under 25% of its current length,
            # when that length exceeds 2000 chars). Deliberate condense only.
            OVERRIDE_SHRINK="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --outcome-note)
            # : companion outcome_note riding a status write — the
            # daemon lands both fields in ONE locked RMW (one S3 PUT instead
            # of the measured 2-call close ritual). Only valid with
            # <field>=status; refused otherwise below.
            OUTCOME_NOTE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --outcome-note-file)
            # : same as --outcome-note but reads the text from a
            # file — the safe transport for multi-KB notes (argv caps:
            # guard-5634 CreateProcess ~32k / guard-1187 MAX_ARG_STRLEN).
            OUTCOME_NOTE_FILE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --blocker-ref)
            BLOCKER_REF="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --force-unstructured-defer)
            FORCE_UNSTRUCTURED_DEFER="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-blocker-gate)
            # : bypass the credential-enumeration check on a
            # credentials-required blocker_ref. Same flag name + same ledger
            # as blocker-create-gate.py's override (Door A).
            OVERRIDE_BLOCKER_GATE="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --allow-new-field)
            # : bypass the goal-field allowlist. Justification-bearing
            # and audited to world/override-bypass-ledger.jsonl by the daemon, so
            # a genuinely new field stays a readable decision rather than a
            # keystroke slip that silently mutates the shared goal schema.
            ALLOW_NEW_FIELD="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --value-stdin)
            # : take VALUE from stdin so it never enters argv. A
            # composed prose value passed as an argv element is bounded by the
            # OS — Windows CreateProcess caps the whole command line at ~32,767
            # chars (guard-5634) and Linux MAX_ARG_STRLEN at ~128KB per string
            # (guard-1187) — which made a growing progress_note PERMANENTLY
            # unwritable from every Windows box once it crossed the ceiling.
            # The bound is on the COMPOSED TOTAL, so no smaller append helps.
            # Takes NO argument, so this is a BARE shift — deliberately NOT the
            # `shift $(( $# >= 2 ? 2 : 1 ))` form every value-taking flag above
            # uses (guard-1224 governs those; copying it here would silently eat
            # the next token, which on this wrapper is a POSITIONAL).
            # Not appended to PASSTHROUGH: this selects an INPUT SHAPE for this
            # wrapper and is not a daemon-side override (and PASSTHROUGH has no
            # reader here anyway — see the -*) arm below).
            VALUE_STDIN=1; shift;;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "<goal-id> <field> <value>" \
                "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). This arm used to append the unknown flag to
            # PASSTHROUGH and shift, on the strength of a comment promising that
            # "argparse on the fallback path surfaces a canonical error message".
            # That fallback was deleted by the 2026-05-14 daemon-only cutover this
            # file announces at line 2, and PASSTHROUGH has no reader in this script
            # at all — so the flag vanished and the NEXT token landed in POSITIONALS[2],
            # the VALUE slot. Measured casualty: a --value-file path overwrote a live
            # 1606-char description with rc=0, and a second agent hit the identical
            # shape ~24h later on a different box.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            POSITIONALS+=("$1")
            PASSTHROUGH+=("$1"); shift;;
    esac
done

GOAL_ID="${POSITIONALS[0]-}"
FIELD="${POSITIONALS[1]-}"
VALUE="${POSITIONALS[2]-}"

# --value-stdin: the value arrives on stdin, so POSITIONALS carries only
# <goal-id> <field>. READ IT ONLY WHEN THE FLAG WAS PASSED. An ungated
# `$(cat)` — the shape the stdin-JSON wrapper family uses — does not reject a
# bad call, it HANGS: with no redirect the read blocks on an empty-but-open
# stdin until the caller's 2-minute timeout kills the turn with exit 143, no
# message and no write, which reads as a wedged daemon rather than a wrong call
# shape (guard-3173). Refusing on a terminal keeps the failure fast and named.
if [ -n "$VALUE_STDIN" ]; then
    if [ -n "$VALUE" ]; then
        echo "Error: --value-stdin was passed AND a positional value is present ('${VALUE:0:40}...'). Supply the value on stdin OR positionally, never both." >&2
        exit 1
    fi
    if [ -t 0 ]; then
        echo "Error: --value-stdin was passed but stdin is a terminal — there is nothing to read. Pipe the value in or redirect a file (< payload.txt). Refusing rather than blocking (guard-3173)." >&2
        exit 1
    fi
    # `$(cat)` alone STRIPS trailing newlines — a command-substitution property, not a
    # cat one. The positional path preserves them, so a bare $(cat) would make the two
    # call shapes store DIFFERENT bytes for the same value, and the caller's post-write
    # confirm-read (which compares against the value it composed) would disagree for a
    # reason nothing in the output names. The sentinel-x idiom is byte-exact.
    VALUE="$(cat; printf x)"
    VALUE="${VALUE%x}"
fi

# Missing positionals → error
if [ -z "$GOAL_ID" ] || [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    echo "Error: goal_id, field, and value are all required." >&2
    exit 1
fi

# --source takes a STORE name, and the daemon reads it as "agent or not-agent"
# (aspirations_write.py: `if source == "agent" … else world`) — so any other
# token silently becomes a world lookup. Measured 2026-08-29 (coach reducer):
# `--source aspirations-execute` (a skill name) → `goal_not_found … (world)`
# three times over, an error that named the wrong cause. Refuse here, name the
# two values, exit 2 like the unknown-flag arm.
case "$SOURCE_VAL" in
    world|agent) ;;
    *)
        echo "Error: --source takes 'world' or 'agent' (the store the goal lives in), got '${SOURCE_VAL}'." >&2
        exit 2;;
esac

# --- Daemon path ---------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# : resolve the companion outcome_note BEFORE the encode step below
# (which wraps it into the body via MIND_COMPANION_NOTE — env transport,
# never argv interpolation, guard-165). File form wins the size problem;
# both forms refuse on a non-status field so the flag can never silently
# no-op.
if [ -n "$OUTCOME_NOTE_FILE" ]; then
    if [ -n "$OUTCOME_NOTE" ]; then
        echo "Error: pass --outcome-note OR --outcome-note-file, not both." >&2
        exit 1
    fi
    if [ ! -f "$OUTCOME_NOTE_FILE" ]; then
        echo "Error: --outcome-note-file not found: $OUTCOME_NOTE_FILE" >&2
        exit 1
    fi
    OUTCOME_NOTE="$(cat "$OUTCOME_NOTE_FILE"; printf x)"
    OUTCOME_NOTE="${OUTCOME_NOTE%x}"
fi
if [ -n "$OUTCOME_NOTE" ] && [ "$FIELD" != "status" ]; then
    echo "Error: --outcome-note/--outcome-note-file ride only a status write (g-358-36). For a standalone note use: aspirations-update-goal.sh <id> outcome_note <text>." >&2
    exit 1
fi

# Encode value as JSON, mirroring aspirations.py parse_value. Single py -3
# call (~30-50ms on Windows) vs full aspirations.py module load (~400-500ms).
# : VALUE travels on STDIN, never argv. Passing it as `-c '...' "$VALUE"`
# re-spawned the WHOLE value through CreateProcess (~32,767-char cap on the composed
# command line, guard-5634) / execve (MAX_ARG_STRLEN ~128KB, guard-1187), so fixing only
# the caller's hop would have moved WinError 206 here instead of removing it. `printf`
# is a bash BUILTIN, so the pipe adds no process and no argv.
# The program still arrives via `-c` (argv), which is what keeps stdin free for the
# data: `python3 -` or a heredoc-fed program would consume stdin ITSELF and silently
# discard the piped value (guard-4740 / guard-4728).
ENCODED_VALUE=$(printf '%s' "$VALUE" | MIND_COMPANION_NOTE="$OUTCOME_NOTE" $(rt_python_launcher) -c '
import json, sys
v = sys.stdin.read()
if v == "true":
    r = True
elif v == "false":
    r = False
elif v == "null":
    r = None
elif v == "[]":
    r = []
elif v.startswith("{") or v.startswith("["):
    try:
        r = json.loads(v)
    except json.JSONDecodeError:
        r = v
else:
    try:
        r = int(v)
    except ValueError:
        try:
            r = float(v)
        except ValueError:
            r = v
# : companion outcome_note rides the same POST as the status value —
# the daemon unwraps {"value": ..., "outcome_note": ...} and lands both
# fields in one locked RMW. Empty env var (flag not passed) leaves the body
# byte-identical to the historical shape.
import os
_n = os.environ.get("MIND_COMPANION_NOTE", "")
if _n:
    r = {"value": r, "outcome_note": _n}
sys.stdout.write(json.dumps(r))
')

QUERY="id=${GOAL_ID}&field=${FIELD}&source=${SOURCE_VAL}"
# Session identity () — mirrors aspirations-complete-by.sh. This is the
# MOST-TRAVELLED terminal door (status->completed/skipped/expired lands here), so
# without it `completed_by_sid` would always inherit the CLAIM's sid rather than
# recording the body that actually closed the goal. The daemon reads it via
# ctx.query only; `_nonholder_claim_warning` is called from complete_by/release
# ONLY (measured), so adding the param changes no other endpoint behavior.
# Best-effort; omitted when unset.
if [ -n "${MIND_SID:-}" ]; then
    QUERY="${QUERY}&sid=$(rt_url_encode "$MIND_SID")"
fi
# Cross-lane override () — MUST travel as a QUERY param, mirroring
# aspirations-claim.sh. update_goal() reads `ctx.query.get("cross_lane")`; it is
# the one override on this wrapper that is NOT read via `_header_override`, so
# the header below reaches nothing on its own. This wrapper is daemon-only (no
# CLI fallback since the 2026-05-14 cutover), so until this line existed the
# flag was inert for EVERY production caller — including anyone following
# update_goal's own refusal text, which says "Pass cross_lane=<justification>
# to override". Regression: test_update_goal_takeover_guard.py
# ::test_wrapper_cross_lane_flag_reaches_the_daemon (drives THIS wrapper; the
# CLI-driven override test was green throughout the defect).
if [ -n "$CROSS_LANE" ]; then
    QUERY="${QUERY}&cross_lane=$(rt_url_encode "$CROSS_LANE")"
fi

declare -a HEADER_ARGS=()
[ -n "$FORCE_DEFER" ] && HEADER_ARGS+=(--header "X-Mind-Force-Defer: $FORCE_DEFER")
[ -n "$OVERRIDE_UNCOMMITTED" ] && HEADER_ARGS+=(--header "X-Mind-Override-Uncommitted: $OVERRIDE_UNCOMMITTED")
# : carry this Body's role to the daemon so the uncommitted-work
# gate can enforce its DELIVERY half. BODY_ROLE is injected into every Bash
# call by the PreToolUse hook (bash-agent-inject.py), so it is present here
# and absent inside the daemon process -- which is why it has to travel as a
# header rather than being read on the far side. Empty when unset, and the
# gate treats an unknown role as report-but-do-not-block.
[ -n "${BODY_ROLE:-}" ] && HEADER_ARGS+=(--header "X-Mind-Body-Role: $BODY_ROLE")
[ -n "$OVERRIDE_MISSING_ARTIFACT" ] && HEADER_ARGS+=(--header "X-Mind-Override-Missing-Artifact: $OVERRIDE_MISSING_ARTIFACT")
[ -n "$OVERRIDE_RESIDUAL" ] && HEADER_ARGS+=(--header "X-Mind-Override-Residual: $OVERRIDE_RESIDUAL")
[ -n "$OVERRIDE_SHRINK" ] && HEADER_ARGS+=(--header "X-Mind-Override-Shrink: $OVERRIDE_SHRINK")
[ -n "$BLOCKER_REF" ] && HEADER_ARGS+=(--header "X-Mind-Blocker-Ref: $BLOCKER_REF")
[ -n "$FORCE_UNSTRUCTURED_DEFER" ] && HEADER_ARGS+=(--header "X-Mind-Force-Unstructured-Defer: $FORCE_UNSTRUCTURED_DEFER")
[ -n "$OVERRIDE_BLOCKER_GATE" ] && HEADER_ARGS+=(--header "X-Mind-Override-Blocker-Gate: $OVERRIDE_BLOCKER_GATE")
[ -n "$ALLOW_NEW_FIELD" ] && HEADER_ARGS+=(--header "X-Mind-Allow-New-Field: $ALLOW_NEW_FIELD")
[ -n "$CROSS_LANE" ] && HEADER_ARGS+=(--header "X-Mind-Cross-Lane: $CROSS_LANE")

rc=0
COMBINED="$(rt_call POST /v1/aspirations/update-goal \
    --query "$QUERY" \
    --body-string "$ENCODED_VALUE" \
    "${HEADER_ARGS[@]+"${HEADER_ARGS[@]}"}" 2>&1)" || rc=$?

case $rc in
    0)
        # 200: re-emit warnings[] to stderr (matches add-goal wrapper), then
        # print `goal` to stdout (legacy CLI shape). If `goal` is missing the
        # response is from an older daemon — print the ack body so wrappers
        # calling THIS script still get parseable JSON during a rolling
        # daemon upgrade.
        # shellcheck disable=SC2086
        printf '%s' "$COMBINED" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        # 4xx/5xx: terminal refusal — the daemon already handled side-effects
        # (Layer-D auto-Unblock filing for capability_blocked is now inline
        # per PR 7j). Print the body to stderr and exit 1. No fallback.
        printf '%s\n' "$COMBINED" >&2
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            COMBINED="$(rt_call POST /v1/aspirations/update-goal \
                --query "$QUERY" \
                --body-string "$ENCODED_VALUE" \
                "${HEADER_ARGS[@]+"${HEADER_ARGS[@]}"}" 2>&1)" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$COMBINED" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-update-goal.sh";;
    *)
        exit $rc;;
esac
