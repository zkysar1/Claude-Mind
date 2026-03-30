#!/usr/bin/env bash
# meta-read.sh — Read a meta-strategy file or field
# Usage: meta-read.sh <file> [--field <dotpath>] [--json]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-yaml.py" read "$@"
