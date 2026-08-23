#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# skill-stdin-json-lint.sh
#
# Greps .claude/skills/ for unsafe double-quoted JSON piped to stdin-reading
# scripts (e.g., `echo "{...}" | bash core/scripts/aspirations-add-goal.sh ...`).
# Double quotes leave backticks and $(...) live — a silent shell-injection
# vector before the JSON ever reaches the Python handler.
#
# Convention: core/config/conventions/stdin-json-inputs.md
#
# Exit 0: no violations.
# Exit 1: one or more violations; each printed as file:line: <offending line>.
#
# Wired into PostToolUse[Edit|Write] in .claude/settings.json so drift
# surfaces to the author immediately.

set -u

SKILLS_DIR="${1:-.claude/skills}"

if [ ! -d "$SKILLS_DIR" ]; then
    # Directory missing (alternate layout) — exit 0 so the hook does not
    # block unrelated edits. Fail-open is intentional.
    exit 0
fi

# Match the unsafe shape: `echo "{...}" | ... core/scripts/...` or the
# `[` variant. Three structural requirements, all on the same line:
#   (1) `echo` at start-of-line, after whitespace, or after `:` —
#       rules out `echo` appearing inside another command's quoted
#       argument (e.g., `grep 'echo "..."'` where `echo` follows `'`);
#   (2) `echo` followed by a double-quoted string whose first char is
#       `{` or `[` — the JSON-literal marker inside double quotes, which
#       is the shell-injection site because double quotes leave
#       backticks, $(...), and $VAR live;
#   (3) a pipe `|` appears before `core/scripts/` on the same line —
#       the defining feature of JSON-to-stdin. Without it, the line is
#       some unrelated command that happens to reference both tokens.
#
# DO NOT rewrite `(\{|\[)` as a character class like `[\{\[]` or `[{[]`.
# POSIX treats `\` as literal inside `[...]`, so the class form silently
# includes `\` as a member and produces false positives on escaped
# anti-examples like `echo "\{evil}"` in prose. Alternation is the only
# form portable across grep implementations that matches exactly `{` or `[`.
PATTERN='(^|[[:space:]]|:)echo[[:space:]]+"(\{|\[).*[|].*core/scripts/'

violations=$(grep -rEn --include='*.md' "$PATTERN" "$SKILLS_DIR")

if [ -n "$violations" ]; then
    echo "skill-stdin-json-lint: unsafe echo-to-stdin pattern found" >&2
    echo "See core/config/conventions/stdin-json-inputs.md — use quoted heredoc (<<'EOF')" >&2
    echo "" >&2
    echo "$violations" >&2
    exit 1
fi

exit 0
