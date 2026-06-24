#!/usr/bin/env bash
# domain-leak-exempt: boto3 / S3 / DynamoDB here are FUNCTIONAL own-cloud-backend
# references — the literal AWS SDK module checked via `import boto3` and its
# backing services — not pedagogical domain examples. Mirrors the exempt
# core/scripts/owncloud_backend.py and core/scripts/owncloud_sync.py.
# check-prerequisites.sh — verify the Mind framework's runtime prerequisites.
#
# Created 2026-05-17 (Phase 2.1 packaging cleanup). Replaces the
# previous fail-piecemeal behavior where a fresh install would crash four
# scripts deep with a cryptic ModuleNotFoundError. This script runs ONCE at
# /start time, collects every missing prerequisite, and prints a single
# friendly error block with copy-pasteable fix commands.
#
# Exit codes:
#   0 — all required prerequisites satisfied (warnings ok)
#   2 — one or more required prerequisites missing
#
# What's required vs warned:
#   REQUIRED: Python 3.8+, PyYAML (`yaml` importable), bash 4+
#   REQUIRED (own-cloud only): boto3 — checked ONLY when STORAGE_BACKEND=own-cloud
#     (the S3/DynamoDB backend imports it lazily; local installs never need it).
#     Missing boto3 on an own-cloud machine otherwise surfaces only as a dead
#     daemon (g-115-1334, 2026-06-04 machine-2 bring-up).
#   WARNING-ONLY: git (loop runs without git, but iteration audit trail
#     + pre-commit gates + post-commit daemon recycle are disabled),
#     psutil (agent-watchdog degrades gracefully)
#
# Usage:
#   bash core/scripts/check-prerequisites.sh
#   bash core/scripts/check-prerequisites.sh --quiet    # only print on failure

set -uo pipefail

QUIET=false
[[ "${1:-}" == "--quiet" ]] && QUIET=true

REQUIRED_MISSING=()
WARNINGS=()
DETAILS=()

# Pick the right Python launcher for the platform.
PY=""
if command -v py >/dev/null 2>&1; then
    if py -3 --version >/dev/null 2>&1; then
        PY="py -3"
    fi
fi
if [[ -z "$PY" ]] && command -v python3 >/dev/null 2>&1; then
    PY="python3"
fi
if [[ -z "$PY" ]] && command -v python >/dev/null 2>&1; then
    # On some Windows installs only `python` is on PATH
    if python --version 2>&1 | grep -qE 'Python 3\.'; then
        PY="python"
    fi
fi

# --- Python presence + version ---
if [[ -z "$PY" ]]; then
    REQUIRED_MISSING+=("Python 3.8+ (not found on PATH)")
    DETAILS+=("  Install from https://www.python.org/downloads/ (check 'Add to PATH' on Windows)")
else
    # Extract version. eval is safe here — PY is one of three known tokens.
    PY_VERSION=$(eval "$PY --version" 2>&1 | head -1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [[ -z "$PY_MAJOR" || -z "$PY_MINOR" ]] \
       || (( PY_MAJOR < 3 )) \
       || (( PY_MAJOR == 3 && PY_MINOR < 8 )); then
        REQUIRED_MISSING+=("Python 3.8+ (found: $PY_VERSION via '$PY')")
        DETAILS+=("  Upgrade Python from https://www.python.org/downloads/")
    fi
fi

# --- PyYAML (required) ---
if [[ -n "$PY" ]]; then
    if ! eval "$PY -c 'import yaml' " 2>/dev/null; then
        REQUIRED_MISSING+=("PyYAML (Python package 'yaml' not importable)")
        DETAILS+=("  Install: $PY -m pip install -r requirements.txt")
        DETAILS+=("  Or:      $PY -m pip install pyyaml")
    fi
fi

# --- bash version (we need 4+ for associative arrays and modern features) ---
BASH_MAJOR=${BASH_VERSINFO[0]:-0}
if (( BASH_MAJOR < 4 )); then
    REQUIRED_MISSING+=("bash 4+ (found: $BASH_VERSION)")
    DETAILS+=("  On Windows, install Git for Windows (includes bash 5.x): https://git-scm.com/download/win")
fi

# --- git (optional, warning only) ---
if ! command -v git >/dev/null 2>&1; then
    WARNINGS+=("git not installed — iteration audit trail, pre-commit gates, post-commit daemon recycle disabled")
    DETAILS+=("  Optional install: https://git-scm.com/downloads")
fi

# --- psutil (optional, warning only) ---
if [[ -n "$PY" ]]; then
    if ! eval "$PY -c 'import psutil' " 2>/dev/null; then
        WARNINGS+=("psutil not installed — agent-watchdog process inspection degrades gracefully")
        DETAILS+=("  Optional install: $PY -m pip install psutil")
    fi
fi

# --- boto3 (REQUIRED only when the own-cloud backend is configured) ---
# boto3 is the AWS SDK that the own-cloud S3/DynamoDB backend imports lazily
# (core/scripts/owncloud_backend.py). The DEFAULT local backend never needs it,
# so gate the check on STORAGE_BACKEND: read the live env first, else parse the
# one line from .env.local (the canonical location — loaded elsewhere via
# `set -a; source .env.local`). We grep a single non-secret line rather than
# sourcing the whole file, so no credentials enter this script's environment.
STORAGE_BACKEND_VAL="${STORAGE_BACKEND:-}"
if [[ -z "$STORAGE_BACKEND_VAL" && -f .env.local ]]; then
    STORAGE_BACKEND_VAL="$(grep -E '^[[:space:]]*STORAGE_BACKEND[[:space:]]*=' .env.local 2>/dev/null \
        | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
fi
STORAGE_BACKEND_VAL="$(printf '%s' "$STORAGE_BACKEND_VAL" | tr '[:upper:]' '[:lower:]')"
if [[ "$STORAGE_BACKEND_VAL" == "own-cloud" && -n "$PY" ]]; then
    if ! eval "$PY -c 'import boto3' " 2>/dev/null; then
        REQUIRED_MISSING+=("boto3 (required for STORAGE_BACKEND=own-cloud — the S3/DynamoDB backend imports it)")
        DETAILS+=("  Install: $PY -m pip install -r mind_api/requirements-owncloud.txt")
        DETAILS+=("  Or:      $PY -m pip install boto3")
    fi
fi

# --- Report ---
if (( ${#REQUIRED_MISSING[@]} > 0 )); then
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  Mind framework prerequisites NOT MET"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    echo "REQUIRED (these block startup):"
    for item in "${REQUIRED_MISSING[@]}"; do
        echo "  ✗ $item"
    done
    if (( ${#WARNINGS[@]} > 0 )); then
        echo ""
        echo "OPTIONAL (will work without, but with degraded capability):"
        for item in "${WARNINGS[@]}"; do
            echo "  ! $item"
        done
    fi
    echo ""
    echo "How to fix:"
    for item in "${DETAILS[@]}"; do
        echo "$item"
    done
    echo ""
    echo "Once fixed, re-run /start <agent-name>."
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    exit 2
fi

# Success path
if [[ "$QUIET" != "true" ]]; then
    echo "[check-prerequisites] OK: Python $PY_VERSION (via '$PY'), PyYAML importable, bash $BASH_MAJOR.x"
    if (( ${#WARNINGS[@]} > 0 )); then
        for item in "${WARNINGS[@]}"; do
            echo "[check-prerequisites] WARN: $item"
        done
    fi
fi

exit 0
