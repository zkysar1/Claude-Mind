#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# context-reads-record-bash-fetch.sh — PostToolUse[Bash] hook recording URLs this
# session fetched with curl/wget into the session provenance manifest ().
#
# RELATIONSHIP TO ITS SIBLINGS, stated because guard-152 requires it and because
# three PostToolUse recorders now look interchangeable and are not:
#   context-reads-record.sh        PostToolUse[Read]              -> read tracker (paths)
#   context-reads-record-fetch.sh  PostToolUse[WebFetch|WebSearch]-> provenance (urls/queries)
#   THIS                           PostToolUse[Bash]              -> provenance (urls, curl/wget only)
#   bash-edit-record.sh            PostToolUse[Bash]              -> uncommitted-edits.jsonl (WRITES)
# The last one shares this hook's event and is NOT a candidate to extend: it
# records files this command WROTE, which is a different act from the one the Q4
# consumer measures (guard-4818 — uniformity across sibling call sites is not
# correctness). Kept SEPARATE from context-reads-record-fetch.sh on purpose: that
# script is on a working WebFetch/WebSearch path, and adding a Bash branch to its
# stdin handling would put a regression risk on a path that is not broken. The
# shared logic lives where it belongs — in _fetch_provenance_extract.extract(),
# which both call — so this is a second CALL SITE, never a second implementation.
#
# WHY IT EXISTS: the session-level instruction mandates preferring Bash over the
# Read tool, and the provenance manifest recorded only tool-fetches — so obeying
# the instruction GUARANTEED a Q4 `decorative-citation` on a genuinely fetched
# URL. Measured on  occurrence 10: three URLs pulled live with curl
# (HTTP 200, 49,276-byte body) all reported "cited but NOT retrieved this
# session". guard-4407 recorded that scope limit honestly; this closes it.
#
# THE FETCH-VERB REQUIREMENT LIVES IN THE EXTRACTOR, not here, so it is unit
# testable without a hook payload. Without it `echo "https://fabricated"` would
# register as a retrieval and this recorder would launder the very fabricated
# citation Q4 exists to catch (guard-1901).
#
# Fail-open EVERYWHERE (guard-141): a provenance miss must never break the
# command whose result the user is waiting on. No `set -e`, no pipefail.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || exit 0

# ONE pass over stdin, into a variable — a hook payload arrives on a pipe and
# cannot be rewound, and this hook must pre-filter BEFORE paying for a python3
# subprocess. That pre-filter is the whole latency argument for a separate
# script: PostToolUse[Bash] fires on EVERY Bash call, and the overwhelming
# majority mention no URL at all, so the common path spawns nothing.
input=$(cat 2>/dev/null) || exit 0
case "$input" in
    *http*) ;;
    *) exit 0 ;;
esac

payload=$(printf '%s' "$input" | RESULT_CAP=10 python3 "$CORE_ROOT/scripts/_fetch_provenance_extract.py" 2>/dev/null) || exit 0
[ -n "$payload" ] || exit 0
session_id=$(printf '%s\n' "$payload" | head -1)
records=$(printf '%s\n' "$payload" | tail -n +2)
[ -n "$records" ] || exit 0

sid_arg=""
[ -n "$session_id" ] && sid_arg="--session-id $session_id"

# Resolve agent from session_id — MIND_AGENT is not injected into PostToolUse
# hook env (PreToolUse[Bash]'s bash-agent-inject stamps the COMMAND env, not the
# hook's). ORDER-CRITICAL: must stay BEFORE `source _platform.sh`; MSYS_NO_PATHCONV
# (set there) breaks session-binding-read.sh on Git Bash ().
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi

source "$CORE_ROOT/scripts/_platform.sh" 2>/dev/null || true

while IFS=$'\t' read -r kind value; do
    [ -n "${value:-}" ] || continue
    env MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" \
        record-prov $sid_arg --kind "$kind" "$value" >/dev/null 2>&1 || true
done <<< "$records"

exit 0
