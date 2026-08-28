#!/usr/bin/env bash
# test_temp_drain_purge.sh — regression test for  (agent-hang fix)
# PLUS  (drained/ age-based GC + stray-dir cleanup lanes).
# Unit-tests the assert_safe_temp_dir guard in temp-drain-purge.sh: hostile
# inputs (empty agent_dir/project_root/temp_dir, non-absolute path, /temp,
# outside-project-root, wrong basename) MUST be REFUSED (rc 1); only a real
# "$PROJECT_ROOT/.../temp" passes (rc 0). This guarantees the agent-hang class
# — an unguarded rm on a possibly-empty variable path triggering the Claude
# Code dangerous-rm dialog — cannot recur through this canonical purge helper.
# Also unit-tests the two extracted lane functions (gc_drained_archive,
# cleanup_stray_dirs) against a synthetic temp/: age-gating, dir preservation,
# fresh-item survival, and the empty-temp_dir no-delete guard.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/../temp-drain-purge.sh"

if [ ! -f "$HELPER" ]; then
  echo "FAIL: helper not found at $HELPER"; exit 1
fi

# Source the helper — main() does NOT run (guarded on BASH_SOURCE==0), so the
# guard function is callable in isolation with hostile inputs.
# shellcheck disable=SC1090
source "$HELPER"

PR="/opt/example/root"     # synthetic project root
AD="$PR/agents/alpha"      # synthetic agent dir
GOOD="$AD/temp"            # the one safe shape

fails=0
# check <description> <expected_rc> <temp_dir> <project_root> <agent_dir>
check() {
  local desc="$1" exp="$2" td="$3" pr="$4" ad="$5" rc
  assert_safe_temp_dir "$td" "$pr" "$ad" 2>/dev/null; rc=$?
  if [ "$rc" -eq "$exp" ]; then
    echo "  [PASS] $desc (rc=$rc)"
  else
    echo "  [FAIL] $desc — expected rc=$exp, got rc=$rc"
    fails=$((fails+1))
  fi
}

echo "assert_safe_temp_dir guard cases:"
check "empty agent_dir REFUSED"           1 "$GOOD" "$PR" ""
check "empty project_root REFUSED"        1 "$GOOD" ""    "$AD"
check "empty temp_dir REFUSED"            1 ""      "$PR"  "$AD"
check "non-absolute temp_dir REFUSED"     1 "relative/temp" "$PR" "$AD"
check "/temp (empty-AGENT_DIR shape) REFUSED" 1 "/temp" "$PR" "$AD"
check "outside-project-root REFUSED"      1 "/other/agents/alpha/temp" "$PR" "$AD"
check "wrong basename (scratch) REFUSED"  1 "$AD/scratch" "$PR" "$AD"
check "valid temp dir PASSES"             0 "$GOOD" "$PR" "$AD"

echo "executed dry-run smoke:"
out="$(bash "$HELPER" --dry-run 2>/dev/null)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '"temp_dir"'; then
  echo "  [PASS] dry-run rc=0 + JSON with temp_dir"
else
  echo "  [FAIL] dry-run rc=$rc out=$out"; fails=$((fails+1))
fi
# : main() must emit the new lane fields (else a downstream JSON
# consumer of drained_gc_*/stray_* silently sees nulls).
for k in '"drained_gc_would_purge"' '"stray_would_purge"' '"drained_age_days"' '"citation_lookup"' '"drained_gc_files"' '"stray_preserved_git"' '"stray_preserved_git_dirs"' '"watermark_source"'; do
  if printf '%s' "$out" | grep -q "$k"; then
    echo "  [PASS] dry-run JSON carries $k"
  else
    echo "  [FAIL] dry-run JSON missing $k — out=$out"; fails=$((fails+1))
  fi
done

#  main()-level wiring. The function-level cases above prove the
# EXEMPTION and the LIST; they cannot prove main() actually READS them, and the
# two ways that wiring silently breaks both yield a well-formed, empty
# "drained_gc_files":[] — indistinguishable from an honestly-empty drained/ dir.
# So assert on a fixture that GUARANTEES a non-empty list rather than on the live
# temp dir, whose drained/ may legitimately have nothing aged past 30d
# (measured on cc-04 at authoring time: 0 — a real zero that would have made a
# live-dir assertion pass vacuously forever).
#   (1) `gc_count="$(gc_drained_archive ...)"` forks a subshell, so the
#       GC_DRAINED_FILES global set inside is discarded (this was a real bug in
#       the first draft of the fix, caught before commit).
#   (2) the JSON builder could emit [] regardless of the global.
# Driven via MIND_AGENT_DIR, the documented test-only agent-dir override
# (_paths.sh:163) — plain PROJECT_ROOT/AGENT_DIR env vars do NOT work here
# because main() sources _paths.sh, which recomputes both from the real repo
# (measured: the fixture resolved to the LIVE agents/wiretest path and the test
# read the no-temp-dir branch instead). The fixture must sit UNDER the real
# PROJECT_ROOT to clear assert_safe_temp_dir guard 5, and its basename must be
# "temp" for guard 6 — hence a nested temp/ inside this agent's own temp store,
# which also keeps it self-cleaning and out of live agents/.
echo "main() lane-2 file-list wiring (g-306-102):"
TW="$(cd "$SCRIPT_DIR/../.." && pwd)/agents/${MIND_AGENT:-alpha}/temp/.wiretest-$$"
mkdir -p "$TW/temp/drained"
: > "$TW/temp/drained/wire-old.md"
touch -d '40 days ago' "$TW/temp/drained/wire-old.md"
w_out="$(MIND_AGENT_DIR="$TW" bash "$HELPER" --dry-run 2>/dev/null)"
if printf '%s' "$w_out" | grep -q '"drained_gc_files":\["wire-old.md"\]'; then
  echo "  [PASS] main() propagates the lane-2 basename into drained_gc_files"
else
  echo "  [FAIL] main() lane-2 list empty/wrong (subshell or builder regression) — out=$w_out"
  fails=$((fails+1))
fi
rm -rf "$TW"

echo "no-temp-dir exit path (g-115-2955) — schema parity with main path:"
# The dry-run smoke above runs against the LIVE agent temp/ (which exists), so it
# exercises the MAIN JSON path only; the soft-guard no-op branch (temp dir absent —
# a fresh agent) went untested. Drive it deterministically via a nonexistent agent:
# assert_safe_temp_dir passes the valid-SHAPE path, then `[ ! -d ]` fires the no-op.
# That branch MUST emit the SAME lane-field schema as the main path — else a
# strict-field JSON consumer KeyErrors on a fresh agent (the fresh-eyes finding on
#  that this test locks in). Hermetic + side-effect-free: the helper only
# READS temp-dir existence, so no agent dir is created for the sentinel name.
nt_out="$(MIND_AGENT=nonexistent-drain-test-zzz bash "$HELPER" --dry-run 2>/dev/null)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$nt_out" | grep -q '"note":"temp dir does not exist"'; then
  echo "  [PASS] no-temp-dir rc=0 + soft-guard no-op JSON"
else
  echo "  [FAIL] no-temp-dir rc=$rc out=$nt_out"; fails=$((fails+1))
fi
# All 5 lane fields the fresh-eyes finding flagged as omitted from THIS branch MUST
# be present so both exit paths share ONE schema ().
for k in '"drained_gc_purged"' '"drained_gc_would_purge"' '"stray_purged"' '"stray_would_purge"' '"drained_age_days"' '"citation_lookup"' '"stray_preserved_git"' '"stray_preserved_git_dirs"' '"watermark_source"'; do
  if printf '%s' "$nt_out" | grep -q "$k"; then
    echo "  [PASS] no-temp-dir JSON carries $k"
  else
    echo "  [FAIL] no-temp-dir JSON missing $k — out=$nt_out"; fails=$((fails+1))
  fi
done

echo "main() third-class watermark wiring (file default + flag override — guard-1482):"
# The predicate-level watermark pins below prove the SSOT function; they cannot
# prove main() actually RESOLVES the file/flag precedence (guard-1482: an
# always-passed test argument leaves the code's DEFAULT untested — the default
# here is reading temp/.drain-watermark, which is what every production caller
# uses). Same MIND_AGENT_DIR fixture idiom as the lane-2 wiretest above.
WT="$(cd "$SCRIPT_DIR/../.." && pwd)/agents/${MIND_AGENT:-alpha}/temp/.wmtest-$$"
mkdir -p "$WT/temp"
printf 'x\n' > "$WT/temp/wmtest-orphan.jsonl"
touch -d '200 minutes ago' "$WT/temp/wmtest-orphan.jsonl"
# (a) no watermark file, no flag → absent; third class exempt (fail-closed)
wm1="$(MIND_AGENT_DIR="$WT" bash "$HELPER" --dry-run 2>/dev/null)"
lcheck "wm absent: watermark_source=absent"      yes "$(printf '%s' "$wm1" | grep -q '"watermark_source":"absent"' && echo yes || echo no)"
lcheck "wm absent: third-class file exempt"      no  "$(printf '%s' "$wm1" | grep -qF 'wmtest-orphan.jsonl' && echo yes || echo no)"
# (b) THE DEFAULT PATH: watermark FILE present → source=file, third class purgeable
date +%Y-%m-%dT%H:%M:%S > "$WT/temp/.drain-watermark"
wm2="$(MIND_AGENT_DIR="$WT" bash "$HELPER" --dry-run 2>/dev/null)"
lcheck "wm file: watermark_source=file"          yes "$(printf '%s' "$wm2" | grep -q '"watermark_source":"file"' && echo yes || echo no)"
lcheck "wm file: third-class file purgeable"     yes "$(printf '%s' "$wm2" | grep -qF 'wmtest-orphan.jsonl' && echo yes || echo no)"
lcheck "wm file: marker NOT reported unmanaged"  no  "$(printf '%s' "$wm2" | grep -qF '".drain-watermark"' && echo yes || echo no)"
# (c) flag 'none' overrides the file → disabled, exempt again
wm3="$(MIND_AGENT_DIR="$WT" bash "$HELPER" --dry-run --third-class-watermark none 2>/dev/null)"
lcheck "wm none: watermark_source=disabled"      yes "$(printf '%s' "$wm3" | grep -q '"watermark_source":"disabled"' && echo yes || echo no)"
lcheck "wm none: third-class file exempt again"  no  "$(printf '%s' "$wm3" | grep -qF 'wmtest-orphan.jsonl' && echo yes || echo no)"
# (d) garbage in the marker file → invalid, fail-closed exempt
echo garbage-not-a-timestamp > "$WT/temp/.drain-watermark"
wm4="$(MIND_AGENT_DIR="$WT" bash "$HELPER" --dry-run 2>/dev/null)"
lcheck "wm invalid: watermark_source=invalid"    yes "$(printf '%s' "$wm4" | grep -q '"watermark_source":"invalid"' && echo yes || echo no)"
lcheck "wm invalid: third-class file exempt"     no  "$(printf '%s' "$wm4" | grep -qF 'wmtest-orphan.jsonl' && echo yes || echo no)"
rm -rf "$WT"

echo "lane functions (g-115-2948) — drained/ GC + stray-dir cleanup:"
lcheck() {  # lcheck <desc> <expected> <actual>
  if [ "$3" = "$2" ]; then echo "  [PASS] $1"; else echo "  [FAIL] $1 — expected '$2', got '$3'"; fails=$((fails+1)); fi
}
T2="$(mktemp -d)"
mkdir -p "$T2/temp/drained" "$T2/temp/stale-dir" "$T2/temp/fresh-dir"
: > "$T2/temp/drained/old.md";    touch -d '40 days ago' "$T2/temp/drained/old.md"
: > "$T2/temp/drained/recent.md"  # today — must survive the 30-day GC
echo x > "$T2/temp/stale-dir/leftover.txt"
touch -d '3 hours ago' "$T2/temp/stale-dir/leftover.txt" "$T2/temp/stale-dir"
# fresh-dir left at now-mtime — must survive the 120-min stray guard

# Lane 2 — drained/ age-based GC (>30d)
lcheck "gc_drained dry-run counts 1 stale"        1          "$(gc_drained_archive "$T2/temp/drained" 30 1)"
lcheck "gc_drained dry-run deleted nothing"       2          "$(ls "$T2/temp/drained" | grep -c .)"
lcheck "gc_drained real purges 1"                 1          "$(gc_drained_archive "$T2/temp/drained" 30 0)"
lcheck "gc_drained kept recent.md only"           recent.md  "$(ls "$T2/temp/drained")"
lcheck "gc_drained preserved drained/ dir"        yes        "$([ -d "$T2/temp/drained" ] && echo yes || echo no)"
lcheck "gc_drained missing dir -> 0"              0          "$(gc_drained_archive "$T2/temp/nope" 30 0)"
# (T2 is NOT a git work-tree, so the whole block above is ALSO the not-a-repo
# pin for the  tracked-file filter: nothing is tracked there, and the
# age GC must proceed exactly as before — conflating not-a-repo with
# ls-files-errored would have failed every count above.)

# Lane 2 tracked-file survival (, back-ported from ZDS): a git-TRACKED
# file under drained/ is durable BY the invariant the deployment's .gitignore
# reasons from — the age GC must never delete it (206 tracked prod deliverables
# were scheduled for a same-day mass deletion because provisioning gave them one
# shared mtime). Untracked siblings still age out, and the count excludes kept
# tracked files.
TG="$(mktemp -d)"
# Windows/MSYS: mktemp yields /tmp/... while `git rev-parse --show-toplevel`
# returns the C:/... form, so the function's repo-relative prefix strip cannot
# match — a SANDBOX artifact, not a production shape (callers pass paths in the
# same form git returns, and on Windows boxes temp/ is fully gitignored anyway,
# so the tracked-filter is load-bearing only on Linux/local-backend). Normalize
# the sandbox to git's form so this pins the real semantics on every platform.
command -v cygpath >/dev/null 2>&1 && TG="$(cygpath -m "$TG")"
mkdir -p "$TG/temp/drained"
git -C "$TG" init -q 2>/dev/null
: > "$TG/temp/drained/tracked-old.md";   touch -d '40 days ago' "$TG/temp/drained/tracked-old.md"
: > "$TG/temp/drained/untracked-old.md"; touch -d '40 days ago' "$TG/temp/drained/untracked-old.md"
git -C "$TG" add temp/drained/tracked-old.md 2>/dev/null
lcheck "gc_drained tracked: dry-run counts untracked only" 1   "$(gc_drained_archive "$TG/temp/drained" 30 1)"
lcheck "gc_drained tracked: real purges untracked only"    1   "$(gc_drained_archive "$TG/temp/drained" 30 0)"
lcheck "gc_drained tracked: tracked-old.md SURVIVED"       yes "$([ -f "$TG/temp/drained/tracked-old.md" ] && echo yes || echo no)"
lcheck "gc_drained tracked: untracked-old.md purged"       no  "$([ -f "$TG/temp/drained/untracked-old.md" ] && echo yes || echo no)"
rm -rf "$TG"

# ── Lane 2 CITED exemption + file list () ────────────────────────────
# Before this, a citation protected an artifact in temp/ (Lane 1, ) but
# NOT once /drain-temp archived it into temp/drained/ — protection was a property
# of WHICH DIRECTORY the file sat in, not of the artifact. Lane 2 also returned a
# bare COUNT, so durability-property-check.py had nothing to intersect and was
# Lane-1-only BY CONSTRUCTION; an exemption nobody can verify is the
# conditionally-active-mechanism pattern asp-306 exists to kill.
TC="$(mktemp -d)"
mkdir -p "$TC/temp/drained"
: > "$TC/temp/drained/cited-evidence.md";   touch -d '40 days ago' "$TC/temp/drained/cited-evidence.md"
: > "$TC/temp/drained/uncited-old.md";      touch -d '40 days ago' "$TC/temp/drained/uncited-old.md"

# POSITIVE CONTROL, and it must come FIRST: with NO cited args the lane must
# still see BOTH files and publish BOTH basenames. Without this, a
# GC_DRAINED_FILES that is empty for a MECHANICAL reason (e.g. the global lost to
# a `$(...)` subshell) would make every exemption assertion below pass vacuously
# — the list would be empty either way and "cited file absent from the list"
# would prove nothing. Asserting the list is NON-empty here is what gives the
# assertions their meaning.
lcheck "gc_drained cited: no-cited-args dry-run counts BOTH" 2 "$(gc_drained_archive "$TC/temp/drained" 30 1)"
gc_drained_archive "$TC/temp/drained" 30 1 >/dev/null
lcheck "gc_drained cited: CONTROL list is non-empty (2)"     2 "$(printf '%s' "$GC_DRAINED_FILES" | grep -c . || true)"
lcheck "gc_drained cited: CONTROL list names the cited file" yes \
  "$(printf '%s\n' "$GC_DRAINED_FILES" | grep -qFx 'cited-evidence.md' && echo yes || echo no)"
lcheck "gc_drained cited: GC_DRAINED_COUNT global matches"    2 "$GC_DRAINED_COUNT"

# THE FIX: pass the basename as cited — it must be exempt, absent from the list,
# and survive a REAL (non-dry) run.
lcheck "gc_drained cited: dry-run counts uncited only"       1 \
  "$(gc_drained_archive "$TC/temp/drained" 30 1 cited-evidence.md)"
gc_drained_archive "$TC/temp/drained" 30 1 cited-evidence.md >/dev/null
lcheck "gc_drained cited: cited file EXCLUDED from list"     no \
  "$(printf '%s\n' "$GC_DRAINED_FILES" | grep -qFx 'cited-evidence.md' && echo yes || echo no)"
lcheck "gc_drained cited: uncited file still IN list"        yes \
  "$(printf '%s\n' "$GC_DRAINED_FILES" | grep -qFx 'uncited-old.md' && echo yes || echo no)"
lcheck "gc_drained cited: real run purges uncited only"      1 \
  "$(gc_drained_archive "$TC/temp/drained" 30 0 cited-evidence.md)"
lcheck "gc_drained cited: cited-evidence.md SURVIVED"        yes \
  "$([ -f "$TC/temp/drained/cited-evidence.md" ] && echo yes || echo no)"
lcheck "gc_drained cited: uncited-old.md purged"             no \
  "$([ -f "$TC/temp/drained/uncited-old.md" ] && echo yes || echo no)"

rm -rf "$TC"

# ARITY PIN: a SHORT call must read an EMPTY cited set, never its own positionals.
# Two things make this pin work, and the first draft got both wrong:
#
#   ARITY — it must be a SHORT call (2 args), not a 3-arg one. With exactly 3
#   args `shift 3` SUCCEEDS, so the guarded and unguarded forms are identical and
#   a 3-arg pin passes against sabotaged code (measured: mutation-proof-test.sh
#   returned VACUOUS on the 3-arg version). Short calls are a real shape because
#   the function documents ${2:-30} / ${3:-0} defaults.
#
#   FILENAME — the fixture must be named "30" so it COLLIDES with the age_days
#   argument. Under `shift 3 || true` on a short call the original positionals
#   survive in "$@", so "30" lands in cited_arr and that file is wrongly EXEMPT.
#   A normally-named fixture collides with no argument, so the count is identical
#   either way and the pin passes vacuously for a second, independent reason.
#
# Worth pinning because the failure only ever exempts MORE: the lane silently
# under-deletes while every count still looks plausible.
# NOTE: a 2-arg call takes dry_run's default of 0, so this REALLY deletes — which
# is why it runs last, in its own fixture dir.
TA="$(mktemp -d)"; mkdir -p "$TA/temp/drained"
: > "$TA/temp/drained/30";            touch -d '40 days ago' "$TA/temp/drained/30"
: > "$TA/temp/drained/normal-old.md"; touch -d '40 days ago' "$TA/temp/drained/normal-old.md"
lcheck "gc_drained arity: 2-arg short call sees an EMPTY cited set" 2 \
  "$(gc_drained_archive "$TA/temp/drained" 30)"
lcheck "gc_drained arity: arg-named file '30' was NOT exempted"     no \
  "$([ -f "$TA/temp/drained/30" ] && echo yes || echo no)"
# Positive counterpart: the same name IS exempt when genuinely passed as cited.
: > "$TA/temp/drained/30"; touch -d '40 days ago' "$TA/temp/drained/30"
lcheck "gc_drained arity: 4-arg call DOES exempt the same name"     0 \
  "$(gc_drained_archive "$TA/temp/drained" 30 0 30)"
lcheck "gc_drained arity: cited '30' SURVIVED the real run"         yes \
  "$([ -f "$TA/temp/drained/30" ] && echo yes || echo no)"
rm -rf "$TA"

# Lane 3 — stray-dir cleanup (>120min, NOT drained/)
lcheck "cleanup_stray dry-run counts 1"           1          "$(cleanup_stray_dirs "$T2/temp" 120 1)"
lcheck "cleanup_stray dry-run kept stale-dir"     yes        "$([ -d "$T2/temp/stale-dir" ] && echo yes || echo no)"
lcheck "cleanup_stray real purges 1"              1          "$(cleanup_stray_dirs "$T2/temp" 120 0)"
lcheck "cleanup_stray removed stale-dir w/content" no        "$([ -d "$T2/temp/stale-dir" ] && echo yes || echo no)"
lcheck "cleanup_stray kept fresh-dir"             yes        "$([ -d "$T2/temp/fresh-dir" ] && echo yes || echo no)"
lcheck "cleanup_stray never removed drained/"     yes        "$([ -d "$T2/temp/drained" ] && echo yes || echo no)"
lcheck "cleanup_stray empty temp_dir -> 0"        0          "$(cleanup_stray_dirs "" 120 0)"

# Lane 3 archive-before-delete preservation (): a stray dir carrying a
# top-level RECEIPT.md OR a .archive-marker sentinel is an archive-before-delete
# recovery layer and MUST survive a purge — destroying it would be the exact
# anti-pattern archive-before-delete.md forbids (nearly lost the  zeta
# archive). Both are aged past the 120-min guard so, WITHOUT the guard, they'd be
# deleted; the guard must preserve them AND exclude them from the purge count.
mkdir -p "$T2/temp/arc-receipt/bodies" "$T2/temp/arc-marker" "$T2/temp/plain-stale"
: > "$T2/temp/arc-receipt/RECEIPT.md"
: > "$T2/temp/arc-receipt/bodies/obj-1.json"
: > "$T2/temp/arc-marker/.archive-marker"
: > "$T2/temp/plain-stale/leftover.txt"
touch -d '3 hours ago' \
  "$T2/temp/arc-receipt/RECEIPT.md" "$T2/temp/arc-receipt/bodies/obj-1.json" "$T2/temp/arc-receipt/bodies" "$T2/temp/arc-receipt" \
  "$T2/temp/arc-marker/.archive-marker" "$T2/temp/arc-marker" \
  "$T2/temp/plain-stale/leftover.txt" "$T2/temp/plain-stale"
# dry-run: only the 1 plain-stale dir would purge; both archives excluded
lcheck "cleanup_stray dry-run counts 1 (archives excluded)" 1 "$(cleanup_stray_dirs "$T2/temp" 120 1 2>/dev/null)"
# real: purges the 1 plain-stale, preserves both archives
lcheck "cleanup_stray real purges 1 (archives preserved)"   1 "$(cleanup_stray_dirs "$T2/temp" 120 0 2>/dev/null)"
lcheck "cleanup_stray preserved RECEIPT.md archive dir"     yes "$([ -d "$T2/temp/arc-receipt" ] && echo yes || echo no)"
lcheck "cleanup_stray preserved RECEIPT bodies/ + object"   yes "$([ -f "$T2/temp/arc-receipt/bodies/obj-1.json" ] && echo yes || echo no)"
lcheck "cleanup_stray preserved .archive-marker dir"        yes "$([ -d "$T2/temp/arc-marker" ] && echo yes || echo no)"
lcheck "cleanup_stray removed the plain-stale dir"          no  "$([ -d "$T2/temp/plain-stale" ] && echo yes || echo no)"
rm -rf "$T2"

echo "receipt-sentinel extension/case agnosticism (g-115-3397, via _has_archive_receipt SSOT):"
# The reader required RECEIPT.md exactly while ZERO producers write that name —
# _seed_engine.py writes RECEIPT.json, history_vacuum_archive.py writes
# lowercase receipt.json. Every case below is aged past the 120-min guard, so
# WITHOUT the widened predicate the three real-producer shapes are DELETED.
# The two NEGATIVE cases are the anti-vacuity control: a predicate widened to a
# bare *receipt* substring, or one that dropped -maxdepth 1, would pass all the
# positives and be unfalsifiable. They must stay RED-able independently.
T3="$(mktemp -d)"
mkdir -p "$T3/temp/arc-json/bodies" "$T3/temp/arc-lower" "$T3/temp/arc-bare" \
         "$T3/temp/decoy-substring" "$T3/temp/decoy-nested/bodies"
: > "$T3/temp/arc-json/RECEIPT.json"              # _seed_engine.py shape
: > "$T3/temp/arc-json/bodies/obj-1.json"
: > "$T3/temp/arc-lower/receipt.json"             # history_vacuum_archive.py shape
: > "$T3/temp/arc-bare/RECEIPT"                   # extensionless receipt
: > "$T3/temp/decoy-substring/old-receipt-notes.txt"   # NEGATIVE: scratch, not an archive
: > "$T3/temp/decoy-nested/bodies/RECEIPT.json"        # NEGATIVE: not top-level
find "$T3/temp" -mindepth 1 -exec touch -d '3 hours ago' {} + 2>/dev/null || true
touch -d '3 hours ago' "$T3/temp"

# Predicate-level assertions (the SSOT function, independent of Lane 3's loop)
lcheck "_has_archive_receipt: RECEIPT.json (seed-engine shape)"  0 "$(_has_archive_receipt "$T3/temp/arc-json"; echo $?)"
lcheck "_has_archive_receipt: lowercase receipt.json"            0 "$(_has_archive_receipt "$T3/temp/arc-lower"; echo $?)"
lcheck "_has_archive_receipt: extensionless RECEIPT"             0 "$(_has_archive_receipt "$T3/temp/arc-bare"; echo $?)"
lcheck "_has_archive_receipt: NEG substring old-receipt-notes"   1 "$(_has_archive_receipt "$T3/temp/decoy-substring"; echo $?)"
lcheck "_has_archive_receipt: NEG nested receipt is not top-level" 1 "$(_has_archive_receipt "$T3/temp/decoy-nested"; echo $?)"
lcheck "_has_archive_receipt: NEG empty arg"                     1 "$(_has_archive_receipt ""; echo $?)"

# Lane-3 integration: only the 2 decoys purge; the 3 real receipts survive.
lcheck "cleanup_stray dry-run counts 2 (3 receipts excluded)" 2 "$(cleanup_stray_dirs "$T3/temp" 120 1 2>/dev/null)"
lcheck "cleanup_stray real purges 2 (3 receipts preserved)"   2 "$(cleanup_stray_dirs "$T3/temp" 120 0 2>/dev/null)"
lcheck "preserved RECEIPT.json dir"          yes "$([ -d "$T3/temp/arc-json" ] && echo yes || echo no)"
lcheck "preserved RECEIPT.json payload"      yes "$([ -f "$T3/temp/arc-json/bodies/obj-1.json" ] && echo yes || echo no)"
lcheck "preserved lowercase receipt.json dir" yes "$([ -d "$T3/temp/arc-lower" ] && echo yes || echo no)"
lcheck "preserved extensionless RECEIPT dir"  yes "$([ -d "$T3/temp/arc-bare" ] && echo yes || echo no)"
lcheck "purged the substring decoy"           no  "$([ -d "$T3/temp/decoy-substring" ] && echo yes || echo no)"
lcheck "purged the nested-receipt decoy"      no  "$([ -d "$T3/temp/decoy-nested" ] && echo yes || echo no)"
rm -rf "$T3"

echo "unmanaged-dotfile REPORT lane (g-115-3397, Lane 0 — reports, never deletes):"
# A dotfile under temp/ is matched by NO lane: the drain enumerates temp/*.md +
# temp/*.json (a glob that cannot match a leading dot) and Lane 1 exempts
# `! -name '.*'`. The originating case was a 221-byte secret-bearing dotfile.
# This lane makes the residue VISIBLE without adding a way to destroy live state
# — the survival assertion below is the load-bearing one, not the count.
T4="$(mktemp -d)"
mkdir -p "$T4/temp" "$T4/temp/.hidden-dir"
: > "$T4/temp/.launch-payload.json"     # the originating shape
: > "$T4/temp/.fresh-eyes-last-ts"      # live cadence marker — must be reported, NOT deleted
: > "$T4/temp/.gitkeep"                 # allowlisted lifecycle marker
: > "$T4/temp/.archive-marker"          # allowlisted lifecycle marker
: > "$T4/temp/plain.txt"                # NEGATIVE: not a dotfile, must not be reported
lcheck "report_unmanaged_dotfiles counts only non-allowlisted" 2 \
  "$(report_unmanaged_dotfiles "$T4/temp" 2>/dev/null)"
report_unmanaged_dotfiles "$T4/temp" >/dev/null 2>&1
lcheck "reported .launch-payload.json"   yes "$(printf '%s' "$UNMANAGED_DOTFILES" | grep -Fqx '.launch-payload.json' && echo yes || echo no)"
lcheck "reported .fresh-eyes-last-ts"    yes "$(printf '%s' "$UNMANAGED_DOTFILES" | grep -Fqx '.fresh-eyes-last-ts' && echo yes || echo no)"
lcheck "did NOT report .gitkeep"         no  "$(printf '%s' "$UNMANAGED_DOTFILES" | grep -Fqx '.gitkeep' && echo yes || echo no)"
lcheck "did NOT report .archive-marker"  no  "$(printf '%s' "$UNMANAGED_DOTFILES" | grep -Fqx '.archive-marker' && echo yes || echo no)"
lcheck "did NOT report plain.txt (non-dotfile)" no "$(printf '%s' "$UNMANAGED_DOTFILES" | grep -Fqx 'plain.txt' && echo yes || echo no)"
lcheck "did NOT report .hidden-dir (-type f only)" no "$(printf '%s' "$UNMANAGED_DOTFILES" | grep -Fqx '.hidden-dir' && echo yes || echo no)"
# REPORT, NOT PURGE — every reported file MUST still be on disk afterwards.
lcheck "report did NOT delete .launch-payload.json" yes "$([ -f "$T4/temp/.launch-payload.json" ] && echo yes || echo no)"
lcheck "report did NOT delete .fresh-eyes-last-ts"  yes "$([ -f "$T4/temp/.fresh-eyes-last-ts" ] && echo yes || echo no)"
lcheck "report did NOT delete .gitkeep"             yes "$([ -f "$T4/temp/.gitkeep" ] && echo yes || echo no)"
# CAPTURE FIRST, then match. Do NOT pipe the producer straight into `grep -q`
# here: under `set -uo pipefail` GNU grep exits at the FIRST match, the
# producer's remaining stderr writes take EPIPE, and the pipeline status becomes
# that failure — so the assertion reads "no" while the stderr it is testing for
# was emitted correctly. It reproduces ONLY with 2+ reported dotfiles (one write
# never meets a closed pipe) and ONLY under real GNU grep — a hand-probe in an
# interactive shell whose profile defines a `grep` function reads GREEN, which
# is the guard-1742 / probe-with-canonical-code-path.md rule-4 shell-shape trap.
_dot_stderr="$(report_unmanaged_dotfiles "$T4/temp" 2>&1 >/dev/null)"
lcheck "report emits a name on stderr"   yes \
  "$(printf '%s' "$_dot_stderr" | grep -Fq 'UNMANAGED DOTFILE' && echo yes || echo no)"
lcheck "report on a missing temp_dir -> 0" 0 "$(report_unmanaged_dotfiles "$T4/nonexistent" 2>/dev/null)"
lcheck "DOTFILE_ALLOWLIST override honored"  1 \
  "$(DOTFILE_ALLOWLIST='.gitkeep .archive-marker .fresh-eyes-last-ts' report_unmanaged_dotfiles "$T4/temp" 2>/dev/null)"
rm -rf "$T4"

echo "purge-lane behavior (via _purge_find_predicate SSOT function, g-115-2947):"
# Run the SSOT predicate against a synthetic temp dir. We assert the MATCHED set
# WITHOUT -delete, so the test is hermetic — it never deletes and never touches
# the live agent temp/. This locks in the two lanes (ephemera extensions +
# 0-byte empties) and the two exclusions (age guard, maxdepth/drained/).
SYNTH="$(mktemp -d)"
mkdir -p "$SYNTH/drained"
# aged (>120 min) purgeable — one per ephemera extension
for f in suite.log dump.txt build.py restart.sh gs.err selector.raw probe.out config.bak; do
  printf 'x\n' > "$SYNTH/$f"
done
: > "$SYNTH/empty-scratch.json"          # 0-byte empty — any-name -empty lane
: > "$SYNTH/empty-note.md"               # 0-byte empty .md — any-name -empty lane
# aged NON-purgeable — real working docs WITH content (must be drained, not purged)
printf '# design\n' > "$SYNTH/design-notes.md"
printf '{"k":1}\n'   > "$SYNTH/realdata.json"
touch -d '200 minutes ago' "$SYNTH"/*.log "$SYNTH"/*.txt "$SYNTH"/*.py "$SYNTH"/*.sh \
  "$SYNTH"/*.err "$SYNTH"/*.raw "$SYNTH"/*.out "$SYNTH"/*.bak "$SYNTH"/*.json "$SYNTH"/*.md 2>/dev/null
# NEGATIVE: fresh purgeable-extension file — age guard must EXCLUDE it
printf 'fresh\n' > "$SYNTH/fresh.raw"
# NEGATIVE: archived file under drained/ — maxdepth must EXCLUDE it
printf 'archived\n' > "$SYNTH/drained/old.md"; touch -d '200 minutes ago' "$SYNTH/drained/old.md"
# NEGATIVE: git-tracked 0-byte .gitkeep + a 0-byte dotfile marker (aged) — the
# dotfile exclusion (! -name '.*') MUST protect them from the -empty lane
# ( fresh-eyes catch: temp/'s tracked .gitkeep was being deleted, and
# iteration-commit would have committed the deletion, breaking the fresh-clone
# dir guarantee in temp-store.md).
: > "$SYNTH/.gitkeep"; touch -d '200 minutes ago' "$SYNTH/.gitkeep"
: > "$SYNTH/.hidden-marker"; touch -d '200 minutes ago' "$SYNTH/.hidden-marker"

PURGE_FIND_PRED=()
_purge_find_predicate 120
got="$(find "$SYNTH" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
want="build.py config.bak dump.txt empty-note.md empty-scratch.json gs.err probe.out restart.sh selector.raw suite.log "
if [ "$got" = "$want" ]; then
  echo "  [PASS] 8 ephemera extensions + 2 empties purge; content-docs/fresh/drained excluded"
else
  echo "  [FAIL] matched set mismatch"; echo "         got:  $got"; echo "         want: $want"; fails=$((fails+1))
fi
# Explicit negative assertions — each MUST be absent from the matched set
# (fixed-string, no -w: dotfile names like .gitkeep have a leading non-word char
# that makes -w boundary matching unreliable; these basenames are distinct
# enough that plain -F substring is unambiguous)
for neg in design-notes.md realdata.json fresh.raw old.md .gitkeep .hidden-marker; do
  if printf '%s' "$got" | grep -qF "$neg"; then
    echo "  [FAIL] $neg must NOT be purged but matched"; fails=$((fails+1))
  else
    echo "  [PASS] $neg correctly excluded"
  fi
done
rm -rf "$SYNTH"

echo "third-class inversion (g-306-111) — purge-by-default with exemptions:"
# Every assertion below FAILS against the pre-inversion allow-list, which is the
# point: the block above passes identically before and after  (it only
# covers behavior the inversion preserves), so it proves nothing about the new
# predicate. One distinct mutation per constraint (guard-1861).
SYNTH2="$(mktemp -d)"
mkdir -p "$SYNTH2/drained"
# THIRD CLASS — the complement of drain (.md/.json) and the old 8-extension
# purge list. Unreachable by BOTH lanes before the inversion, which is why it
# accrued without bound. Suffixes drawn from the cc-02 2026-07-31 census,
# including a one-off a single goal invented (.premutation) and an
# extensionless file, to pin that the predicate keys on the COMPLEMENT rather
# than on any enumerated list.
for f in census.jsonl config.yaml rows.tsv archive.gz notes.eml sum.sha256 patch.patch weird.premutation extensionless; do
  printf 'content\n' > "$SYNTH2/$f"
done
# EXEMPTION (ii) — non-empty .md/.json still drain, never purge
printf '# doc\n'   > "$SYNTH2/keep-doc.md"
printf '{"k":1}\n' > "$SYNTH2/keep-data.json"
# EXEMPTION (iii) — a THIRD-CLASS file cited by a durable record. Without the
# cited-set exemption this is indistinguishable from the purgeable files above,
# so it is the one case that proves the exemption is wired, not just declared.
printf 'cited\n'   > "$SYNTH2/cited-evidence.jsonl"
touch -d '200 minutes ago' "$SYNTH2"/*
# EXEMPTION (i) — dotfile, aged past the guard
: > "$SYNTH2/.gitkeep"; touch -d '200 minutes ago' "$SYNTH2/.gitkeep"
# NEGATIVE: under drained/ — maxdepth must still exclude it
printf 'archived\n' > "$SYNTH2/drained/old.jsonl"; touch -d '200 minutes ago' "$SYNTH2/drained/old.jsonl"

# ENCODE-BEFORE-DELETE watermark (2026-08-21): the third class is additionally
# gated on a completed-drain watermark. Three states pinned before the original
# inversion assertions run:
#   absent → EXEMPT (fail-closed default — what every pre-watermark caller and
#            every box that has never completed a drain inherits)
#   stale  → files NEWER than the last drain keep their encode chance
#   fresh  → files the drain provably saw purge exactly as  specified
unset PURGE_THIRD_CLASS_WATERMARK 2>/dev/null || true
PURGE_FIND_PRED=()
_purge_find_predicate 120 cited-evidence.jsonl
got2a="$(find "$SYNTH2" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ -z "$got2a" ]; then
  echo "  [PASS] no watermark → third class EXEMPT (encode-before-delete fail-closed default)"
else
  echo "  [FAIL] no watermark must exempt the third class, matched: $got2a"; fails=$((fails+1))
fi
# stale: last drain completed 300 min ago; fixtures are 200 min old → they
# postdate that drain (never classified) and must keep their encode chance.
PURGE_THIRD_CLASS_WATERMARK="$(date -d '300 minutes ago' +%Y-%m-%dT%H:%M:%S)"
PURGE_FIND_PRED=()
_purge_find_predicate 120 cited-evidence.jsonl
got2b="$(find "$SYNTH2" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ -z "$got2b" ]; then
  echo "  [PASS] stale watermark → post-drain files stay exempt"
else
  echo "  [FAIL] stale watermark must exempt post-drain files, matched: $got2b"; fails=$((fails+1))
fi
# fresh: every fixture predates the watermark — the drain provably saw them —
# so the original  inversion assertions below apply unchanged.
PURGE_THIRD_CLASS_WATERMARK="$(date +%Y-%m-%dT%H:%M:%S)"
PURGE_FIND_PRED=()
_purge_find_predicate 120 cited-evidence.jsonl
got2="$(find "$SYNTH2" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
want2="archive.gz census.jsonl config.yaml extensionless notes.eml patch.patch rows.tsv sum.sha256 weird.premutation "
if [ "$got2" = "$want2" ]; then
  echo "  [PASS] 9 third-class files purge (9 suffix shapes incl. one-off + extensionless)"
else
  echo "  [FAIL] third-class matched set mismatch"; echo "         got:  $got2"; echo "         want: $want2"; fails=$((fails+1))
fi
for neg in keep-doc.md keep-data.json cited-evidence.jsonl .gitkeep old.jsonl; do
  if printf '%s' "$got2" | grep -qF "$neg"; then
    echo "  [FAIL] $neg must NOT be purged but matched"; fails=$((fails+1))
  else
    echo "  [PASS] $neg correctly exempt"
  fi
done

# The FAIL-CLOSED fallback. When the cited set is unknown, main() uses the
# legacy allow-list — which must reach NONE of the third class, or the
# degradation would still delete files it cannot prove are uncited.
PURGE_FIND_PRED=()
_purge_find_predicate_legacy 120
got3="$(find "$SYNTH2" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ -z "$got3" ]; then
  echo "  [PASS] legacy fallback matches NO third-class file (fail-closed degrade)"
else
  echo "  [FAIL] legacy fallback matched: $got3"; fails=$((fails+1))
fi
rm -rf "$SYNTH2"

echo "cited-pattern breadth guard (g-306-111):"
# Wildcards in cited paths are REAL and must be honored (measured: 4 of 64 live
# cited paths carry one). But a pattern matching ANY name would exempt every
# file and silently revert the inversion — the failure that looks like success.
SYNTH3="$(mktemp -d)"
# Watermark set fresh: these blocks pin CITATION-SHAPE behavior on an ACTIVE
# third class (unrelated.jsonl is third-class and appears in expected sets).
PURGE_THIRD_CLASS_WATERMARK="$(date +%Y-%m-%dT%H:%M:%S)"
printf 'x\n' > "$SYNTH3/g-335-531-residue.py"
printf 'x\n' > "$SYNTH3/unrelated.jsonl"
touch -d '200 minutes ago' "$SYNTH3"/*
# A family wildcard exempts its family and NOTHING else.
PURGE_FIND_PRED=(); _purge_find_predicate 120 'g-335-531-*'
g4="$(find "$SYNTH3" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ "$g4" = "unrelated.jsonl " ]; then
  echo "  [PASS] family wildcard 'g-335-531-*' exempts its family only"
else
  echo "  [FAIL] family wildcard: got '$g4' want 'unrelated.jsonl '"; fails=$((fails+1))
fi
# An over-broad pattern must be DROPPED, not honored — else the lane empties.
PURGE_FIND_PRED=(); _purge_find_predicate 120 '*' 2>/dev/null
g5="$(find "$SYNTH3" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ "$g5" = "g-335-531-residue.py unrelated.jsonl " ]; then
  echo "  [PASS] over-broad '*' dropped — lane still purges (cannot silently self-disable)"
else
  echo "  [FAIL] over-broad '*' was honored, lane emptied: got '$g5'"; fails=$((fails+1))
fi
if _purge_find_predicate 120 '*' 2>&1 >/dev/null | grep -q 'over-broad'; then
  echo "  [PASS] over-broad exemption warns on stderr (never silent)"
else
  echo "  [FAIL] over-broad exemption dropped SILENTLY"; fails=$((fails+1))
fi
rm -rf "$SYNTH3"

echo "class-wide cited exemption (g-001-84):"
# The over-broad sentinel above is necessary but NOT sufficient: '*.raw' does not
# match the sentinel, so it passed through and exempted ALL 84 aged .raw files on
# cc-03 (would_purge reported 0 against the dir the lane exists to drain). The
# discriminator is the literal STEM — a family names artifacts an author made,
# a stemless pattern names a whole file CLASS. Both directions are asserted here:
# rejecting every glob would delete the 7 legitimately-cited families.
SYNTH4="$(mktemp -d)"
# Watermark fresh here too (unrelated.jsonl is third-class in every expected set).
PURGE_THIRD_CLASS_WATERMARK="$(date +%Y-%m-%dT%H:%M:%S)"
printf 'x\n' > "$SYNTH4/dump.raw"
printf 'x\n' > "$SYNTH4/mergeback-a.raw"
printf 'x\n' > "$SYNTH4/unrelated.jsonl"
touch -d '200 minutes ago' "$SYNTH4"/*
# NEGATIVE: a stemless class pattern is dropped — the whole extension still purges.
PURGE_FIND_PRED=(); _purge_find_predicate 120 '*.raw' 2>/dev/null
g6="$(find "$SYNTH4" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ "$g6" = "dump.raw mergeback-a.raw unrelated.jsonl " ]; then
  echo "  [PASS] class-wide '*.raw' dropped — the .raw class still purges"
else
  echo "  [FAIL] class-wide '*.raw' honored, .raw class shielded: got '$g6'"; fails=$((fails+1))
fi
# POSITIVE CONTROL: a stem-bearing family wildcard over the SAME extension is honored.
PURGE_FIND_PRED=(); _purge_find_predicate 120 'mergeback-*' 2>/dev/null
g7="$(find "$SYNTH4" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ "$g7" = "dump.raw unrelated.jsonl " ]; then
  echo "  [PASS] stem-bearing 'mergeback-*' still exempts its family (not a blanket glob ban)"
else
  echo "  [FAIL] family wildcard over a purgeable extension was dropped: got '$g7'"; fails=$((fails+1))
fi
if _purge_find_predicate 120 '*.raw' 2>&1 >/dev/null | grep -q 'class-wide'; then
  echo "  [PASS] class-wide exemption warns on stderr (never silent)"
else
  echo "  [FAIL] class-wide exemption dropped SILENTLY"; fails=$((fails+1))
fi
# '*.*' reaches THIS branch, not the sentinel: the sentinel string carries no dot,
# so '*.*' never matched it and was honored outright before the stem test existed.
# Pinned separately from '*' because they take different branches ( review).
PURGE_FIND_PRED=(); _purge_find_predicate 120 '*.*' 2>/dev/null
g8="$(find "$SYNTH4" "${PURGE_FIND_PRED[@]}" 2>/dev/null | sed 's#.*/##' | sort | tr '\n' ' ')"
if [ "$g8" = "dump.raw mergeback-a.raw unrelated.jsonl " ]; then
  echo "  [PASS] '*.*' dropped — the sentinel never covered it, the stem test does"
else
  echo "  [FAIL] '*.*' honored, lane shielded: got '$g8'"; fails=$((fails+1))
fi
rm -rf "$SYNTH4"
# Don't leak the citation-shape watermark into blocks added below this line.
unset PURGE_THIRD_CLASS_WATERMARK 2>/dev/null || true

echo "stray git-repo preservation (g-115-3648) — sole-copy git content survives Lane 3:"
# A stray git repo with a clean worktree looks maximally safe by every signal
# Lane 3 read before — no dirty files, no RECEIPT — yet can carry commits that
# exist NOWHERE else. Five shapes, each MEASURED with raw probes before this
# block was authored (2026-08-21; empty-repo log rc=0/empty, status clean):
#   unpushed commit (no remote)    → PRESERVED (log --branches --not --remotes)
#   fully pushed + clean           → PURGED    (no marginal git content)
#   pushed + dirty TRACKED file    → PRESERVED (--porcelain -uno non-empty)
#   empty init'd repo (no commits) → PURGED    (scaffold only, nothing to lose)
#   plain non-repo dir             → PURGED    (pre-existing behavior, control)
TG3="$(mktemp -d)"
GITC="-c user.email=t@t -c user.name=t -c commit.gpgsign=false"
mkdir -p "$TG3/temp" "$TG3/bare"
git -C "$TG3/bare" init -q --bare 2>/dev/null
mk_repo() { # <dir> — init + one committed file
  mkdir -p "$1"; git -C "$1" init -q 2>/dev/null
  echo x > "$1/f.txt"; git -C "$1" add f.txt 2>/dev/null
  # shellcheck disable=SC2086
  git -C "$1" $GITC commit -qm c1 2>/dev/null
}
mk_repo "$TG3/temp/repo-unpushed"
mk_repo "$TG3/temp/repo-pushed"
git -C "$TG3/temp/repo-pushed" remote add origin "$TG3/bare" 2>/dev/null
git -C "$TG3/temp/repo-pushed" push -q origin HEAD 2>/dev/null
mk_repo "$TG3/temp/repo-dirty"
git -C "$TG3/temp/repo-dirty" remote add origin "$TG3/bare" 2>/dev/null
git -C "$TG3/temp/repo-dirty" push -q origin HEAD:dirty 2>/dev/null
echo CHANGED >> "$TG3/temp/repo-dirty/f.txt"
mkdir -p "$TG3/temp/repo-empty"; git -C "$TG3/temp/repo-empty" init -q 2>/dev/null
mkdir -p "$TG3/temp/plain"; echo x > "$TG3/temp/plain/junk.txt"
find "$TG3/temp" -mindepth 1 -exec touch -d '3 hours ago' {} + 2>/dev/null || true
touch -d '3 hours ago' "$TG3/temp"/* 2>/dev/null
lcheck "git-guard dry-run counts 3 (pushed+empty+plain)" 3 "$(cleanup_stray_dirs "$TG3/temp" 120 1 2>/dev/null)"
cleanup_stray_dirs "$TG3/temp" 120 1 >/dev/null 2>&1
lcheck "git-guard preserved list names repo-unpushed"    yes "$(printf '%s' "$STRAY_PRESERVED_GIT" | grep -qFx 'repo-unpushed' && echo yes || echo no)"
lcheck "git-guard preserved list names repo-dirty"       yes "$(printf '%s' "$STRAY_PRESERVED_GIT" | grep -qFx 'repo-dirty' && echo yes || echo no)"
lcheck "git-guard real run purges 3"                     3   "$(cleanup_stray_dirs "$TG3/temp" 120 0 2>/dev/null)"
lcheck "repo-unpushed SURVIVED (sole-copy commit)"       yes "$([ -d "$TG3/temp/repo-unpushed" ] && echo yes || echo no)"
lcheck "repo-dirty SURVIVED (dirty tracked file)"        yes "$([ -d "$TG3/temp/repo-dirty" ] && echo yes || echo no)"
lcheck "repo-pushed purged (fully pushed + clean)"       no  "$([ -d "$TG3/temp/repo-pushed" ] && echo yes || echo no)"
lcheck "repo-empty purged (no commits, nothing to lose)" no  "$([ -d "$TG3/temp/repo-empty" ] && echo yes || echo no)"
lcheck "plain dir purged (control)"                      no  "$([ -d "$TG3/temp/plain" ] && echo yes || echo no)"
rm -rf "$TG3"

echo "cited-set lookup contract (g-306-111):"
# UNKNOWN must be distinguishable from EMPTY, or a box with an unreadable world
# purges everything. The missing-script case is the hermetic proxy for that.
if _cited_basenames "/nonexistent-dir-for-temp-drain-test-zzz" >/dev/null 2>&1; then
  echo "  [FAIL] _cited_basenames returned 0 for a missing script — caller would purge-by-default on an unknown cited set"; fails=$((fails+1))
else
  echo "  [PASS] _cited_basenames returns non-zero when the cited set is UNKNOWN"
fi
# Success path against the live corpus. A world this box cannot read is a
# legitimate environment (satellite box), so that is a SKIP, not a FAIL —
# the fail-closed contract above is what protects that case.
if cb_out="$(_cited_basenames "$SCRIPT_DIR/.." 2>/dev/null)"; then
  if printf '%s' "$cb_out" | grep -q '/'; then
    echo "  [FAIL] _cited_basenames emitted a path, not a basename: $(printf '%s' "$cb_out" | grep -m1 '/')"; fails=$((fails+1))
  else
    echo "  [PASS] _cited_basenames emits basenames only ($(printf '%s\n' "$cb_out" | grep -c . || true) cited)"
  fi
else
  echo "  [SKIP] cited-set unreadable on this box — fail-closed path covered above"
fi

echo "age-guard visibility (g-115-4994) — a skipped dir must be NAMED, not omitted:"
# The reported failure: an operator MOVED an archive into a RECEIPT-bearing dir
# so Lane 3 would preserve it, then re-ran the dry-run to confirm. The dir
# appeared in NO lane, which reads as safety — but the protective move had
# refreshed its mtime, so the age guard never evaluated it. The reader most
# likely to hit this is the one confirming a protective action, and it is
# silent in the SAFE-LOOKING direction.
# INVOCATION SHAPE IS LOAD-BEARING HERE. cleanup_stray_dirs reports its count on
# stdout but its age-skip detail through GLOBALS, and production calls it bare
# (temp-drain-purge.sh:841 `cleanup_stray_dirs ... >/dev/null`) so those globals
# reach the emitter. Capturing with `$(...)` forks a subshell and every global is
# discarded at the closing paren -- silently, with the count still correct. That
# is a green-looking harness measuring a branch production never takes
# (probe-with-canonical-code-path.md). Redirect to a file; never $(...).
TG4="$(mktemp -d)"
mkdir -p "$TG4/temp/aged-dir" "$TG4/temp/just-touched"
echo x > "$TG4/temp/aged-dir/f.txt"
echo x > "$TG4/temp/just-touched/f.txt"
touch -d '3 hours ago' "$TG4/temp/aged-dir/f.txt" "$TG4/temp/aged-dir"
# just-touched keeps its now-mtime: inside the 120-min window, so never evaluated.

cleanup_stray_dirs "$TG4/temp" 120 1 >"$TG4/n" 2>/dev/null; _n="$(cat "$TG4/n")"
lcheck "aged dir is evaluated (counted)"                 1   "$_n"
lcheck "fresh dir is reported as age-skipped"            1   "${STRAY_AGE_SKIPPED:-0}"
lcheck "age-skipped list NAMES the fresh dir"            yes "$(printf '%s' "${STRAY_AGE_SKIPPED_DIRS:-}" | grep -q 'just-touched' && echo yes || echo no)"
lcheck "age-skipped dir is NOT folded into the count"    no  "$(printf '%s' "$_n" | grep -q '^2$' && echo yes || echo no)"

# Neutralized guard: the same dir must now report a REAL lane verdict, and the
# skip tally must fall to zero — the operator's escape hatch actually works.
cleanup_stray_dirs "$TG4/temp" 0 1 >"$TG4/n0" 2>/dev/null; _n0="$(cat "$TG4/n0")"
lcheck "--age-min 0 evaluates both dirs"                 2   "$_n0"
lcheck "--age-min 0 leaves nothing age-skipped"          0   "${STRAY_AGE_SKIPPED:-0}"

# The empty-candidate case is the one that matters most: with NO evaluable dir,
# the lane used to return early and the skipped dir vanished entirely.
TG5="$(mktemp -d)"
mkdir -p "$TG5/temp/only-fresh"
echo x > "$TG5/temp/only-fresh/f.txt"
cleanup_stray_dirs "$TG5/temp" 120 1 >"$TG5/n1" 2>/dev/null; _n1="$(cat "$TG5/n1")"
lcheck "no evaluable dirs -> count 0"                    0   "$_n1"
lcheck "...but the skipped dir is STILL reported"        1   "${STRAY_AGE_SKIPPED:-0}"
rm -rf "$TG4" "$TG5"

if [ "$fails" -gt 0 ]; then echo ""; echo "$fails failure(s)"; exit 1; fi
echo ""
echo "All temp-drain-purge guard + lane cases verified."
exit 0
