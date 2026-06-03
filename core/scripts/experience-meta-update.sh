#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# experience-meta-update — daemon-aware wrapper. Sets one flat top-level
# field on experience-meta.json.
#
# Usage: bash core/scripts/experience-meta-update.sh <field> <value>
#
# Daemon path: rt_call POST /v1/experience/meta-update?field=&value=
# On success, prints the updated meta dict (indent=2, ensure_ascii=False)
# to stdout — byte-compat with the CLI's json.dumps output.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse positional args ------------------------------------------------
FIELD="${1-}"
VALUE="${2-}"

if [ -z "$FIELD" ] || [ -z "${2+set}" ]; then
    echo "Usage: experience-meta-update.sh <field> <value>" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="field=$(rt_url_encode "$FIELD")&value=$(rt_url_encode "$VALUE")"

_translate() {
    # Extract .meta from daemon JSON response and print indent=2 ensure_ascii=False
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
meta = resp.get('meta') or resp
print(json.dumps(meta, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/experience/meta-update --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/meta-update --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-meta-update.sh";;
    *) exit $rc;;
esac
