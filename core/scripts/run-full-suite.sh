#!/usr/bin/env bash
# run-full-suite — thin wrapper over run-full-suite.py.
#
# The ONE safe way to run the framework suite on this box. Pins
# STORAGE_BACKEND=local (guard-955), excludes daemon_integration
# (Live-Daemon Exception), and chunks into fresh processes (guard-1448).
#
# Exit: 0 clean | 1 genuine failures | 2 INVALID/contended (re-measure) | 3 setup
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

#  cygpath conversion. Under Git Bash on Windows, $(cd ... && pwd)
# returns POSIX /c/... which Windows python3 reads as drive C: plus a literal
# c/ subdir, yielding FileNotFoundError on C:\c\...\run-full-suite.py. Convert
# to Windows-native form before exec. Linux/macOS lack cygpath and fall
# through with SCRIPT_DIR unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/run-full-suite.py" "$@"
