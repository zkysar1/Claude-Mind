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
# the removed names; only a live env read reintroduces the behavior.
#
# TWO DETECTORS, split by comment model (). This script's grep path
# strips leading-'#' comment lines, which is a COMPLETE comment model for .sh and
# SKILL.md and is NOT one for Python: a triple-quoted docstring carries no '#', so
# a line of prose documenting the removed flag matched READ_RE and BLOCKED the
# commit (measured 2026-07-26; the only escapes were --no-verify, which the message
# below forbids, or deleting accurate documentation — the false-positive-blocker
# class of rb-246/guard-147). So .py is delegated to check-no-ownership-flag-py.py,
# which parses an AST: an env read is a Call or Subscript node and a docstring is
# Expr(Constant(str)), so prose cannot reach the detector at all rather than being
# filtered out of it. Same split, same reason, as check-no-bare-bash.py.
#   .sh + .claude/skills/*/SKILL.md  -> READ_RE grep below ('#' strip is complete)
#   *.py                             -> check-no-ownership-flag-py.py (AST)
#
# Scope: executable framework code only — core/scripts/*.{sh,py}, mind_api/src +
# mind_api/scripts *.py, and .claude/skills/*/SKILL.md pseudocode. Deliberately
# EXCLUDED: this detector; mind_api/docs/ (design history); */tests/ (fixtures may
# reference the names in docstrings); core/config/upgrade-recipes/ (the n3-mind
# rename maps MACHINE_OWNED_AGENTS as a string literal for a downstream env
# migration, not a read). Fail-open over false-positive-blocker (rb-246/guard-147).
set -euo pipefail

# Resolved BEFORE the cd below — the .py delegate is looked up here, not under
# $REPO_ROOT, so the lookup survives an absolute-path invocation from another tree.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    # Python is owned by the AST detector (see the header split). Returning "no
    # hit" here is not a coverage hole: run_py_detector below scans the SAME .py
    # surface in the same mode, and its rc is OR-ed into this script's rc.
    case "$f" in *.py) return 1;; esac
    # strip diff '+' prefix then drop shell/python '#' comment lines + blanks
    code="$(printf '%s\n' "$body" | sed 's/^+//' \
            | grep -vE '^[[:space:]]*#' | grep -vE '^[[:space:]]*$' || true)"
    [ -z "$code" ] && return 1
    printf '%s\n' "$code" | grep -Eq "$READ_RE" && return 0
    return 1
}

# .py delegate (see the header split). Same two modes, rc OR-ed into ours, so this
# script stays the SINGLE entry point both the pre-commit hook (Gate at
# core/githooks/pre-commit) and the Layer-D audit goal call.
# Interpreter selection is the check-no-bare-bash.sh precedent verbatim: `py -3` on
# Git Bash / MSYS, where a bare `python3` hits the Microsoft Store stub, and plain
# `python3` elsewhere. Fail-open when the helper is absent — a gate that cannot run
# must never block a commit, which is the same rb-246/guard-147 class this split
# exists to fix. Callers MUST use `|| rc=1`, never a bare call: `set -e` is on.
PY_VERDICT="unset"   # unset | clean | hit | absent | error — gates the clean banner

run_py_detector() {
    # Resolved from THIS script's dir, not $REPO_ROOT — the check-no-bare-bash.sh
    # precedent. The two coincide in-repo, but SCRIPT_DIR also survives being
    # invoked by absolute path from another tree, where a $REPO_ROOT-relative
    # lookup would miss the helper.
    local helper="$SCRIPT_DIR/check-no-ownership-flag-py.py"
    if [ ! -f "$helper" ]; then
        # FAIL OPEN on blocking, FAIL LOUD on the verdict. These are different
        # failure modes and conflating them is what made this a real defect:
        # scan() early-returns for *.py, so a missing delegate leaves .py covered
        # by NOTHING, and the caller below would still have printed "audit clean".
        # Measured in this goal's own fresh-eyes review (F1): with the helper
        # absent and a live os.environ.get(OWNERSHIP_MODE) committed, the script
        # printed the clean banner and exited 0. That is the guard-2097 hazard —
        # delegation removes the old path's coverage, so a failure in the new
        # path is a hole with nothing behind it.
        echo "WARNING: delegate missing: $helper" >&2
        echo "  The .py surface is NOT being checked for OWNERSHIP_MODE /" >&2
        echo "  MACHINE_OWNED_AGENTS reads. Restore it — the .py half of this" >&2
        echo "  gate is silently absent." >&2
        PY_VERDICT="absent"
        return 0
    fi
    local prc=0
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) py -3 "$helper" "$@" || prc=$? ;;
        *) python3 "$helper" "$@" || prc=$? ;;
    esac
    case "$prc" in
        0) PY_VERDICT="clean"; return 0 ;;
        1) PY_VERDICT="hit";   return 1 ;;
        *) # rc=3 (git could not enumerate) or any unexpected code: population
           # UNKNOWN. Never block on it, never call it clean.
           PY_VERDICT="error"
           echo "WARNING: .py detector did not complete (rc=$prc) — .py surface NOT checked." >&2
           return 0 ;;
    esac
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
    run_py_detector || rc=1
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
    # Must run BEFORE the banner — otherwise a .py hit prints "audit clean".
    run_py_detector --audit || rc=1
    if [ "$rc" = "0" ]; then
        # "clean" is a claim about a surface, so it may only be made when BOTH
        # halves actually read their surface. PY_VERDICT distinguishes "the .py
        # detector ran and found nothing" from "the .py detector never ran" —
        # without it those two print the same reassuring line.
        if [ "$PY_VERDICT" = "clean" ]; then
            echo "audit clean: no OWNERSHIP_MODE / MACHINE_OWNED_AGENTS reads in framework code"
        else
            echo "audit PARTIAL: .sh + SKILL.md surface clean; .py surface NOT checked (delegate: $PY_VERDICT)" >&2
        fi
    fi
fi

exit "$rc"
