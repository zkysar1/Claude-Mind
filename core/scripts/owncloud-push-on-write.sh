#!/usr/bin/env bash
# PostToolUse[Write,Edit,MultiEdit] hook — real-time own-cloud single-file push.
#
# 6: owncloud_sync.py has documented this invocation as "the PostToolUse
# hook" since B15, and the sweep's no-baseline classifier RELIES on it existing
# ("a no-baseline divergence reaching the PERIODIC sweep is a STALE CACHE, not an
# unpushed authored write") — but nothing ever wired it. Consequence: every LLM
# Edit/Write to a world/ or meta/ file on an own-cloud box stayed local-only,
# the sweep refused to push it (cannot prove local authority), and the 
# S3-authoritative reconcile eventually PULLED the stale S3 object back over the
# verified local fix (observed twice on world/scripts/stale-jobs-scan.py:
# 7 fix eaten between 2026-07-08 and 2026-07-10; re-fixed 3).
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
# TIMEOUT BUDGET: parse+filter is one python3 spawn (~1s with the shim);
# the daemon POST is capped at 8s (curl --max-time); the CLI fallback adds an
# S3 HEAD+PUT (~1-3s on small world files). settings.json timeout is 30s,
# matching tree-sync-check.sh's margin rationale.
#
# Test knobs (mirrors embedding-index-freshness.py's pattern):
#   OWNCLOUD_PUSH_HOOK_ENV_LOCAL — override the .env.local path (backend probe)
#   OWNCLOUD_PUSH_HOOK_DRYRUN=1  — print "[owncloud-push-on-write] would push X"
#                                  and exit before any backend construction
set -u

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
# grep silently killed this hook on them (7, proven cc-02
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

# Parse hook stdin + governed-root filter in ONE python3 spawn. Prints the
# resolved absolute path when the write is governed, empty otherwise.
input=$(cat)
target=$(printf '%s' "$input" | \
    WORLD_P="${WORLD_PATH:-}" META_P="${META_PATH:-}" \
    PROOT="${PROJECT_ROOT:-}" APD="${AGENTS_PARENT_DIR:-agents}" \
    python3 -c "
import json, os, sys
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
            print(t)
            raise SystemExit
        except ValueError:
            pass
except SystemExit:
    raise
except Exception:
    pass
" 2>/dev/null || echo "")
[ -n "$target" ] || exit 0

if [ "${OWNCLOUD_PUSH_HOOK_DRYRUN:-}" = "1" ]; then
    echo "[owncloud-push-on-write] would push $target"
    exit 0
fi

# Real push — daemon-first (7): POST the per-file admin route. The
# daemon already carries the registry-derived STORAGE_* + MIND_AWS_* context,
# so this works on env-config deployments where the bare CLI would see no
# backend config. NEVER auto-spawn a daemon from a hook (30s budget, guard-141
# fail-open) — when the daemon is down or predates the route (404 → curl -f
# fails), fall through to the CLI with registry-derived env instead.
PORT_FILE="$PROJECT_ROOT/mind_api/state/daemon.port"
if [ -f "$PORT_FILE" ]; then
    port=$(tr -d '[:space:]' < "$PORT_FILE")
    enc=$(printf '%s' "$target" | python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))' 2>/dev/null || echo "")
    if [ -n "$port" ] && [ -n "$enc" ]; then
        resp=$(curl -sf -X POST --max-time 8 "http://127.0.0.1:${port}/v1/admin/owncloud-sync-file?path=${enc}" 2>/dev/null || echo "")
        if [ -n "$resp" ]; then
            case "$resp" in
                *'"ok": true'*|*'"ok":true'*)
                    echo "[owncloud-push-on-write] daemon push ok: $resp"
                    exit 0 ;;
                *)
                    # Route reached but the sync itself failed — the CLI
                    # fallback runs the SAME sync code and would repeat it.
                    # Surface the guard-983 warning and stop.
                    echo "[owncloud-push-on-write] daemon push FAILED for $target — $resp — the edit is local-only until the next sweep. Manual recovery per guard-983: curl -X POST 'http://127.0.0.1:${port}/v1/admin/owncloud-sync-file?path=<urlencoded-path>'" >&2
                    exit 0 ;;
            esac
        fi
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
    echo "[owncloud-push-on-write] real-time push FAILED for $target — the edit is local-only and WILL be reverted by the next no-baseline reconcile. Push manually per guard-983: curl -X POST 'http://127.0.0.1:<daemon-port>/v1/admin/owncloud-sync-file?path=<urlencoded-path>' (port: mind_api/state/daemon.port) or py -3 core/scripts/owncloud_sync.py --file \"$target\"" >&2
fi
exit 0
