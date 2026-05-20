#!/usr/bin/env bash
# skill-frontmatter-audit.sh — wrapper for skill-frontmatter-audit.py.
# Verifies every `.claude/skills/*/SKILL.md` has parseable front matter
# with a non-empty `name`. Defends the regression class caught 2026-05-11
# where an HTML comment above `---` made parse_front_matter return {} for
# 7 files silently.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/skill-frontmatter-audit.py" "$@"
