#!/usr/bin/env bash
# Centralized path resolution for the cognitive core.
# Source this at the top of every shell script.
# Sets: SCRIPT_DIR, CORE_ROOT, PROJECT_ROOT, MIND_DIR, CONFIG_DIR, REPO_ROOT
#
# BASH_SOURCE[0] resolves to THIS file's location (not the caller's $0).
# This is critical — it anchors all paths to core/scripts/ regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"       # core/
PROJECT_ROOT="$(cd "$CORE_ROOT/.." && pwd)"      # repo root
MIND_DIR="$PROJECT_ROOT/mind"
CONFIG_DIR="$CORE_ROOT/config"
REPO_ROOT="$PROJECT_ROOT"                        # legacy alias
