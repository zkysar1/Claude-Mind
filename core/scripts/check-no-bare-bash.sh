#!/usr/bin/env bash
# Layer B (pre-commit, default) + Layer D (--audit) of the bare-bash argv[0]
# defense (guard-580, goal ). Thin wrapper over
# core/scripts/check-no-bare-bash.py — the detection logic lives there because
# it needs Python's `ast` to exclude docstring/comment prose mentions exactly
# (see that file's module docstring for why grep cannot).
#
# Usage:
#   bash core/scripts/check-no-bare-bash.sh            # pre-commit: staged added lines
#   bash core/scripts/check-no-bare-bash.sh --audit    # whole tracked tree
#   bash core/scripts/check-no-bare-bash.sh --paths a.py b.py
#   echo '<python>' | bash core/scripts/check-no-bare-bash.sh --snippet
#
# Exit 0 = clean, 1 = violation(s) found. Fails open (0) outside a git tree.
#
# Companion to check-no-python-cli-fallback.sh (the wiring precedent) and
# check-no-ownership-flag.sh. Wired as Gate 12 in core/githooks/pre-commit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Windows Git Bash reaches Python through the `py` launcher; POSIX uses
# python3. Same idiom as core/githooks/pre-commit.
case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) exec py -3 "$SCRIPT_DIR/check-no-bare-bash.py" "$@" ;;
    *) exec python3 "$SCRIPT_DIR/check-no-bare-bash.py" "$@" ;;
esac
