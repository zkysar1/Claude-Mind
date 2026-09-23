#!/usr/bin/env bash
# completion-digest.sh -- build the USER-FACING digest the completion report
# emails (thin wrapper over completion_digest.py). The on-disk
# COMPLETION-REPORT.md stays the agent-facing archive; this is what the user reads.
#   completion-digest.sh --agent <name> [--since ISO] [--notes-file F] [--out F]
#                        [--html-out F] [--world D] [--max-items N] [--json]
#
# --out IS MARKDOWN, WHATEVER THE FILENAME SAYS. The renderer is chosen by the
# FLAG, never by the extension: `--out x.html` writes MARKDOWN into x.html and
# reports a byte count, so the success echo confirms the write and not the
# content. The HTML email twin has its own flag, `--html-out`, and it was absent
# from this usage line until 2026-09-21 (, zeta/cc-02) -- a reader who
# consults the documented usage, as they should, finds no way to ask for HTML and
# infers the extension does it. Measured that day: `--out …/fleet-digest.html`
# silently replaced a 55,590-byte HTML digest with a 16,270-byte markdown one,
# byte-identical to the .md beside it. Pass BOTH flags in one call to write both
# twins; verify by an independent read-back (the HTML starts `<html>`), never by
# the wrapper's own "wrote N bytes" line.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\completion_digest.py.
# Convert to Windows-native form before exec. Linux/macOS lack cygpath and
# fall through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"; else SCRIPT_DIR_NATIVE="$SCRIPT_DIR"; fi
exec python3 "$SCRIPT_DIR_NATIVE/completion_digest.py" "$@"
