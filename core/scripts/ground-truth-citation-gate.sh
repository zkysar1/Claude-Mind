#!/usr/bin/env bash
# ground-truth-citation-gate.sh — PreToolUse[Write|Edit|MultiEdit] wrapper ().
#
# DELIBERATE DEVIATION FROM THE SIBLING WRAPPERS, do not "tidy" it back
# (guard-2410): the python call's stderr is NOT redirected to /dev/null here.
# This gate's advisory is written on BOTH channels on purpose — stdout carries
# the structured payload the model reads, stderr carries the same text for the
# human terminal, and neither reaches the other's reader (guard-1680). The
# exemplar wrappers suppress stderr because they emit a DENY on stdout and write
# nothing else; copying that policy would mute half of this gate's output while
# leaving the source line looking live.
#
# The hazard that suppression normally guards — a traceback reaching stderr — is
# closed at its SOURCE instead: the .py wraps main() so any exception exits 0.
set -uo pipefail
_fail_open() { exit 0; }
trap '_fail_open' ERR

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
python3 "$CORE_ROOT/scripts/ground-truth-citation-gate.py" || exit 0
exit 0
