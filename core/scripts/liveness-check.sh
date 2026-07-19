#!/usr/bin/env bash
# domain-leak-exempt: own-cloud liveness wrapper — loads STORAGE_BACKEND/S3 creds
# for the authoritative-store freshness probe; these are framework infrastructure
# identifiers (same class as owncloud_backend.py / bring-up-doctor.sh), not domain content.
# liveness-check.sh — is <agent> ALIVE, DORMANT, or UNKNOWN? (g-115-2149)
#
# Combines the daemon-routed `last_active` snapshot with an INHERENTLY-FRESH
# signal (own-cloud: the partner's team-state SHARD S3 LastModified; local
# backend: the shard's local mtime) so a stale local `last_active` mirror never
# yields a false-DORMANT verdict. This is the reusable resolution to the
# check-team-state-before-silent.md rule-5 pull-path caveat: the daemon composes
# team-state from LOCAL shard mirrors (_team_state.load_rows raw open, no S3
# read-through), so a busy partner whose fresh shard is on S3 reads as stale on
# every OTHER box. See core/scripts/liveness_check.py for the decision logic.
#
# Usage: liveness-check.sh --agent <name> [--threshold-hours N] [--json]
#   text (default): prints one word alive|dormant|unknown; diagnostic to stderr.
#   --json: prints the full verdict object (verdict, reason, signal, ages).
# Always exits 0 — the VERDICT is the signal (mirrors world/scripts/inbox-watch-pull.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/_paths.sh"

AGENT=""
THRESHOLD="6"
JSON_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --agent) AGENT="$2"; shift 2 ;;
        --threshold-hours) THRESHOLD="$2"; shift 2 ;;
        --json) JSON_FLAG="--json"; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "liveness-check: unknown arg: $1" >&2; exit 2 ;;
    esac
done
[ -n "$AGENT" ] || { echo "liveness-check: --agent <name> required" >&2; exit 2; }

# Load own-cloud config + creds (STORAGE_BACKEND, STORAGE_S3_BUCKET, AWS_*) so the
# stale-path boto3 HEAD can reach S3. The engine short-circuits BEFORE any boto3
# call when last_active is already fresh, so on the common path these are loaded
# but unused. Values stay in this process env only — never echoed (guard-724).
if [ -f "$PROJECT_ROOT/.env.local" ]; then
    set -a; . "$PROJECT_ROOT/.env.local"; set +a
fi
BACKEND="${STORAGE_BACKEND:-local}"

# Daemon-routed last_active read (guard-980: never judge liveness from a raw
# local read of a backend-routed store; team-state-read.sh routes through the
# daemon). Empty when the field is absent — the engine treats that as "no
# last_active" and leans on the fresh signal.
LAST_ACTIVE="$(bash "$HERE/team-state-read.sh" --field "agent_status.$AGENT.last_active" 2>/dev/null || true)"

ARGS=(--agent "$AGENT" --last-active "$LAST_ACTIVE" --threshold-hours "$THRESHOLD"
      --world-dir "$WORLD_PATH" --backend "$BACKEND")
[ -n "$JSON_FLAG" ] && ARGS+=("$JSON_FLAG")

python3 "$HERE/liveness_check.py" "${ARGS[@]}"
