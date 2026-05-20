# shellcheck shell=bash
# _goal-arg-normalize.sh — dual-accept goal-ID normalization library.
#
# Source this file (NOT exec) from any wrapper that takes a goal ID. It
# reads the caller's positional parameters ($@), normalises ONLY the
# explicit-flag forms:
#     --goal <id>      --goal=<id>
#     --goal-id <id>   --goal-id=<id>
# into the caller's preferred style, then rewrites $@ via `set --`.
#
# Bare positional `g-NNN-NN` is intentionally NOT matched. The normalizer
# does one thing: translate flag aliases. Whether a script accepts a bare
# positional is a property of its own arg-parse logic, not of this library.
# Guessing intent from value shape was a fallback that masked user errors
# (e.g. mis-ordered args silently auto-reordering) and broke the
# `--summary "g-NNN-NN"` corner case in iteration-close.sh.
#
# Configure via env var GOAL_NORMALIZE_TARGET BEFORE the source line:
#   positional   — prepend the goal value as $1, drop the flag form
#   --goal       — append `--goal <value>` after other args, drop --goal-id
#   --goal-id    — append `--goal-id <value>` after other args, drop --goal
#
# Why a sourced library instead of inline-per-script: 12 wrappers need this
# and scripts that reach for the same idiom in the future get a one-line
# integration. Single source of truth keeps the goal-id contract enforced
# in exactly one place. See core/config/conventions/aspirations.md.
#
# Bash quirk: `source file` runs in the calling shell, so `set --` here
# rewrites the caller's positional parameters. This is the whole point.

_GAN_TARGET="${GOAL_NORMALIZE_TARGET:-}"
if [ -z "$_GAN_TARGET" ]; then
    return 0 2>/dev/null || exit 0
fi

_GAN_GOAL=""
_GAN_REST=()
while [ $# -gt 0 ]; do
    if [ "$1" = "--goal" ] || [ "$1" = "--goal-id" ]; then
        if [ $# -lt 2 ]; then
            # Missing value — leave the flag alone, let downstream complain.
            _GAN_REST+=("$1")
            shift
        else
            _GAN_GOAL="$2"
            shift 2
        fi
    elif [[ "$1" == --goal=* ]] || [[ "$1" == --goal-id=* ]]; then
        _GAN_GOAL="${1#*=}"
        shift
    else
        _GAN_REST+=("$1")
        shift
    fi
done

case "$_GAN_TARGET" in
    positional)
        if [ -n "$_GAN_GOAL" ]; then
            set -- "$_GAN_GOAL" "${_GAN_REST[@]}"
        else
            set -- "${_GAN_REST[@]}"
        fi
        ;;
    --goal)
        if [ -n "$_GAN_GOAL" ]; then
            set -- "${_GAN_REST[@]}" --goal "$_GAN_GOAL"
        else
            set -- "${_GAN_REST[@]}"
        fi
        ;;
    --goal-id)
        if [ -n "$_GAN_GOAL" ]; then
            set -- "${_GAN_REST[@]}" --goal-id "$_GAN_GOAL"
        else
            set -- "${_GAN_REST[@]}"
        fi
        ;;
esac

unset _GAN_TARGET _GAN_GOAL _GAN_GOAL_SEEN _GAN_REST GOAL_NORMALIZE_TARGET
