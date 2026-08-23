#!/usr/bin/env bash
#  — call-site wrapper for the reducer's no-double-resolution guard.
#
# WHY THIS WRAPPER EXISTS AT ALL. The call site it replaces was two Bash:
# directives in review-hypotheses/SKILL.md:
#     Bash: slot=$(bash core/scripts/wm-read.sh hyp_capture --json)
#     Bash: py -3 core/scripts/hyp_capture_guard.py --hypothesis-id <id> --slot-json "$slot"
# Shell state does not survive across Bash tool calls — each is a fresh shell
# (guard-128, guard-492) — so the second call received an EMPTY string and
# hyp_capture_guard.py's json.loads fail-open reported has_evidence:false on
# EVERY invocation, including against a fully populated slot.
#
# Measured 2026-08-11 (alpha, hostname cc-04, uname -r 6.8.0-137-generic), one
# seeded entry in the live slot, same hypothesis id, the two shapes back to
# back: two-call shape -> has_evidence:false, count 0. One-call shape ->
# has_evidence:true, count 1. The guard was inert at its only call site, and
# inert in the SILENT direction: "no worker evidence" and "I never looked" were
# the same output, so nothing downstream could notice (guard-2352).
#
# The fail-open inside hyp_capture_guard.main() could not have helped. Arguments
# are evaluated before the callee is entered, so a try/except around json.loads
# is structurally unreachable for a value that was already empty when python
# started (guard-3001). The fix has to move the read to where the value cannot
# be lost — here — which is also what guard-350 asks for independently (route
# SKILL.md python invocations through a .sh wrapper).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"

# Read the slot HERE, inside one shell, so no value crosses a tool-call
# boundary. Deliberately NOT the `2>/dev/null || echo '{}'` idiom used by
# belief-contradiction-check.sh:47 — guard-332 forbids it, and it is precisely
# what would re-hide this defect: a failed read would be laundered into a
# well-formed empty slot and report "no evidence" with total confidence.
# An unreadable slot must stay DISTINGUISHABLE from an empty one.
SLOT=""
if SLOT_OUT="$(bash "$CORE_ROOT/scripts/wm-read.sh" hyp_capture --json)"; then
  SLOT="$SLOT_OUT"
fi

# Advisory by contract: always exits 0 (see hyp_capture_guard.py main()). A
# non-zero exit here would invite a caller to read "evidence exists" as "do not
# resolve", which is the stuck-hypothesis state the module rejects.
exec python3 "$CORE_ROOT/scripts/hyp_capture_guard.py" --slot-json "$SLOT" "$@"
