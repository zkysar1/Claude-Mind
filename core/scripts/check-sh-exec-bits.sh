#!/usr/bin/env bash
# check-sh-exec-bits.sh — regression check for the exec bit on core/scripts *.sh.
#
# WHY: this repo was authored on Windows (git stores *.sh at mode 100644) but
# runs on a Linux box. On a Linux checkout a *.sh at 100644 is NOT executable,
# so DIRECT-EXEC chains — `exec "$DIR/foo.sh"`, `./foo.sh` — fail with
# 'Permission denied'. Scripts invoked as `bash foo.sh` are unaffected, which is
# why the gap hid until an `exec`-chain hit it (echo boot 2026-07-10, :
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
# TWO MODES, MEASURING DIFFERENT THINGS — both are needed ():
#
#   (default)  FILESYSTEM scan: is this file executable ON THIS BOX RIGHT NOW.
#              Answers "will a direct-exec chain work here". Consumed by
#              /verify-learning.
#   --staged   GIT INDEX scan of staged *.sh: what mode is about to be
#              COMMITTED. Answers "will this file be executable on every OTHER
#              box". Consumed by the pre-commit hook (Gate 13).
#
# The distinction is the whole point and the filesystem check CANNOT stand in
# for the index check. What propagates fleet-wide is the mode git records, and
# the two diverge exactly where this bug lives: on a clone with
# `core.filemode=false` (the default git picks on Windows, and this repo was
# authored on Windows) a `chmod +x` never reaches the index, so the filesystem
# reports executable while the commit carries 100644. The filesystem gate then
# passes on the box that introduces the defect, and the breakage surfaces on a
# Linux checkout — which is precisely the  incident above, and
# precisely the case a pre-commit gate exists to stop at the source.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # core/scripts

if [[ "${1:-}" == "--staged" ]]; then
    # Index modes for staged core/scripts *.sh. Ask git for the PATHS and then
    # ask git for each path's MODE — never hand-parse `--raw`.
    #
    # WHY NOT --raw (measured, and this shipped wrong once): rename detection is
    # ON BY DEFAULT, so a rename inside core/scripts emits
    # ":100755 100644 <sha> <sha> R100\t<old>\t<new>" — TWO tabs. An awk that
    # strips to the FIRST tab then reports "<old>\t<new>" as the path and names
    # the OLD path, which no longer exists, so the "chmod +x <file>" fix line is
    # unrunnable. The bug hid because the obvious test moves a file INTO
    # core/scripts from outside the pathspec, which git renders as a plain add
    # with ONE tab (guard-920: replicate the literal production shape, not a
    # convenient one; guard-1083: never write a parsing pipe against an output
    # shape you have not looked at).
    #
    # --name-only yields exactly the NEW path for a rename, one per line;
    # --diff-filter=d drops deletions (no mode to gate); core.quotePath=false
    # keeps non-ASCII paths literal instead of C-quoted.
    bad=""
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        mode="$(git ls-files -s -- "$p" 2>/dev/null | awk '{print $1; exit}')"
        [ -n "$mode" ] || continue
        [ "$mode" = "100755" ] || bad="${bad}${mode}  ${p}"$'\n'
    done < <(git -c core.quotePath=false diff --cached --name-only --diff-filter=d \
                 -- 'core/scripts/*.sh' 2>/dev/null)
    bad="$(printf '%s' "$bad" | sed '/^$/d' | sort)"
    if [[ -n "$bad" ]]; then
        echo "FAIL: staged core/scripts *.sh with a non-executable mode in the INDEX —" >&2
        echo "      they will land at 100644 and break direct-exec chains on every Linux checkout:" >&2
        printf '%s\n' "$bad" | sed 's/^/  /' >&2
        echo "Fix: chmod +x <file> && git add <file>" >&2
        echo "     (if the mode will not stick: git config core.filemode true, or" >&2
        echo "      git update-index --chmod=+x <file> to set it in the index directly)" >&2
        exit 1
    fi
    exit 0
fi

# (default) FILESYSTEM scan -- unless the filesystem answer is meaningless
# here. Two cases, one remedy: core.fileMode=false (git ignores the
# filesystem mode, so a chmod never reaches the index) and Windows itself
# (NTFS has no execute bit at all; MSYS `-perm -u+x` is a shebang heuristic,
# not a mode). Measured 2026-09-02 () on a Windows box with
# fileMode=true: the find reported two 100755 sourced libraries as "missing"
# and passed fifteen 100644 scripts. In both cases the INDEX is the only
# meaningful answer to EITHER question, so scan every tracked *.sh in the
# index instead -- and say so, because "OK" from the wrong instrument is the
# shape that hid the v2.12.47 strip.
filemode="$(git -C "$SCRIPT_DIR" config --get core.fileMode 2>/dev/null || true)"
why=""
if [[ "${filemode,,}" == "false" ]]; then
    why="core.fileMode=false on this clone"
elif [[ "$(uname -s 2>/dev/null || true)" == MINGW* || "$(uname -s 2>/dev/null || true)" == MSYS* || "$(uname -s 2>/dev/null || true)" == CYGWIN* ]]; then
    why="Windows has no filesystem execute bit"
fi
if [[ -n "$why" ]]; then
    bad=""
    total=0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        mode="${line%% *}"
        p="${line#*$'\t'}"
        total=$((total + 1))
        [ "$mode" = "100755" ] || bad="${bad}${mode}  core/scripts/${p}"$'\n'
    done < <(git -C "$SCRIPT_DIR" -c core.quotePath=false ls-files -s -- '*.sh' 2>/dev/null)
    bad="$(printf '%s' "$bad" | sed '/^$/d' | sort)"
    if [[ -n "$bad" ]]; then
        count="$(printf '%s\n' "$bad" | grep -c . || true)"
        echo "FAIL: $count core/scripts *.sh file(s) at a non-executable mode in the INDEX ($why, so the index mode is the one that ships) — direct-exec chains will fail with 'Permission denied' on Linux:" >&2
        printf '%s\n' "$bad" | sed 's/^/  /' >&2
        echo "Fix: git update-index --chmod=+x <file> (a chmod +x does not reach the index here), then commit." >&2
        exit 1
    fi
    echo "OK: all ${total} tracked core/scripts *.sh files carry 100755 in the index ($why — index mode checked, not the filesystem)."
    exit 0
fi

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
