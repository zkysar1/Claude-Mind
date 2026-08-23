#!/usr/bin/env bash
# agent-resume-scaffold.sh <agent-name>
#
# Destination-side TRANSPLANT-RESUME scaffold.
#
# When an agent directory arrives on a machine via `git clone`, its TRACKED
# content comes with it (`.initialized`, `self.md`, `aspirations.jsonl`,
# `curriculum.yaml`, journal/, experience/, ...) but its MACHINE-LOCAL pieces
# do NOT — `session/` and `local-paths.conf` are gitignored (`**/session/`,
# `**/local-paths.conf`) precisely because they hold per-machine state (runner
# PIDs, heartbeat, the local cache paths) that must never travel between
# machines. Because `session/agent-state` is therefore absent, plain
# `session-state-get.sh` reports UNINITIALIZED — indistinguishable, on its own,
# from a brand-new agent.
#
# This script scaffolds ONLY the missing machine-local pieces so that /start can
# RESUME the cloned agent as an EXISTING one, instead of re-running first-boot
# init (which would re-elicit identity and overwrite the cloned self.md /
# curriculum.yaml). The `.initialized` marker IS tracked, so its presence after
# a clone is the reliable "this agent already exists" discriminator.
#
# Does (idempotent):
#   - Verifies the agent dir + `.initialized` marker exist (else NOT a
#     transplanted agent -> exit 2; caller runs full init).
#   - Writes a DEFAULT local-paths.conf (own-cloud local cache under
#     <cache-root>/<env-id>/{world,meta}) ONLY when no WORLD_PATH is configured
#     yet. Honors a pre-existing conf and the RUNTIME_CACHE_ROOT override.
#   - Creates the agent's session/ dir.
#
# Deliberately does NOT (single-responsibility + invariant safety):
#   - Write agent-state / agent-mode — runner-owned; /start owns those
#     (.claude/rules/user-interaction.md, guard-340).
#   - Touch self.md / curriculum.yaml / aspirations.jsonl / any tracked content.
#
# Exit codes: 0 = scaffolded (resume case), 2 = not a transplanted agent,
#             1 = usage/error. Emits one JSON line on stdout for the caller.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AGENT="${1:-}"
if [ -z "$AGENT" ]; then
    echo "usage: agent-resume-scaffold.sh <agent-name>" >&2
    exit 1
fi

# Resolve the right agent before sourcing so _paths.sh does not fall through to
# the first-available agent (it would emit a misleading WARN to stderr).
export MIND_AGENT="$AGENT"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
set -euo pipefail

ADIR="$(agent_dir "$AGENT")"

# A transplanted agent has BOTH the dir and the tracked `.initialized` marker.
if [ ! -d "$ADIR" ]; then
    printf '{"scaffolded":false,"reason":"agent-dir-absent","agent":"%s"}\n' "$AGENT"
    exit 2
fi
if [ ! -f "$ADIR/.initialized" ]; then
    printf '{"scaffolded":false,"reason":"no-initialized-marker","agent":"%s"}\n' "$AGENT"
    exit 2
fi

CONF="$ADIR/local-paths.conf"
WROTE_CONF=false

if [ -f "$CONF" ] && grep -q '^WORLD_PATH=' "$CONF" 2>/dev/null; then
    # Idempotent: an existing conf is authoritative — never clobber it.
    WORLD_PATH="$(grep '^WORLD_PATH=' "$CONF" | head -1 | cut -d= -f2- | tr -d '\r')"
    META_PATH="$(grep '^META_PATH=' "$CONF" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true)"
else
    # Derive a default local cache root. On Windows/Git-Bash, normalise $HOME to
    # the C:/ drive form Python's path resolver expects (local-paths.conf is
    # documented to use forward-slash drive paths). RUNTIME_CACHE_ROOT overrides.
    HOME_PORTABLE="$HOME"
    if command -v cygpath >/dev/null 2>&1; then
        HOME_PORTABLE="$(cygpath -m "$HOME" 2>/dev/null || echo "$HOME")"
    fi
    CACHE_ROOT="${RUNTIME_CACHE_ROOT:-$HOME_PORTABLE/.mind-cache}"

    # Namespace by ENVIRONMENT_ID (from .env.local) so multiple environments on one
    # machine do not share a cache directory.
    ENVID="mind"
    if [ -f "$PROJECT_ROOT/.env.local" ]; then
        _eid="$(grep '^ENVIRONMENT_ID=' "$PROJECT_ROOT/.env.local" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true)"
        [ -n "${_eid:-}" ] && ENVID="$_eid"
    fi

    WORLD_PATH="$CACHE_ROOT/$ENVID/world"
    META_PATH="$CACHE_ROOT/$ENVID/meta"
    mkdir -p "$WORLD_PATH" "$META_PATH"
    {
        echo "# Auto-written by agent-resume-scaffold.sh (transplant resume)."
        echo "# Local cache of the own-cloud world/meta; rehydrates from S3 on first read."
        echo "# Edit these paths, or set RUNTIME_CACHE_ROOT before /start, to relocate the cache."
        echo "WORLD_PATH=$WORLD_PATH"
        echo "META_PATH=$META_PATH"
    } > "$CONF"
    WROTE_CONF=true
fi

# Scaffold the machine-local session dir. NEVER writes agent-state here.
mkdir -p "$ADIR/session"

printf '{"scaffolded":true,"agent":"%s","world_path":"%s","meta_path":"%s","wrote_conf":%s}\n' \
    "$AGENT" "$WORLD_PATH" "${META_PATH:-}" "$WROTE_CONF"
exit 0
