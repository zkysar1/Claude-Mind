#!/usr/bin/env bash
# Layer B (pre-commit, default) + Layer D (--audit) drift defense — catches
# "defensive non-CLI fallback" patterns: a daemon wrapper that calls rt_call
# AND ALSO opens the daemon's data files directly to second-guess responses.
#
# Sister gate to check-no-python-cli-fallback.sh. That gate catches the
# OBVIOUS regression shape (_fallback_exec / python3 -m core.scripts.X).
# This gate catches the SUBTLER shape — wrapper carefully avoids the
# obvious patterns and inlines its own reader.
#
# Canonical incident:  / pipeline-read.sh — wrapper called
# rt_call for /v1/pipeline/read?counts=1, then ran an inline `py -3 -c`
# block that opened WORLD_PATH/pipeline.jsonl directly to re-derive the
# counts when the daemon response looked all-zero. The wrapper itself
# was technically free of `_fallback_exec` (the obvious gate had nothing
# to say about it) but it duplicated daemon logic in shell, masking an
# intermittent daemon bug rather than fixing it at source. The root
# cause (pipeline_write._update_meta missing live_path.lock) was fixed
# 2026-05-18; this gate prevents the next such band-aid from landing.
#
# Detection model:
#   A wrapper is flagged when ALL of these hold:
#     1. The file calls `rt_call` (it IS a daemon wrapper).
#     2. The file contains an inline `py -3 -c "..."` or `python3 -c "..."`
#        block (multi-line ok — shell heredoc semantics keep it one logical
#        invocation).
#     3. That file references `Path(...WORLD_PATH...)` /
#        `Path(...META_PATH...)` / `Path(...AGENT_DIR...)` —
#        constructing a path into daemon-managed data — and the path
#        ends in `.jsonl` / `.json` / `.yaml` somewhere in the file.
#
#   The 3-of-3 combination is the smoking gun. Wrappers that only do (1)+(2)
#   without (3) are fine (legitimate response-shape transforms). Wrappers
#   that do (3) without (1) are not daemon wrappers and the gate doesn't
#   apply.
#
# Fail-open: if a check raises infrastructurally (no git, no PROJECT_ROOT,
# etc.) the gate exits 0. Loud-fail belongs to (1) the daemon at runtime
# when the response is actually wrong, and (2) the regression test added
# in the same PR as the root-cause fix.
#
# Exemption: add `# daemon-wrapper-reparse-exempt: <rationale>` anywhere
# in the file. Use sparingly; the right answer to a "I need this band-aid"
# moment is almost always "fix the daemon endpoint instead." If you DO
# exempt, file the daemon-side root-cause goal in the same PR.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

MODE="precommit"
[ "${1:-}" = "--audit" ] && MODE="audit"

# scan <file> → exit 0 if the anti-pattern is present.
scan() {
    local f="$1"
    # Never flag the detector itself (it contains its own patterns).
    [ "$f" = "core/scripts/check-no-daemon-wrapper-reparse.sh" ] && return 1
    [ -f "$f" ] || return 1

    # Exemption marker takes precedence over detection.
    grep -q 'daemon-wrapper-reparse-exempt:' "$f" && return 1

    # Strip line-comments and blank lines so a comment that documents the
    # pattern (e.g., a guard rule referencing rt_call + WORLD_PATH for
    # pedagogical purposes) doesn't false-positive. Inline-python source
    # body is not comment-prefixed in bash; this stripping affects shell
    # context only — the python body inside `py -3 -c "..."` survives.
    local code
    code="$(sed 's/[[:space:]]*#.*$//' "$f" | grep -vE '^[[:space:]]*$' || true)"
    [ -z "$code" ] && return 1

    # (1) Daemon wrapper signal.
    printf '%s\n' "$code" | grep -Eq 'rt_call' || return 1

    # (2) Inline python block.
    printf '%s\n' "$code" | grep -Eq '(py -3|python3)[[:space:]]+-c' || return 1

    # (3) Constructs a Path under WORLD_PATH / META_PATH / AGENT_DIR.
    printf '%s\n' "$code" | grep -Eq 'Path\([^)]*(WORLD_PATH|META_PATH|AGENT_DIR)' || return 1

    # (3b) The constructed path is a daemon-served data file.
    printf '%s\n' "$code" | grep -Eq '\.(jsonl|json|yaml)' || return 1

    return 0
}

rc=0
files_to_check=()
if [ "$MODE" = "precommit" ]; then
    STAGED="$(git diff --cached --name-only 2>/dev/null || true)"
    [ -z "$STAGED" ] && exit 0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        case "$f" in core/scripts/*.sh) files_to_check+=("$f");; esac
    done <<< "$STAGED"
else
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        files_to_check+=("$f")
    done < <(git ls-files 'core/scripts/*.sh' 2>/dev/null || true)
fi

if [ ${#files_to_check[@]} -eq 0 ]; then
    [ "$MODE" = "audit" ] && \
        echo "audit clean: no core/scripts/*.sh files to scan"
    exit 0
fi

for f in "${files_to_check[@]}"; do
    if scan "$f"; then
        if [ "$MODE" = "precommit" ]; then
            echo "BLOCKED: $f looks like a daemon wrapper re-parsing daemon" >&2
            echo "  data files directly (rt_call + inline py-c block +" >&2
            echo "  Path(WORLD_PATH|META_PATH|AGENT_DIR) → *.jsonl|json|yaml)." >&2
            echo "  This pattern masks daemon bugs instead of fixing them at" >&2
            echo "  source. Canonical incident: g-115-740. Fix the daemon" >&2
            echo "  endpoint; don't band-aid the wrapper. If genuinely" >&2
            echo "  unavoidable, add a 'daemon-wrapper-reparse-exempt: <why>'" >&2
            echo "  comment and file the daemon-side root-cause goal in the" >&2
            echo "  same PR." >&2
        else
            echo "AUDIT HIT: $f matches the daemon-bypass-reparse pattern." >&2
        fi
        rc=1
    fi
done

if [ "$rc" = "0" ] && [ "$MODE" = "audit" ]; then
    echo "audit clean: no daemon-bypass-reparse regressions in core/scripts/*.sh"
fi
exit "$rc"
