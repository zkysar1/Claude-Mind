#!/usr/bin/env bash
# Verify an ephemeral test EC2 instance is terminated.
# Exists so a teardown obligation registered with background-jobs.sh survives a
# compaction (guard-5683 option b): a later Body that never saw the launch can
# run this against the id recorded in the job's metadata.
# NOTE: keyed on INSTANCE ID, not a tag -- ec2:CreateTags is DENIED for
# user/ayoai-fleet-agent (measured 2026-09-05, ), so an ephemeral
# instance created by a fleet box cannot carry a discriminating tag.
# Exit 0 = terminated/shutting-down/absent, 2 = still alive,
#      3 = COULD NOT VERIFY (the lookup itself failed -- see below).
set -uo pipefail
IID="${1:?usage: ec2-ephemeral-verify-terminated.sh <instance-id> [region]}"
REGION="${2:-us-east-2}"
CORE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$CORE_ROOT/_paths.sh"
# THE rc IS THE DISCRIMINATOR, NOT EMPTINESS (, measured 2026-09-13).
# The lookup returns EMPTY stdout on a transport failure (rc 255), a bad id
# (rc 254), and a credential/IAM refusal alike, and the `""` arm below used to
# fold every one of those into `exit 0 verified not-running`. Measured against
# one live RUNNING instance, same second, both ways:
#   healthy lookup -> "STILL ALIVE: ... -> running"          exit 2  (correct)
#   lookup broken  -> "verified not-running: ... -> absent"  exit 0  (the defect)
# That is the failure direction that COSTS: this script exists so a later Body
# that never saw the launch can discharge an inherited teardown obligation
# (guard-5683), so a false all-clear here closes the obligation while the
# instance keeps billing. stderr is no longer discarded -- a verdict must not be
# built on a silenced error (guard-1675) -- and guard-3299 puts the fix HERE,
# at the caller, because the failing and empty-success cases differ only in rc.
state="$(bash "$WORLD_PATH/scripts/aws-exec.sh" ec2 describe-instances --region "$REGION" \
  --instance-ids "$IID" --query 'Reservations[0].Instances[0].State.Name' \
  --output text | tr -d '[:space:]')"
state_rc=$?
if [ "$state_rc" -ne 0 ]; then
  echo "COULD NOT VERIFY: $IID -> lookup failed (rc=$state_rc). Termination NOT confirmed; the instance may still be BILLING. Re-run before closing the teardown obligation (guard-795)." >&2
  exit 3
fi
# Reached only with a lookup that SUCCEEDED, which is what makes the `""|None`
# absent verdict honest rather than a fallback.
case "$state" in
  terminated|shutting-down|None|"") echo "verified not-running: $IID -> ${state:-absent}"; exit 0 ;;
  *) echo "STILL ALIVE: $IID -> $state" >&2; exit 2 ;;
esac
