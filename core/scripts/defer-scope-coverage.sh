#!/usr/bin/env bash
# defer-scope-coverage — consume the four recheck lanes' exclusion counts.
# See defer-scope-coverage.py for the full docstring and  for the
# rationale. Report-only: it never writes a scope onto a goal, and no --apply
# exists or may be added (inferring a scope from prose and storing it would
# launder a guess into a fact — reclaim-routed-work.md rule 2).
#
# WHY A WRAPPER AT ALL (, 2026-09-22): the .py shipped 2026-08-09 with
# zero call sites, and precheck-medium-battery.py dispatches BASH scripts only
# (`_run` is documented "Run a core/scripts bash script"; every lane's `script`
# field is a .sh). Without this file the consumer cannot be wired into the tier
# the rest of its family runs in. It also gives the lane the same env contract
# as its siblings — only _paths.sh maps the per-agent world root (guard-3864).
#
# Usage: defer-scope-coverage.sh [--output json|text] [--exit-on-unkeyable]
#                                [--show N]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/defer-scope-coverage.py" "$@"
