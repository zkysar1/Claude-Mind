#!/usr/bin/env bash
# permissions-add.sh — Sanctioned wrapper that adds external-path permissions
# to .claude/settings.local.json.
#
# This is the user-authorized maintenance path for the constitutional anchor
# (rb-931, CLAUDE.md "two-file settings rule"). The LLM cannot Edit/Write/
# MultiEdit .claude/settings.local.json directly because the file's own
# deny[] hard-blocks those tool calls. Bash-level writes via this script
# ARE permitted — the deny patterns match Claude tool names, not the
# underlying filesystem.
#
# Resolves WORLD_DIR + META_DIR from the bound agent's local-paths.conf via
# _paths.sh — caller does not pass paths explicitly. PROJECT_ROOT is also
# resolved from _paths.sh. Requires MIND_AGENT to be set (PreToolUse[Bash]
# hook auto-injects this from the session binding written by /start A2).
#
# Usage:
#   bash core/scripts/permissions-add.sh          # the ONLY form that writes
#   bash core/scripts/permissions-add.sh --help   # usage; writes nothing
#
# This wrapper takes NO flags and NO positionals. Any flag is REFUSED (exit 2)
# rather than dropped — see the arg loop below for why that matters here more
# than on a typical wrapper.
#
# Exit codes:
#   0 — success (created or merged idempotently), or --help
#   2 — required state missing (MIND_AGENT unset, paths unresolved), or an
#       unrecognised flag was passed
#   3 — existing settings.local.json is malformed (refused to clobber)
#   4 — Python helper missing or Python launcher unavailable
#
# Origin: 2026-05-19 zeta /start audit, finding #1.

set -uo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# Shared strict-argv refusal, same adoption shape as aspirations-read.sh.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

# ONE literal, referenced by BOTH the --help arm and the refusal, so the two
# strings that must agree cannot drift apart ( fresh-eyes F-002).
_ACCEPTED_FLAGS="(none — this wrapper takes no flags and no positionals)"

# ARGUMENT PARSING. Until 2026-09-03 this script had NONE — no case, no while,
# no getopts — and never referenced "$@" at all. So `--help` was not a query, it
# was an INVOCATION: every argument was silently dropped and control fell
# through to the write path below, mutating .claude/settings.local.json, THE
# CONSTITUTIONAL ANCHOR (, measured — `--help` created a fresh
# settings.local.json on the first attempt).
#
# Why that is worse here than on a typical wrapper: `--help` is the single most
# reflexively-typed argument there is, and a caller reaches for it precisely
# when they do NOT yet know what a script does. The least-informed possible
# caller therefore got an unconfirmed write to the most-protected file in the
# repo. The blast radius was not theoretical.
#
# Placed AFTER _paths.sh (which resolves CORE_ROOT for the source above) and
# BEFORE the WORLD_DIR guard, so --help still answers when paths are
# unresolved — the state a confused caller is most likely to be in.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            # BEFORE the -*) arm: --help matches `-*`, and refusing it would be
            # a regression the refusal INTRODUCED rather than a defect it fixed
            # (guard-2680, ). Help exits 0 and writes nothing.
            argv_strict_help "$(basename "$0")" "[no positionals]" \
                "$_ACCEPTED_FLAGS" \
                "  Running this with NO arguments is the only form that writes: it merges
  external-path allows and the constitutional deny baseline into
  .claude/settings.local.json through the user-authorized maintenance path
  (rb-931, CLAUDE.md \"two-file settings rule\"). It is idempotent.";;
        -*)
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # Positionals accepted-and-ignored, matching aspirations-read.sh.
            # This wrapper takes none, so a positional is already a caller
            # error — but refusing them is a WIDER blast radius than this goal
            # measured (guard-1562: never ship a refusal without enumerating
            # what would newly fire), and the sole production call site
            # (core/config/start-uninitialized-ceremony.md) passes no arguments
            # at all.
            shift;;
    esac
done

if [ -z "${WORLD_DIR:-}" ] || [ -z "${META_DIR:-}" ]; then
    echo "ERROR: WORLD_DIR or META_DIR not resolved from local-paths.conf." >&2
    echo "  Bound agent: ${MIND_AGENT:-(none)}" >&2
    echo "  Expected agents/<agent>/local-paths.conf with WORLD_PATH and META_PATH set." >&2
    echo "  /start Phase B writes this file; ensure A2 binding + B1-B7 paths completed first." >&2
    exit 2
fi

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/permissions-add.py"
if [ ! -f "$SCRIPT_PATH" ]; then
    echo "ERROR: helper script not found: $SCRIPT_PATH" >&2
    exit 4
fi

# Pick the right Python launcher — same precedence as check-prerequisites.sh.
PY=""
if command -v py >/dev/null 2>&1 && py -3 --version >/dev/null 2>&1; then
    PY="py -3"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -qE 'Python 3\.'; then
    PY="python"
fi

if [ -z "$PY" ]; then
    echo "ERROR: neither 'py -3' nor 'python3' nor 'python' (3.x) found on PATH." >&2
    echo "  Install Python 3.10+ — see core/scripts/check-prerequisites.sh." >&2
    exit 4
fi

eval "$PY \"$SCRIPT_PATH\" \"$WORLD_DIR\" \"$META_DIR\" \"$PROJECT_ROOT\""
exit $?
