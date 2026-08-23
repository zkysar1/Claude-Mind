#!/usr/bin/env bash
# /seed create — maintenance helper for the seed manifest.
#
# For the INITIAL seed the manifest is hand-curated and committed at
# core/config/seed-manifest.yaml. This command scans for new framework files
# added since the manifest's `updated` date and prints proposed updates.
#
# In `--dry-run` mode (default) it only prints proposals.
# `--write` is reserved for future implementation of automatic manifest update.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

MANIFEST="$CONFIG_DIR/seed-manifest.yaml"
DRY_RUN=1

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="$2"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --write) DRY_RUN=0 ;;
        -*) echo "Unknown flag: $1" >&2; exit 2 ;;
        *) echo "Unexpected positional arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ ! -f "$MANIFEST" ]; then
    echo "Manifest not found: $MANIFEST" >&2
    exit 3
fi

echo "[seed-create] manifest=$MANIFEST"
echo "[seed-create] Scanning for new files since manifest 'updated' date..."

export PROJECT_ROOT
export MANIFEST

py -3 "$SCRIPT_DIR/_seed_create_scan.py"

if [ $DRY_RUN -eq 1 ]; then
    echo "[seed-create] Dry run (default). Re-run with --write when auto-update is implemented."
fi
