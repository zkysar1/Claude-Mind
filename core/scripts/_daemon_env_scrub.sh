#!/usr/bin/env bash
# _daemon_env_scrub.sh — THE one definition of the environment scrub applied
# before spawning the mind_api daemon. Sourced by BOTH spawn sites:
#
#   core/scripts/_runtime.sh      rt_spawn        (wrapper auto-respawn)
#   core/scripts/mind-api-start.sh                (direct / --restart launcher)
#
# WHY A SHARED FILE RATHER THAN A LIST IN EACH (). The two spawn
# sites are already documented twins — both carry "Twin of the … spawn site —
# fix both or neither" comments for their `cd`/`disown` shape and their log
# cap. The scrub was the one thing that existed on ONE side only, and that
# asymmetry is precisely what the 2026-09-02 incident rode in on: the launcher
# spawned the SHARED daemon with the caller env intact, so the daemon resolved
# STORAGE_BACKEND=local on an own-cloud box and every daemon-mediated world
# write from that box stayed on the local mirror for six hours. A second copy
# of the list would have re-armed exactly that drift, so there is one list and
# two callers (guard-2676 — a capability is a scoped CALL into the shared
# component, never a transcription of it).
#
# WHAT IS SCRUBBED, and why each group:
#   - test markers (PYTEST_*, MOTO_*) and the tempdir-tripwire escape hatch.
#     No daemon should ever carry a test session's flags.
#   - every storage/config key the daemon self-resolves from .env.local / the
#     environment registry (_N3_ALLOWED_EXACT in mind_api/src/__main__.py,
#     minus RUNTIME_DIR). mind_api's _load_env_local uses setdefault
#     ("explicit launch env wins"), so an inherited var BLOCKS the .env.local
#     value — which is what makes an inherited STORAGE_BACKEND decisive rather
#     than merely advisory (guard-2617: the DAEMON resolves the backend, so
#     the only env that matters is the one the daemon is handed).
#   - git's repository-override variables. git exports these into HOOK
#     processes; a daemon that inherits GIT_INDEX_FILE points at a temp index
#     that is deleted seconds later, and every git call it makes afterwards
#     runs against a MISSING index (observed 2026-09-02: the uncommitted-work
#     gate called every tracked file dirty while `git status` was clean).
#
# WHAT IS DELIBERATELY KEPT:
#   - RUNTIME_DIR — the sanctioned per-test daemon-isolation override
#     (lifecycle.runtime_dir). Spawning into your own runtime dir is never a
#     hijack, so isolation must survive the scrub.
#   - WORLD_PATH / META_PATH — the documented spawn-shell path channel
#     (see _load_env_local's docstring). These arrive from the spawning shell
#     BY DESIGN and are not self-resolved from .env.local.
#
# Adding a key to the daemon's self-resolved set (_N3_ALLOWED_EXACT) means
# adding it HERE too, once. `test_runtime_spawn_env_scrub.py` and
# `test_daemon_start_env_scrub.py` pin the two call sites against this list.
#
# WHAT IS FILLED IN (): daemon_overlay_settings_env exports each key
# of the committed .claude/settings.json "env" block that the spawning shell
# does NOT carry. That block reaches a Claude Code session's own tool calls as
# it stood when the SESSION LAUNCHED; a detached daemon has no session and
# takes whatever shell spawned it. The pull that brings a new flag also moves
# the daemon's code, and the staleness respawn then runs in that same pre-pull
# shell, so the flag misses the one process that acts on it. Measured on the
# segmented board writer (BOARD_SEGMENTED_CHANNELS): 21h after the flip reached
# main, four boxes' daemons still wrote the legacy channel file.
#   - FILL, never override: an inherited value wins (the "explicit launch env
#     wins" contract of _load_env_local), so a hand-set or test-pinned value
#     survives — including an empty one.
#   - never a scrubbed key: those are the daemon's to resolve (above).
#   - not when the parent is a test (PYTEST_CURRENT_TEST): a test daemon gets
#     exactly the environment its test built.
#   - fail-open: a missing or malformed settings file fills nothing.
# `test_daemon_env_settings_overlay.py` pins it at both call sites.

# daemon_env_key_is_scrubbed <name> — exit 0 when the scrub removes <name>.
# The ONE statement of the scrubbed set: the scrub unsets it and the overlay
# never fills it.
daemon_env_key_is_scrubbed() {
    case "$1" in
        PYTEST_*|MOTO_*) return 0 ;;
        GIT_INDEX_FILE|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_OBJECT_DIRECTORY|GIT_ALTERNATE_OBJECT_DIRECTORIES|GIT_PREFIX|GIT_NAMESPACE) return 0 ;;
        MIND_ALLOW_TMP_OWNCLOUD_PUT|STORAGE_BACKEND|STORAGE_S3_BUCKET|STORAGE_S3_ENDPOINT_URL|STORAGE_DDB_SESSIONS_TABLE|STORAGE_DDB_LOCK_TABLE|ENVIRONMENT_ID|MACHINE_ID|MACHINE_MULTI|OWNCLOUD_SYNC_INTERVAL|OWNCLOUD_CACHE_TTL|MIND_API_TOKEN|MIND_API_BIND|MIND_API_PORT) return 0 ;;
    esac
    return 1
}

# daemon_scrub_inherited_env — unset every key the spawned daemon must resolve
# for itself. Intended to be called INSIDE the caller's spawn subshell, so the
# unsets scope to the child and never disturb the calling shell.
daemon_scrub_inherited_env() {
    local _v
    for _v in $(compgen -e); do
        if daemon_env_key_is_scrubbed "$_v"; then unset "$_v"; fi
    done
}

# daemon_overlay_settings_env <python-launcher> — export each committed
# settings env key the calling shell lacks, and print the names it filled (the
# callers append that line to their spawn log). Call INSIDE the spawn subshell,
# before daemon_scrub_inherited_env. Always returns 0.
daemon_overlay_settings_env() {
    local _py="${1:-}" _settings="${PROJECT_ROOT:-}/.claude/settings.json"
    local _k _v _filled=""
    if [ -n "${PYTEST_CURRENT_TEST:-}" ] || [ -z "$_py" ] || [ ! -r "$_settings" ]; then
        return 0
    fi
    while IFS= read -r -d '' _k && IFS= read -r -d '' _v; do
        case "$_k" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
        if daemon_env_key_is_scrubbed "$_k" || [ -n "${!_k+x}" ]; then continue; fi
        export "$_k=$_v"
        _filled="$_filled $_k"
    done < <($_py -c '
import json, sys
try:
    env = json.load(open(sys.argv[1], encoding="utf-8")).get("env")
except Exception:
    env = None
if isinstance(env, dict):
    for k, v in env.items():
        if isinstance(k, str) and isinstance(v, str) and "\0" not in k + v:
            sys.stdout.buffer.write((k + "\0" + v + "\0").encode("utf-8"))
' "$_settings" 2>/dev/null)
    if [ -n "$_filled" ]; then
        echo "[$(date +%Y-%m-%dT%H:%M:%S)] daemon env: filled from .claude/settings.json (absent in the spawning shell):$_filled"
    fi
    return 0
}
