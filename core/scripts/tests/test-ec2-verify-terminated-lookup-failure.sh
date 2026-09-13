#!/usr/bin/env bash
# test-ec2-verify-terminated-lookup-failure.sh — .
#
# Pins the ONE thing that makes ec2-ephemeral-verify-terminated.sh a usable
# teardown gate: a lookup that FAILS must never read as "verified not-running".
#
# THE CLASS (this goal's subject): a signal that breaks and keeps reporting,
# always toward ALL CLEAR. Measured 2026-09-13 against one live RUNNING
# instance, same second, both ways:
#     healthy lookup -> "STILL ALIVE: ... -> running"          exit 2  (correct)
#     lookup broken  -> "verified not-running: ... -> absent"  exit 0  (defect)
# The empty stdout of a failed describe was folded into the same arm as a
# genuinely-absent instance. This script exists so a later Body that never saw
# the launch can discharge an inherited teardown obligation (guard-5683), so the
# false all-clear closes the obligation while the instance keeps BILLING.
#
# HERMETIC: no AWS, no credentials, no network. `_paths.sh` takes MIND_WORLD
# first (core/scripts/_paths.sh:391-393), so a stub world dir puts our own
# aws-exec.sh on the path the script calls.
#
# CASE 4 IS A POSITIVE CONTROL ON THE ASSERTIONS THEMSELVES (guard-2298 class):
# it runs the PRE-FIX source shape and requires it to FAIL the case-1 assertion.
# Without it a green here would also be green against a script that never looks
# at the exit code, which is exactly the vacuous pass this goal is about.
#
# Pass: prints "TEST PASS", exit 0.  Fail: prints the mismatch, exit 1.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUBJECT="$SCRIPT_DIR/../ec2-ephemeral-verify-terminated.sh"
[ -f "$SUBJECT" ] || { echo "TEST FAIL: subject not found: $SUBJECT"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/world/scripts"

# The stub IS the lookup. STUB_MODE picks which reality the caller meets.
cat > "$TMP/world/scripts/aws-exec.sh" <<'STUB'
#!/usr/bin/env bash
case "${STUB_MODE:-}" in
  fail)       echo "aws: [ERROR]: Could not connect to the endpoint URL" >&2; exit 255 ;;
  running)    echo "running";    exit 0 ;;
  terminated) echo "terminated"; exit 0 ;;
  absent)     echo "None";       exit 0 ;;
  *)          echo "stub: unknown STUB_MODE" >&2; exit 99 ;;
esac
STUB
chmod +x "$TMP/world/scripts/aws-exec.sh"

FAILED=0
IID="i-0aaaaaaaaaaaaaaaa"

run_subject() {  # $1 = STUB_MODE, $2 = script path -> sets OUT / RC
  OUT="$(STUB_MODE="$1" MIND_WORLD="$TMP/world" bash "$2" "$IID" us-east-2 2>&1)"
  RC=$?
}

check() {  # $1 label, $2 expected-rc, $3 substring
  if [ "$RC" != "$2" ]; then
    echo "  FAIL $1: expected rc $2, got $RC — output: $OUT"; FAILED=1; return
  fi
  case "$OUT" in
    *"$3"*) echo "  ok   $1 (rc=$RC)" ;;
    *) echo "  FAIL $1: rc ok but output lacks '$3' — output: $OUT"; FAILED=1 ;;
  esac
}

echo "CASE 1 — lookup FAILS: must refuse to verify, never all-clear"
run_subject fail "$SUBJECT";       check "lookup-failure" 3 "COULD NOT VERIFY"

echo "CASE 2 — instance RUNNING: must stay non-clear"
run_subject running "$SUBJECT";    check "still-alive"    2 "STILL ALIVE"

echo "CASE 3a — instance TERMINATED: the all-clear must still work"
run_subject terminated "$SUBJECT"; check "terminated"     0 "verified not-running"
echo "CASE 3b — describe SUCCEEDS with no instance: idempotent absent preserved"
run_subject absent "$SUBJECT";     check "absent"         0 "verified not-running"

echo "CASE 4 — POSITIVE CONTROL: the pre-fix shape must FAIL case 1"
PREFIX_COPY="$TMP/prefix-verify.sh"
# The defect, reconstructed minimally: stderr discarded, rc never consulted.
cat > "$PREFIX_COPY" <<'OLD'
#!/usr/bin/env bash
set -uo pipefail
IID="${1:?id}"; REGION="${2:-us-east-2}"
CORE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
state="$(bash "$MIND_WORLD/scripts/aws-exec.sh" ec2 describe-instances --region "$REGION" \
  --instance-ids "$IID" --query 'Reservations[0].Instances[0].State.Name' \
  --output text 2>/dev/null | tr -d '[:space:]')"
case "$state" in
  terminated|shutting-down|None|"") echo "verified not-running: $IID -> ${state:-absent}"; exit 0 ;;
  *) echo "STILL ALIVE: $IID -> $state" >&2; exit 2 ;;
esac
OLD
run_subject fail "$PREFIX_COPY"
if [ "$RC" = "0" ]; then
  echo "  ok   pre-fix shape reproduces the defect (rc=0 all-clear on a failed lookup) — case 1 has teeth"
else
  echo "  FAIL positive control: pre-fix shape gave rc=$RC, expected 0. Case 1 may be vacuous."; FAILED=1
fi

if [ "$FAILED" = "0" ]; then echo "TEST PASS"; exit 0; fi
echo "TEST FAIL"; exit 1
