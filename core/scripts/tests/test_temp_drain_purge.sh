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
for k in '"drained_gc_would_purge"' '"stray_would_purge"' '"drained_age_days"'; do
  if printf '%s' "$out" | grep -q "$k"; then
    echo "  [PASS] dry-run JSON carries $k"
  else
    echo "  [FAIL] dry-run JSON missing $k — out=$out"; fails=$((fails+1))
  fi
done

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
for k in '"drained_gc_purged"' '"drained_gc_would_purge"' '"stray_purged"' '"stray_would_purge"' '"drained_age_days"'; do
  if printf '%s' "$nt_out" | grep -q "$k"; then
    echo "  [PASS] no-temp-dir JSON carries $k"
  else
    echo "  [FAIL] no-temp-dir JSON missing $k — out=$nt_out"; fails=$((fails+1))
  fi
done

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

if [ "$fails" -gt 0 ]; then echo ""; echo "$fails failure(s)"; exit 1; fi
echo ""
echo "All temp-drain-purge guard + lane cases verified."
exit 0
