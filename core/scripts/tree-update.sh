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
# --encoding-source / --encoding-reason are ACCEPTED but no-ops: they drove the
# CLI's L1-pick-log telemetry (_log_l1_pick_for_key), a fail-open append to a
# SEPARATE file that the daemon endpoint defers (no _tree.yaml byte impact). See
# tree_write.py "DELIBERATELY NOT YET IMPLEMENTED" + mycelium-api-impl.md.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
OP=""
declare -a BODY_ARGS=()
BACKLOG_MODE=false
STOP_MODE=false
WITH_RUN_RECORD=false
NO_DEDUP=false
ACCEPT_OVERFLOW=""
HAVE_ACCEPT=false

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
        --encoding-source|--encoding-reason)
            # Accepted, no-op (daemon defers the L1-pick-log telemetry append).
            shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

if [ -z "$OP" ]; then
    echo "Specify an update subcommand. Use --help for options." >&2
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
    STDIN_DATA="$(cat)"
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
    $(rt_python_launcher) -c '
import json, os, sys
op = os.environ["TU_OP"]
a = sys.argv[1:]
body = {"op": op}
if op == "set":
    body["key"], body["field"], body["value"] = a[0], a[1], a[2]
elif op == "add-child":
    body["parent"] = a[0]
    s = os.environ.get("TU_STDIN", "")
    body["child"] = json.loads(s) if s.strip() else {}
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
    print(json.dumps(resp["node"], indent=2, ensure_ascii=False))
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

BODY="$(_build_body)"

rc=0
RESPONSE="$(rt_call POST /v1/tree/write --body-string "$BODY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
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
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
            if [ "$rc" = "2" ]; then printf '%s\n' "$RESPONSE" >&2; exit 1; fi
        fi
        rt_no_daemon_error "tree-update.sh";;
    *) exit $rc;;
esac
