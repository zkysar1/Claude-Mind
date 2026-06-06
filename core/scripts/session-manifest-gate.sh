#!/usr/bin/env bash
# session-manifest-gate.sh — fail-closed completeness gate for
# core/config/session-manifest.yaml's `sync_tier` field.
#
# Origin: session-continuity redesign (2026-06-02). The manifest is the SINGLE
# SOURCE OF TRUTH for the own-cloud session-file sync disposition:
# owncloud_sync.py derives the machine-local denylist + the continuity pull-set
# from each entry's `sync_tier`. If an entry is missing `sync_tier` (or carries
# an invalid value), the runtime loader fails closed (treats the WHOLE session/
# tree as machine-local — the safe pre-redesign behavior) so NOTHING silently
# mis-syncs. This gate surfaces that defect at COMMIT time instead of letting a
# session file silently fall back to not-synced (knowledge-loss) or, worse, a
# liveness file silently sync (phantom-runner) once heuristics are involved.
#
# Checks (ALL must pass, else exit 1):
#   1. The manifest parses as YAML.
#   2. Every files[] entry has a `file` key.
#   3. Every files[] entry has `sync_tier` in {machine_local, continuity, ephemeral}.
#
# This is a GATE (fail-closed), not the advisory session-yaml-lint. There is no
# override flag: fix the manifest. With SSOT there is no second list to diverge
# from — the gate validates completeness + valid values only.
#
# Exit codes:
#   0  — every entry has a valid sync_tier
#   1  — one or more entries missing/invalid sync_tier, or manifest unparseable
#
# Usage:
#   bash core/scripts/session-manifest-gate.sh            # validate the repo manifest
#   bash core/scripts/session-manifest-gate.sh --file P   # validate an arbitrary manifest path (testing)
#   bash core/scripts/session-manifest-gate.sh --quiet    # suppress the OK summary line
#
# Wired from: core/githooks/pre-commit Gate 9 (2026-06-03), gated on
#   core/config/session-manifest.yaml being staged in the commit. (A Phase-6
#   recurring-audit wiring is planned but not yet in place.)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MANIFEST=""
QUIET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file)  [[ $# -ge 2 ]] || { echo "[session-manifest-gate] --file requires a path argument" >&2; exit 2; }; MANIFEST="$2"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        --help|-h) sed -n '2,33p' "$0"; exit 0 ;;
        *) echo "[session-manifest-gate] unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Resolve the default manifest path via _paths.sh PROJECT_ROOT (SSOT for the
# repo root). The gate validates a framework config file, not per-agent state,
# so it needs PROJECT_ROOT but NOT an agent binding.
if [[ -z "$MANIFEST" ]]; then
    # shellcheck source=/dev/null
    if source "$SCRIPT_DIR/_paths.sh" 2>/dev/null && [[ -n "${PROJECT_ROOT:-}" ]]; then
        MANIFEST="$PROJECT_ROOT/core/config/session-manifest.yaml"
    else
        # Fallback: _paths.sh unavailable. Derive root from this script's
        # location (core/scripts/ → repo root is two levels up).
        MANIFEST="$SCRIPT_DIR/../config/session-manifest.yaml"
    fi
fi

PY="${PY_CMD:-python3}"

MANIFEST="$MANIFEST" QUIET="$QUIET" "$PY" - <<'PYEOF'
import json
import os
import sys

try:
    import yaml
except ImportError:
    print("[session-manifest-gate] ERROR: PyYAML not available — cannot validate", file=sys.stderr)
    sys.exit(1)

manifest = os.environ["MANIFEST"]
quiet = os.environ.get("QUIET", "0") == "1"
VALID = {"machine_local", "continuity", "ephemeral"}

try:
    with open(manifest, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
except FileNotFoundError:
    print(f"[session-manifest-gate] ERROR: manifest not found: {manifest}", file=sys.stderr)
    sys.exit(1)
except yaml.YAMLError as e:
    line = ""
    if hasattr(e, "problem_mark") and e.problem_mark is not None:
        line = f" (line {e.problem_mark.line + 1})"
    print(f"[session-manifest-gate] ERROR: manifest does not parse{line}: "
          f"{str(e).splitlines()[0][:200]}", file=sys.stderr)
    sys.exit(1)

if not isinstance(data, dict) or not isinstance(data.get("files"), list):
    print("[session-manifest-gate] ERROR: manifest has no top-level files: list", file=sys.stderr)
    sys.exit(1)

files = data["files"]
missing_file_key = []
missing_tier = []
invalid_tier = []

for i, entry in enumerate(files):
    if not isinstance(entry, dict):
        missing_file_key.append(f"<entry #{i} is not a mapping>")
        continue
    name = entry.get("file")
    if not name:
        missing_file_key.append(f"<entry #{i} has no file: key>")
        continue
    tier = entry.get("sync_tier")
    if tier is None:
        missing_tier.append(name)
    elif tier not in VALID:
        invalid_tier.append(f"{name} (sync_tier={tier!r})")

problems = bool(missing_file_key or missing_tier or invalid_tier)

if problems:
    print("[session-manifest-gate] REFUSED — session-manifest.yaml has defects:", file=sys.stderr)
    if missing_file_key:
        print(f"  entries missing file: key  -> {missing_file_key}", file=sys.stderr)
    if missing_tier:
        print(f"  entries missing sync_tier  -> {missing_tier}", file=sys.stderr)
    if invalid_tier:
        print(f"  entries with invalid tier  -> {invalid_tier}", file=sys.stderr)
    print(f"  valid sync_tier values: {sorted(VALID)}", file=sys.stderr)
    print("  Every session file MUST be classified so owncloud_sync.py knows "
          "whether to sync it to S3. Add the field; do not bypass.", file=sys.stderr)
    sys.exit(1)

if not quiet:
    import collections
    tally = collections.Counter(e.get("sync_tier") for e in files)
    print(f"[session-manifest-gate] OK: {len(files)} entries, all classified "
          f"({dict(tally)})")
sys.exit(0)
PYEOF
