#!/usr/bin/env bash
# domain-leak-exempt: fleet-provisioning recipe. The credential env-key names
# and the object-store bucket are FUNCTIONAL domain tokens here (the script
# writes named daemon env vars on a fleet box — asp-372 Phase 1, g-372-16),
# the sanctioned marker use per domain-free-examples.md "Marker Restriction"
# and the domain-recipe-seed-purity.md decision; same precedent as
# provision-from-vault.sh and minio-standup.sh.
#
# cold-snapshot-target-provision.sh — pin the cold-snapshot DR target on THIS
# box by provisioning the COLD_SNAPSHOT_* family into its .env.local (g-372-16,
# the pre-flip B6 step of the object-store cutover, runbook §13.6a DECIDED).
#
# WHY. cold_snapshot.py resolves its OWN target from COLD_SNAPSHOT_S3_BUCKET +
# COLD_SNAPSHOT_AWS_ACCESS_KEY_ID + COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY (+ an
# optional COLD_SNAPSHOT_S3_ENDPOINT_URL, blank = the regional cloud endpoint)
# and REFUSES to run the moment STORAGE_S3_ENDPOINT_URL is set with no target
# configured (refused-colocated, rc 2). So every box must carry the family
# BEFORE its endpoint is flipped, or the first weekly DR snapshot after the
# cutover refuses — loudly, by design.
#
# VALUES (same-pair policy, decided 2026-09-11 with g-372-07): the DR key pair
# IS the box's existing scoped pair in MIND_AWS_*. The own-cloud backend builds
# its object-store client and its lock-table client from ONE credential session,
# the lock table stays on the incumbent cloud after the flip, and the basement
# store's fleet user carries the same pair — so MIND_AWS_* stays cloud-valid
# and the DR archive can reuse it. The bucket is the RETAINED cloud bucket —
# by default the box's STORAGE_S3_BUCKET as read at provisioning time, which
# pre-flip is exactly that bucket (the basement store deliberately reuses the
# name, so the value is the same post-flip too). The endpoint is deliberately
# NOT written: blank means the regional cloud endpoint, which is the point.
#
# RUN FROM THE REPO ROOT ON THE BOX (ships cleanly over `ssh ... 'bash -s'`):
#     cd <repo-root> && bash core/scripts/cold-snapshot-target-provision.sh [--dry-run] [--replace] [--bucket B] [--env-file P] [--no-verify]
#
# BEHAVIOUR. Idempotent: a key already present with the same value is left
# alone; a different value is REFUSED unless --replace (a rotation is a
# deliberate act). Appends preserve mode 600 (umask 077, temp file + mv).
# NO SECRET VALUE IS EVER PRINTED — the summary carries key NAMES and verdicts.
# Verification runs `cold-snapshot.sh --dry-run` and requires its [target] line
# to read mode=pinned cold_endpoint=aws-regional; anything else exits 1.
#
# EXIT: 0 provisioned+verified (or dry-run); 1 refusal/verification failure; 2 usage.
set -euo pipefail
umask 077

DRY_RUN=0; REPLACE=0; VERIFY=1; BUCKET=""; ENV_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --replace) REPLACE=1 ;;
        --no-verify) VERIFY=0 ;;
        --bucket) [ $# -ge 2 ] || { echo "cold-snapshot-target-provision: --bucket requires a value" >&2; exit 2; }; BUCKET="$2"; shift ;;
        --env-file) [ $# -ge 2 ] || { echo "cold-snapshot-target-provision: --env-file requires a value" >&2; exit 2; }; ENV_FILE="$2"; shift ;;
        -h|--help) echo "usage: cold-snapshot-target-provision.sh [--dry-run] [--replace] [--no-verify] [--bucket B] [--env-file P]   (full doc: the header comments of core/scripts/cold-snapshot-target-provision.sh — under 'bash -s' \$0 is the shell, so they cannot be printed from here)"; exit 0 ;;
        *) echo "cold-snapshot-target-provision: unknown arg '$1'" >&2; exit 2 ;;
    esac
    shift
done

ROOT="${REPO_ROOT:-$PWD}"
[ -f "$ROOT/core/scripts/cold_snapshot.py" ] || { echo "cold-snapshot-target-provision: run from the repo root (no core/scripts/cold_snapshot.py under $ROOT)" >&2; exit 2; }
: "${ENV_FILE:=$ROOT/.env.local}"
[ -f "$ENV_FILE" ] || { echo "cold-snapshot-target-provision: $ENV_FILE not found" >&2; exit 1; }

_get() {
    local v
    v="$(grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
    # strip ONE pair of surrounding quotes, exactly as the Python readers do
    # (storage_backend._bootstrap_env_defaults) — otherwise a quoted bucket
    # value fails the [target] comparison below (fresh-eyes review 2026-09-11).
    case "$v" in \"*\"|\'*\') [ ${#v} -ge 2 ] && v="${v:1:${#v}-2}" ;; esac
    printf '%s' "$v"
}
log() { printf '[cold-snapshot-target-provision] %s\n' "$*" >&2; }

backend="$(_get STORAGE_BACKEND)"
if [ "$backend" != "own-cloud" ]; then
    log "STORAGE_BACKEND is '${backend:-unset}' on this box — nothing to pin (the family is only meaningful under own-cloud)"
    printf '{"ok": true, "host": "%s", "skipped": "backend-not-own-cloud"}\n' "$(hostname)"
    exit 0
fi
akid="$(_get MIND_AWS_ACCESS_KEY_ID)"; asec="$(_get MIND_AWS_SECRET_ACCESS_KEY)"
[ -n "$akid" ] && [ -n "$asec" ] || { log "MIND_AWS_ACCESS_KEY_ID / MIND_AWS_SECRET_ACCESS_KEY missing or empty in $ENV_FILE — refusing (the same-pair policy needs the scoped pair)"; exit 1; }
live_endpoint="$(_get STORAGE_S3_ENDPOINT_URL)"
if [ -z "$BUCKET" ]; then
    BUCKET="$(_get STORAGE_S3_BUCKET)"
    [ -n "$BUCKET" ] || { log "STORAGE_S3_BUCKET missing in $ENV_FILE and no --bucket given"; exit 1; }
    if [ -n "$live_endpoint" ]; then
        log "NOTE: STORAGE_S3_ENDPOINT_URL is already set on this box; defaulting the DR bucket to STORAGE_S3_BUCKET=$BUCKET (the basement store reuses the cloud bucket's name, so this is the retained cloud bucket — pass --bucket to override)"
    fi
fi

declare -A WANT=(
    [COLD_SNAPSHOT_S3_BUCKET]="$BUCKET"
    [COLD_SNAPSHOT_AWS_ACCESS_KEY_ID]="$akid"
    [COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY]="$asec"
)
ORDER=(COLD_SNAPSHOT_S3_BUCKET COLD_SNAPSHOT_AWS_ACCESS_KEY_ID COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY)
APPEND=(); REPLACE_KEYS=(); UNCHANGED=()
for k in "${ORDER[@]}"; do
    cur="$(_get "$k")"
    if ! grep -q "^$k=" "$ENV_FILE"; then APPEND+=("$k")
    elif [ "$cur" = "${WANT[$k]}" ]; then UNCHANGED+=("$k")
    elif [ "$REPLACE" -eq 1 ]; then REPLACE_KEYS+=("$k")
    else log "$k is present with a DIFFERENT value — refusing to overwrite without --replace (values compared in memory, not printed)"; exit 1
    fi
done
# The endpoint var must stay ABSENT or BLANK: a non-blank value that equals the
# live endpoint is the colocated DR archive this whole step exists to prevent.
ep="$(_get COLD_SNAPSHOT_S3_ENDPOINT_URL)"
if [ -n "$ep" ]; then
    log "COLD_SNAPSHOT_S3_ENDPOINT_URL is set to a non-blank value on this box — the DR target must be the regional cloud endpoint (blank). Remove it and re-run."; exit 1
fi

log "plan: append=[${APPEND[*]:-}] replace=[${REPLACE_KEYS[*]:-}] unchanged=[${UNCHANGED[*]:-}] bucket=$BUCKET file=$ENV_FILE"
if [ "$DRY_RUN" -eq 1 ]; then
    printf '{"ok": true, "dry_run": true, "host": "%s", "append": %d, "replace": %d, "unchanged": %d}\n' "$(hostname)" "${#APPEND[@]}" "${#REPLACE_KEYS[@]}" "${#UNCHANGED[@]}"
    exit 0
fi

if [ "${#APPEND[@]}" -gt 0 ] || [ "${#REPLACE_KEYS[@]}" -gt 0 ]; then
    # Preserve the file's owner: on a box where the agent runs as a non-root
    # user and this script arrives over root ssh, a root-owned replacement
    # would lock the agent out of its own credentials.
    owner="$(stat -c '%u:%g' "$ENV_FILE")"
    tmp="$(mktemp "${ENV_FILE}.cold.XXXXXX")"
    chmod 600 "$tmp"
    while IFS= read -r ln || [ -n "$ln" ]; do
        key="${ln%%=*}"
        replaced=0
        for k in "${REPLACE_KEYS[@]:-}"; do
            [ -n "$k" ] || continue
            if [ "$key" = "$k" ]; then printf '%s=%s\n' "$k" "${WANT[$k]}" >> "$tmp"; replaced=1; break; fi
        done
        [ "$replaced" -eq 1 ] || printf '%s\n' "${ln%$'\r'}" >> "$tmp"
    done < "$ENV_FILE"
    if [ -s "$tmp" ] && [ "$(tail -c1 "$tmp" | wc -l)" -eq 0 ]; then printf '\n' >> "$tmp"; fi
    for k in "${APPEND[@]:-}"; do
        [ -n "$k" ] || continue
        printf '%s=%s\n' "$k" "${WANT[$k]}" >> "$tmp"
    done
    chown "$owner" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown "$owner" "$ENV_FILE" 2>/dev/null || true
    log "wrote $ENV_FILE (mode 600, owner preserved): appended ${#APPEND[@]}, replaced ${#REPLACE_KEYS[@]}"
fi

# post-write read-back by NAME (values compared in memory)
for k in "${ORDER[@]}"; do
    [ "$(_get "$k")" = "${WANT[$k]}" ] || { log "read-back mismatch on $k"; exit 1; }
done

verdict="not-verified"
if [ "$VERIFY" -eq 1 ]; then
    # The dry run prints the guard-5551 [target] line on stderr before any
    # enumeration; that line is the whole verification.
    target_line="$( (cd "$ROOT" && bash core/scripts/cold-snapshot.sh --dry-run 2>&1 >/dev/null) | grep -m1 '^\[target\]' || true)"
    log "${target_line:-[target] line not found}"
    case "$target_line" in
        *"mode=pinned"*"cold_endpoint=aws-regional"*"cold_bucket=${BUCKET}"*) verdict="pinned" ;;
        *) log "verification FAILED: expected mode=pinned cold_endpoint=aws-regional cold_bucket=$BUCKET"; printf '{"ok": false, "host": "%s", "verdict": "%s"}\n' "$(hostname)" "${target_line//\"/\'}"; exit 1 ;;
    esac
fi
printf '{"ok": true, "host": "%s", "verdict": "%s", "bucket": "%s", "appended": %d, "replaced": %d, "unchanged": %d}\n' \
    "$(hostname)" "$verdict" "$BUCKET" "${#APPEND[@]}" "${#REPLACE_KEYS[@]}" "${#UNCHANGED[@]}"
