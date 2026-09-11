#!/usr/bin/env bash
# session-end-notice-mode.sh -- resolve the owner's shutdown-report knob.
# Thin wrapper over session_end_notice.py.
#
#   (no arguments)   prints exactly one of: auto | always | never
#                    exit 0 = the config was understood (explicitly, or by the
#                             documented absence of the key)
#                    exit 3 = config unreadable/unrecognised; stdout still
#                             carries the documented default `auto`
#
# Knob: core/config/aspirations.yaml -> session_end_notice.mode
# Consumer: .claude/skills/aspirations-consolidate/SKILL.md Step 9.7.
#
# WHY A SCRIPT AND NOT A PROSE MENTION (): the key was declared in
# config and named in Step 9.7's prose but read by NO code path -- measured
# 2026-09-10, three references, none of them executable. `mode: never` would
# have changed nothing while looking like it had. rb-189 names the class,
# guard-399 prescribes writing the bash path, and the neighbouring
# productivity_gate block in the same config file already records a prior
# author REFUSING an unreadable knob for the same reason ("a config key here
# would lie about what the script can do").
#
# stdout is always a usable mode, so a caller that ignores the exit status
# still behaves safely; one that checks it can distinguish "the owner chose
# auto" from "we could not tell what the owner chose".
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
# : under Git Bash on Windows $(cd ... && pwd) returns POSIX /c/...,
# which Windows python3 reads as drive C: plus a literal `c/` subdir. Convert
# to native form first. Linux/macOS lack cygpath and fall through unchanged.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi
exec python3 "$SCRIPT_DIR_NATIVE/session_end_notice.py" "$@"
