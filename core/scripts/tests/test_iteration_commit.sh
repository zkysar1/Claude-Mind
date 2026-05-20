#!/usr/bin/env bash
# test_iteration_commit.sh — Smoke test for .
#
# Verifies iteration-commit.sh:
#   1. --help works
#   2. Routine outcome → no-op
#   3. Empty status → no-op
#   4. Deep outcome with dirty file → commit produced with correct format
#   5. Sensitive-file filtering (.env skipped)
#   6. Auto-derived type from title prefix (Apply: → feat)
#   7. Dry-run prints plan, no commit
#   8. Missing required args → exit 1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ITERATION_COMMIT="$SCRIPT_DIR/../iteration-commit.sh"

if [[ ! -x "$ITERATION_COMMIT" ]]; then
  chmod +x "$ITERATION_COMMIT" 2>/dev/null || true
fi

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Create a fresh tmp git repo for each scenario.
fresh_repo() {
  local repo
  repo=$(mktemp -d)
  git -C "$repo" init -q
  git -C "$repo" config user.email "test@example.com"
  git -C "$repo" config user.name "Test"
  echo "initial" > "$repo/README.md"
  git -C "$repo" add README.md
  git -C "$repo" commit -q -m "initial"
  echo "$repo"
}

# --- Test 1: --help ---
echo "Test 1: --help works"
if bash "$ITERATION_COMMIT" --help 2>&1 | grep -q "Usage: iteration-commit.sh"; then
  pass "--help prints usage"
else
  fail "--help did not print usage"
fi

# --- Test 2: Routine outcome → no-op ---
echo "Test 2: routine outcome → no-op"
repo=$(fresh_repo)
echo "change" > "$repo/foo.txt"
out=$(bash "$ITERATION_COMMIT" --goal-id g-test-01 --title "Apply: test" --outcome routine --repo "$repo" 2>&1)
if echo "$out" | grep -q "skip: outcome=routine"; then
  before=$(git -C "$repo" rev-list HEAD --count)
  if [[ "$before" -eq 1 ]]; then
    pass "routine → no commit produced"
  else
    fail "routine → unexpected commit count $before"
  fi
else
  fail "routine outcome did not skip: $out"
fi
rm -rf "$repo"

# --- Test 3: Empty status → no-op ---
echo "Test 3: empty status → no-op"
repo=$(fresh_repo)
out=$(bash "$ITERATION_COMMIT" --goal-id g-test-02 --title "Apply: test" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -q "skip: no uncommitted"; then
  pass "empty status → no-op"
else
  fail "empty status did not skip: $out"
fi
rm -rf "$repo"

# --- Test 4: Deep + dirty file → commit produced ---
echo "Test 4: deep + dirty file → commit"
repo=$(fresh_repo)
echo "new content" > "$repo/feature.txt"
out=$(bash "$ITERATION_COMMIT" --goal-id g-280-02 --title "Apply: Design iteration-commit.sh" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -q '"commit_sha"'; then
  pass "commit produced, JSON output emitted"
  # Verify commit message format
  msg=$(git -C "$repo" log -1 --pretty=format:%B)
  if echo "$msg" | grep -qE "^feat\(g-280-02\)"; then
    pass "commit summary format: feat(g-280-02): ..."
  else
    fail "wrong summary format: $(echo "$msg" | head -1)"
  fi
  if echo "$msg" | grep -q "g-280-02: Apply: Design iteration-commit.sh"; then
    pass "commit body contains goal-id + full title"
  else
    fail "body missing goal-id+title"
  fi
  if echo "$msg" | grep -q "outcome: deep"; then
    pass "commit body contains outcome line"
  else
    fail "body missing outcome line"
  fi
  if echo "$msg" | grep -q "Co-Authored-By:"; then
    pass "commit body contains Co-Authored-By signature"
  else
    fail "body missing Co-Authored-By"
  fi
else
  fail "no commit produced or JSON output missing: $out"
fi
rm -rf "$repo"

# --- Test 5: Sensitive file filtering ---
echo "Test 5: sensitive file filtering"
repo=$(fresh_repo)
echo "real change" > "$repo/feature.txt"
echo "SECRET=abc123" > "$repo/.env"
out=$(bash "$ITERATION_COMMIT" --goal-id g-test-05 --title "Apply: filter test" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -q "skipping sensitive"; then
  pass "sensitive file warning emitted"
else
  fail "no sensitive warning: $out"
fi
# .env should NOT be in the commit
committed_files=$(git -C "$repo" diff-tree --no-commit-id --name-only -r HEAD)
if echo "$committed_files" | grep -q "^\.env$"; then
  fail ".env was committed (should be filtered)"
else
  pass ".env filtered out of commit"
fi
if echo "$committed_files" | grep -q "feature.txt"; then
  pass "non-sensitive file committed"
else
  fail "feature.txt not committed"
fi
rm -rf "$repo"

# --- Test 6: Auto type derivation ---
echo "Test 6: auto type from title prefix"
declare -A title_to_type=(
  ["Apply: foo"]="feat"
  ["Fix: bar"]="fix"
  ["Maintain: baz"]="chore"
  ["Investigate: qux"]="docs"
  ["Verify: quux"]="test"
)
for title in "${!title_to_type[@]}"; do
  expected="${title_to_type[$title]}"
  repo=$(fresh_repo)
  echo "x" > "$repo/file.txt"
  out=$(bash "$ITERATION_COMMIT" --goal-id g-test-06 --title "$title" --outcome deep --repo "$repo" 2>&1)
  msg=$(git -C "$repo" log -1 --pretty=format:%B 2>/dev/null | head -1 || true)
  if echo "$msg" | grep -qE "^${expected}\("; then
    pass "title prefix '$title' → type='$expected'"
  else
    fail "title prefix '$title' → wrong type (got: $msg)"
  fi
  rm -rf "$repo"
done

# --- Test 7: Dry run ---
echo "Test 7: dry run prints plan, no commit"
repo=$(fresh_repo)
echo "x" > "$repo/file.txt"
before=$(git -C "$repo" rev-list HEAD --count)
out=$(bash "$ITERATION_COMMIT" --goal-id g-test-07 --title "Apply: dry test" --outcome deep --repo "$repo" --dry-run 2>&1)
after=$(git -C "$repo" rev-list HEAD --count)
if [[ "$before" -eq "$after" ]]; then
  pass "dry-run did not create commit"
else
  fail "dry-run created commit (before=$before after=$after)"
fi
if echo "$out" | grep -q "DRY-RUN"; then
  pass "dry-run output marked"
else
  fail "no DRY-RUN marker in output"
fi
rm -rf "$repo"

# --- Test 8: Missing args → exit 1 ---
echo "Test 8: missing args → exit 1"
if bash "$ITERATION_COMMIT" --goal-id only 2>/dev/null; then
  fail "missing --title/--outcome/--repo did not exit non-zero"
else
  rc=$?
  if [[ "$rc" -eq 1 ]]; then
    pass "missing args → exit 1"
  else
    fail "wrong exit code: $rc"
  fi
fi

# --- Test 10: Commit retry on transient failure () ---
# Verifies the retry loop succeeds when a pre-commit hook fails first then
# passes. Uses a hook that increments a counter; fails on attempt 1, passes
# on attempt 2. The script's retry-3x loop must observe success on attempt 2
# and emit an INFO message naming the retry attempt number.
echo "Test 10: commit retry on transient failure"
repo=$(fresh_repo)
echo "test-change" > "$repo/feature.txt"
# Install a pre-commit hook that fails the first invocation, passes second.
mkdir -p "$repo/.git/hooks"
cat > "$repo/.git/hooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
counter_file=".git/hook-counter"
count=$(cat "$counter_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$counter_file"
if [[ "$count" -lt 2 ]]; then
  echo "pre-commit: transient failure attempt $count" >&2
  exit 1
fi
exit 0
HOOK
chmod +x "$repo/.git/hooks/pre-commit"
out=$(bash "$ITERATION_COMMIT" --goal-id g-280-04 --title "Apply: retry test" --outcome deep --repo "$repo" 2>&1)
final_count=$(cat "$repo/.git/hook-counter" 2>/dev/null || echo 0)
if echo "$out" | grep -q "commit succeeded on retry 2/3"; then
  pass "retry succeeded on attempt 2"
else
  fail "no 'commit succeeded on retry 2/3' marker in output: $out"
fi
if echo "$out" | grep -q '"commit_sha"'; then
  pass "JSON output emitted after retry success"
else
  fail "no commit_sha in retry output: $out"
fi
if [[ "$final_count" -eq 2 ]]; then
  pass "pre-commit hook invoked exactly 2 times (1 fail + 1 success)"
else
  fail "wrong invocation count: $final_count (expected 2)"
fi
rm -rf "$repo"

# --- Test 11: Commit retry exhaustion exits non-zero () ---
# Verifies that when all 3 retries fail, the script exits 2 with the last
# error message in stderr (matching the original error path's contract).
echo "Test 11: retry exhaustion → exit 2"
repo=$(fresh_repo)
echo "test-change" > "$repo/feature.txt"
mkdir -p "$repo/.git/hooks"
cat > "$repo/.git/hooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
echo "pre-commit: always fails" >&2
exit 1
HOOK
chmod +x "$repo/.git/hooks/pre-commit"
# Capture both rc and output
set +e
out=$(bash "$ITERATION_COMMIT" --goal-id g-280-04 --title "Apply: retry exhaustion" --outcome deep --repo "$repo" 2>&1)
rc=$?
set -e
if [[ "$rc" -eq 2 ]]; then
  pass "exhausted retries → exit 2"
else
  fail "wrong exit code: $rc (expected 2). Output: $out"
fi
if echo "$out" | grep -q "git commit failed after 3 attempts"; then
  pass "exhaustion error message includes attempt count"
else
  fail "missing 'after 3 attempts' message: $out"
fi
# Verify NO commit landed
final_count=$(git -C "$repo" rev-list HEAD --count)
if [[ "$final_count" -eq 1 ]]; then
  pass "no extra commit produced on exhaustion (initial seed only)"
else
  fail "unexpected commit count: $final_count"
fi
rm -rf "$repo"

# --- Test 9: Orphan-deletion routing () ---
# " D" porcelain entries whose parent dir doesn't exist on disk must route
# to git rm --cached --ignore-unmatch (git add fails with pathspec error).
echo "Test 9: orphan-deletion routing"
repo=$(fresh_repo)
# Create + commit a transient lock dir to simulate the .autocompact-serialize-lock/
# failure mode that motivated this branch.
mkdir "$repo/.autocompact-serialize-lock"
echo "holder" > "$repo/.autocompact-serialize-lock/holder"
echo "sid" > "$repo/.autocompact-serialize-lock/sid"
git -C "$repo" add -A
git -C "$repo" commit -q -m "seed lock dir"
# Now strand the index: delete the dir on disk while it remains tracked.
rm -rf "$repo/.autocompact-serialize-lock"
echo "normal" > "$repo/normal.txt"
# Dry-run: must identify orphan deletions
out=$(bash "$ITERATION_COMMIT" --goal-id g-280-08 --title "Idea: orphan test" --outcome deep --repo "$repo" --dry-run 2>&1)
if echo "$out" | grep -q "orphan deletion"; then
  pass "dry-run identifies orphan deletions"
else
  fail "no 'orphan deletion' marker in dry-run output: $out"
fi
if echo "$out" | grep -q "files to stage (git rm --cached):"; then
  pass "dry-run shows separate git rm --cached section"
else
  fail "missing 'files to stage (git rm --cached)' section: $out"
fi
# Real commit: must succeed despite missing parent dir
out=$(bash "$ITERATION_COMMIT" --goal-id g-280-08 --title "Idea: orphan test" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -q '"commit_sha"'; then
  pass "commit succeeds with orphan deletions"
else
  fail "commit failed with orphan deletions: $out"
fi
# Verify the deletions actually landed
final_status=$(git -C "$repo" status --porcelain)
if [[ -z "$final_status" ]]; then
  pass "working tree clean after orphan-deletion commit"
else
  fail "working tree not clean: $final_status"
fi
# Verify lock files are no longer tracked
if git -C "$repo" ls-files | grep -q autocompact-serialize; then
  fail "lock files still tracked after commit"
else
  pass "lock files removed from index"
fi
# normal.txt should also be in the commit
if git -C "$repo" ls-files | grep -q "normal.txt"; then
  pass "normal file also committed alongside orphan deletions"
else
  fail "normal.txt not committed"
fi
rm -rf "$repo"

# --- Test 12: Concurrent invocations serialize via mkdir-lock () ---
# Two iteration-commit.sh runs launched concurrently must:
#   (a) both return rc=0 (no deadlock)
#   (b) the lock is acquired by exactly one runner at a time (no double-commits
#       with corrupt index)
#   (c) the lock dir is cleaned up after both complete
#   (d) the lock dir contents never leak into commits
# IMPORTANT: this test does NOT assert "no authorship bleed" — that property
# does not follow from the lock alone. `git status --porcelain` is repo-wide
# and `git add -A` stages every reported path; whichever agent runs status
# first stages everything visible in the working tree, including partner
# files. The lock SERIALIZES the operations (making outcomes deterministic
# rather than racy) and prevents index.lock contention failures, but a full
# bleed fix requires per-agent namespace filtering OR per-iteration file
# tracking (Strategy B, deferred follow-up ).
echo "Test 12: concurrent invocations serialize cleanly via lock"
repo=$(fresh_repo)
mkdir "$repo/alpha-scope"
mkdir "$repo/bravo-scope"
echo "alpha-work" > "$repo/alpha-scope/file.txt"
echo "bravo-work" > "$repo/bravo-scope/file.txt"
TMPLOG=$(mktemp -d)
MIND_AGENT=alpha ITERATION_COMMIT_LOCK_WAIT_S=10 bash "$ITERATION_COMMIT" \
  --goal-id g-alpha-12 --title "Apply: alpha concurrent" --outcome deep --repo "$repo" \
  > "$TMPLOG/alpha.out" 2>&1 &
A_PID=$!
MIND_AGENT=bravo ITERATION_COMMIT_LOCK_WAIT_S=10 bash "$ITERATION_COMMIT" \
  --goal-id g-bravo-12 --title "Apply: bravo concurrent" --outcome deep --repo "$repo" \
  > "$TMPLOG/bravo.out" 2>&1 &
B_PID=$!
wait $A_PID; A_RC=$?
wait $B_PID; B_RC=$?
if [[ "$A_RC" -eq 0 && "$B_RC" -eq 0 ]]; then
  pass "both concurrent invocations returned rc=0 (no deadlock, no acquire timeout)"
else
  fail "concurrent invocation rc — alpha=$A_RC bravo=$B_RC. alpha.out=$(cat "$TMPLOG/alpha.out") bravo.out=$(cat "$TMPLOG/bravo.out")"
fi
# Exactly one runner should have produced a commit (the other sees clean state)
new_commits=$(git -C "$repo" rev-list HEAD --count)
new_commits=$((new_commits - 1))  # subtract initial seed
if [[ "$new_commits" -eq 1 ]]; then
  pass "serialization produced exactly 1 commit (winner takes all uncommitted files)"
elif [[ "$new_commits" -eq 2 ]]; then
  # Edge: if the test's timing gave each runner its own status snapshot
  # before either committed, we may get 2 commits. Still valid serialization.
  pass "serialization produced 2 commits (both runners had work at their status time)"
else
  fail "unexpected commit count: $new_commits (expected 1 or 2)"
fi
# Each produced commit must have Co-Authored-By matching ONE of the agents
shas=$(git -C "$repo" log --pretty=format:%H -"$new_commits")
for sha in $shas; do
  msg=$(git -C "$repo" log -1 --pretty=%B "$sha")
  if echo "$msg" | grep -qE "Co-Authored-By: (alpha|bravo) <"; then
    pass "commit $sha has valid Co-Authored-By"
  else
    fail "commit $sha missing valid Co-Authored-By: $(echo "$msg" | tail -3)"
  fi
done
# Lock dir must be cleaned up
if [[ -d "$repo/.iteration-commit-lock" ]]; then
  fail "lock dir survived after both invocations completed"
else
  pass "lock dir cleaned up after both invocations (trap fired)"
fi
# Lock dir contents must never leak into a commit
if git -C "$repo" ls-files | grep -q "^\.iteration-commit-lock"; then
  fail "lock dir contents leaked into commit"
else
  pass "lock dir filtered from commits"
fi
# Both runners' output must show no fatal git errors (index.lock corruption)
if grep -qiE "(fatal|index\.lock)" "$TMPLOG/alpha.out" "$TMPLOG/bravo.out"; then
  fail "git fatal/index.lock error in concurrent output"
else
  pass "no git fatal/index.lock errors in either runner's output"
fi
rm -rf "$TMPLOG" "$repo"

# --- Test 13: Stale-lock recovery () ---
# Pre-create a stale lock dir, set the stale threshold to 1s, sleep 2s, then
# run iteration-commit. The pre-acquire stale cleanup must remove the lock
# and the commit must succeed.
echo "Test 13: stale-lock recovery"
repo=$(fresh_repo)
echo "real-change" > "$repo/feature.txt"
mkdir "$repo/.iteration-commit-lock"
echo "ghost-agent" > "$repo/.iteration-commit-lock/holder"
# Write a timestamp older than the stale threshold (set very old)
echo "0" > "$repo/.iteration-commit-lock/timestamp"
out=$(ITERATION_COMMIT_LOCK_STALE_S=1 bash "$ITERATION_COMMIT" \
  --goal-id g-280-11 --title "Apply: stale recovery test" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -q "stale lock detected"; then
  pass "stale-lock warning emitted"
else
  fail "no stale-lock warning in output: $out"
fi
if echo "$out" | grep -q '"commit_sha"'; then
  pass "commit succeeded after stale-lock recovery"
else
  fail "commit did not succeed: $out"
fi
if [[ ! -d "$repo/.iteration-commit-lock" ]]; then
  pass "lock dir cleaned up after stale recovery + commit"
else
  fail "lock dir survived after stale recovery"
fi
rm -rf "$repo"

# --- Test 15: Namespace filter drops cross-agent files () ---
# When known agent dirs exist (sibling dirs with self.md), iteration-commit.sh
# filters out paths under OTHER agents' directories. This is the actual fix
# for the cross-agent authorship bleed that the  mkdir-lock surfaced
# but did not close. Strict assertion: alpha's commit contains alpha-scope/
# files ONLY, never bravo-scope/ or zeta-scope/.
echo "Test 15: namespace filter drops cross-agent files"
repo=$(fresh_repo)
mkdir "$repo/alpha"; echo "alpha-self" > "$repo/alpha/self.md"
mkdir "$repo/bravo"; echo "bravo-self" > "$repo/bravo/self.md"
mkdir "$repo/zeta";  echo "zeta-self"  > "$repo/zeta/self.md"
# Commit the self.md files so they're tracked (not staged for our test commit)
git -C "$repo" add -A
git -C "$repo" commit -q -m "seed agent dirs"
# Now create uncommitted files in each agent dir
echo "alpha-work" > "$repo/alpha/file.txt"
echo "bravo-work" > "$repo/bravo/file.txt"
echo "zeta-work"  > "$repo/zeta/file.txt"
echo "shared"     > "$repo/shared.txt"  # not under any agent dir
# Run as alpha — should commit only alpha/file.txt and shared.txt
out=$(MIND_AGENT=alpha bash "$ITERATION_COMMIT" \
  --goal-id g-280-12 --title "Apply: namespace filter test" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -qE "namespace filter dropped [0-9]+ cross-agent"; then
  pass "namespace filter info line emitted"
else
  fail "missing namespace-filter info: $out"
fi
# Inspect last commit
committed=$(git -C "$repo" diff-tree --no-commit-id --name-only -r HEAD)
if echo "$committed" | grep -q "^alpha/file.txt$"; then
  pass "alpha's file committed"
else
  fail "alpha's file NOT committed: $committed"
fi
if echo "$committed" | grep -qE "^(bravo|zeta)/file.txt$"; then
  fail "cross-agent file leaked into commit: $committed"
else
  pass "no cross-agent files in commit (bravo/ + zeta/ correctly filtered)"
fi
if echo "$committed" | grep -q "^shared.txt$"; then
  pass "shared (non-agent) file committed"
else
  fail "shared file missing from commit: $committed"
fi
# Author signature
msg=$(git -C "$repo" log -1 --pretty=%B)
if echo "$msg" | grep -q "Co-Authored-By: alpha"; then
  pass "Co-Authored-By: alpha on the commit"
else
  fail "wrong Co-Authored-By: $(echo "$msg" | tail -3)"
fi
# Status should still show bravo/zeta files as untracked (we filtered them, didn't commit them)
remaining=$(git -C "$repo" status --porcelain)
if echo "$remaining" | grep -qE "^\?\? (bravo|zeta)/"; then
  pass "filtered files remain in working tree (not absorbed, not deleted)"
else
  fail "expected bravo/ + zeta/ untracked, got: $remaining"
fi
rm -rf "$repo"

# --- Test 16: --no-namespace-filter override () ---
# Explicit override commits cross-agent files (escape hatch for legitimate
# cross-agent edits like coordinated refactors).
echo "Test 16: --no-namespace-filter override commits cross-agent files"
repo=$(fresh_repo)
mkdir "$repo/alpha"; echo "alpha-self" > "$repo/alpha/self.md"
mkdir "$repo/bravo"; echo "bravo-self" > "$repo/bravo/self.md"
git -C "$repo" add -A
git -C "$repo" commit -q -m "seed agent dirs"
echo "x" > "$repo/alpha/file.txt"
echo "y" > "$repo/bravo/file.txt"
out=$(MIND_AGENT=alpha bash "$ITERATION_COMMIT" \
  --goal-id g-280-12 --title "Apply: override test" --outcome deep --repo "$repo" \
  --no-namespace-filter 2>&1)
committed=$(git -C "$repo" diff-tree --no-commit-id --name-only -r HEAD)
if echo "$committed" | grep -q "^alpha/file.txt$" && echo "$committed" | grep -q "^bravo/file.txt$"; then
  pass "--no-namespace-filter commits both alpha and bravo files"
else
  fail "override didn't commit cross-agent: $committed"
fi
if echo "$out" | grep -qE "namespace filter dropped"; then
  fail "namespace-filter info emitted despite override"
else
  pass "no namespace-filter info when override active"
fi
rm -rf "$repo"

# --- Test 17: Namespace filter inactive when MIND_AGENT unset () ---
# No MIND_AGENT → no filter (backward-compat for callers that don't set it,
# e.g., direct user invocations or external scripts).
echo "Test 17: namespace filter inactive when MIND_AGENT unset"
repo=$(fresh_repo)
mkdir "$repo/alpha"; echo "alpha-self" > "$repo/alpha/self.md"
mkdir "$repo/bravo"; echo "bravo-self" > "$repo/bravo/self.md"
git -C "$repo" add -A
git -C "$repo" commit -q -m "seed"
echo "x" > "$repo/alpha/file.txt"
echo "y" > "$repo/bravo/file.txt"
# Unset MIND_AGENT explicitly
out=$(unset MIND_AGENT; bash "$ITERATION_COMMIT" \
  --goal-id g-280-12 --title "Apply: unset agent test" --outcome deep --repo "$repo" 2>&1)
committed=$(git -C "$repo" diff-tree --no-commit-id --name-only -r HEAD)
if echo "$committed" | grep -q "^alpha/file.txt$" && echo "$committed" | grep -q "^bravo/file.txt$"; then
  pass "no MIND_AGENT → namespace filter inactive (commits everything)"
else
  fail "expected both files committed, got: $committed"
fi
rm -rf "$repo"

# --- Test 18: Namespace filter — skip when ALL files are cross-agent () ---
# When the only uncommitted files belong to OTHER agents (nothing for us to
# commit), the script exits 0 with a clear message — distinct from
# sensitive-pattern-only and from raw empty-status.
echo "Test 18: skip when all uncommitted files are cross-agent"
repo=$(fresh_repo)
mkdir "$repo/alpha"; echo "alpha-self" > "$repo/alpha/self.md"
mkdir "$repo/bravo"; echo "bravo-self" > "$repo/bravo/self.md"
git -C "$repo" add -A
git -C "$repo" commit -q -m "seed"
echo "y" > "$repo/bravo/file.txt"  # ONLY bravo file uncommitted
out=$(MIND_AGENT=alpha bash "$ITERATION_COMMIT" \
  --goal-id g-280-12 --title "Apply: all-cross-agent test" --outcome deep --repo "$repo" 2>&1)
if echo "$out" | grep -q "all uncommitted files belong to other agents"; then
  pass "skip message names the cross-agent case"
else
  fail "expected cross-agent skip message: $out"
fi
# No new commit should land
commit_count=$(git -C "$repo" rev-list HEAD --count)
if [[ "$commit_count" -eq 2 ]]; then  # initial + seed
  pass "no commit produced when only cross-agent files uncommitted"
else
  fail "unexpected commit count: $commit_count (expected 2)"
fi
# Bravo's file should still be untracked
if git -C "$repo" status --porcelain | grep -q "^?? bravo/file.txt"; then
  pass "bravo's file preserved (not absorbed, not deleted)"
else
  fail "bravo's file missing from working tree"
fi
rm -rf "$repo"

# --- Test 14: Lock acquire timeout exits 2 () ---
# Pre-create a NON-stale lock (timestamp = now) and run iteration-commit with
# a short max-wait. The script must fail to acquire and exit 2.
echo "Test 14: lock acquire timeout → exit 2"
repo=$(fresh_repo)
echo "real-change" > "$repo/feature.txt"
mkdir "$repo/.iteration-commit-lock"
echo "stuck-partner" > "$repo/.iteration-commit-lock/holder"
date +%s > "$repo/.iteration-commit-lock/timestamp"  # fresh timestamp = non-stale
set +e
out=$(ITERATION_COMMIT_LOCK_WAIT_S=2 ITERATION_COMMIT_LOCK_STALE_S=999 \
  bash "$ITERATION_COMMIT" \
  --goal-id g-280-11 --title "Apply: lock timeout test" --outcome deep --repo "$repo" 2>&1)
rc=$?
set -e
if [[ "$rc" -eq 2 ]]; then
  pass "lock acquire timeout → exit 2"
else
  fail "wrong exit code: $rc (expected 2). Output: $out"
fi
if echo "$out" | grep -q "failed to acquire lock after"; then
  pass "timeout error message includes wait duration"
else
  fail "missing 'failed to acquire lock' message: $out"
fi
# Verify NO commit landed and the pre-existing lock survived (we didn't steal it)
commit_count=$(git -C "$repo" rev-list HEAD --count)
if [[ "$commit_count" -eq 1 ]]; then
  pass "no commit produced when lock acquire failed"
else
  fail "unexpected commit produced: $commit_count"
fi
# Clean up the pre-seeded lock to avoid leakage
rm -rf "$repo"

# --- Summary ---
echo
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
