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
# WHICH HELPER DO I USE? (the single-writer decision, )
# Two helpers harden the two DIFFERENT halves of a sentinel dispatch, and they
# compose rather than compete:
#
#   verified-wm-set.sh      proves the CLEAR landed   — universal
#   sentinel-clear-guarded  proves the GATED WRITE landed — subset
#
# verified-wm-set.sh is the SINGLE WRITER of every sentinel clear, including
# this script's own (see Step 3 below). That is safe to make universal because
# its read-back reads the SLOT ITSELF, which always exists — there is no gate
# for which the assertion is unavailable.
#
# This script is deliberately NOT the single writer, and the reason is worth
# stating so nobody "finishes the job" later. It REQUIRES `--verify`, and that
# is correct only where the dispatch produces ONE observable artifact to read
# back. Measured 2026-08-10: of the seven live sentinels, exactly ONE uses this
# script — force_experience_archival, whose dispatch writes an experience record
# that `--verify` can read back by id. The rest dispatch an ACTION with no such
# artifact: `force_pre_apply_consult` dispatches "run a consult OR log why it is
# not applicable", `fresh_eyes_dispatch_pending` and `pipeline_reconcile_pending`
# dispatch skill invocations. Forcing those through this script would mean
# inventing a synthetic `--verify` per site, and a synthetic verify that always
# succeeds is strictly WORSE than none: it manufactures the proof, which is the
# vacuously-satisfied-conditional failure (guard-2982). Use this script where a
# real artifact exists; use verified-wm-set.sh everywhere, including here.
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
# TWO REGISTRIES, ONE GATE (). There are two sentinel families and this
# gate validated against ONE, so it REFUSED every entry-battery slot —
# pending_phase_6_spark and blocked_sleep_until — which have the most producers
# (recurring-close.sh, iteration-close.sh, iteration-close-reminder.py). The
# mechanism built to couple clear-to-proof was therefore structurally unavailable
# to the family that needed it most, leaving a bare `echo null | wm-set.sh` as the
# only way to clear them: exactly the uncoupled pattern this script exists to kill.
#   _sentinel_registry.SENTINELS                 -> PRECHECK battery (aspirations-precheck)
#   orchestrator-entry-battery.ENTRY_CHECKS      -> ENTRY battery (aspirations Phase -0.5*)
# The fix UNIONS the two slot-name sets HERE rather than hoisting the entry slots
# into _sentinel_registry.py, and that is a deliberate scope choice (guard-3448 —
# a gate is only as broad as its entry points, so check every caller). SENTINELS
# has three consumers; hoisting would change what precheck-sentinel-battery.py
# enumerates and would falsify _sentinel_registry's own docstring ("SSOT for
# PRECHECK force-gate sentinels"). This gate reads the registries for slot NAMES
# only — no other field — so a union here fixes the defect while leaving every
# other consumer's view byte-identical.
#
# Asymmetric failure posture, deliberately: fail-CLOSED on a definite miss (the
# slot is provably not a sentinel), fail-OPEN on a validator error (the PRECHECK
# registry unreadable). Wedging the gate because a helper broke would be worse
# than the hole this closes, and the read-back is still the primary safety.
#
# The entry-battery half is loaded BEST-EFFORT and deliberately does NOT share
# that fail-open path — this was measured, not assumed. Folding both loads into
# one try made the entry registry a HARD dependency, so a context where only the
# precheck registry is present (test-sentinel-clear-guarded.sh copies the script
# to a tmp dir and stubs _sentinel_registry.py alone) degraded EVERY verdict to
# VALIDATOR_ERROR — silently voiding the typo protection this gate exists for.
# Its own suite caught it: 2 of 16 red, including the exact force_experience_archivl
# case from the founding incident. Best-effort keeps every pre-existing verdict
# byte-identical and only ADDS entry slots when the file is there.
# The residue, stated because it is a real trade: if orchestrator-entry-battery.py
# were MISSING in production, entry slots would refuse again (the old defect
# returns) rather than fail open. That is the right way round — the file is a
# committed framework file whose absence is a far larger problem, whereas voiding
# typo protection is silent and is what the founding incident actually cost.
_VALID="$(REG_DIR="$(dirname "${BASH_SOURCE[0]}")" SLOT_TO_CHECK="$SLOT" py -3 -c '
import os, sys, importlib.util
reg_dir = os.environ["REG_DIR"]
sys.path.insert(0, reg_dir)
try:
    from _sentinel_registry import SENTINELS
except Exception:
    print("VALIDATOR_ERROR"); raise SystemExit(0)
known = {s["slot"] for s in SENTINELS}
try:
    # Hyphenated filename is not importable by name — load it by path.
    spec = importlib.util.spec_from_file_location(
        "_entry_battery", os.path.join(reg_dir, "orchestrator-entry-battery.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    known |= {c["slot"] for c in mod.ENTRY_CHECKS if c.get("kind") == "wm_slot"}
except Exception:
    pass  # best-effort: precheck verdict stands on its own
print("YES" if os.environ["SLOT_TO_CHECK"] in known else "NO")
' 2>/dev/null)"

if [[ "$_VALID" == "NO" ]]; then
    echo "[sentinel-clear-guarded] ERROR: '${SLOT}' is not a registered sentinel slot" >&2
    echo "[sentinel-clear-guarded]   The slot name matches no entry in EITHER sentinel registry, so it is" >&2
    echo "[sentinel-clear-guarded]   most likely a typo — clearing it would report success while the real" >&2
    echo "[sentinel-clear-guarded]   sentinel stays SET. Registered slots live in:" >&2
    echo "[sentinel-clear-guarded]     core/scripts/_sentinel_registry.py            (precheck battery)" >&2
    echo "[sentinel-clear-guarded]     core/scripts/orchestrator-entry-battery.py    (entry battery, kind=wm_slot)" >&2
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

# Routed through verified-wm-set.sh, not a bare wm-set.sh (). This
# script proves the GATED WRITE landed; until now it proved nothing about its
# OWN clear, which it issued as `wm-set.sh ... >/dev/null 2>&1` and judged by rc
# alone. That is the very shape this file exists to refuse: an rc=0 write that
# did not persist would print the OK line below while the sentinel stayed SET —
# the read-back is the assertion, and rc is not sufficient. verified-wm-set.sh
# writes, reads the slot back, asserts JSON-canonical equality, retries once,
# and only then reports success.
#
# stdout is suppressed (its "landed (verified)" line duplicates the OK line
# below) but stderr is NOT — its failure diagnostic is the whole point, and
# re-silencing it would reintroduce the defect one layer down.
if ! echo 'null' | bash "$(dirname "${BASH_SOURCE[0]}")/verified-wm-set.sh" "$SLOT" >/dev/null; then
    # Do NOT report success here. A failed clear leaves the sentinel set, which
    # is the safe direction, but the caller must know the clear did not happen.
    echo "[sentinel-clear-guarded] ERROR: both checks passed but clearing slot '${SLOT}' FAILED — slot still SET" >&2
    exit 3
fi

echo "[sentinel-clear-guarded] OK — producing command succeeded, read-back confirmed the write landed, slot '${SLOT}' cleared"
exit 0
