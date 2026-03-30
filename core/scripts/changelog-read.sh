#!/usr/bin/env bash
# Read recent changelog entries.
# Usage: bash core/scripts/changelog-read.sh [--since <duration>] [--agent <name>] [--last <N>]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/changelog.py" read "$@"
