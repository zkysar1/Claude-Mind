#!/usr/bin/env bash
# meta-cat.sh — Read a file from the meta directory with correct path resolution.
# For YAML files, prefer meta-read.sh (supports --field extraction).
# Usage: meta-cat.sh <relative-path>
# Example: meta-cat.sh improvement-instructions.md → cats $META_DIR/improvement-instructions.md
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cat "$META_DIR/$1"
