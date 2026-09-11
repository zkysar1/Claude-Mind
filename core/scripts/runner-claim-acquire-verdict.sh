#!/usr/bin/env bash
# runner-claim-acquire-verdict.sh -- turn a `runner-claim.sh acquire` exit code
# into a /start verdict. Thin wrapper over runner_claim_acquire.py.
#
#   runner-claim-acquire-verdict.sh <rc> [--reducer-only]
#     stdout: exactly one of  proceed | join-as-worker | refuse
#     stderr: one line naming the rc and why
#     exit:   always 0 -- the VERDICT is the product; an exit code here would
#             just be a second thing to interpret, which is the failure this
#             script exists to remove.
#
# WHY (2026-09-10, live paying-member incident): a resident agent hit
# ACQUIRE_RC=1 (daemon unreachable), found no branch for it in
# .claude/skills/start/SKILL.md -- which documents ONLY the two rc=4 cases --
# and halted, sitting IDLE through a billed hour while mailing the owner the
# same three options twice. The fail-open rule for every non-4 rc was real but
# lived in core/config/start-phase-c.md, a different file describing a
# different flow, and in runner-claim.sh's own rc contract ("caller decides;
# the three mutating call sites fail open"). rb-189 / guard-399: an
# "LLM must do X at step N" instruction is worth nothing without the bash path.
#
# Usage from /start, replacing any interpretation of the integer:
#   bash core/scripts/runner-claim.sh acquire --agent <a>; RC=$?
#   bash core/scripts/runner-claim-acquire-verdict.sh "$RC" [--reducer-only]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
# : under Git Bash on Windows $(cd ... && pwd) returns POSIX /c/...,
# which Windows python3 reads as drive C: plus a literal `c/` subdir.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi
exec python3 "$SCRIPT_DIR_NATIVE/runner_claim_acquire.py" "$@"
