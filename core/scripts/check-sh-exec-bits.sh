#!/usr/bin/env bash
# check-sh-exec-bits.sh — regression check for the exec bit on core/scripts *.sh.
#
# WHY: this repo was authored on Windows (git stores *.sh at mode 100644) but
# runs on a Linux box. On a Linux checkout a *.sh at 100644 is NOT executable,
# so DIRECT-EXEC chains — `exec "$DIR/foo.sh"`, `./foo.sh` — fail with
# 'Permission denied'. Scripts invoked as `bash foo.sh` are unaffected, which is
# why the gap hid until an `exec`-chain hit it (echo boot 2026-07-10, 1:
# agent-aspirations-add-goal.sh line 7 exec'd aspirations-add-goal.sh).
#
# This check catches a newly-added core/scripts *.sh that lacks the exec bit
# BEFORE it breaks a direct-exec chain. Exit 0 = all carry +x; exit 1 = one or
# more lack it (listed on stderr with the fix).
#
# SCOPE: core/scripts only (git-tracked, so the 100755 mode commits and
# propagates fleet-wide). world/scripts *.sh exec bits are machine-local (the
# external world store is not git-tracked and S3 object sync does not preserve
# POSIX perms), so they are re-applied per machine and are intentionally OUT of
# this git-regression scope.
#
# Consumed by: /verify-learning (see core/config/verification-checklist.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # core/scripts

missing="$(find "$SCRIPT_DIR" -name '*.sh' -type f ! -perm -u+x 2>/dev/null | sort)"

if [[ -n "$missing" ]]; then
    count="$(printf '%s\n' "$missing" | grep -c . || true)"
    echo "FAIL: $count core/scripts *.sh file(s) missing the exec bit (100755) — direct-exec chains will fail with 'Permission denied' on Linux:" >&2
    printf '%s\n' "$missing" | sed 's/^/  /' >&2
    echo "Fix: chmod +x <file> (git core.filemode=true records the 100644->100755 mode change to commit)." >&2
    exit 1
fi

total="$(find "$SCRIPT_DIR" -name '*.sh' -type f | grep -c . || true)"
echo "OK: all ${total} core/scripts *.sh files carry the exec bit (100755)."
exit 0
