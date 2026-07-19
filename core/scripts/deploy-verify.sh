#!/usr/bin/env bash
# core/scripts/deploy-verify.sh - post-push CI/deploy verification (blocking).
#
# THE canonical probe for guard-119: after pushing to any repo with GitHub
# Actions, run this to watch every workflow run triggered by the pushed SHA
# to conclusion BEFORE claiming deployment success or closing the goal.
# Replaces the email-primary path (post-execution.md Step 2.5) as the primary
# signal; email remains secondary confirmation.
#
# Design constraints honored:
#   - guard-647: reads LIVE state every poll — no caching, no stored verdicts.
#   - rb-611: three-way verdict. "cannot tell" (timeout, no runs appeared,
#     API error) is exit 2 (unverified), never collapsed into pass or fail.
#   - domain-free (.claude/rules/domain-free-examples.md): repo/sha are
#     arguments or inferred from the git dir; nothing hardcoded.
#
# Usage:
#   deploy-verify.sh [--dir <git-dir>] [--repo owner/name] [--sha <sha>]
#                    [--timeout-mins N] [--poll-secs N] [--grace-secs N]
#
#   --dir defaults to CWD; --repo/--sha inferred from it when omitted.
#
# Output: single-line JSON on stdout:
#   {"status":"ok|failed|unverified|no_ci","repo":...,"sha":...,
#    "runs":[{"name":...,"conclusion":...,"url":...}],"detail":...}
# Exit: 0 = ok or no_ci (nothing to verify: repo has no active workflows)
#       1 = failed (>=1 run concluded failure/cancelled/timed_out)
#       2 = unverified (timeout before conclusion, no runs appeared for the
#           SHA within grace, or API error) — verify manually before claiming
#       3 = usage error
set -uo pipefail

DIR="."
REPO=""
SHA=""
TIMEOUT_MINS=15
POLL_SECS=20
GRACE_SECS=120

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)          DIR="${2:?}"; shift 2 ;;
        --repo)         REPO="${2:?}"; shift 2 ;;
        --sha)          SHA="${2:?}"; shift 2 ;;
        --timeout-mins) TIMEOUT_MINS="${2:?}"; shift 2 ;;
        --poll-secs)    POLL_SECS="${2:?}"; shift 2 ;;
        --grace-secs)   GRACE_SECS="${2:?}"; shift 2 ;;
        *) echo "{\"status\":\"unverified\",\"detail\":\"unknown arg: $1\"}"; exit 3 ;;
    esac
done

command -v gh >/dev/null 2>&1 || { echo '{"status":"unverified","detail":"gh CLI not found"}'; exit 2; }

if [ -z "$REPO" ]; then
    url=$(git -C "$DIR" remote get-url origin 2>/dev/null) || url=""
    REPO=$(printf '%s' "$url" | sed -E 's#^(git@|https://)([^/:]+)[:/]##; s#\.git$##')
    [ -z "$REPO" ] && { echo '{"status":"unverified","detail":"cannot infer repo from --dir (no origin remote)"}'; exit 2; }
fi
if [ -z "$SHA" ]; then
    SHA=$(git -C "$DIR" rev-parse HEAD 2>/dev/null) || SHA=""
    [ -z "$SHA" ] && { echo '{"status":"unverified","detail":"cannot infer sha from --dir"}'; exit 2; }
fi

# The runs API head_sha filter matches FULL 40-char shas only — a short sha
# silently returns zero runs and would read as eternal "unverified".
if [ "${#SHA}" -lt 40 ]; then
    full=$(gh api "repos/$REPO/commits/$SHA" -q .sha 2>/dev/null) || full=""
    [ -n "$full" ] && SHA="$full" || {
        echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"short sha could not be resolved to full sha\"}"
        exit 2
    }
fi

# No active workflows => nothing to verify (this is a real pass, not unverified:
# repos without CI are legitimately out of scope for deploy verification).
active_count=$(gh api "repos/$REPO/actions/workflows?per_page=100" \
    -q '[.workflows[] | select(.state=="active")] | length' 2>/dev/null) || active_count=""
if [ -z "$active_count" ]; then
    echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"workflow list API error\"}"
    exit 2
fi
if [ "$active_count" = "0" ]; then
    echo "{\"status\":\"no_ci\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"no active workflows\"}"
    exit 0
fi

deadline=$(( $(date +%s) + TIMEOUT_MINS * 60 ))
grace_end=$(( $(date +%s) + GRACE_SECS ))

while :; do
    runs_json=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=50" 2>/dev/null) || runs_json=""
    if [ -z "$runs_json" ]; then
        now=$(date +%s)
        [ "$now" -ge "$deadline" ] && { echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"runs API error until timeout\"}"; exit 2; }
        sleep "$POLL_SECS"; continue
    fi

    verdict=$(printf '%s' "$runs_json" | py -3 -c "
import json, sys
data = json.load(sys.stdin)
runs = data.get('workflow_runs', [])
out = [{'name': r.get('name'), 'status': r.get('status'),
        'conclusion': r.get('conclusion'), 'url': r.get('html_url')} for r in runs]
if not runs:
    print(json.dumps({'state': 'none', 'runs': out})); raise SystemExit
if any(r.get('status') != 'completed' for r in runs):
    print(json.dumps({'state': 'pending', 'runs': out})); raise SystemExit
bad = [r for r in out if r.get('conclusion') not in ('success', 'skipped', 'neutral')]
print(json.dumps({'state': 'failed' if bad else 'ok', 'runs': out, 'bad': bad}))
" 2>/dev/null) || verdict='{"state":"error"}'

    state=$(printf '%s' "$verdict" | py -3 -c "import json,sys; print(json.load(sys.stdin).get('state','error'))" 2>/dev/null || echo error)
    now=$(date +%s)

    case "$state" in
        ok)
            printf '%s' "$verdict" | py -3 -c "
import json, sys
v = json.load(sys.stdin)
print(json.dumps({'status': 'ok', 'repo': '$REPO', 'sha': '$SHA', 'runs': v['runs']}))"
            exit 0 ;;
        failed)
            printf '%s' "$verdict" | py -3 -c "
import json, sys
v = json.load(sys.stdin)
print(json.dumps({'status': 'failed', 'repo': '$REPO', 'sha': '$SHA', 'runs': v['runs'],
                  'detail': 'latest code is NOT deployed: fix before closing the goal'}))"
            exit 1 ;;
        none)
            if [ "$now" -ge "$grace_end" ]; then
                echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"no runs appeared for this sha within grace (${GRACE_SECS}s) — workflows may not trigger on this path; verify manually\"}"
                exit 2
            fi ;;
        pending) : ;;
        *)
            [ "$now" -ge "$deadline" ] && { echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"parse/API error until timeout\"}"; exit 2; } ;;
    esac

    [ "$now" -ge "$deadline" ] && { echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"runs still pending at timeout (${TIMEOUT_MINS}m) — re-run deploy-verify.sh or watch $REPO actions\"}"; exit 2; }
    echo "[deploy-verify] $REPO@${SHA:0:7}: $state — polling again in ${POLL_SECS}s" >&2
    sleep "$POLL_SECS"
done
