#!/usr/bin/env bash
# GRANT store script API — thin wrapper over core/scripts/grants.py.
#
# Schema, semantics and worked examples: core/config/conventions/grants.md
#
#   grants.sh add < record.json                  create a grant (G1-G5 validated)
#   grants.sh list [--from-env X] [--to-env Y] [--covering NODE] [--status S|any]
#   grants.sh memberships --base-env X [--status any]
#   grants.sh check --from-env X --to-env Y [--node KEY]
#
# NOT DAEMON-ROUTED, deliberately, and this is the one place to say why. Every
# other store wrapper POSTs to the generic /v1/store/* endpoint; this one does
# not, because the policy half (`_grants.py`) is PURE BY CONTRACT — it imports
# no _fileops precisely so a gate deciding authorization can never bind a
# caller's storage backend. Routing reads through the daemon would hand that
# binding back. The WRITE path still gets locking + history + changelog: it goes
# through _fileops.locked_append_jsonl_with_allocator inside grants.py, which is
# the same primitive the daemon's append endpoint uses (guard-480).
#
# `add` READS THE RECORD ON STDIN, never as a flag or a positional (guard-979 /
# guard-2037). Passing it any other way is the measured wedge: a $(cat) reader
# with an inherited-open stdin blocks FOREVER with no error. grants.py refuses a
# TTY stdin fast and names the correct shape rather than hanging, but the call
# site is where it should be got right — redirect from a file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

# exec, not a subshell: stdin passes through untouched for `add`, and the exit
# code is grants.py's own — `check` returns 0 allow / 3 deny / 4 unavailable,
# and a wrapper that swallowed those would collapse the DENY-vs-UNAVAILABLE
# split the gate depends on.
exec python3 "$SCRIPT_DIR/grants.py" "$@"
