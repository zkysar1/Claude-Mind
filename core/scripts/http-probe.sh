#!/usr/bin/env bash
# core/scripts/http-probe.sh - loud HTTP reachability probe for structured
# preconditions and infra checks.
#
# WHY THIS EXISTS (): a `verification.preconditions[]` entry of type
# command_succeeds may only run a command matching predicate.py's
# ALLOWED_COMMAND_PREFIXES, because that allowlist is a SAFETY BOUNDARY - a
# stored precondition must not be able to run arbitrary shell. A raw
# `curl <url>` therefore cannot be evaluated AT ALL: the refusal used to return
# passed:false, indistinguishable from a measured failure, so the goal was
# filtered out of every selector permanently and both re-probe sweeps
# re-derived the same false every 2h forever. This wrapper is the sanctioned
# allowlisted surface; the fix for such a precondition is a rewrite to
# `bash core/scripts/http-probe.sh <url>`, NEVER a widened allowlist
# (guard-5859: a fail-closed allow-list's refusal is a redirect, not a
# capability wall).
#
# IT IS DELIBERATELY NOT `curl -sf`. `-f` collapses every 4xx/5xx into a bare
# rc=22 with no body, and `-s` hides transport errors; together they are the
# silent-failure form .claude/rules/verify-before-assuming.md rule 4 calls ZERO
# signals rather than one. Everything measured is printed - status, byte count,
# curl's own rc - so a caller reading the output can tell "reachable but 503"
# from "connection refused" from "DNS failure".
#
# Three-way by construction (rb-611): reachable-and-expected / reachable-but-
# unexpected-status / could-not-reach are three distinct printed verdicts. The
# EXIT code is two-way because a precondition consumes an exit code, and it
# fails CLOSED - "could not reach" never reads as success.
#
# Usage:  http-probe.sh <url> [--timeout SECONDS] [--expect-status CODE]
#   --timeout        total seconds allowed (default 10)
#   --expect-status  exact status required (default: any 2xx)
# Exit 0 = response received AND status matched. Exit 1 = otherwise.

set -uo pipefail

URL=""
TIMEOUT=10
EXPECT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)       TIMEOUT="${2:-10}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --expect-status) EXPECT="${2:-}";    shift $(( $# >= 2 ? 2 : 1 )) ;;
    -h|--help)       sed -n '1,40p' "$0"; exit 0 ;;
    -*)              echo "http-probe: unknown flag '$1'" >&2; exit 2 ;;
    *)               if [ -z "$URL" ]; then URL="$1"; else
                       echo "http-probe: unexpected extra argument '$1'" >&2; exit 2
                     fi; shift ;;
  esac
done

if [ -z "$URL" ]; then
  echo "http-probe: missing <url>" >&2
  exit 2
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "http-probe: VERDICT=unverified url=$URL reason=curl-not-installed"
  exit 1
fi

# The body is sunk to a TEMP FILE, not /dev/null. MEASURED 2026-09-05 on
# Windows/Git-Bash under the environment predicate-eval.sh builds:
# `-o /dev/null` returned curl rc=23 "Failure writing output to
# destination" AFTER a clean status=200 -- a reachable service reported as
# unreachable because the SINK failed, not the request. -w prints what was
# actually measured. `-sS` is the SHOW-ERRORS pair, not the silent form: -s only
# drops curl's progress meter (which would otherwise be captured into the
# verdict line) while -S keeps every transport error visible. `-f` is
# deliberately ABSENT - it is the half that collapses every 4xx/5xx into a bare
# rc=22 with no status at all.
BODY_SINK="$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/http-probe.$$")"
trap 'rm -f "$BODY_SINK"' EXIT
OUT="$(curl -sS -o "$BODY_SINK" --max-time "$TIMEOUT" \
        -w 'status=%{http_code} bytes=%{size_download} time=%{time_total}s' \
        "$URL" 2>&1)"
RC=$?

STATUS="$(printf '%s' "$OUT" | sed -n 's/.*status=\([0-9][0-9]*\).*/\1/p' | tail -1)"
[ -n "$STATUS" ] || STATUS=000

if [ "$RC" -ne 0 ] || [ "$STATUS" = "000" ]; then
  echo "http-probe: VERDICT=unreachable url=$URL curl_rc=$RC $OUT"
  exit 1
fi

if [ -n "$EXPECT" ]; then
  if [ "$STATUS" = "$EXPECT" ]; then
    echo "http-probe: VERDICT=ok url=$URL $OUT (expected $EXPECT)"
    exit 0
  fi
  echo "http-probe: VERDICT=unexpected-status url=$URL $OUT (expected $EXPECT)"
  exit 1
fi

case "$STATUS" in
  2??) echo "http-probe: VERDICT=ok url=$URL $OUT"; exit 0 ;;
  *)   echo "http-probe: VERDICT=unexpected-status url=$URL $OUT (expected 2xx)"; exit 1 ;;
esac
