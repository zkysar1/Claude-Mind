#!/usr/bin/env bash
# Verify an ephemeral test EC2 instance is terminated.
# Exists so a teardown obligation registered with background-jobs.sh survives a
# compaction (guard-5683 option b): a later Body that never saw the launch can
# run this against the id recorded in the job's metadata.
# NOTE: keyed on INSTANCE ID, not a tag -- ec2:CreateTags is DENIED for
# user/ayoai-fleet-agent (measured 2026-09-05, ), so an ephemeral
# instance created by a fleet box cannot carry a discriminating tag.
# Exit 0 = terminated/shutting-down/absent, 2 = still alive.
set -uo pipefail
IID="${1:?usage: ec2-ephemeral-verify-terminated.sh <instance-id> [region]}"
REGION="${2:-us-east-2}"
CORE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$CORE_ROOT/_paths.sh"
state="$(bash "$WORLD_PATH/scripts/aws-exec.sh" ec2 describe-instances --region "$REGION" \
  --instance-ids "$IID" --query 'Reservations[0].Instances[0].State.Name' \
  --output text 2>/dev/null | tr -d '[:space:]')"
case "$state" in
  terminated|shutting-down|None|"") echo "verified not-running: $IID -> ${state:-absent}"; exit 0 ;;
  *) echo "STILL ALIVE: $IID -> $state" >&2; exit 2 ;;
esac
