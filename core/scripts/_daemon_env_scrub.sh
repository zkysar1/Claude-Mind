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

# daemon_scrub_inherited_env — unset every key the spawned daemon must resolve
# for itself. Intended to be called INSIDE the caller's spawn subshell, so the
# unsets scope to the child and never disturb the calling shell.
daemon_scrub_inherited_env() {
    local _v
    for _v in $(compgen -e); do
        case "$_v" in
            PYTEST_*|MOTO_*) unset "$_v" ;;
            GIT_INDEX_FILE|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_OBJECT_DIRECTORY|GIT_ALTERNATE_OBJECT_DIRECTORIES|GIT_PREFIX|GIT_NAMESPACE) unset "$_v" ;;
            MIND_ALLOW_TMP_OWNCLOUD_PUT|STORAGE_BACKEND|STORAGE_S3_BUCKET|STORAGE_DDB_SESSIONS_TABLE|STORAGE_DDB_LOCK_TABLE|ENVIRONMENT_ID|MACHINE_ID|MACHINE_MULTI|OWNCLOUD_SYNC_INTERVAL|OWNCLOUD_CACHE_TTL|MIND_API_TOKEN|MIND_API_BIND|MIND_API_PORT) unset "$_v" ;;
        esac
    done
}
