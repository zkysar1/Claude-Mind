#!/usr/bin/env bash
# promote-to-upstream.sh — Promote this repo's framework release ONE step down
# the chain (frontier->seed, or seed->downstream) into a local clone of the
# target repo, via the seed-plant machinery. The SCRIPT opens a PR (with --pr).
# It does NOT merge by DEFAULT; the AGENT may merge that PR as a separate
# verified step once it is mergeable + checks pass (user-granted 2026-06-06;
# guard-680 / capability-routing grant-002). --auto-merge OPTS IN to merging in
# the same run, under the preconditions documented at that flag below.
#
# Usage:
#   bash core/scripts/promote-to-upstream.sh --target <path-to-target-clone> \
#        [--branch "promote/vX.Y.Z"] [--pr] [--auto-merge] [--dry-run] \
#        [--living-prod] [--force-past-plan "<justification>"]
#
#   --living-prod: force living-prod mode — pass --living-prod through to
#     seed-transplant so the target's deployment-local files (CLAUDE.md,
#     .claude/settings.json) and its OWN resident forged skills are preserved.
#     Auto-enabled when the target carries a resident .mind-data/ store or a
#     git-tracked in-repo world/ or meta/ store. For a living/populated dest a
#     read-only --plan blast-radius report runs BEFORE the plant ( P1.5).
#     See guard-1056: the seed pipeline has known living-prod bugs () —
#     verify deployment-local + forged-skill survival post-promote.
#
#   --force-past-plan "<justification>": proceed even when the pre-plant --plan
#     returns DO NOT PROMOTE (rc 21 — the destination carries framework lines
#     this source lacks, so planting DELETES them). Requires a written reason;
#     the valueless form is a usage error, because a bare boolean would let the
#     gate be waved through by reflex. Prefer back-porting the prod-ahead files
#     UP to this source (guard-119) over overriding. rc 20 (REVIEW REQUIRED) is
#     advisory and does not need this flag. ()
#
# Exit: 0 success/dry-run-ok; 1 pre-flight/invariant/chain failure (now including
#   a DO NOT PROMOTE plan verdict); 2 usage error.
# Design: rails A.7 + omni delta M2 (never push/merge from here beyond a PR) +
# guardrails CW2 (hard invariant) / CW4 (chain order).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo "ERROR: failed to source _paths.sh" >&2; exit 2; }
LIB="$SCRIPT_DIR/_release_lib.py"
INIT_PY="$PROJECT_ROOT/mind_api/src/__init__.py"
# RELEASES.json check delegated to check-releases-current.sh (role-aware, );
# this script no longer references RELEASES.json directly.
WORLD="${WORLD_DIR:-${WORLD_PATH:-}}"
OVERLAY="$WORLD/config/compatibility.yaml"
FW_COMPAT="$CONFIG_DIR/compatibility.yaml"

TARGET=""; BRANCH=""; DO_PR=0; DRY=0; LIVING_PROD=0; FORCE_PAST_PLAN=""; AUTO_MERGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; [[ -z "$TARGET" || "$TARGET" == --* ]] && { echo "ERROR: --target requires a path" >&2; exit 2; }; shift $(( $# >= 2 ? 2 : 1 ));;
    --branch) BRANCH="${2:-}"; [[ -z "$BRANCH" || "$BRANCH" == --* ]] && { echo "ERROR: --branch requires a value" >&2; exit 2; }; shift $(( $# >= 2 ? 2 : 1 ));;
    --pr) DO_PR=1; shift;;
    # Opt-in merge of the PR this run just opened. OFF by default: the default
    # contract stays "open a PR, let a human or a later verified step merge it".
    --auto-merge) AUTO_MERGE=1; shift;;
    --dry-run) DRY=1; shift;;
    --living-prod) LIVING_PROD=1; shift;;
    # Escape hatch for a DO NOT PROMOTE verdict (). Requires a written
    # justification — a bare boolean would let the gate be waved through by
    # reflex, and the whole defect being fixed is a refusal nobody had to read.
    # The justification is echoed to stdout and carried into the plant log.
    --force-past-plan) FORCE_PAST_PLAN="${2:-}"; [[ -z "$FORCE_PAST_PLAN" || "$FORCE_PAST_PLAN" == --* ]] && { echo "ERROR: --force-past-plan requires a justification string" >&2; exit 2; }; shift $(( $# >= 2 ? 2 : 1 ));;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//;/^set -euo/d'; exit 0;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2;;
  esac
done
[[ -n "$TARGET" ]] || { echo "ERROR: --target <path-to-target-clone> is required" >&2; exit 2; }
# --auto-merge without --pr has nothing to merge. Reject at parse time rather
# than no-op'ing silently at Step 6 — an accepted-but-inert flag is worse than a
# rejected one, because the caller believes it took effect (guard-386, rb-538).
# `||` form (not `&&`) mirrors the --target guard above and is set -e safe.
[[ $AUTO_MERGE -eq 0 || $DO_PR -eq 1 ]] || { echo "ERROR: --auto-merge requires --pr (there is no PR to merge without it)" >&2; exit 2; }

say() { echo "[promote] $*"; }
fail() { echo "[promote] ERROR: $*" >&2; exit 1; }

# --- Step 0: resolve self role + the single legal target role (CW4) --------
# `import yaml` is INSIDE the try so a missing pyyaml is handled gracefully
# (prints ERR|, exit 0) rather than crashing before the handler. The set +e /
# RC check converts a HARD failure (py -3 not found, or a future SyntaxError in
# this source) from a silent set -e death into an actionable diagnostic — the
# same silent-exit-1 class that the ','.join bug originally produced. (review F5)
set +e
ROLES="$(OVERLAY="$OVERLAY" FW="$FW_COMPAT" py -3 -c '
import os
try:
    import yaml
    ov = yaml.safe_load(open(os.environ["OVERLAY"], encoding="utf-8")) or {}
    fw = yaml.safe_load(open(os.environ["FW"], encoding="utf-8")) or {}
    chain = [c.get("role") for c in (fw.get("promotion_chain") or [])]
    self_role = ov.get("self_role") or ""
    if not self_role or self_role not in chain:
        print("ERR|self_role missing or not in chain"); raise SystemExit(0)
    i = chain.index(self_role)
    if i + 1 >= len(chain):
        print("NOTARGET|" + self_role); raise SystemExit(0)
    print(f"{self_role}|{chain[i+1]}|" + ",".join(chain))
except SystemExit:
    raise
except Exception as e:
    print("ERR|" + str(e).replace("|", " "))
' 2>/dev/null)"
RPY_RC=$?
set -e
[[ $RPY_RC -eq 0 ]] || fail "role-resolution python failed (exit $RPY_RC) -- is 'py -3' + pyyaml available? cannot resolve the promotion chain"
IFS='|' read -r SELF_ROLE TARGET_ROLE CHAIN <<< "$ROLES"
if [[ "$SELF_ROLE" == "NOTARGET" ]]; then fail "role '$TARGET_ROLE' is the end of the chain — nothing downstream to promote to (CW4)"; fi
if [[ "$SELF_ROLE" == "ERR" || -z "$SELF_ROLE" ]]; then fail "cannot resolve promotion roles: ${TARGET_ROLE:-unknown}"; fi
# CW4: hard chain-order check via the lib.
if ! py -3 "$LIB" check-promotion-order "$CHAIN" "$SELF_ROLE" "$TARGET_ROLE" >/dev/null; then
  fail "promotion $SELF_ROLE -> $TARGET_ROLE is not a single downstream step (CW4)"
fi
say "promoting $SELF_ROLE -> $TARGET_ROLE  (target clone: $TARGET)"

# --- Step 1: pre-flight ----------------------------------------------------
[[ -d "$TARGET" ]] || fail "target is not a directory: $TARGET"
LOCAL="$(grep -E '^__version__' "$INIT_PY" | sed -E 's/.*"([^"]+)".*/\1/' || true)"
[[ -n "$LOCAL" ]] || fail "could not read local __version__"
say "local version: $LOCAL"

# (a) RELEASES.json is current — DELEGATED to check-releases-current.sh, the
# single role-aware canonical checker (seed-preflight check #7 also runs it).
# RELEASES.json is FRONTIER-ONLY provenance: release.sh writes it and only the
# frontier cuts releases (core/config/compatibility.yaml promotion_chain). A
# non-frontier role (seed/downstream) legitimately has no RELEASES.json, and
# check-releases-current.sh PASSes it as N/A (version SSOT __version__ is
# authoritative) — which is what lets a seed->downstream promote run WITHOUT
# --force-release. The prior inline `seed-latest` check was a duplicate of
# check-releases-current.sh that dropped its role-awareness and hard-failed
# EVERY non-frontier promote (). Single source of truth now.
CRC_OUT="$(bash "$SCRIPT_DIR/check-releases-current.sh" 2>&1)" \
  || fail "RELEASES.json not current: $CRC_OUT (cut a release with release.sh before promoting)"
say "$CRC_OUT"

# (b) working tree clean — enforced for ALL roles (promoting an uncommitted
#     tree is wrong regardless of role). (c) HEAD tagged v$LOCAL is frontier-only
#     (guarded below).
#
# SOURCE-PROVENANCE PREDICATE (). Both conditions are evaluated by ONE
# function because they are re-checked a SECOND time immediately before the plant
# (Step 4a.9), and two hand-maintained copies of this predicate would drift —
# which is the same class of defect this re-check exists to catch.
#
# WHY BOTH CONDITIONS, TOGETHER, ARE THE WHOLE GUARANTEE: a clean working tree
# AND HEAD == the v$LOCAL commit together imply the working tree's content IS the
# tag's content. seed-transplant copies from the WORKING TREE (`--source
# "$PROJECT_ROOT"`, _seed_engine copy-staged), so asserting both at plant time is
# logically equivalent to planting from the tagged commit — without changing
# seed-transplant's source resolution, which is deliberately tag-agnostic
# (it is also invoked standalone, where no tag exists at all).
# Re-checking HEAD==tag ALONE would be strictly weaker: a tree dirtied after the
# assertion still injects untagged content while HEAD stays put.
#
# Sets SRC_DRIFT_KIND (dirty|no-tag|head-not-tag|"") + SRC_DRIFT_DETAIL.
# Returns 0 when the source faithfully represents v$LOCAL, 1 otherwise.
# Evaluates the two conditions INDEPENDENTLY and accumulates every kind found
# into SRC_DRIFT_KIND (space-separated). Deliberately not first-match-wins: the
# dry-run below exists to report everything wrong BEFORE an operator commits to a
# 15-minute run, so short-circuiting after the first would make them fix one
# problem, re-run, and discover the next. `dirty` is independent of the tag
# checks; `no-tag` and `head-not-tag` are mutually exclusive (you cannot compare
# against a tag that does not exist).
source_provenance_drift() {
  SRC_DRIFT_KIND=""; SRC_DRIFT_DETAIL=""
  local _dirty _head_sha _tag_sha _drc=0
  _add() { SRC_DRIFT_KIND="${SRC_DRIFT_KIND:+$SRC_DRIFT_KIND }$1"
           SRC_DRIFT_DETAIL="${SRC_DRIFT_DETAIL:+$SRC_DRIFT_DETAIL; }$2"; }

  # FAIL CLOSED (). `2>/dev/null || true` discarded BOTH stderr and
  # the exit code, so ANY git failure produced an empty string that this
  # function read as "working tree is clean". stderr is now routed INTO the
  # capture and rc is checked explicitly, so a failure yields NON-empty output
  # and reads as dirty. Sanctioned sibling idiom: iteration-commit.sh:492.
  #
  # The likeliest failure here is .git/index.lock contention from a partner's
  # concurrent iteration-commit.sh — so the check was least trustworthy exactly
  # when partners are committing, which is the live-fleet condition the
  #  TOCTOU re-check exists for. That re-check calls this same
  # predicate a second time, so a wedged git made BOTH calls report clean.
  # --no-optional-locks removes the contention at the source: a status probe
  # can no longer take that lock at all. Verified by running it on git 2.43.0
  # (the goal required verifying, not assuming); the flag appeared nowhere in
  # core/scripts before this fix.
  #
  # NOTE the asymmetry this restores: the TAG half below already fails CLOSED
  # (rev-parse -q --verify routes to no-tag; the two unsuppressed rev-parse /
  # rev-list calls abort under set -e). Only the dirty half swallowed.
  _dirty="$(git --no-optional-locks -C "$PROJECT_ROOT" status --porcelain 2>&1)" || _drc=$?
  if [[ $_drc -ne 0 ]]; then
    _dirty="git status failed (rc=$_drc) — treating as DIRTY: ${_dirty:-<no stderr>}"
  fi
  if [[ -n "$_dirty" ]]; then
    _add dirty "$(printf '%s' "$_dirty" | head -5 | tr '\n' ';')"
  fi
  # Tag provenance is FRONTIER-ONLY. release.sh is the sole v-tagger and only
  # runs at the frontier; a non-frontier role (seed/downstream) re-transplants
  # adopted framework and has no v-tag by design (, option 2
  # role-conditional gating).
  if [[ "$SELF_ROLE" == "frontier" ]]; then
    if ! git -C "$PROJECT_ROOT" rev-parse -q --verify "refs/tags/v$LOCAL" >/dev/null 2>&1; then
      _add no-tag "v$LOCAL"
    else
      _head_sha="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
      _tag_sha="$(git -C "$PROJECT_ROOT" rev-list -n1 "v$LOCAL")"
      [[ "$_head_sha" == "$_tag_sha" ]] || _add head-not-tag "HEAD=$_head_sha v$LOCAL=$_tag_sha"
    fi
  fi
  [[ -z "$SRC_DRIFT_KIND" ]]
}

if ! source_provenance_drift; then
  # Report EVERY kind found. A real promote fails on the first (fail exits); a
  # dry-run emits all notes, matching the pre- diagnostic behavior.
  for _kind in $SRC_DRIFT_KIND; do
    case "$_kind" in
      dirty)
        if [[ $DRY -eq 1 ]]; then say "[dry-run] note: working tree dirty (would FAIL a real promote)";
        else fail "working tree is dirty — commit before promoting"; fi ;;
      no-tag)
        if [[ $DRY -eq 1 ]]; then say "[dry-run] note: HEAD has no tag v$LOCAL (would FAIL — cut a release with release.sh first)";
        else fail "no tag v$LOCAL — promote only TAGGED releases (run release.sh first)"; fi ;;
      head-not-tag)
        if [[ $DRY -eq 1 ]]; then say "[dry-run] note: HEAD is not the v$LOCAL commit (would FAIL)";
        else fail "HEAD is not the tagged v$LOCAL commit"; fi ;;
    esac
  done
fi
if [[ "$SELF_ROLE" != "frontier" ]]; then
  say "skip tag-check: role '$SELF_ROLE' is non-frontier — v-tags are frontier-only (release.sh); a seed re-transplants adopted framework (g-115-1811)"
fi

# --- Step 2: frontier-invariant (CW2 — HARD) -------------------------------
TARGET_INIT="$TARGET/mind_api/src/__init__.py"
if [[ -f "$TARGET_INIT" ]]; then
  TGT_VER="$(grep -E '^__version__' "$TARGET_INIT" | sed -E 's/.*"([^"]+)".*/\1/' || true)"
  if [[ -n "$TGT_VER" ]]; then
    set +e; CMP="$(py -3 "$LIB" compare "$LOCAL" "$TGT_VER")"; CRC=$?; set -e
    if [[ $CRC -ne 0 ]]; then fail "target version '$TGT_VER' is not valid semver"; fi
    if [[ "$CMP" == "-1" ]]; then fail "INVARIANT VIOLATION (CW2): local $LOCAL < target $TARGET_ROLE $TGT_VER — cannot promote backwards"; fi
    say "frontier-invariant OK: local $LOCAL >= target $TGT_VER"
  else
    fail "target $TARGET has no readable __version__ (not a framework repo?)"
  fi
else
  fail "target $TARGET has no mind_api/src/__init__.py (not a framework repo?)"
fi

# --- Step 3: seed-preflight (7 checks, incl. releases-current) -------------
say "running seed-preflight (publishability gate)..."
if ! bash "$SCRIPT_DIR/seed-preflight.sh" --quiet; then
  fail "seed-preflight FAILED — not publishable; fix the failing checks before promoting"
fi
say "seed-preflight: PUBLISHABLE"

# --- Step 3b: promotion-preflight (reconcile-not-mirror drift gate) --------
# : the gate existed since its authoring but had ZERO callers — the
# target-drift audit (and the  weights-contract cross-check) only
# fired on manual invocation, which is exactly how the rb-498-era promotion
# clobbered prod-side content. seed-preflight answers "is the SOURCE
# publishable?"; promotion-preflight answers "does the TARGET lead on anything
# a mirror would clobber?" — both must pass. Runs for dry-run too (before the
# dry-run stop). Exit 2 = DRIFT: hard-fail per the gate's contract; conscious
# acceptance via PROMOTE_ALLOW_DRIFT=1 (loud warning, e.g. a first wired run
# over known pre-existing divergence being reconciled separately).
say "running promotion-preflight (reconcile-not-mirror drift gate)..."
set +e
bash "$SCRIPT_DIR/promotion-preflight.sh" --source "$PROJECT_ROOT" --target "$TARGET"
PF_RC=$?
set -e
if [[ $PF_RC -eq 0 ]]; then
  say "promotion-preflight: CLEAN"
elif [[ $PF_RC -eq 2 ]]; then
  if [[ "${PROMOTE_ALLOW_DRIFT:-0}" == "1" ]]; then
    say "WARNING: promotion-preflight detected DRIFT — proceeding because PROMOTE_ALLOW_DRIFT=1 (drift consciously accepted; reconcile after)"
  elif [[ $DRY -eq 1 ]]; then
    say "[dry-run] note: promotion-preflight detected DRIFT (would FAIL a real promote — back-port/reconcile first, or PROMOTE_ALLOW_DRIFT=1 to consciously accept)"
  else
    fail "promotion-preflight DRIFT — target leads on framework content or carries orphaned meta-strategy weights; back-port/reconcile before promoting (or PROMOTE_ALLOW_DRIFT=1 to consciously accept)"
  fi
else
  fail "promotion-preflight errored (exit $PF_RC) — cannot audit target drift; fix the gate invocation before promoting"
fi

[[ -z "$BRANCH" ]] && BRANCH="promote/v$LOCAL"

# --- Step 3c: living-prod detection (guard-1056) ---------------------------
# A living/populated dest must be planted with --living-prod so its
# deployment-local files (CLAUDE.md, .claude/settings.json) + its OWN resident
# forged skills are preserved. Detection errs toward preservation (ANY positive
# signal => living-prod): a fresh seed has nothing to preserve, so --living-prod
# is a safe no-op there, whereas MISSING it on a real living dest clobbers
# deployment-local — the manual-hop hazard this fixes (v2.5.0 seed->prod, where
# Step 4b's flag-less transplant would have overwritten settings.json + CLAUDE.md
# and deleted resident forged skills at ZDS-Mind).
if [[ $LIVING_PROD -eq 0 ]]; then
  if [[ -d "$TARGET/.mind-data" ]]; then
    LIVING_PROD=1; say "auto-detected living-prod: '$TARGET/.mind-data' present (guard-1056)"
  elif [[ -n "$(git -C "$TARGET" ls-files world/ meta/ 2>/dev/null | head -1)" ]]; then
    LIVING_PROD=1; say "auto-detected living-prod: git-tracked in-repo world/ or meta/ store in target (guard-1056)"
  fi
fi
LP_FLAG=""; [[ $LIVING_PROD -eq 1 ]] && { LP_FLAG="--living-prod"; say "living-prod mode ON — preserve deployment-local + dest forged skills; --plan runs first (guard-1056)"; }

# --- Dry-run stops here (no mutation of the target) ------------------------
if [[ $DRY -eq 1 ]]; then
  [[ $DO_PR -eq 1 ]] && say "[dry-run] would: add an ISOLATED WORKTREE for PR branch '$BRANCH' FIRST and plant into it (the plant commits there; the live checkout at $TARGET never switches branch), then tear the worktree down after the push"
  [[ $LIVING_PROD -eq 1 ]] && say "[dry-run] would: seed-transplant.sh \"$TARGET\" --living-prod --plan  (read-only blast-radius report FIRST — g-306-90/guard-1056)"
  say "[dry-run] would: seed-transplant.sh \"$TARGET\" ${LP_FLAG:+$LP_FLAG }--force --commit  (domain-strip + transforms + verify${LP_FLAG:+; living-prod: deployment-local + dest forged skills preserved})"
  [[ $DO_PR -eq 1 && $AUTO_MERGE -eq 0 ]] && say "[dry-run] would: push branch '$BRANCH' + gh pr create (does NOT merge — pass --auto-merge to merge in-run)"
  [[ $DO_PR -eq 1 && $AUTO_MERGE -eq 1 ]] && say "[dry-run] would: push branch '$BRANCH' + gh pr create + gh pr merge --merge (--auto-merge ON; merges only when MERGEABLE, then settles the verdict at origin — guard-1897)"
  say "[dry-run] would: seed-verify.sh \"$TARGET\" --expect-commit"
  say "[dry-run] OK — all pre-flight + invariant + preflight checks passed"
  exit 0
fi

# --- Step 4: (if --pr) create the PR branch in an ISOLATED WORKTREE ---------
# The plant commit MUST land on the PR branch, not the target's main. If the
# branch is created AFTER seed-transplant --commit (which commits onto whatever
# branch is currently checked out), the commit is already on main and the PR
# diff is EMPTY — the change is effectively merged by the agent (M2 violation).
# (review F3, HIGH)
#
# WHY A WORKTREE AND NOT `git checkout -b` (). The previous form ran
# `cd "$TARGET" && git checkout -b "$BRANCH"`, which SWITCHES THE LIVE CHECKOUT
# to the PR branch — and leaves it there, because nothing switches it back. The
# target is a working deployment whose agents read that tree; a promotion that
# silently repoints their checkout at an unmerged branch is the one genuinely
# unique hazard here, and it survives long past the promotion (a failed plant,
# a failed verify, or a crashed run all exit with the live checkout moved).
# A worktree gives the plant its own directory and its own HEAD, so the live
# checkout's branch is never touched at all — not during, not after, not on any
# failure path.
#
# PLANT_DIR is the seam: it is $TARGET in the normal (no --pr) flow, preserving
# the existing "commit onto whatever branch is checked out" behaviour exactly,
# and the isolated worktree under --pr. Every downstream step (plan, plant,
# verify, push) reads PLANT_DIR; the ONE step that deliberately keeps reading
# $TARGET is the living-prod autodetect above — see the note at Step 4a.
PLANT_DIR="$TARGET"
WT_DIR=""
_wt_torn_down=0
_wt_teardown() {
  [[ -n "$WT_DIR" && $_wt_torn_down -eq 0 ]] || return 0
  _wt_torn_down=1
  # Teardown is unconditional, including on the failure paths, and loses
  # NOTHING durable: `git worktree remove` deletes the checkout directory, not
  # the branch. A plant that committed before failing leaves its commit on
  # '$BRANCH' in $TARGET's object store, reachable with `git -C "$TARGET" log
  # "$BRANCH"`. Leaving the worktree behind, by contrast, wedges the NEXT run —
  # `git worktree add` refuses a branch already checked out elsewhere.
  # --owner is explicit rather than left to derivation: the worktree may already
  # be gone (a crash, a hand-cleanup) and derivation needs the directory to ask.
  bash "$SCRIPT_DIR/worktree-teardown.sh" "$WT_DIR" --owner "$TARGET" --force --quiet \
    || say "WARNING: worktree teardown reported a problem for $WT_DIR — remove it by hand (git -C \"$TARGET\" worktree remove --force \"$WT_DIR\")"
}
if [[ $DO_PR -eq 1 ]]; then
  WT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/promote-wt-XXXXXX")" \
    || fail "could not create a temp dir for the promotion worktree"
  rmdir "$WT_DIR" 2>/dev/null || true   # git worktree add wants to create it
  trap _wt_teardown EXIT
  # A branch cannot live in two worktrees, so if the target's LIVE checkout is
  # already sitting on '$BRANCH' the add fails rc=128 with git's own message
  # ("already used by worktree at <target>"), which does not say what to do.
  # This is not a hypothetical: it is precisely the state the PRE-
  # code left behind, so the FIRST run of this version against a
  # previously-promoted target lands here. Diagnose it explicitly rather than
  # forcing — `worktree add --force` would put the same branch in two working
  # trees, and the whole point of this step is to stop touching the live one.
  _live_branch="$(git -C "$TARGET" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  if [[ "$_live_branch" == "$BRANCH" ]]; then
    fail "the target's LIVE checkout at $TARGET is itself on '$BRANCH', so it cannot also be checked out in a worktree. This is the residue of a promotion run BEFORE g-115-4803 (which switched the live checkout and never switched it back). Move the deployment back to its own branch first — e.g. 'git -C \"$TARGET\" checkout main' — then re-run. Nothing has been mutated."
  fi
  # Reuse an existing branch when re-running a promotion; create it otherwise.
  if git -C "$TARGET" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git -C "$TARGET" worktree add "$WT_DIR" "$BRANCH" \
      || fail "could not add a worktree at '$WT_DIR' for existing branch '$BRANCH' in $TARGET"
  else
    git -C "$TARGET" worktree add -b "$BRANCH" "$WT_DIR" \
      || fail "could not add a worktree at '$WT_DIR' for new branch '$BRANCH' in $TARGET"
  fi
  PLANT_DIR="$WT_DIR"
  say "PR branch '$BRANCH' checked out in an ISOLATED WORKTREE: $WT_DIR"
  say "  (the live checkout at $TARGET keeps its own branch — this promotion never switches it)"
fi

# --- Step 4a: living-prod blast-radius gate ( P1.5 / guard-1056) ----
# For a living/populated dest, surface the read-only --plan report BEFORE any
# mutation, so deployment-local + forged-skill survival is visible pre-plant
# (the seed pipeline has known living-prod bugs —  — where --living-prod
# alone under-protects; the plan is the load-bearing safety checkpoint).
if [[ $LIVING_PROD -eq 1 ]]; then
  say "living-prod: seed-transplant --plan (blast-radius report) BEFORE planting (g-306-90/guard-1056) ..."
  # The plan's rc IS its verdict (): 0 SAFE / 20 REVIEW REQUIRED /
  # 21 DO NOT PROMOTE. SSOT for the vocabulary is the `plan` dispatch comment in
  # _seed_engine.py; seed-transplant.sh propagates it verbatim.
  #
  # This block previously read `... --plan || fail "seed-transplant --plan
  # failed"`. That LOOKED like a gate and was the reason a reader believed one
  # existed, but both layers below hardcoded exit 0, so `|| fail` could only
  # fire if bash could not launch the command. Every actual verdict — including
  # DO NOT PROMOTE over 151 prod-ahead files on Hop 2 — passed straight through
  # it into the plant. Distinguishing "could not assess" from "assessed, and the
  # answer is no" is the entire fix; collapsing them is what hid it.
  # NOTE — this runs against PLANT_DIR while the LIVING_PROD *detection* above
  # deliberately ran against $TARGET, and the split is load-bearing ().
  # Detection asks "is the DEPLOYMENT living?", a property of the real clone: its
  # strongest signal is `$TARGET/.mind-data`, which is gitignored and therefore
  # ABSENT from any worktree. Detecting against the worktree would read a living
  # production deployment as a fresh seed, drop --living-prod, and let the plant
  # clobber the deployment-local files this flag exists to preserve. The PLAN, by
  # contrast, is a blast-radius report and MUST describe the directory that is
  # actually about to be mutated — reporting on $TARGET while planting into the
  # worktree would make the report describe a different tree than the plant.
  set +e
  bash "$SCRIPT_DIR/seed-transplant.sh" "$PLANT_DIR" --living-prod --plan
  PLAN_RC=$?
  set -e
  case $PLAN_RC in
    0)  say "plan verdict: SAFE — proceeding to plant" ;;
    20) say "plan verdict: REVIEW REQUIRED — diverged deployment-local, cruft-swept protected paths, or real orphan deletions were reported ABOVE. Not blocking (advisory), but read the report before trusting this plant." ;;
    21)
        if [[ -n "$FORCE_PAST_PLAN" ]]; then
          say "plan verdict: DO NOT PROMOTE — OVERRIDDEN by --force-past-plan"
          say "  justification: $FORCE_PAST_PLAN"
          say "  proceeding under explicit operator override; the prod-ahead files listed above WILL lose their dest-only lines"
        else
          fail "plan verdict: DO NOT PROMOTE — the destination carries framework lines the seed lacks (prod-ahead; see the per-file list above). Back-port those UP to this source first (guard-119), then re-run. To override deliberately: --force-past-plan \"<why this is safe>\". Aborting before any mutation."
        fi
        ;;
    *)  fail "seed-transplant --plan failed to run (exit $PLAN_RC) — cannot assess blast radius; aborting before mutation" ;;
  esac
fi

# --- Step 4a.9: re-assert source provenance IMMEDIATELY before the plant ----
# TOCTOU CLOSE (). Step 1 asserted clean-tree + HEAD==v$LOCAL at the
# START of the run. Everything between here and there takes real wall-clock —
# seed-preflight alone runs minutes, plus promotion-preflight, the PR-branch
# creation, and the --plan blast-radius pass; a full chain promote takes 15+.
# The plant below copies from the WORKING TREE, so any commit, checkout, pull, or
# stray write landing in that window is planted downstream WEARING THE v$LOCAL
# LABEL. Measured 2026-07-27: two fixes committed inside the window shipped as
# v2.7.1, so the ZDS payload for that tag contains code the tag does not.
#
# The consequence is worse than one mislabeled ship: it silently weakens
# guard-678 frontier-monotonicity (comparing tags stops comparing what is
# deployed) and makes what-changed-between-versions reasoning unsound at the
# exact moment it matters most — a downstream regression hunt.
#
# This is a re-check, not a second opinion: the SAME predicate as Step 1, so the
# two can never disagree. It is a hard fail with no override — an operator who
# genuinely wants the newer code should cut a new tag, which costs one release.sh
# run and keeps the label honest. Note the runbook's worktree-at-tag method
# (core/config/conventions/promotion-runbook.md Phase 3) makes this a guaranteed
# no-op, because a detached worktree at the tag cannot drift while the fleet
# commits to main — but that method is documented, not enforced, and the
# measured incident is what running WITHOUT it costs.
if ! source_provenance_drift; then
  fail "SOURCE DRIFTED MID-PROMOTION ($SRC_DRIFT_KIND: $SRC_DRIFT_DETAIL) — the tree changed between the Step 1 assertion and this plant, so planting now would ship content that is NOT in tag v$LOCAL under the v$LOCAL label. Aborting before any mutation of $TARGET. Fix: re-run from a worktree pinned at the tag (promotion-runbook.md Phase 3), or cut a new release with release.sh so the label matches what you are shipping."
fi
say "source provenance re-verified at plant time: tree clean and HEAD is the v$LOCAL commit"

# --- Step 4b: seed-plant into the target (commits onto the CURRENT branch) --
say "planting framework into $PLANT_DIR ${LP_FLAG:+(living-prod) }..."
bash "$SCRIPT_DIR/seed-transplant.sh" "$PLANT_DIR" $LP_FLAG --force --commit || fail "seed plant failed"

# --- Step 5: post-promotion verify -----------------------------------------
# --expect-commit is load-bearing, not decorative (). Without it,
# seed-verify's git-state check reports a dirty destination as "expected after
# plant" and exits 0 — so this `|| fail` was DEAD, and Step 4b's `|| fail` above
# was dead too (seed-transplant swallowed its own commit failure). Both gates
# that stop the PR were defeated by the same collapse, which is how a plant that
# committed NOTHING reached "═══ PROMOTED ═══" with a PR open. Post-plant, a
# dirty tree IS the failure; the flag is what lets the verifier say so.
say "verifying plant at $PLANT_DIR ..."
bash "$SCRIPT_DIR/seed-verify.sh" "$PLANT_DIR" --expect-commit || fail "post-promotion verify FAILED at $PLANT_DIR"

# --- Step 6: optional PR push (merges ONLY under --auto-merge) -------------
if [[ $DO_PR -eq 1 ]]; then
  # Resolve gh robustly. `command -v gh` ALONE is wrong on Windows git-bash:
  # the GitHub CLI installs to "C:\Program Files\GitHub CLI", which is on the
  # *Windows* PATH but NOT on the narrower MSYS PATH that command -v searches —
  # so a real, authenticated gh reads as "not installed" and the promotion
  # false-warns the operator into a manual-PR path (incident 2026-06-06: a live
  # promotion did exactly this while gh 2.88 was installed + authed). Resolution
  # order: explicit override -> command -v -> known installer dir -> Windows-PATH
  # search via where.exe. PROMOTE_GH_BIN, when SET (even to ""), overrides
  # detection entirely:
  #   - non-empty -> use it verbatim (manual escape hatch; tests inject a shim)
  #   - empty     -> force the not-found/warn branch (deterministic test of it)
  if [[ -n "${PROMOTE_GH_BIN+x}" ]]; then
    GH_BIN="$PROMOTE_GH_BIN"
  else
    GH_BIN="$(command -v gh 2>/dev/null || true)"
    if [[ -z "$GH_BIN" ]]; then
      for _cand in "/c/Program Files/GitHub CLI/gh.exe" "/c/Program Files (x86)/GitHub CLI/gh.exe"; do
        [[ -x "$_cand" ]] && { GH_BIN="$_cand"; break; }
      done
    fi
    if [[ -z "$GH_BIN" ]] && command -v where.exe >/dev/null 2>&1; then
      GH_BIN="$(where.exe gh 2>/dev/null | head -n1 | tr -d '\r' || true)"
    fi
  fi
  if [[ -z "$GH_BIN" ]]; then
    # The manual-recovery commands below are deliberately phrased against
    # $TARGET, not $PLANT_DIR: the worktree is torn down when this script exits,
    # but the branch and its commit live in $TARGET's object store and outlive
    # it. `git -C "$TARGET" push` pushes a ref and does not care which branch
    # $TARGET has checked out, so it is safe to hand an operator.
    say "WARNING: --pr requested but 'gh' is not installed/locatable. The plant is committed on branch '$BRANCH' in $TARGET (planted via worktree $PLANT_DIR, which is torn down on exit)."
    say "Open a PR manually:  git -C \"$TARGET\" push -u origin \"$BRANCH\" && gh pr create ..."
  else
    # Pushed FROM the worktree: it shares $TARGET's git dir, so 'origin' and the
    # branch ref are the same objects the live checkout sees. (1/7, .)
    #
    # MERGE RESOLUTION 2026-08-10, resolved on cc-07 ( prescribes
    # "resolve toward the PLANT_DIR form"). Steps 1/7 and 2/7 of the live
    # promotion sequence were built by different Bodies on different boxes and
    # collided exactly here, because they changed ORTHOGONAL things on the same
    # lines: 1/7 changed WHERE the push happens (worktree, never the target's
    # live checkout), 2/7 changed WHAT happens after the PR exists (--auto-merge).
    # Neither side was wrong, so neither is discarded — 2/7's machinery below
    # runs from 1/7's $PLANT_DIR instead of $TARGET.
    #
    # `git push` output is routed to stderr (>&2), NOT suppressed: stdout must
    # carry ONLY the PR URL that `gh pr create` prints, so --auto-merge has
    # something to act on. Silencing it would violate guard-139/guard-1972.
    if PR_RAW="$( cd "$PLANT_DIR" && git push -u origin "$BRANCH" >&2 && \
      "$GH_BIN" pr create --title "Promote framework v$LOCAL from $SELF_ROLE" \
        --body "Automated framework promotion v$LOCAL ($SELF_ROLE -> $TARGET_ROLE). The agent merges once mergeable + checks pass (user-granted 2026-06-06)." )"; then
      # Take the LAST non-empty stdout line, not the whole capture: gh may print
      # an advisory line before the URL, and a multi-line PR_URL would be passed
      # verbatim to `gh pr view` and fail — turning a healthy PR into a false
      # "UNREADABLE -> REFUSED". Trimming happens OUTSIDE the `if` condition on
      # purpose: a pipe inside it would replace gh's exit status with tail's and
      # silently defeat the push/create failure branch below (guard-1150).
      PR_URL="$(printf '%s\n' "$PR_RAW" | sed '/^[[:space:]]*$/d' | tail -n1)"
      say "PR opened: ${PR_URL:-<no url returned>}"
    else
      PR_URL=""
      say "WARNING: PR push/create failed — the plant is committed on '$BRANCH' at $TARGET; open the PR manually."
    fi

    if [[ $AUTO_MERGE -eq 1 && -n "$PR_URL" ]]; then
      # Preconditions, measured rather than assumed (guard-1199/1264/2640).
      #   mergeable        — the genuinely checkable one (MERGEABLE = no conflict)
      #   statusCheckRollup— NOT read as green when EMPTY. An empty rollup is an
      #     empty population reporting clean, and GitHub renders it identically
      #     to a genuine green; where a chain repo defines no pull_request-
      #     triggered workflow, [] is its PERMANENT state. Laundering [] into
      #     "CI passed" is exactly guard-2640. We instead SAY which case we are
      #     in and rest on the LOCAL gate that already ran: seed-verify.sh
      #     --expect-commit above, which fails the promotion outright. When
      #     checks DO exist, any non-SUCCESS blocks the merge. Per-deployment
      #     measurements live in the domain promotion convention, not here.
      # GitHub computes `mergeable` ASYNCHRONOUSLY. A read of a just-created PR
      # commonly returns UNKNOWN and merely TRIGGERS the computation, resolving
      # on a later read — measured 2026-08-10 against a live PR: poll 1 UNKNOWN,
      # poll 2 MERGEABLE. --auto-merge reads seconds after `gh pr create`, i.e.
      # exactly when UNKNOWN is most likely, so a SINGLE-SHOT read would refuse
      # perfectly mergeable PRs on most real runs while passing every mocked
      # test. Poll until it resolves; UNKNOWN at the end is treated as a refusal,
      # never as consent.
      PR_MERGEABLE="UNKNOWN"; PR_CHECKS=0; PR_BAD=0
      for _mtry in 1 2 3 4 5; do
        if PR_FACTS="$( cd "$TARGET" && "$GH_BIN" pr view "$PR_URL" --json mergeable,statusCheckRollup \
             --jq '[.mergeable, (.statusCheckRollup|length), ([.statusCheckRollup[]|select((.conclusion // .state // "") != "SUCCESS")]|length)] | @tsv' )"; then
          IFS=$'\t' read -r PR_MERGEABLE PR_CHECKS PR_BAD <<<"$PR_FACTS"
        else
          PR_MERGEABLE="UNREADABLE"; PR_CHECKS=0; PR_BAD=0
          break
        fi
        if [[ "$PR_MERGEABLE" != "UNKNOWN" ]]; then break; fi
        # Announce the read we are ABOUT to make, and only when there is one:
        # saying "re-reading (5/5)" on the last pass then falling out of the loop
        # describes a read that never happens, and sleeps for nothing.
        if [[ "$_mtry" -lt 5 ]]; then
          say "--auto-merge: mergeable=UNKNOWN (GitHub still computing) — re-reading ($((_mtry + 1))/5)"
          sleep 2
        fi
      done

      if [[ "$PR_MERGEABLE" != "MERGEABLE" ]]; then
        say "--auto-merge REFUSED: mergeable=$PR_MERGEABLE (expected MERGEABLE). PR left OPEN at $PR_URL"
      elif [[ "${PR_CHECKS:-0}" -gt 0 && "${PR_BAD:-0}" -gt 0 ]]; then
        say "--auto-merge REFUSED: ${PR_BAD} of ${PR_CHECKS} status check(s) not SUCCESS. PR left OPEN at $PR_URL"
      else
        if [[ "${PR_CHECKS:-0}" -eq 0 ]]; then
          say "--auto-merge: 0 status checks on this PR — NOT read as CI-green (guard-2640). Basis for merging is the local gate: seed-verify.sh --expect-commit passed above."
        else
          say "--auto-merge: ${PR_CHECKS} status check(s) all SUCCESS"
        fi
        # guard-1897: `gh pr merge` can print `fatal: Not possible to
        # fast-forward` WHEN THE REMOTE MERGE ALREADY SUCCEEDED — that fatal
        # comes from gh updating the LOCAL checkout afterwards. Its exit status
        # is therefore NOT the verdict, and this `|| true` is a deliberate
        # discard of a known-unreliable signal, not a guard-139 error-silencing:
        # the authoritative check is the remote read on the next line.
        ( cd "$TARGET" && "$GH_BIN" pr merge "$PR_URL" --merge ) || true
        MERGED_AT="$( cd "$TARGET" && "$GH_BIN" pr view "$PR_URL" --json mergedAt --jq '.mergedAt // ""' 2>/dev/null )" || MERGED_AT=""
        if [[ -n "$MERGED_AT" ]]; then
          say "--auto-merge OK: merged at $MERGED_AT — $PR_URL"
        else
          say "WARNING: --auto-merge did NOT land — PR still open at $PR_URL; merge it manually."
        fi
      fi
    fi
  fi
  # Teardown right after the push rather than waiting for the EXIT trap, so the
  # ═══ PROMOTED ═══ banner below is printed by a run that has already cleaned
  # up. The trap stays armed and is a no-op after this (idempotent by flag).
  _wt_teardown
fi

say "═══ PROMOTED v$LOCAL ($SELF_ROLE -> $TARGET_ROLE) ═══"
# Closing line must not outlive what actually happened: with --auto-merge on,
# "the agent merges it later" is false, and with --pr off there is no PR at all.
# All three branches are gated on the same flags as the actions above (guard-527).
if [[ $DO_PR -eq 1 && $AUTO_MERGE -eq 1 ]]; then
  say "Target: $TARGET  (--auto-merge ran — see the --auto-merge line above for whether it landed; guard-680)"
elif [[ $DO_PR -eq 1 ]]; then
  say "Target: $TARGET  (PR opened; the agent merges it once mergeable + checks pass — guard-680)"
else
  say "Target: $TARGET  (planted on branch '$BRANCH'; no PR requested)"
fi
