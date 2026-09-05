#!/usr/bin/env bash
# abandoned-claim-check.sh — deferrable precheck lane for abandoned claims ().
#
# Detects goals sitting `status=in-progress, claimed_by=<agent>` that NO live
# in-flight row accounts for. See _abandoned_claim.py for the predicate, why the
# three existing claim tools cannot see this class, and the keep-safe rationale.
#
# THIS WRAPPER IS NOT OPTIONAL CEREMONY. It is what makes the reads
# AUTHORITATIVE: only `_paths.sh` resolves WORLD_PATH from the per-agent
# local-paths.conf, and `team-state-read.sh` is the authoritative reader while
# the local tree is a read-through cache (guard-980). A bare
# `py -3 abandoned-claim-check.py` would read a mirror, see zero in-flight rows,
# and report the entire fleet as abandoned. The `--authoritative` flag is passed
# from here and ONLY from here; without it the detector releases nothing.
#
# Report-only by default. `--apply` releases ONLY rows the detector marked
# RELEASABLE, which requires all four keep-safe conditions to hold.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

APPLY=0
JSON=0
THRESHOLD=180
PASSTHRU=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --json) JSON=1; PASSTHRU+=(--json); shift ;;
        --threshold-minutes) THRESHOLD="$2"; shift 2 ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) PASSTHRU+=("$1"); shift ;;
    esac
done

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

TS_FILE="$TMPDIR_RUN/team-state.json"
GOALS_FILE="$TMPDIR_RUN/goals.json"

# Authoritative team-state read. If this fails the detector still RUNS (it must
# report), but --authoritative is withheld so nothing can be released.
AUTH_FLAG=()
if bash "$SCRIPT_DIR/team-state-read.sh" --json > "$TS_FILE" 2>/dev/null \
   && [[ -s "$TS_FILE" ]]; then
    AUTH_FLAG=(--authoritative)
else
    echo "[abandoned-claim] WARN: authoritative team-state read FAILED — report only, no releases" >&2
fi

# The in-progress population. A failure here yields an empty scan, which the
# detector reports loudly rather than treating as clean.
bash "$SCRIPT_DIR/aspirations-query.sh" --goal-status in-progress --full \
    > "$GOALS_FILE" 2>/dev/null || true

OUT_FILE="$TMPDIR_RUN/report.txt"
python3 "$SCRIPT_DIR/abandoned-claim-check.py" \
    --team-state "$TS_FILE" \
    --goals "$GOALS_FILE" \
    --threshold-minutes "$THRESHOLD" \
    "${AUTH_FLAG[@]+"${AUTH_FLAG[@]}"}" \
    "${PASSTHRU[@]+"${PASSTHRU[@]}"}" > "$OUT_FILE" 2>&1
rc=$?

# Strip the machine-readable tail from human output; it is for the loop below.
grep -v '^RELEASABLE_IDS ' "$OUT_FILE" || true

if [[ $APPLY -eq 1 ]]; then
    # THE GATE USED TO READ `$APPLY -eq 1 && $JSON -eq 0`, which made
    # `--apply --json` -- the natural machine-readable form -- a SILENT no-op
    # whose stdout was BYTE-IDENTICAL to a dry run. Apply now runs in both
    # modes; under --json every release line goes to stderr so stdout stays
    # pure JSON.
    if [[ $JSON -eq 1 ]]; then say() { echo "$@" >&2; }; else say() { echo "$@"; }; fi
    IDS="$(grep '^RELEASABLE_IDS ' "$OUT_FILE" | head -1 | cut -d' ' -f2-)"
    if [[ -n "${IDS:-}" ]]; then
        for gid in $IDS; do
            # `--reason-kind` TYPES the reason; it does not replace it.
            # aspirations-release.sh REFUSES the kind with no `--reason`
            # (exit 1, "the token types the reason"), so passing the kind alone
            # failed EVERY release this lane ever attempted -- and the old
            # `>/dev/null 2>&1` swallowed the message that says so. Detection
            # worked from day one while remediation was dead on arrival; the
            # unit tests covered the pure predicate and never the wrapper's
            # production arg shape (guard-920). Keep the reason and the kind
            # together, and keep the diagnostic.
            # --reason-kind progress preserves the outcome/progress notes. The
            #  record carried 219,496 chars of prior work and release
            # kept all of it; a note-destroying release would make this lane
            # worse than the bug.
            if err="$(bash "$SCRIPT_DIR/aspirations-release.sh" "$gid" \
                        --source world --reason-kind progress \
                        --reason "abandoned-claim lane (precheck 0.5b.23): no in-flight row of either shape accounts for this claim, it is past the ${THRESHOLD}m threshold, and the team-state read was authoritative. Returned to the pool; notes preserved." \
                        2>&1 >/dev/null)"; then
                say "[abandoned-claim] released $gid (notes preserved)"
            else
                # Loud, WITH the tool's own message. rc stays 0 by contract
                # (see the note below) -- judge this lane by OUTPUT.
                say "[abandoned-claim] release FAILED for $gid — left claimed: ${err:-no diagnostic}"
            fi
        done
    else
        say "[abandoned-claim] --apply: nothing met all four keep-safe conditions"
    fi
fi

# Always 0: this is a report lane, and a non-zero would make the precheck
# battery read a healthy detection as a stage failure. Judge by OUTPUT.
exit 0
