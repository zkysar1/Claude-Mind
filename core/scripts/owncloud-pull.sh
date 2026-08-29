#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#
# owncloud-pull.sh — pull an agent's continuity-tier session files from S3 to
# local, freshness-aware, via the daemon.
#
# The read-side complement of owncloud-flush.sh. Called by the /start IDLE
# branch BEFORE boot does its raw Read of handoff.yaml, so an agent moved to a
# new machine resumes from the last machine's flushed handoff / working-memory /
# execution-diary / ... instead of a stale (or absent) local copy. The endpoint
# (owncloud_sync.pull_continuity) NEVER clobbers a local file with unpushed local
# writes (the manifest baseline gates every overwrite).
#
# Why a daemon endpoint and NOT a CLI sweep: only the daemon holds the scoped
# MIND_AWS_* creds + governed roots in its env.
#
# Agent resolution: --agent <name> overrides; otherwise MIND_AGENT.
# Output: a one-line summary on stdout. Exit 0 on success / local-backend no-op;
# exit 1 on a daemon error or a per-file pull error.
#
# --all-agents (FLEET MODE, ): repeat the pull for EVERY agent in the
# fleet instead of one. Motivation: this script is --agent-scoped, so an
# alpha-bound session never refreshes PEER agents' session files. On cc-04 that
# left bravo's pending-questions.yaml 18 days stale and echo/foxtrot/zeta's
# ABSENT entirely — so /open-questions showed the user a fraction of the fleet
# backlog (user-surfaced 2026-07-25). pending-questions.yaml is sync_tier
# continuity in core/config/session-manifest.yaml, so the existing per-agent
# endpoint already fetches it; fleet mode just stops scoping the sweep to one
# agent. This is the freshness half of the fix — pending-questions-read.sh
# --all-agents is the read half. Fixing either alone still leaves the user blind.
#
# Fleet roster comes from team-state agent_status (the live fleet roster per
# coordination.md), NOT from local-paths.conf enumeration: on a given box only
# the RESIDENT agent has a conf (cc-04 has one for alpha only), so conf-based
# enumeration would silently degrade fleet mode back to single-agent — the exact
# defect being fixed. Falls back to on-disk agent dirs if team-state is
# unreadable. Per-agent failures are isolated: one bad agent does not abort the
# rest, and the exit code still reports that something failed.
#
# --only <a.yaml[,b.jsonl]> narrows the pull to those continuity files. Names are
# matched against the continuity set, so --only can never widen the pull. Use it
# for a targeted refresh when you want ONE file rather than the ~17-object
# continuity set.
#
# A NAME THAT IS NOT IN THE CONTINUITY SET MATCHES NOTHING, and this wrapper now
# says so and exits 2 (). It used to exit 0 after printing
# `pulled=0 in_sync=0 scanned=0 s3_absent=0 local_ahead=0 errors=0`. `scanned`
# DOES distinguish the two cases -- but the three signals a caller reads to
# decide "did anything change" are pulled, errors and rc, and those are
# IDENTICAL to a matched scope that was already in sync. Measured 2026-08-28
# (alpha, cc-08, own-cloud), same box, one turn:
#     --only handoff.yaml      -> pulled=0 in_sync=1 scanned=1 errors=0 rc=0
#     --only no-such-file.yaml -> pulled=0 in_sync=0 scanned=0 errors=0 rc=0
# So a caller that force-freshes a file and diffs it concludes UNCHANGED off a
# scan that never opened it -- the  incident (foxtrot, 2026-08-11).
# The discriminator was never missing: pull_continuity has always set
# `requested_missing` ("surfaced, not fatal") and the endpoint spreads it via
# **stats, so it was on the wire the whole time and THIS RENDERER DROPPED IT.
# guard-2018 (an absent field can BE the zero); guard-3489 (emit the coverage
# count beside the result count, and refuse to exit 0 when it is zero).
#
# AND NO, --only WILL NOT BE WIDENED TO ARBITRARY PATHS. That was considered
# under  and DECLINED, because the capability already exists with a
# better contract: `backend-cat.sh head <path>` reports the authoritative
# version/size and the local-mirror drift for ANY governed path (absolute, or
# a world//meta/ virtual prefix), and `--exit-on-drift` turns that verdict into
# an exit code -- verified on world/conventions/pre-execution.md, cc-08
# 2026-08-28. Widening --only would also break the invariant pull_continuity's
# docstring states as a safety property: "names are matched against the
# continuity set, so `only` can never widen the pull beyond it or reach a
# non-continuity file". Reach for backend-cat.sh, not for a wider --only.
#
# --with-temp () opts INTO the temp/ working-doc sweep, which is OFF by
# default. It is off because temp/ is NOT continuity-tier — session-manifest.yaml
# does not list it — yet sweeping it made this command's cost scale with scratch
# population instead of with the continuity set, and pushed it past its own
# RT_CURL_TIMEOUT ceiling exactly when it had work to do. Measured cc-04
# (uname -r 6.8.0-136-generic, own-cloud, live fleet, 2026-08-02): default-on
# 164.6s / scanned=1590 / pulled=125, of which just 2 were continuity files and
# 123 were temp probe leftovers from closed goals. Prior readings 1054/95.4s
# (cc-03) and 904/59s (cc-04) show the ceiling re-eroding as temp/ grows, which
# is why raising RT_CURL_TIMEOUT was rejected as the fix. Cost of the default:
# a machine-moved agent does not auto-resume its temp/ docs; pass --with-temp to
# fetch them (nothing is deleted — the objects stay in S3).
#
# Usage: bash core/scripts/owncloud-pull.sh [--agent <name>]
#        bash core/scripts/owncloud-pull.sh --all-agents
#        bash core/scripts/owncloud-pull.sh --all-agents --only pending-questions.yaml
#        bash core/scripts/owncloud-pull.sh --agent <name> --with-temp
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

AGENT=""
ALL_AGENTS=""
ONLY=""
WITH_TEMP=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent) AGENT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --all-agents) ALL_AGENTS=1; shift;;
        --only) ONLY="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --with-temp) WITH_TEMP=1; shift;;
        *) echo "[owncloud-pull] unknown arg: $1" >&2; exit 2;;
    esac
done
if [ -n "$ALL_AGENTS" ] && [ -n "$AGENT" ]; then
    echo "[owncloud-pull] ERROR: --all-agents and --agent are mutually exclusive" >&2
    exit 2
fi

# --- Local-backend continuity pull () ------------------------------
# This script IS the session-start continuity pull: /start's IDLE branch calls it
# so a session resumes with current world/meta state. Under own-cloud that means
# fetching from S3. Under STORAGE_BACKEND=local there is no S3 authority — git is
# the sync mechanism — and the daemon endpoint below correctly reports a no-op,
# so the continuity pull did NOTHING on a local-backend deployment.
#
# The consequence was not theoretical: the ONLY caller of iteration-push.sh is
# iteration-close.sh, i.e. the AUTONOMOUS LOOP, so assistant- and reader-mode
# sessions never fetched at all. Measured 2026-07-29 — a laptop assistant session
# was 47 commits behind origin while actively reading and writing those files,
# because the agent had moved to another box and been pushing there for hours.
# own-cloud deployments never had this gap; it was purely a missing local-backend
# implementation behind an existing hook.
#
# Handled HERE rather than in /start because Edit/Write on .claude/skills/start/*
# is in the settings.json DENY bucket (guard-103): making the existing call site
# backend-complete needs no /start change at all, and keeps ONE continuity-pull
# entry point for both backends.
#
# --no-push is load-bearing: starting a session must never publish state as a
# side effect. Delegating to iteration-push.sh reuses its hardened fetch+integrate
# (FETCH_HEAD throttle, dirty-tree refusal, merge --abort on true conflict,
# fail-soft on auth/network) instead of re-deriving that logic here.
_ocp_storage_backend() {
    local v="${STORAGE_BACKEND:-}"
    if [[ -z "$v" && -f "$PROJECT_ROOT/.env.local" ]]; then
        v="$(grep -E '^[[:space:]]*STORAGE_BACKEND[[:space:]]*=' "$PROJECT_ROOT/.env.local" 2>/dev/null \
             | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
    fi
    # Default to "local" when nothing is set anywhere, MATCHING the daemon's own
    # default: mind_api/src/endpoints/admin.py resolves
    # os.environ.get("STORAGE_BACKEND", "local").strip().lower(). Without this
    # the two disagree exactly where it matters — a local-backend deployment
    # that relies on the default (no env var, no .env.local entry) would have
    # the daemon report backend=local while this helper returned "", so the
    # continuity pull would silently skip on the very deployments the fix
    # targets. Verified at the emitter, not assumed.
    [ -z "$v" ] && v="local"
    printf '%s' "$v" | tr '[:upper:]' '[:lower:]'
}
#
# TWO GATES BELOW, both added by the bravo half of this converged change. Each
# blocks a case where the git work is wrong rather than merely unnecessary.
if [ "$(_ocp_storage_backend)" = "local" ]; then
    # GATE 1 — --only means TARGETED REFRESH, not continuity pull.
    # /open-questions calls `--all-agents --only pending-questions.yaml`, and
    # --only exists precisely because a full sweep is "far too slow for an
    # interactive dashboard" (this file's header). Attaching a repo-wide
    # fetch+merge to that path reintroduces the cost the flag was built to
    # avoid — and the integrate can COMMIT self-namespace churn to self-heal a
    # dirty tree (), so a read-oriented user command could produce a
    # commit. Found by /fresh-eyes-code reviewing this change.
    if [ -n "$ONLY" ]; then
        echo "[owncloud-pull] backend=local: --only is a targeted refresh — skipping git integrate (use a bare /start for the continuity pull)"
        exit 0
    fi
    # GATE 2 — reader mode must stay side-effect-free, and --no-push alone is
    # NOT sufficient to make it so: the integrate path can COMMIT self-namespace
    # churn BEFORE the --no-push seam is ever reached (). So reader
    # does not integrate at all; it stays as current as the checkout it opened
    # on, which is the right trade against writing to disk in a read-only
    # session. /start sets the mode (SKILL.md:634) before calling this script
    # (SKILL.md:668), so this reads the TARGET mode, not a stale one — verified,
    # because a reversed order would make every local /start read the "reader"
    # disk-default and skip the integrate silently.
    _ocp_mode="$(bash "$CORE_ROOT/scripts/session-mode-get.sh" 2>/dev/null || true)"
    _ocp_mode="$(printf '%s' "$_ocp_mode" | tr -d '[:space:]')"
    if [ "$_ocp_mode" = "reader" ]; then
        echo "[owncloud-pull] backend=local: reader mode — skipping git integrate (side-effect-free contract)"
        exit 0
    fi
    echo "[owncloud-pull] backend=local — continuity pull via git fetch+integrate (no push)"
    # Fail-soft by contract: iteration-push.sh always exits 0 without --strict, so
    # a network/auth/dirty-tree hiccup degrades to "resume from local state"
    # exactly as the own-cloud path's WARN branch does. Never block session start.
    bash "$_RUNTIME_SELF/iteration-push.sh" --repo "$PROJECT_ROOT" --no-push \
        || echo "[owncloud-pull] WARN: git continuity pull returned non-zero — resuming from local state (may be stale)" >&2
    exit 0
fi
if [ -z "$ALL_AGENTS" ]; then
    [ -z "$AGENT" ] && AGENT="${MIND_AGENT:-}"
    if [ -z "$AGENT" ]; then
        echo "[owncloud-pull] ERROR: no agent (pass --agent <name>, --all-agents, or set MIND_AGENT)" >&2
        exit 2
    fi
fi

source "$CORE_ROOT/scripts/_runtime.sh"

# Fleet roster: team-state agent_status keys (live roster), falling back to
# on-disk agent dirs. See the header for why local-paths.conf enumeration is
# WRONG here. The fallback glob is routed through _paths.sh's agents_root() so
# it tracks an AGENTS_PARENT_DIR rename.
_fleet_roster() {
    local roster=""
    # $PYLAUNCH UNQUOTED (the launcher is the two-word "py -3" on Windows; a
    # quoted call looks for a single command literally named "py -3" and dies
    # "command not found"). Guarded on [ -n "$PYLAUNCH" ] rather than defaulting
    # to bare python3 — rt_python_launcher deliberately refuses that fallback
    # ( v3), and on Windows bare python3 hits the Store stub. Matches
    # the two pre-existing call sites below (L158/L200).  F1.
    if [ -n "$PYLAUNCH" ]; then
        roster="$(bash "$CORE_ROOT/scripts/team-state-read.sh" --json 2>/dev/null \
            | $PYLAUNCH -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
print("\n".join(sorted((d.get("agent_status") or {}).keys())))' 2>/dev/null || true)"
    fi
    if [ -n "$roster" ]; then
        printf '%s\n' "$roster"
        return 0
    fi
    echo "[owncloud-pull] WARN: team-state roster unavailable — falling back to on-disk agent dirs" >&2
    # shellcheck disable=SC1091
    source "$CORE_ROOT/scripts/_paths.sh" 2>/dev/null || true
    local root
    root="$(agents_root 2>/dev/null || true)"
    [ -z "$root" ] && return 1
    local d
    for d in "$root"/*/; do
        [ -d "$d" ] || continue
        basename "$d"
    done
}

_do_call() {
    local _q="agent=$(rt_url_encode "$1")"
    [ -n "$ONLY" ] && _q="$_q&only=$(rt_url_encode "$ONLY")"
    [ -n "$WITH_TEMP" ] && _q="$_q&with_temp=1"
    rt_call POST /v1/admin/owncloud-pull --query "$_q"
}

# rt_python_launcher is the SSOT launcher ("py -3" on Windows). UNQUOTED at the
# call site so "py -3" splits into two words. RESPONSE passed via env
# (guard-165), not interpolated into the single-quoted source. Resolved BEFORE
# the pull so _fleet_roster (which parses team-state JSON) can reuse it.
PYLAUNCH="$(rt_python_launcher 2>/dev/null || true)"

# _pull_one_agent <name> — the single-agent pull, factored out so fleet mode can
# repeat it. Sets RESPONSE; returns the rt_call rc (0 ok, 2 daemon error, 3 no
# daemon). Capture STDOUT only (no 2>&1): rt_call routes the JSON body to stdout,
# while staleness warnings + rc=2 error bodies go to stderr. Merging stderr would
# corrupt the JSON and break the parser (and a stale daemon is the common case
# right after a framework edit).
_pull_one_agent() {
    local _a="$1" _rc=0
    RESPONSE="$(_do_call "$_a")" || _rc=$?
    if [ "$_rc" = "3" ]; then
        # rc=3 CONFLATES "no daemon" with "request exceeded RT_CURL_TIMEOUT":
        # _runtime.sh:855 returns 3 for connection-refused, DNS failure AND
        # timeout alike, and curl's own stderr is discarded by 2>/dev/null
        # (guard-114), so the caller cannot tell which happened. Retrying a
        # TIMEOUT is never right — the request was not lost, it was slow, so the
        # retry re-issues the same slow sweep for another full ceiling AND
        # autospawns against a HEALTHY daemon, which is the orphan hazard
        # _runtime.sh:52-55 names.
        #
        # Measured (, cc-03, 2026-08-02): RT_CURL_TIMEOUT=5 took
        # 20.2s wall — 4x the ceiling — decomposing as 5s + ~10s autospawn + 5s.
        # At the 90s default that is ~190s of TOTAL SILENCE before the caller's
        # rt_no_daemon_error can print its (accurate) message, so an observer
        # who kills at 120s sees ZERO BYTES and reads a timeout as a silent
        # hang. That is the whole of the reported symptom, on three boxes.
        #
        # Probe first (guard-597: never declare "unreachable" without a fast
        # health check). A reachable daemon means this was a timeout, so return
        # immediately and let the caller's rt_no_daemon_error print the correct
        # diagnostic at ~1x the ceiling instead of ~2x. Only a genuinely-absent
        # daemon is worth respawning, which is the case autospawn is FOR.
        # Return 4, NOT 3 — the probe has just answered the question rc=3
        # conflates, and throwing that answer away is what let ONE slow agent
        # abort a whole fleet sweep (). The two cases need OPPOSITE
        # dispositions in fleet mode: a healthy-but-slow daemon is this agent's
        # problem (isolate, count, continue), while a genuinely absent one is
        # every agent's problem (nothing later in the roster can succeed, so
        # abort). Both single-agent call sites route 4 to rt_no_daemon_error
        # exactly as they routed 3, and rt_no_daemon_error re-probes rt_is_up
        # itself and prints the accurate "REACHABLE but exceeded
        # RT_CURL_TIMEOUT" diagnostic — so single-agent behaviour is byte-for-
        # byte unchanged and only the fleet loop gains a branch.
        if rt_is_up; then
            return 4
        fi
        if rt_try_autospawn; then
            _rc=0
            RESPONSE="$(_do_call "$_a")" || _rc=$?
        fi
    fi
    return "$_rc"
}

if [ -n "$ALL_AGENTS" ]; then
    # Fleet mode. Per-agent failures are ISOLATED — one unreachable agent must
    # not abort the sweep, or a single bad peer re-creates the blindness this
    # fixes. Track a worst-case exit code and report it at the end.
    fleet_rc=0
    agents_done=0
    agents_failed=0
    while IFS= read -r _fa; do
        [ -z "$_fa" ] && continue
        _arc=0
        _pull_one_agent "$_fa" || _arc=$?
        case $_arc in
            0) ;;
            # 3 = genuinely ABSENT daemon (rt_is_up said no AND autospawn could
            # not recover it). Fatal for all: nothing later in the roster can
            # succeed either, so continuing would print N identical failures and
            # bury the one message that matters.
            3) rt_no_daemon_error "owncloud-pull.sh";;
            # 4 = this agent TIMED OUT against a daemon rt_is_up confirmed
            # healthy. Isolated, like every other per-agent failure. Slow is the
            # EXPECTED case, not the exotic one —  measured 84.7-95.4s
            # per agent against a 90s ceiling — so before  this was the
            # ordinary way a sweep died partway down a sorted roster, always
            # leaving the same tail unrefreshed, which is precisely the fleet
            # blindness  exists to prevent.
            4) echo "[owncloud-pull] agent=$_fa TIMED OUT (daemon reachable; request exceeded RT_CURL_TIMEOUT=${RT_CURL_TIMEOUT:-?}s) — isolated, continuing roster" >&2
               agents_failed=$((agents_failed + 1)); fleet_rc=1; continue;;
            *) echo "[owncloud-pull] agent=$_fa FAILED (rc=$_arc; detail on stderr above)" >&2
               agents_failed=$((agents_failed + 1)); fleet_rc=1; continue;;
        esac
        if [ -n "$PYLAUNCH" ]; then
            _s="$(RESPONSE="$RESPONSE" $PYLAUNCH - <<'PYEOF'
import json, os, sys
try:
    r = json.loads(os.environ["RESPONSE"])
except Exception:
    sys.exit(3)
backend = r.get("backend", "?")
if not r.get("ok"):
    if "error" in r:
        print(f"  agent={r.get('agent','?')} FAILED (backend={backend}): {r['error']}")
        sys.exit(2)
    print(f"  agent={r.get('agent','?')} no-op (backend={backend}; {r.get('reason','')})")
    sys.exit(0)
errs = r.get("errors", 0)
print(f"  agent={r.get('agent','?')} pulled={r.get('pulled',0)} "
      f"in_sync={r.get('in_sync',0)} scanned={r.get('scanned',0)} "
      f"s3_absent={r.get('s3_absent',0)} errors={errs}")
# --only COVERAGE (), fleet twin of the single-agent block below.
# The continuity set is manifest-derived and agent-INDEPENDENT, so an unmatched
# name is unmatched for every agent -- N loud lines for one bad invocation is
# the intended output, not noise.
#
# DELIBERATE ASYMMETRY, so it does not read as an oversight and get "fixed":
# this gates the coverage line on `missing` while the single-agent block gates
# it on `only`, i.e. a FULLY-MATCHED scope prints nothing here and one
# confirming line there. Fleet output is one line PER AGENT, and the sole
# production --only caller (/open-questions, --all-agents --only
# pending-questions.yaml) always matches -- so a line-per-agent confirmation
# would double an interactive dashboard's output on the happy path to say
# nothing. The single-agent path prints one summary, where the positive
# confirmation is cheap and worth having.
only = r.get("only") or []
missing = r.get("requested_missing") or []
if missing:
    print(f"    --only: {len(only) - len(missing)}/{len(only)} matched; "
          f"NOT CONTINUITY FILES: {', '.join(missing)}")
if only and not r.get("scanned", 0):
    print("    NOTHING WAS SCANNED for this agent: the --only scope matched no "
          "continuity file (pulled=0 means 'never looked', not 'in sync').")
    sys.exit(4)
sys.exit(2 if errs else 0)
PYEOF
            )" || _prc=$?
            echo "${_s:-  agent=$_fa (raw) $RESPONSE}"
            # 4 = vacuous --only scope for this agent (nothing scanned). Counted
            # as a failure for the SAME reason as 2: the agent's continuity files
            # were not checked, so reporting it as pulled would be a false
            # all-clear ().
            case "${_prc:-0}" in
                2|4) agents_failed=$((agents_failed + 1)); fleet_rc=1;;
            esac
            _prc=0
        else
            echo "  agent=$_fa (raw) $RESPONSE"
        fi
        agents_done=$((agents_done + 1))
    done < <(_fleet_roster)
    # Zero-roster is a FAILURE, not a clean sweep ( F4). If the roster
    # comes back empty the loop body never runs, so agents_done and agents_failed
    # are both 0 and fleet_rc is still 0 — the command would print "0 agent(s)
    # pulled, 0 failed" and exit 0. That reads as success while nothing was
    # pulled at all, which is precisely the silent fleet-blindness 
    # exists to prevent. Both roster sources must have failed to reach here (the
    # team-state read AND the on-disk glob fallback), so this is always a real
    # fault worth surfacing loudly.
    if [ "$agents_done" -eq 0 ] && [ "$agents_failed" -eq 0 ]; then
        echo "[owncloud-pull] ERROR: fleet mode enumerated ZERO agents — nothing was pulled." >&2
        echo "[owncloud-pull]   Both roster sources failed: team-state agent_status AND the on-disk agent-dir glob." >&2
        echo "[owncloud-pull]   Check: bash core/scripts/team-state-read.sh --json | head, and that \$PYLAUNCH resolves (rt_python_launcher)." >&2
        exit 1
    fi
    echo "[owncloud-pull] fleet: ${agents_done} agent(s) pulled, ${agents_failed} failed"
    exit "$fleet_rc"
fi

rc=0
_pull_one_agent "$AGENT" || rc=$?
case $rc in
    0) ;;  # fall through to summary
    2) echo "[owncloud-pull] daemon returned an error (detail on stderr above)" >&2; exit 1;;
    # 3 (absent) and 4 (timeout, daemon healthy) BOTH route here, deliberately.
    # rt_no_daemon_error re-probes rt_is_up and prints the branch-correct
    # diagnostic itself, so single-agent output and exit code are unchanged by
    # the  split — only the fleet loop distinguishes them.
    3|4) rt_no_daemon_error "owncloud-pull.sh";;
    *) echo "[owncloud-pull] unexpected rc=$rc" >&2; exit "$rc";;
esac

if [ -z "$PYLAUNCH" ]; then
    echo "[owncloud-pull] (raw) $RESPONSE"
    exit 0
fi
pyrc=0
SUMMARY="$(RESPONSE="$RESPONSE" $PYLAUNCH - <<'PYEOF'
import json, os, sys
try:
    r = json.loads(os.environ["RESPONSE"])
except Exception:
    sys.exit(3)  # unparseable -> bash degrades to raw echo
backend = r.get("backend", "?")
if not r.get("ok"):
    if "error" in r:
        print(f"[owncloud-pull] FAILED (backend={backend}): {r['error']}")
        sys.exit(2)
    print(f"[owncloud-pull] no-op (backend={backend}; {r.get('reason','')})")
    sys.exit(0)
errs = r.get("errors", 0)
print(f"[owncloud-pull] agent={r.get('agent','?')} backend={backend} "
      f"pulled={r.get('pulled',0)} in_sync={r.get('in_sync',0)} "
      f"scanned={r.get('scanned',0)} s3_absent={r.get('s3_absent',0)} "
      f"local_ahead={r.get('local_ahead_skipped',0)} errors={errs}")
pf = r.get("pulled_files") or []
if pf:
    print(f"[owncloud-pull] pulled: {', '.join(pf)}")
# --only COVERAGE (). Report what the scope LOCATED beside what it
# pulled, so a zero is readable. Measurement + rationale in this file's header.
only = r.get("only") or []
missing = r.get("requested_missing") or []
if only:
    _line = (f"[owncloud-pull] --only scope: {len(only) - len(missing)}/{len(only)} "
             f"name(s) matched the continuity set")
    if missing:
        _line += f"; NOT CONTINUITY FILES: {', '.join(missing)}"
    print(_line)
if only and not r.get("scanned", 0):
    print("[owncloud-pull] NOTHING WAS SCANNED: the --only scope matched no "
          "continuity file, so pulled=0 means 'never looked', NOT 'in sync'.")
    sys.exit(4)
sys.exit(2 if errs else 0)
PYEOF
)" || pyrc=$?

case $pyrc in
    0) echo "$SUMMARY"; exit 0;;
    2) echo "$SUMMARY"
       echo "[owncloud-pull] WARN: pull reported errors — some continuity files may be stale on this machine" >&2
       exit 1;;
    # 4 = the --only scope matched ZERO continuity files, so the pull was
    # VACUOUS: nothing was examined. rc 2 (usage/config) rather than 1 (a pull
    # error) because nothing FAILED — the invocation named files the continuity
    # set does not contain. Exiting 0 here was the whole defect ():
    # a caller that checks only the rc reads a scan that never happened as
    # "everything already in sync".
    4) echo "$SUMMARY"
       echo "[owncloud-pull] ERROR: --only matched no continuity file — nothing was pulled and nothing was CHECKED." >&2
       echo "[owncloud-pull]   Name a file from the continuity set (session-manifest.yaml, sync_tier: continuity), or drop --only for the full pull." >&2
       exit 2;;
    *) echo "[owncloud-pull] (raw) $RESPONSE"; exit 0;;
esac
