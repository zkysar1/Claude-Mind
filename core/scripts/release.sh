#!/usr/bin/env bash
# release.sh — Cut a framework release: bump the version SSOT, append a
# RELEASES.json entry, commit, and create an annotated git tag.
#
# This script is the SOLE creator of v* git tags (M2). It NEVER pushes —
# the human decides when to `git push origin main --tags`.
#
# Usage:
#   bash core/scripts/release.sh {major|minor|patch} --summary "text" [flags]
#
# Flags:
#   --summary "text"                          One-line release description (required for a real cut).
#   --cross-world                             Mark the release as affecting world/+meta/ state.
#   --recipe core/config/upgrade-recipes/vX.sh  Upgrade recipe (REQUIRED for breaking releases, CW3).
#   --allow-non-breaking-cross-world "reason"   Audited override: a cross_world release that is NOT
#                                             breaking (Q3 — default is fail-closed to breaking).
#   --force-release "reason"                  Audited override: bypass the HARD frontier-invariant
#                                             check (H1) — e.g. cutting ahead of an un-bootstrapped seed.
#   --dry-run                                 Run steps 1-6 (all validation) but write/commit/tag NOTHING.
#
# Semver: major = breaking (always, no escape). minor = feature. patch = fix.
# Exit: 0 success/dry-run-ok; 1 validation or invariant failure; 2 usage error.
#
# Design: rails A.4 + omni deltas H1/M2/M3/Q3 + guardrails CW1/CW2/CW3.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || { echo "ERROR: failed to source _paths.sh" >&2; exit 2; }
LIB="$SCRIPT_DIR/_release_lib.py"
INIT_PY="$PROJECT_ROOT/mind_api/src/__init__.py"
RELEASES_JSON="$PROJECT_ROOT/RELEASES.json"
WORLD="${WORLD_DIR:-${WORLD_PATH:-}}"
OVERLAY="$WORLD/config/compatibility.yaml"
# Durable, queryable force-release audit ledger (omni#5). meta/ — this records a
# framework-level governance decision (cutting ahead of the seed), independent of
# domain state (contrast world/override-bypass-ledger.jsonl, a goal-execution
# ledger). No-op if META is unconfigured (bare clone / CI).
META="${META_DIR:-${META_PATH:-}}"
FORCE_RELEASE_LEDGER="$META/force-release-ledger.jsonl"

# --- Parse args -----------------------------------------------------------
KIND=""
SUMMARY=""
CROSS_WORLD=0
RECIPE=""
ALLOW_NB_CW=0
ALLOW_NB_CW_REASON=""
FORCE_RELEASE=0
FORCE_REASON=""
DRY=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//;/^set -euo/d'; }

# A value-taking flag must be followed by a real value: non-empty AND not itself
# a flag. Without this, `release.sh patch --summary --dry-run` would swallow
# --dry-run as the summary, leave DRY=0, and cut a REAL release (irreversible
# tag) when the user asked for a dry run. (review: HIGH flag-swallowing)
require_value() {
  if [[ -z "${2:-}" || "$2" == --* ]]; then
    echo "ERROR: $1 requires a value (non-empty, not a flag like --dry-run); got '${2:-<none>}'" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    major|minor|patch) KIND="$1"; shift;;
    --summary) require_value "$1" "${2:-}"; SUMMARY="$2"; shift $(( $# >= 2 ? 2 : 1 ));;
    --cross-world) CROSS_WORLD=1; shift;;
    --recipe) require_value "$1" "${2:-}"; RECIPE="$2"; shift $(( $# >= 2 ? 2 : 1 ));;
    --allow-non-breaking-cross-world) require_value "$1" "${2:-}"; ALLOW_NB_CW=1; ALLOW_NB_CW_REASON="$2"; shift $(( $# >= 2 ? 2 : 1 ));;
    --force-release) require_value "$1" "${2:-}"; FORCE_RELEASE=1; FORCE_REASON="$2"; shift $(( $# >= 2 ? 2 : 1 ));;
    --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2;;
  esac
done

[[ -n "$KIND" ]] || { echo "ERROR: bump kind required (major|minor|patch)" >&2; usage; exit 2; }
# --summary is required for a real cut (the audit trail). Dry-run may omit it.
if [[ $DRY -eq 0 && -z "$SUMMARY" ]]; then
  echo "ERROR: --summary is required for a real release cut (omit only for --dry-run)" >&2; exit 2; fi

say() { echo "[release] $*"; }
fail() { echo "[release] ERROR: $*" >&2; exit 1; }

# --- Step 1: preconditions ------------------------------------------------
# In a real cut the working tree must be clean (so the release commit carries
# only the version bump) and we must be on main. In --dry-run these are
# reported but not enforced (dry-run must be usable on a working tree).
# FAIL CLOSED (). THIS IS THE WORST OF THE FIVE SITES: release.sh is
# the sole v-tagger, so a fail-open here mints a version tag on a dirty tree,
# upstream of everything the  provenance work protects. The old form
# (`2>/dev/null || true`) discarded stderr AND the exit code, so any git failure
# — most likely .git/index.lock contention from a partner's concurrent
# iteration-commit.sh — produced an empty string that read as "clean".
# stderr now lands IN the capture and rc is checked explicitly, so a failure is
# non-empty and trips the dirty branch. --no-optional-locks stops this probe
# contending for index.lock at all (verified on git 2.43.0, not assumed).
# Sanctioned sibling idiom: iteration-commit.sh:492.
DIRTY_RC=0
DIRTY="$(git --no-optional-locks -C "$PROJECT_ROOT" status --porcelain 2>&1)" || DIRTY_RC=$?
if [[ $DIRTY_RC -ne 0 ]]; then
  DIRTY="git status failed (rc=$DIRTY_RC) — cannot verify tree is clean: ${DIRTY:-<no stderr>}"
fi
BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ -n "$DIRTY" ]]; then
  # Distinguish "tree is dirty" from "probe could not run" (). Both
  # REFUSE — that is the fail-closed guarantee — but "commit or stash" is the
  # wrong instruction for a wedged git and would send an operator hunting for
  # changes that are not there.
  if [[ $DIRTY_RC -ne 0 ]]; then
    if [[ $DRY -eq 1 ]]; then say "[dry-run] note: dirtiness probe FAILED (would FAIL a real cut): $DIRTY";
    else fail "cannot verify the working tree is clean — refusing to tag. $DIRTY"; fi
  else
    if [[ $DRY -eq 1 ]]; then say "[dry-run] note: working tree is dirty (would FAIL a real cut)";
    else fail "working tree is dirty — commit or stash before cutting a release"; fi
  fi
fi
if [[ "$BRANCH" != "main" ]]; then
  if [[ $DRY -eq 1 ]]; then say "[dry-run] note: on branch '$BRANCH', not main (would FAIL a real cut)";
  else fail "must be on branch 'main' to cut a release (on '$BRANCH')"; fi
fi

# --- Step 2: read current version from DISK (M3 — not a daemon/import cache)
# `|| true` neutralizes pipefail when __init__.py has no __version__ line, so the
# empty-check below produces the diagnostic instead of a bare errexit abort.
CURRENT="$(grep -E '^__version__' "$INIT_PY" | sed -E 's/.*"([^"]+)".*/\1/' || true)"
[[ -n "$CURRENT" ]] || fail "could not read __version__ from $INIT_PY"
say "current version (from disk): $CURRENT"

# --- Step 3: compute new version ------------------------------------------
NEW="$(py -3 "$LIB" bump "$CURRENT" "$KIND")" || fail "version bump failed"
say "new version: $NEW ($KIND bump)"

# Refuse if the target tag already exists (e.g. a prior interrupted run). M2 tags
# are immutable; a stale tag would otherwise collide at step 9 AFTER the commit
# lands, orphaning a release commit. (review: MED commit-tag atomicity)
if git -C "$PROJECT_ROOT" rev-parse -q --verify "refs/tags/v$NEW" >/dev/null 2>&1; then
  if [[ $DRY -eq 1 ]]; then say "[dry-run] note: tag v$NEW already exists (would FAIL a real cut)";
  else fail "tag v$NEW already exists (prior interrupted run?) — delete it or pick another version"; fi
fi

# --- Steps 3-5 + CW1 + CW3 + Q3: full validation via the logic lib --------
set +e
VALID_OUT="$(PROJECT_ROOT="$PROJECT_ROOT" RELEASES_PATH="$RELEASES_JSON" \
  CURRENT_VERSION="$CURRENT" NEW_VERSION="$NEW" BUMP_KIND="$KIND" \
  CROSS_WORLD="$CROSS_WORLD" ALLOW_NB_CW="$ALLOW_NB_CW" RECIPE_PATH="$RECIPE" \
  py -3 "$LIB" validate)"
VALID_RC=$?
set -e
if [[ $VALID_RC -ne 0 ]]; then
  echo "$VALID_OUT" | sed 's/^/[release] /' >&2
  fail "validation failed (CW1/CW3/Q3/semver/chain) — see errors above"
fi
BREAKING="$(echo "$VALID_OUT"   | sed -n 's/^BREAKING=//p')"
CW_FINAL="$(echo "$VALID_OUT"   | sed -n 's/^CROSS_WORLD=//p')"
UPGRADE_RECIPE="$(echo "$VALID_OUT" | sed -n 's/^UPGRADE_RECIPE=//p')"
ROLLBACK_RECIPE="$(echo "$VALID_OUT" | sed -n 's/^ROLLBACK_RECIPE=//p')"
MIN_SOURCE="$(echo "$VALID_OUT"     | sed -n 's/^MIN_SOURCE=//p')"
say "classified: breaking=$BREAKING cross_world=$CW_FINAL"
[[ $ALLOW_NB_CW -eq 1 ]] && echo "[release] AUDIT override allow-non-breaking-cross-world: $ALLOW_NB_CW_REASON" >&2

# --- Step 6: frontier-invariant pre-check (H1 — HARD, --force-release escape)
INVARIANT_OK=0
INVARIANT_FAIL_REASON=""
# Captured for the force-release ledger (omni#5): which override path fired
# ("violated" vs "unverifiable") and, for the violated path, the exact seed
# version we cut ahead of. Set inside the two FORCE_RELEASE branches below.
FORCE_INVARIANT_STATE=""
SEED_LATEST_FOR_LEDGER=""
# RELEASE_SEED_URL env overrides the overlay's seed releases_url — useful for
# cutting against a staging/mirror feed, and for deterministic, fast tests
# (point it at an instant-fail URL instead of waiting on a 30s network fetch).
SEED_URL="${RELEASE_SEED_URL:-}"
if [[ -z "$SEED_URL" && -f "$OVERLAY" ]]; then
  SEED_URL="$(SEED_OVERLAY="$OVERLAY" py -3 -c 'import os,sys
try:
    import yaml
    d=yaml.safe_load(open(os.environ["SEED_OVERLAY"],encoding="utf-8")) or {}
    print((((d.get("sources") or {}).get("seed") or {}).get("releases_url")) or "")
except Exception:
    print("")' 2>/dev/null)"
fi
if [[ -n "$SEED_URL" ]]; then
  SEED_TMP="$(mktemp 2>/dev/null || echo "$PROJECT_ROOT/.seed-releases.tmp")"
  if curl --fail --silent --show-error --max-time 30 "$SEED_URL" -o "$SEED_TMP" 2>/dev/null; then
    set +e
    SEED_LATEST="$(py -3 "$LIB" seed-latest "$SEED_TMP" 2>/dev/null)"
    SEED_RC=$?
    set -e
    rm -f "$SEED_TMP"
    if [[ $SEED_RC -ne 0 || -z "$SEED_LATEST" ]]; then
      INVARIANT_FAIL_REASON="seed feed malformed/unparseable"
      say "frontier-invariant: $INVARIANT_FAIL_REASON (fail-closed, M1)"
    else
      # compare can raise on a non-semver seed version — wrap so a parse failure
      # routes through fail-closed instead of killing the script. (review: MED)
      set +e
      CMP="$(py -3 "$LIB" compare "$NEW" "$SEED_LATEST")"
      CMP_RC=$?
      set -e
      if [[ $CMP_RC -ne 0 ]]; then
        INVARIANT_FAIL_REASON="seed version '$SEED_LATEST' not valid semver"
        say "frontier-invariant: $INVARIANT_FAIL_REASON (fail-closed, M1)"
      elif [[ "$CMP" == "-1" ]]; then
        if [[ $FORCE_RELEASE -eq 1 ]]; then
          echo "[release] AUDIT force-release (invariant violated: $NEW < seed $SEED_LATEST): $FORCE_REASON" >&2
          FORCE_INVARIANT_STATE="violated"; SEED_LATEST_FOR_LEDGER="$SEED_LATEST"
          INVARIANT_OK=1
        else
          fail "frontier-invariant violation (CW2): new $NEW < seed latest $SEED_LATEST — use --force-release \"<reason>\" to override"
        fi
      else
        say "frontier-invariant OK: $NEW >= seed latest $SEED_LATEST"
        INVARIANT_OK=1
      fi
    fi
  else
    rm -f "$SEED_TMP"
    INVARIANT_FAIL_REASON="seed feed unreachable"
    say "frontier-invariant: cannot fetch seed RELEASES.json from $SEED_URL (fail-closed)"
  fi
else
  INVARIANT_FAIL_REASON="no seed releases_url configured"
  say "frontier-invariant: $INVARIANT_FAIL_REASON in $OVERLAY (fail-closed)"
fi
if [[ $INVARIANT_OK -ne 1 ]]; then
  if [[ $FORCE_RELEASE -eq 1 ]]; then
    echo "[release] AUDIT force-release (invariant unverifiable — ${INVARIANT_FAIL_REASON:-unknown}): $FORCE_REASON" >&2
    FORCE_INVARIANT_STATE="unverifiable"
  else
    fail "cannot verify frontier-invariant (CW2) — ${INVARIANT_FAIL_REASON:-unknown}. Use --force-release \"<reason>\" to cut ahead of the seed."
  fi
fi

# --- Step 7: smoke test (H3 — SYNTAX ONLY; recipe is never executed here) --
if [[ -n "$UPGRADE_RECIPE" ]]; then
  bash -n "$PROJECT_ROOT/$UPGRADE_RECIPE" 2>/dev/null || bash -n "$UPGRADE_RECIPE" || fail "upgrade recipe has a syntax error: $UPGRADE_RECIPE"
  bash -n "$PROJECT_ROOT/$ROLLBACK_RECIPE" 2>/dev/null || bash -n "$ROLLBACK_RECIPE" || fail "rollback recipe has a syntax error: $ROLLBACK_RECIPE"
  say "recipe smoke test (syntax-only) passed: $UPGRADE_RECIPE + rollback"
fi

DATE="$(date +%Y-%m-%d)"

# --- Step 8: write (atomic, M3) — skipped on --dry-run --------------------
if [[ $DRY -eq 1 ]]; then
  say "[dry-run] would set __version__ = \"$NEW\" in $INIT_PY"
  say "[dry-run] would prepend RELEASES.json entry: version=$NEW previous=$CURRENT breaking=$BREAKING cross_world=$CW_FINAL date=$DATE"
  say "[dry-run] would commit + tag v$NEW (no push)"
  say "[dry-run] OK — all validation passed, nothing written"
  exit 0
fi

# --- Concurrency lock + mid-cut restore trap (omni#2 + omni#6) -------------
# A mkdir-based lock is atomic on every platform (incl. Git-Bash/Windows), so
# two concurrent REAL cuts are mutually exclusive (dry-runs exited above and
# never take the lock). The EXIT trap (a) restores __init__.py + RELEASES.json
# from HEAD if the cut fails AFTER the version bump but BEFORE the commit lands
# — so a mid-cut failure never leaves the SSOT dirty at a version with no
# recorded release — and (b) always releases the lock. mkdir makes an EMPTY
# dir, which git does not track, so a hard-kill leftover is invisible to
# `git status` (and removable with `rmdir`).
LOCK_DIR="$PROJECT_ROOT/.release.lock"
LOCK_HELD=0
NEEDS_RESTORE=0
_release_cleanup() {
  local rc=$?
  if [[ $rc -ne 0 && $NEEDS_RESTORE -eq 1 ]]; then
    echo "[release] mid-cut failure (rc=$rc) — restoring __init__.py + RELEASES.json from HEAD (no orphan version bump)" >&2
    git -C "$PROJECT_ROOT" checkout HEAD -- "$INIT_PY" "$RELEASES_JSON" 2>/dev/null || true
  fi
  if [[ $LOCK_HELD -eq 1 ]]; then rmdir "$LOCK_DIR" 2>/dev/null || true; fi
}
trap _release_cleanup EXIT
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  fail "another release is in progress (lock dir exists: $LOCK_DIR). If no cut is running, remove it:  rmdir \"$LOCK_DIR\""
fi
LOCK_HELD=1

# Atomic write of __init__.py: edit a temp copy, then rename over the original.
TMP_INIT="$INIT_PY.tmp.$$"
sed -E "s/^__version__ = \"[^\"]+\"/__version__ = \"$NEW\"/" "$INIT_PY" > "$TMP_INIT"
grep -qE "^__version__ = \"$NEW\"" "$TMP_INIT" || { rm -f "$TMP_INIT"; fail "sed did not produce the expected __version__ line"; }
mv -f "$TMP_INIT" "$INIT_PY"
NEEDS_RESTORE=1   # from here a failure must restore the SSOT from HEAD (omni#6)
say "wrote __version__ = \"$NEW\""

# Atomic write of RELEASES.json: build new array via lib, validate, rename.
TMP_REL="$RELEASES_JSON.tmp.$$"
PROJECT_ROOT="$PROJECT_ROOT" RELEASES_PATH="$RELEASES_JSON" NEW_VERSION="$NEW" \
  CURRENT_VERSION="$CURRENT" DATE="$DATE" BREAKING="$BREAKING" CROSS_WORLD="$CW_FINAL" \
  SUMMARY="$SUMMARY" UPGRADE_RECIPE="$UPGRADE_RECIPE" ROLLBACK_RECIPE="$ROLLBACK_RECIPE" \
  MIN_SOURCE="$MIN_SOURCE" py -3 "$LIB" build-prepended > "$TMP_REL" || { rm -f "$TMP_REL"; fail "failed to build new RELEASES.json"; }
py -3 -c 'import json,sys; json.load(open(sys.argv[1],encoding="utf-8"))' "$TMP_REL" || { rm -f "$TMP_REL"; fail "new RELEASES.json does not parse"; }
mv -f "$TMP_REL" "$RELEASES_JSON"
say "prepended RELEASES.json entry for $NEW"

# --- Step 9: git commit + annotated tag (M2 — sole tagger; NEVER pushes) ---
# Pathspec-scope the commit to ONLY the release-bump files (mirrors
# iteration-commit.sh:1105 / ). A bare `git commit` records the WHOLE
# index, so if an autonomous agent has pre-staged WIP in the shared tree when a
# maintainer cuts a release, that WIP gets swept into the release commit
# (guard-741). release.sh stages a fixed, known file set, so commit exactly those.
release_paths=("$INIT_PY" "$RELEASES_JSON")
git -C "$PROJECT_ROOT" add "$INIT_PY" "$RELEASES_JSON"
if [[ -n "$UPGRADE_RECIPE" ]]; then
  git -C "$PROJECT_ROOT" add "$PROJECT_ROOT/$UPGRADE_RECIPE" "$PROJECT_ROOT/$ROLLBACK_RECIPE" 2>/dev/null || true
  [[ -f "$PROJECT_ROOT/$UPGRADE_RECIPE" ]] && release_paths+=("$PROJECT_ROOT/$UPGRADE_RECIPE")
  [[ -f "$PROJECT_ROOT/$ROLLBACK_RECIPE" ]] && release_paths+=("$PROJECT_ROOT/$ROLLBACK_RECIPE")
fi
git -C "$PROJECT_ROOT" commit -m "release: v$NEW" -- "${release_paths[@]}" >/dev/null
NEEDS_RESTORE=0   # bump is committed; the tag-failure handler below owns recovery
TAGMSG="Release v$NEW: $SUMMARY"
[[ $FORCE_RELEASE -eq 1 ]] && TAGMSG="$TAGMSG"$'\n'"OVERRIDE(force-release): $FORCE_REASON"
[[ $ALLOW_NB_CW -eq 1 ]] && TAGMSG="$TAGMSG"$'\n'"OVERRIDE(allow-non-breaking-cross-world): $ALLOW_NB_CW_REASON"
# If the tag fails AFTER the commit landed, roll the commit back so we never
# leave an orphan release commit with no tag. (review: MED commit-tag atomicity)
if ! git -C "$PROJECT_ROOT" tag -a "v$NEW" -m "$TAGMSG"; then
  say "tag v$NEW failed after commit — rolling back the release commit (git reset --soft HEAD~1)"
  git -C "$PROJECT_ROOT" reset --soft HEAD~1 || true
  fail "git tag v$NEW failed; release commit rolled back (version bump remains staged — resolve, then re-run)"
fi
say "committed + tagged v$NEW (annotated)"

# --- Step 9.5: durable force-release audit ledger (omni#5) ----------------
# Append ONE JSONL record per SUCCESSFUL --force-release cut. Placed AFTER the
# tag so a failed cut never leaves a phantom entry; release.sh still holds the
# mkdir lock here, so there is no concurrent-append race. The record is built
# AND appended by py -3 (env-driven, per guard-165 — never interpolate bash vars
# into the python source; pass values via env, read via os.environ). Non-fatal:
# a ledger-write failure must never undo a committed, tagged release.
if [[ $FORCE_RELEASE -eq 1 && -n "$META" ]]; then
  FR_TS="$(date +%Y-%m-%dT%H:%M:%S)" FR_VER="$NEW" FR_PREV="$CURRENT" \
  FR_REASON="$FORCE_REASON" FR_STATE="$FORCE_INVARIANT_STATE" \
  FR_DETAIL="${INVARIANT_FAIL_REASON:-}" FR_SEED="$SEED_LATEST_FOR_LEDGER" \
  FR_BREAKING="$BREAKING" FR_CW="$CW_FINAL" FR_SUMMARY="$SUMMARY" \
  FR_AGENT="${MIND_AGENT:-}" FR_SID="${MIND_SID:-}" FR_LEDGER="$FORCE_RELEASE_LEDGER" \
  py -3 -c 'import os, json, pathlib
p = pathlib.Path(os.environ["FR_LEDGER"])
p.parent.mkdir(parents=True, exist_ok=True)
rec = {
  "ts": os.environ["FR_TS"], "type": "force-release",
  "version": os.environ["FR_VER"], "previous_version": os.environ["FR_PREV"],
  "reason": os.environ["FR_REASON"],
  "invariant_state": os.environ["FR_STATE"] or None,
  "invariant_detail": os.environ.get("FR_DETAIL") or None,
  "seed_latest": os.environ.get("FR_SEED") or None,
  "breaking": os.environ["FR_BREAKING"] == "1",
  "cross_world": os.environ["FR_CW"] == "1",
  "summary": os.environ["FR_SUMMARY"],
  "agent": os.environ.get("FR_AGENT") or None,
  "session_id": os.environ.get("FR_SID") or None,
}
with open(p, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")' \
    && say "force-release audit ledger entry appended -> $FORCE_RELEASE_LEDGER" \
    || echo "[release] WARN: force-release ledger write failed (non-fatal)" >&2
fi

# --- Step 10: report ------------------------------------------------------
echo ""
say "═══ RELEASE v$NEW CUT ═══"
say "breaking=$BREAKING cross_world=$CW_FINAL date=$DATE"
[[ -n "$UPGRADE_RECIPE" ]] && say "upgrade_recipe=$UPGRADE_RECIPE  rollback_recipe=$ROLLBACK_RECIPE  min_source=$MIN_SOURCE"
say "NEXT: review, then push when ready:  git push origin main --tags"
