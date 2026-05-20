#!/usr/bin/env bash
# Thin wrapper around _gate_log.py CLI. Used by SKILL.md-level gates that
# need to emit firings but can't import _gate_log directly (LLM-executed
# pseudocode, not Python). Best-effort — never fails the caller.
#
# Usage:
#   bash core/scripts/gate-log.sh <gate-id> <decision> \
#     [--caller "..."] [--trigger "..."] [--payload "..."] \
#     [--override-reason "..."] [--gate-error "..."] [--extra-json '{...}']
#
# decision must be one of: noop, pass, block, override, fail_open
# gate-id must match an id in core/config/gates.yaml.
set +e  # never propagate failure to caller — telemetry must not break gates
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
python3 "$CORE_ROOT/scripts/_gate_log.py" log "$@" 2>/dev/null
exit 0
