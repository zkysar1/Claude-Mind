#!/usr/bin/env bash
# Audit every non-null defer_reason across world + agent queues and classify
# each as (a) genuine / (b) ambiguous-or-stale / (c) narrative-only.
# Read-only — never mutates. See audit-deferred-defers.py docs for the
# classifier and JSON schema.
#
# Lane B of the reclaim duty (.claude/rules/reclaim-routed-work.md). This
# wrapper existed nowhere until 2026-07-29: the .py had NO bash wrapper and
# NO call site in any loop phase, so the auditor was structurally unrunnable
# through the standard path and never fired.
#
# Flags: --output json|human  --report PATH  --stale-days N
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/audit-deferred-defers.py" "$@"
