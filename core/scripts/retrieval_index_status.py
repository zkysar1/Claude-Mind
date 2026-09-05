#!/usr/bin/env python3
"""Per-box retrieval-index IDENTITY: publish it locally, census it fleet-wide ().

THE DEFECT CLASS. A PER-BOX DERIVED CACHE whose correctness depends on a SHARED
config value has no parity check by construction: the shared half is identical
everywhere so config-parity tooling sees nothing, and the derived half is local
so no shared store sees it. Measured 2026-09-03 (g-115-8779): a box served
retrieval from an index built on BAAI/bge-small-en-v1.5 while tree.yaml named
all-MiniLM-L6-v2. Both cosine floors are calibrated on MiniLM, so under bge
98.9-100% of nodes cleared the floor and ranking collapsed to the static terms
(hit@1 0, MRR 0.094). The index was FRESH, the daemon healthy, the channel read
'alive', and every liveness probe passed -- invisible by construction, because
no probe compared the index's model to the configured one. Five weeks.

The generalizable remedy is to publish the CALIBRATION ANCHOR: which shared
value the local artifact was built against. That is what `local_block()` emits.

TWO LEGS, AND THEY ARE DELIBERATELY ASYMMETRIC (guard-1414: fixing either alone
still shows nothing, and each is individually verifiable, so a one-leg close can
honestly report success while the reader stays blind).

  DATA leg  -- runs on EVERY box. heartbeat-tick.sh publishes local_block() into
               agent_status.<agent>.retrieval_index, ABOVE its agent-state gate,
               because a cross-box worker Body is IDLE by design and a publish
               below the gate would never run on the boxes least likely to be
               noticed. Same hoist and same reason as core_hooks_path.
  SCOPE leg -- runs on ONE box. census() iterates every agent row. The watchdog
               wiring is reducer-only on the precedent of agent-watchdog.py's
               own comment: a worker COULD run it, but two Bodies polling one
               fleet condition means two alerts for one fault.

WHY THE BLOCK CARRIES ITS HOSTNAME. `agent_status.<agent>` is AGENT-keyed with
no sid and no box, and the per-agent shard merges WHOLE-ROW newest-wins (never
field-stitch -- _team_state.py). So one agent spanning two boxes writes both
answers into ONE key and the row keeps whichever wrote last. A box-keyed submap
would NOT fix this: whole-row newest-wins discards the losing row entirely, so
the submap cannot accumulate across boxes either. Carrying `box` does not make
the key hold N boxes -- it makes the one value it holds honest, so a reader can
say WHICH box answered instead of averaging them. Same reasoning, verbatim in
shape, as the core_hooks_path publish this sits beside.

WHY THE CENSUS READS AUTHORITATIVE. On own-cloud the local shard mirror is
conflict-skipped and frozen for PEER shards, so a plain load_rows() returns
stale OR ABSENT peer rows (measured: peer shards 7 days stale; two agents'
shards absent locally but present and fresh in the store). A mirror-vs-mirror
census passes while blind -- which is the exact failure this goal exists to
close, so census() uses load_rows_authoritative_with_provenance() and REPORTS
the provenance rather than hiding it.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_INDEX_DIR = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"

#: Verdicts census() assigns per agent. `missing` is a first-class answer, not an
#: error: guard-4455 -- a null from a team-state field read is NOT evidence of
#: absence, so the census names the gap explicitly instead of returning nothing.
VERDICT_OK = "ok"
VERDICT_DRIFTED = "DRIFTED"
VERDICT_CHANNEL = "channel-not-alive"
VERDICT_MISSING = "missing"

#: Hours of `last_active` staleness past which a row is not counted as a LIVE
#: agent. 6h is not a number invented here -- it is the fleet's documented
#: partner-liveness threshold (.claude/rules/check-team-state-before-silent.md),
#: reused so this census and every other liveness consumer agree on the word.
#:
#: It earns its keep immediately: measured on this box 2026-09-03, 15 shards
#: carry 5 live agents and 10 pieces of test residue (`test-race-0`,
#: `zz-hbtest`, `no-such-agent-xyz`, ... at 111h-892h stale or no last_active at
#: all). Counting those as fleet gaps would make "block missing" fire on ten
#: rows that will never publish one, on every tick, forever -- an alert nobody
#: can ever drive to zero is an alert people learn to skip.
LIVE_THRESHOLD_HOURS = 6.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _hostname() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _is_live(row) -> bool:
    """True when the row's `last_active` is within LIVE_THRESHOLD_HOURS.

    An UNPARSEABLE or absent `last_active` reads NOT-live, and that direction is
    deliberate: this predicate only ever de-escalates a report (a non-live agent
    drops out of the actionable lists but stays in the full per-agent map), so
    failing toward "not live" can hide nothing that the census does not still
    print. Failing the other way would resurrect ten dead test rows into a
    permanent alert.
    """
    if not isinstance(row, dict):
        return False
    la = row.get("last_active")
    if not la:
        return False
    try:
        ts = datetime.fromisoformat(str(la))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        # Naive stamps are UTC wall time by fleet fiat (CLAUDE.md Naming Rules,
        # TZ=UTC on every box), so attach UTC rather than assuming local.
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= LIVE_THRESHOLD_HOURS * 3600


def configured_model() -> str:
    """The model tree.yaml says this fleet's indexes should be built on.

    Calls embedding-index-build.resolve_model_name(), which is the SSOT for the
    g-306-82 precedence (--model > tree.yaml embedding_model_name > fallback).
    Imported by path because the filename carries hyphens. Re-deriving the
    precedence here would be a second implementation that drifts silently the
    first time the builder's rule changes (guard-2676).
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_eib_for_status", str(SCRIPT_DIR / "embedding-index-build.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.resolve_model_name()
    except Exception:
        # Last-resort fallback mirrors the builder's own MODEL_NAME constant.
        # Kept deliberately: a status reader that raises would take the
        # heartbeat down with it, and the whole point is visibility.
        return "all-MiniLM-L6-v2"


def channel_status() -> str:
    """retrieve.embedding_channel_status() -- 'off' | 'alive' | 'DEAD: ...'.

    A scoped call into the shared component, never a reimplementation: the
    channel predicate is exactly the flags-plus-index-presence logic retrieve.py
    already owns, and a copy here would report health the query path does not.
    """
    try:
        import retrieve
        return retrieve.embedding_channel_status()
    except Exception as exc:
        return "unknown: %s" % exc


def index_identity(index_dir=None) -> dict:
    """(model, doc_count, built_at) read from the built index's own meta.json.

    meta.json is the right source rather than the config: _embedding_retrieval
    reads the model name FROM the index at query time, so meta.json is what the
    query path actually uses. Reading the config here would report the model we
    MEANT to build and hide exactly the drift this exists to surface.
    """
    d = Path(index_dir) if index_dir else DEFAULT_INDEX_DIR
    meta_p = d / "meta.json"
    out = {"model": None, "doc_count": None, "built_at": None}
    try:
        st = meta_p.stat()
    except OSError:
        return out
    out["built_at"] = datetime.fromtimestamp(
        st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(meta_p, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:
        return out
    out["model"] = meta.get("model")
    cnt = meta.get("count")
    if cnt is None and isinstance(meta.get("docs"), list):
        cnt = len(meta["docs"])
    out["doc_count"] = cnt
    return out


def local_block(index_dir=None) -> dict:
    """The retrieval_index block THIS box publishes onto its team-state row."""
    ident = index_identity(index_dir)
    cfg_model = configured_model()
    model = ident["model"]
    # `drifted` is False when the model is UNKNOWN, and that is deliberate: an
    # absent index is a CHANNEL fault, reported by `channel`, not a drift claim.
    # Asserting drift from a missing file would manufacture a mismatch out of no
    # evidence -- and the census keys its alert on both fields, so the condition
    # is still surfaced, under the name that is actually true.
    return {
        "box": _hostname(),
        "model": model,
        "configured_model": cfg_model,
        "drifted": bool(model) and model != cfg_model,
        "doc_count": ident["doc_count"],
        "built_at": ident["built_at"],
        "channel": channel_status(),
        "published_at": _now_iso(),
    }


def classify(block) -> str:
    """Verdict for one agent's published block. Order matters: DRIFTED outranks
    a bad channel, because a drifted index answers queries WRONG while a dead
    channel merely answers them token-only -- the loud failure must not be
    laundered into the quiet one."""
    if not isinstance(block, dict) or not block:
        return VERDICT_MISSING
    if block.get("drifted"):
        return VERDICT_DRIFTED
    if (block.get("channel") or "") != "alive":
        return VERDICT_CHANNEL
    return VERDICT_OK


def census(world_dir=None) -> dict:
    """Fleet-wide: every agent row, its published block, and a verdict.

    Reads AUTHORITATIVE rows (see module docstring). Reports `provenance` per
    agent so a reader can tell a genuinely-absent block from an unread shard --
    guard-1753: the bare dict cannot say which layer produced it, so a caller
    that decides anything from a missing row must be told.
    """
    import _paths
    import _team_state
    wd = world_dir or _paths.WORLD_DIR
    try:
        rows, prov = _team_state.load_rows_authoritative_with_provenance(wd)
    except Exception as exc:
        return {"error": "row read failed: %s" % exc, "agents": {},
                "agent_count": 0, "shard_count": None}

    # Shard COUNT is reported beside the row count so the goal's own check --
    # "assert count == number of agent shards, not 1" -- is checkable from the
    # output rather than taken on faith.
    shard_count = None
    try:
        rd = _team_state.rows_dir(wd)
        shard_count = len(list(rd.glob("*.yaml")))
    except Exception:
        pass

    # Provenance is {"by_agent": {agent: layer}, "roster": layer} -- NOT a flat
    # agent->layer map. Reading it flat returns None for every agent, which
    # renders as "we could not tell" for a read that was in fact fully
    # authoritative: the guard-1753 signal inverted into silence. Measured here
    # before this line existed: 15/15 agents reported provenance=None while the
    # real answer was "authoritative" for all 15.
    by_agent = (prov or {}).get("by_agent") or {}

    agents, drifted, missing, bad_channel, live_agents = {}, [], [], [], []
    for agent in sorted(rows or {}):
        row = rows.get(agent) or {}
        block = row.get("retrieval_index") if isinstance(row, dict) else None
        verdict = classify(block)
        live = _is_live(row)
        if live:
            live_agents.append(agent)
        agents[agent] = {
            "verdict": verdict,
            "live": live,
            "last_active": row.get("last_active") if isinstance(row, dict) else None,
            "block": block,
            "provenance": by_agent.get(agent),
        }
        # Only LIVE agents populate the actionable lists. A dead row cannot
        # publish and its silence is not a finding; the full per-agent map above
        # still carries every row, so nothing is hidden -- only de-escalated.
        if not live:
            continue
        if verdict == VERDICT_DRIFTED:
            drifted.append(agent)
        elif verdict == VERDICT_MISSING:
            missing.append(agent)
        elif verdict == VERDICT_CHANNEL:
            bad_channel.append(agent)

    return {
        "agents": agents,
        "agent_count": len(agents),
        "live_agents": live_agents,
        "live_count": len(live_agents),
        "shard_count": shard_count,
        "roster_provenance": (prov or {}).get("roster"),
        "drifted": drifted,
        "missing": missing,
        "channel_not_alive": bad_channel,
        "configured_model": configured_model(),
        "live_threshold_hours": LIVE_THRESHOLD_HOURS,
        "checked_at": _now_iso(),
        "checked_from_box": _hostname(),
    }


def _render(rep) -> str:
    if rep.get("error"):
        return "retrieval-index census: ERROR %s" % rep["error"]
    lines = ["retrieval-index census — %s live of %s agent row(s), %s shard(s) "
             "on disk, configured model %s, read from %s (roster=%s)"
             % (rep["live_count"], rep["agent_count"], rep["shard_count"],
                rep["configured_model"], rep["checked_from_box"],
                rep.get("roster_provenance"))]
    for agent, info in rep["agents"].items():
        if not info.get("live"):
            continue  # full detail stays in --json; the report names live rows
        b = info.get("block") or {}
        if info["verdict"] == VERDICT_MISSING:
            lines.append("  %-10s %-16s (no retrieval_index block published; "
                         "provenance=%s)" % (agent, VERDICT_MISSING,
                                             info.get("provenance")))
            continue
        lines.append(
            "  %-10s %-16s box=%s model=%s configured=%s docs=%s built=%s channel=%s"
            % (agent, info["verdict"], b.get("box"), b.get("model"),
               b.get("configured_model"), b.get("doc_count"),
               b.get("built_at"), b.get("channel")))
    stale = rep["agent_count"] - rep["live_count"]
    if stale:
        lines.append("  (%d row(s) not live past %sh — not counted as gaps; "
                     "see --json for all rows)" % (stale, rep["live_threshold_hours"]))
    if rep["drifted"]:
        lines.append("  DRIFTED: %s" % ", ".join(rep["drifted"]))
    if rep["channel_not_alive"]:
        lines.append("  channel not alive: %s" % ", ".join(rep["channel_not_alive"]))
    if rep["missing"]:
        lines.append("  no block published: %s" % ", ".join(rep["missing"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("publish-value", "census"),
                    help="publish-value: emit THIS box's block as one JSON line "
                         "(for team-state-update --value). census: report every "
                         "agent's published block.")
    ap.add_argument("--json", action="store_true", help="census: raw JSON")
    ap.add_argument("--index-dir", default=None)
    ap.add_argument("--world-dir", default=None)
    ap.add_argument("--exit-nonzero-on-drift", action="store_true",
                    help="census: exit 1 when any agent is DRIFTED (for gates)")
    args = ap.parse_args(argv)

    if args.mode == "publish-value":
        print(json.dumps(local_block(args.index_dir), sort_keys=True))
        return 0

    rep = census(args.world_dir)
    print(json.dumps(rep, indent=2, sort_keys=True) if args.json else _render(rep))
    if args.exit_nonzero_on_drift and rep.get("drifted"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
