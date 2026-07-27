#!/usr/bin/env bash
# Layer B (pre-commit, default) + Layer D (--audit) regression guard for the
# OWNERSHIP_MODE elimination (, 2026-07-02). SINGLE SOURCE of detection
# for both layers — a recurring audit goal and the pre-commit hook call this same
# script. Companion to check-no-python-cli-fallback.sh; see CLAUDE.md and
# mind_api/docs/lodestar-dynamic-ownership-design.md (the SUPERSEDED design record).
#
# What was removed: the OWNERSHIP_MODE cutover flag, the static MACHINE_OWNED_AGENTS
# env list, and the runner-token sync-ownership fallback. Single-runner ownership is
# now UNCONDITIONAL, derived from STORAGE_BACKEND alone (own-cloud => live DDB
# runner-claim resolution in owncloud_sync._owned_agents; any other backend =>
# own-all). Re-introducing a READ of either removed env var is the regression this
# gate refuses.
#
# Detection model (the non-obvious part): match the env-READ pattern
# (os.environ / getenv / $VAR), NOT the bare flag name. Prose, docstrings,
# changelog entries, journals, and the historical design docs legitimately MENTION
# the removed names; only a live env read reintroduces the behavior. Comment lines
# are stripped before matching (a '# OWNERSHIP_MODE removed' note is not a read).
#
# Scope: executable framework code only — core/scripts/*.{sh,py}, mind_api/src +
# mind_api/scripts *.py, and .claude/skills/*/SKILL.md pseudocode. Deliberately
# EXCLUDED: this detector; mind_api/docs/ (design history); */tests/ (fixtures may
# reference the names in docstrings); core/config/upgrade-recipes/ (the n3-mind
# rename maps MACHINE_OWNED_AGENTS as a string literal for a downstream env
# migration, not a read). Fail-open over false-positive-blocker (rb-246/guard-147).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    # Not in a git work tree — nothing to gate. Fail open, never block.
    exit 0
}
cd "$REPO_ROOT"

MODE="precommit"
[ "${1:-}" = "--audit" ] && MODE="audit"

SELF="core/scripts/check-no-ownership-flag.sh"

# In-scope executable code (an env read here is behavior, not documentation).
in_scope() {
    case "$1" in
        "$SELF") return 1;;
        core/config/upgrade-recipes/*) return 1;;
        */tests/*|*/__pycache__/*) return 1;;
        mind_api/docs/*) return 1;;
        core/scripts/*.sh|core/scripts/*.py) return 0;;
        mind_api/src/*.py|mind_api/src/*/*.py|mind_api/scripts/*.py) return 0;;
        .claude/skills/*/SKILL.md) return 0;;
        *) return 1;;
    esac
}

# The env-READ patterns. A match is a regression:
#   Python  — (os.)environ[.get](...) or (os.)getenv(...) on the same line as the flag
#   Shell   — $OWNERSHIP_MODE / ${OWNERSHIP_MODE...}
READ_RE='((environ|getenv).*(OWNERSHIP_MODE|MACHINE_OWNED_AGENTS))|(\$\{?(OWNERSHIP_MODE|MACHINE_OWNED_AGENTS)\b)'

# scan <file> <body> -> exit 0 if a regression READ is present.
scan() {
    local f="$1" body="$2" code
    in_scope "$f" || return 1
    # strip diff '+' prefix then drop shell/python '#' comment lines + blanks
    code="$(printf '%s\n' "$body" | sed 's/^+//' \
            | grep -vE '^[[:space:]]*#' | grep -vE '^[[:space:]]*$' || true)"
    [ -z "$code" ] && return 1
    printf '%s\n' "$code" | grep -Eq "$READ_RE" && return 0
    return 1
}

rc=0
if [ "$MODE" = "precommit" ]; then
    # Batched precommit scan (). The prior form ran a separate
    # `git diff --cached -- "$f"` (plus a ~6-subprocess scan pipeline) for EVERY
    # staged file — ~134s on the v2.5.0 436-file ZDS plant, contributing to the
    # >10-min hook budget overrun that forced a documented --no-verify (rb-4251).
    # Instead: compute the IN-SCOPE staged set first (a plant is mostly non-code,
    # so this is a small fraction), then ONE `git diff --cached` over ONLY those
    # files, demuxed into per-file added-line blocks in a pure-bash loop, then the
    # UNCHANGED `scan` function runs once per file — identical detection (same
    # sed-strip + comment/blank drop + READ_RE grep). Two git diffs total
    # (name-only + in-scope content), not one-per-staged-file.
    mapfile -t staged < <(git diff --cached --name-only)
    INSCOPE=()
    for f in "${staged[@]}"; do
        [ -n "$f" ] || continue
        in_scope "$f" && INSCOPE+=("$f")
    done
    if [ "${#INSCOPE[@]}" -gt 0 ]; then
        declare -A ADDED_BY_FILE=()
        order=()   # first-seen (path-sorted) order → deterministic output matching the old per-file loop
        current=""
        while IFS= read -r dl; do
            case "$dl" in
                'diff --git '*) current="" ;;              # new file block — clear until +++ b/
                '+++ b/'*) current="${dl#+++ b/}" ;;       # set current file
                '+++'*) : ;;                               # any other +++ (dev/null, or ++-content rendered +++) — skip, mirroring `grep -v '^+++'`
                '+'*)
                    # Added content line. git keeps the leading '+', which scan's
                    # `sed 's/^+//'` strips — store WITH the '+', exactly like the old
                    # `git diff | grep '^+' | grep -v '^+++'` ADDED block.
                    if [ -n "$current" ]; then
                        [ -n "${ADDED_BY_FILE[$current]+x}" ] || order+=("$current")
                        ADDED_BY_FILE["$current"]+="${dl}"$'\n'
                    fi
                    ;;
            esac
        done < <(git diff --cached -- "${INSCOPE[@]}")
        for f in "${order[@]}"; do
            if scan "$f" "${ADDED_BY_FILE[$f]}"; then
                echo "BLOCKED: $f re-introduces a read of OWNERSHIP_MODE / MACHINE_OWNED_AGENTS." >&2
                echo "  Both were removed 2026-07-02 (g-115-1737). Single-runner ownership is" >&2
                echo "  unconditional, keyed on STORAGE_BACKEND. Fix the code, do not --no-verify." >&2
                echo "  See mind_api/docs/lodestar-dynamic-ownership-design.md (SUPERSEDED note)." >&2
                rc=1
            fi
        done
    fi
else
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        if scan "$f" "$(cat "$f")"; then
            echo "AUDIT HIT: $f reads OWNERSHIP_MODE / MACHINE_OWNED_AGENTS (removed g-115-1737)." >&2
            rc=1
        fi
    done < <(git ls-files 'core/scripts/*.sh' 'core/scripts/*.py' \
                 'mind_api/src/*.py' 'mind_api/src/**/*.py' \
                 'mind_api/scripts/*.py' '.claude/skills/*/SKILL.md')
    [ "$rc" = "0" ] && echo "audit clean: no OWNERSHIP_MODE / MACHINE_OWNED_AGENTS reads in framework code"
fi

exit "$rc"
