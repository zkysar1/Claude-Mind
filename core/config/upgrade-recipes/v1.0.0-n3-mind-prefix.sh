#!/usr/bin/env bash
# upgrade-recipe: v1.0.0  — N3 de-overload of the MIND_ env-var prefix
#
#   upgrade-recipe: v1.0.0
#   min_source:     0.2.0
#   breaking:       true
#   cross_world:    false      # framework-only: renames code + .env.local KEYS, not world/+meta/ DATA
#   rollback:       core/config/upgrade-recipes/v1.0.0-n3-mind-prefix-rollback.sh
#
# WHAT N3 DOES (user sign-off 2026-06-05): de-overloads the conflated MIND_
# env-var prefix into 7 purpose buckets — MACHINE_ / RUNTIME_ / STORAGE_ /
# OWNCLOUD_ / ENVIRONMENT_ / AGENTS_ / COMMONS_. The code rename ships IN the
# v1.0.0 release commit; this recipe migrates the one thing the commit cannot
# touch: each machine's gitignored, machine-local `.env.local`.
#
# HARD CUTOVER — NO BACK-COMPAT (user directive 2026-06-05): the daemon's
# .env.local loader (mind_api/src/__main__.py `_N3_ALLOWED_EXACT`) accepts ONLY
# the new exact keys + the MIND_AWS_* credential family + AWS_DEFAULT_REGION. An
# un-migrated .env.local (old MIND_STORAGE_BACKEND etc.) will NOT be seen by the
# new daemon → it silently falls back to the local backend → split-brain with the
# shared S3 store. THEREFORE this recipe MUST run (and the daemon MUST restart)
# before the new daemon serves any agent. Run it with all agents IDLE.
#
# CREDENTIAL BOUNDARY (unchanged): MIND_AWS_ACCESS_KEY_ID / MIND_AWS_SECRET_ACCESS_KEY
# / MIND_AWS_ALLOW_DEFAULT_CHAIN STAY MIND_AWS_* — the MIND_AWS_ prefix IS the
# loader's credential allowlist. This recipe NEVER renames them.
#
# DOWNSTREAM SEED NOTE: a downstream repo still on legacy env-var prefixes must
# rename ITS prefixes when it adopts N3 via seed-transplant — out of THIS frontier
# recipe's scope (the frontier has no such production reads; the map below is the
# frontier's 12 storage keys only).
#
# CONTRACT (validated structurally by release.sh; NEVER executed at release-cut —
# the operator runs it during deploy): Pre-check, Steps, Post-check.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/../../scripts/_paths.sh"

ENV_LOCAL="$PROJECT_ROOT/.env.local"

# The 12 .env.local storage-config keys renamed by N3 (old -> new). The 3
# MIND_AWS_* credential keys are DELIBERATELY ABSENT (they stay MIND_AWS_*).
# MIND_CACHE_ROOT (shell-only override) and MIND_COMMONS_POLICY (doc-only, no
# .env.local presence) are also absent — nothing to migrate for them here.
_N3_KEYMAP=(
  "MIND_ENV_ID=ENVIRONMENT_ID"
  "MIND_STORAGE_BACKEND=STORAGE_BACKEND"
  "MIND_S3_BUCKET=STORAGE_S3_BUCKET"
  "MIND_DDB_LOCK_TABLE=STORAGE_DDB_LOCK_TABLE"
  "MIND_DDB_SESSIONS_TABLE=STORAGE_DDB_SESSIONS_TABLE"
  "MIND_MACHINE_ID=MACHINE_ID"
  "MIND_OWNED_AGENTS=MACHINE_OWNED_AGENTS"
  "MIND_MULTI_MACHINE=MACHINE_MULTI"
  "MIND_CLOUD_CACHE_TTL=OWNCLOUD_CACHE_TTL"
  "MIND_OWNCLOUD_SYNC_DISABLE=OWNCLOUD_SYNC_DISABLE"
  "MIND_OWNCLOUD_SYNC_INTERVAL=OWNCLOUD_SYNC_INTERVAL"
  "MIND_RUNTIME_DIR=RUNTIME_DIR"
)

# --- Pre-check ---
# Refuse to migrate while any agent is live (a running daemon could restart mid-
# migration on a half-renamed .env.local). All agents MUST be IDLE.
for sf in "$(agents_root)"/*/session/agent-state; do
  [[ -f "$sf" && "$(cat "$sf" 2>/dev/null)" == "RUNNING" ]] && {
    echo "ERROR: an agent is RUNNING — stop all agents before the N3 .env.local migration" >&2; exit 1; }
done
CURRENT="$(grep -E '^__version__' "$PROJECT_ROOT/mind_api/src/__init__.py" | sed -E 's/.*"([^"]+)".*/\1/')"
echo "[v1.0.0-n3] code version on disk: $CURRENT (N3 ships in v1.0.0; the .env.local migration below is version-agnostic and idempotent)"
if [[ ! -f "$ENV_LOCAL" ]]; then
  echo "[v1.0.0-n3] no .env.local at $ENV_LOCAL — single-machine LOCAL backend needs none. Nothing to migrate. Done."
  exit 0
fi

# --- Steps ---
# 1) Back up .env.local once (idempotent — never clobber an existing pre-N3 backup).
BAK="$ENV_LOCAL.pre-v1.0.0-n3.bak"
if [[ ! -f "$BAK" ]]; then
  cp "$ENV_LOCAL" "$BAK" || { echo "ERROR: could not back up .env.local — aborting before migration" >&2; exit 1; }
  echo "[v1.0.0-n3] backed up .env.local -> $BAK"
else
  echo "[v1.0.0-n3] backup already exists ($BAK) — re-running idempotently"
fi
# 2) Rename each old key to its new name (anchored at line start; preserves the
#    value verbatim). Idempotent: a line already renamed has no `^OLD=` to match.
#    MIND_AWS_* is never in the map, so the credential keys are never touched.
for pair in "${_N3_KEYMAP[@]}"; do
  old="${pair%%=*}"; new="${pair#*=}"
  sed -i "s/^${old}=/${new}=/" "$ENV_LOCAL"
done
echo "[v1.0.0-n3] renamed ${#_N3_KEYMAP[@]} storage keys in $ENV_LOCAL (MIND_AWS_* credentials left untouched)"

# --- Post-check ---
# No OLD storage key may remain; the MIND_AWS_* credential keys MUST remain.
LEFT="$(grep -E '^(MIND_ENV_ID|MIND_STORAGE_BACKEND|MIND_S3_BUCKET|MIND_DDB_LOCK_TABLE|MIND_DDB_SESSIONS_TABLE|MIND_MACHINE_ID|MIND_OWNED_AGENTS|MIND_MULTI_MACHINE|MIND_CLOUD_CACHE_TTL|MIND_OWNCLOUD_SYNC_DISABLE|MIND_OWNCLOUD_SYNC_INTERVAL|MIND_RUNTIME_DIR)=' "$ENV_LOCAL" || true)"
if [[ -n "$LEFT" ]]; then
  echo "ERROR: old N3 storage keys still present after migration:" >&2; echo "$LEFT" >&2; exit 1
fi
if grep -qE '^STORAGE_BACKEND=' "$ENV_LOCAL"; then
  echo "[v1.0.0-n3] post-check OK: new STORAGE_BACKEND key present, no old storage keys remain."
fi
if grep -qE '^MIND_AWS_' "$ENV_LOCAL"; then
  echo "[v1.0.0-n3] credential keys (MIND_AWS_*) preserved — credential boundary intact."
fi
echo
echo "[v1.0.0-n3] .env.local migration complete. NEXT: restart the daemon onto the new"
echo "            code so the new exact-allowlist loader reads the new keys:"
echo "              bash core/scripts/mind-api-start.sh --restart"
echo "            Then bring agents back up. Rollback: $(dirname "$0")/v1.0.0-n3-mind-prefix-rollback.sh"
echo "Upgrade to v1.0.0 (N3) complete."
