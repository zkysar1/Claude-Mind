#!/usr/bin/env bash
# domain-leak-exempt: fixture mirrors the FUNCTIONAL env keys owncloud-endpoint-flip.sh
# reads (STORAGE_S3_ENDPOINT_URL, the COLD_SNAPSHOT_* DR family). Same marker
# precedent as the script under test.
#
# g-372-39 — --revert must RESTORE the pre-flip endpoint, not delete the line.
#
# THE DIRECTION THAT MATTERS. Outcome 2 (a box with NO prior line still ends with
# no line) is the regression half: the fix must not turn a correct delete into a
# spurious write. Both directions are asserted, plus the chain case a single
# flip/revert pair cannot see.
#
# END-TO-END AGAINST THE REAL SCRIPT, not a re-implementation of its logic: the
# script runs under REPO_ROOT=<fixture> with stub companions that let P1-P5 and
# V2/V3 pass. --no-restart keeps the live daemon untouched (there IS one on this
# box) and skips V1, which is the only check that needs a real daemon.
set -uo pipefail
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/core/scripts/owncloud-endpoint-flip.sh"
[ -f "$SCRIPT" ] || { echo "FAIL: script not found at $SCRIPT"; exit 1; }
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     want: %s\n     got : %s\n' "$1" "$2" "$3"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }

mkfix() {  # $1 = initial STORAGE_S3_ENDPOINT_URL value ("" = no line at all)
    FIX="$(mktemp -d)"
    mkdir -p "$FIX/core/scripts" "$FIX/mind_api/src" "$FIX/mind_api/state"
    { echo 'STORAGE_BACKEND=own-cloud'
      echo 'COLD_SNAPSHOT_S3_BUCKET=b'
      echo 'COLD_SNAPSHOT_AWS_ACCESS_KEY_ID=k'
      echo 'COLD_SNAPSHOT_AWS_SECRET_ACCESS_KEY=s'
      [ -n "$1" ] && echo "STORAGE_S3_ENDPOINT_URL=$1"
    } > "$FIX/.env.local"
    chmod 600 "$FIX/.env.local"
    printf 'x = "STORAGE_S3_ENDPOINT_URL"\n' > "$FIX/mind_api/src/__main__.py"
    # cold-snapshot stub: reports live_endpoint from whatever .env.local NOW says,
    # so V2 is a real read-back of the file the script just wrote, not a constant.
    cat > "$FIX/core/scripts/cold-snapshot.sh" <<'STUB'
#!/usr/bin/env bash
v="$(grep -m1 '^STORAGE_S3_ENDPOINT_URL=' "$(dirname "$0")/../../.env.local" 2>/dev/null | cut -d= -f2- || true)"
echo "[target] mode=pinned cold_endpoint=aws-regional live_endpoint=${v:-aws-regional}" >&2
STUB
    printf '#!/usr/bin/env python3\nprint(\x27{"ok": true, "resolved_endpoint": "stub", "ms": 1}\x27)\n' \
        > "$FIX/core/scripts/owncloud-endpoint-probe.py"
    chmod +x "$FIX/core/scripts/cold-snapshot.sh" "$FIX/core/scripts/owncloud-endpoint-probe.py"
    printf '%s' "$FIX"
}
envval() { grep -m1 '^STORAGE_S3_ENDPOINT_URL=' "$1/.env.local" 2>/dev/null | cut -d= -f2- || true; }
run()    { ( cd "$1" && REPO_ROOT="$1" bash "$SCRIPT" "${@:2}" --no-restart ) >/dev/null 2>&1; }

echo "== outcome 1: a box WITH an explicit endpoint gets that exact value back =="
F="$(mkfix 'http://100.76.251.73:9000')"
eq "precondition: the fixture starts on the configured endpoint" "http://100.76.251.73:9000" "$(envval "$F")"
run "$F" --to http://10.0.0.241:9000
eq "flip wrote the new endpoint"                    "http://10.0.0.241:9000"      "$(envval "$F")"
eq "flip captured the pre-flip value"               "http://100.76.251.73:9000"   "$(cat "$F/mind_api/state/owncloud-endpoint-preflip" 2>/dev/null)"
run "$F" --revert
eq "REVERT RESTORED the pre-flip endpoint"          "http://100.76.251.73:9000"   "$(envval "$F")"
eq "revert consumed the capture"                    "absent"                      "$([ -e "$F/mind_api/state/owncloud-endpoint-preflip" ] && echo present || echo absent)"
rm -rf "$F"

echo "== outcome 2 (the regression half): a box with NO prior line still ends with none =="
F="$(mkfix '')"
eq "precondition: no line to begin with"            ""                            "$(envval "$F")"
run "$F" --to http://10.0.0.241:9000
eq "flip wrote the new endpoint"                    "http://10.0.0.241:9000"      "$(envval "$F")"
eq "capture EXISTS and is EMPTY (records 'no line')" "present-empty" \
   "$([ -e "$F/mind_api/state/owncloud-endpoint-preflip" ] && { [ -s "$F/mind_api/state/owncloud-endpoint-preflip" ] && echo present-nonempty || echo present-empty; } || echo absent)"
run "$F" --revert
eq "revert left NO line (no spurious restore)"      ""                            "$(envval "$F")"
eq "the key line is gone from the file entirely"    "0"                           "$(grep -c '^STORAGE_S3_ENDPOINT_URL=' "$F/.env.local" || true)"
rm -rf "$F"

echo "== chain: flip -> flip -> revert returns to what the FIRST flip found =="
F="$(mkfix 'http://100.76.251.73:9000')"
run "$F" --to http://10.0.0.241:9000
run "$F" --to http://10.0.0.99:9000
eq "second flip did NOT overwrite the original capture" "http://100.76.251.73:9000" \
   "$(cat "$F/mind_api/state/owncloud-endpoint-preflip" 2>/dev/null)"
run "$F" --revert
eq "revert returned to the state the caller started from" "http://100.76.251.73:9000" "$(envval "$F")"
rm -rf "$F"

echo "== no capture recorded (older build / hand-edit): historical delete is kept =="
F="$(mkfix 'http://100.76.251.73:9000')"
run "$F" --revert   # no --to ran, so no capture exists
eq "revert with no capture deletes, as before"      ""                            "$(envval "$F")"
rm -rf "$F"

echo "== UNREADABLE capture: refuse, never fall through to the delete =="
# REACHABLE RED. Before this guard the read was `$(... || true)`, so an unreadable
# capture collapsed to restore='' — indistinguishable from the legitimately-empty
# capture of outcome 2 — and the delete branch discarded a real endpoint while
# logging "there was no line before the flip". A directory at the capture path is
# the portable way to force the read to fail: `[ -e ]` is true and `< dir` errors,
# and unlike chmod 000 it still fails for root, which is who runs this suite.
F="$(mkfix 'http://100.76.251.73:9000')"
run "$F" --to http://10.0.0.241:9000
rm -f "$F/mind_api/state/owncloud-endpoint-preflip"
mkdir -p "$F/mind_api/state/owncloud-endpoint-preflip"
( cd "$F" && REPO_ROOT="$F" bash "$SCRIPT" --revert --no-restart ) >/dev/null 2>&1
eq "an unreadable capture makes --revert exit non-zero" "nonzero" \
   "$([ $? -ne 0 ] && echo nonzero || echo zero)"
eq "the endpoint line is LEFT INTACT, not deleted"  "http://10.0.0.241:9000"      "$(envval "$F")"
eq "the unreadable capture is NOT consumed"         "present"                     "$([ -e "$F/mind_api/state/owncloud-endpoint-preflip" ] && echo present || echo absent)"
rm -rf "$F"

printf '\n%s: %d passed, %d failed\n' "$([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
