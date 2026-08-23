#!/usr/bin/env bash
# migrate-to-mind-data.sh --  M4 ()
#
# Migrate a mind that stores world/ and meta/ at EXTERNAL paths (configured in
# agents/<agent>/local-paths.conf) into a SELF-CONTAINED PROJECT_ROOT/.mind-data/
# local storage root. After migration the repo carries its own world/meta and no
# longer depends on an external data directory.
#
# What it does (in order):
#   1. Read WORLD_PATH / META_PATH from the bound agent's local-paths.conf
#      (falls back to the first-discovered agents/*/local-paths.conf). These are
#      read DIRECTLY from the conf file, NOT via _paths.sh resolution -- once
#      .mind-data/ exists _paths would shadow the external source with the
#      destination, so a re-run must never resolve through it.
#   2. Copy WORLD_PATH  -> .mind-data/world/
#           META_PATH   -> .mind-data/meta/
#      (prefers `rsync -a` when available; falls back to `cp -r`, since this
#       git-bash ships no rsync.)
#   3. (--agent-dirs) Copy agents/ -> .mind-data/agents/
#   4. Write .mind-data/.env.local with STORAGE_BACKEND=local.
#   5. Back up each agents/*/local-paths.conf -> .bak (unless --no-backup) so
#      path resolution falls through to the .mind-data tier.
#
# Resolution after migration (see core/scripts/_paths.py priority chain):
#   MIND_{WORLD,META} env > .mind-data/.env.local {WORLD,META}_PATH
#     > .mind-data/{world,meta} bare default > local-paths.conf > None
# Because .mind-data/ now exists, the bare-default tier resolves world ->
# .mind-data/world and meta -> .mind-data/meta with no conf entry needed.
#
# NOTE on STORAGE_BACKEND: the backend layer (core/scripts/owncloud_sync.py)
# reads STORAGE_BACKEND from the ENVIRONMENT / PROJECT_ROOT .env.local and
# defaults to "local" when unset -- it does NOT source .mind-data/.env.local.
# The STORAGE_BACKEND=local line we write there is the explicit, documented
# marker of intent for a self-contained mind. If PROJECT_ROOT/.env.local pins
# STORAGE_BACKEND=own-cloud it would OVERRIDE that intent, so we warn on it.
#
# Flags:
#   --dry-run     Print the plan and make NO changes.
#   --agent-dirs  Also copy agents/ -> .mind-data/agents/.
#   --no-backup   Do NOT rename local-paths.conf -> .bak (keep the external pointer).
#   -h | --help   Show usage.
#
# Idempotent: rsync -a / cp -r overwrite in place; re-runs refresh the copy.
set -euo pipefail

DRY_RUN=0
AGENT_DIRS=0
NO_BACKUP=0

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN=1 ;;
    --agent-dirs) AGENT_DIRS=1 ;;
    --no-backup)  NO_BACKUP=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "migrate-to-mind-data: unknown flag: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- PROJECT_ROOT from the script location (no _paths.sh dependency: this tool
#     must run on a fresh clone whose resolution layer is mid-migration). ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# core/scripts/<this> -> SCRIPT_DIR=core/scripts ; PROJECT_ROOT is TWO levels up.
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

MIND_DATA="$PROJECT_ROOT/.mind-data"
WORLD_DST="$MIND_DATA/world"
META_DST="$MIND_DATA/meta"

# --- locate the source local-paths.conf ---
AGENT="${MIND_AGENT:-}"
CONF=""
if [ -n "$AGENT" ] && [ -f "$PROJECT_ROOT/agents/$AGENT/local-paths.conf" ]; then
  CONF="$PROJECT_ROOT/agents/$AGENT/local-paths.conf"
else
  for c in "$PROJECT_ROOT"/agents/*/local-paths.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
  done
fi
if [ -z "$CONF" ]; then
  echo "migrate-to-mind-data: ERROR no agents/*/local-paths.conf found under $PROJECT_ROOT/agents/" >&2
  echo "  (nothing to migrate -- this repo has no external WORLD_PATH/META_PATH configured)" >&2
  exit 1
fi

# read a key=value from a conf file, stripping surrounding whitespace and quotes
read_conf_val() {  # $1=key $2=file
  grep -E "^[[:space:]]*$1[[:space:]]*=" "$2" 2>/dev/null | head -1 \
    | sed -E "s/^[^=]*=[[:space:]]*//; s/[[:space:]]*\$//; s/^[\"']//; s/[\"']\$//"
}

WORLD_SRC="$(read_conf_val WORLD_PATH "$CONF")"
META_SRC="$(read_conf_val META_PATH "$CONF")"

if [ -z "$WORLD_SRC" ] || [ -z "$META_SRC" ]; then
  echo "migrate-to-mind-data: ERROR WORLD_PATH/META_PATH not found in $CONF" >&2
  exit 1
fi
if [ ! -d "$WORLD_SRC" ]; then
  echo "migrate-to-mind-data: ERROR WORLD_PATH source dir missing: $WORLD_SRC" >&2
  exit 1
fi
if [ ! -d "$META_SRC" ]; then
  echo "migrate-to-mind-data: ERROR META_PATH source dir missing: $META_SRC" >&2
  exit 1
fi

# --- counts / sizes for the plan (best-effort, bounded; never fatal) ---
# A multi-GB world/.history must NOT block the dry-run preview, so each scan is
# wrapped in `timeout`; on timeout/error we show "?" rather than a misleading
# partial count. (`timeout` ships with this git-bash; falls through to "?" if not.)
count_files() {  # $1=dir
  local n
  if command -v timeout >/dev/null 2>&1; then
    if n="$(timeout 8 find "$1" -type f 2>/dev/null | wc -l)"; then printf '%s' "$n" | tr -d ' '; else printf '?'; fi
  else
    find "$1" -type f 2>/dev/null | wc -l | tr -d ' '
  fi
}
dir_size() {  # $1=dir
  local s
  if command -v timeout >/dev/null 2>&1; then
    if s="$(timeout 8 du -sh "$1" 2>/dev/null | cut -f1)" && [ -n "$s" ]; then printf '%s' "$s"; else printf '?'; fi
  else
    du -sh "$1" 2>/dev/null | cut -f1
  fi
}

WORLD_N="$(count_files "$WORLD_SRC")"; WORLD_SZ="$(dir_size "$WORLD_SRC")"
META_N="$(count_files "$META_SRC")";   META_SZ="$(dir_size "$META_SRC")"

# --- copy engine: rsync if available, else cp -r ---
COPY_TOOL="cp -r"
command -v rsync >/dev/null 2>&1 && COPY_TOOL="rsync -a"
copy_tree() {  # $1=src $2=dst
  local src="$1" dst="$2"
  mkdir -p "$dst"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$src/" "$dst/"
  else
    cp -r "$src/." "$dst/"
  fi
}

# --- own-cloud override warning (the local intent is ineffective if the
#     environment / PROJECT_ROOT .env.local pins own-cloud) ---
OWN_CLOUD_WARN=""
ENV_BACKEND="${STORAGE_BACKEND:-}"
if [ -z "$ENV_BACKEND" ] && [ -f "$PROJECT_ROOT/.env.local" ]; then
  ENV_BACKEND="$(read_conf_val STORAGE_BACKEND "$PROJECT_ROOT/.env.local")"
fi
ENV_BACKEND="$(printf '%s' "$ENV_BACKEND" | tr '[:upper:]' '[:lower:]')"
if [ "$ENV_BACKEND" = "own-cloud" ]; then
  OWN_CLOUD_WARN="WARN: STORAGE_BACKEND=own-cloud is set (env or PROJECT_ROOT/.env.local). The migrated .mind-data/.env.local local marker will NOT take effect until that own-cloud setting is removed/overridden."
fi

# --- the plan (always printed) ---
echo "=== migrate-to-mind-data (asp-330 M4) ==="
echo "project root : $PROJECT_ROOT"
echo "source conf  : $CONF"
echo "world source : $WORLD_SRC  ($WORLD_N files, ${WORLD_SZ:-?})"
echo "meta source  : $META_SRC  ($META_N files, ${META_SZ:-?})"
echo "world dest   : $WORLD_DST"
echo "meta dest    : $META_DST"
echo "copy tool    : $COPY_TOOL"
echo "agent-dirs   : $([ "$AGENT_DIRS" -eq 1 ] && echo "yes -> $MIND_DATA/agents" || echo "no (--agent-dirs to include)")"
echo "backup confs : $([ "$NO_BACKUP" -eq 1 ] && echo "no (--no-backup)" || echo "yes -> agents/*/local-paths.conf.bak")"
echo "env.local    : $MIND_DATA/.env.local  (STORAGE_BACKEND=local)"
[ -n "$OWN_CLOUD_WARN" ] && echo "$OWN_CLOUD_WARN" >&2
if [ -d "$MIND_DATA" ]; then
  echo "note         : $MIND_DATA already exists -- this is a re-run / refresh (copy overwrites in place)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "(dry run -- no changes written. Re-run without --dry-run to migrate.)"
  exit 0
fi

# --- execute ---
echo ""
echo "migrating world ..."
copy_tree "$WORLD_SRC" "$WORLD_DST"
echo "migrating meta ..."
copy_tree "$META_SRC" "$META_DST"

if [ "$AGENT_DIRS" -eq 1 ]; then
  echo "migrating agents/ ..."
  copy_tree "$PROJECT_ROOT/agents" "$MIND_DATA/agents"
fi

# .mind-data/.env.local -- the explicit local-backend marker.
mkdir -p "$MIND_DATA"
cat > "$MIND_DATA/.env.local" <<'ENVEOF'
# Written by core/scripts/migrate-to-mind-data.sh ( M4, ).
# This repo's world/meta now live under .mind-data/ as a self-contained local
# store. core/scripts/_paths.{py,sh} resolve world -> .mind-data/world and
# meta -> .mind-data/meta from the bare-default tier once .mind-data/ exists.
# STORAGE_BACKEND=local is the explicit marker of intent (the backend layer
# defaults to local when STORAGE_BACKEND is unset; it sources the ENVIRONMENT /
# PROJECT_ROOT .env.local, not this file -- keep PROJECT_ROOT free of an
# own-cloud override to honor this).
STORAGE_BACKEND=local
ENVEOF
echo "wrote $MIND_DATA/.env.local"

if [ "$NO_BACKUP" -eq 0 ]; then
  for c in "$PROJECT_ROOT"/agents/*/local-paths.conf; do
    [ -f "$c" ] || continue
    mv "$c" "$c.bak"
    echo "backed up $(basename "$(dirname "$c")")/local-paths.conf -> .bak"
  done
else
  echo "kept local-paths.conf (--no-backup); .mind-data tier still takes priority over it"
fi

echo ""
echo "=== migration complete ==="
echo "world -> $WORLD_DST"
echo "meta  -> $META_DST"
echo "Verify resolution:  python -c \"import sys; sys.path.insert(0,'core/scripts'); import _paths; print('WORLD_DIR=',_paths.WORLD_DIR); print('META_DIR=',_paths.META_DIR)\""
echo "Then /start <agent> resolves world/meta from .mind-data/ (no external local-paths.conf needed)."
[ -n "$OWN_CLOUD_WARN" ] && echo "$OWN_CLOUD_WARN" >&2
exit 0
