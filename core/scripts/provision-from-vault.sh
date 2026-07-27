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
#   PER-AGENT SCOPE (g-335-239). The prefix namespaces by ENVIRONMENT, and
#   ENVIRONMENT_ID is per-DEPLOYMENT — so every agent on a deployment resolves the
#   same prefix and the same entries. Right for a shared daemon credential, wrong
#   for a per-agent one. A key may therefore carry an agent scope:
#       MIND_LODESTAR_CONTRIBUTE_KEY__BRAVO=...  ->  LODESTAR_CONTRIBUTE_KEY=...
#   resolving ONLY on that agent's box (MIND_AGENT, uppercased). A scope for a
#   DIFFERENT agent is never written here; a scope for THIS agent overrides its
#   generic sibling; an unscoped key applies to everyone exactly as before. `__`
#   is reserved as the separator, so a container env name must not contain it.
#   Backward-compatible: a vault with no scoped entries maps identically to before.
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
# g-335-253: REPO_ROOT was "$SCRIPT_DIR/.." — but SCRIPT_DIR is core/scripts, so that
# is CORE_ROOT, and OUT defaulted to <repo>/core/.env.local, where nothing reads it.
# A fresh container provisioning with default flags would have written its entire
# credential set to a dead path and still exited 0 with a clean verify block — silent
# failure on the exact bootstrap path this script exists to serve. Latent only because
# no fresh-container run had happened yet (see VALIDATION STATUS above).
# The name was the trap: _paths.sh:18-22 binds this SAME expression to CORE_ROOT and
# defines REPO_ROOT as an alias for PROJECT_ROOT, so "REPO_ROOT" already means repo
# root everywhere else in this codebase. Two parents, not one, to match that meaning.
# (This script deliberately does NOT source _paths.sh — it must run standalone on a
# fresh container before the framework is usable — so the derivation is local by
# necessity and has to be kept correct by hand.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # core/scripts
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"                    # core/
REPO_ROOT="$(cd "$CORE_ROOT/.." && pwd)"                     # repo root
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
# TEST SEAM (g-115-3180). VAULT_SSH_BIN overrides the ssh binary; unset in
# production, so the default below is the only path real callers take. This seam
# exists because the obvious stubbing technique DOES NOT WORK on Git Bash: tests
# that write a stub `ssh` into a temp dir and prepend it to $PATH are silently
# ignored, because MSYS bash prepends its OWN /mingw64/bin:/usr/bin ahead of the
# inherited Windows PATH at startup. Measured 2026-07-26: with the stub dir
# prepended, `command -v ssh` still resolved to /usr/bin/ssh and the stub sat at
# PATH position 4. The real OpenSSH then ran and failed with "Could not resolve
# hostname", which reads like a product bug and is not one. Matches the seam
# pattern already used by test_provision_github_from_vault.py.
echo "provision-from-vault: reading vault from ${VAULT_SSH_USER}@${VAULT_SSH_HOST}:${VAULT_REMOTE_PATH} ..." >&2
VAULT_CONTENT="$("${VAULT_SSH_BIN:-ssh}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
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
#
# PER-AGENT SCOPING (g-335-239). The prefix namespaces the vault by ENVIRONMENT,
# and ENVIRONMENT_ID is per-DEPLOYMENT, not per-agent — every agent on a
# deployment resolves the same VAULT_KEY_PREFIX and therefore the same entries.
# That is correct for a shared daemon credential and WRONG for a per-agent one
# (e.g. a per-agent service contribute key), where two agents must not receive
# the same value. A vault key may therefore carry an agent scope:
#     ${PREFIX}_<CONTAINER_KEY>__<AGENT>=value      # AGENT uppercased
# resolving to <CONTAINER_KEY> on that agent's box only. Rules:
#   1. An agent-scoped entry for THIS agent WINS over its generic sibling.
#   2. An agent-scoped entry for ANOTHER agent is never written here. This is
#      the security-critical rule — a shared vault must not leak agent A's
#      credential onto agent B's box.
#   3. An entry with no scope is generic and applies to every agent (unchanged
#      behavior — a vault with no scoped entries maps exactly as it did before,
#      so this extension is backward-compatible).
#   4. `__` is RESERVED as the scope separator, so a container env name must not
#      contain it. No key in the current surface does (MIND_AWS_*, STORAGE_*).
# Two passes, because single-pass last-wins would make scoped-vs-generic precedence
# depend on line order in the vault — ordering is not part of the contract and must
# not decide which credential a box receives. The guarantee covers scope CLASSES
# only: a base name duplicated at the SAME scope is emitted twice (last wins on
# load, counted twice in verify) — an operator error this does not detect.
ENV_BODY=""
WRITTEN_KEYS=""
SCOPED_KEYS=""
FOREIGN_SCOPED_KEYS=""
UNKNOWN_SCOPE_KEYS=""
prefix="${VAULT_KEY_PREFIX}_"
# Unset MIND_AGENT => no entry resolves as "mine" and every scoped entry is
# skipped. Fail-safe: never guess which agent's credential belongs on this box.
agent_suffix=""
[ -n "${MIND_AGENT:-}" ] && agent_suffix="$(printf '%s' "$MIND_AGENT" | tr '[:lower:]' '[:upper:]')"

# Known-agent roster, used ONLY to classify pass-2 scope skips for reporting
# (g-335-250) — it never decides what gets written. Derived from the in-repo
# agent dirs, which are present on any cloned node.
#
# IRREDUCIBLY LOCAL: this is a COLD-NODE bootstrap script and must not source
# _paths.sh, which reads local-paths.conf — a file that by definition does not
# exist yet before provisioning. `_APD` mirrors AGENTS_PARENT_DIR by hand, using
# the same name as the other sanctioned inliners so CLAUDE.md's constant-audit
# grep matches this line and a future rename is caught without a doc edit.
_APD="agents"
# CAUTION: the `REPO_ROOT` computed above is `$SCRIPT_DIR/..` = <repo>/core, NOT
# the project root — a pre-existing misnomer (tracked separately; it also decides
# the default --out path). Derive the project root explicitly here instead of
# inheriting that error, or this glob silently matches the wrong directory.
_PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Roster source, in precedence order (g-335-254):
#   1. MIND_AGENTS_ROOT — env override. Mirrors the same-named override in
#      core/scripts/gates/goal_duplication.py, added there for EXACTLY this
#      defect class (tests silently depending on whichever agent dirs happen
#      to exist live). Tests MUST pin it explicitly rather than inherit an
#      ambient value (rb-3208: deploy-platform env vars leak into test runs).
#   2. the in-repo agent dirs — the default; production sets no override.
# The roster is advisory: it only classifies a skip for REPORTING and never
# decides what gets written, so a wrong roster can at worst mislabel a warning.
_ROSTER_ROOT="${MIND_AGENTS_ROOT:-$_PROJECT_ROOT/$_APD}"
KNOWN_AGENTS=""
for _d in "$_ROSTER_ROOT"/*/; do
    [ -d "$_d" ] || continue
    KNOWN_AGENTS+="$(basename "$_d" | tr '[:lower:]' '[:upper:]')"$'\n'
done

# Pass 1 — entries scoped to THIS agent.
if [ -n "$agent_suffix" ]; then
    while IFS= read -r line; do
        case "$line" in ''|\#*) continue ;; esac
        case "$line" in "${prefix}"*=*) : ;; *) continue ;; esac
        vault_key="${line%%=*}"
        value="${line#*=}"
        container_key="${vault_key#"$prefix"}"
        case "$container_key" in
            *__"${agent_suffix}") base="${container_key%__"${agent_suffix}"}" ;;
            *) continue ;;
        esac
        [ -n "$base" ] || continue
        ENV_BODY+="${base}=${value}"$'\n'
        WRITTEN_KEYS+="${base}"$'\n'
        SCOPED_KEYS+="${base}"$'\n'
    done <<< "$VAULT_CONTENT"
fi

# Pass 2 — generic entries. Skips anything scoped (rule 2) and anything this
# agent already resolved from a scoped entry (rule 1).
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
    # Scoped to some agent — ours was taken in pass 1, so anything still here is
    # not ours and is never written (rule 2). Dropping is correct; dropping
    # SILENTLY is not, so classify before discarding. A suffix naming a KNOWN
    # agent is the expected security skip. A suffix naming no known agent means
    # someone put the reserved `__` separator in a plain config name and their
    # key is being thrown away with no trace — and the person who trips a
    # reserved-separator contract is by definition someone who did not know it.
    # Reported below by NAME only, never by value (invariant 5). (g-335-250)
    case "$container_key" in
        *__*)
            skip_suffix="${container_key##*__}"
            # Pass 2 re-scans the WHOLE vault, so this agent's own scoped entries
            # reappear here after pass 1 already resolved them. They are not skips
            # and must not be counted as foreign — drop them silently as before.
            if [ -n "$agent_suffix" ] && [ "$skip_suffix" = "$agent_suffix" ]; then
                continue
            fi
            case $'\n'"$KNOWN_AGENTS" in
                *$'\n'"$skip_suffix"$'\n'*) FOREIGN_SCOPED_KEYS+="${container_key}"$'\n' ;;
                *)                          UNKNOWN_SCOPE_KEYS+="${container_key}"$'\n' ;;
            esac
            continue
            ;;
    esac
    # generic entry superseded by this agent's scoped value
    case $'\n'"$SCOPED_KEYS" in *$'\n'"$container_key"$'\n'*) continue ;; esac
    ENV_BODY+="${container_key}=${value}"$'\n'
    WRITTEN_KEYS+="${container_key}"$'\n'
done <<< "$VAULT_CONTENT"

# ── Scope-skip reporting (g-335-250) ────────────────────────────────────────────
# Pass 2 discards every remaining `__`-bearing key. A discarded key is absent from
# the written env, absent from the verify listing, and absent from the counts — so
# without this block the operator sees a clean run and a missing value with nothing
# connecting the two. Names only; no value is ever printed (invariant 5).
n_foreign="$(printf '%s' "$FOREIGN_SCOPED_KEYS" | grep -c . || true)"
n_unknown="$(printf '%s' "$UNKNOWN_SCOPE_KEYS" | grep -c . || true)"
if [ -z "$KNOWN_AGENTS" ] && [ "$n_unknown" -gt 0 ]; then
    # No roster to classify against. Say exactly that instead of accusing every
    # scoped key of a contract violation — a false-positive storm would train
    # operators to ignore this warning, which is worse than the silence it replaces.
    echo "provision-from-vault: ${n_unknown} agent-scoped key(s) skipped; could not classify them (no agent dirs under ${_ROSTER_ROOT} to check suffixes against)." >&2
elif [ "$n_unknown" -gt 0 ]; then
    # Claim only what the evidence supports (g-335-254). The roster is whatever
    # agent dirs exist ON THIS BOX, so a suffix missing from it means "not
    # recognized here" — NOT proven "not an agent". A fleet agent whose dir has
    # not landed yet would otherwise be accused of a contract violation, and a
    # warning that is sometimes wrong is a warning operators learn to ignore.
    echo "provision-from-vault: NOTICE — ${n_unknown} vault key(s) carry the reserved '__' separator with a suffix not matching any agent known on this box. DROPPED, not written:" >&2
    while IFS= read -r k; do
        [ -n "$k" ] || continue
        printf '    %-32s [DROPPED: scope suffix unrecognized here]\n' "$k" >&2
    done <<< "$UNKNOWN_SCOPE_KEYS"
    echo "  Two possibilities: (a) '__' was used in a plain container env name — it is RESERVED as the agent-scope separator, so rename the vault key; or (b) the suffix names a real agent whose dir is not present on this box, in which case the key is correctly skipped here and nothing is wrong." >&2
    echo "  Roster checked: ${_ROSTER_ROOT} (override with MIND_AGENTS_ROOT)." >&2
fi
if [ "$n_foreign" -gt 0 ]; then
    if [ -z "$agent_suffix" ]; then
        echo "provision-from-vault: ${n_foreign} agent-scoped key(s) skipped — MIND_AGENT is unset, so no scoped entry could resolve as this box's (fail-safe). Set MIND_AGENT to provision per-agent credentials." >&2
    else
        echo "provision-from-vault: ${n_foreign} key(s) scoped to another agent — not written (expected)." >&2
    fi
fi

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
    # Scope tag is a KEY-NAME-level fact, so it stays inside the values-blind
    # contract (invariant 5) while letting an operator confirm that per-agent
    # resolution actually fired instead of silently falling back to generic.
    scope_tag=""
    case $'\n'"$SCOPED_KEYS" in *$'\n'"$k"$'\n'*) scope_tag=" (agent-scoped: ${agent_suffix})" ;; esac
    if [ -n "$v" ]; then
        printf '    %-32s [OK]%s\n' "$k" "$scope_tag" >&2; n_ok=$((n_ok+1))
    else
        printf '    %-32s [EMPTY]%s\n' "$k" "$scope_tag" >&2; n_empty=$((n_empty+1))
    fi
done <<< "$WRITTEN_KEYS"
n_scoped="$(printf '%s' "$SCOPED_KEYS" | grep -c . || true)"
echo "provision-from-vault: ${n_ok} key(s) OK, ${n_empty} EMPTY, ${n_scoped} agent-scoped." >&2

# Non-zero if any provisioned key came through empty (a partial vault is a failure).
[ "$n_empty" -eq 0 ]
