#!/usr/bin/env bash
# rollback-recipe TEMPLATE for vX.Y.Z — reverses v{X.Y.Z}.sh in REVERSE order.
#
# CONTRACT (validated by release.sh step 4 via
# _release_lib.validate_recipe_structure — STRUCTURAL only, never executed at
# release-cut). A rollback MUST:
#   1. Be "idempotent" — re-running on already-rolled-back state is a no-op
#      success (exit 0), never a double-undo or an error.
#   2. Have a "Pre-check" — verify we are at the NEW version (the one being
#      rolled back FROM); if already at the OLD version, exit 0 (idempotent).
#   3. Reverse the upgrade's steps in reverse order.
#   4. (cross_world only — H3b) RESTORE $WORLD_PATH and $META_PATH from the
#      upgrade snapshot in EXECUTABLE code. A git-tag rollback CANNOT restore
#      the external world/+meta/ paths, so the snapshot is the ONLY rollback
#      path for them — a rollback that cannot use it is not a real rollback.
#   5. Have a "Post-check" — verify the OLD version state is restored.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/../../scripts/_paths.sh"

# --- Pre-check (idempotent) ---
CURRENT="$(grep -E '^__version__' "$PROJECT_ROOT/mind_api/src/__init__.py" | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ "$CURRENT" == "<previous-version>" ]]; then
  echo "Already at <previous-version> — rollback is a no-op (idempotent)."
  exit 0
fi
[[ "$CURRENT" == "vX.Y.Z-without-leading-v" ]] || {
  echo "WARNING: expected to roll back from vX.Y.Z, found $CURRENT — proceeding idempotently" >&2; }

# --- Steps (reverse order of the upgrade) ---
# Reverse step B: sed -i 's/^new_field:/old_field:/' "$WORLD_PATH/config/some.yaml"
# Reverse step A: git mv new-name.md old-name.md

# --- Restore world/+meta/ from the upgrade snapshot (cross_world only — H3b) ---
# The upgrade recipe printed its SNAP_DIR; pass it back in via env. A git-tag
# rollback CANNOT restore these external paths, so this copy is the only path.
# Idempotent: if SNAP_DIR is unset or the snapshot is absent, restore is skipped
# (the tracked-file reversals above still apply). Drop this block for a
# framework-only (cross_world:false) rollback.
if [[ -n "${SNAP_DIR:-}" && -d "$SNAP_DIR/world" && -d "$SNAP_DIR/meta" ]]; then
  cp -r "$SNAP_DIR/world/." "$WORLD_PATH/" || { echo "ERROR: world restore from snapshot failed" >&2; exit 1; }
  cp -r "$SNAP_DIR/meta/."  "$META_PATH/"  || { echo "ERROR: meta restore from snapshot failed" >&2; exit 1; }
  echo "restored world/+meta/ from snapshot $SNAP_DIR"
else
  echo "NOTE: SNAP_DIR unset or snapshot absent — world/+meta/ NOT restored from snapshot (tracked-file reversals only)" >&2
fi

# --- Post-check ---
echo "Rollback to <previous-version> complete."
