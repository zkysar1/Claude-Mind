#!/usr/bin/env bash
# sleep-directive.sh -- the loop's "how to yield on a sleep" block, for a SHELL printer
# (idle-tick.sh). One owner: _sleep_directive.py (the same text the Python printers get).
#
# Usage:
#   sleep-directive.sh <sleep_seconds> <agent> <env_prefix>
#
# This wrapper exists so a shell script gets the python-invocation-safe path
# (python-invocation.md: python3 only inside a .sh that sources _paths.sh).
source "$(dirname "${BASH_SOURCE[0]}")/_paths.sh"
exec python3 "$(dirname "${BASH_SOURCE[0]}")/_sleep_directive.py" "$@"
