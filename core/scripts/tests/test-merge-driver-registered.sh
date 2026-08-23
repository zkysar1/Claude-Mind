#!/usr/bin/env bash
# test-merge-driver-registered.sh — .
#
# WHAT THIS PINS. check-merge-driver-registered.sh asserts that the merge driver
# every tracked record store RESOLVES to is actually REGISTERED in this clone's
# .git/config. That registration is written by install-git-hooks.sh and git
# config is not version-controlled, so a clone where the installer never ran has
# correct-looking .gitattributes pointing at a driver that does not exist.
#
# The load-bearing case is CASE 7, and without it this file would be a test that
# passes either way (guard-1220): it runs the SAME broken fixture through the
# pre-existing sibling probe check-merge-driver-drift.sh and asserts that the
# sibling reports OK. That is the whole reason this second check exists — git
# check-attr is answered from the attributes files alone and has no knowledge of
# git config, so a check-attr-only probe cannot see this failure class.
#
# Hermetic: every case builds a throwaway git repo in a temp dir and copies the
# script under test into it so `_paths.sh` resolves PROJECT_ROOT there. NOTHING
# touches the live repo or its .git/config.
#
# Run: bash core/scripts/tests/test-merge-driver-registered.sh
set -uo pipefail
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SUT="$PROJECT_ROOT/core/scripts/check-merge-driver-registered.sh"
SIB="$PROJECT_ROOT/core/scripts/check-merge-driver-drift.sh"
[ -f "$SUT" ] || { echo "FAIL: script under test not found at $SUT"; exit 1; }
[ -f "$SIB" ] || { echo "FAIL: sibling probe not found at $SIB"; exit 1; }

PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); echo "  PASS  $1"; }
no(){ FAIL=$((FAIL+1)); echo "  FAIL  $1"; echo "        got: $2"; }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "$2 (want $3)"; }

# mkrepo <dir> <register-driver: yes|no> [attr-line]
mkrepo(){
    local d="$1" reg="$2"
    local attr="${3:-agents/*/journal.jsonl merge=ayoai-ledger}"
    mkdir -p "$d/core/scripts" "$d/agents/alpha"
    cp "$PROJECT_ROOT/core/scripts/_paths.sh" "$SUT" "$SIB" "$d/core/scripts/"
    printf '%s\n' "$attr" > "$d/.gitattributes"
    printf '{"id":1}\n' > "$d/agents/alpha/journal.jsonl"
    ( cd "$d" && git init -q . && git add -A >/dev/null 2>&1 &&
      git -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1 )
    if [ "$reg" = "yes" ]; then
        ( cd "$d" && git config merge.ayoai-ledger.driver \
            'bash core/scripts/git-merge-ayoai-ledger.sh %O %A %B %P' )
    fi
}
probe(){ ( cd "$1" && bash core/scripts/check-merge-driver-registered.sh --json 2>/dev/null ); }
probe_err(){ ( cd "$1" && bash core/scripts/check-merge-driver-registered.sh 2>&1 >/dev/null ); }
jfield(){ py -3 -c '
import sys,json
try: d=json.loads(sys.stdin.read() or "{}")
except Exception: d={}
v=d.get(sys.argv[1]); print("" if v is None else v)' "$1"; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

echo "== VERIFY (2): a correctly installed clone is SILENT =="
mkrepo "$T/good" yes
OUT=$( cd "$T/good" && bash core/scripts/check-merge-driver-registered.sh 2>&1 ); RC=$?
chk "registered driver -> exit 0"            "$RC" "0"
chk "  and emits NOTHING without --json"     "$(printf '%s' "$OUT" | wc -c | tr -d ' ')" "0"
chk "  --json still reports clean"           "$(probe "$T/good" | jfield status)" "clean"

echo "== VERIFY (1): an UNREGISTERED driver fails loudly and names the fix =="
mkrepo "$T/bad" no
OUT=$(probe "$T/bad"); RC=$?
chk "unregistered driver -> exit 1"          "$RC" "1"
chk "  status is unregistered"               "$(printf '%s' "$OUT" | jfield status)" "unregistered"
chk "  detail names the driver"              "$(printf '%s' "$OUT" | jfield detail | grep -c 'merge.ayoai-ledger.driver is NOT configured')" "1"
ERR=$(probe_err "$T/bad")
# Match the FIX line specifically, not the bare script name: the CAUSE paragraph
# names install-git-hooks.sh too, so a bare -c returns 2 and an =1 assertion
# fails on correct output.
chk "  stderr carries the FIX line"          "$(printf '%s' "$ERR" | grep -c '^FIX: *bash core/scripts/install-git-hooks.sh$')" "1"

echo "== CASE 7 (the reason this check exists): the SIBLING probe reads OK on the SAME repo =="
# check-attr is answered from .gitattributes alone, so the pre-existing drift
# probe cannot see an unregistered driver. If this ever starts failing, the two
# checks have converged and one of them is redundant.
SIBOUT=$( cd "$T/bad" && bash core/scripts/check-merge-driver-drift.sh --json 2>/dev/null ); SIBRC=$?
chk "sibling drift probe exits 0"            "$SIBRC" "0"
chk "  sibling reports clean on a BROKEN clone" "$(printf '%s' "$SIBOUT" | jfield status)" "clean"
# ...and git itself agrees the attribute resolves, which is what makes it invisible.
ATTR=$( cd "$T/bad" && git check-attr merge -- agents/alpha/journal.jsonl )
chk "  check-attr still says ayoai-ledger"   "$(printf '%s' "$ATTR" | grep -c 'merge: ayoai-ledger')" "1"

echo "== --repo targets the tree being merged, not the caller's cwd =="
# iteration-push.sh honours --repo / ITERATION_PUSH_REPO and merges THAT tree.
# Run from the GOOD repo but point at the BAD one: a script that ignored --repo
# would read its own cwd and report clean, which is a check reporting OK about a
# repo it never looked at.
OUT=$( cd "$T/good" && bash core/scripts/check-merge-driver-registered.sh --json --repo "$T/bad" 2>/dev/null ); RC=$?
chk "--repo from a clean cwd -> exit 1"      "$RC" "1"
chk "  status follows the TARGET repo"       "$(printf '%s' "$OUT" | jfield status)" "unregistered"
# ...and the reverse direction, so this is not a test that passes either way.
OUT=$( cd "$T/bad" && bash core/scripts/check-merge-driver-registered.sh --json --repo "$T/good" 2>/dev/null ); RC=$?
chk "--repo from a dirty cwd -> exit 0"      "$RC" "0"
chk "  status follows the TARGET repo"       "$(printf '%s' "$OUT" | jfield status)" "clean"

echo "== the inverse: registered but referenced by nothing =="
mkrepo "$T/orphan" yes "agents/*/journal.jsonl merge=union"
OUT=$(probe "$T/orphan"); RC=$?
chk "orphan registration -> exit 1"          "$RC" "1"
chk "  detail says nothing resolves to it"   "$(printf '%s' "$OUT" | jfield detail | grep -c 'NO tracked path resolves to it')" "1"

echo "== built-in merge values need no driver =="
mkrepo "$T/builtin" no "agents/*/journal.jsonl merge=union"
OUT=$(probe "$T/builtin"); RC=$?
chk "merge=union alone -> exit 0"            "$RC" "0"
chk "  custom_drivers is 0"                  "$(printf '%s' "$OUT" | jfield custom_drivers)" "0"

echo "== documented quiet paths stay quiet =="
E="$T/empty"; mkdir -p "$E/core/scripts"
cp "$PROJECT_ROOT/core/scripts/_paths.sh" "$SUT" "$E/core/scripts/"
( cd "$E" && git init -q . )
OUT=$(probe "$E"); RC=$?
chk "no tracked files -> exit 0"             "$RC" "0"
chk "  and emits nothing"                    "$(printf '%s' "$OUT" | wc -c | tr -d ' ')" "0"

NG="$T/nongit"; mkdir -p "$NG/core/scripts"
cp "$PROJECT_ROOT/core/scripts/_paths.sh" "$SUT" "$NG/core/scripts/"
( cd "$NG" && bash core/scripts/check-merge-driver-registered.sh >/dev/null 2>&1 )
chk "non-git checkout -> exit 0"             "$?" "0"

echo
echo "merge-driver registration: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
