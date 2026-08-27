#!/usr/bin/env bash
# peer-board-post.sh -- post a board message to a PEER deployment's board.
#
# Usage: echo "message" | bash core/scripts/peer-board-post.sh \
#          --peer <environment-id> --channel <name> \
#          [--type <t>] [--tags <t1,t2>] [--reply-to <id>] [--author <a>] [--dry-run]
#
# Message comes via STDIN (guard-1036 -- same contract as board-post.sh).
#
# Exit codes: 0 ok | 2 usage/registry error | 3 peer unreachable from this box
#             | 4 refused (peer == self)
#
# See core/config/conventions/cross-deployment-channel.md. The engine pins the
# PEER's storage backend before importing _fileops -- see peer_board_post.py.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  cygpath conversion. Under Git Bash on Windows, $(cd ... && pwd)
# returns POSIX /c/... which Windows python3 reads as drive C: plus a literal
# c/ subdir, yielding FileNotFoundError on C:\...\peer_board_post.py. Convert
# to Windows-native form before exec. Linux/macOS lack cygpath and fall
# through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/peer_board_post.py" "$@"
