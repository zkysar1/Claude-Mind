#!/usr/bin/env bash
# upgrade-recipe TEMPLATE — copy to v{X.Y.Z}.sh and fill in the marked spots.
#
#   upgrade-recipe: vX.Y.Z
#   min_source:     <previous-version>
#   breaking:       true
#   cross_world:    true       # set false for a framework-only breaking change
#   rollback:       core/config/upgrade-recipes/vX.Y.Z-rollback.sh
#
# CONTRACT (validated by release.sh step 4 via
# _release_lib.validate_recipe_structure — STRUCTURAL only; the recipe is
# NEVER executed at release-cut. H3: the data migration touches external
# world/+meta/ state that cannot be safely reproduced in a temp clone, so the
# smoke test is syntax-only). A recipe MUST contain:
#   1. A "Pre-check" section  — verify the FROM version + all agents IDLE.
#   2. (cross_world only) a "snapshot" of $WORLD_PATH and $META_PATH taken
#      BEFORE any data migration. A git-tag rollback CANNOT restore the
#      external world/+meta/ paths (they live outside the repo), so the
#      recipe must take its own copy-snapshot first.
#   3. The migration "Steps".
#   4. A "Post-check" section — verify the TO version state is reached.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/../../scripts/_paths.sh"

# --- Pre-check ---
# Refuse to run unless we are at the expected FROM version and no agent is live.
CURRENT="$(grep -E '^__version__' "$PROJECT_ROOT/mind_api/src/__init__.py" | sed -E 's/.*"([^"]+)".*/\1/')"
[[ "$CURRENT" == "<previous-version>" ]] || { echo "ERROR: expected <previous-version>, found $CURRENT" >&2; exit 1; }
for sf in "$(agents_root)"/*/session/agent-state; do
  [[ -f "$sf" && "$(cat "$sf" 2>/dev/null)" == "RUNNING" ]] && {
    echo "ERROR: an agent is RUNNING — stop all agents before upgrading" >&2; exit 1; }
done

# --- World/meta snapshot (cross_world only — H3) ---
# Snapshot BEFORE migrating. git-tag rollback CANNOT restore external paths, so
# this snapshot is the ONLY rollback path for world/+meta/. A FAILED snapshot
# MUST abort the migration (never proceed silently — a half/empty snapshot plus
# a corrupting migration = irrecoverable data loss).
# SNAP_DIR_OVERRIDE lets an operator relocate the snapshot. Default is a sibling
# of WORLD_PATH (the `..`) so the `cp -r "$WORLD_PATH"` below does not recurse
# into the snapshot it is creating.
SNAP_DIR="${SNAP_DIR_OVERRIDE:-$WORLD_PATH/../upgrade-snapshots/vX.Y.Z-$(date +%Y%m%dT%H%M%S)}"
mkdir -p "$SNAP_DIR" || { echo "ERROR: cannot create snapshot dir $SNAP_DIR — aborting before migration" >&2; exit 1; }
cp -r "$WORLD_PATH" "$SNAP_DIR/world" || { echo "ERROR: world snapshot failed — aborting before migration" >&2; exit 1; }
cp -r "$META_PATH"  "$SNAP_DIR/meta"  || { echo "ERROR: meta snapshot failed — aborting before migration" >&2; exit 1; }
echo "snapshot: $SNAP_DIR (pass via SNAP_DIR to the rollback recipe to restore world/+meta/)"

# --- Steps ---
# Example step A (file rename — tracked content): git mv old-name.md new-name.md
# Example step B (YAML field edit — domain overlay):
#   sed -i 's/^old_field:/new_field:/' "$WORLD_PATH/config/some.yaml"

# --- Post-check ---
# Verify the migration reached its target state (grep for the new shape, etc.).
echo "Upgrade to vX.Y.Z complete."
