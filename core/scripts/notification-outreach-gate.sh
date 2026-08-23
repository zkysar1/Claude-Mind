#!/usr/bin/env bash
# notification-outreach-gate.sh -- fleet-wide "have we already told the user
# this?" ledger + gate. Thin wrapper over notification_outreach.py.
#
#   check  --subject S [--body B|--body-file F|--body -] [--category C] [--goal-id G] [--json]
#          exit 0 = no prior outreach (send) | 1 = DUPLICATE (prior rows printed) | 2 = usage
#   record --subject S ... [--transport T] [--rc N] [--to ADDR] [--mirror-peers]
#          [--suppressed-duplicate-of ID] [--override-reason "<why>"]
#   list   [--since-hours H] [--json]
#
# Ledger: world/notifications-sent.jsonl (S3-synced, fleet-visible). Peer worlds
# are covered by best-effort `user-outreach` mirrors on their coordination
# board (record --mirror-peers) and by reading the same tag on ours.
# Wired into: world/scripts/email-send.sh (the transport chokepoint -- every
# email passes through it, so the 11 direct call sites of  are
# covered too) and .claude/skills/notify-user/SKILL.md Step 1.7.
# NOT daemon-routed: pure local file read/append through _fileops.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\<script>.py. Convert to
# Windows-native form before passing as a Python arg. Linux/macOS lack cygpath
# and fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi
exec python3 "$SCRIPT_DIR_NATIVE/notification_outreach.py" "$@"
