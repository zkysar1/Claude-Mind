#!/usr/bin/env bash
# test_seed_orphan_reporter_branches.sh — pin seed-transplant.sh Step 10.5's
# THREE operator-facing reporter branches (, sq-019 finding from
# ).
#
# WHY THIS EXISTS.  gave do_remove_orphans the full
# archive-before-delete protocol and pinned the ENGINE with 8 unit tests. The
# last hop — engine JSON -> OPERATOR-FACING REPORT — had no test. The branch
# that matters is fail-closed: its whole job is to tell a human that orphans
# were found, the pre-delete archive did NOT verify, and NOTHING was deleted.
# If that branch breaks, the operator sees silence and reads it as a clean
# run: a safety mechanism that works and cannot be observed to have worked.
#
# HOW IT DRIVES PRODUCTION. The reporter is an embedded `py -3 -c "..."` block
# inside seed-transplant.sh. This test EXTRACTS that block from the real file
# via the canonical extractor (core/scripts/extract-embedded-block.sh) rather
# than copying it, so the test cannot drift from the source it pins. It does
# NOT invoke seed-transplant.sh — driving a production write path to probe a
# reporter is guard-1006.
#
# THE UNESCAPE STEP, AND WHY IT IS NOT A HACK. The extractor emits the block
# VERBATIM (correct: for the single-quoted embedded blocks it is designed
# around — note its default --close-line is a single quote — bash passes the
# body through unchanged). This block is DOUBLE-quoted, so bash strips its
# escapes before python ever sees them. We apply that same rule here. Inside a
# bash double-quoted string, backslash is special before exactly four
# characters -- " \ $ ` -- and literal before everything else; the helper
# implements precisely that, and then COMPILES the result. A future edit that
# introduces a shape this does not handle fails loudly at the compile step
# instead of silently testing the wrong program.
#
# ASSERTIONS ARE PROPERTIES, NOT INSTANCES (guard-1726): each branch is pinned
# on its distinguishing meaning -- and on the absence of the other branches'
# meanings -- never on incidental formatting.
set -uo pipefail
cd "$(dirname "$0")/../../.." 2>/dev/null || cd "$(git rev-parse --show-toplevel)"

HOST="core/scripts/seed-transplant.sh"
MARKER='ORPHAN_JSON" | py -3 -c'
FAILED=0
note(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*"; FAILED=1; }

# ---- host must still PARSE (guard-4135) -------------------------------------
# Extraction is marker-based and the extracted body is valid Python on its own,
# so every assertion below can pass while the HOST shell script no longer
# parses -- a break in seed-transplant.sh's own quoting is invisible to a test
# that only ever runs the extracted block. Check the host directly.
if ! bash -n "$HOST" 2>/tmp/seed_host_parse.err; then
  note "FAIL: $HOST does not parse (bash -n). The reporter block below may still"
  note "      extract and pass in isolation — that is exactly the blind spot"
  note "      guard-4135 names. Host parse error:"
  sed 's/^/      /' /tmp/seed_host_parse.err
  exit 1
fi

# ---- extract the live block -------------------------------------------------
BODY_JSON=$(bash core/scripts/extract-embedded-block.sh --grammar shell \
              --file "$HOST" --open-marker "$MARKER" --close-line '"' --json 2>/dev/null)
if [ -z "$BODY_JSON" ]; then
  note "FAIL: could not extract the Step 10.5 reporter block from $HOST."
  note "      The block moved, was renamed, or its quoting changed. This test"
  note "      pins operator-facing output for a delete path — fix the marker,"
  note "      do not delete the test."
  exit 1
fi

# ---- unescape (bash double-quote rule) + compile ----------------------------
PROG=$(printf '%s' "$BODY_JSON" | py -3 -c '
import sys, json
body = json.load(sys.stdin)["body"]
out, i, n = [], 0, len(body)
while i < n:                      # bash: inside "..." backslash is special
    c = body[i]                   # ONLY before " \ $ ` ; literal otherwise
    if c == "\\" and i + 1 < n and body[i+1] in "\"\\$`":
        out.append(body[i+1]); i += 2
    else:
        out.append(c); i += 1
prog = "".join(out)
try:
    compile(prog, "<step-10.5-reporter>", "exec")
except SyntaxError as e:
    sys.stderr.write("EXTRACTED BLOCK DOES NOT COMPILE: %s\n" % e)
    sys.exit(3)
sys.stdout.write(prog)
')
if [ -z "$PROG" ]; then
  fail "extracted reporter block did not compile after unescaping (see stderr above)"
  exit 1
fi

# ---- run one fixture through the real block ---------------------------------
run_branch(){ printf '%s' "$1" | py -3 -c "$PROG" 2>&1; }

F_REMOVED='{"removed":["stale/a.txt","stale/b.txt"],"kept_preserved_count":0,"dry_run":false,"archive":{"archived":true,"verified":true,"count":2,"bytes":4096,"path":"/gy/2026-09-01-orphans","failures":[]}}'
F_FAILCLOSED='{"removed":[],"kept_preserved_count":0,"dry_run":false,"archive":{"archived":true,"verified":false,"count":0,"bytes":0,"path":"/gy/partial-2026-09-01","failures":[{"stage":"copy","path":"stale/a.txt","error":"EACCES"}]}}'
F_NOORPHANS='{"removed":[],"kept_preserved_count":0,"dry_run":false,"archive":{"archived":false,"verified":false,"count":0,"bytes":0,"path":null,"failures":[]}}'

# ---- branch 1: orphans removed, archive verified ----------------------------
O=$(run_branch "$F_REMOVED")
case "$O" in *"removed: 2 orphan(s)"*) ;; *) fail "branch REMOVED: does not state the orphan count. got: $O";; esac
case "$O" in *"stale/a.txt"*) ;; *) fail "branch REMOVED: does not name the removed files. got: $O";; esac
# archive-before-delete.md step 6: an archive nobody can find is not an archive
case "$O" in *"/gy/2026-09-01-orphans"*) ;; *) fail "branch REMOVED: does not print the graveyard path. got: $O";; esac
case "$O" in *"RECEIPT.json"*) ;; *) fail "branch REMOVED: does not print the receipt location. got: $O";; esac
case "$O" in *"no orphans found"*) fail "branch REMOVED: leaked the no-orphans wording. got: $O";; esac

# ---- branch 2: fail-closed (THE one that must never read like a no-op) ------
O=$(run_branch "$F_FAILCLOSED")
case "$O" in *"NOTHING was deleted"*) ;; *) fail "branch FAIL-CLOSED: does not state that nothing was deleted — an operator would read this as a clean run. got: $O";; esac
case "$O" in *"FAILED to verify"*) ;; *) fail "branch FAIL-CLOSED: does not say the archive failed to verify. got: $O";; esac
case "$O" in *"no orphans found"*) fail "branch FAIL-CLOSED: printed the no-orphans wording — this is the silent-success bug the branch exists to prevent. got: $O";; esac
case "$O" in *"removed:"*) fail "branch FAIL-CLOSED: claims removal when nothing was deleted. got: $O";; esac
case "$O" in *"EACCES"*) ;; *) fail "branch FAIL-CLOSED: does not surface the per-file failure reason. got: $O";; esac
case "$O" in *"/gy/partial-2026-09-01"*) ;; *) fail "branch FAIL-CLOSED: does not name the partial archive. got: $O";; esac

# ---- branch 3: no orphans ---------------------------------------------------
O=$(run_branch "$F_NOORPHANS")
case "$O" in *"no orphans found"*) ;; *) fail "branch NO-ORPHANS: does not report a clean run. got: $O";; esac
case "$O" in *"NOTHING was deleted"*|*"WARNING"*) fail "branch NO-ORPHANS: emitted the fail-closed warning on a clean run. got: $O";; esac

# ---- positive control: the fail-closed assertion must be able to FAIL -------
# Goal check: "Test fails if the fail-closed branch's message is removed."
# A guard that cannot fail is not a guard, so prove it here rather than
# asserting it in prose.
MUTANT=$(printf '%s' "$PROG" | py -3 -c '
import sys
p = sys.stdin.read()
old = "NOTHING was deleted (fail-closed)."
if old not in p:
    sys.stderr.write("control: fail-closed wording not found to mutate\n"); sys.exit(3)
sys.stdout.write(p.replace(old, "done."))
')
if [ -z "$MUTANT" ]; then
  fail "positive control: could not build the mutant (fail-closed wording absent from the live block)"
else
  MO=$(printf '%s' "$F_FAILCLOSED" | py -3 -c "$MUTANT" 2>&1)
  case "$MO" in
    *"NOTHING was deleted"*) fail "positive control BROKEN: mutant still emits the wording — these assertions cannot detect its removal";;
    *) note "ok: positive control — removing the fail-closed wording is detected";;
  esac
fi

if [ "$FAILED" -eq 0 ]; then
  note "PASS: seed-transplant Step 10.5 — all three reporter branches pinned (removed / fail-closed / no-orphans) + positive control"
  exit 0
fi
exit 1
