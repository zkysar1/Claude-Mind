#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-complete — daemon-aware wrapper (PR 9a).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse args + read stdin body (if --intent-satisfied)
#   3. POST /v1/aspirations/complete with params mapped to query string
#   4. On 200, re-emit `warnings[]` to stderr and print `aspiration` to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Shared unknown-flag refusal (, the sweep  mandated).
# Sourced BEFORE _runtime.sh so the refusal is cheap and cannot be masked by a
# daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
# ONE literal, referenced by BOTH the --help arm and the refusal, so the two
# strings that must agree cannot drift apart ( fresh-eyes F-002).
# HAND-ENUMERATED, deliberately: unknown-flag-caller-scan.py's `accepted_flags`
# for THIS wrapper is POLLUTED — its arm parser mis-reads the rt_call
# continuation lines below as case arms and reports '--query "$QUERY' plus two
# multi-line '--body-string' fragments alongside the real flags. That direction
# is OVER-acceptance, which can MASK a genuine unknown flag, so the scan was NOT
# trusted here; the three flags below come from reading the arg loop.
_ACCEPTED_FLAGS="--source <world|agent> | --force | --intent-satisfied | --needle-satisfied | --override-supply-close <why> | --override-all <why>"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
ASP_ID=""
FORCE=0
INTENT_SATISFIED=0
NEEDLE_SATISFIED=0
declare -a HEADERS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --force)
            FORCE=1
            shift;;
        --intent-satisfied)
            INTENT_SATISFIED=1
            shift;;
        --needle-satisfied)
            # : the record carries supply_evidence.needle and the
            # needle IS met — stdin is {"statement": "<how the delivered work
            # gives the operator what the needle names>", "artifacts": ["<file
            # / tree node / board post the operator uses — not a goal id>"]}.
            # Without it the daemon REFUSES (aspiration_needle_unmet): a
            # self-generated aspiration does not close on goal count.
            NEEDLE_SATISFIED=1
            shift;;
        --override-supply-close)
            # Audited bypass of the closure gate () — ledgered to
            # world/aspiration-supply-overrides.jsonl (kind: close).
            HEADERS+=(--header "X-Mind-Override-Supply-Close: ${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-all)
            # Bulk header; the daemon fans it into the closure slot when unset
            # and audits it to world/override-bypass-ledger.jsonl.
            HEADERS+=(--header "X-Mind-Override-All: ${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "<asp-id>" \
                "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). This arm used to append the unknown flag to
            # PASSTHROUGH on the strength of a comment reading "Unknown flag —
            # passthrough for argparse on fallback". BOTH halves were false:
            # PASSTHROUGH had NO READER anywhere in this file (appended in four
            # arms, consumed in none — the daemon path builds QUERY from
            # SOURCE_VAL/FORCE/INTENT_SATISFIED alone at the QUERY= lines
            # below), and the "fallback" it names was DELETED in the 2026-05-14
            # daemon-only cutover named in this file's own header, twelve lines
            # above the arm that promised it. PASSTHROUGH_SOURCE was write-only
            # too. Measured on this box before the fix:
            # `--bogus-flag asp-zzz-refusal-fixture` returned rc=1 having
            # silently swallowed the flag and reached the daemon, which rejected
            # only the id — the unknown flag was never mentioned.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # POSITIONALS stay accepted-and-ignored past the first, the same
            # boundary the aspirations-read.sh / pipeline-read.sh /
            # tree-find-node.sh adoptions drew (guard-1562: never ship a
            # refusal without enumerating what would newly fire). The documented
            # call form IS positional — `aspirations-complete.sh <asp-id>` and
            # the agent-aspirations-complete.sh forwarder, which execs this
            # script with `--source agent "$@"` — so this arm is load-bearing
            # and must not become a refusal. The required-arg guard below
            # already exits 1 when no id is passed at all.
            [ -z "$ASP_ID" ] && ASP_ID="$1"
            shift;;
    esac
done

# Missing asp_id → error
if [ -z "$ASP_ID" ]; then
    echo "Error: asp_id is required." >&2
    exit 1
fi

# Read stdin BEFORE invoking the daemon. If --intent-satisfied, the body is
# the intent_satisfaction JSON block; if --needle-satisfied (), the
# needle_satisfaction block; otherwise body is empty.
BODY=""
if [ "$INTENT_SATISFIED" = "1" ] || [ "$NEEDLE_SATISFIED" = "1" ]; then
    BODY="$(cat)"
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="asp_id=${ASP_ID}&source=${SOURCE_VAL}"
[ "$FORCE" = "1" ] && QUERY="${QUERY}&force=true"
[ "$INTENT_SATISFIED" = "1" ] && QUERY="${QUERY}&intent_satisfied=true"
[ "$NEEDLE_SATISFIED" = "1" ] && QUERY="${QUERY}&needle_satisfied=true"

rc=0
if [ -n "$BODY" ]; then
    RESPONSE="$(rt_call POST /v1/aspirations/complete \
        --query "$QUERY" \
        --body-string "$BODY" \
        "${HEADERS[@]+"${HEADERS[@]}"}")" || rc=$?
else
    RESPONSE="$(rt_call POST /v1/aspirations/complete \
        --query "$QUERY" \
        "${HEADERS[@]+"${HEADERS[@]}"}")" || rc=$?
fi

case $rc in
    0)
        # 200: parse response. Re-emit warnings[] to stderr and print the
        # aspiration record to stdout (matches legacy CLI json.dumps shape).
        #  fix: route response via stdin (was argv). Windows argv
        # limit is ~32KB; large archived aspirations ( had 23 goals
        # ~57KB serialized) hit "Argument list too long" at exec time. The
        # daemon-side archival had already succeeded but the wrapper exited
        # non-zero from this print failure, leaving callers thinking the
        # operation failed. stdin path has no length limit. The 17 sibling
        # daemon wrappers share this shape; tracked as a follow-up Idea.
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            if [ -n "$BODY" ]; then
                RESPONSE="$(rt_call POST /v1/aspirations/complete \
                    --query "$QUERY" \
                    --body-string "$BODY" \
                    "${HEADERS[@]+"${HEADERS[@]}"}")" || rc=$?
            else
                RESPONSE="$(rt_call POST /v1/aspirations/complete \
                    --query "$QUERY" \
                    "${HEADERS[@]+"${HEADERS[@]}"}")" || rc=$?
            fi
            if [ "$rc" = "0" ]; then
                #  fix: stdin route (same rationale as success path above).
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-complete.sh";;
    *)
        exit $rc;;
esac
