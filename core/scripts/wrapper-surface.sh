#!/usr/bin/env bash
# wrapper-surface — print ONE wrapper's accepted invocation surface, four layers
# deep (flags / subcommands / stdin / daemon endpoint). See wrapper-surface.py
# for full docs and the measured origin.
#
# Read this BEFORE invoking an unfamiliar core/scripts wrapper. It answers the
# question guard-136 / guard-2172 / guard-2350 keep asking you to answer by hand
# — and that kept getting answered wrong anyway (9 misses in one session,
# ; 3 in one goal, ; 5-6 in one session, ).
#
# Local-only: pure static read of the script sources. No daemon, no network, no
# writes. Safe to run anytime, in any mode, including reader.
#
#   bash core/scripts/wrapper-surface.sh goal-selector.sh
#   bash core/scripts/wrapper-surface.sh board-post.sh --json
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/wrapper-surface.py" "$@"
