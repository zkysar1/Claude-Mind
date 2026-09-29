#!/usr/bin/env bash
# PostToolUse[Write,Edit,MultiEdit] hook — real-time own-cloud single-file push.
#
# : owncloud_sync.py has documented this invocation as "the PostToolUse
# hook" since B15, and the sweep's no-baseline classifier RELIES on it existing
# ("a no-baseline divergence reaching the PERIODIC sweep is a STALE CACHE, not an
# unpushed authored write") — but nothing ever wired it. Consequence: every LLM
# Edit/Write to a world/ or meta/ file on an own-cloud box stayed local-only,
# the sweep refused to push it (cannot prove local authority), and the 
# S3-authoritative reconcile eventually PULLED the stale S3 object back over the
# verified local fix (observed twice on world/scripts/stale-jobs-scan.py:
#  fix eaten between 2026-07-08 and 2026-07-10; re-fixed ).
# This hook closes the class: a tool write under a governed root is pushed to S3
# immediately via sync_file (multi_machine=False — the single-file path that
# KNOWS the write is locally authored and records the manifest baseline).
#
# Scope: pushes only under WORLD_PATH / META_PATH / PROJECT_ROOT/<agents>/.
# Everything else (core/, .claude/, product repos) is git-synced — fast-exit.
# sync_file applies its own second-layer filters (machine-local exclusions,
# H4a agent ownership), so this shim never duplicates that policy.
#
# CRITICAL — DO NOT add `set -e` or `set -o pipefail`. Per guard-141, Claude
# Code hooks MUST fail open on every error path: a push failure must never
# block the user's edit. Failures print a stderr pointer to the guard-983
# manual push recipe instead.
#
# ORDERING NOTE: hook commands for the same matcher may run concurrently, so
# tree-front-matter-sync (inside tree-sync-check.sh) can mutate a tree-node .md
# after this push captures it. That divergence self-heals: the mutation is a
# genuine local write over a recorded baseline, which the next periodic sweep
# classifies local-authored and pushes.
#
# TIMEOUT BUDGET: two python3 spawns, parse+filter+encode and the verdict
# (0.5-1.7s each through the shim, measured 2026-09-28 on a Windows box under
# fleet load); the daemon POST is capped at 8s (curl --max-time); the CLI
# fallback adds an S3 HEAD+PUT (~1-3s on small world files). settings.json
# timeout is 30s, matching tree-sync-check.sh's margin rationale.
#
# Test knobs (mirrors embedding-index-freshness.py's pattern):
#   OWNCLOUD_PUSH_HOOK_ENV_LOCAL — override the .env.local path (backend probe)
#   OWNCLOUD_PUSH_HOOK_DRYRUN=1  — print "[owncloud-push-on-write] would push X"
#                                  and exit before any backend construction
#   OWNCLOUD_PUSH_HOOK_PORT_FILE — override the daemon.port path, so a test can
#                                  aim the real curl + verdict path at a fake daemon
set -u

#  outcome 4. An exit-0 hook's stderr never enters the model's context
# (guard-1680), so every "did not land" warning below reached only the human
# terminal, and the session that made the edit carried on as if it had synced.
# hookSpecificOutput.additionalContext on stdout is the channel that reaches the
# model (the retrieval-pulse-hook.sh shape); stderr keeps the terminal copy.
_tell_model() {
    echo "$1" >&2
    MSG="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': os.environ['MSG'],
    }
}))
" 2>/dev/null || true
}

# _paths.sh MUST come first: it puts core/scripts/.python-shim/python3 on PATH
# (Windows Store-stub defense) and exports WORLD_PATH/META_PATH/PROJECT_ROOT/
# AGENTS_PARENT_DIR. See CLAUDE.md "Python Invocation (Windows)".
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0

# Fast-exit 1: backend. Explicit STORAGE_BACKEND in .env.local wins (legacy
# form); else derive from ENVIRONMENT_ID + the committed environment registry
# (core/config/environments/<id>.yaml `backend:` key) — the SAME resolution
# chain the daemon runs (_apply_environment_registry, setdefault precedence).
# Env-config deployments set ONLY ENVIRONMENT_ID, so the old env.local-only
# grep silently killed this hook on them (, proven cc-02
# 2026-07-16: every governed write waited for the ~120s sweep — the
# both-moved conflict-freeze breeder).
ENV_LOCAL="${OWNCLOUD_PUSH_HOOK_ENV_LOCAL:-$PROJECT_ROOT/.env.local}"
[ -f "$ENV_LOCAL" ] || exit 0
backend=$(grep -E '^[[:space:]]*STORAGE_BACKEND=' "$ENV_LOCAL" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
if [ -z "$backend" ]; then
    env_id=$(grep -E '^[[:space:]]*ENVIRONMENT_ID=' "$ENV_LOCAL" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
    reg="$PROJECT_ROOT/core/config/environments/${env_id}.yaml"
    if [ -n "$env_id" ] && [ -f "$reg" ]; then
        backend=$(grep -E '^[[:space:]]*backend:' "$reg" 2>/dev/null | head -1 | cut -d: -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
    fi
fi
[ "$backend" = "own-cloud" ] || exit 0

# Parse hook stdin + governed-root filter in ONE python3 spawn. For a governed
# write it prints ONE tab-separated line: the resolved path, its URL-encoded
# form, and its repo-relative POSIX form ("" for world/meta, which live outside
# the repo). Nothing otherwise. One line, not three: python on Windows ends
# every line with \r\n, and $(...) strips only the last one.
input=$(cat)
parsed=$(printf '%s' "$input" | \
    WORLD_P="${WORLD_PATH:-}" META_P="${META_PATH:-}" \
    PROOT="${PROJECT_ROOT:-}" APD="${AGENTS_PARENT_DIR:-agents}" \
    python3 -c "
import json, os, sys, urllib.parse
from pathlib import Path
try:
    fp = (json.load(sys.stdin).get('tool_input') or {}).get('file_path') or ''
    if not fp:
        raise SystemExit
    t = Path(fp)
    if not t.is_absolute():
        proot = os.environ.get('PROOT') or ''
        if not proot:
            raise SystemExit
        t = Path(proot) / t
    t = t.resolve()
    roots = [os.environ.get('WORLD_P') or '', os.environ.get('META_P') or '']
    proot = os.environ.get('PROOT') or ''
    if proot:
        roots.append(str(Path(proot) / (os.environ.get('APD') or 'agents')))
    for r in roots:
        if not r:
            continue
        try:
            t.relative_to(Path(r).resolve())
        except ValueError:
            continue
        try:
            rel = t.relative_to(Path(proot).resolve()).as_posix() if proot else ''
        except ValueError:
            rel = ''
        print('\t'.join((str(t), urllib.parse.quote(str(t), safe=''), rel)))
        raise SystemExit
except SystemExit:
    raise
except Exception:
    pass
" 2>/dev/null || echo "")
IFS=$'\t' read -r target enc rel <<< "$parsed"
[ -n "$target" ] || exit 0

if [ "${OWNCLOUD_PUSH_HOOK_DRYRUN:-}" = "1" ]; then
    echo "[owncloud-push-on-write] would push $target"
    exit 0
fi

# Real push — daemon-first (): POST the per-file admin route. The
# daemon already carries the registry-derived STORAGE_* + MIND_AWS_* context,
# so this works on env-config deployments where the bare CLI would see no
# backend config. NEVER auto-spawn a daemon from a hook (30s budget, guard-141
# fail-open). Only "no answer" (status 000: no daemon, or none within 8s) and
# "no route" (404: a daemon older than the route) fall through to the CLI
# below. Every other answer is final: the CLI runs the SAME sync code and would
# repeat it. curl runs without -f: the route reports its own failures as a 500
# whose body names the error, and -f discards that body.
PORT_FILE="${OWNCLOUD_PUSH_HOOK_PORT_FILE:-$PROJECT_ROOT/mind_api/state/daemon.port}"
port=""
[ -f "$PORT_FILE" ] && port=$(tr -d '[:space:]' < "$PORT_FILE")
if [ -n "$port" ]; then
    out=$(curl -s -X POST --max-time 8 -w '\n%{http_code}' \
        "http://127.0.0.1:${port}/v1/admin/owncloud-sync-file?path=${enc}" 2>/dev/null)
    code=${out##*$'\n'}
    resp=${out%$'\n'*}
    if [ -n "$code" ] && [ "$code" != 000 ] && [ "$code" != 404 ]; then
        # ok:true means "no error", NOT "landed" (guard-5663): a both-diverged
        # skip answers ok:true pushed:0 diverged_skipped:1. A "reason" is a
        # sync_file _skip, which never pushes. Verdicts:
        #   landed        a landing counter > 0: pushed, in_sync,
        #                 local_wins_resolved, *_merged
        #   by-design     not_governed / machine_local: the daemon's own policy
        #                 keeps the file out of the store
        #   peer          peer_agent: this box does not hold the agent dir's
        #                 runner claim, so the store refuses the edit from here
        #   invisible     missing_or_dir: the daemon cannot see the path ()
        #   wrong-daemon  a non-own-cloud backend answered on this own-cloud box
        #   skipped       any other reason
        #   not-landed    ok, but no landing counter
        #   failed        ok not true (every error status answers ok:false), or
        #                 an unreadable body
        # Only landed, by-design, and a git-tracked peer file stay out of the
        # model's context: every other verdict means the edit is not in the store.
        verdict=$(printf '%s' "$resp" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = None
if not isinstance(d, dict) or d.get("ok") is not True:
    print("failed")
elif "reason" in d:
    r = str(d["reason"])
    print("by-design" if r in ("not_governed", "machine_local") else
          "peer" if r == "peer_agent" else
          "invisible" if r == "missing_or_dir" else
          "wrong-daemon" if r.startswith("non-own-cloud") else "skipped")
elif any(isinstance(v, int) and not isinstance(v, bool) and v > 0
         and (k in ("pushed", "in_sync", "local_wins_resolved") or k.endswith("_merged"))
         for k, v in d.items()):
    print("landed")
else:
    print("not-landed")
' 2>/dev/null)
        case "$verdict" in
            landed)
                echo "[owncloud-push-on-write] daemon push ok: $resp" ;;
            by-design)
                echo "[owncloud-push-on-write] not pushed, by design: $resp" ;;
            peer)
                agent=${rel#"${AGENTS_PARENT_DIR:-agents}"/}
                agent=${agent%%/*}
                if [ -n "$rel" ] && git -C "$PROJECT_ROOT" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
                    echo "[owncloud-push-on-write] not pushed (agent dir '$agent' is not owned here; git tracks the file, so a commit carries it): $resp"
                else
                    _tell_model "[owncloud-push-on-write] NOT pushed: $target is in agent dir '$agent', whose runner claim this box does not hold (peer_agent), so the store never takes this edit from here, and git does not track the file. Nothing delivers it: make the change on the claim-holder's box (bash core/scripts/runner-claim.sh status --agent $agent names it)."
                fi ;;
            invisible)
                _tell_model "[owncloud-push-on-write] NOT pushed: the daemon cannot see $target (missing_or_dir) — $resp. The edit is local-only. Most likely it landed somewhere unintended, such as a literal PROJECT_ROOT/world or /meta path instead of the configured external root. Check: bash core/scripts/backend-cat.sh head <path> --exit-on-drift" ;;
            wrong-daemon)
                _tell_model "[owncloud-push-on-write] NOT pushed: the process on port $port answered as a non-own-cloud backend, but this box's config selects own-cloud, so it is not this box's daemon (a test daemon can hold the port). $target is local-only until the real daemon answers: check mind_api/state/daemon.port, then re-push per guard-983." ;;
            skipped)
                _tell_model "[owncloud-push-on-write] NOT pushed: the daemon skipped $target for a reason this hook does not know — $resp. Treat the edit as not in the store until checked: bash core/scripts/backend-cat.sh head <path> --exit-on-drift" ;;
            not-landed)
                _tell_model "[owncloud-push-on-write] NOT pushed: the daemon answered ok but did not land $target in the store — $resp. ok means no error, not pushed (guard-5663). diverged_skipped: local and store both changed since the baseline, so every sweep skips the file until it is reconciled (/reconcile-owncloud-conflicts). stale_pulled or nobaseline_reconciled: the store copy replaced this edit locally. Check before the next write to this file: bash core/scripts/backend-cat.sh head <path> --exit-on-drift" ;;
            *)
                _tell_model "[owncloud-push-on-write] push FAILED or unreadable for $target (HTTP $code) — $resp. Treat the edit as not in the store until checked: bash core/scripts/backend-cat.sh head <path> --exit-on-drift. A transient error clears on a re-push or the next sweep; a refused merge (both sides changed since the baseline) does not, and the file stays frozen until reconciled (/reconcile-owncloud-conflicts). Re-push per guard-983: curl -X POST 'http://127.0.0.1:${port}/v1/admin/owncloud-sync-file?path=<urlencoded-path>'" ;;
        esac
        exit 0
    fi
fi

# CLI fallback: creds (MIND_AWS_*) come from .env.local; governed-root env vars
# follow owncloud_sync.py's own documented recipe. Guard empties under set -u.
set -a
# shellcheck disable=SC1090
source "$ENV_LOCAL" 2>/dev/null || true
set +a
[ -n "${WORLD_PATH:-}" ] && export WORLD_PATH MIND_WORLD="${MIND_WORLD:-$WORLD_PATH}"
[ -n "${META_PATH:-}" ] && export META_PATH MIND_META="${MIND_META:-$META_PATH}"
# Env-config deployments: derive STORAGE_*/region from the registry exactly as
# the daemon does. Mapping inlined from _REGISTRY_KEY_TO_ENV
# (mind_api/src/__main__.py) because a hook must stay self-contained and fast;
# setdefault semantics — explicit .env.local values win.
if [ -z "${STORAGE_BACKEND:-}" ] && [ -n "${ENVIRONMENT_ID:-}" ]; then
    reg="$PROJECT_ROOT/core/config/environments/$ENVIRONMENT_ID.yaml"
    if [ -f "$reg" ]; then
        eval "$(REG_PATH="$reg" python3 -c '
import os
try:
    import yaml
    d = yaml.safe_load(open(os.environ["REG_PATH"])) or {}
    m = {"backend": "STORAGE_BACKEND", "bucket": "STORAGE_S3_BUCKET",
         "sessions_table": "STORAGE_DDB_SESSIONS_TABLE",
         "lock_table": "STORAGE_DDB_LOCK_TABLE",
         "region": "AWS_DEFAULT_REGION"}
    for k, env in m.items():
        v = d.get(k)
        if v is not None and str(v).strip() and not os.environ.get(env):
            print("export %s=%s" % (env, repr(str(v).strip())))
except Exception:
    pass
' 2>/dev/null)"
    fi
fi

if ! python3 "$SCRIPT_DIR/owncloud_sync.py" --file "$target"; then
    # The bare-CLI recipe this line used to offer silently no-ops from a shell
    # (guard-5663), so it is not handed to the model.
    _tell_model "[owncloud-push-on-write] NOT pushed: no daemon answered (or it predates the push route), and the CLI fallback failed for $target. Check the store (bash core/scripts/backend-cat.sh head <path> --exit-on-drift), then re-push through the daemon once it is up, per guard-983: curl -X POST 'http://127.0.0.1:<daemon-port>/v1/admin/owncloud-sync-file?path=<urlencoded-path>' (port: mind_api/state/daemon.port)"
fi
exit 0
