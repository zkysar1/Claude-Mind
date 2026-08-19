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
#      STAGED MODE ONLY — see --scan-head below.
#
# Modes:
#   (default)     Scan STAGED content. This is Gate 8 of the pre-commit chain.
#   --scan-head   Scan COMMITTED content at HEAD. Audit mode, not a gate.
#
# WHY --scan-head EXISTS (g-306-105, 2026-08-01). The default mode is
# DIFF-SCOPED: it reads the staged index and never inspects what is already
# committed. So any credential committed BEFORE this script landed (2026-05-21)
# is invisible to it permanently — the control cannot see the residue that
# predates it. Measured on a sibling deployment: an account root key sat in
# plaintext in two tracked files in current HEAD, first committed one month
# before the scanner existed, and was found by accident while pre-flighting an
# unrelated commit. This is the conditionally-active-mechanism class: the gate
# works exactly as designed and still guarantees nothing about the tree it
# guards. --scan-head closes the retroactive half; a recurring audit invokes it
# so the residue surfaces on a schedule instead of by luck.
#
# ALLOW_SECRETS_IN_COMMIT deliberately does NOT apply to --scan-head. That
# variable is a per-COMMIT bypass; honoring it in audit mode would let a stale
# exported value silence the whole audit, which is the failure this mode exists
# to prevent. The per-line and per-file bypasses DO apply in both modes.
#
# NOTE ON REMEDIATION: finding a secret in HEAD means it is also in history, and
# history keeps the value regardless of any scrub. ROTATION is the remediation;
# removing the file is hygiene, not containment.
#
# Cross-references:
#   - core/githooks/pre-commit Gate 8 — wire-up site (staged mode)
#   - .claude/rules/no-auto-memory.md — secret-handling rules
#   - core/config/conventions/secrets.md — credentials convention
# domain-leak-exempt: this script literally contains token regex patterns
# (`ghp_`, `github_pat_`, etc.) as its detection contract — the strings are
# the script's reason for existing, not accidental domain bleed.

set -eu

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

# ─── Mode ─────────────────────────────────────────────────────────────────
# Parsed BEFORE the override block on purpose: the override is commit-scoped
# and must not be able to short-circuit the audit mode (see header).
MODE="staged"
case "${1:-}" in
    --scan-head) MODE="head" ;;
    -h|--help)
        sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    "") : ;;
    *)
        echo "[secret-scanner] unknown argument: $1 (expected --scan-head or no argument)" >&2
        exit 2 ;;
esac

# ─── Override path (audited) ──────────────────────────────────────────────
if [[ -n "${ALLOW_SECRETS_IN_COMMIT:-}" && "$MODE" == "staged" ]]; then
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

# ─── Scan ─────────────────────────────────────────────────────────────────
# Two modes, one report. Each branch below produces `raw` in the SAME
# `path:lineno:content` shape; everything after the branch is shared, which is
# what keeps the two modes from drifting into two different report formats.
hits=0
hit_report=""
raw=""

if [[ "$MODE" == "head" ]]; then
    # ─── Scan committed content at HEAD ───────────────────────────────────
    # `git grep <rev>` emits `HEAD:path:lineno:content` — one extra leading
    # field vs `--cached`. Strip it so both modes hand the SAME
    # `path:lineno:content` shape to the shared regroup loop below; that
    # shared shape is what keeps the two modes from drifting into two
    # different report formats.
    # -I skips BINARY files, matching the scope staged mode ALREADY declares at
    # its numstat pre-filter below ("regex on binary is noise", ~L147). Audit
    # mode scanning them was the anomaly, not a feature: `git grep` emits a
    # DIFFERENT line shape for a binary match — `Binary file HEAD:blob.bin
    # matches` — which the `sed 's/^HEAD://'` + `${gl%%:*}` extraction below
    # resolves to the literal path "Binary file HEAD". That named a file nobody
    # could open, and it was suppressable by NEITHER bypass: allowed_path() is
    # keyed on real repo paths, and a binary carries no line to hold a
    # `# secret-scanner: skip` marker. One false positive would therefore wedge
    # the g-115-4398 recurring HEAD audit red with no remedy available.
    # Measured (g-115-4400): -I drops the binary line and leaves the text match
    # and its correct path untouched — it costs no detection on anything staged
    # mode would have scanned. Deliberately NOT `-a` (treat binaries as text):
    # that keeps the unusable report and answers the scope question differently
    # in the two modes, which is the drift this comment block exists to prevent.
    raw=$(git grep -I -nE "$patterns" HEAD 2>/dev/null || true)
    raw=$(printf '%s\n' "$raw" | sed 's/^HEAD://')
    # The allowlist is applied to RESULTS here, not to inputs as in staged
    # mode. Staged mode can pre-filter because numstat hands it a short,
    # already-enumerated file list; a HEAD scan has no such list short of
    # enumerating every tracked file, so filtering after the grep is both
    # cheaper and exactly equivalent.
    filtered=""
    while IFS= read -r gl; do
        [[ -z "$gl" ]] && continue
        allowed_path "${gl%%:*}" && continue
        filtered+="$gl"$'\n'
    done <<< "$raw"
    raw="$filtered"
else

# ─── Scan staged content (Gate 8) ─────────────────────────────────────────
# git diff --cached --numstat outputs `adds\tdels\tpath` per file. Binary
# files show `-\t-\tpath` — we skip them (regex on binary is noise). Deleted
# files don't appear with --diff-filter=ACM.
#
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
fi

fi   # end mode branch

# ─── Shared: per-line skip marker + regroup ───────────────────────────────
# Both modes reach here with the SAME `path:lineno:content` stream, so the
# per-line bypass and the report format are defined exactly once.
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

# ─── Verdict ──────────────────────────────────────────────────────────────
if [[ $hits -gt 0 && "$MODE" == "head" ]]; then
    cat >&2 <<EOF

[secret-scanner] HEAD AUDIT — $hits committed file(s) contain token-shaped strings:

$hit_report
These are ALREADY COMMITTED. The pre-commit gate cannot have caught them: it is
diff-scoped, so anything committed before it landed is outside what it can see.

ROTATE FIRST. The value is in git history and stays there whether or not the
file is scrubbed, so removing the file is hygiene, not containment. Treat every
hit as live until the credential has been rotated at its issuer.

Then, if a hit is a FALSE POSITIVE (a fixture, a doc example, a regex):
  1. Per-line:   add  '# secret-scanner: skip'  to the matching line
  2. Per-file:   add the path to allowed_path() in
                 core/scripts/check-no-hardcoded-secrets.sh

ALLOW_SECRETS_IN_COMMIT does NOT apply here — it is a per-commit bypass and is
deliberately ignored in audit mode.

EOF
elif [[ $hits -gt 0 ]]; then
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
fi

# Non-zero on ANY hit, in BOTH modes. Kept outside the branch on purpose: with
# the exit inside the staged arm only, audit mode printed a full findings report
# and still exited 0, so the recurring audit that consumes this exit code could
# never fire on the residue it exists to find (caught by the positive control
# during g-306-105 — a report nobody acts on is the same silence as no report).
if [[ $hits -gt 0 ]]; then
    exit 1
fi

exit 0
