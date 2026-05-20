#!/usr/bin/env bash
# Layer B (pre-commit, default) + Layer D (--audit) — Phase 2.6 regression
# defense for the `<agent>/X/` path bug.
#
# After Phase 2.5.D moved agent dirs under `agents/`, every path reference
# of the form `<agent>/<subdir>/...` or `<agent-name>/<subdir>/...` in
# SKILL.md / rule / convention markdown is a bug: when the LLM substitutes
# `<agent>` with an actual agent name like "alpha", the result is
# `alpha/session/foo` at PROJECT_ROOT — but the canonical path is
# `agents/alpha/session/foo`. The L1 hook only gates Write/Edit, not Bash
# heredoc writes, so a bare prefix silently creates cruft at the wrong
# root (the 2026-05-19 bravo/ incident).
#
# Detection: any literal `<agent>/<word>/` or `<agent-name>/<word>/` that
# is NOT preceded by `agents/`. The legitimate form is `agents/<agent>/...`.
#
# Scope:
#   .claude/skills/**/*.md
#   .claude/rules/*.md
#   core/config/**/*.md
#   CLAUDE.md
#
# Exit codes:
#   0  no regressions found
#   1  regressions detected (lists offending file:line:match on stderr)
#
# Modes:
#   precommit (default) — checks added lines of staged files
#   --audit             — checks ENTIRE repo at HEAD
#
# Fail-open: if not in a git repo, exit 0 (nothing to gate).
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

MODE="precommit"
[ "${1:-}" = "--audit" ] && MODE="audit"

# Pattern: `<agent>/<word>` or `<agent-name>/<word>` NOT preceded by `agents/`.
# Use grep -P (Perl regex) for the negative lookbehind. If PCRE not available,
# fall back to a 2-stage filter (match all, drop ones with `agents/` prefix).

# Collect target files (resolve globs via git ls-files in audit mode, or
# from the staged set in precommit mode).
collect_audit_files() {
    git ls-files \
        '.claude/skills/*.md' \
        '.claude/rules/*.md' \
        'core/config/*.md' \
        'CLAUDE.md' 2>/dev/null
}

collect_staged_files() {
    git diff --cached --name-only --diff-filter=AM 2>/dev/null \
        | grep -E '\.md$' \
        | grep -E '^(\.claude/(skills|rules)/|core/config/|CLAUDE\.md$)'
}

found_any=0
report_hit() {
    local file="$1" line="$2" match="$3"
    echo "BARE_AGENT_PREFIX_REGRESSION: $file:$line:$match" >&2
    found_any=1
}

scan_text() {
    local file="$1" text="$2"
    # awk pass: match `<agent>/X` or `<agent-name>/X` where X starts with
    # alpha. Emit hits NOT preceded by `agents/`.
    awk -v F="$file" '
        {
            line = $0
            # Find each occurrence of <agent...>/word
            while (match(line, /<agent(-name)?>\/[a-zA-Z]/)) {
                pos = RSTART
                len = RLENGTH
                # Look at preceding 7 chars for "agents/" or "agents\"
                start = pos - 7
                if (start < 1) start = 1
                preceding = substr(line, start, pos - start)
                if (index(preceding, "agents/") == 0 && index(preceding, "agents\\") == 0) {
                    snippet = substr(line, pos, len + 12)
                    printf("%s:%d:%s\n", F, NR, snippet)
                }
                line = substr(line, pos + len)
            }
        }
    ' <<<"$text"
}

if [ "$MODE" = "audit" ]; then
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        out=$(scan_text "$f" "$(cat "$f")")
        if [ -n "$out" ]; then
            while IFS= read -r hit; do
                [ -n "$hit" ] || continue
                file="${hit%%:*}"
                rest="${hit#*:}"
                line="${rest%%:*}"
                match="${rest#*:}"
                report_hit "$file" "$line" "$match"
            done <<<"$out"
        fi
    done < <(collect_audit_files)
else
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        # Get added/modified lines in this commit
        added=$(git diff --cached -U0 -- "$f" \
            | awk '/^@@/ {match($0, /\+[0-9]+/); n=substr($0,RSTART+1,RLENGTH-1); next}
                   /^\+[^+]/ {print n":"substr($0,2); n++}
                   /^\+$/ {print n":"; n++}')
        [ -n "$added" ] || continue
        while IFS= read -r ln; do
            [ -n "$ln" ] || continue
            lineno="${ln%%:*}"
            content="${ln#*:}"
            out=$(scan_text "$f" "$content")
            if [ -n "$out" ]; then
                # scan_text reports NR which is 1 inside this slice; rewrite to real lineno
                while IFS= read -r hit; do
                    [ -n "$hit" ] || continue
                    match="${hit##*:}"
                    report_hit "$f" "$lineno" "$match"
                done <<<"$out"
            fi
        done <<<"$added"
    done < <(collect_staged_files)
fi

if [ $found_any -ne 0 ]; then
    cat >&2 <<'EOF'

Phase 2.6 regression: bare `<agent>/X/` path reference detected (no `agents/`
prefix). After Phase 2.5.D, agent dirs live under `agents/`. References in
SKILL.md/rule/convention markdown MUST use `agents/<agent>/X/` so the LLM's
literal substitution produces a valid path.

Fix: prepend `agents/` to each match. See:
  .claude/rules/path-resolution.md — "Agent Paths" section
  core/config/conventions/session-state.md — "Phase 2.6 — Two-Tier Session Layout"

To bypass for legitimate reasons (e.g., documenting the legacy form in a
comment), wrap the reference in code-quote backticks AND prefix with
`legacy:` or `old:` so the gate skips it (NOT YET IMPLEMENTED — file a
follow-up if you hit a legitimate need).
EOF
    exit 1
fi
exit 0
