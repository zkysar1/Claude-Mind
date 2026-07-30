#!/usr/bin/env bash
# core/scripts/probe-staleness-leak.sh - domain-agnostic deploy-chain
# staleness-leak detector.
#
# Reads an ordered chain of deploy stages (upstream->downstream) from STDIN as a
# JSON array; each element: {"name": str, "state": "fresh"|"stale"|"unverified",
# "detail": str}. Flags a STALENESS LEAK at any stage that is "stale" while some
# UPSTREAM stage is "fresh" -- i.e. a deploy that succeeded upstream but never
# propagated downstream.
#
# The asymmetry is the whole point (, from the 2026-05-25 false
# "roblox-down-10-days" alarm): a downstream stage stale-while-upstream-fresh is a
# REAL leak; ALL stages uniformly stale is NOT a leak, just a quiet period with
# nothing to propagate. Diffing the chain distinguishes the two -- a single stale
# read in isolation could not.
#
# Design constraints honored:
#   - rb-611: THREE-WAY verdict (ok / leak / unverified). "cannot tell" (a stage
#     whose state could not be determined) is never collapsed into "leak".
#   - guard-647 (live state): this engine is pure diff logic over the states it is
#     handed; the SHIM that feeds it is responsible for reading CURRENT stage state
#     every call (no caching, no stored-verdict reuse).
#   - domain-free (.claude/rules/domain-free-examples.md): stage names + states are
#     INPUT, never hardcoded. The concrete the framework deploy chain lives in a domain
#     shim (e.g. world/scripts/probe-deploy-chain.sh) that gathers each stage's
#     live state and wires this engine into infra-health as a component.
#   - rb-1903: engine-in-core (this file, reusable) + shim-in-world (the concrete
#     chain) split.
#
# Part of the deploy-loop health probes ( / ).
#
# Usage:  <chain-json-array> | probe-staleness-leak.sh [--stale-hours N]
# Output: JSON {status, stale_hours, checked, summary, leaks[], unverified[], chain[]}
# Exit:   0 = JSON emitted (any verdict)   1 = usage error (no/invalid chain)
set -uo pipefail

STALE_HOURS=24
while [ $# -gt 0 ]; do
    case "$1" in
        --stale-hours) STALE_HOURS="${2:-24}"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --stale-hours=*) STALE_HOURS="${1#*=}"; shift ;;
        --*) shift ;;
        *) shift ;;
    esac
done

PROBE_CHAIN_JSON="$(cat)"
export PROBE_CHAIN_JSON
export PROBE_STALE_HOURS="$STALE_HOURS"

# Heredoc-quoted ('PYEOF') so bash performs NO expansion on the Python source
# (guard-165: values cross the boundary via env, never string interpolation).
py -3 <<'PYEOF'
import os, sys, json

raw = os.environ.get("PROBE_CHAIN_JSON", "").strip()
try:
    stale_hours = float(os.environ.get("PROBE_STALE_HOURS", "24"))
except ValueError:
    stale_hours = 24.0

if not raw:
    print(json.dumps({"status": "unverified", "reason": "no_chain",
                      "detail": "no chain JSON on stdin"}))
    sys.exit(1)
try:
    chain = json.loads(raw)
except json.JSONDecodeError as e:
    print(json.dumps({"status": "unverified", "reason": "bad_chain",
                      "detail": "invalid chain JSON: " + str(e)[:120]}))
    sys.exit(1)
if not isinstance(chain, list) or not chain:
    print(json.dumps({"status": "unverified", "reason": "empty_chain",
                      "detail": "chain must be a non-empty JSON array"}))
    sys.exit(1)

VALID = {"fresh", "stale", "unverified"}
leaks = []
unverified = []
upstream_fresh = []   # names of upstream stages seen fresh so far (in order)
norm_chain = []

for i, stage in enumerate(chain):
    if not isinstance(stage, dict):
        stage = {}
    name = stage.get("name") or "stage-{}".format(i)
    state = stage.get("state", "unverified")
    detail = stage.get("detail", "")
    if state not in VALID:
        state = "unverified"
    norm_chain.append({"name": name, "state": state, "detail": detail})
    if state == "unverified":
        unverified.append(name)
    elif state == "stale" and upstream_fresh:
        # Downstream stage stale while an upstream stage is fresh: LEAK.
        leaks.append({"stage": name, "detail": detail,
                      "upstream_fresh": list(upstream_fresh)})
    if state == "fresh":
        upstream_fresh.append(name)

# THREE-WAY verdict (rb-611): a leak always wins; otherwise an undeterminable
# stage downgrades the whole verdict to unverified rather than a false "ok".
if leaks:
    status = "leak"
elif unverified:
    status = "unverified"
else:
    status = "ok"

fresh_n = sum(1 for s in norm_chain if s["state"] == "fresh")
stale_n = sum(1 for s in norm_chain if s["state"] == "stale")
summary = "{} fresh, {} stale, {} unverified, {} leak(s) (of {} stages; stale>{}h)".format(
    fresh_n, stale_n, len(unverified), len(leaks), len(norm_chain), int(stale_hours))

print(json.dumps({
    "status": status,
    "stale_hours": stale_hours,
    "checked": len(norm_chain),
    "summary": summary,
    "leaks": leaks,
    "unverified": unverified,
    "chain": norm_chain,
}))
PYEOF
