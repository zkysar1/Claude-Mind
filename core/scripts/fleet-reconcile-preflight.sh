#!/usr/bin/env bash
# fleet-reconcile-preflight.sh — READ-ONLY preflight probe for a fleet fork
# reconciliation (forged-skill companion, gap-15 / 4).
#
# Emits a single JSON object measuring everything the reconcile playbook
# (world/knowledge/tree/system/fleet-git-divergence-reconciliation.md, sig-29,
# rb-3161) needs BEFORE any mutation:
#   - two-sided divergence (ahead/behind vs origin/<branch>) + merge-base
#   - working-tree churn (modified tracked files) that must be committed first
#   - untracked-collision list (untracked local files that origin tracks —
#     the class that ABORTS `git merge`; archive-before-delete applies)
#   - constitutional-anchor delta (anchor files differing HEAD vs origin —
#     resolution rule: take origin side untouched)
#   - conflict preview via `git merge-tree --write-tree` when supported
#
# READ-ONLY CONTRACT: the ONLY ref this script touches is refs/remotes/* via
# `git fetch` (skippable with --no-fetch). It never modifies the working tree,
# index, HEAD, or local branches. Mutations (branch, commit, tarball, merge)
# belong to the LLM following the reconcile-fleet-fork skill.
#
# Usage: bash core/scripts/fleet-reconcile-preflight.sh [--no-fetch] [--branch main]
set -euo pipefail

BRANCH="main"
DO_FETCH=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-fetch) DO_FETCH=0; shift ;;
    --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO"

command -v git >/dev/null || { echo '{"error":"git not found"}'; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo '{"error":"not a git repo"}'; exit 1; }

FETCH_STATUS="skipped"
if [[ "$DO_FETCH" -eq 1 ]]; then
  if git fetch origin "$BRANCH" --quiet 2>/dev/null; then FETCH_STATUS="ok"; else FETCH_STATUS="failed"; fi
fi

UPSTREAM="origin/$BRANCH"
git rev-parse --verify "$UPSTREAM" >/dev/null 2>&1 || { echo "{\"error\":\"$UPSTREAM not found\",\"fetch\":\"$FETCH_STATUS\"}"; exit 1; }

# Two-sided divergence + merge base
read -r BEHIND AHEAD < <(git rev-list --left-right --count "$UPSTREAM"...HEAD)
MERGE_BASE="$(git merge-base "$UPSTREAM" HEAD)"

# Working-tree churn: tracked files with unstaged or staged modifications
CHURN_COUNT="$(git status --porcelain=v1 --untracked-files=no | wc -l | tr -d ' ')"

# Untracked collisions: untracked locally AND tracked in origin tree (merge-abort class)
UNTRACKED_COLLISIONS="$(comm -12 \
  <(git ls-files --others --exclude-standard | sort) \
  <(git ls-tree -r --name-only "$UPSTREAM" | sort) || true)"

# Constitutional-anchor delta: anchor files whose content differs HEAD vs origin.
# Resolution rule for these is fixed: take the origin side untouched.
ANCHORS=".claude/settings.local.json core/scripts/settings-structural-validator.py core/scripts/settings-structural-validator.sh"
ANCHOR_DELTAS=""
for a in $ANCHORS; do
  if ! git diff --quiet HEAD "$UPSTREAM" -- "$a" 2>/dev/null; then
    ANCHOR_DELTAS="${ANCHOR_DELTAS}${a}"$'\n'
  fi
done

# Conflict preview (git >= 2.38): merge-tree --write-tree lists conflicted paths
# without touching the worktree. Older git: preview unsupported, report null.
# --no-messages is REQUIRED (fresh-eyes F1, msg-3113): without it the
# informational-messages section (Auto-merging.../CONFLICT...) follows the
# conflicted-file section after a blank line, and a strip-all-blanks parse
# folds ~3x message lines into the file list (probe: 119 vs 32 lines on the
# 35d21a5 parents, 31 real conflicts).
CONFLICT_PREVIEW_SUPPORTED=0
CONFLICT_FILES=""
if git merge-tree --write-tree "$UPSTREAM" HEAD >/dev/null 2>&1 || [[ $? -eq 1 ]]; then
  CONFLICT_PREVIEW_SUPPORTED=1
  # Exit 0 = clean merge, exit 1 = conflicts; with --no-messages --name-only
  # the output is exactly: OID line, then one conflicted path per line.
  MT_OUT="$(git merge-tree --write-tree --name-only --no-messages "$UPSTREAM" HEAD 2>/dev/null || true)"
  CONFLICT_FILES="$(printf '%s\n' "$MT_OUT" | tail -n +2 | sed '/^$/d' || true)"
fi

# python3 with _paths.sh sourced (python-invocation convention for .sh scripts;
# fresh-eyes F2, msg-3114 — the py launcher is not guaranteed fleet-wide).
# Multi-line values travel via exported env vars, not argv-after-heredoc.
source "$SCRIPT_DIR/_paths.sh"
export PF_UNTRACKED="$UNTRACKED_COLLISIONS" PF_ANCHORS="$ANCHOR_DELTAS" PF_CONFLICTS="$CONFLICT_FILES"
python3 - "$BEHIND" "$AHEAD" "$MERGE_BASE" "$FETCH_STATUS" "$CHURN_COUNT" "$CONFLICT_PREVIEW_SUPPORTED" <<'PYEOF'
import json, os, sys
behind, ahead, base, fetch, churn, mt_supported = sys.argv[1:7]
def env_lines(name):
    return [l for l in os.environ.get(name, "").splitlines() if l.strip()]
print(json.dumps({
    "behind": int(behind), "ahead": int(ahead), "merge_base": base,
    "fetch": fetch, "working_tree_churn": int(churn),
    "untracked_collisions": env_lines("PF_UNTRACKED"),
    "anchor_deltas": env_lines("PF_ANCHORS"),
    "conflict_preview": {"supported": mt_supported == "1", "files": env_lines("PF_CONFLICTS")},
    "reconcile_needed": int(behind) > 0,
    "safety_rails_needed": {
        "churn_commit": int(churn) > 0,
        "collision_tarball": len(env_lines("PF_UNTRACKED")) > 0,
    },
}, indent=1))
PYEOF
