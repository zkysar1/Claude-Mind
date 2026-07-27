#!/usr/bin/env bash
# mirror-health.sh — is this box's own-cloud MIRROR serving fresh data, or
# silently wedged on both-diverged conflict-skips? ()
#
# Classifies mind_api/state/owncloud-conflict-streaks.json (rewritten by every
# real sync sweep to the CURRENT conflict set) instead of grepping spawn.log.
# See core/scripts/mirror_health.py for the decision logic + the 
# incident this makes visible (30 files frozen 21h, days-stale reads).
#
# Usage: mirror-health.sh [--json] [--threshold N] [--max-age-min M]
#   text (default): one line "mirror-health: <verdict> — <reason>" + wedged files
#   --json: full verdict object {verdict, reason, wedged_count, files, age_min}
# Exit: 0 healthy, 1 wedged (file list printed), 2 unknown (no live signal).
# Advisory/display-first — repair is the  /reconcile-owncloud-conflicts
# protocol; this probe only surfaces the condition.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/_paths.sh"

exec python3 "$HERE/mirror_health.py" "$@"
