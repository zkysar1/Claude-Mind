#!/usr/bin/env bash
# test-merge-driver-drift-agent-enumeration.sh — .
#
# THE REGRESSION THIS PINS. check-merge-driver-drift.sh enumerated agent dirs by
# `agents_root()/*/local-paths.conf`. On any given box only the RESIDENT agent
# has a conf, so the probe silently degraded to the bound-agent-only scan its own
# comment explicitly rules out. Measured on cc-07: 5 agent dirs, 1 conf, and each
# of the other four carried 5 tracked ledger files resolving to ayoai-ledger —
# 5 of 25 paths checked, 20 invisible, reported as `OK`.
#
# The originating cc-04 override was scoped to `agents/alpha/*.jsonl` where alpha
# IS resident, which is the only reason it was ever caught. Case 2 below is the
# same override aimed at a NON-resident agent: it FAILS under the conf
# enumerator and PASSES under the dir enumerator, which is what makes this a
# two-way pin rather than a test that passes either way (guard-1220).
#
# Hermetic: every case builds a throwaway git repo in a temp dir with its own
# .gitattributes and .git/info/attributes, and copies the script under test into
# it so `_paths.sh` resolves PROJECT_ROOT there. NOTHING touches the live repo —
# which matters more than usual here, because the artifact under test is a real
# `.git/info/attributes` override, i.e. the exact hazard the probe detects.
#
# Run: bash core/scripts/tests/test-merge-driver-drift-agent-enumeration.sh
set -uo pipefail
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SUT="$PROJECT_ROOT/core/scripts/check-merge-driver-drift.sh"
[ -f "$SUT" ] || { echo "FAIL: script under test not found at $SUT"; exit 1; }

PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); echo "  PASS  $1"; }
no(){ FAIL=$((FAIL+1)); echo "  FAIL  $1"; echo "        got: $2"; }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "$2 (want $3)"; }

LEDGERS="journal experience experience-archive changelog aspirations"

# mkrepo <dir> <conf-bearing-agents-csv> <all-agents-csv>
mkrepo(){
    local d="$1" confs="$2" all="$3" a l
    mkdir -p "$d/core/scripts"
    cp "$PROJECT_ROOT/core/scripts/_paths.sh" "$PROJECT_ROOT/core/scripts/check-merge-driver-drift.sh" "$d/core/scripts/"
    : > "$d/.gitattributes"
    for l in $LEDGERS; do echo "agents/*/$l.jsonl merge=ayoai-ledger" >> "$d/.gitattributes"; done
    for a in ${all//,/ }; do
        mkdir -p "$d/agents/$a"
        for l in $LEDGERS; do : > "$d/agents/$a/$l.jsonl"; done
    done
    for a in ${confs//,/ }; do : > "$d/agents/$a/local-paths.conf"; done
    ( cd "$d" && git init -q . && git add -A >/dev/null 2>&1 &&
      git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1 )
}
probe(){ ( cd "$1" && bash core/scripts/check-merge-driver-drift.sh --json 2>/dev/null ); }
jfield(){ py -3 -c '
import sys,json
try: d=json.loads(sys.stdin.read() or "{}")
except Exception: d={}
v=d.get(sys.argv[1]); print("" if v is None else v)' "$1"; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

echo "== clean repo =="
mkrepo "$T/clean" alpha alpha,bravo,echo
OUT=$(probe "$T/clean"); RC=$?
chk "clean repo exits 0"                     "$RC" "0"
chk "  status is clean"                      "$(printf '%s' "$OUT" | jfield status)" "clean"
# 3 agents x 5 ledgers. Under the conf enumerator this was 5 — the whole defect.
chk "  checks ALL agent dirs, not just conf" "$(printf '%s' "$OUT" | jfield checked)" "15"

echo "== THE REGRESSION PIN: drift on a NON-resident agent (no local-paths.conf) =="
mkrepo "$T/nonres" alpha alpha,bravo,echo
printf 'agents/bravo/*.jsonl merge=union\n' > "$T/nonres/.git/info/attributes"
OUT=$(probe "$T/nonres"); RC=$?
chk "detected -> exit 1"                     "$RC" "1"
chk "  status is drift"                      "$(printf '%s' "$OUT" | jfield status)" "drift"
chk "  and it names bravo"                   "$(printf '%s' "$OUT" | jfield detail | grep -c 'agents/bravo/aspirations.jsonl: merge=union')" "1"
chk "  count still covers every agent"       "$(printf '%s' "$OUT" | jfield checked)" "15"

echo "== the resident-agent case must NOT regress (it is what cc-04 caught) =="
mkrepo "$T/res" alpha alpha,bravo
printf 'agents/alpha/*.jsonl merge=union\n' > "$T/res/.git/info/attributes"
OUT=$(probe "$T/res"); RC=$?
chk "resident-agent drift still detected"    "$RC" "1"
chk "  and it names alpha"                   "$(printf '%s' "$OUT" | jfield detail | grep -c 'agents/alpha/journal.jsonl: merge=union')" "1"

echo "== a box where NO agent has a conf is now covered (was totally blind) =="
mkrepo "$T/noconf" "" bravo,echo
printf 'agents/echo/*.jsonl merge=union\n' > "$T/noconf/.git/info/attributes"
OUT=$(probe "$T/noconf"); RC=$?
chk "no-conf box still probes"               "$(printf '%s' "$OUT" | jfield checked)" "10"
chk "  and detects drift"                    "$RC" "1"

echo '== "checked" must count REAL ledgers, or it stops being an anti-vacuity signal =='
# git check-attr is pattern-only and answers for paths that do not exist, so
# without the existence guard a stray dir under agents/ inflates `checked` while
# adding no coverage — a count that rises when coverage does not.
mkrepo "$T/stray" alpha alpha
mkdir -p "$T/stray/agents/not-an-agent/session"
OUT=$(probe "$T/stray")
chk "stray dir does not inflate the count"   "$(printf '%s' "$OUT" | jfield checked)" "5"
# ...and a real agent missing one ledger counts 4, not 5. Must RE-PROBE: asserting
# against the $OUT captured before the removal would pass no matter what the
# script does, which is the vacuous shape this whole file exists to reject.
rm -f "$T/stray/agents/alpha/changelog.jsonl"
OUT=$(probe "$T/stray")
chk "absent ledger drops the count to 4"     "$(printf '%s' "$OUT" | jfield checked)" "4"

echo "== documented quiet paths stay quiet =="
mkrepo "$T/empty" "" ""
OUT=$(probe "$T/empty"); RC=$?
chk "nothing resolvable -> exit 0, silent"   "$RC" "0"
chk "  and emits nothing"                    "$(printf '%s' "$OUT" | wc -c | tr -d ' ')" "0"

NG="$T/nongit"; mkdir -p "$NG/core/scripts"
cp "$PROJECT_ROOT/core/scripts/_paths.sh" "$PROJECT_ROOT/core/scripts/check-merge-driver-drift.sh" "$NG/core/scripts/"
( cd "$NG" && bash core/scripts/check-merge-driver-drift.sh >/dev/null 2>&1 )
chk "non-git checkout -> exit 0"             "$?" "0"

echo
echo "merge-driver-drift agent enumeration: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
