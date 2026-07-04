#!/usr/bin/env bash
# domain-leak-exempt: fleet-provisioning recipe. The EFS/own-cloud vault path, the
# operator SSH host, and the MIND_AWS_*/STORAGE_* env-key surface are FUNCTIONAL
# domain tokens here (the script SSHes to a named operator box and writes named
# daemon env vars) — not pedagogical examples. This is the sanctioned marker use
# per domain-free-examples.md "Marker Restriction" (executable code with functional
# domain strings) and the domain-recipe-seed-purity.md decision (domain-touching
# provisioning code lives in core/ under the marker and travels in the seed).
# Genericizing operator-facing bring-up code would harm the operator who runs it.
#
# provision-from-vault.sh — self-service a fleet container's .env.local from a
# single bootstrap secret. This is the executable instantiation of the pattern
# formalized in core/config/conventions/fleet-secret-provisioning.md
# (bootstrap-key -> remote vault -> self-serviced credential set).
#
# ── VALIDATION STATUS ──────────────────────────────────────────────────────────
# The PATTERN was validated 2026-07-02 on a fleet container (cc-02): one bootstrap
# EFS master key -> a fully self-serviced .env.local. THIS file is the re-encoding
# per current framework conventions (guard-97/98 dev-origination): it DEFINES the
# vault-file contract (see "Vault contract" below) and makes every deployment
# specific configurable via env. Before FIRST production use on a fresh container,
# run a live smoke test on an EFS-reachable box (`--dry-run` first, then confirm
# the verify block reports OK for every required key). Do NOT run blind against a
# vault whose format has not been confirmed to match the contract.
#
# ── SECRETS HYGIENE (non-negotiable; secrets.md + guard-724) ────────────────────
#   - The vault is read into a shell variable ONLY. Never a temp file, never disk.
#   - No secret value is ever echoed. The verify block prints KEY NAMES + OK/EMPTY.
#   - .env.local is created mode 600 (umask 077) in one write.
#   - `set -x` is NEVER enabled (it would echo values).
#
# ── Vault contract ──────────────────────────────────────────────────────────────
#   The vault is a flat `KEY=VALUE` file (one per line; blank lines and `#`
#   comments ignored). Every entry destined for the container env is prefixed with
#   `${VAULT_KEY_PREFIX}_`. The provisioner strips exactly that prefix to derive the
#   container env name, e.g. with VAULT_KEY_PREFIX=AYOAI:
#       MIND_MIND_AWS_ACCESS_KEY_ID=...  ->  MIND_AWS_ACCESS_KEY_ID=...
#       MIND_STORAGE_BACKEND=own-cloud   ->  STORAGE_BACKEND=own-cloud
#   Entries NOT carrying the prefix are ignored (they are not for this environment).
#   The prefix namespaces one vault across many environments without collision.
#
# ── Usage ───────────────────────────────────────────────────────────────────────
#   provision-from-vault.sh [--force] [--dry-run] [--out <path>]
#     --force    overwrite an already-provisioned .env.local (FROM-state guard off)
#     --dry-run  read + map + verify, but do NOT write .env.local
#     --out P    target path (default: <repo-root>/.env.local)
#
# ── Configuration (env vars; defaults suit ENVIRONMENT_ID=ayoai-mind) ───────────
#   BOOTSTRAP_KEY_PATH  bootstrap SSH key (default /root/.ssh/efs-master-key.pem)
#   VAULT_SSH_HOST      operator host fronting the vault                 (REQUIRED)
#   VAULT_SSH_USER      SSH user on the operator host   (default ec2-user)
#   VAULT_REMOTE_PATH   path to the vault file on the operator host      (REQUIRED)
#   ENVIRONMENT_ID      environment id (default ayoai-mind)
#   VAULT_KEY_PREFIX    vault key prefix to strip (default: uppercased first
#                       segment of ENVIRONMENT_ID, e.g. ayoai-mind -> AYOAI)
#
# Exits non-zero on any failure. Idempotent: re-running against an already-
# provisioned node is a safe no-op unless --force.

set -euo pipefail

# ── Args ────────────────────────────────────────────────────────────────────────
FORCE=0
DRY_RUN=0
OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force)   FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --out)     OUT="${2:-}"; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "provision-from-vault: unknown arg '$1'" >&2; exit 2 ;;
    esac
    shift
done

# ── Resolve repo root + defaults ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
: "${OUT:=$REPO_ROOT/.env.local}"
: "${BOOTSTRAP_KEY_PATH:=/root/.ssh/efs-master-key.pem}"
: "${VAULT_SSH_USER:=ec2-user}"
: "${ENVIRONMENT_ID:=ayoai-mind}"
# Derive the default vault key prefix from the first segment of the env id.
if [ -z "${VAULT_KEY_PREFIX:-}" ]; then
    VAULT_KEY_PREFIX="$(printf '%s' "$ENVIRONMENT_ID" | cut -d- -f1 | tr '[:lower:]' '[:upper:]')"
fi

# ── FROM-state guard (invariant 6) ──────────────────────────────────────────────
if [ ! -f "$BOOTSTRAP_KEY_PATH" ]; then
    echo "provision-from-vault: bootstrap key not found at $BOOTSTRAP_KEY_PATH" >&2
    echo "  (this is the ONLY pre-seeded secret; provisioning cannot proceed without it)" >&2
    exit 1
fi
if [ -z "${VAULT_SSH_HOST:-}" ] || [ -z "${VAULT_REMOTE_PATH:-}" ]; then
    echo "provision-from-vault: VAULT_SSH_HOST and VAULT_REMOTE_PATH are required." >&2
    echo "  Set them to the operator host and the vault file path on it." >&2
    exit 2
fi
if [ -f "$OUT" ] && grep -q '^MIND_AWS_ACCESS_KEY_ID=..*' "$OUT" 2>/dev/null && [ "$FORCE" -eq 0 ]; then
    echo "provision-from-vault: $OUT already provisioned (MIND_AWS_ACCESS_KEY_ID present)." >&2
    echo "  Re-run with --force to overwrite. (idempotent no-op by default)" >&2
    exit 0
fi

# ── Reach the vault + read into memory (mechanism steps 2-3) ────────────────────
# Bootstrap context: the world/ domain wrapper (efs-ssh.sh) is NOT yet present on a
# cold node, so the canonical flags are inlined here (StrictHostKeyChecking=no,
# UserKnownHostsFile=/dev/null) exactly as the validated reference used them. The
# vault content is captured into a variable and never touches disk.
echo "provision-from-vault: reading vault from ${VAULT_SSH_USER}@${VAULT_SSH_HOST}:${VAULT_REMOTE_PATH} ..." >&2
VAULT_CONTENT="$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes -o ConnectTimeout=20 \
    -i "$BOOTSTRAP_KEY_PATH" "${VAULT_SSH_USER}@${VAULT_SSH_HOST}" \
    "cat '$VAULT_REMOTE_PATH'")" || {
    echo "provision-from-vault: failed to read vault over SSH (check bootstrap key, host, path)" >&2
    exit 1
}
if [ -z "$VAULT_CONTENT" ]; then
    echo "provision-from-vault: vault file is empty or unreadable at $VAULT_REMOTE_PATH" >&2
    exit 1
fi

# ── Map (mechanism step 4): strip ${VAULT_KEY_PREFIX}_ from each entry ───────────
# Built as a newline-joined string of `CONTAINER_KEY=value` lines. Values are held
# only in ENV_BODY / the loop; nothing is echoed.
ENV_BODY=""
WRITTEN_KEYS=""
prefix="${VAULT_KEY_PREFIX}_"
while IFS= read -r line; do
    # skip blanks and comments
    case "$line" in ''|\#*) continue ;; esac
    # must be KEY=... and carry the environment prefix
    case "$line" in
        "${prefix}"*=*) : ;;
        *) continue ;;
    esac
    vault_key="${line%%=*}"
    value="${line#*=}"
    container_key="${vault_key#"$prefix"}"
    # guard against a malformed empty container key
    [ -n "$container_key" ] || continue
    ENV_BODY+="${container_key}=${value}"$'\n'
    WRITTEN_KEYS+="${container_key}"$'\n'
done <<< "$VAULT_CONTENT"

if [ -z "$ENV_BODY" ]; then
    echo "provision-from-vault: no vault entries matched prefix '${prefix}' — check VAULT_KEY_PREFIX (currently '$VAULT_KEY_PREFIX')." >&2
    exit 1
fi

# ── Write (mechanism step 4): mode 600, single write ────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    echo "provision-from-vault: --dry-run — NOT writing $OUT" >&2
else
    ( umask 077; printf '# self-serviced by provision-from-vault.sh (mode 600, values from vault)\n%s' "$ENV_BODY" > "$OUT" )
    chmod 600 "$OUT" 2>/dev/null || true
    echo "provision-from-vault: wrote $OUT (mode 600)" >&2
fi

# ── Verify (mechanism step 5): VALUES-BLIND — key names + OK/EMPTY only ──────────
echo "provision-from-vault: verify (key names + presence only; NO values printed):" >&2
n_ok=0; n_empty=0
while IFS= read -r k; do
    [ -n "$k" ] || continue
    # re-derive presence from ENV_BODY without printing the value
    v_line="$(printf '%s' "$ENV_BODY" | grep -m1 "^${k}=" || true)"
    v="${v_line#*=}"
    if [ -n "$v" ]; then
        printf '    %-32s [OK]\n' "$k" >&2; n_ok=$((n_ok+1))
    else
        printf '    %-32s [EMPTY]\n' "$k" >&2; n_empty=$((n_empty+1))
    fi
done <<< "$WRITTEN_KEYS"
echo "provision-from-vault: ${n_ok} key(s) OK, ${n_empty} EMPTY." >&2

# Non-zero if any provisioned key came through empty (a partial vault is a failure).
[ "$n_empty" -eq 0 ]
