#!/usr/bin/env bash
# domain-leak-exempt: fleet-github-provisioning recipe. The GitHub API host, the
# zkysar1/Ayoai-Mind repo slug, the /root/.ssh deploy-key paths, and the MIND_*
# vault-key surface are FUNCTIONAL domain tokens here (the script registers a
# named repo's deploy key and writes named ssh identity files) — not pedagogical
# examples. Sanctioned marker use per domain-free-examples.md "Marker Restriction"
# (executable code with functional domain strings) and the
# domain-recipe-seed-purity.md decision, exactly as its sibling
# provision-from-vault.sh. Genericizing operator bring-up code would harm the
# operator who runs it.
#
# provision-github-from-vault.sh — self-service a fleet container's GitHub WRITE
# deploy key from the shared bootstrap vault, so a container can push to the fleet
# repo WITHOUT any agent personally holding repo-admin. Companion to
# provision-from-vault.sh (same bootstrap-key gate, same values-blind hygiene);
# the GitHub lane of the mechanism formalized in
# core/config/conventions/fleet-secret-provisioning.md.
#
# WHAT IT DOES (per agent, at container bring-up):
#   1. Read a dedicated repo-scoped GitHub admin token from the vault IN-MEMORY
#      (same vault-reach path as provision-from-vault.sh; token never on disk),
#      vault entry <PREFIX>_FLEET_GH_DEPLOYKEY_ADMIN_TOKEN.
#   2. Generate the agent's own ed25519 keypair at <SSH_KEY_DIR>/<agent>_deploy
#      (private key mode 600) if absent.
#   3. SELF-REGISTER the pubkey as a WRITE deploy key via the GitHub REST API
#      (raw HTTPS; no gh install needed). Idempotent: GET /keys first, skip when
#      our key MATERIAL is already registered. GitHub's read_only flag is
#      authoritative for write-status — never a key-name heuristic (guard-133).
#   4. Ensure <SSH_KEY_DIR>/config routes Host github.com -> the deploy key.
#   5. Verify VALUES-BLIND: print the deploy-key title + OK/registered, NEVER the
#      token, NEVER the private key.
#
# DORMANT-BUT-READY: if the vault has no deploy-key token entry, this is a clear
# skip + exit 0 (bring-up never breaks). The production PAT is minted + seeded
# ZDS-side (omni + Zachary, human-only); this provisioner ships ready and
# activates the moment the vault entry appears.
#
# SECRETS HYGIENE (secrets.md + guard-724): token read into a shell var only;
# passed to curl via --config stdin so it never lands in argv (/proc) or on disk;
# no value ever echoed; set -x never enabled.
#
# ── Usage ─────────────────────────────────────────────────────────────────────
#   provision-github-from-vault.sh --agent <name> [--force] [--dry-run]
#     --agent NAME  the hosted agent (default: $MIND_AGENT). Names the keypair,
#                   the deploy-key title, and the ssh-config identity.
#     --force       re-register even if a matching key exists (GitHub still
#                   rejects true duplicates; --force just re-attempts the POST).
#     --dry-run     do everything EXCEPT the registering POST + ssh-config write.
#
# ── Config (env; defaults suit ENVIRONMENT_ID=ayoai-mind) ──────────────────────
#   BOOTSTRAP_KEY_PATH   bootstrap SSH key (default /root/.ssh/efs-master-key.pem)
#   VAULT_SSH_HOST       operator host fronting the vault              (REQUIRED*)
#   VAULT_SSH_USER       SSH user on the operator host (default ec2-user)
#   VAULT_REMOTE_PATH    vault file path on the operator host          (REQUIRED*)
#   VAULT_KEY_PREFIX     vault key prefix (default: uppercased 1st seg of ENV_ID)
#   GH_REPO              owner/repo to register the deploy key on
#                        (default zkysar1/Ayoai-Mind)
#   GH_DEPLOYKEY_VAULT_KEY  vault key holding the admin token
#                        (default <PREFIX>_FLEET_GH_DEPLOYKEY_ADMIN_TOKEN)
#   GITHUB_API_BASE      API base (default https://api.github.com)
#   SSH_KEY_DIR          deploy-key + ssh config dir (default /root/.ssh)
#   ENVIRONMENT_ID       environment id (default ayoai-mind)
#   (* REQUIRED unless PROVISION_GH_VAULT_FILE is set — the test seam below)
#
# ── Test seams (UNSET in production) ───────────────────────────────────────────
#   PROVISION_GH_VAULT_FILE   read the vault from this LOCAL file, not over SSH.
#   PROVISION_GH_MOCK_STATE   use this LOCAL json file as a fake GitHub keys store
#                             (GET reads it, POST appends) instead of curl.
#   Both let the acceptance test run hermetically (no SSH, no network). The mock
#   store NEVER receives the token (that stays in the curl --config stdin path).
#
# Exits non-zero on any hard failure; exit 0 on the dormant-vault skip.

set -euo pipefail

# ── Args ───────────────────────────────────────────────────────────────────────
AGENT="${MIND_AGENT:-}"
FORCE=0
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --agent)   AGENT="${2:-}"; shift ;;
        --force)   FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "provision-github-from-vault: unknown arg '$1'" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$AGENT" ]; then
    echo "provision-github-from-vault: --agent NAME is required (or set MIND_AGENT)." >&2
    exit 2
fi

# ── Resolve defaults ───────────────────────────────────────────────────────────
: "${BOOTSTRAP_KEY_PATH:=/root/.ssh/efs-master-key.pem}"
: "${VAULT_SSH_USER:=ec2-user}"
: "${ENVIRONMENT_ID:=ayoai-mind}"
: "${GH_REPO:=zkysar1/Ayoai-Mind}"
: "${GITHUB_API_BASE:=https://api.github.com}"
: "${SSH_KEY_DIR:=/root/.ssh}"
if [ -z "${VAULT_KEY_PREFIX:-}" ]; then
    VAULT_KEY_PREFIX="$(printf '%s' "$ENVIRONMENT_ID" | cut -d- -f1 | tr '[:lower:]' '[:upper:]')"
fi
: "${GH_DEPLOYKEY_VAULT_KEY:=${VAULT_KEY_PREFIX}_FLEET_GH_DEPLOYKEY_ADMIN_TOKEN}"

KEY_PATH="$SSH_KEY_DIR/${AGENT}_deploy"
PUB_PATH="$KEY_PATH.pub"
SSH_CONFIG_PATH="$SSH_KEY_DIR/config"
DEPLOY_TITLE="fleet-deploy-${AGENT}"

# ── Reach the vault + read the ONE token into memory ───────────────────────────
# Reuses provision-from-vault.sh's vault-reach shape (same bootstrap key, same
# canonical SSH flags for the cold-node case where the world/ wrapper is absent).
# The token is captured into a variable and never touches disk.
if [ -n "${PROVISION_GH_VAULT_FILE:-}" ]; then
    # Test seam: read a local vault file instead of SSH.
    [ -f "$PROVISION_GH_VAULT_FILE" ] || { echo "provision-github-from-vault: PROVISION_GH_VAULT_FILE not found: $PROVISION_GH_VAULT_FILE" >&2; exit 1; }
    VAULT_CONTENT="$(cat "$PROVISION_GH_VAULT_FILE")"
else
    if [ ! -f "$BOOTSTRAP_KEY_PATH" ]; then
        echo "provision-github-from-vault: bootstrap key not found at $BOOTSTRAP_KEY_PATH" >&2
        echo "  (the same single pre-seeded secret provision-from-vault.sh uses)" >&2
        exit 1
    fi
    if [ -z "${VAULT_SSH_HOST:-}" ] || [ -z "${VAULT_REMOTE_PATH:-}" ]; then
        echo "provision-github-from-vault: VAULT_SSH_HOST and VAULT_REMOTE_PATH are required." >&2
        exit 2
    fi
    echo "provision-github-from-vault: reading vault from ${VAULT_SSH_USER}@${VAULT_SSH_HOST}:${VAULT_REMOTE_PATH} ..." >&2
    VAULT_CONTENT="$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=20 \
        -i "$BOOTSTRAP_KEY_PATH" "${VAULT_SSH_USER}@${VAULT_SSH_HOST}" \
        "cat '$VAULT_REMOTE_PATH'")" || {
        echo "provision-github-from-vault: failed to read vault over SSH (check bootstrap key, host, path)" >&2
        exit 1
    }
fi

# Extract exactly GH_DEPLOYKEY_VAULT_KEY's value (in-memory; never echoed).
GH_TOKEN=""
while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in
        "${GH_DEPLOYKEY_VAULT_KEY}"=*) GH_TOKEN="${line#*=}" ;;
    esac
done <<< "$VAULT_CONTENT"

# ── DORMANT-BUT-READY: no token entry -> clear skip, exit 0 ─────────────────────
if [ -z "$GH_TOKEN" ]; then
    echo "provision-github-from-vault: vault has no '${GH_DEPLOYKEY_VAULT_KEY}' entry —" >&2
    echo "  GitHub write-access provisioning is DORMANT-BUT-READY; skipping (exit 0)." >&2
    echo "  Seed that vault entry (ZDS-side, human-only) to activate." >&2
    exit 0
fi

# ── Generate the agent's ed25519 keypair if absent (private key mode 600) ──────
mkdir -p "$SSH_KEY_DIR"
chmod 700 "$SSH_KEY_DIR" 2>/dev/null || true
if [ ! -f "$KEY_PATH" ]; then
    HOST_TAG="$(hostname -s 2>/dev/null || echo container)"
    echo "provision-github-from-vault: generating ed25519 deploy keypair at $KEY_PATH" >&2
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "${AGENT}@${HOST_TAG}-deploy" >/dev/null
    chmod 600 "$KEY_PATH" 2>/dev/null || true
else
    echo "provision-github-from-vault: deploy keypair already present at $KEY_PATH (reusing)" >&2
fi

if [ ! -f "$PUB_PATH" ]; then
    echo "provision-github-from-vault: pubkey missing at $PUB_PATH after keygen — aborting" >&2
    exit 1
fi
# GitHub stores the key material as "<type> <base64>" (no comment). Match on that.
PUB_MATERIAL="$(cut -d' ' -f1,2 "$PUB_PATH")"

# ── GitHub API indirection (real curl, or the file-backed mock test seam) ──────
_gh_api() {
    # _gh_api METHOD PATH_SUFFIX [BODY_JSON]  -> prints response JSON on stdout.
    local method="$1" path="$2" body="${3:-}"
    if [ -n "${PROVISION_GH_MOCK_STATE:-}" ]; then
        local state="$PROVISION_GH_MOCK_STATE"
        [ -f "$state" ] || printf '[]' > "$state"
        if [ "$method" = "GET" ]; then
            cat "$state"
        else
            # Append the posted key object (fake id) to the array. The mock NEVER
            # sees the token — only the non-secret {title,key,read_only} body.
            python3 - "$state" "$body" <<'PY'
import json, sys
state_path, body = sys.argv[1], sys.argv[2]
arr = json.load(open(state_path, encoding="utf-8"))
obj = json.loads(body)
obj.setdefault("id", len(arr) + 1)
arr.append(obj)
json.dump(arr, open(state_path, "w", encoding="utf-8"))
print(json.dumps(obj))
PY
        fi
        return 0
    fi
    # Real path: token via --config stdin so it never enters argv/proc.
    local url="${GITHUB_API_BASE}${path}"
    local cfg
    cfg="$(printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github+json"\nheader = "X-GitHub-Api-Version: 2022-11-28"\n' "$GH_TOKEN")"
    if [ "$method" = "GET" ]; then
        printf '%s' "$cfg" | curl -sS --config - "$url"
    else
        printf '%s' "$cfg" | curl -sS --config - -X "$method" -d "$body" "$url"
    fi
}

# Classify our key material against the live keys list. read_only is authoritative
# for write-status (guard-133) — NEVER inferred from the title.
# NOTE: the keys JSON arrives on STDIN (the pipe), so the python PROGRAM must come
# from -c (a single-quoted literal — guard-165: no bash interpolation), NOT from a
# `python3 - <<'PY'` heredoc, which would claim stdin for the program and starve
# json.load(sys.stdin). Data crosses only via argv ($1) + stdin.
_key_status() {
    # stdin: keys JSON array. argv1: our pubkey material. prints write|readonly|absent.
    python3 -c '
import json, sys
material = sys.argv[1].strip()
try:
    data = json.load(sys.stdin)
except Exception:
    data = []
# GitHub returns a LIST on success but an OBJECT ({"message":...}) on
# 401/403/404. Guard: iterating a dict yields its string keys, and str.get()
# would AttributeError. A non-list response means "could not read keys" -> emit
# a distinct token the caller surfaces, NOT a crash and NOT a false "absent".
if not isinstance(data, list):
    print("error")
    sys.exit(0)
for k in data:
    if isinstance(k, dict) and str(k.get("key", "")).strip() == material:
        print("write" if not k.get("read_only", True) else "readonly")
        break
else:
    print("absent")
' "$1"
}

# ── Register the WRITE deploy key (idempotent) ─────────────────────────────────
KEYS_JSON="$(_gh_api GET "/repos/${GH_REPO}/keys" || true)"
STATUS="$(printf '%s' "$KEYS_JSON" | _key_status "$PUB_MATERIAL")"

REGISTERED_STATE="unknown"
case "$STATUS" in
    write)
        echo "provision-github-from-vault: deploy key already registered as WRITE (read_only=false) — skip" >&2
        REGISTERED_STATE="already-write"
        ;;
    readonly)
        echo "provision-github-from-vault: WARN — this key material is registered READ-ONLY on ${GH_REPO}." >&2
        echo "  GitHub rejects re-adding identical key material, and deleting a deploy key is a" >&2
        echo "  destructive op left to the operator (archive-before-delete). Remove the read-only" >&2
        echo "  entry in GitHub, then re-run — or provision a fresh keypair (delete $KEY_PATH first)." >&2
        REGISTERED_STATE="readonly-conflict"
        ;;
    absent)
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "provision-github-from-vault: --dry-run — would POST WRITE deploy key '${DEPLOY_TITLE}' to ${GH_REPO}" >&2
            REGISTERED_STATE="dry-run-would-register"
        else
            BODY="$(python3 - "$DEPLOY_TITLE" "$PUB_MATERIAL" <<'PY'
import json, sys
print(json.dumps({"title": sys.argv[1], "key": sys.argv[2], "read_only": False}))
PY
)"
            RESP="$(_gh_api POST "/repos/${GH_REPO}/keys" "$BODY" || true)"
            NEW_STATUS="$(printf '%s' "[$RESP]" | _key_status "$PUB_MATERIAL")"
            if [ "$NEW_STATUS" = "write" ]; then
                echo "provision-github-from-vault: registered WRITE deploy key '${DEPLOY_TITLE}' on ${GH_REPO}" >&2
                REGISTERED_STATE="registered-write"
            else
                echo "provision-github-from-vault: registration did NOT return a write key (got '${NEW_STATUS}') — aborting" >&2
                exit 1
            fi
        fi
        ;;
    error)
        echo "provision-github-from-vault: could not read existing deploy keys from ${GH_REPO} —" >&2
        echo "  the GitHub API returned a non-list response (likely an auth/permission error)." >&2
        echo "  Response (values-blind — a keys-list response never carries the token):" >&2
        printf '%s' "$KEYS_JSON" | head -c 400 | sed 's/^/    /' >&2
        echo "" >&2
        echo "  Check the ${GH_DEPLOYKEY_VAULT_KEY} token scope (needs Administration:read/write on ${GH_REPO})." >&2
        exit 1
        ;;
    *)
        echo "provision-github-from-vault: could not classify key status (got '${STATUS}') — aborting" >&2
        exit 1
        ;;
esac

# ── Ensure ssh-config routes github.com through this deploy key ─────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    echo "provision-github-from-vault: --dry-run — would ensure ssh-config Host github.com -> $KEY_PATH" >&2
else
    touch "$SSH_CONFIG_PATH"
    chmod 600 "$SSH_CONFIG_PATH" 2>/dev/null || true
    # Idempotent: only append the block if this exact IdentityFile is not present.
    if ! grep -qF "IdentityFile $KEY_PATH" "$SSH_CONFIG_PATH" 2>/dev/null; then
        {
            printf '\n# fleet-deploy: %s (provision-github-from-vault.sh)\n' "$AGENT"
            printf 'Host github.com\n'
            printf '    IdentityFile %s\n' "$KEY_PATH"
            printf '    IdentitiesOnly yes\n'
            printf '    StrictHostKeyChecking accept-new\n'
        } >> "$SSH_CONFIG_PATH"
        echo "provision-github-from-vault: ssh-config Host github.com -> $KEY_PATH added" >&2
    else
        echo "provision-github-from-vault: ssh-config already routes github.com -> $KEY_PATH (no change)" >&2
    fi
fi

# ── Verify (VALUES-BLIND): title + registration state only, never the token ────
echo "provision-github-from-vault: verify (values-blind — no token, no private key):" >&2
printf '    agent            %s\n' "$AGENT" >&2
printf '    repo             %s\n' "$GH_REPO" >&2
printf '    deploy-key-title %s\n' "$DEPLOY_TITLE" >&2
printf '    deploy-key-path  %s\n' "$KEY_PATH" >&2
printf '    registration     %s\n' "$REGISTERED_STATE" >&2

# Non-zero only on the read-only conflict (a real misconfiguration needing action).
[ "$REGISTERED_STATE" != "readonly-conflict" ]
