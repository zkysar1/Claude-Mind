#!/usr/bin/env bash
# Displaced-id audit — stale references to ids reassigned by the collision-reid
# merge path — thin wrapper. See core/scripts/displaced-id-audit.py for full docs.
#
# WHY THIS WRAPPER EXISTS (authored by omni in ZDS-Mind as , 2026-08-22;
# back-ported UP to the frontier during the 2026-08-23 Claude-Mind -> ZDS-Mind
# promotion reconcile, guard-119). The .py had NO shell entry point, and that is
# a large part of why it had no caller for its whole life: the recurring-goal
# `command_succeeds` check allowlist requires a `bash core/scripts/...` form, so
# the audit was literally unwireable through the standard path. First run ever
# reported 212 displacement events and 195 UNRELATED-class stale citations
# already live. A detector that cannot be invoked the way every sibling audit is
# invoked will not be invoked.
#
# Usage:
#   bash core/scripts/displaced-id-audit.sh [--strict] [...]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/displaced-id-audit.py" "$@"
