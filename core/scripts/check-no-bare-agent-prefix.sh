#!/usr/bin/env bash
# Layer B (pre-commit, default) + Layer D (--audit) — regression defense for
# UNRESOLVABLE PATH PREFIXES in skill/rule/convention markdown. Two classes,
# same family: a path prefix an LLM copies verbatim that does not resolve at
# runtime. (Filename names class 1 only, for historical reasons — class 2 was
# added by . Both are reported with distinct tags and diagnostics.)
#
#   Class 1  BARE_AGENT_PREFIX_REGRESSION — `<agent>/X/` with no `agents/`
#   Class 2  BARE_WORLD_PREFIX_REGRESSION — `bash world/X` / `Bash: meta/X`
#            in an executable line (world/ and meta/ are EXTERNAL paths)
#
# NOTE ON BACKLOG: the class-2 defect existed at 97 sites / 13 files when it
# was measured (). That goal fixed the 23 framework-owned sites and
# shipped this gate with 74 sites / 7 files still outstanding, all in DOMAIN
# forged skills — tracked for drain by .
# Pre-commit mode scans ADDED LINES OF STAGED FILES ONLY, so that backlog does
# NOT block commits; only NEW introductions are refused. `--audit` reports the
# whole backlog and so exits 1 until  drains it. Do not "fix" that
# non-zero audit exit by weakening the detector.
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
found_agent=0
found_path=0
report_hit() {
    local file="$1" line="$2" match="$3"
    case "$match" in
        PATH:*)
            echo "BARE_WORLD_PREFIX_REGRESSION: $file:$line:${match#PATH:}" >&2
            found_path=1
            ;;
        *)
            echo "BARE_AGENT_PREFIX_REGRESSION: $file:$line:$match" >&2
            found_agent=1
            ;;
    esac
    found_any=1
}

scan_text() {
    local file="$1" text="$2"
    # awk pass: match `<agent>/X` or `<agent-name>/X` where X starts with
    # alpha. Emit hits NOT preceded by `agents/`.
    awk -v F="$file" '
        {
            orig = $0
            line = orig
            # --- Class 1: bare `<agent>/X` (Phase 2.6 agent-dir prefix) ---
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
            # --- Class 2: bare `world/` or `meta/` in an executable Bash line ---
            # Emits a PATH: marker so report_hit can pick the right diagnostic.
            # Only the FIRST match per line is reported: one hit is enough to
            # make the author fix the line, and lines carry at most one call.
            if (match(orig, /(bash|Bash:)[ \t]+(world|meta)\//)) {
                p = RSTART
                before = substr(orig, 1, p - 1)
                # Backtick-parity: an ODD count before the match means the match
                # sits INSIDE a `code span` -> prose/doc reference, not executable.
                # gsub (not split) because split("") returns 0, which would make
                # the count -1 and wrongly skip every match starting at column 1
                # -- i.e. the single most common form.
                tmp = before; nbt = gsub(/`/, "`", tmp)
                # `command:` -> a predicate.py precondition. The bare form is
                # REQUIRED there: predicate.py rewrites `bash world/X` to an
                # absolute WORLD_DIR path itself (command_succeeds and
                # metric_threshold both), and its ALLOWED_COMMAND_PREFIXES
                # would REJECT the $WORLD_PATH-resolved form.
                if (nbt % 2 == 0 && index(before, "command:") == 0) {
                    printf("%s:%d:PATH:%s\n", F, NR, substr(orig, p, RLENGTH + 24))
                }
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
    # Batched precommit scan (). Collect in-scope staged files via the
    # SAME filter as before (collect_staged_files → identical file set), then run
    # ONE `git diff --cached -U0` over all of them and scan in a SINGLE awk pass.
    # The prior form spawned an awk subprocess PER ADDED LINE (scan_text inside a
    # per-line loop) — ~87k awk spawns on the v2.5.0 436-file ZDS plant (~300s,
    # exceeded the 10-min hook budget, forced a documented --no-verify, rb-4251).
    # The awk below tracks the current file from `+++ b/` headers and the line
    # number from `@@` hunks (same n-tracking as the old added-parse awk), then
    # applies the EXACT match logic copied verbatim from scan_text — same
    # `<agent(-name)?>/[a-zA-Z]` scan, same `agents/`-prefix exclusion, same
    # +12-char snippet, same `/^\+[^+]/` + `/^\+$/` added-line selection. Identical
    # detection, one process instead of tens of thousands.
    mapfile -t staged_files < <(collect_staged_files)
    if [ "${#staged_files[@]}" -gt 0 ]; then
        hits=$(git diff --cached -U0 -- "${staged_files[@]}" \
            | awk '
                /^\+\+\+ b\// { f = substr($0, 7); next }
                /^@@/ { match($0, /\+[0-9]+/); n = substr($0, RSTART+1, RLENGTH-1); next }
                /^\+[^+]/ {
                    orig = substr($0, 2)
                    line = orig
                    while (match(line, /<agent(-name)?>\/[a-zA-Z]/)) {
                        pos = RSTART; len = RLENGTH
                        start = pos - 7; if (start < 1) start = 1
                        preceding = substr(line, start, pos - start)
                        if (index(preceding, "agents/") == 0 && index(preceding, "agents\\") == 0) {
                            snippet = substr(line, pos, len + 12)
                            printf("%s:%d:%s\n", f, n, snippet)
                        }
                        line = substr(line, pos + len)
                    }
                    # Class 2 -- same logic as scan_text (see there for the
                    # backtick-parity and `command:` rationale). Kept in sync
                    # by hand, as the class-1 block above already is.
                    if (match(orig, /(bash|Bash:)[ \t]+(world|meta)\//)) {
                        p = RSTART
                        before = substr(orig, 1, p - 1)
                        tmp = before; nbt = gsub(/`/, "`", tmp)
                        if (nbt % 2 == 0 && index(before, "command:") == 0) {
                            printf("%s:%d:PATH:%s\n", f, n, substr(orig, p, RLENGTH + 24))
                        }
                    }
                    n++
                    next
                }
                /^\+$/ { n++; next }
            ')
        if [ -n "$hits" ]; then
            while IFS= read -r hit; do
                [ -n "$hit" ] || continue
                file="${hit%%:*}"
                rest="${hit#*:}"
                line="${rest%%:*}"
                match="${rest#*:}"
                report_hit "$file" "$line" "$match"
            done <<<"$hits"
        fi
    fi
fi

if [ $found_path -ne 0 ]; then
    cat >&2 <<'EOF'

Bare `world/` (or `meta/`) prefix in an executable Bash line. These are
EXTERNAL paths (WORLD_PATH / META_PATH in <agent>/local-paths.conf), NOT
subdirectories of PROJECT_ROOT. The PreToolUse[Bash] hooks do NOT rewrite
path arguments (bash-agent-inject only prepends env exports; the path hook
only denies cruft), so `bash world/scripts/X.sh` resolves relative to
PROJECT_ROOT -- where no world/ dir exists -- and dies rc=127 "No such file
or directory". Measured live: bare form rc=127 vs resolved form rc=2+usage.

Fix -- the canonical form (guard-666):
  source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/X.sh" ARGS

Legitimate forms this gate deliberately does NOT flag:
  * a reference inside `backticks` (prose, or a documented anti-pattern).
    To document this anti-pattern ON PURPOSE, use INLINE backticks — a FENCED
    (```) block is treated as executable and WILL be flagged, because a fenced
    block is exactly what gets copied and run.
  * a `command:` field in a precondition -- predicate.py rewrites `bash
    world/X` to an absolute path itself, and its ALLOWED_COMMAND_PREFIXES
    would REJECT the $WORLD_PATH-resolved form there.

See .claude/rules/path-resolution.md -- "Bash hooks do NOT rewrite ..."
EOF
fi

if [ $found_agent -ne 0 ]; then
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
fi

# Exit on found_any, NOT on either class alone: a path-class-only hit must
# still fail the gate.
if [ $found_any -ne 0 ]; then
    exit 1
fi
exit 0
