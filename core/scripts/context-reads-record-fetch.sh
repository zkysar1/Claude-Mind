#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-tool-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PostToolUse[WebFetch|WebSearch] hook — record retrieved URLs into the session
# provenance manifest ().
#
# Sibling of context-reads-record.sh, which does the same job for Read. Both
# write the SAME tracker file, so one query surface answers "did this session
# actually retrieve that?" for a fetched URL and a read file alike. Entries go in
# behind PROVENANCE_PREFIX, which keeps them out of the BLOCKING dedup gate —
# a URL is not a file that can be "already in context".
#
# WHY THIS EXISTS: an agent can emit a plausible-looking citation from parametric
# memory. The manifest makes that falsifiable — provenance-check.sh answers from
# what the tools actually fetched, not from what the prose claims.
#
# SCOPE LIMIT, STATED HONESTLY (guard-4407): this binds to the WebFetch and
# WebSearch TOOLS. A page pulled with `curl` from a Bash call leaves no entry
# here, exactly as a `cat` leaves none in the read manifest. A miss therefore
# means "no tool-fetch record", never "this URL was fabricated".
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

# ONE pass over stdin — a hook payload arrives on a pipe and cannot be rewound,
# so everything needed is extracted here (this is why the sibling hook packs
# partial|sid|path into a single line). Line 1 is the session id; every later
# line is `kind<TAB>value`.
#   WebFetch  -> the url
#   WebSearch -> the query, plus the result URLs it actually returned
# RESULT_CAP bounds the work one tool call can cause; a search returning more
# links records the first RESULT_CAP and stops.
payload=$(RESULT_CAP=10 python3 "$CORE_ROOT/scripts/_fetch_provenance_extract.py" 2>/dev/null) || exit 0

[ -n "$payload" ] || exit 0
session_id=$(printf '%s\n' "$payload" | head -1)
records=$(printf '%s\n' "$payload" | tail -n +2)
[ -n "$records" ] || exit 0

sid_arg=""
[ -n "$session_id" ] && sid_arg="--session-id $session_id"

# Resolve agent from session_id — MIND_AGENT is not injected in these hooks.
# ORDER-CRITICAL: must stay BEFORE `source _platform.sh`; MSYS_NO_PATHCONV
# (set by _platform.sh) breaks session-binding-read.sh on Git Bash ().
AGENT_NAME="${MIND_AGENT:-}"
if [ -z "$AGENT_NAME" ] && [ -n "$session_id" ]; then
    AGENT_NAME="$(bash "$CORE_ROOT/scripts/session-binding-read.sh" "$session_id" 2>/dev/null || true)"
fi

source "$CORE_ROOT/scripts/_platform.sh"

# Fail-open on every record: a provenance miss must never break the tool call
# whose result the user is waiting on.
while IFS=$'\t' read -r kind value; do
    [ -n "${value:-}" ] || continue
    env MIND_AGENT="${AGENT_NAME:-}" python3 "$CORE_ROOT/scripts/context-reads.py" \
        record-prov $sid_arg --kind "$kind" "$value" >/dev/null 2>&1 || true
done <<< "$records"

exit 0
