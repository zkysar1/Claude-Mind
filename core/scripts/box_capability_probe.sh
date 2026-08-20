#!/usr/bin/env bash
# Box Capability Probe — thin wrapper for box_capability_probe.py.
# See the .py header for what it answers ("can THIS box do it", not "am I
# allowed") and  / rb-8408 for the rationale.
#
# Usage:
#   bash core/scripts/box_capability_probe.sh --self-check
#   bash core/scripts/box_capability_probe.sh --secret MIND_STRIPE_SECRET_KEY
#   bash core/scripts/box_capability_probe.sh --peer zds-mind --path /opt/GitHub/X
#
# Exit 0 = every probed capability PRESENT, 1 = any ABSENT, 2 = any UNKNOWN
# (the probe could not run — which is NOT absence).
#
# The `source _paths.sh` is load-bearing, not ceremony (guard-3864 / rb-7918):
# MIND_WORLD / WORLD_PATH come from the per-agent local-paths.conf that ONLY
# _paths.sh reads, while STORAGE_BACKEND is set globally in settings.json. A
# bare `py -3` therefore has a backend but no mappable world root, so the
# peer-world lane resolves against the LOCAL MIRROR instead of the
# authoritative store — and this probe's whole job is to answer a
# filesystem question truthfully.
#
# This wrapper was MISSING for its first ~1.5h (2026-08-19): the goal that
# shipped the probe recorded "built the .py + a .sh exec passthrough" in its
# experience record, and only the .py existed. Caught when a caller ran the
# documented .sh form and got "No such file or directory" — which reads
# exactly like a broken probe rather than an absent wrapper.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/box_capability_probe.py" "$@"
