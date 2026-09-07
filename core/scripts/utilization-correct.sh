#!/usr/bin/env bash
# utilization-correct — the ONE sanctioned way to correct a mis-credited
# utilization counter on a reasoning-bank OR guardrails record ().
#
# Usage:
#   utilization-correct.sh --store <reasoning-bank|guardrails> --id <rec-id> \
#       --counter <name> --reason "<why>" [--by <positive-int>]
#
#   e.g. utilization-correct.sh --store guardrails --id guard-352 \
#            --counter times_helpful --by 1 \
#            --reason "credited to a retrieval that never surfaced it"
#
# WHY THIS AND NOT `--by -1` ON THE INCREMENT WRAPPERS. The originating goal
# named the bare signed delta as the minimal option. It is structurally unsafe,
# measured rather than preferred: `coordination_merge.merge_utilization_counters`
# takes a per-counter MAX across boxes ("MAX never loses an increment, it can
# only fail to gain one" -- its own docstring), so a decrement is silently
# reverted at the next cross-box merge. Every layer below that merge already
# accepts a signed delta, which is what makes the bare form look correct: it
# works on ONE box. A correction is therefore an INCREMENT of a monotone
# `<counter>__corrected` sibling, which MAX reconciles correctly, and
# `_utilization_store.apply_corrections` nets it out at read time.
#
# ONE MECHANISM FOR BOTH STORES, per the goal: --store selects, everything else
# is identical. Both stores carry the same increment endpoint, the same counter
# set and the same read path, so two wrappers would only be two places to drift.
#
# A THIN SHIM ON PURPOSE. Validation, the audit ledger and the counter write all
# live in `_utilization_correct.py`, which drives the store's OWN sanctioned
# wrapper (`guardrails-increment.sh` / `reasoning-bank-increment.sh`) for the
# write. That is the canonical production code path, so the daemon ceremony is
# not re-implemented here -- and it keeps this file free of the daemon-call
# helper, which is exactly how `check-no-python-cli-fallback.sh` distinguishes a
# daemon-aware wrapper (must never exec python) from a pure-CLI one. This IS a
# pure-CLI wrapper: it has no daemon endpoint of its own.
#
# The token naming that helper is deliberately not spelled here: the gate's
# membership test is a bare whole-file grep, comments included (its own header
# documents the two files that qualify on a mention alone), so writing it out
# would put this file in scope and block the commit for a sentence.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$PROJECT_ROOT/core/scripts/_utilization_correct.py" correct "$@"
