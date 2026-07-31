#!/usr/bin/env bash
# sentinel-clear-guarded.sh — mechanically couple a one-shot sentinel's CLEAR
# to proof that the write it gates actually LANDED.
#
# THE DEFECT THIS EXISTS TO KILL (, routed from ZDS/omni):
# the rb-428 sentinel lifecycle is documented as
#   wm-read -> if non-null -> action -> clear
# and precheck Phase 0-pre2 says "clear signal after successful write". Nothing
# MECHANICALLY couples the two. A composed one-liner joining them with `;`
# instead of `&&` clears the sentinel even though the write failed, and the
# state then asserts an obligation is satisfied while no record exists.
# Measured six times; the last three within ~30h by an agent that had already
# documented the failure mode each time. An encoded rule did not hold, so the
# corrective has to be a mechanical precondition at the point of action
# (rb-745, guard-232).
#
# WHY `&&` IS NOT ENOUGH, AND WHY THIS OWNS THE READ-BACK:
# several stores exit **rc=0 while REFUSING the write**. Confirmed on two
# independent stores — experience-add.sh (documented in aspirations-spark's
# verbatim_anchors note) and guardrails-add.sh (ZDS, 2026-07-31, an entry
# refused over an invalid `applies_to` field). Exit status alone is therefore
# an unreliable success signal, so this script performs the READ-BACK itself
# rather than delegating it to caller discipline. That is the whole point:
# `--verify` is REQUIRED, not optional.
#
# CONTRACT
#   sentinel-clear-guarded.sh --slot <wm-slot> --verify <read-back-cmd> \
#       [--expect <substring>] [--dry-run] -- <producing-cmd...>
#
#   1. run <producing-cmd>; capture rc
#   2. run <read-back-cmd>; require rc=0 AND non-empty stdout
#      (and, with --expect, require that substring to be present)
#   3. clear <wm-slot> ONLY if BOTH pass
#   4. on ANY failure leave the sentinel SET and emit a loud diagnostic naming
#      WHICH check failed
#
# FAIL-CLOSED BY DESIGN. Leaving the sentinel SET is the correct failure mode:
# the obligation genuinely is not satisfied, so the next iteration must
# re-surface it. This script never fails open — contrast the advisory precheck
# sweeps, which do.
#
# EXIT CODES (distinct so a caller can branch, and so tests can pin them)
#   0  both checks passed; slot cleared
#   1  producing command failed (rc != 0)          -> slot left SET
#   2  read-back failed (rc != 0, empty, or no --expect match) -> slot left SET
#   3  usage error, or the clear itself failed     -> slot state reported
#
# The clear is issued as its OWN invocation after both checks pass, which is
# exactly what guard-1870 requires ("a one-shot sentinel's CLEAR must never
# share a Bash invocation with the write it gates"). Callers get that guarantee
# mechanically instead of having to remember it.
#
# NOTE ON `--verify` EVALUATION: the read-back is eval'd in THIS shell rather
# than handed to `bash -c`. Deliberate — it avoids spawning a child whose
# argv[0] is a bare "bash", which on Windows resolves via CreateProcess through
# System32 to the WSL launcher and can block forever (guard-580). The command
# is framework-authored, not user input.

set -uo pipefail

SLOT=""
VERIFY=""
EXPECT=""
DRY_RUN=0
PRODUCING=()

usage() {
    cat >&2 <<'USAGE'
usage: sentinel-clear-guarded.sh --slot <wm-slot> --verify <read-back-cmd>
                                 [--expect <substring>] [--dry-run]
                                 -- <producing-cmd...>

  --slot    working-memory slot to clear on success (required)
  --verify  read-back command proving the write landed (required; rc=0 AND
            non-empty stdout are both required)
  --expect  additional substring that must appear in the read-back stdout
  --dry-run run both checks and report the verdict, but never clear

Exit: 0 cleared | 1 producing-cmd failed | 2 read-back failed | 3 usage/clear error
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --slot)    SLOT="${2:-}";   shift $(( $# >= 2 ? 2 : 1 )) ;;
        --verify)  VERIFY="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --expect)  EXPECT="${2:-}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --dry-run) DRY_RUN=1;       shift ;;
        -h|--help) usage; exit 3 ;;
        --)        shift; PRODUCING=("$@"); break ;;
        *)
            echo "[sentinel-clear-guarded] ERROR: unknown argument '$1'" >&2
            usage
            exit 3
            ;;
    esac
done

if [[ -z "$SLOT" ]]; then
    echo "[sentinel-clear-guarded] ERROR: --slot is required" >&2
    usage; exit 3
fi
if [[ -z "$VERIFY" ]]; then
    # Deliberately fatal rather than defaulted. A caller that omits the
    # read-back is asking for exactly the rc=0-on-refusal hole this exists to
    # close, so silently degrading to an exit-status-only check would
    # reintroduce the defect under a name that reads as safe.
    echo "[sentinel-clear-guarded] ERROR: --verify is required — exit status alone is not proof a write landed (several stores exit 0 on refusal)" >&2
    usage; exit 3
fi
if [[ ${#PRODUCING[@]} -eq 0 ]]; then
    echo "[sentinel-clear-guarded] ERROR: no producing command given after '--'" >&2
    usage; exit 3
fi

# ── Validate --slot against the registry SSOT ────────────────────────────────
# Found by fresh-eyes review of this very script, PROBED not assumed: a typo'd
# slot (force_experience_archivl) ran the whole pipeline, cleared the nonexistent
# slot, printed "OK ... cleared" and exited 0 — while the REAL sentinel stayed
# SET. That is a false satisfied-state claim made BY the primitive whose entire
# purpose is preventing false satisfied-state claims. The read-back cannot catch
# it: the read-back proves the WRITE landed, and says nothing about whether the
# SLOT being cleared is the one that gates it.
#
# _sentinel_registry.py is already the SSOT every other sentinel consumer reads,
# so validate against it rather than against a caller-supplied string — the
# rb-6013 lesson from the goal that built this script (assert the obligation via
# the SSOT the code already reads, never pin a shape or trust an argument).
#
# Asymmetric failure posture, deliberately: fail-CLOSED on a definite miss (the
# slot is provably not a sentinel), fail-OPEN on a validator error (registry
# unreadable). Wedging the gate because a helper broke would be worse than the
# hole this closes, and the read-back is still the primary safety.
_VALID="$(REG_DIR="$(dirname "${BASH_SOURCE[0]}")" SLOT_TO_CHECK="$SLOT" py -3 -c '
import os, sys
sys.path.insert(0, os.environ["REG_DIR"])
try:
    from _sentinel_registry import SENTINELS
except Exception:
    print("VALIDATOR_ERROR"); raise SystemExit(0)
known = {s["slot"] for s in SENTINELS}
print("YES" if os.environ["SLOT_TO_CHECK"] in known else "NO")
' 2>/dev/null)"

if [[ "$_VALID" == "NO" ]]; then
    echo "[sentinel-clear-guarded] ERROR: '${SLOT}' is not a registered sentinel slot" >&2
    echo "[sentinel-clear-guarded]   Clearing it would report success while the real sentinel stays SET." >&2
    echo "[sentinel-clear-guarded]   Registered slots live in core/scripts/_sentinel_registry.py (SSOT)." >&2
    exit 3
elif [[ "$_VALID" != "YES" ]]; then
    echo "[sentinel-clear-guarded] WARN: could not validate slot '${SLOT}' against the registry — proceeding (read-back is still enforced)" >&2
fi

# ── Step 1: run the producing command ────────────────────────────────────────
"${PRODUCING[@]}"
PRODUCE_RC=$?

if [[ $PRODUCE_RC -ne 0 ]]; then
    echo "[sentinel-clear-guarded] REFUSED — producing command exited rc=${PRODUCE_RC}" >&2
    echo "[sentinel-clear-guarded]   command : ${PRODUCING[*]}" >&2
    echo "[sentinel-clear-guarded]   slot    : ${SLOT} — LEFT SET (obligation not satisfied; it will re-surface next iteration)" >&2
    exit 1
fi

# ── Step 2: read-back (the check exit status cannot give us) ─────────────────
VERIFY_OUT="$(eval "$VERIFY" 2>/dev/null)"
VERIFY_RC=$?

REASON=""
if [[ $VERIFY_RC -ne 0 ]]; then
    REASON="read-back exited rc=${VERIFY_RC}"
elif [[ -z "${VERIFY_OUT//[[:space:]]/}" ]]; then
    # Empty stdout is the signature of the rc=0-on-refusal case: the producing
    # command claimed success and the record is simply not there.
    REASON="read-back returned EMPTY (producing command exited 0 but wrote nothing — the rc=0-on-refusal case)"
elif [[ -n "$EXPECT" && "$VERIFY_OUT" != *"$EXPECT"* ]]; then
    REASON="read-back stdout did not contain --expect '${EXPECT}'"
fi

if [[ -n "$REASON" ]]; then
    echo "[sentinel-clear-guarded] REFUSED — ${REASON}" >&2
    echo "[sentinel-clear-guarded]   verify  : ${VERIFY}" >&2
    echo "[sentinel-clear-guarded]   slot    : ${SLOT} — LEFT SET (obligation not satisfied; it will re-surface next iteration)" >&2
    exit 2
fi

# ── Step 3: both checks passed — clear, as its own invocation (guard-1870) ──
if [[ $DRY_RUN -eq 1 ]]; then
    echo "[sentinel-clear-guarded] DRY-RUN: both checks passed; would clear slot '${SLOT}'"
    exit 0
fi

if ! echo 'null' | bash "$(dirname "${BASH_SOURCE[0]}")/wm-set.sh" "$SLOT" >/dev/null 2>&1; then
    # Do NOT report success here. A failed clear leaves the sentinel set, which
    # is the safe direction, but the caller must know the clear did not happen.
    echo "[sentinel-clear-guarded] ERROR: both checks passed but clearing slot '${SLOT}' FAILED — slot still SET" >&2
    exit 3
fi

echo "[sentinel-clear-guarded] OK — producing command succeeded, read-back confirmed the write landed, slot '${SLOT}' cleared"
exit 0
