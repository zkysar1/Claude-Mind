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
# --only <a.yaml[,b.jsonl]> narrows the pull to those continuity files and skips
# the temp/ sweep. Names are matched against the continuity set, so --only can
# never widen the pull. Use it for a targeted refresh: a full sweep is ~59s/agent
# (904 files, measured on cc-04), i.e. ~5min fleet-wide — fine for /start, far
# too slow for an interactive dashboard that needs one file per agent.
#
# Usage: bash core/scripts/owncloud-pull.sh [--agent <name>]
#        bash core/scripts/owncloud-pull.sh --all-agents
#        bash core/scripts/owncloud-pull.sh --all-agents --only pending-questions.yaml
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

AGENT=""
ALL_AGENTS=""
ONLY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent) AGENT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --all-agents) ALL_AGENTS=1; shift;;
        --only) ONLY="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
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
            3) rt_no_daemon_error "owncloud-pull.sh";;  # no daemon: fatal for all, not just one
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
sys.exit(2 if errs else 0)
PYEOF
            )" || _prc=$?
            echo "${_s:-  agent=$_fa (raw) $RESPONSE}"
            if [ "${_prc:-0}" = "2" ]; then agents_failed=$((agents_failed + 1)); fleet_rc=1; fi
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
    3) rt_no_daemon_error "owncloud-pull.sh";;
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
sys.exit(2 if errs else 0)
PYEOF
)" || pyrc=$?

case $pyrc in
    0) echo "$SUMMARY"; exit 0;;
    2) echo "$SUMMARY"
       echo "[owncloud-pull] WARN: pull reported errors — some continuity files may be stale on this machine" >&2
       exit 1;;
    *) echo "[owncloud-pull] (raw) $RESPONSE"; exit 0;;
esac
