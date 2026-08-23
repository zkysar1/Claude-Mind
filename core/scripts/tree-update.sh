#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# tree-update — daemon-aware dispatcher for EVERY tree WRITE op. Mirrors
# `tree.py update --<op>` for each subcommand, POSTing to /v1/tree/write and
# translating the daemon {ok,op,...} envelope back to the EXACT CLI stdout
# shape (per-op; byte-compat with the CLI's json.dumps(...) — note remove-child
# is single-line, every other op is indent=2; all ensure_ascii=False).
#
# Ops:
#   --set KEY FIELD VALUE
#   --add-child PARENT          (child JSON on stdin; +--no-dedup +--accept-overflow)
#   --remove-child PARENT CHILD
#   --increment KEY FIELD
#   --batch                     (operations JSON on stdin)
#   --propagate KEY
#   --reconcile-capabilities
#   --reparent NODE NEW_PARENT
#   --record-maintenance        (+--backlog-mode +--stop-mode +--with-run-record
#                                with run-record JSON on stdin)
#
# --encoding-source / --encoding-reason are FORWARDED as encoding_source /
# encoding_reason in the POST body (): the daemon now appends the
# S9 L1-pick-log telemetry itself (tree_write.py → _l1_pick.py SSOT) for
# add-child / batch / reparent. Fail-open on the daemon side — a telemetry
# error never blocks the tree write. They were no-ops 2026-05-28→2026-07-12
# while the daemonization deferred the append (log went silent that window).
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Usage ----------------------------------------------------------------
# Printed by --help (exit 0) and by a bare/unrecognized invocation (exit 1/2).
# Before  both paths emitted "Use --help for options." and --help hit
# the catch-all, so --help answered itself and named nothing — a dead end that
# routed callers into guessing the call shape, and the specific guess that hangs
# this wrapper is passing the JSON payload positionally (see the argv reject).
# NOT the discovery mechanism of record: guard-136 / guard-2172 / guard-2350 say
# derive a wrapper's surface from its parsing block, and
# `py -3 core/scripts/wrapper-surface.py describe tree-update.sh` does that
# mechanically. This text is the reflex-path courtesy, not a substitute.
_usage() {
    cat <<'USAGE'
tree-update.sh — daemon-aware dispatcher for tree WRITE ops.

  --set KEY FIELD VALUE
  --add-child PARENT              child JSON on STDIN   [+--no-dedup +--accept-overflow N]
  --remove-child PARENT CHILD
  --increment KEY FIELD
  --batch                         operations JSON on STDIN
  --propagate KEY
  --reconcile-capabilities
  --reparent NODE NEW_PARENT
  --record-maintenance            [+--backlog-mode +--stop-mode]
                                  [+--with-run-record: run-record JSON on STDIN]

Shared flags: --encoding-source SRC, --encoding-reason REASON

JSON payloads go on STDIN, never as a positional argument (guard-2037):
  echo '{"key":"my-node","summary":"..."}' | bash core/scripts/tree-update.sh --add-child parent-key

Authoritative surface: py -3 core/scripts/wrapper-surface.py describe tree-update.sh
USAGE
}

# --- Parse args -----------------------------------------------------------
OP=""
declare -a BODY_ARGS=()
BACKLOG_MODE=false
STOP_MODE=false
WITH_RUN_RECORD=false
NO_DEDUP=false
ACCEPT_OVERFLOW=""
HAVE_ACCEPT=false
ENC_SOURCE=""
ENC_REASON=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --set)
            OP=set
            BODY_ARGS=("${2-}" "${3-}" "${4-}")
            shift $(( $# >= 4 ? 4 : $# ));;
        --add-child)
            OP=add-child
            BODY_ARGS=("${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --remove-child)
            OP=remove-child
            BODY_ARGS=("${2-}" "${3-}")
            shift $(( $# >= 3 ? 3 : $# ));;
        --increment)
            OP=increment
            BODY_ARGS=("${2-}" "${3-}")
            shift $(( $# >= 3 ? 3 : $# ));;
        --batch)
            OP=batch
            shift;;
        --propagate)
            OP=propagate
            BODY_ARGS=("${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --reconcile-capabilities)
            OP=reconcile-capabilities
            shift;;
        --reparent)
            OP=reparent
            BODY_ARGS=("${2-}" "${3-}")
            shift $(( $# >= 3 ? 3 : $# ));;
        --record-maintenance)
            OP=record-maintenance
            shift;;
        --backlog-mode)  BACKLOG_MODE=true; shift;;
        --stop-mode)     STOP_MODE=true; shift;;
        --with-run-record) WITH_RUN_RECORD=true; shift;;
        --no-dedup)      NO_DEDUP=true; shift;;
        --accept-overflow)
            HAVE_ACCEPT=true
            ACCEPT_OVERFLOW="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --encoding-source)
            ENC_SOURCE="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --encoding-reason)
            ENC_REASON="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --help|-h)
            _usage
            exit 0;;
        *)
            # ARGV-SHAPE REJECT — guard-3393 door (a), mirroring
            # aspirations-add-goal.sh's "is not a CLI flag for this script".
            # The previous `*) shift;;` SILENTLY DISCARDED unrecognized args,
            # which is how a positionally-passed JSON payload disappears: the
            # body is dropped, execution falls through to the stdin read with
            # nothing piped, and the call then either wedges forever (open-but-
            # idle stdin) or returns an opaque missing_param (closed stdin) —
            # identical input, two outcomes, decided only by an inherited
            # descriptor. Rejecting here never touches stdin, so it stays safe
            # for hook-wired callers. Door (b) is closed separately below;
            # neither guard substitutes for the other.
            echo "Error: '$1' is not a recognized argument for this script." >&2
            echo "       JSON payloads go on STDIN, not as a positional (guard-2037)." >&2
            echo "Run: bash $0 --help" >&2
            exit 2;;
    esac
done

if [ -z "$OP" ]; then
    echo "Error: no update subcommand given." >&2
    _usage >&2
    exit 1
fi

# Per-op required-positional validation (mirrors argparse nargs requirements).
case "$OP" in
    set)
        if [ -z "${BODY_ARGS[0]-}" ] || [ -z "${BODY_ARGS[1]-}" ] || [ "${#BODY_ARGS[@]}" -lt 3 ]; then
            echo "Error: --set requires KEY FIELD VALUE." >&2; exit 1
        fi;;
    add-child|propagate)
        if [ -z "${BODY_ARGS[0]-}" ]; then
            echo "Error: --$OP requires its key argument." >&2; exit 1
        fi;;
    remove-child|increment|reparent)
        if [ -z "${BODY_ARGS[0]-}" ] || [ -z "${BODY_ARGS[1]-}" ]; then
            echo "Error: --$OP requires two arguments." >&2; exit 1
        fi;;
esac

# --- Read stdin for ops that take a JSON blob -----------------------------
STDIN_DATA=""
_needs_stdin=false
case "$OP" in
    add-child|batch) _needs_stdin=true;;
    record-maintenance) [ "$WITH_RUN_RECORD" = true ] && _needs_stdin=true;;
esac
if [ "$_needs_stdin" = true ] && [ ! -t 0 ]; then
    # BOUNDED READ — guard-3393 door (b) /  / guard-664 bash twin.
    # `[ ! -t 0 ]` distinguishes a terminal from a non-terminal; it does NOT
    # promise EOF. A backgrounded Bash task inherits an open, never-closing
    # stdin, so the bare `STDIN_DATA="$(cat)"` this replaces blocked until the
    # harness timeout and landed nothing — silent in the worst direction, since
    # the phase looks busy and an agent that does not re-read the tree records
    # the close as encoded. Reproduced here at rc=124 before the fix.
    # Probe the FIRST line with a bounded timeout: real piped callers
    # (`echo '<json>' | ...`) have data in the pipe buffer at exec, so the
    # timeout never fires for them; an idle inherited descriptor times out and
    # degrades to an empty payload, which the daemon rejects FAST and loudly
    # (missing_param) instead of wedging. `|| [ -n "$first_chunk" ]` keeps
    # single-line input that lacks a trailing newline (read exits nonzero on
    # EOF but still fills the var).
    _first_chunk=""
    _rc_read=0
    IFS= read -r -t 2 _first_chunk || _rc_read=$?
    if [ "$_rc_read" -eq 0 ] || [ -n "$_first_chunk" ]; then
        _rest="$(cat)"
        if [ -n "$_rest" ]; then
            STDIN_DATA="$_first_chunk"$'\n'"$_rest"
        else
            STDIN_DATA="$_first_chunk"
        fi
    elif [ "$_rc_read" -gt 128 ]; then
        echo "tree-update.sh: stdin open but idle after 2s — proceeding without the --$OP payload (backgrounded-task guard, g-115-2291)" >&2
    fi
    # _rc_read == 1 with an empty var (immediate EOF, e.g. </dev/null): silent —
    # the caller genuinely sent nothing, and the daemon's missing_param is the
    # correct answer.
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# Build the request body in Python (safe quoting; argv carries positionals,
# env carries flags + stdin). Mirrors the per-op argparse → body mapping.
_build_body() {
    # shellcheck disable=SC2086
    TU_OP="$OP" TU_BACKLOG="$BACKLOG_MODE" TU_STOP="$STOP_MODE" \
    TU_WRR="$WITH_RUN_RECORD" TU_NODEDUP="$NO_DEDUP" \
    TU_HAVE_ACCEPT="$HAVE_ACCEPT" TU_ACCEPT="$ACCEPT_OVERFLOW" \
    TU_STDIN="$STDIN_DATA" \
    TU_ENC_SOURCE="$ENC_SOURCE" TU_ENC_REASON="$ENC_REASON" \
    $(rt_python_launcher) -c '
import json, os, sys
op = os.environ["TU_OP"]
a = sys.argv[1:]
body = {"op": op}
# S9 telemetry passthrough (): the daemon reads these top-level for
# add-child/batch/reparent; harmless-ignored elsewhere. Only set when given.
for flag, key in (("TU_ENC_SOURCE", "encoding_source"),
                  ("TU_ENC_REASON", "encoding_reason")):
    v = os.environ.get(flag, "")
    if v:
        body[key] = v
if op == "set":
    body["key"], body["field"], body["value"] = a[0], a[1], a[2]
    # : declare the byte length WE were handed, so the daemon can
    # refuse a write whose value lost bytes between here and there. This is
    # the only length in the chain derived independently of the daemon copy —
    # an in-daemon comparison is vacuous (it compares the request value to
    # itself). The 2026-08-19 incident lost 9,522 bytes somewhere at or before
    # this serialization and returned rc=0 with a complete-looking echo.
    body["value_bytes"] = len(a[2].encode("utf-8"))
elif op == "add-child":
    body["parent"] = a[0]
    s = os.environ.get("TU_STDIN", "")
    body["child"] = json.loads(s) if s.strip() else {}
    # rb-8572: body-bearing keys in the child JSON mean the caller expects
    # add-child to write the .md body. It registers the INDEX ONLY — the
    # daemon would silently drop these keys and an orphan node results
    # (file: path 404s). Refuse at the door with the correct flow.
    _bk = [k for k in ("content", "body", "markdown") if k in body["child"]]
    if _bk:
        sys.stderr.write(
            "Refusing --add-child: child JSON carries body-bearing key(s) "
            + ", ".join(repr(k) for k in _bk)
            + " — add-child registers the INDEX ONLY and would silently drop "
            "the content (rb-8572 orphan-node class). Write the .md body "
            "first (front matter + content) at the node file: path, then "
            "register; or use the /tree add flow, which does both.\n")
        sys.exit(1)
    if os.environ.get("TU_NODEDUP") == "true":
        body["no_dedup"] = True
    if os.environ.get("TU_HAVE_ACCEPT") == "true":
        body["accept_overflow"] = os.environ.get("TU_ACCEPT", "")
elif op == "remove-child":
    body["parent"], body["child_key"] = a[0], a[1]
elif op == "increment":
    body["key"], body["field"] = a[0], a[1]
elif op == "batch":
    s = os.environ.get("TU_STDIN", "") or "{}"
    data = json.loads(s)
    body["operations"] = (data.get("operations", [])
                          if isinstance(data, dict) else data)
elif op == "propagate":
    body["key"] = a[0]
elif op == "reconcile-capabilities":
    pass
elif op == "reparent":
    body["key"], body["new_parent"] = a[0], a[1]
elif op == "record-maintenance":
    body["backlog_mode"] = os.environ.get("TU_BACKLOG") == "true"
    body["stop_mode"] = os.environ.get("TU_STOP") == "true"
    if os.environ.get("TU_WRR") == "true":
        body["with_run_record"] = True
        s = os.environ.get("TU_STDIN", "")
        body["run_record_input"] = json.loads(s) if s.strip() else {}
sys.stdout.write(json.dumps(body))
' "${BODY_ARGS[@]+"${BODY_ARGS[@]}"}"
}

# Translate the daemon {ok,op,...} envelope to the exact CLI stdout shape.
_translate() {
    # shellcheck disable=SC2086
    # B11: the env-prefix MUST sit on the python (RIGHT of the pipe), not on
    # printf (left). `TU_OP=.. printf .. | python` exported TU_OP to printf
    # only, so the python child KeyError'd on os.environ["TU_OP"] — and since
    # the daemon write (rt_call above) had ALREADY landed, --set /
    # --record-maintenance threw at this local response-formatter while the
    # write itself succeeded (3-agent report: alpha-1787, zeta-1786, bravo).
    # Mirrors _build_body's working env-on-the-launcher form.
    printf '%s' "$1" | TU_OP="$OP" $(rt_python_launcher) -c '
import json, os, sys
op = os.environ["TU_OP"]
resp = json.load(sys.stdin)
if op in ("set", "add-child", "increment"):
    # CLI prints apply_defaults(node) + key; the daemon returns exactly that
    # in resp["node"] (md_written / ancestors / capability_changes already
    # folded in for the relevant ops).
    # rb-8572/guard-4578: fold response-level advisories INTO the printed
    # node — printing resp["node"] alone stripped body_presence_warning at
    # this exact hop, which is how two index-only orphan nodes shipped past
    # a daemon that was warning correctly the whole time.
    node_out = resp["node"]
    if resp.get("body_presence_warning"):
        node_out["body_presence_warning"] = resp["body_presence_warning"]
    print(json.dumps(node_out, indent=2, ensure_ascii=False))
elif op == "remove-child":
    # CLI: single-line, NO indent=2 (the lone exception).
    print(json.dumps({"removed": resp["removed"], "parent": resp["parent"]},
                     ensure_ascii=False))
elif op == "batch":
    prop = resp.get("propagate") or []
    if not prop:
        # Backward-compat: plain array when no propagate ops.
        print(json.dumps(resp["updated_nodes"], indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"updated_nodes": resp["updated_nodes"],
                          "propagate": prop}, indent=2, ensure_ascii=False))
elif op == "propagate":
    print(json.dumps({"source_node": resp["source_node"],
                      "ancestors_updated": resp["ancestors_updated"],
                      "capability_changes": resp["capability_changes"]},
                     indent=2, ensure_ascii=False))
elif op == "reconcile-capabilities":
    print(json.dumps({"reconciled": resp.get("reconciled", 0),
                      "total_nodes": resp.get("total_nodes", 0),
                      "changes": resp.get("changes", [])},
                     indent=2, ensure_ascii=False))
elif op == "reparent":
    # CLI result key order = reparented, old_parent, new_parent, new_depth,
    # file_moves, old_chain_propagation, new_chain_propagation. The daemon
    # returns those in the SAME order after ok/op (dropped here).
    out = {k: v for k, v in resp.items() if k not in ("ok", "op")}
    print(json.dumps(out, indent=2, ensure_ascii=False))
elif op == "record-maintenance":
    print(json.dumps(resp["result"], indent=2, ensure_ascii=False))
'
}

# : end-to-end value-length assertion, the CLIENT half of the
# integrity check. BODY carries the length we computed from argv; RESPONSE
# carries the length the daemon actually stored. Those two numbers were derived
# independently on opposite sides of the wire, which is the whole point — the
# in-daemon comparison everyone reaches for first is a string compared to
# itself (_apply_set does node[field] = the request value) and passes 100% of
# the time while looking like protection.
#
# Runs AFTER _translate deliberately: the write has already landed by then, so
# stdout must still carry the node JSON every existing caller parses. What this
# adds is a non-zero EXIT plus a loud stderr line, so a silent partial write
# stops being silent. Detection, not prevention — prevention is the daemon's
# pre-write refusal (guard-3150).
#
# FAIL-OPEN on anything unexpected (unparseable JSON, older daemon that does not
# report value_bytes, non-string value). A false alarm here would fire on every
# tree write in the fleet; a miss costs one undetected write. Given the check is
# new and the write path is the busiest in the framework, that asymmetry is the
# right one — but it does mean an older daemon silently provides NO coverage.
_assert_value_bytes() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | TU_BODY="$BODY" $(rt_python_launcher) -c '
import json, os, sys
try:
    sent = json.loads(os.environ["TU_BODY"]).get("value_bytes")
    got = json.load(sys.stdin).get("value_bytes")
except Exception:
    sys.exit(0)
if sent is None or got is None or sent == got:
    sys.exit(0)
sys.stderr.write(
    "tree-update.sh: VALUE INTEGRITY FAILURE — sent %d bytes, daemon stored %d "
    "(lost %d). The write LANDED SHORT; the node now holds a truncated value. "
    "Recover the prior value from world/.history (read the pointer at "
    ".history/snapshots/<path>/<ts>.yaml, gunzip "
    ".history/blobs/<hash[:2]>/<hash[2:]>.gz, pull the single field) and "
    "re-write via --batch. Do NOT use history.py restore: it reverts the whole "
    "_tree.yaml and clobbers concurrent partner writes. See g-115-6823.\n"
    % (sent, got, sent - got))
sys.exit(1)
'
}

BODY="$(_build_body)"

rc=0
RESPONSE="$(rt_call POST /v1/tree/write --body-string "$BODY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; _assert_value_bytes "$RESPONSE"; exit $?;;
    2)
        # 4xx/5xx terminal refusal — print the daemon body to stderr, exit 1.
        printf '%s\n' "$RESPONSE" >&2
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback. One
        # auto-spawn attempt, then fail loud. See no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/tree/write --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; _assert_value_bytes "$RESPONSE"; exit $?; fi
            if [ "$rc" = "2" ]; then printf '%s\n' "$RESPONSE" >&2; exit 1; fi
        fi
        rt_no_daemon_error "tree-update.sh";;
    *) exit $rc;;
esac
