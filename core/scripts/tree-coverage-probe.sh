#!/usr/bin/env bash
# tree-coverage-probe.sh — does the knowledge tree cover this category?
#
# Companion to the scaffolded-exploration protocol (, see
# world/conventions/scaffolded-exploration.md). When deciding whether to
# file an Investigate-precursor before an Apply goal, the agent calls this
# probe with the Apply's category. Exit 0 means the tree already has
# coverage; exit 2 means the category has no tree presence and an
# Investigate-precursor is the right move.
#
# Implementation note: this is a WORLD_DIR existence check, nothing more.
# Coverage QUALITY (depth, freshness) is out of scope — that's what the
# precursor's tree-encoding deliverable is FOR. Probe answers "does the
# directory exist", not "is the documentation good".
#
# Usage:
#   tree-coverage-probe.sh <category>
#
# Exit codes:
#   0  — covered (world/knowledge/tree/<category>/ exists as a directory)
#   1  — bad input (missing argument, missing WORLD_DIR)
#   2  — uncovered (directory does not exist)

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

if [[ $# -lt 1 ]]; then
    echo "Usage: tree-coverage-probe.sh <category>" >&2
    exit 1
fi

CATEGORY="$1"

if [[ -z "${WORLD_DIR:-}" ]]; then
    echo "tree-coverage-probe: WORLD_DIR not set (check local-paths.conf)" >&2
    exit 1
fi

TREE_DIR="$WORLD_DIR/knowledge/tree/$CATEGORY"

if [[ -d "$TREE_DIR" ]]; then
    echo "covered: $TREE_DIR"
    exit 0
fi

echo "uncovered: $TREE_DIR (not found)"
exit 2
