#!/usr/bin/env bash
# notify-user.sh -- the FRAMEWORK notification chokepoint (thin wrapper over
# notify_dispatch.py). Core decides it wants to notify the user and runs every
# check; the domain supplies only the transport via the executable slot
# $WORLD_DIR/scripts/notify-transport.sh (payload JSON on stdin,
# NOTIFY_DISPATCHED=1 in env). Core never names an email/SMS/webhook script.
#
#   notify-user.sh --category C --subject S (--message M | --message-file F)
#                  [--goal-id G] [--allow-duplicate '<what is new>'] [--dry-run]
#                  [--in-reply-to '<what he asked, and when>']
#     --in-reply-to is REQUIRED for --category reply (rc 2 without it) and is
#     appended to the body. `reply` is an ALWAYS_SEND category, so the citation
#     is what keeps it from becoming a way to re-send a message the routing gate
#     refused (, guard-4722).
#   <builder payload> | notify-user.sh --payload-stdin   (already-built payload)
#
# Order: routing gate -> prior-outreach gate -> payload builder -> transport
#        slot -> ledger record (+ peer mirror).
# Exit: 0 sent | 2 usage/build | 3 suppressed by routing (re-routed to board)
#       | 4 duplicate | 5 no transport configured | 6 transport failed
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
exec python3 "$SCRIPT_DIR_NATIVE/notify_dispatch.py" "$@"
