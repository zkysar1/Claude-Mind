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

while IFS=$'\t' read -r adds dels path; do
    [[ -z "$path" ]] && continue
    [[ "$adds" == "-" ]] && continue      # binary
    allowed_path "$path" && continue

    # Scan the STAGED blob, not the working tree (working tree may have
    # unstaged edits that aren't part of the commit).
    matches=$(git show ":$path" 2>/dev/null | grep -nE "$patterns" 2>/dev/null || true)
    [[ -z "$matches" ]] && continue

    # Drop lines carrying the per-line skip marker.
    matches=$(echo "$matches" | grep -vE 'secret-scanner:[[:space:]]*skip' || true)
    [[ -z "$matches" ]] && continue

    hits=$((hits + 1))
    hit_report+="${path}:"$'\n'
    # Indent for readability; truncate each match line at 200 chars so we
    # don't echo full long tokens to the user's terminal.
    while IFS= read -r line; do
        hit_report+="    ${line:0:200}"$'\n'
    done <<< "$matches"
    hit_report+=$'\n'
done < <(git diff --cached --numstat --diff-filter=ACM)

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
