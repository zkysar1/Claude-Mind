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

# ── Domain platform-build hook (Pattern B world-script slot) ────────────────
# guard-119 names THIS script as the canonical post-push probe, and it read
# GitHub Actions only. Measured 2026-08-06 (): on one commit the
# Actions run concluded SUCCESS while the hosting platform's build of the SAME
# commit FAILED and main was blocked — and this script returned
# {"status":"ok"} exit 0. Following the guardrail exactly closed the goal clean
# on a blocked pipeline. Two independent environments run the same suite; a
# green in one is not evidence about the other.
#
# Core stays domain-free (.claude/rules/domain-free-examples.md): no vendor or
# product name appears in this file. The DOMAIN supplies the probe, exactly as
# iteration-close.sh:1838 does for its pipeline gate. No hook => byte-identical
# behavior to before this seam existed, which is what makes it safe for the
# many repos and worlds it does not apply to (pinned by
# test_no_hook_verdict_is_unchanged).
#
# Hook contract — argv: --repo <owner/name> --sha <40-char>. stdout: one JSON
# line {"state":"absent|ok|failed|pending|unknown","detail":...}.
#   absent  = looked, and this repo has no platform app  -> CI verdict stands
#   ok      = platform built this commit successfully    -> CI verdict stands
#   failed  = platform build failed                      -> failed (exit 1)
#   pending = still building                             -> unverified (exit 2)
#   unknown = could not determine (no CLI, no creds, API error) -> unverified
# `absent` and `unknown` are deliberately DIFFERENT: "no app here" is a genuine
# no-op, "I could not see" is not. Collapsing them would rebuild the false
# clean this seam exists to remove.
#
# rb-611 fail-safe: anything not recognized — a crashed hook, an unparseable
# line, a state this version does not know — is treated as `unknown`, never as
# a pass. The defect class here is a false ok, so an unreadable probe must
# never be able to produce one.
# WORLD_DIR must be RESOLVED here, not inherited. This script is invoked
# directly (guard-119's canonical probe, a bare `deploy-verify.sh --dir <repo>`)
# and never had a reason to source _paths.sh before, so WORLD_DIR is simply
# absent from the environment at every real call site. Reading `${WORLD_DIR:-}`
# alone made the hook path resolve to "/scripts/..." — never a file, always
# `absent`, so the seam was inert in production while its unit tests (which set
# WORLD_DIR explicitly) all passed. Measured 2026-08-06 end-to-end against the
# real red commit: it returned {"status":"ok"} exit 0 with the hook in place.
# guard-1943 — a green suite certifies the FUNCTION, never the WIRING; the only
# environment where it failed was the only environment where it actually runs.
# Pinned by test_hook_found_without_world_dir_in_env, which deliberately unsets
# WORLD_DIR to replicate the production shape (guard-920).
if [ -z "${WORLD_DIR:-}" ] && [ -z "${DEPLOY_VERIFY_PLATFORM_HOOK:-}" ]; then
    _dv_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # shellcheck source=/dev/null
    [ -f "$_dv_dir/_paths.sh" ] && . "$_dv_dir/_paths.sh" 2>/dev/null || true
    # _paths.sh exports WORLD_PATH; WORLD_DIR is its alias in older callers.
    WORLD_DIR="${WORLD_DIR:-${WORLD_PATH:-}}"
fi
PLATFORM_HOOK="${DEPLOY_VERIFY_PLATFORM_HOOK-${WORLD_DIR:-}/scripts/deploy-verify-platform.sh}"
platform_state=""
platform_detail=""

probe_platform() {
    platform_state="absent"
    platform_detail=""
    [ -n "$PLATFORM_HOOK" ] && [ -f "$PLATFORM_HOOK" ] || return 0

    # Called DIRECTLY, never as $(probe_platform): this publishes through
    # globals, and command substitution forks a subshell that discards them
    # (guard-2226).
    local raw parsed rc errfile
    errfile="$(mktemp 2>/dev/null)" || errfile=""

    # BOUND THE HOOK. It reaches an external control plane, and deploy-verify.sh
    # is guard-119's canonical post-push probe invoked from post-execution — so an
    # unbounded hang here hangs the loop. --timeout-mins covers only the polling
    # loop further down; it never covered this call.
    # Generous and env-overridable rather than a short fixed cap (guard-918): a
    # slow-but-working probe must not be downgraded to `unknown`. Expiry maps to
    # `unknown`, which is already the fail-safe state. `timeout` is coreutils and
    # may be absent, so degrade to the unbounded call rather than failing.
    rc=0
    if command -v timeout >/dev/null 2>&1; then
        raw=$(timeout "${DEPLOY_VERIFY_HOOK_TIMEOUT:-120}" bash "$PLATFORM_HOOK" \
                  --repo "$REPO" --sha "$SHA" 2>"${errfile:-/dev/null}") || rc=$?
    else
        raw=$(bash "$PLATFORM_HOOK" --repo "$REPO" --sha "$SHA" \
                  2>"${errfile:-/dev/null}") || rc=$?
    fi
    # Parse stdout REGARDLESS of rc. The previous `|| raw=""` discarded a valid
    # verdict whenever the hook exited non-zero — and a hook author has every
    # reason to do that, since this very seam maps failed->exit 1. Measured: a
    # hook emitting {"state":"failed"} + exit 1 was reported as "no parseable
    # verdict" (exit 2), which both lost the verdict and named the wrong cause.
    # rc is consulted only when stdout carried nothing.
    #
    # THE NO-STDOUT CASE IS HANDLED HERE, BEFORE py. It cannot be handled after:
    # the parser below has a try/except that ALWAYS emits "unknown<TAB>...", so
    # `parsed` is never empty and an rc/stderr branch placed after it is dead
    # code. That is not hypothetical — it was the first cut of this very fix,
    # and test_hanging_hook_times_out_and_is_unverified caught it.
    if [ -z "$raw" ]; then
        platform_state="unknown"
        if [ "$rc" = "124" ]; then
            platform_detail="platform hook timed out after ${DEPLOY_VERIFY_HOOK_TIMEOUT:-120}s"
        else
            platform_detail="platform hook produced no output (exit $rc)"
        fi
        # Fold the hook's own stderr into the verdict. Discarding it made every
        # hook failure look identical and forced a by-hand re-run to learn
        # anything (verify-before-assuming rule 4: a silenced command is zero
        # signals). Captured to a temp file, never /dev/stderr — that path does
        # not resolve when stderr is a pipe (guard-1883).
        if [ -n "$errfile" ] && [ -s "$errfile" ]; then
            platform_detail="$platform_detail: $(tr '\n\t' '  ' < "$errfile" | tail -c 300)"
        fi
        [ -n "$errfile" ] && rm -f "$errfile"
        return 0
    fi
    # One py call returns "state<TAB>detail"; guard-165 — the value goes
    # through the environment and the source is single-quoted, never
    # interpolated.
    parsed=$(RAW="$raw" py -3 -c '
import json, os, sys
raw = os.environ.get("RAW", "")
line = ""
for cand in raw.splitlines():
    if cand.strip().startswith("{"):
        line = cand.strip()
try:
    d = json.loads(line)
    st = str(d.get("state") or "").strip().lower()
    if st not in ("absent", "ok", "failed", "pending", "unknown"):
        st = "unknown"
    detail = str(d.get("detail") or "")
except Exception:
    st, detail = "unknown", "platform hook emitted no parseable verdict"
sys.stdout.write(st + "\t" + detail.replace("\t", " ").replace("\n", " "))
' 2>/dev/null) || parsed=""

    if [ -z "$parsed" ]; then
        platform_state="unknown"
        if [ "$rc" = "124" ]; then
            platform_detail="platform hook timed out after ${DEPLOY_VERIFY_HOOK_TIMEOUT:-120}s"
        else
            platform_detail="platform hook emitted no parseable verdict (exit $rc)"
            # Fold the hook's own stderr into the verdict. Discarding it made
            # every hook failure look identical and forced a by-hand re-run to
            # learn anything (verify-before-assuming rule 4: a silenced command
            # is zero signals). Captured to a temp file, never /dev/stderr —
            # that path does not resolve when stderr is a pipe (guard-1883).
            if [ -n "$errfile" ] && [ -s "$errfile" ]; then
                platform_detail="$platform_detail: $(tr '\n\t' '  ' < "$errfile" | tail -c 300)"
            fi
        fi
        [ -n "$errfile" ] && rm -f "$errfile"
        return 0
    fi
    [ -n "$errfile" ] && rm -f "$errfile"
    platform_state="${parsed%%$'\t'*}"
    platform_detail="${parsed#*$'\t'}"
}

# Applies the platform verdict on top of a NON-failing CI verdict, then exits.
# Wired at three sites: the CI-green path and both no_ci paths (a repo with no
# push CI is where reading Actions alone is most misleading, not least).
# $1 = the complete CI JSON payload that would have been emitted alone.
emit_with_platform() {
    local ci_json="$1"
    probe_platform
    case "$platform_state" in
        absent|ok)
            printf '%s\n' "$ci_json"
            exit 0 ;;
        failed)
            # The CI half is described from the payload's own status, not
            # hardcoded: this emitter is wired to the no_ci paths too, where
            # asserting "CI passed" would be false. A verdict that misstates
            # its own evidence is the defect class this seam exists to remove.
            CI_JSON="$ci_json" PDETAIL="$platform_detail" py -3 -c '
import json, os
d = json.loads(os.environ["CI_JSON"])
ci = "CI passed" if d.get("status") == "ok" else "this repo has no push CI"
d["status"] = "failed"
d["platform_state"] = "failed"
d["detail"] = ("platform build FAILED for this sha (" + ci + "): "
               + os.environ.get("PDETAIL", "")
               + " -- the deploy pipeline is blocked; fix before closing the goal")
print(json.dumps(d))'
            exit 1 ;;
        *)
            CI_JSON="$ci_json" PSTATE="$platform_state" PDETAIL="$platform_detail" py -3 -c '
import json, os
d = json.loads(os.environ["CI_JSON"])
st = os.environ.get("PSTATE", "unknown")
ci = "CI passed" if d.get("status") == "ok" else "this repo has no push CI"
d["status"] = "unverified"
d["platform_state"] = st
d["detail"] = (ci + " but the platform build is " + st + ": "
               + os.environ.get("PDETAIL", "")
               + " -- verify the platform job before claiming success")
print(json.dumps(d))'
            exit 2 ;;
    esac
}

if [ -z "$REPO" ]; then
    url=$(git -C "$DIR" remote get-url origin 2>/dev/null) || url=""
    REPO=$(printf '%s' "$url" | sed -E 's#^(git@|https://)([^/:]+)[:/]##; s#\.git$##')
    [ -z "$REPO" ] && { echo '{"status":"unverified","detail":"cannot infer repo from --dir (no origin remote)"}'; exit 2; }
fi
if [ -z "$SHA" ]; then
    SHA=$(git -C "$DIR" rev-parse HEAD 2>/dev/null) || SHA=""
    [ -z "$SHA" ] && { echo '{"status":"unverified","detail":"cannot infer sha from --dir"}'; exit 2; }

    # ── STALE-SUBJECT GATE ( third manifestation) ──────────────────
    # Inferring the sha from the LOCAL checkout answers a question nobody asked
    # when the checkout is behind. Measured on Ayoai-Environment-Server right
    # after a PR merge: HEAD d87b915 vs origin/main 377fa15 (14 commits behind),
    # and the bare form emitted {"status":"ok"} for d87b915. Every field in that
    # JSON — repo, sha, status — was factually correct; only the SUBJECT was
    # never the commit anyone asked about, so the usual false-green tell (an
    # implausible value) is absent by construction. A merge performed through
    # the GitHub API is exactly the case that leaves the checkout stale, which
    # is the fleet's highest-volume caller.
    #
    # WHY FAIL-CLOSED. This script's own rb-611 rule (see the `absent`/`unknown`
    # comment above) is that an unreadable probe must never produce a pass,
    # because the defect class here is a false ok. A stale subject is precisely
    # an unreadable probe wearing a green.
    #
    # MEASURED BEFORE CHOOSING (2026-08-08, cc-07, 56 repos under /opt/GitHub):
    #   42 of 56 (75%) would have been verified at the WRONG sha
    #    7 genuinely current — the only calls this gate newly rejects is zero of them
    #    7 on unpushed feature branches (no remote ref; see below)
    #    0 detached HEAD, 0 without an origin remote
    # The goal asked what fail-closed would NEWLY reject; two of the four
    # concerns it named are empty here, and the rejected population is the
    # defective one.
    #
    # ls-remote, NOT `@{u}`. The cheaper option is "resolve the tracked remote
    # head" — but the tracking ref is itself a local cache that goes stale
    # without a fetch, and 2 of the 16 repos that looked current by that measure
    # were NOT. Using it would reproduce the same staleness bug one level down.
    # ls-remote queries the remote directly and mutates nothing, so it is both
    # more accurate than `@{u}` and cheaper than the fetch option.
    #
    # Passing --sha explicitly bypasses this entirely: the caller has named its
    # own subject and the local checkout is irrelevant. That is the invocation
    # guard-119 now prescribes.
    # REFUSE ONLY ON PROVEN DIVERGENCE. The three ls-remote outcomes are NOT
    # equivalent and collapsing them was measured to be a real regression:
    #   rc!=0            the remote could not be read      -> UNKNOWN, warn, proceed
    #   rc=0, empty      the branch has no remote head     -> UNKNOWN, warn, proceed
    #   rc=0, differs    POSITIVE evidence of staleness    -> refuse
    # An unreadable remote is a statement about the network, not about this
    # checkout, so refusing there would make every offline call fail and — the
    # thing that actually caught it — would preempt the PLATFORM HOOK, which is
    # consulted later and is the whole verification path for deploys that are
    # not GitHub Actions. A first draft of this gate refused on all three and
    # broke 8 of the 12 platform-hook tests while 3 of the remaining 4 went
    # VACUOUSLY green: they assert `unverified`, which the over-eager gate also
    # returned, so the suite stayed 4/12 green while exercising none of the hook.
    #
    # The measured defect is the PROVEN case (42 of 56 repos), and refusing
    # exactly that costs nothing this measurement can find: of the 7 genuinely
    # current repos, zero are newly rejected.
    #
    # rc is captured on its own line, never through `| cut` — a pipe reports the
    # LAST command's status, so `ls-remote ... | cut` returns cut's 0 even when
    # the remote is unreachable, silently turning "unknown" into "empty" and
    # re-collapsing the distinction this block exists to draw (guard-1150).
    _dv_branch=$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null) || _dv_branch=""
    if [ -n "$_dv_branch" ] && [ "$_dv_branch" != "HEAD" ]; then
        _dv_ls=$(git -C "$DIR" ls-remote origin "refs/heads/$_dv_branch" 2>/dev/null)
        _dv_rc=$?
        if [ "$_dv_rc" -ne 0 ]; then
            echo "deploy-verify: WARNING — could not read origin to confirm the subject commit; verifying local HEAD $SHA, which may not be what was pushed (g-115-3273). Pass --sha to be certain." >&2
        elif [ -z "$_dv_ls" ]; then
            echo "deploy-verify: WARNING — branch '$_dv_branch' has no remote head (never pushed?); verifying local HEAD $SHA, for which no CI run can exist (g-115-3273). Pass --sha to be certain." >&2
        else
            _dv_remote_sha=${_dv_ls%%[!0-9a-f]*}
            if [ -n "$_dv_remote_sha" ] && [ "$_dv_remote_sha" != "$SHA" ]; then
                echo "{\"status\":\"unverified\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"STALE SUBJECT: local $_dv_branch is at $SHA but origin/$_dv_branch is at $_dv_remote_sha — verifying the local sha would report on a commit nobody asked about (g-115-3273). Re-run as: deploy-verify.sh --dir $DIR --sha $_dv_remote_sha\",\"local_sha\":\"$SHA\",\"remote_sha\":\"$_dv_remote_sha\"}"
                exit 2
            fi
        fi
    fi
    # Detached HEAD is deliberately NOT gated: it is the CI-runner shape, where
    # the checkout IS the pushed sha by construction, and there is no branch to
    # compare against. Measured 0 detached checkouts across 56 repos here, so
    # gating it would add a rejection path with no observed population.
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
    # A repo with no Actions can still be built by a hosting platform — that is
    # the case where reading Actions alone is MOST misleading, so the hook runs
    # here too rather than only on the CI-green path.
    emit_with_platform "{\"status\":\"no_ci\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"no active workflows\"}"
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
    emit_with_platform "{\"status\":\"no_ci\",\"repo\":\"$REPO\",\"sha\":\"$SHA\",\"detail\":\"active workflows exist but none are push-triggered (schedule/dispatch-only or ghost registrations with deleted files); a git push cannot produce a deploy run\"}"
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
            # Captured into a var rather than printed directly, so the platform
            # hook can override a CI-green verdict. Values reach python through
            # the environment (guard-165) — the previous form interpolated
            # $REPO/$SHA into the source text, and this site had to be rewritten
            # to route through the emitter regardless.
            ci_ok_json=$(printf '%s' "$verdict" | REPO="$REPO" SHA="$SHA" py -3 -c '
import json, os, sys
v = json.load(sys.stdin)
print(json.dumps({"status": "ok", "repo": os.environ["REPO"],
                  "sha": os.environ["SHA"], "runs": v["runs"]}))')
            emit_with_platform "$ci_ok_json"
            ;;
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
