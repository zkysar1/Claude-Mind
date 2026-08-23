#!/usr/bin/env bash
# world-cat.sh — Read a file from the world directory with correct path resolution.
# Usage: world-cat.sh <relative-path>
# Example: world-cat.sh program.md → cats $WORLD_DIR/program.md
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cat "$WORLD_DIR/$1"
