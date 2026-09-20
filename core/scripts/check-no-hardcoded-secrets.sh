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
#   --scan-untracked  Scan the GITIGNORED surface (agents/*/temp/) by name/size
#                 SHAPE only — never content. Audit mode, not a gate.
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
    --scan-head)      MODE="head" ;;
    --scan-untracked) MODE="untracked" ;;
    -h|--help)
        sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    "") : ;;
    *)
        echo "[secret-scanner] unknown argument: $1 (expected --scan-head, --scan-untracked, or no argument)" >&2
        exit 2 ;;
esac

# ─── --scan-untracked: the GITIGNORED surface (g-115-9947) ────────────────
# SELF-CONTAINED AND EXITS HERE, DELIBERATELY. Everything below this block
# reads file CONTENT (git grep over blobs). This mode MUST NOT, and keeping it
# above the patterns/override/report machinery is what makes that checkable by
# reading rather than by trusting: the branch runs `find -printf` and nothing
# else, so there is no code path from here to a byte of any scanned file.
#
# WHY IT EXISTS. `--scan-head` and the staged gate both scan the GIT-TRACKED
# surface. `agents/*/temp/` is gitignored in its entirety (.gitignore
# `agents/*/temp/*`, guard-872), so both are STRUCTURALLY BLIND to it and report
# clean forever regardless of what sits there. Independently,
# temp-drain-purge.sh Lane 0 EXEMPTS dotfiles from purge — correctly, since the
# exemption protects the tracked .gitkeep and the live cadence sentinels. Two
# correct mechanisms, and nothing in between looks at untracked
# credential-shaped residue (the guard-1802 narrow-predicate shape, at fleet
# scale).
#
# WHY NAME-SHAPE AND NOT SIZE. Measured (echo, cc-03, 2026-09-18): the two
# instances on the record are 342 B (.tok, bearer-token shape) and 20 B (.ea-pw,
# password shape) — and 20 B is ALSO the exact width of six benign ISO-8601
# cadence stamps on that same box. Any detector keyed on size alone must either
# miss .ea-pw or flag every cadence stamp on every box. The NAME is what
# separated them in a live population.
#
# WHY NOT A PURGE. The live dotfile population is working cadence state that
# running machinery reads; deleting it breaks cadence, and draining it writes
# scratch into the tree. This mode REPORTS. Disposition stays a human-or-agent
# decision per file.
#
# Exit 0 = no credential-shaped residue. Exit 1 = hits (audit mode, like
# --scan-head — never a commit gate). ALLOW_SECRETS_IN_COMMIT does not apply.
if [[ "$MODE" == "untracked" ]]; then
    # Name shapes that indicate a credential. Anchored on word-ish boundaries so
    # `.last-selector-path` does not match on "sel" and `.fe-ts` does not match
    # on "ts". Extend this list rather than loosening it: a loose pattern here
    # flags every cadence sentinel and the report stops being read.
    cred_name='(^|[.._-])(tok|tok2|token|pw|passwd|password|secret|secrets|cred|creds|credential|key|apikey|api_key|auth|bearer|session|cookie|jwt|pem|p12|pfx)([._-]|$)'
    hits=0
    scanned=0
    report=""
    while IFS='|' read -r path size mode mtime; do
        [[ -z "$path" ]] && continue
        scanned=$((scanned + 1))
        base="${path##*/}"
        # .gitkeep is TRACKED and is the Lane 0 exemption's own reason to exist.
        [[ "$base" == ".gitkeep" ]] && continue
        if printf '%s' "$base" | grep -qiE "$cred_name"; then
            if [[ "$size" -eq 0 ]]; then
                verdict="EMPTY-CRED-NAME"
                reason="credential-shaped NAME but ZERO bytes — holds nothing; safe to remove, and removing it stops it generating this finding forever"
            else
                verdict="CRED-SHAPED"
                reason="credential-shaped name at ${size}B — route through the secrets path (core/config/conventions/secrets.md); do NOT read, echo, or copy the value"
            fi
            hits=$((hits + 1))
            report+="  ${verdict} ${path}|${size}B|mode ${mode}|mtime ${mtime}"$'
'
            report+="      ${reason}"$'
'
        fi
    done < <(find agents/*/temp -maxdepth 1 -name '.*' -type f                   -printf '%p|%s|%m|%TY-%Tm-%Td
' 2>/dev/null | sort)

    # PER-DIR COVERAGE (g-115-9947, zeta/cc-02 leg 2026-09-20). The verdict below
    # is a PER-BOX reading of a READ-THROUGH-CACHED surface, and without this it
    # does not say so: `agents/*/temp/` materialises lazily under own-cloud, so a
    # box that has never pulled a peer's temp globs an absent or bare directory
    # and folds it into one fleet-shaped "CLEAN" sentence. Measured on cc-02:
    # foxtrot/temp ABSENT entirely, alpha+bravo+echo at exactly 1 entry (their
    # tracked .gitkeep), zeta at 79 — so 16 of 17 scanned files were one agent's,
    # and neither named instance on this goal's record was disposable from here.
    # That is the SAME composition defect this mode was built to close, one level
    # up: a confident clean over a population the instrument never had. Emitting
    # presence + count per dir makes "peer is clean" and "peer is not here"
    # distinguishable, which is what lets a fleet claim be the union of box runs.
    # Present dirs get a row each; absent ones are named together on one line.
    # Every dir is still NAMED — summarising is not hiding, and an unnamed
    # absence is the thing this block exists to prevent.
    coverage=""
    _absent=""
    for _agent_dir in agents/*/; do
        _a="$(basename "$_agent_dir")"
        if [[ -d "${_agent_dir}temp" ]]; then
            _n=$(find "${_agent_dir}temp" -maxdepth 1 -mindepth 1 2>/dev/null | wc -l)
            coverage+="    ${_a}: present, ${_n} entr$([[ "$_n" -eq 1 ]] && echo y || echo ies)"$'\n'
        else
            _absent+="${_absent:+, }${_a}"
        fi
    done
    if [[ -n "$_absent" ]]; then
        coverage+="    NOT SCANNED HERE (no temp dir on this box — says NOTHING about their residue): ${_absent}"$'\n'
    fi

    # ANTI-VACUITY. "no credential-shaped residue" is satisfied perfectly by a
    # scan that enumerated NOTHING — a moved temp root, a renamed agents dir, or
    # a find that errored all produce the identical clean exit. A population of
    # zero is a BROKEN SCAN here, not a clean one (guard-2298).
    if [[ "$scanned" -eq 0 ]]; then
        echo "[secret-scanner] --scan-untracked FOUND NO FILES AT ALL under agents/*/temp/." >&2
        echo "  That is a broken scan, not a clean one: every agent dir carries a tracked" >&2
        echo "  .gitkeep, so a live tree always enumerates at least one file. Check the" >&2
        echo "  temp root and AGENTS_PARENT_DIR before reading this as an all-clear." >&2
        exit 2
    fi

    if [[ "$hits" -eq 0 ]]; then
        echo "[secret-scanner] --scan-untracked CLEAN on THIS BOX: ${scanned} dotfile(s) under agents/*/temp/, none credential-shaped by name."
        echo "  Coverage (a fleet claim is the union of per-box runs, never one of them):"
        printf '%s' "$coverage"
        exit 0
    fi

    echo "[secret-scanner] --scan-untracked: ${hits} credential-shaped of ${scanned} dotfile(s) under agents/*/temp/ ON THIS BOX"
    echo "  Coverage (a fleet claim is the union of per-box runs, never one of them):"
    printf '%s' "$coverage"
    printf '%s' "$report"
    echo "  NOTE: metadata only — this mode reads no file content, by construction."
    echo "  NOTE: this box's view only. agents/<agent>/temp/ is nominally fleet-synced"
    echo "        (guard-3422) but the synced view is NOT uniform per box (measured"
    echo "        g-115-9947): a file present here can be absent on a peer and vice"
    echo "        versa, so a clean run HERE does not clear the fleet."
    exit 1
fi

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
