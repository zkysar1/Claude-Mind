#!/usr/bin/env bash
# domain-leak-exempt: example sibling-repo names (Ayoai-Environment-Server, Ayoai-Roblox-Integration) appear in a comment illustrating the cross-repo commit pattern. The script reads AGENT_WRITE_PATH dynamically and is host-agnostic; only the comment names AyoAI product repos as concrete examples.
# g-115-746: detect sibling product repos under AGENT_WRITE_PATH and invoke
# iteration-commit.sh on each. Wraps the cross-repo commit ceremony that
# iteration-close.sh do_state_update previously skipped (hardcoded to
# --repo PROJECT_ROOT at core/scripts/iteration-close.sh:709). The hardcoded
# scope left sibling product repos under AGENT_WRITE_PATH with stale
# uncommitted product code on deep closures — see the g-115-744 recovery
# of f72fd70 (LOGIT zero-clamp fix that sat 3 days because the 2026-05-14
# g-250-85 close didn't commit the product repo). post-execution.md Step 2
# is the canonical mechanism for cross-repo commits but requires explicit
# LLM-driven invocation that pre-compaction g-250-85 skipped.
#
# Reads AGENT_WRITE_PATH from <agent>/local-paths.conf. Direct children
# that contain a .git/ directory are considered product repos. For each,
# invokes iteration-commit.sh --repo <path> with the passed goal-id/title/
# outcome. iteration-commit.sh's own no-op rules (empty git status, routine
# outcome) handle the "nothing to commit" case — this helper does not
# pre-scan status.
#
# Fail-open at multiple layers:
#   - Missing MIND_AGENT       → return 0 (silent)
#   - Missing local-paths.conf  → return 0 (silent)
#   - Missing AGENT_WRITE_PATH  → return 0 (silent; AGENT_WRITE_PATH is
#                                  optional per local-paths.conf schema)
#   - Per-repo commit failure   → logged with [iteration-close] prefix, no
#                                  exit (|| true on the subshell)
# Caller MUST NOT block state-update on any failure here.
#
# Usage:
#   source "$SCRIPT_DIR/_cross_repo_commit.sh"
#   cross_repo_commit_product --goal-id <id> --title "<title>" --outcome <class>
#
# Invoked once from iteration-close.sh do_state_update right after the
# existing PROJECT_ROOT iteration-commit.sh call (g-280-03 wiring) so
# log ordering is mind-repo first, then product repos in directory order.

cross_repo_commit_product() {
    local goal_id="" title="" outcome=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --goal-id) goal_id="${2:-}"; shift 2 ;;
            --title)   title="${2:-}"; shift 2 ;;
            --outcome) outcome="${2:-}"; shift 2 ;;
            *) shift ;;
        esac
    done

    # Routine outcomes: iteration-commit.sh no-ops anyway. Short-circuit
    # here to skip the local-paths.conf read entirely on the hot path.
    [[ "$outcome" != "deep" ]] && return 0

    local agent="${MIND_AGENT:-}"
    [[ -z "$agent" ]] && return 0

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local project_root
    project_root="$(cd "$script_dir/../.." && pwd)"

    local conf="$project_root/$agent/local-paths.conf"
    [[ ! -f "$conf" ]] && return 0

    # local-paths.conf entries are simple KEY=VALUE on a line, no quoting
    # mandated. Strip CR (Windows line endings), surrounding quotes, and
    # whitespace defensively. Use cut -d= -f2- so values containing '='
    # are preserved.
    local awp
    awp=$(grep -E '^AGENT_WRITE_PATH=' "$conf" 2>/dev/null \
          | head -1 \
          | cut -d= -f2- \
          | tr -d '\r' \
          | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
          | sed 's/^["'\''"]\(.*\)["'\''"]$/\1/')
    [[ -z "$awp" ]] && return 0

    # MULTI-ROOT (g-321-05): $awp may name several write roots separated by ';'
    # (e.g. ".../Ayoai;.../Zak-Data-Solutions") so an agent can commit across
    # more than one product workspace. Split on ';' and enumerate each root.
    # `IFS=';' read -ra` preserves paths containing spaces (only ';' splits).
    local -a write_roots
    IFS=';' read -ra write_roots <<< "$awp"

    # Enumerate direct children with .git/ under EACH write root. Glob expansion
    # handles spaces; we cap depth at 1 because the product workspace convention
    # is one repo per top-level directory (Ayoai-Environment-Server, Ayoai-
    # Roblox-Integration, etc.) — deeper nesting would be a submodule scenario
    # outside this helper's scope.
    local write_root repo_dir
    for write_root in "${write_roots[@]}"; do
        write_root="$(printf '%s' "$write_root" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [[ -z "$write_root" || ! -d "$write_root" ]] && continue
        for repo_dir in "$write_root"/*/; do
            repo_dir="${repo_dir%/}"
            [[ ! -d "$repo_dir/.git" ]] && continue
            local out
            # iteration-commit.sh exit codes: 0 on success-or-no-op, non-zero
            # on hard failure. `|| true` keeps state-update green even on
            # sensitive-file-only changes or transient git-lock contention.
            out="$(bash "$script_dir/iteration-commit.sh" \
                --goal-id "$goal_id" \
                --title "$title" \
                --outcome "$outcome" \
                --repo "$repo_dir" 2>&1 || true)"
            echo "[iteration-close] cross-repo iteration-commit ($(basename "$repo_dir")): $out"
        done
    done
    return 0
}
