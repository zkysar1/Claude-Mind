#!/usr/bin/env bash
# box-capability-probe.sh — thin exec passthrough to box_capability_probe.py.
#
# Exists for the same reason every other wrapper here does (guard-3864): sourcing
# _paths.sh is what supplies WORLD_PATH/META_PATH from the per-agent
# local-paths.conf, and a bare `py -3` invocation gets STORAGE_BACKEND from
# settings.json with no mappable world root. This probe does not read the world
# today, but callers copy invocation shapes far more often than they read them,
# and a bare-py habit learned here transfers to a script where it silently reads
# the local mirror instead of the authoritative store.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$(dirname "$0")/box_capability_probe.py" "$@"
