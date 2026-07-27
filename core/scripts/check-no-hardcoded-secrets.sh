#!/usr/bin/env bash
# check-no-hardcoded-secrets.sh — Refuse commits whose staged content contains
# GitHub PAT, AWS access-key, or other token-shaped strings. Gate 8 in the
# pre-commit chain (core/githooks/pre-commit).
#
# Origin: 2026-05-21. The iteration-commit hook auto-bundles dirty files into
# commits; a runbook script carrying a 33-char identifier-truncation of a
# fine-grained PAT slipped through because:
#   - L1 path-resolution-hook only catches Write/Edit/MultiEdit, not Bash
#     mkdir+heredoc creation
#   - iteration-commit.sh filters by FILENAME (.env, *.key, *.pem,
#     credentials*, secrets*) — no CONTENT scan
#   - pre-commit had four structural gates and no secret-content gate
# This script closes the third gap.
#
# Patterns scanned (≥20 chars after the type-prefix to catch real tokens AND
# identifier-truncations of them — 20 chars is enough to uniquely identify a
# token but small enough to also block short prefix references):
#   - ghp_[A-Za-z0-9]{20,}          — classic PAT
#   - github_pat_[A-Za-z0-9_]{20,}  — fine-grained PAT
#   - gho_|ghu_|ghs_|ghr_           — OAuth (user-server / server-server / refresh)
#   - AKIA[0-9A-Z]{16}              — AWS access key ID
#
# Bypass mechanisms (least-broad to most-broad):
#   1. Per-line: append `# secret-scanner: skip` to the matching source line.
#   2. Per-file: add the path to ALLOWED_PATHS in allowed_path() below.
#   3. Per-commit: ALLOW_SECRETS_IN_COMMIT="<one-line justification>" git commit ...
#      (Audited to core/logs/secret-scanner-overrides.log.)
#
# Cross-references:
#   - core/githooks/pre-commit Gate 8 — wire-up site
#   - .claude/rules/no-auto-memory.md — secret-handling rules
#   - core/config/conventions/secrets.md — credentials convention
# domain-leak-exempt: this script literally contains token regex patterns
# (`ghp_`, `github_pat_`, etc.) as its detection contract — the strings are
# the script's reason for existing, not accidental domain bleed.

set -eu

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

# ─── Override path (audited) ──────────────────────────────────────────────
if [[ -n "${ALLOW_SECRETS_IN_COMMIT:-}" ]]; then
    mkdir -p "$REPO/core/logs"
    {
        echo "$(date +%Y-%m-%dT%H:%M:%S)" \
             "override=${ALLOW_SECRETS_IN_COMMIT}" \
             "user=${USER:-${USERNAME:-unknown}}" \
             "agent=${MIND_AGENT:-unbound}"
    } >> "$REPO/core/logs/secret-scanner-overrides.log"
    echo "[secret-scanner] OVERRIDDEN: ${ALLOW_SECRETS_IN_COMMIT}" >&2
    exit 0
fi

# ─── Patterns ─────────────────────────────────────────────────────────────
# Combined ERE alternation. Order doesn't matter; first match per line wins.
patterns='(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,}|ghr_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})'

# ─── Path allowlist ───────────────────────────────────────────────────────
# Files whose nature is to contain pattern strings (this script's regex
# definitions, test fixtures with intentional realistic-shaped tokens,
# gitignore patterns enumerating sensitive shapes).
allowed_path() {
    case "$1" in
        core/scripts/check-no-hardcoded-secrets.sh) return 0 ;;
        core/scripts/tests/fixtures/*)              return 0 ;;
        .gitignore)                                 return 0 ;;
    esac
    return 1
}

# ─── Scan staged content ──────────────────────────────────────────────────
# git diff --cached --numstat outputs `adds\tdels\tpath` per file. Binary
# files show `-\t-\tpath` — we skip them (regex on binary is noise). Deleted
# files don't appear with --diff-filter=ACM.
hits=0
hit_report=""

# Collect in-scope staged files (non-binary, not allowlisted) via numstat —
# the SAME filter as the prior per-file loop (identical file set): binary files
# (adds=="-") and allowed_path() files are excluded so the scan never touches
# them.
staged_files=()
while IFS=$'\t' read -r adds dels path; do
    [[ -z "$path" ]] && continue
    [[ "$adds" == "-" ]] && continue      # binary
    allowed_path "$path" && continue
    staged_files+=("$path")
done < <(git diff --cached --numstat --diff-filter=ACM)

if [[ ${#staged_files[@]} -gt 0 ]]; then
    # ONE `git grep --cached` over ALL in-scope files instead of a per-file
    # `git show ":$path" | grep` (one subprocess PER FILE — ~76s on the 436-file
    # v2.5.0 plant, g-115-2750). `--cached` greps the STAGED index content — the
    # same blob `git show ":$path"` returned — so this preserves the FULL staged
    # blob scan (every line, not diff-only): detection stays IDENTICAL (same
    # $patterns, same all-line coverage, verified git-grep-vs-grep ERE parity).
    # Output shape: `path:lineno:content` (git grep -n); no matches => exit 1.
    raw=$(git grep --cached -nE "$patterns" -- "${staged_files[@]}" 2>/dev/null || true)
    # Drop lines carrying the per-line skip marker (same override as before).
    raw=$(printf '%s\n' "$raw" | grep -vE 'secret-scanner:[[:space:]]*skip' || true)

    if [[ -n "$raw" ]]; then
        # Regroup git grep's flat `path:lineno:content` stream into the prior
        # report shape: one `path:` header per file, then `    lineno:content`
        # (truncated 200). hits = distinct files (git grep emits a file's matches
        # contiguously, so a path change marks a new file block).
        prev=""
        while IFS= read -r gl; do
            [[ -z "$gl" ]] && continue
            path="${gl%%:*}"           # path (before first colon)
            linecontent="${gl#*:}"     # lineno:content — same shape as old grep -n
            if [[ "$path" != "$prev" ]]; then
                [[ -n "$prev" ]] && hit_report+=$'\n'   # blank line ends prior block
                hit_report+="${path}:"$'\n'
                hits=$((hits + 1))
                prev="$path"
            fi
            hit_report+="    ${linecontent:0:200}"$'\n'
        done <<< "$raw"
        [[ -n "$prev" ]] && hit_report+=$'\n'           # trailing blank after last block
    fi
fi

# ─── Verdict ──────────────────────────────────────────────────────────────
if [[ $hits -gt 0 ]]; then
    cat >&2 <<EOF

[secret-scanner] BLOCKED — $hits file(s) contain token-shaped strings:

$hit_report
Bypass options (least-broad to most-broad):

  1. Per-line:   add  '# secret-scanner: skip'  to the matching line
  2. Per-file:   add the path to allowed_path() in
                 core/scripts/check-no-hardcoded-secrets.sh
  3. Per-commit: ALLOW_SECRETS_IN_COMMIT="<reason>" git commit ...

If these are REAL credentials:
  - Remove them from the file before committing.
  - Rotate them — they have already touched disk and may be in shell history.
  - Do NOT use bypass option 3 as a shortcut.

EOF
    exit 1
fi

exit 0
