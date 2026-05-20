#!/usr/bin/env bash
# validate-agent-name.sh — Lightweight name validator for /start A1 fail-fast.
#
# Mirrors the validation in init-agent.sh:33-46 so the customer learns of an
# invalid name BEFORE they complete path/program setup, not after. Same rules:
#   - Lowercase kebab-case: ^[a-z][a-z0-9-]*$
#   - Not in the reserved-name list (core, meta, world, node_modules, etc.)
#
# Usage:
#   bash core/scripts/validate-agent-name.sh <agent-name>
#
# Exit codes:
#   0 — valid
#   2 — invalid format (uppercase, underscore, leading digit, special chars)
#   3 — reserved name (conflicts with framework directory)
#
# Single source of truth caveat: init-agent.sh:33-46 re-runs equivalent
# checks at C0 (defense in depth — A1 may have been bypassed somehow). The
# two MUST stay in sync; if you change the regex or reserved list here,
# change it in init-agent.sh too.
#
# Origin: 2026-05-19 zeta /start audit, finding #3 — "Agent-name validation
# fires LATE (init-agent.sh:33-46), not at A1."

set -uo pipefail

NAME="${1:-}"

if [ -z "$NAME" ]; then
    echo "ERROR: Agent name is required." >&2
    echo "Usage: bash core/scripts/validate-agent-name.sh <agent-name>" >&2
    exit 2
fi

# Reserved names that conflict with project structure.
# MUST stay in sync with init-agent.sh:34.
RESERVED_NAMES="core meta world node_modules .git .claude .github"
for reserved in $RESERVED_NAMES; do
    if [ "$NAME" = "$reserved" ]; then
        echo "ERROR: '$NAME' is a reserved name (conflicts with framework directory)." >&2
        echo "  Reserved: $RESERVED_NAMES" >&2
        echo "  Pick a different agent name (e.g., teacher, alpha, pokemon-research)." >&2
        exit 3
    fi
done

# Validate kebab-case: must start with a letter, then letters/digits/hyphens.
# MUST stay in sync with init-agent.sh:43.
if ! echo "$NAME" | grep -qE '^[a-z][a-z0-9-]*$'; then
    echo "ERROR: Agent name '$NAME' is not valid." >&2
    echo "  Must be lowercase kebab-case: start with a letter, then letters/digits/hyphens." >&2
    echo "  Valid examples:   teacher, alpha, deep-scout, pokemon-research" >&2
    echo "  Invalid examples: MyAgent (uppercase), my_agent (underscore), 1agent (leading digit), agent! (special char)" >&2
    exit 2
fi

exit 0
