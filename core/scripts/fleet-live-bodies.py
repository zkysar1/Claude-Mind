#!/usr/bin/env python3
"""Derive the LIVE BODY list at run time — the stop list for any ceremony that
must reach every Body (quiesce, drain, restart, credential rotation, upgrade).

WHY THIS EXISTS (g-115-9008, guard-6027). A ceremony's stop list used to be a
hand-maintained table of AGENTS. The fleet is composed of BODIES: one reducer
Body plus N worker Bodies per agent, each its own session on its own box, each
needing its own stop. A hand table cannot follow that and keeps reading as
authoritative because it WAS correct when written. Measured 2026-09-05: a
five-row agent table against nine live Bodies — one agent alone was live on six
boxes, so five of its terminals would have kept running and merging the shared
tree at every turn-end. The operator executes the ceremony faithfully and gets a
fleet that was never quiet, then measures against it.

THE NOUN-CHECK, MECHANIZED. The rows of this output COUNT BODIES, and a ceremony
that must reach every Body is composed OF Bodies. Same noun. That is the whole
check guard-6027 asks a reader to perform by hand; here it is a property of the
producer instead.

TWO SOURCES, UNIONED, AND THE UNION IS THE POINT — neither is safe alone:

  * FRESH CARRIERS are the ground truth for what is actually alive, but they are
    a FLOOR, not a census: a Body whose heartbeat carrier has gone stale simply
    vanishes from the scan, and the scan also excludes the Body running it.
    Trusting it alone re-creates the original defect one layer down — a live
    Body silently missing from the stop list.
  * THE AGENT ROSTER is complete for agents but says nothing about Bodies.

So: every fresh carrier, PLUS self, PLUS a flagged row for every rostered agent
with no fresh Body at all. A flagged row is not a claim that the agent is live —
it is a refusal to claim it is idle from a signal that cannot prove it. For a
quiesce the asymmetry is decisive: a needless extra terminal check costs
seconds, a missed live Body silently invalidates the whole window.

Exit 0 always — this is a read, and an advisory that refuses to run is worse
than one that reports what it could see. `status` on every row says which
source it came from and how far it can be trusted.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
for p in (str(SCRIPTS), str(SCRIPTS.parent.parent)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fresh_carriers(agent: str, sid: str | None) -> tuple[list[dict], int, str | None]:
    """Fresh heartbeat carriers fleet-wide, via the existing reducer-promotion
    discriminator. Reused rather than re-implemented: a second scanner would
    drift from the one the promotion gate actually trusts."""
    try:
        import reducer_promotion as rp
        from _paths import agents_root

        d = rp.measure_discriminators(agents_root(), agent, sid)
        det = d[1]["only_fresh_carrier_is_mine"]
        return (list(det.get("fresh") or []), int(det.get("carriers_scanned") or 0), None)
    except Exception as exc:  # never let a read stop a ceremony
        return ([], 0, f"{type(exc).__name__}: {exc}")


def _roster() -> list[str]:
    try:
        from _paths import agents_root

        root = agents_root()
        return sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and (p / "self.md").exists()
        )
    except Exception:
        return []


def collect(agent: str | None, sid: str | None, host: str | None) -> dict:
    agent = agent or os.environ.get("MIND_AGENT") or ""
    sid = sid or os.environ.get("MIND_SID") or ""
    host = host or socket.gethostname()

    fresh, scanned, scan_err = _fresh_carriers(agent, sid)

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for f in fresh:
        a, h = str(f.get("agent") or ""), str(f.get("host") or "")
        rows.append({
            "agent": a, "host": h, "sid": str(f.get("sid") or ""),
            "age_s": f.get("age_s"), "source": "fresh-carrier", "status": "live",
        })
        seen.add((a, h))

    # SELF — the scan excludes the Body it runs from, by construction.
    if agent and (agent, host) not in seen:
        rows.append({
            "agent": agent, "host": host, "sid": sid, "age_s": 0,
            "source": "self", "status": "live",
        })
        seen.add((agent, host))

    # Rostered agents with NO live Body anywhere: report, never omit.
    live_agents = {r["agent"] for r in rows}
    unaccounted = [a for a in _roster() if a and a not in live_agents]
    for a in unaccounted:
        rows.append({
            "agent": a, "host": None, "sid": None, "age_s": None,
            "source": "roster", "status": "carrier-stale-verify-at-terminal",
        })

    rows.sort(key=lambda r: (r["agent"], str(r["host"] or "~")))
    live = [r for r in rows if r["status"] == "live"]
    return {
        "bodies_live": len(live),
        "rows_total": len(rows),
        "unverified_agents": len(unaccounted),
        "carriers_scanned": scanned,
        "scan_error": scan_err,
        "counts": "rows COUNT BODIES (not agents) — same noun as the thing a fleet ceremony acts on",
        "floor_not_census": (
            "fresh carriers are a FLOOR: a live Body with a stale carrier does not appear. "
            "Rows marked carrier-stale-verify-at-terminal are agents this scan could not "
            "account for at all — check their terminals rather than assuming idle."
        ),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive the live-Body stop list at run time.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--agent", default=None, help="override MIND_AGENT (self row)")
    ap.add_argument("--sid", default=None, help="override MIND_SID (self row)")
    ap.add_argument("--host", default=None, help="override hostname (self row)")
    args = ap.parse_args()

    out = collect(args.agent, args.sid, args.host)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"LIVE BODIES: {out['bodies_live']}   "
          f"(carriers scanned {out['carriers_scanned']}; "
          f"{out['unverified_agents']} agent(s) unaccounted)")
    if out["scan_error"]:
        print(f"  ⚠ carrier scan FAILED ({out['scan_error']}) — the live rows below are "
              f"self only; treat every roster row as unverified.")
    print()
    print(f"  {'AGENT':10} {'BOX':16} {'SID':10} {'AGE':>7}  SOURCE / STATUS")
    for r in out["rows"]:
        age = f"{r['age_s']}s" if r["age_s"] is not None else "-"
        mark = "  " if r["status"] == "live" else "! "
        print(f"{mark}{r['agent']:10} {str(r['host'] or '?'):16} "
              f"{str(r['sid'] or '')[:8]:10} {age:>7}  {r['source']} / {r['status']}")
    print()
    print("Rows COUNT BODIES. Stop EVERY row: `/stop <agent>` in that Body's own terminal.")
    print("Rows marked ! could not be confirmed from here — verify at the terminal, do not")
    print("assume idle. Fresh carriers are a floor, not a census (a stale carrier vanishes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
