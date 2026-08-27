#!/usr/bin/env bash
# mind-api-code-changed.sh — SINGLE-SOURCE predicate: did the daemon's code
# surface change between <BASE> and HEAD?
#
# The runtime daemon (`python -m mind_api.src`) loads these files into its
# long-lived process:
#   1. everything under mind_api/src/**     (the daemon package itself)
#   2. core/scripts/_*.py                 (underscore-prefixed shared modules
#                                          it imports)
#   3. specific NON-underscore core/scripts modules imported via the sys.path
#                                          insert (file_locks.py et al. prepend
#                                          core/scripts onto sys.path):
#                                            core/scripts/gates/**  (gate evaluators)
#                                            core/scripts/storage_backend.py
#                                            core/scripts/owncloud_backend.py
#                                            core/scripts/coordination_merge.py
#                                            core/scripts/owncloud_sync.py
#                                            core/scripts/retrieve.py
#                                            core/scripts/tree_idf.py
#                                            core/scripts/trigger_firings.py
#                                            core/scripts/experience.py   (see NOTE)
#                                            core/scripts/tree.py
#                                            core/scripts/tree_match.py
#                                            core/scripts/predicate.py
#                                            core/scripts/aspirations.py
#                                            core/scripts/peer_surface.py
#                                            core/scripts/git_ref_claim.py
#
#      peer_surface.py added 2026-08-14 (): board_write.py's post
#      handler now imports suspected_routing_tags from it for the routing-tag
#      advisory, which puts it in the daemon's import surface. Caught by
#      test_daemon_import_surface.py in the same run that introduced the
#      import — without this entry a commit touching ONLY peer_surface.py
#      would diff clean, post-commit would skip the restart, and the daemon
#      would serve the old matcher.
#
#      predicate.py + aspirations.py added 2026-08-08 (, reducer pass on
#      hostname cc-04, uname -r 6.8.0-136-generic). Both entered the daemon surface
#      via 's own commit 0a0b81427, which created
#      core/scripts/gates/check_schema.py — the only `import predicate` that REACHES
#      THE DAEMON, via mind_api/src/endpoints/aspirations_write.py:100
#      (`from gates.check_schema import evaluate`). This line read "the ONLY non-test
#      `import predicate` in core/scripts + mind_api" until 2026-08-09; measured
#      (alpha, hostname cc-04, uname -r 6.8.0-136-generic) that is FALSE —
#      recurring-precondition-sweep.py, verify-check-eval.py,
#      precondition-defer-recheck.py and recurring-starvation-check.py all import it.
#      None of the four is reachable from mind_api/src, so the DAEMON-scoped claim
#      holds and the worker-inference correction at the end of this block still
#      stands. Stated imprecisely, though, a reader who greps finds FIVE importers
#      and mistrusts the block that depends on there being one. The gate reads
#      predicate.PREDICATE_TYPES and parses its `reason=` literals, so a commit
#      touching ONLY predicate.py diffed CLEAN, post-commit skipped the restart, and
#      the daemon would keep validating filings against a STALE type list —
#      refusing checks that had just become valid.
#
#      That is not hypothetical: sibling goal  ships exactly such a
#      commit (TYPE_ALIASES + the new not_machine_checkable type, both in
#      predicate.py alone). The two goals shipped a day apart into the same daemon.
#
#      The worker pass judged this red "NOT mine" after removing check_schema.py and
#      seeing the same ['aspirations','predicate'] list. That inference does not
#      hold — with the only importer deleted, `predicate` cannot be in the surface,
#      so the removal probe was measuring something other than it claimed. Recorded
#      because "I removed my change and the failure persisted" is otherwise a
#      strong-looking exoneration (guard-1448 solo-rerun discipline says re-run the
#      FILE; it does not license inferring the CAUSE from an unchanged failure list).
#
#      This list is NOT hand-maintained guesswork — it is COMPUTED and pinned.
#      core/scripts/tests/test_daemon_import_surface.py recomputes the daemon's
#      real core/scripts import surface and FAILS if any member is unmatched by
#      the pathspec below. Adding an import to daemon code without adding it here
#      now breaks a test instead of silently serving stale code. Run that test
#      after ANY new import in mind_api/src or in a module it loads.
#
#      The 5 modules added 2026-07-14 () were ALL missing while being
#      loaded. A grep of top-of-file imports does NOT find them and that is
#      exactly how the gap survived — most are lazy in-function imports:
#        coordination_merge <- owncloud_backend.py:122 (LAZY; owns _union_goals +
#                              the concurrent-mint re-key -> a stale daemon copy
#                              silently serves OLD merge semantics)
#        owncloud_sync      <- mind_api/src/endpoints/admin.py:77,149 +
#                              __main__.py:348 + owncloud_backend.py:537 (LAZY)
#        retrieve           <- mind_api/src/endpoints/retrieve.py:68 (EAGER!) —
#                              the retrieval ENGINE was never in the pathspec, so
#                              every commit to it since the daemon-only cutover
#                              skipped the restart and the daemon served a stale
#                              retrieve.py
#        trigger_firings    <- core/scripts/retrieve.py:82 (eager, via retrieve)
#        tree_idf           <- retrieve.py:1663 + tree_match.py:520 (LAZY)
#
#      Added 2026-08-20 ( — near-dup refusal at store append):
#        store_dupe_warn    <- mind_api/src/endpoints/store.py _near_dup_verdict
#                              (LAZY, in-function) — owns the refuse_threshold
#                              config; a stale daemon copy silently serves OLD
#                              thresholds against the live corpus
#        mdl_gate           <- store_dupe_warn.refuse_check (LAZY) — the
#                              tokenize/jaccard/nearest primitives the verdict
#                              is computed with
#
#      NOTE on experience.py: it is in the pathspec but is NOT in the computed
#      surface — mind_api/src/store_registry.py:327 says "Do NOT `from experience
#      import` — daemon-import-unsafe", and nothing under mind_api/src imports it
#      (the daemon's experience endpoint reimplements via _jsonl_common). It is
#      therefore OVER-INCLUSIVE: touching it forces a restart the daemon does not
#      need ("pure churn"). Left in deliberately — removing an entry moves AWAY
#      from the fail-toward-restart guarantee and deserves its own goal, not a
#      drive-by delete. The test allowlists it as a known over-inclusion.
#      owncloud_backend.py added 2026-07-14 (). It was MISSING while
#      being TRANSITIVELY loaded: storage_backend.py (in the list) instantiates
#      OwnCloudBackend.from_env() at import-use, so the daemon holds this module
#      in-process. A commit touching ONLY owncloud_backend.py therefore diffed
#      CLEAN against this pathspec -> post-commit skipped the restart -> the
#      daemon kept serving the OLD storage backend (stale _put / fence /
#      _overwrite_decision / root-map). That is precisely the "never serve stale
#      daemon code" guarantee this predicate exists to uphold (rb-711, rb-936,
#      guard-559). NOTE the same transitive-load question is OPEN for
#      coordination_merge.py and owncloud_sync.py (both lazily imported INSIDE
#      owncloud_backend functions, so they land in the daemon process at first
#      use) — audited under a follow-up goal, not assumed here.
#                                          The `_` prefix is NOT the boundary —
#                                          these non-underscore modules are
#                                          loaded too. Audited 2026-05-31 against
#                                          the E402/sys.path-resolved import set
#                                          in mind_api/src. If the daemon's
#                                          import surface changes, update BOTH
#                                          this list AND the pathspec below.
#
# A commit that touches only docs, world/, meta/, an agent dir, a *.sh
# wrapper, or a standalone (NON-imported) CLI cannot change daemon behaviour —
# the running process is still current. Recycling it then is pure churn.
#
# Three callers share THIS one predicate (do NOT inline the pathspec into
# any — the boundary lives here and only here):
#   - core/githooks/post-commit             BASE = HEAD~1
#       restart-on-commit trigger (recycle only when the commit touched
#       daemon code).
#   - core/githooks/post-merge              BASE = ORIG_HEAD
#       restart-on-merge trigger (merges bypass post-commit; ORIG_HEAD is the
#       pre-merge HEAD, so the FULL merged range — including fast-forwards
#       that move HEAD by N commits with no merge commit — is diffed, which
#       HEAD~1 would miss). .
#   - core/scripts/_runtime.sh rt_check_staleness   BASE = daemon's running
#       /v1/admin/health git_head_sha
#       narrows the  stale-code auto-restart so a docs/world-only
#       commit no longer warns-and-recycles a daemon whose code is current.
#
# Contract:
#   $1 = BASE ref (any git-resolvable commit-ish — a full SHA or HEAD~1)
#   exit 0  → daemon code surface changed in BASE..HEAD (restart warranted)
#   exit 1  → no daemon-surface change (the running daemon is current)
#
# Fail-toward-restart: ANY git error (missing/un-resolvable BASE, not a
# repo, first-commit HEAD~1, GC'd SHA) exits 0. The correctness guarantee
# is "never serve stale daemon code"; a spurious restart is cheap, a missed
# restart serves stale routes/resolvers (rb-711, rb-936, guard-559). When
# we cannot PROVE "unchanged", we restart.

set -uo pipefail

BASE="${1:-}"
if [ -z "$BASE" ]; then
    # No base to diff against → cannot prove "unchanged" → fail toward restart.
    exit 0
fi

# Derive PROJECT_ROOT inline — this predicate needs ONLY the repo root,
# never WORLD/META/AGENT_DIR. Sourcing _paths.sh would also run its
# Windows-shell-auto-detect block (cygpath + uname + command -v subprocess
# chain) and Python-shim setup — ~2 seconds on Windows, paid SYNCHRONOUSLY
# by the post-commit hook before its detached spawn fires. Inline derivation
# is one subshell (~50 ms). The structural assumption (this script lives at
# <repo>/core/scripts/) is the SAME assumption _paths.sh makes; both encode
# it identically with `..` traversals — neither is "more authoritative."
# CRITICAL — TWO `..`s. dirname($0)=core/scripts, /..=core, /../..=<repo>.
# One `..` would give CORE_ROOT, not PROJECT_ROOT, and `git -C <core>` would
# still work (it walks up to find .git) but the pathspec `mind_api/src` would
# resolve relative to the WRONG cwd in some git versions. Don't shorten it.
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GIT=(git -C "$PROJECT_ROOT")

# Resolve BASE to a commit. A bad/unknown/absent ref must fail TOWARD
# restart, never be silently treated as "unchanged".
"${GIT[@]}" rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null 2>&1 || exit 0

# CRITICAL — the pathspec below IS the daemon-code boundary and the single
# source of truth for it. The single-quotes are load-bearing: they stop the
# SHELL from glob-expanding `_*.py` so git receives the literal pathspec and
# does its own match. Do NOT inline this list into the post-commit hook or
# into _runtime.sh — both must call this script so the boundary stays in one
# place. Changing the daemon's import surface? Change this pathspec AND the
# import-surface list in the header comment above (keep the two in sync).
# B1: retry transient git-lock contention before failing toward restart.
# A concurrent agent commit (or the operator's) briefly holds .git/index.lock
# or a ref lock; the bare `|| exit 0` below used to treat that transient error
# as "cannot prove unchanged -> restart", producing the soak's spurious
# "pure fail-open" daemon recycles. We retry ONLY transient lock errors; any
# other error, or a transient one that persists past the retries, still exits
# 0 (fail toward restart — never serve stale code). Hot path: first try
# succeeds, zero added latency. stderr is folded into the capture to classify
# the error: on rc=0 the capture is the file list (stderr empty); on rc!=0 it
# is the error text (git diff fails before listing any file).
changed=""
_diff_rc=1
for _attempt in 1 2 3; do
    _out="$("${GIT[@]}" diff --name-only "$BASE" HEAD -- \
        mind_api/src \
        'core/scripts/_*.py' \
        core/scripts/gates \
        core/scripts/storage_backend.py \
        core/scripts/owncloud_backend.py \
        core/scripts/coordination_merge.py \
        core/scripts/owncloud_sync.py \
        core/scripts/retrieve.py \
        core/scripts/tree_idf.py \
        core/scripts/trigger_firings.py \
        core/scripts/store_dupe_warn.py \
        core/scripts/mdl_gate.py \
        core/scripts/experience.py \
        core/scripts/tree.py \
        core/scripts/tree_match.py \
        core/scripts/predicate.py \
        core/scripts/aspirations.py \
        core/scripts/peer_surface.py \
        core/scripts/git_ref_claim.py \
        2>&1)"
    _diff_rc=$?
    if [ "$_diff_rc" -eq 0 ]; then
        changed="$_out"
        break
    fi
    case "$_out" in
        *index.lock*|*"cannot lock"*|*"Unable to create"*|*"nother git process"*)
            sleep 0.2 ;;          # transient lock — retry
        *)
            break ;;              # non-transient error — fail toward restart now
    esac
done
[ "$_diff_rc" -eq 0 ] || exit 0   # error (incl. exhausted transient retries) -> fail toward restart

if [ -n "$changed" ]; then
    exit 0   # daemon code surface changed → restart warranted
fi
exit 1       # no daemon-surface change → running daemon is current
