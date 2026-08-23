#!/usr/bin/env bash
# verify-check-registry.sh — thin wrapper over verify-check-registry.py.
#
# The registry holds the verify-learning evidence-check corpus, which moved out
# of SKILL.md on 2026-08-18 (): inline it sat past the 63,515 B
# skill-injection ceiling, so a verification skill could not see most of its own
# checks. See the .py module docstring for the record schema and the byte-exact
# round-trip property.
#
# The wrapper is not decoration. `/verify-learning` runs these commands FROM a
# Bash tool call, where a bare `python3` hits the Microsoft Store stub on
# Windows boxes (CLAUDE.md "Python Invocation", rb-370 / guard-335) — the skill
# would fail on exactly the machines nobody tests it on. `python3` inside a .sh
# is the sanctioned form.
#
#   bash core/scripts/verify-check-registry.sh sections
#   bash core/scripts/verify-check-registry.sh show --section 4T
#   bash core/scripts/verify-check-registry.sh show --section 4T --offset 327
#   bash core/scripts/verify-check-registry.sh verify
#   bash core/scripts/verify-check-registry.sh add --section PU2 --check '...'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

exec python3 "$CORE_ROOT/scripts/verify-check-registry.py" "$@"
