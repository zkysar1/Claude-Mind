#!/usr/bin/env bash
# /seed diff <destination> — compare source (post-transform) vs destination.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
# py -3 on Windows, python3 elsewhere: never a bare `py -3` (, guard-1098).
source "$SCRIPT_DIR/_python_launcher.sh"
PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3

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

$PYLAUNCH "$SCRIPT_DIR/_seed_engine.py" diff --manifest "$MANIFEST" --source "$PROJECT_ROOT" --dest "$DEST"
