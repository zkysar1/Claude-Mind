#!/usr/bin/env bash
# domain-leak-exempt: fleet cutover recipe (asp-372 Phase 1, g-372-08). The
# object-store endpoint env key, the DR-family key names and the health-field
# names are FUNCTIONAL tokens here, the sanctioned marker use per
# domain-free-examples.md "Marker Restriction" — same precedent as
# minio-standup.sh and cold-snapshot-target-provision.sh.
#
# owncloud-endpoint-flip.sh — flip THIS box's object-store endpoint (runbook
# §13.7 FLIP / §14.2 row B6), revert it, or show where the box stands.
#
#   bash core/scripts/owncloud-endpoint-flip.sh --status
#   bash core/scripts/owncloud-endpoint-flip.sh --to http://<store-host>:9000 [--dry-run] [--no-restart]
#   bash core/scripts/owncloud-endpoint-flip.sh --revert [--dry-run] [--no-restart]
#
# WHAT A FLIP IS. One line in .env.local — STORAGE_S3_ENDPOINT_URL=<url> — plus
# a daemon restart (guard-1556: a running process never re-reads its env). The
# bucket name is reused on the basement store, the lock tables stay on the
# incumbent cloud (g-372-01: the override is S3-only) and the credential pair is
# the same (same-pair policy, g-372-07 / g-372-16), so nothing else changes.
# REVERT is the mirror: PUT BACK WHAT WAS THERE, restart. --to parks the pre-flip
# value in mind_api/state/owncloud-endpoint-preflip and --revert restores it,
# falling back to deleting the line only when the capture is empty (there was no
# line before the flip) or absent (nothing captured — an older build, or a
# hand-edit). Before g-372-39 --revert always DELETED, so on a box carrying an
# explicit endpoint a flip/revert cycle silently moved the box off its configured
# route onto the regional default and still reported ok:true. The incumbent bucket
# is kept read-only for 30 days for exactly this reason (§14.2 row B7).
#
# PRECONDITIONS (each refused loudly; none is skippable). P0 gates EVERY action;
# P1-P5 gate --to and --revert:
#   P0 this process runs as the user who OWNS .env.local — the daemon this tool
#      restarts is spawned by whoever runs it, and its rightful owner is that
#      file's owner (root on the containers; the agent user on a box where the
#      agent is not root). Any other user would restart the daemon as the WRONG
#      user with that user's python. Measured 2026-09-11 on the laptop: root's
#      botocore sits below the own-cloud floor while the daemon user's does not,
#      so P5 happened to refuse — nothing structural did. Refused with the
#      exact runuser line to re-run.
#   P1 STORAGE_BACKEND=own-cloud on this box.
#   P2 the DR family is present: COLD_SNAPSHOT_S3_BUCKET + both COLD_SNAPSHOT_AWS_*
#      keys — without it the first weekly DR tick after the flip REFUSES
#      (refused-colocated), by design (g-372-16).
#   P3 `cold-snapshot.sh --print-target` prints a [target] line — proves the
#      guard-bearing cold_snapshot.py is on this box. A stale checkout loses it
#      SILENTLY (cc-14, 2026-09-11: 294 commits behind, Aug-5 file, no guard).
#   P4 this checkout's daemon loader carries the endpoint key
#      (_N3_ALLOWED_EXACT in mind_api/src/__main__.py) — before 2026-09-11 the
#      flipped value reached the daemon only through a best-effort self-heal.
#   P5 the target answers a put/head/get/delete round trip through the
#      PRODUCTION client from THIS box (owncloud-endpoint-probe.py with the
#      override set) BEFORE the line is written.
# VERIFICATION after the restart — the daemon must say it about ITSELF
# (guard-1976; /proc/<pid>/environ is not evidence, guard-4274):
#   V1 /v1/admin/health reports storage_backend=OwnCloudBackend and
#      storage_endpoint == the target (after --revert: the captured pre-flip
#      value, or "" when the capture was empty or absent).
#   V2 cold-snapshot --print-target [target] reads mode=pinned cold_endpoint=aws-regional
#      and live_endpoint=<target> (after --revert: the restored value, or
#      aws-regional when there was nothing to restore).
#   V3 the probe again, with the environment exactly as .env.local now reads.
#
# NO SECRET VALUE IS EVER PRINTED — the URL is not a secret; keys appear by NAME.
# The endpoint var is unset in this script's own environment before the daemon
# is spawned, so the daemon reads the FILE, never a stale shell value.
# EXIT: 0 ok; 1 refused / verification failed (the JSON names which); 2 usage.
set -euo pipefail
umask 077

ACTION=""; URL=""; DRY_RUN=0; RESTART=1
while [ $# -gt 0 ]; do
    case "$1" in
        --status) ACTION=status ;;
        --revert) ACTION=revert ;;
        --to) [ $# -ge 2 ] || { echo "owncloud-endpoint-flip: --to requires a URL" >&2; exit 2; }; ACTION=flip; URL="$2"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --no-restart) RESTART=0 ;;
        -h|--help) echo "usage: owncloud-endpoint-flip.sh --status | --to URL [--dry-run] [--no-restart] | --revert [--dry-run] [--no-restart]   (full doc: the header comments of core/scripts/owncloud-endpoint-flip.sh)"; exit 0 ;;
        *) echo "owncloud-endpoint-flip: unknown arg '$1'" >&2; exit 2 ;;
    esac
    shift
done
[ -n "$ACTION" ] || { echo "owncloud-endpoint-flip: one of --status | --to URL | --revert is required" >&2; exit 2; }

ROOT="${REPO_ROOT:-$PWD}"
[ -f "$ROOT/core/scripts/owncloud-endpoint-probe.py" ] || { echo "owncloud-endpoint-flip: run from the repo root (no core/scripts/owncloud-endpoint-probe.py under $ROOT)" >&2; exit 2; }
cd "$ROOT"
ENV_FILE="$ROOT/.env.local"
[ -f "$ENV_FILE" ] || { echo "owncloud-endpoint-flip: $ENV_FILE not found" >&2; exit 1; }
KEY=STORAGE_S3_ENDPOINT_URL
HOST="$(hostname)"
# Where --to parks the PRE-FLIP value so --revert can put it back (g-372-39).
# Machine-local daemon state, NOT beside .env.local: a sidecar at the repo root
# is a NEW top-level entry, which check-repo-root-entries correctly refuses at
# commit time, and that refusal strands the box's push lane behind a staged index.
# An EMPTY file is meaningful — it records "there was no line before the flip",
# which is what makes the delete path a restore rather than a default.
PREFLIP_FILE="$ROOT/mind_api/state/owncloud-endpoint-preflip"

log()  { printf '[owncloud-endpoint-flip] %s\n' "$*" >&2; }
fail() { printf '{"ok": false, "host": "%s", "action": "%s", "stage": "%s", "detail": "%s"}\n' "$HOST" "$ACTION" "$1" "${2//\"/\'}"; exit 1; }
_get() {
    local v
    v="$(grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
    case "$v" in \"*\"|\'*\') [ ${#v} -ge 2 ] && v="${v:1:${#v}-2}" ;; esac
    printf '%s' "$v"
}

# P0 — user identity, before ANY probe, read or write (see the header). The check is
# uid-to-uid: the file's owner is whoever the daemon must run as.
env_uid="$(stat -c '%u' "$ENV_FILE")"
if [ "$(id -u)" != "$env_uid" ]; then
    env_user="$(stat -c '%U' "$ENV_FILE")"
    fail P0 "running as uid $(id -u) but $ENV_FILE is owned by $env_user (uid $env_uid), who runs the daemon — re-run as that user: runuser -u $env_user -- bash -lc 'cd $ROOT && bash core/scripts/owncloud-endpoint-flip.sh <same arguments>'"
fi

# The governed roots: the probe builds the production backend, which needs them.
# shellcheck disable=SC1091
source "$ROOT/core/scripts/_paths.sh" >/dev/null 2>&1 || true
export WORLD_PATH="${WORLD_PATH:-}" META_PATH="${META_PATH:-}"
# Never let a stale shell value shadow the file — for the probe AND the daemon.
unset STORAGE_S3_ENDPOINT_URL

daemon_health() {
    # Prints "<pid>|<storage_backend>|<storage_endpoint>" from the live daemon,
    # or "-|-|-" when no daemon answers. The daemon reports about ITSELF.
    local port tok body
    port="$(cat "$ROOT/mind_api/state/daemon.port" 2>/dev/null || true)"
    [ -n "$port" ] || { printf -- '-|-|-'; return; }
    body="$(curl -s -m 8 "http://127.0.0.1:$port/v1/admin/health" || true)"
    if [ -z "$body" ] || ! printf '%s' "$body" | grep -q '"ok"'; then
        tok="$(_get MIND_API_TOKEN)"
        [ -n "$tok" ] && body="$(curl -s -m 8 -H "Authorization: Bearer $tok" "http://127.0.0.1:$port/v1/admin/health" || true)"
    fi
    HB="$body" python3 -c '
import json, os
try:
    d = json.loads(os.environ.get("HB", ""))
except Exception:
    print("-|-|-"); raise SystemExit
def s(v): return "-" if v is None else str(v)
print(s(d.get("pid")) + "|" + s(d.get("storage_backend")) + "|" + s(d.get("storage_endpoint")))' 2>/dev/null || printf -- '-|-|-'
}

cold_target() {
    # The guard-5551 [target] line, resolved and printed on stderr WITHOUT any
    # enumeration or hashing (--print-target, g-372-35). This used to call
    # --dry-run, which prints the same line and THEN hashes the whole working
    # set: budgeted 10-25s, measured >630s and still running on a large box, so
    # --status (the advertised SAFE READ-ONLY query) did not return at all.
    # `grep -m1` does not bound it -- command substitution waits for the whole
    # pipeline, and a producer busy hashing never writes again, so it never
    # takes the SIGPIPE that would end it. Measured after: 56ms.
    ( bash core/scripts/cold-snapshot.sh --print-target 2>&1 >/dev/null ) | grep -m1 '^\[target\]' || true
}

probe() {
    # $1 = endpoint to force ("" = exactly what .env.local says). Prints the JSON line.
    local want="$1"
    ( set -a; . "$ENV_FILE"; set +a
      if [ -n "$want" ]; then export STORAGE_S3_ENDPOINT_URL="$want"; fi
      python3 core/scripts/owncloud-endpoint-probe.py ) 2>/dev/null || true
}

write_env() {
    # $1 = "set <url>" | "delete". Temp file beside the target, mode 600, owner
    # preserved, atomic mv — the same shape as cold-snapshot-target-provision.sh.
    local mode="$1" url="${2:-}" owner tmp ln key wrote=0
    owner="$(stat -c '%u:%g' "$ENV_FILE")"
    tmp="$(mktemp "${ENV_FILE}.flip.XXXXXX")"
    chmod 600 "$tmp"
    while IFS= read -r ln || [ -n "$ln" ]; do
        key="${ln%%=*}"
        if [ "$key" = "$KEY" ]; then
            if [ "$mode" = set ]; then printf '%s=%s\n' "$KEY" "$url" >> "$tmp"; wrote=1; fi
            continue   # delete: drop the line
        fi
        printf '%s\n' "${ln%$'\r'}" >> "$tmp"
    done < "$ENV_FILE"
    if [ "$mode" = set ] && [ "$wrote" -eq 0 ]; then
        if [ -s "$tmp" ] && [ "$(tail -c1 "$tmp" | wc -l)" -eq 0 ]; then printf '\n' >> "$tmp"; fi
        printf '%s=%s\n' "$KEY" "$url" >> "$tmp"
    fi
    chown "$owner" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown "$owner" "$ENV_FILE" 2>/dev/null || true
}

restart_daemon() {
    [ "$RESTART" -eq 1 ] || { log "--no-restart: the running daemon keeps its OLD endpoint until it is recycled (guard-1556)"; return 0; }
    log "restarting the daemon so it re-reads .env.local"
    bash core/scripts/mind-api-start.sh --restart >/dev/null 2>&1 || fail restart "mind-api-start.sh --restart failed"
    local i h
    for i in $(seq 1 30); do
        h="$(daemon_health)"
        [ "${h%%|*}" != "-" ] && return 0
        sleep 1
    done
    fail restart "daemon did not answer /v1/admin/health within 30s of the restart"
}

current="$(_get "$KEY")"
git_head="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '-')"

# ── status ────────────────────────────────────────────────────────────────────
if [ "$ACTION" = status ]; then
    h="$(daemon_health)"; dpid="${h%%|*}"; rest="${h#*|}"; dbackend="${rest%%|*}"; dendpoint="${rest#*|}"
    ct="$(cold_target)"
    consistent=false
    if [ "$dendpoint" = "$current" ] && [ "$dbackend" = "OwnCloudBackend" ]; then consistent=true; fi
    printf '{"ok": true, "host": "%s", "action": "status", "git_head": "%s", "env_file_endpoint": "%s", "daemon_pid": "%s", "daemon_storage_backend": "%s", "daemon_storage_endpoint": "%s", "daemon_matches_file": %s, "cold_target": "%s"}\n' \
        "$HOST" "$git_head" "$current" "$dpid" "$dbackend" "$dendpoint" "$consistent" "${ct//\"/\'}"
    exit 0
fi

# ── shared preconditions (flip and revert) ────────────────────────────────────
[ "$(_get STORAGE_BACKEND)" = "own-cloud" ] || fail P1 "STORAGE_BACKEND is not own-cloud on this box"
for k in COLD_SNAPSHOT_S3_BUCKET COLD_SNAPSHOT_AWS_ACCESS_KEY_ID COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY; do
    [ -n "$(_get "$k")" ] || fail P2 "$k missing from .env.local — provision the DR family first (cold-snapshot-target-provision.sh)"
done
ct="$(cold_target)"
[ -n "$ct" ] || fail P3 "cold-snapshot.sh --print-target printed no [target] line — this checkout lacks the colocation guard, or predates --print-target (stale? git pull first)"
grep -q "\"$KEY\"" "$ROOT/mind_api/src/__main__.py" || fail P4 "this checkout's daemon loader (_N3_ALLOWED_EXACT) does not carry $KEY — pull the 2026-09-11 fix first"

# ── flip ──────────────────────────────────────────────────────────────────────
if [ "$ACTION" = flip ]; then
    printf '%s' "$URL" | grep -qE '^https?://[^[:space:]/]+(:[0-9]+)?/?$' || fail usage "target must look like http(s)://host[:port] — got '$URL'"
    URL="${URL%/}"
    pre="$(probe "$URL")"
    printf '%s' "$pre" | grep -q '"ok": true' || fail P5 "round trip through the production client FAILED against $URL before writing anything: ${pre:-no output}"
    log "P1-P5 ok: $URL answers a put/head/get/delete through the production client from $HOST"
    h="$(daemon_health)"; rest="${h#*|}"; dendpoint="${rest#*|}"
    if [ "$current" = "$URL" ] && [ "$dendpoint" = "$URL" ]; then
        printf '{"ok": true, "host": "%s", "action": "flip", "endpoint": "%s", "already": true, "cold_target": "%s"}\n' "$HOST" "$URL" "${ct//\"/\'}"; exit 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: would write $KEY=$URL to $ENV_FILE (currently '${current:-<unset>}'), restart the daemon, verify V1-V3"
        printf '{"ok": true, "dry_run": true, "host": "%s", "action": "flip", "endpoint": "%s", "current": "%s", "cold_target": "%s"}\n' "$HOST" "$URL" "$current" "${ct//\"/\'}"; exit 0
    fi
    # Park the PRE-FLIP value so --revert can restore it (g-372-39). Written
    # only on the path that actually changes the file — the already-true exit
    # above and the dry-run exit both return before here, so neither records a
    # capture for a flip that never happened.
    # ONLY WHEN ABSENT, so a flip -> flip -> revert chain returns to the state
    # the FIRST flip found, not to an intermediate one. "Put it back" means the
    # state the caller started from.
    if [ ! -e "$PREFLIP_FILE" ]; then
        mkdir -p "$(dirname "$PREFLIP_FILE")" 2>/dev/null || true
        printf '%s' "$current" > "$PREFLIP_FILE" 2>/dev/null \
            && log "captured pre-flip $KEY='${current:-<unset>}' for --revert ($PREFLIP_FILE)" \
            || log "WARNING: could not write $PREFLIP_FILE — --revert will fall back to deleting the line"
    fi
    write_env set "$URL"
    [ "$(_get "$KEY")" = "$URL" ] || fail write "read-back of $KEY did not match"
    log "wrote $KEY=$URL (mode 600, owner preserved)"
    restart_daemon
    want_live="$URL"
else
    # ── revert ────────────────────────────────────────────────────────────────
    # A capture is present => restore it; absent-but-present-and-empty => the box
    # genuinely had no line before the flip, so deleting IS the restore; no file
    # at all => nothing was captured (flipped by an older build, or by hand), so
    # keep the historical delete behaviour rather than guessing.
    # An UNREADABLE capture is not an empty one. `|| true` would collapse both to
    # restore='' and send a real recorded endpoint down the delete path, logging
    # "there was no line before the flip" — reinstating, through the error path,
    # exactly the silent state loss this flag was written to end (guard-542: an
    # OR-fallback clobbers the value it was meant to default; guard-2455: a read
    # that fails does not make its consumer fail with it).
    restore=""; have_capture=0; capture_read_ok=0
    if [ -e "$PREFLIP_FILE" ]; then
        have_capture=1
        if restore="$(tr -d '\r\n' < "$PREFLIP_FILE" 2>/dev/null)"; then
            capture_read_ok=1
        else
            restore=""
        fi
    fi
    # Fail CLOSED: a capture we cannot read might hold anything, and the only
    # irreversible move available here is deleting the line. Refuse instead.
    if [ "$have_capture" -eq 1 ] && [ "$capture_read_ok" -eq 0 ]; then
        fail read "pre-flip capture $PREFLIP_FILE exists but could not be read — refusing to revert, because the fallback would DELETE the $KEY line and silently discard whatever it recorded. Repair the file's readability and re-run; or, if you have established the box genuinely had no $KEY line before the flip, remove the file and re-run."
    fi
    # "already" is only true when there is nothing to put back. With a non-empty
    # capture there IS something to restore, so this early exit must not fire —
    # otherwise the flip/revert cycle reports success having restored nothing.
    if [ -z "$current" ] && [ -z "$restore" ]; then
        h="$(daemon_health)"; rest="${h#*|}"; dendpoint="${rest#*|}"
        if [ "$dendpoint" = "" ] || [ "$dendpoint" = "-" ]; then
            rm -f "$PREFLIP_FILE" 2>/dev/null || true
            printf '{"ok": true, "host": "%s", "action": "revert", "already": true, "cold_target": "%s"}\n' "$HOST" "${ct//\"/\'}"; exit 0
        fi
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ -n "$restore" ]; then
            log "DRY-RUN: would RESTORE $KEY=$restore from $PREFLIP_FILE (currently '${current:-<unset>}'), restart the daemon, verify V1-V3"
        else
            log "DRY-RUN: would delete the $KEY line from $ENV_FILE (currently '${current:-<unset>}'), restart the daemon, verify V1-V3 against the incumbent (capture: $([ "$have_capture" -eq 1 ] && echo 'empty — there was no line before the flip' || echo 'none recorded'))"
        fi
        printf '{"ok": true, "dry_run": true, "host": "%s", "action": "revert", "current": "%s", "restore": "%s", "captured": %s}\n' "$HOST" "$current" "$restore" "$([ "$have_capture" -eq 1 ] && echo true || echo false)"; exit 0
    fi
    if [ -n "$restore" ]; then
        write_env set "$restore"
        [ "$(_get "$KEY")" = "$restore" ] || fail write "read-back of the restored $KEY did not match"
        log "restored $KEY=$restore from the pre-flip capture (mode 600, owner preserved)"
        want_live="$restore"
    else
        write_env delete
        [ -z "$(_get "$KEY")" ] || fail write "$KEY line still present after the delete"
        log "deleted the $KEY line (mode 600, owner preserved)$([ "$have_capture" -eq 1 ] && echo ' — the capture records that there was no line before the flip' || echo ' — no pre-flip capture was recorded')"
        want_live=""
    fi
    # Consume the capture only after the write succeeded: a failed restore leaves
    # it in place so a re-run can still put the box back.
    rm -f "$PREFLIP_FILE" 2>/dev/null || true
    restart_daemon
fi

# ── verification V1-V3 ────────────────────────────────────────────────────────
if [ "$RESTART" -eq 1 ]; then
    h="$(daemon_health)"; dpid="${h%%|*}"; rest="${h#*|}"; dbackend="${rest%%|*}"; dendpoint="${rest#*|}"
    [ "$dbackend" = "OwnCloudBackend" ] || fail V1 "daemon reports storage_backend='$dbackend' (want OwnCloudBackend)"
    [ "$dendpoint" = "$want_live" ] || fail V1 "daemon reports storage_endpoint='$dendpoint' (want '${want_live:-<regional default>}') — it did not re-read .env.local"
else
    dpid="-"; dendpoint="(not restarted)"
fi
ct2="$(cold_target)"
case "$ct2" in
    *"mode=pinned"*"cold_endpoint=aws-regional"*"live_endpoint=${want_live:-aws-regional}"*) ;;
    *) fail V2 "cold-snapshot [target] after the change reads: ${ct2:-<none>}" ;;
esac
post="$(probe "")"
printf '%s' "$post" | grep -q '"ok": true' || fail V3 "post-change round trip with the file's environment FAILED: ${post:-no output}"
resolved="$(printf '%s' "$post" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("resolved_endpoint",""), d.get("ms",""))' 2>/dev/null || true)"
printf '{"ok": true, "host": "%s", "action": "%s", "endpoint": "%s", "daemon_pid": "%s", "daemon_storage_endpoint": "%s", "probe_resolved_endpoint_ms": "%s", "cold_target": "%s", "restarted": %s}\n' \
    "$HOST" "$ACTION" "$want_live" "$dpid" "$dendpoint" "$resolved" "${ct2//\"/\'}" "$([ "$RESTART" -eq 1 ] && echo true || echo false)"
