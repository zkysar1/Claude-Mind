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

# The `gh` executable. Production default is bare `gh` — unchanged. GH_BIN
# overrides when SET, even to "" (note `-` not `:-`), matching PROMOTE_GH_BIN
# (promote-to-upstream.sh) and pending-deploys.py::gh_bin(). Empty is a
# deliberate value: it simulates an unusable gh so the fail-safe branches below
# can be tested.
#
# EVERY gh call in this file routes through "$GH" — the bash ones here AND the
# python one inside the push-capability helper, which receives GH_BIN via the
# environment (never interpolated into the python source — guard-165).
# Routing ALL of them through one seam is the point (): the bug was
# that PATH interception reached the bash calls but NOT the python one, so a
# "hermetic" fixture made live GitHub API calls and a real 404 read as a ghost
# workflow. A half-covered seam reproduces exactly that class.
GH="${GH_BIN-gh}"

# `-f` first, then command -v. NOT `command -v` alone: GH_BIN may be an explicit
# PATH to a script rather than a name on $PATH, and command -v does not always
# resolve those. NOT `-x` either — on NTFS there is no exec bit, so `-x` reads
# false for files that demonstrably run (measured: -f YES / -e YES / -x no, and
# chmod 0755 changes nothing). `-x` would refuse a working gh.
# An empty GH_BIN fails both predicates, which is the intended "simulate an
# unusable gh" path.
{ [ -f "$GH" ] || command -v "$GH" >/dev/null 2>&1; } \
    || { echo '{"status":"unverified","detail":"gh CLI not found"}'; exit 2; }

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
    full=$("$GH" api "repos/$REPO/commits/$SHA" -q .sha 2>/dev/null) || full=""
    [ -n "$full" ] && SHA="$full" || {
        echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"short sha could not be resolved to full sha\"}"
        exit 2
    }
fi

# No active workflows => nothing to verify (this is a real pass, not unverified:
# repos without CI are legitimately out of scope for deploy verification).
active_count=$("$GH" api "repos/$REPO/actions/workflows?per_page=100" \
    -q '[.workflows[] | select(.state=="active")] | length' 2>/dev/null) || active_count=""
if [ -z "$active_count" ]; then
    echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"workflow list API error\"}"
    exit 2
fi
if [ "$active_count" = "0" ]; then
    echo "{\"status\":\"no_ci\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"no active workflows\"}"
    exit 0
fi

# Active workflows exist, but a git push only produces a deploy run when at
# least one of them is PUSH-triggered. Determine push-capability BEFORE waiting
# for runs — otherwise a repo whose active workflows are all schedule/dispatch-
# only, OR are GHOST registrations (the file was deleted from HEAD but the
# GitHub workflows API keeps reporting state=active; the file 404s and can never
# run), waits out the grace window and returns a false "unverified", leaving an
# un-clearable pending-deploys entry on every framework-repo push ().
# rb-611 fail-safe: return no_ci ONLY when EVERY active workflow is DEFINITIVELY
# non-push — a 200 whose parsed on: lacks push, or a 404 ghost. Any uncertain
# signal (non-404 fetch error, unparseable body, unexpected shape) is treated as
# push-capable → fall through to the poll → unverified, NEVER collapsed to no_ci.
push_capable=$("$GH" api "repos/$REPO/actions/workflows?per_page=100" \
    -q '.workflows[] | select(.state=="active") | .path' 2>/dev/null | REPO="$REPO" GH_BIN="$GH" BASH_BIN="${BASH_BIN:-$BASH}" py -3 -c "
import os, sys, json, base64, subprocess, yaml
repo = os.environ['REPO']
paths = [p.strip() for p in sys.stdin if p.strip()]
if not paths:
    print('yes'); raise SystemExit  # active_count>0 but no paths listed -> fail-safe

def triggers(path):
    # Returns a set of trigger names (empty set = 404 ghost), or None if uncertain.
    gh = os.environ.get('GH_BIN') or 'gh'
    argv = [gh, 'api', 'repos/%s/contents/%s' % (repo, path)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        # GH_BIN names a shell script (not directly executable on Windows:
        # CreateProcess -> WinError 193). Re-invoke through bash. A .cmd shim
        # is NOT the fix -- cmd.exe cuts the runs query at its '&'.
        p = subprocess.run([os.environ.get('BASH_BIN') or 'bash'] + argv,
                           capture_output=True, text=True)
    try:
        resp = json.loads(p.stdout or '')
    except Exception:
        return None  # unparseable body -> uncertain -> fail-safe
    if isinstance(resp, dict) and str(resp.get('status')) == '404':
        return set()  # ghost: the file was deleted, no trigger can fire
    if not (isinstance(resp, dict) and resp.get('encoding') == 'base64' and resp.get('content')):
        return None  # dir listing / >1MB blob / unexpected shape -> uncertain
    try:
        doc = yaml.safe_load(base64.b64decode(resp['content']).decode('utf-8', 'replace'))
    except Exception:
        return None
    on = None
    if isinstance(doc, dict):
        for k in doc:  # GitHub 'on:' parses as YAML-1.1 boolean True when unquoted
            if k is True or str(k).lower() == 'on':
                on = doc[k]; break
    t = set()
    if isinstance(on, str): t.add(on)
    elif isinstance(on, list): t.update(str(x) for x in on)
    elif isinstance(on, dict): t.update(str(k) for k in on.keys())
    return t

for path in paths:
    t = triggers(path)
    if t is None:
        print('yes'); raise SystemExit   # uncertain -> fail-safe push-capable
    if 'push' in t:
        print('yes'); raise SystemExit   # a push-triggered workflow exists
print('no')  # every active workflow is DEFINITIVELY non-push (schedule-only or ghost)
" 2>/dev/null) || push_capable="yes"  # helper crash -> fail-safe push-capable

if [ "$push_capable" = "no" ]; then
    echo "{\"status\":\"no_ci\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"active workflows exist but none are push-triggered (schedule/dispatch-only or ghost registrations with deleted files); a git push cannot produce a deploy run\"}"
    exit 0
fi

deadline=$(( $(date +%s) + TIMEOUT_MINS * 60 ))
grace_end=$(( $(date +%s) + GRACE_SECS ))

while :; do
    runs_json=$("$GH" api "repos/$REPO/actions/runs?head_sha=$SHA&per_page=50" 2>/dev/null) || runs_json=""
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
        'conclusion': r.get('conclusion'), 'url': r.get('html_url'),
        'event': r.get('event')} for r in runs]
# deploy-verify verifies the PUSH's deploy. Runs triggered by 'schedule' or
# 'workflow_dispatch' merely SHARE the pushed head_sha (e.g. a manually
# dispatched read-only diagnostic canary whose FAILURE is an intentional
# outage signal, not a deploy failure) — they were not caused by the push, so
# they must not count toward deploy classification (: the OC
# Instances canary probe-roblox-api.yml, dispatched at a freshly-pushed sha,
# flipped this verdict to 'failed' and filed a spurious HIGH Unblock every run).
deploy_out = [r for r in out if r.get('event') not in ('schedule', 'workflow_dispatch')]
if not deploy_out:
    # Either no runs at all, or only shared-sha diagnostics appeared so far.
    # 'none' routes to the grace/timeout window (the real push-deploy run may
    # not have registered yet, or the repo produced no deploy run for this push).
    print(json.dumps({'state': 'none', 'runs': out})); raise SystemExit
if any(r.get('status') != 'completed' for r in deploy_out):
    print(json.dumps({'state': 'pending', 'runs': out})); raise SystemExit
bad = [r for r in deploy_out if r.get('conclusion') not in ('success', 'skipped', 'neutral')]
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
