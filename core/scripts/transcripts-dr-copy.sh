#!/usr/bin/env bash
# Incremental off-site copy of the transcripts/ prefix to the pinned AWS DR
# target. See core/scripts/transcripts_dr_copy.py for the measurement +
# rationale. Same wrapper shape as cold-snapshot.sh: the env must be loaded
# before the resolver reads STORAGE_S3_* / COLD_SNAPSHOT_*.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/transcripts_dr_copy.py" "$@"
