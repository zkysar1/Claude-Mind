#!/usr/bin/env bash
# promote-to-upstream.sh — Promote this repo's framework release ONE step down
# the chain (frontier->seed, or seed->downstream) into a local clone of the
# target repo, via the seed-plant machinery. The SCRIPT opens a PR (with --pr)
# and never auto-merges; the AGENT may merge that PR as a separate verified step
# once it is mergeable + checks pass (user-granted 2026-06-06; guard-680 /
# capability-routing grant-002).
#
# Usage:
#   bash core/scripts/promote-to-upstream.sh --target <path-to-target-clone> \
#        [--branch "promote/vX.Y.Z"] [--pr] [--dry-run]
#
# Exit: 0 success/dry-run-ok; 1 pre-flight/invariant/chain failure; 2 usage error.
# Design: rails A.7 + omni delta M2 (never push/merge from here beyond a PR) +
# guardrails CW2 (hard invariant) / CW4 (chain order).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo "ERROR: failed to source _paths.sh" >&2; exit 2; }
LIB="$SCRIPT_DIR/_release_lib.py"
INIT_PY="$PROJECT_ROOT/mind_api/src/__init__.py"
# RELEASES.json check delegated to check-releases-current.sh (role-aware, 1);
# this script no longer references RELEASES.json directly.
WORLD="${WORLD_DIR:-${WORLD_PATH:-}}"
OVERLAY="$WORLD/config/compatibility.yaml"
FW_COMPAT="$CONFIG_DIR/compatibility.yaml"

TARGET=""; BRANCH=""; DO_PR=0; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; [[ -z "$TARGET" || "$TARGET" == --* ]] && { echo "ERROR: --target requires a path" >&2; exit 2; }; shift 2;;
    --branch) BRANCH="${2:-}"; [[ -z "$BRANCH" || "$BRANCH" == --* ]] && { echo "ERROR: --branch requires a value" >&2; exit 2; }; shift 2;;
    --pr) DO_PR=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//;/^set -euo/d'; exit 0;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2;;
  esac
done
[[ -n "$TARGET" ]] || { echo "ERROR: --target <path-to-target-clone> is required" >&2; exit 2; }

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
# EVERY non-frontier promote (1). Single source of truth now.
CRC_OUT="$(bash "$SCRIPT_DIR/check-releases-current.sh" 2>&1)" \
  || fail "RELEASES.json not current: $CRC_OUT (cut a release with release.sh before promoting)"
say "$CRC_OUT"

# (b) working tree clean — enforced for ALL roles (promoting an uncommitted
#     tree is wrong regardless of role). (c) HEAD tagged v$LOCAL is frontier-only
#     (guarded below).
DIRTY="$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null || true)"
if [[ -n "$DIRTY" ]]; then
  if [[ $DRY -eq 1 ]]; then say "[dry-run] note: working tree dirty (would FAIL a real promote)";
  else fail "working tree is dirty — commit before promoting"; fi
fi
# (c) HEAD tagged v$LOCAL — FRONTIER-ONLY provenance. release.sh is the sole
# v-tagger and only runs at the frontier; a non-frontier role (seed/downstream)
# re-transplants adopted framework and has no v-tag by design, so the tag gate
# is skipped for it (1, option 2 role-conditional gating).
if [[ "$SELF_ROLE" == "frontier" ]]; then
  if ! git -C "$PROJECT_ROOT" rev-parse -q --verify "refs/tags/v$LOCAL" >/dev/null 2>&1; then
    if [[ $DRY -eq 1 ]]; then say "[dry-run] note: HEAD has no tag v$LOCAL (would FAIL — cut a release with release.sh first)";
    else fail "no tag v$LOCAL — promote only TAGGED releases (run release.sh first)"; fi
  else
    HEAD_SHA="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
    TAG_SHA="$(git -C "$PROJECT_ROOT" rev-list -n1 "v$LOCAL")"
    [[ "$HEAD_SHA" == "$TAG_SHA" ]] || { [[ $DRY -eq 1 ]] && say "[dry-run] note: HEAD is not the v$LOCAL commit (would FAIL)" || fail "HEAD is not the tagged v$LOCAL commit"; }
  fi
else
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
# 9: the gate existed since its authoring but had ZERO callers — the
# target-drift audit (and the 5 weights-contract cross-check) only
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

# --- Dry-run stops here (no mutation of the target) ------------------------
if [[ $DRY -eq 1 ]]; then
  [[ $DO_PR -eq 1 ]] && say "[dry-run] would: create PR branch '$BRANCH' in target FIRST (plant commits there, not on target's main)"
  say "[dry-run] would: seed-transplant.sh \"$TARGET\" --force --commit  (domain-strip + transforms + verify)"
  [[ $DO_PR -eq 1 ]] && say "[dry-run] would: push branch '$BRANCH' + gh pr create (NEVER merges)"
  say "[dry-run] would: seed-verify.sh \"$TARGET\""
  say "[dry-run] OK — all pre-flight + invariant + preflight checks passed"
  exit 0
fi

# --- Step 4: (if --pr) create the PR branch in the target BEFORE planting ---
# The plant commit MUST land on the PR branch, not the target's main. If the
# branch is created AFTER seed-transplant --commit (which commits onto whatever
# branch is currently checked out), the commit is already on main and the PR
# diff is EMPTY — the change is effectively merged by the agent (M2 violation).
# (review F3, HIGH)
if [[ $DO_PR -eq 1 ]]; then
  ( cd "$TARGET" && { git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"; } ) \
    || fail "could not create/switch to PR branch '$BRANCH' in $TARGET"
  say "PR branch ready in target: $BRANCH (the plant commits here, not on the target's main)"
fi

# --- Step 4b: seed-plant into the target (commits onto the CURRENT branch) --
say "planting framework into $TARGET ..."
bash "$SCRIPT_DIR/seed-transplant.sh" "$TARGET" --force --commit || fail "seed plant failed"

# --- Step 5: post-promotion verify -----------------------------------------
say "verifying plant at $TARGET ..."
bash "$SCRIPT_DIR/seed-verify.sh" "$TARGET" || fail "post-promotion verify FAILED at $TARGET"

# --- Step 6: optional PR push (NEVER merges) -------------------------------
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
    say "WARNING: --pr requested but 'gh' is not installed/locatable. The plant is committed on branch '$BRANCH' at $TARGET."
    say "Open a PR manually:  (cd \"$TARGET\" && git push -u origin \"$BRANCH\" && gh pr create ...)"
  else
    ( cd "$TARGET" && git push -u origin "$BRANCH" && \
      "$GH_BIN" pr create --title "Promote framework v$LOCAL from $SELF_ROLE" \
        --body "Automated framework promotion v$LOCAL ($SELF_ROLE -> $TARGET_ROLE). The agent merges once mergeable + checks pass (user-granted 2026-06-06)." ) \
      || say "WARNING: PR push/create failed — the plant is committed on '$BRANCH' at $TARGET; open the PR manually."
  fi
fi

say "═══ PROMOTED v$LOCAL ($SELF_ROLE -> $TARGET_ROLE) ═══"
say "Target: $TARGET  (PR opened; the agent merges it once mergeable + checks pass — guard-680)"
