#!/usr/bin/env bash
# Layer B (pre-commit, default) + Layer D (--audit) of the daemon-only drift
# defense. SINGLE SOURCE of detection logic for both layers — the recurring
# audit goal and the pre-commit hook call this same script.
# See .claude/rules/no-python-cli-fallback.md.
#
# Why this lives in core/ (not world/scripts/): a git pre-commit hook must be
# reliable and version-controlled. The original world/scripts/ copy resolved
# its repo root relative to the external (per-tenant, possibly-NAS) world
# path, so it could never function as a hook. Framework infra belongs in
# core/ and ships with the repo.
#
# Detection model (the non-obvious part):
#   A re-added `python3 .../scripts/X.py <subcmd>` line is SYNTACTICALLY
#   identical in a re-introduced daemon fallback and in a legitimate pure-CLI
#   wrapper. The discriminator is `rt_call`: a daemon-aware wrapper calls
#   rt_call and MUST be daemon-only; a pure-CLI wrapper has no rt_call and
#   legitimately execs python. So a python-script invocation is a
#   regression ONLY inside a file that also contains rt_call.
#   Scale, re-measured 2026-08-17 ( sweep): 116 of 551 core/scripts/*.sh
#   are in scope. This comment previously read "35 daemon-aware / ~280 pure-CLI"
#   — stale by 3.3x, flagged by a prior sweep that judged it out of scope to fix.
#   The DENOMINATOR moved 502 -> 551 in six days while the NUMERATOR held at
#   exactly 116: 49 new wrappers, none of them daemon-aware. That is not a
#   regression this gate detects (it watches for fallbacks coming BACK), but it
#   does say the daemon-only surface is static while the wrapper corpus grows.
#   Counts are DESCRIPTIVE only: scan() computes membership per-file at run time,
#   so behaviour never depended on them. Re-derive rather than trust a number here.
#
#   WHICH grep IS THE MEMBERSHIP TEST — the easy thing to get wrong, and a prior
#   sweep did: it is the BARE `grep -Eq 'rt_call' "$f"` at scan()'s rt_call branch,
#   over the WHOLE file, which yields 116. The comment-stripping just above it
#   applies to $body/$code — the candidate lines judged for a python invocation —
#   NOT to membership. So "files carrying a NON-COMMENT rt_call" (114 here) is a
#   different, narrower set than the one this script actually gates on, and the
#   2-file gap is exactly the files that only MENTION rt_call in a comment.
#   `_fallback_exec` and the `python3 -m core.scripts.` module form are
#   unambiguous regressions anywhere in core/scripts/*.sh.
#
# Feature-path exception: tree-read.sh and retrieve.sh keep rt_call for the
# migrated subset but ALSO exec python for un-migrated operations that have
# NO daemon endpoint (tree.py read = computational flags; retrieve.py =
# counter-bump). That is feature routing, not a daemon-unreachable fallback,
# so the python-invocation check is suppressed for those two files. They are
# still checked for `_fallback_exec`.
#
# Deliberately NOT checked: re-added `def cmd_<name>` / `add_parser("<name>")`
# in *.py files. Some such restorations are legitimate (tree.py `read` was
# correctly restored 2026-05-15 because its computational ops were never
# migrated). A blanket .py pattern would false-positive on legitimate
# un-migrated restorations — and a re-added cmd_ is only harmful if a wrapper
# also calls it, which the rt_call-gated .sh check above already catches.
# Fail-open over false-positive-blocker (rb-246 / guard-147 discipline).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    # Not in a git work tree — nothing to gate. Fail open, never block.
    exit 0
}
cd "$REPO_ROOT"

MODE="precommit"
[ "${1:-}" = "--audit" ] && MODE="audit"

is_feature_path() {
    case "$1" in
        core/scripts/tree-read.sh|core/scripts/retrieve.sh) return 0;;
        *) return 1;;
    esac
}

# scan <file> <body-to-check> → exit 0 if a regression is present.
# <file> is always the on-disk path (used to test rt_call membership);
# <body> is the added-lines slice (pre-commit) or whole file (audit).
#
# A regression is CODE, not a comment. Comment lines are stripped before
# matching: every DAEMON-ONLY marker legitimately says "No Python CLI
# fallback" and _runtime.sh documents "Replaces _fallback_exec" — neither
# is a regression. Pre-commit bodies are diff-prefixed with a single '+'
# ('+++' headers already removed by the caller); that prefix is stripped
# before the comment filter so '+# comment' is correctly treated as a
# comment.
scan() {
    local f="$1" body="$2" code
    # The detector necessarily contains the pattern strings as its own
    # detection logic — never flag it (or it can never be committed).
    [ "$f" = "core/scripts/check-no-python-cli-fallback.sh" ] && return 1
    code="$(printf '%s\n' "$body" | sed 's/^+//' \
            | grep -vE '^[[:space:]]*#' | grep -vE '^[[:space:]]*$' || true)"
    [ -z "$code" ] && return 1
    printf '%s\n' "$code" | grep -Eq '_fallback_exec|python3 -m core\.scripts\.' && return 0
    if grep -Eq 'rt_call' "$f" && ! is_feature_path "$f"; then
        printf '%s\n' "$code" | grep -Eq 'python3[^|]*/scripts/[A-Za-z0-9_-]+\.py' && return 0
    fi
    return 1
}

rc=0
if [ "$MODE" = "precommit" ]; then
    STAGED="$(git diff --cached --name-only)"
    [ -z "$STAGED" ] && exit 0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        case "$f" in core/scripts/*.sh) ;; *) continue;; esac
        [ -f "$f" ] || continue
        ADDED="$(git diff --cached -- "$f" | grep '^+' | grep -v '^+++' || true)"
        [ -z "$ADDED" ] && continue
        if scan "$f" "$ADDED"; then
            echo "BLOCKED: $f re-introduces a Python-CLI-fallback pattern." >&2
            echo "  Daemon-only since 2026-05-14. Fix the code, do not --no-verify." >&2
            echo "  See .claude/rules/no-python-cli-fallback.md" >&2
            rc=1
        fi
    done <<< "$STAGED"
else
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        if scan "$f" "$(cat "$f")"; then
            echo "AUDIT HIT: $f matches a Python-CLI-fallback pattern." >&2
            rc=1
        fi
    done < <(git ls-files 'core/scripts/*.sh')
    [ "$rc" = "0" ] && echo "audit clean: no Python-CLI-fallback regressions in core/scripts/*.sh"
fi

exit "$rc"
