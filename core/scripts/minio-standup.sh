#!/usr/bin/env bash
# domain-leak-exempt: fleet-provisioning recipe. The object-store binary name,
# the bucket, the bind address and the fleet credential surface are FUNCTIONAL
# domain tokens here (the script stands up the basement object store that the
# own-cloud tier cuts over to — asp-372 Phase 1, g-372-07). This is the
# sanctioned marker use per domain-free-examples.md "Marker Restriction" and
# the domain-recipe-seed-purity.md decision (domain-touching provisioning code
# lives in core/ under the marker and travels in the seed), the same precedent
# as provision-from-vault.sh.
#
# minio-standup.sh — stand up the Phase 1 object store (MinIO, single node,
# single drive) on the storage host, idempotently, from the staged binaries.
#
# RUN ON THE STORAGE HOST AS ROOT (it is shipped there over ssh as a stdin
# stream, never copied with secrets inside it):
#
#     { printf 'export MINIO_FLEET_ACCESS_KEY=%q\n' "$AK"; \
#       printf 'export MINIO_FLEET_SECRET_KEY=%q\n' "$SK"; \
#       cat core/scripts/minio-standup.sh; } | ssh root@<host> 'bash -s'
#
# Secrets travel ONLY on the ssh stdin stream: never in the remote argv (which
# is visible in `ps` on the host), never in a file this script writes except
# the mode-600 /etc/default/minio (root credentials, root-only) and the mode-600
# mc alias store under /root/.mc. No secret value is ever echoed; the summary
# prints KEY NAMES and OK/FAIL only. `set -x` is never enabled.
#
# WHAT IT DOES (each step is a no-op when already done):
#   1. preflight: root, staged binaries present, binary sha256 matches the
#      staged .sha256sum, the data dir exists and is on a mounted filesystem
#   2. system user `minio-user` (nologin), data dir owned by it
#   3. /usr/local/bin/{minio,mc} installed from the stage dir
#   4. /etc/default/minio (600): MINIO_VOLUMES, MINIO_OPTS, root credentials
#      (generated once with openssl rand; reused on every later run)
#   5. /etc/systemd/system/minio.service (upstream unit shape, Type=notify,
#      Restart=always so a late tailnet address at boot is retried)
#   6. enable + start, wait for /minio/health/live on the bind address
#   7. mc alias `zakbox1` (root creds, stored 600 under /root/.mc)
#   8. bucket: create, versioning ON (parity with the incumbent store), one
#      lifecycle rule expiring NONCURRENT versions after $NONCURRENT_EXPIRE_DAYS
#   9. fleet identity: a scoped policy (this bucket only) + a user whose
#      access/secret pair is the fleet's EXISTING scoped pair — because the
#      own-cloud backend builds its S3 and DynamoDB clients from ONE credential
#      session and DynamoDB stays on the incumbent cloud in Phase 1a
#      (runbook §13.5), the flip may change ONLY the endpoint, never the keys
#  10. verify: `mc admin info`, `mc ls` of the bucket, policy attached
#
# CREDENTIAL POLICY (decided 2026-09-11, g-372-07): the fleet user on this
# store IS the scoped pair every box already carries in MIND_AWS_*. That is
# not a shortcut — it is forced by owncloud_backend.py building both clients
# from one boto3 Session, and it adds no trust boundary: the storage host is
# the LXD host of every container and already owns their .env.local files.
#
# INPUTS (env):
#   MINIO_FLEET_ACCESS_KEY / MINIO_FLEET_SECRET_KEY   required unless --no-fleet-user
#   MINIO_BIND_ADDR      bind IPv4 (default: this host's tailnet IPv4 from `tailscale ip -4`)
#   MINIO_PORT           default 9000;  MINIO_CONSOLE_ADDR default 127.0.0.1:9001
#   MINIO_DATA_DIR       default /srv/objects
#   MINIO_BUCKET         default zds-own-cloud-data (same name as the incumbent
#                        bucket so STORAGE_S3_BUCKET never changes at the flip)
#   MINIO_STAGE_DIR      default /opt/minio-stage (holds minio, mc, minio.new.sha256sum)
#   NONCURRENT_EXPIRE_DAYS  default 7 (incumbent is 14; disk churn budget — see runbook)
#   MINIO_ALIAS          default zakbox1
# FLAGS: --dry-run (print the plan, change nothing)  --no-fleet-user
#
# EXIT: 0 ok; 1 preflight/verification failure; 2 usage.
set -euo pipefail
umask 077

DRY_RUN=0
FLEET_USER=1
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-fleet-user) FLEET_USER=0 ;;
        -h|--help) echo "usage: minio-standup.sh [--dry-run] [--no-fleet-user]   (full doc: the header comments of core/scripts/minio-standup.sh — under 'bash -s' \$0 is the shell, so they cannot be printed from here)"; exit 0 ;;
        *) echo "minio-standup: unknown arg '$1'" >&2; exit 2 ;;
    esac
    shift
done

: "${MINIO_PORT:=9000}"
: "${MINIO_CONSOLE_ADDR:=127.0.0.1:9001}"
: "${MINIO_DATA_DIR:=/srv/objects}"
: "${MINIO_BUCKET:=zds-own-cloud-data}"
: "${MINIO_STAGE_DIR:=/opt/minio-stage}"
: "${NONCURRENT_EXPIRE_DAYS:=7}"
: "${MINIO_ALIAS:=zakbox1}"
ENV_FILE=/etc/default/minio
UNIT_FILE=/etc/systemd/system/minio.service
POLICY_NAME=fleet-owncloud-rw

log()  { printf '[minio-standup] %s\n' "$*" >&2; }
die()  { printf '[minio-standup] FAIL: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" -eq 1 ]; then log "DRY-RUN would: $(printf '%q ' "$@")"; else "$@"; fi; }
CHANGED=0   # set when step 4 or 5 rewrites a file the running service already loaded

# ── 1. preflight ──────────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "must run as root on the storage host"
for b in minio mc; do
    [ -x "$MINIO_STAGE_DIR/$b" ] || [ -x "/usr/local/bin/$b" ] || die "$b binary not staged at $MINIO_STAGE_DIR and not installed"
done
if [ -f "$MINIO_STAGE_DIR/minio.new.sha256sum" ] && [ -x "$MINIO_STAGE_DIR/minio" ]; then
    want="$(awk '{print $1}' "$MINIO_STAGE_DIR/minio.new.sha256sum")"
    have="$(sha256sum "$MINIO_STAGE_DIR/minio" | awk '{print $1}')"
    [ "$want" = "$have" ] || die "staged minio sha256 does not match minio.new.sha256sum"
    log "preflight: staged minio sha256 verified (${have:0:12}…)"
fi
[ -d "$MINIO_DATA_DIR" ] || die "data dir $MINIO_DATA_DIR does not exist (disk layout not applied?)"
if ! findmnt -T "$MINIO_DATA_DIR" >/dev/null 2>&1; then
    die "data dir $MINIO_DATA_DIR is not on a mounted filesystem"
fi
mnt_src="$(findmnt -n -o SOURCE -T "$MINIO_DATA_DIR")"
root_src="$(findmnt -n -o SOURCE -T / || true)"
[ "$mnt_src" != "$root_src" ] || die "data dir $MINIO_DATA_DIR sits on the ROOT filesystem ($root_src), not the storage volume"
if [ -z "${MINIO_BIND_ADDR:-}" ]; then
    MINIO_BIND_ADDR="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    [ -n "$MINIO_BIND_ADDR" ] || die "MINIO_BIND_ADDR unset and no tailnet IPv4 found"
fi
ip -4 addr show 2>/dev/null | grep -q "inet ${MINIO_BIND_ADDR}/" || die "bind address $MINIO_BIND_ADDR is not assigned to any interface on this host"
if [ "$FLEET_USER" -eq 1 ]; then
    [ -n "${MINIO_FLEET_ACCESS_KEY:-}" ] && [ -n "${MINIO_FLEET_SECRET_KEY:-}" ] \
        || die "MINIO_FLEET_ACCESS_KEY / MINIO_FLEET_SECRET_KEY are required (or pass --no-fleet-user)"
    ak_len=${#MINIO_FLEET_ACCESS_KEY}; sk_len=${#MINIO_FLEET_SECRET_KEY}
    { [ "$ak_len" -ge 3 ] && [ "$ak_len" -le 20 ]; } || die "fleet access key length $ak_len outside MinIO's 3..20"
    { [ "$sk_len" -ge 8 ] && [ "$sk_len" -le 40 ]; } || die "fleet secret key length $sk_len outside MinIO's 8..40"
fi
ENDPOINT="http://${MINIO_BIND_ADDR}:${MINIO_PORT}"
log "preflight ok: endpoint=$ENDPOINT data=$MINIO_DATA_DIR (on $mnt_src) bucket=$MINIO_BUCKET"

# ── 2. user + data dir ────────────────────────────────────────────────────────
if ! id minio-user >/dev/null 2>&1; then
    run useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin --user-group minio-user
    log "created system user minio-user"
fi
run chown minio-user:minio-user "$MINIO_DATA_DIR"
run chmod 750 "$MINIO_DATA_DIR"

# ── 3. binaries ───────────────────────────────────────────────────────────────
for b in minio mc; do
    if [ -x "$MINIO_STAGE_DIR/$b" ]; then
        if [ ! -x "/usr/local/bin/$b" ] || ! cmp -s "$MINIO_STAGE_DIR/$b" "/usr/local/bin/$b"; then
            run install -o root -g root -m 0755 "$MINIO_STAGE_DIR/$b" "/usr/local/bin/$b"
            CHANGED=1
            log "installed /usr/local/bin/$b"
        fi
    fi
done

# ── 4. environment file (root credentials generated ONCE) ─────────────────────
if [ ! -f "$ENV_FILE" ]; then
    root_user="minio-root-$(openssl rand -hex 4)"
    root_pass="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40)"
    [ ${#root_pass} -ge 32 ] || die "root password generation produced fewer than 32 chars"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN would write $ENV_FILE (600) with generated root credentials"
    else
        {
            echo "# MinIO service environment — written by minio-standup.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)"
            echo "# Root credentials: keep this file 600. Fleet boxes never use these; they use the scoped fleet user."
            echo "MINIO_VOLUMES=\"$MINIO_DATA_DIR\""
            echo "MINIO_OPTS=\"--address ${MINIO_BIND_ADDR}:${MINIO_PORT} --console-address ${MINIO_CONSOLE_ADDR}\""
            echo "MINIO_ROOT_USER=\"$root_user\""
            echo "MINIO_ROOT_PASSWORD=\"$root_pass\""
        } > "$ENV_FILE"
        chmod 600 "$ENV_FILE"; chown root:root "$ENV_FILE"
        CHANGED=1
        log "wrote $ENV_FILE (600) — root credentials generated"
    fi
    unset root_pass
else
    log "$ENV_FILE present — reusing existing root credentials and options"
fi

# ── 5. systemd unit ───────────────────────────────────────────────────────────
unit_body="$(cat <<'UNIT'
[Unit]
Description=MinIO object store (asp-372 Phase 1 basement store)
Documentation=https://docs.min.io
Wants=network-online.target
After=network-online.target tailscaled.service
AssertFileIsExecutable=/usr/local/bin/minio

[Service]
Type=notify
WorkingDirectory=/usr/local
User=minio-user
Group=minio-user
ProtectProc=invisible
EnvironmentFile=-/etc/default/minio
ExecStartPre=/bin/bash -c "if [ -z \"${MINIO_VOLUMES}\" ]; then echo \"Variable MINIO_VOLUMES not set in /etc/default/minio\"; exit 1; fi"
ExecStart=/usr/local/bin/minio server $MINIO_OPTS $MINIO_VOLUMES
# A late tailnet address at boot makes the bind fail once; keep retrying.
Restart=always
RestartSec=5
LimitNOFILE=1048576
TasksMax=infinity
TimeoutSec=infinity
SendSIGKILL=no

[Install]
WantedBy=multi-user.target
UNIT
)"
if [ ! -f "$UNIT_FILE" ] || [ "$(cat "$UNIT_FILE")" != "$unit_body" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then log "DRY-RUN would write $UNIT_FILE"; else
        printf '%s\n' "$unit_body" > "$UNIT_FILE"; chmod 644 "$UNIT_FILE"
        systemctl daemon-reload; CHANGED=1
        log "wrote $UNIT_FILE + daemon-reload"
    fi
fi

# ── 6. enable + start + health ────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN would: systemctl enable --now minio; wait for $ENDPOINT/minio/health/live"
else
    systemctl enable minio >/dev/null 2>&1 || true
    if ! systemctl is-active --quiet minio; then systemctl start minio; else
        # unit or env file rewritten this run (CHANGED=1 from step 4/5)? restart so
        # the running process matches the files — daemon-reload alone reloads unit
        # definitions and never touches the live process (fresh-eyes review 2026-09-11).
        if [ "$CHANGED" -eq 1 ]; then systemctl daemon-reload; systemctl restart minio; log "restarted minio (unit/env changed)"; fi
    fi
    ok=0
    for i in $(seq 1 30); do
        if curl -sf -o /dev/null "$ENDPOINT/minio/health/live"; then ok=1; break; fi
        sleep 1
    done
    [ "$ok" -eq 1 ] || { systemctl status minio --no-pager | tail -15 >&2; die "minio did not answer /minio/health/live on $ENDPOINT within 30s"; }
    log "minio service active; health live on $ENDPOINT"
fi

# ── 7. mc alias (root) ────────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ] && [ ! -f "$ENV_FILE" ]; then
    log "DRY-RUN: $ENV_FILE not written yet, so steps 7-10 (alias, bucket, fleet user, verify) are only listed:"
    log "DRY-RUN would: mc alias set $MINIO_ALIAS $ENDPOINT <root>; mc mb $MINIO_ALIAS/$MINIO_BUCKET; version enable; ilm noncurrent-expire ${NONCURRENT_EXPIRE_DAYS}d; policy $POLICY_NAME + fleet user"
    printf '{"ok": true, "dry_run": true, "endpoint": "%s", "bucket": "%s"}\n' "$ENDPOINT" "$MINIO_BUCKET"
    exit 0
fi
# shellcheck disable=SC1090
set +u; . "$ENV_FILE" 2>/dev/null || true; set -u
[ -n "${MINIO_ROOT_USER:-}" ] && [ -n "${MINIO_ROOT_PASSWORD:-}" ] || die "root credentials not readable from $ENV_FILE"
if [ "$DRY_RUN" -eq 1 ]; then log "DRY-RUN would: mc alias set $MINIO_ALIAS $ENDPOINT <root>"; else
    export MC_CONFIG_DIR=/root/.mc
    # Access key on argv, secret on stdin: mc prompts for an omitted secret key.
    if ! printf '%s\n' "$MINIO_ROOT_PASSWORD" | mc alias set "$MINIO_ALIAS" "$ENDPOINT" "$MINIO_ROOT_USER" >/dev/null 2>&1; then
        mc alias set "$MINIO_ALIAS" "$ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    fi
    chmod 700 /root/.mc 2>/dev/null || true
    mc admin info "$MINIO_ALIAS" >/dev/null || die "mc admin info failed against $ENDPOINT with the root credentials"
    log "mc alias $MINIO_ALIAS -> $ENDPOINT ok (root)"
fi

# ── 8. bucket + versioning + lifecycle ────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN would: mc mb $MINIO_ALIAS/$MINIO_BUCKET; versioning enable; ilm noncurrent-expire ${NONCURRENT_EXPIRE_DAYS}d"
else
    mc mb --ignore-existing "$MINIO_ALIAS/$MINIO_BUCKET" >/dev/null
    if ! mc version info "$MINIO_ALIAS/$MINIO_BUCKET" 2>/dev/null | grep -qi enabled; then
        mc version enable "$MINIO_ALIAS/$MINIO_BUCKET" >/dev/null
        log "versioning enabled on $MINIO_BUCKET"
    fi
    if ! mc ilm rule ls "$MINIO_ALIAS/$MINIO_BUCKET" --json 2>/dev/null | grep -qE "NoncurrentDays|noncurrent"; then
        mc ilm rule add --noncurrent-expire-days "$NONCURRENT_EXPIRE_DAYS" "$MINIO_ALIAS/$MINIO_BUCKET" >/dev/null
        log "lifecycle: noncurrent versions expire after ${NONCURRENT_EXPIRE_DAYS}d"
    fi
    log "bucket $MINIO_BUCKET ready (versioning on)"
fi

# ── 9. fleet identity: scoped policy + user ───────────────────────────────────
if [ "$FLEET_USER" -eq 1 ]; then
    policy_file="$(mktemp)"
    cat > "$policy_file" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads", "s3:ListBucketVersions", "s3:GetBucketVersioning"],
     "Resource": ["arn:aws:s3:::${MINIO_BUCKET}"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:GetObjectTagging", "s3:PutObjectTagging"],
     "Resource": ["arn:aws:s3:::${MINIO_BUCKET}/*"]}
  ]
}
POLICY
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN would: mc admin policy create $POLICY_NAME; mc admin user add <fleet key>; attach"
    else
        # `policy create` is create-or-update on current mc; tolerate an older mc
        # that refuses an existing name by confirming the policy is present.
        mc admin policy create "$MINIO_ALIAS" "$POLICY_NAME" "$policy_file" >/dev/null 2>&1 \
            || mc admin policy info "$MINIO_ALIAS" "$POLICY_NAME" >/dev/null \
            || die "could not create or find policy $POLICY_NAME"
        if ! mc admin user info "$MINIO_ALIAS" "$MINIO_FLEET_ACCESS_KEY" >/dev/null 2>&1; then
            # Both values on stdin when mc accepts it; argv fallback (root-only visibility on this host).
            if ! printf '%s\n%s\n' "$MINIO_FLEET_ACCESS_KEY" "$MINIO_FLEET_SECRET_KEY" | mc admin user add "$MINIO_ALIAS" >/dev/null 2>&1; then
                mc admin user add "$MINIO_ALIAS" "$MINIO_FLEET_ACCESS_KEY" "$MINIO_FLEET_SECRET_KEY" >/dev/null
            fi
            log "fleet user created (access key ${MINIO_FLEET_ACCESS_KEY:0:4}…)"
        fi
        mc admin policy attach "$MINIO_ALIAS" "$POLICY_NAME" --user "$MINIO_FLEET_ACCESS_KEY" >/dev/null 2>&1 || true
        mc admin user info "$MINIO_ALIAS" "$MINIO_FLEET_ACCESS_KEY" | grep -q "$POLICY_NAME" || die "policy $POLICY_NAME not attached to the fleet user"
        log "fleet user has policy $POLICY_NAME (bucket $MINIO_BUCKET only)"
    fi
    rm -f "$policy_file"
fi

# ── 10. verify ────────────────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 0 ]; then
    mc admin info "$MINIO_ALIAS" --json >/dev/null 2>&1 || die "mc admin info failed at verify"
    mc ls "$MINIO_ALIAS/$MINIO_BUCKET" >/dev/null || die "mc ls $MINIO_BUCKET failed at verify"
    ver="$(minio --version 2>/dev/null | head -1 | awk '{print $3}')"
    printf '{"ok": true, "endpoint": "%s", "bucket": "%s", "data_dir": "%s", "minio_version": "%s", "versioning": "enabled", "noncurrent_expire_days": %s, "fleet_user": %s, "policy": "%s"}\n' \
        "$ENDPOINT" "$MINIO_BUCKET" "$MINIO_DATA_DIR" "$ver" "$NONCURRENT_EXPIRE_DAYS" "$FLEET_USER" "$POLICY_NAME"
else
    printf '{"ok": true, "dry_run": true, "endpoint": "%s", "bucket": "%s"}\n' "$ENDPOINT" "$MINIO_BUCKET"
fi
