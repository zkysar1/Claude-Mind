#!/usr/bin/env bash
# Thin wrapper around notification_routing_gate.py's CLI, for shell scripts and
# SKILL.md pseudocode that must decide whether a notification reaches the USER
# or a fleet-side destination. Implements the 2026-08-10 user directive
# (); policy lives in world/conventions/notification-routing.md.
#
# guard-350: SKILL.md pseudocode must invoke Python through a .sh wrapper, never
# `py -3 core/scripts/<name>.py` inline. This file is that wrapper.
#
# Usage:
#   bash core/scripts/notification-routing-gate.sh \
#     --category <decision-needed|user-digest|blocker|completion|update|info|...> \
#     --subject "..." [--body "..."] [--caller "file.sh:func"] \
#     [--breadcrumb] [--tags "a,b"] [--quiet]
#
# EXIT CODE IS THE VERDICT — this wrapper deliberately PROPAGATES it, unlike the
# telemetry wrappers next door which always exit 0:
#   0 = SEND     -> the caller performs its send
#   1 = SUPPRESS -> the caller SKIPS its send
#
# Pass --breadcrumb whenever you intend to honor a SUPPRESS. It posts the
# re-route to world/board/findings.jsonl and returns 1 ONLY if that landed; a
# failed breadcrumb returns 0 so the caller sends instead. A suppression with no
# destination is a deletion (), and the shell caller cannot see that
# failure any other way.
#
# THE FAIL-SAFE DIRECTION IS INVERTED, as in the module: any failure of this
# wrapper itself returns 0 (SEND). "He asked not to be told about things we can
# handle; he did not ask to be unreachable." A broken gate must never silence a
# notification — it must let it through.
set +e
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/notification_routing_gate.py" "$@"
rc=$?

# 0 and 1 are the two real verdicts. Anything else (import error, argparse
# usage error, interpreter missing) is a BROKEN GATE, not a suppression —
# resolve it to SEND.
if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
  echo "notification-routing-gate: wrapper rc=$rc is not a verdict — failing open to SEND" >&2
  exit 0
fi
exit "$rc"
