#!/usr/bin/env bash
# /seed diff <destination> — compare source (post-transform) vs destination.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

DEST=""
MANIFEST="$CONFIG_DIR/seed-manifest.yaml"

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="$2"; shift ;;
        -*) echo "Unknown flag: $1" >&2; exit 2 ;;
        *) DEST="$1" ;;
    esac
    shift
done

if [ -z "$DEST" ] || [ ! -d "$DEST" ]; then
    echo "Usage: seed-diff.sh <destination> [--manifest <path>]" >&2
    exit 2
fi
DEST="$(cd "$DEST" && pwd)"

py -3 "$SCRIPT_DIR/_seed_engine.py" diff --manifest "$MANIFEST" --source "$PROJECT_ROOT" --dest "$DEST"
