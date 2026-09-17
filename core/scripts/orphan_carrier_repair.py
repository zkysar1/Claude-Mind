"""Repair orphan body-heartbeat carriers keyed on the AUTHORITATIVE enumeration ().

WHY THIS EXISTS AND WHY NO EXISTING PASS COVERS IT. Every carrier repair in the
tree is keyed on a LOCAL FILE: `_reconcile_orphan_carrier` opens with
`if not carrier.is_file(): return None`, and its one call site
(cleanup-stale-bindings.sh:451) iterates `session/body-heartbeat-*.json`. The
population that alerts, however, is enumerated from the STORE OF RECORD --
`worker_stall.enumerate_carriers`, which unit 29 measured is read by three
consumers and WRITTEN BY NONE. A row whose owning box lost its session dir, its
manifest and its local carrier is therefore unreachable by every writer that
exists, permanently: it has no local file to key on, and the forward-only
remedies (SessionStart ordering, GC deferral) cannot see a row whose stranding
step already ran (guard-6525). Measured 2026-09-13: 20 such rows, the oldest
18.7 days, and the population GREW from 23 to 24 while three units debated it.

THE WRITE IS THE NARROWEST SHAPE guard-6558 PRESCRIBES, and that guardrail was
encoded FROM this goal (unit 24) after measuring the failure this module must
not repeat. `worker_stall.classify_body` returns V_ALIVE on FRESHNESS before it
ever reads `body_state`, and both freshness readers (worker_stall,
reducer_promotion.only_fresh_carrier_is_mine) parse the doc's own `ts`. So a
repair that restamped `ts` would trade a false stall for a PHANTOM LIVE BODY --
strictly worse, and two fleet instruments act on it. This module therefore
mutates `body_state` alone and asserts `ts` is byte-identical afterward, read
back off disk rather than off the dict in hand.

THREE GATES, EACH NARROWING THE BLAST RADIUS:
  1. BOUND AGENT ONLY. Rows are written only for `MIND_AGENT`, never for a
     peer. This is deliberately narrower than "the claim holder may write":
     it needs no cross-agent claim reasoning, it makes the module safe to run
     on any box, and each box clears its own agent's rows. It also keeps the
     ownership fence (guard-5587 / guard-4180) intact by construction -- the
     owning identity is the writer, so nothing here declares a peer dead.
  2. A VALID ENUMERATION. The same four-part gate outcome 4 is measured under
     (`worker_stall.reading_is_valid`), because a repair driven by a
     local-mirror read would write whatever subset this box happened to pull.
  3. THE CORRECTED PREDICATE. `select_rows` below. The clause the originally
     proposed predicate was missing -- `body_state` must be a LIVE state -- is
     what keeps the BLIND population (absent body_state, 24 rows) out of the
     set; without it 23 blind rows are swept and the repair asserts a fact no
     evidence supports. The prior unit named that conflation explicitly as the
     thing not to do.

KEYED ON (agent, sid), NEVER ON sid ALONE. Measured 2026-09-13: 78 carriers,
77 distinct sids -- one sid is held by BOTH alpha and foxtrot on cc-08 with
different carrier ages. A sid-keyed writer writes one row and silently misses
the other, or writes the wrong agent's.

DRY-RUN IS THE DEFAULT. `--apply` is required to write.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import carrier_push_retry as cpr  # noqa: E402
import worker_stall as ws  # noqa: E402
from _fileops import durable_write_text  # noqa: E402
from _paths import WORLD_DIR, agent_state_dir, agents_root  # noqa: E402

# A wide default. The population's own distribution justifies it: at >3d the
# set is 20 rows and every one of them is independently graded `stalled_no_close`
# by the production classifier, while the rows between 1d and 3d are ordinary
# recent orphans that the forward-only remedies are the right fix for. Going
# below the stale threshold would be incoherent -- a fresh carrier is a live
# Body -- and `select_rows` refuses that case on its own clause regardless.
DEFAULT_MIN_AGE_DAYS = 3.0

REPAIR_STATE = "closed-stale"

# Records that a WIRED pass ran, so a quiet clean pass is never
# indistinguishable from a pass that never happened -- the exact ambiguity
# that let this module sit with zero call sites while reading healthy.
MARKER_NAME = ".orphan-carrier-repair-last-run"

# The claim map must cover the SAME stores scan() covers, or a Body that claimed
# into its own agent queue reads as claimless here and becomes eligible. scan()
# builds this list itself; the name is duplicated rather than imported because
# worker_stall does not export it, and a divergence would be a silent widening.
_QUEUE_BASENAME = "aspirations" + ".jsonl"


def live_body_state(state: str | None) -> bool:
    """Is this a state a LIVE Body writes? Mirrors classify_body's partition.

    Deliberately routed through worker_stall's own constants rather than a
    literal list here: a future state joins CLOSED_BODY_STATES or it does not,
    and this predicate must inherit that answer rather than fork it.
    """
    s = (state or "").strip()
    return bool(s) and s not in ws.CLOSED_BODY_STATES and s != ws.PARKED_BODY_STATE


def within_interval(marker, min_interval_hours: float, now: float):
    """Hours since the last recorded pass if it falls INSIDE the window, else None.

    Pure given the filesystem, and separated from main() for the same reason
    select_rows is: a gate that can silently skip work must be directly
    testable, not only observable through a 7-second enumeration.

    EVERY failure path returns None, i.e. RUN. An unreadable or missing marker
    must be able to cost an extra pass, never to skip one -- the failure this
    module exists to fix is a repair that never runs.
    """
    if min_interval_hours <= 0:
        return None
    try:
        if not marker.exists():
            return None
        age_h = (now - marker.stat().st_mtime) / 3600.0
    except OSError:
        return None
    return age_h if age_h < min_interval_hours else None


def select_rows(facts, min_age_minutes: float, writable_agent: str):
    """PURE. Returns (selected, excluded) where each excluded row carries reasons.

    `facts` rows are dicts with: agent, sid, host, age_minutes, body_state,
    held_goal.

    Every clause is a REFUSAL, and the reasons are kept per row rather than
    merely counted, so a caller can show what the predicate leaves out
    (guard-1802: measure the exclusion, not only the selection).
    """
    selected, excluded = [], []
    for f in facts:
        why = []
        if f.get("agent") != writable_agent:
            why.append("not-the-bound-agent")
        host = f.get("host")
        if not (isinstance(host, str) and host.strip()):
            why.append("host-not-named")
        age = f.get("age_minutes")
        if age is None:
            why.append("ts-unparseable")
        elif age <= min_age_minutes:
            why.append("fresher-than-threshold")
        if f.get("held_goal") is not None:
            why.append("holds-live-claim")
        if not live_body_state(f.get("body_state")):
            why.append("not-a-LIVE-body_state(%s)" % (f.get("body_state") or "absent"))
        if why:
            excluded.append(dict(f, exclusion_reasons=why))
        else:
            selected.append(f)
    return selected, excluded


def facts_from_carriers(rows, claims, now):
    """Shape enumerate_carriers output into the rows `select_rows` consumes."""
    facts = []
    for r in rows:
        doc = r.get("doc") or {}
        ts = ws._parse_iso(str(doc.get("ts") or ""))
        age = None if ts is None else (now - ts).total_seconds() / 60.0
        facts.append({
            "agent": r.get("agent"),
            "sid": r.get("sid"),
            "host": doc.get("host"),
            "age_minutes": age,
            "body_state": doc.get("body_state"),
            "held_goal": claims.get(r.get("sid")),
            "doc": doc,
        })
    return facts


def repair_one(sid: str, doc: dict, state_dir: Path) -> dict:
    """Write body_state alone into the carrier, then deliver it. `ts` is PRESERVED.

    Returns a verdict dict. The `ts` assertion is not decoration: it is the one
    check that distinguishes this repair from the one guard-6558 forbids, so it
    runs on the bytes actually read back off disk, never on the dict in hand
    (guard-3356). A divergence ABORTS before the push, so a restamped carrier is
    never delivered to peers.
    """
    carrier = state_dir / f"body-heartbeat-{sid}.json"
    before_ts = doc.get("ts")
    new_doc = dict(doc)
    new_doc["body_state"] = REPAIR_STATE
    carrier.parent.mkdir(parents=True, exist_ok=True)
    tmp = carrier.with_name(carrier.name + ".tmp")
    # fsync BEFORE the rename (guard-1179). A bare write_text + os.replace is
    # durable in metadata only -- the rename can be journalled while the data
    # blocks are still unflushed, so a crash here brings the carrier back as
    # all-0x00. That failure lands on the one file every liveness reader parses:
    # an unreadable carrier is not a benign closed row, it is a row the stall
    # classifier cannot grade at all, which is the state this module exists to
    # remove. The rename stays the caller's, so atomicity is unchanged.
    durable_write_text(tmp, json.dumps(new_doc) + "\n")
    os.replace(tmp, carrier)
    back = json.loads(carrier.read_text(encoding="utf-8"))
    if back.get("ts") != before_ts:
        return {"sid": sid, "verdict": "ABORTED-ts-changed",
                "ts_before": before_ts, "ts_after": back.get("ts")}
    if back.get("body_state") != REPAIR_STATE:
        return {"sid": sid, "verdict": "ABORTED-state-not-written",
                "body_state": back.get("body_state")}
    try:
        from storage_backend import get_backend  # noqa: PLC0415
        get_backend().write_bytes(carrier, carrier.read_bytes())
        return {"sid": sid, "verdict": "repaired", "ts_preserved": True}
    except Exception as exc:  # noqa: BLE001 -- transport must not raise here
        return {"sid": sid, "verdict": "repaired-push-failed", "ts_preserved": True,
                "push_error": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually write; default is a dry run")
    ap.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when the pass selects nothing")
    ap.add_argument("--min-interval-hours", type=float, default=0.0,
                    help="skip if a pass ran within this window; 0 = ungated")
    args = ap.parse_args(argv)

    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print("orphan-carrier-repair: MIND_AGENT is unset -- refusing "
              "(the bound agent IS the writer scope)", file=sys.stderr)
        return 3

    # G4 RETRY LANE (), run BEFORE the interval gate and before the
    # enumeration, and deliberately not subject to either. It is cheap -- one
    # small local file read, and a backend read only when a row is actually
    # pending -- and it must not inherit the 3.0-day selection bar that governs
    # the repair below: the rows it retries have ALREADY been repaired locally,
    # so their delivery gap is open from the moment the push was refused and
    # nothing else in the tree can close it. Its own report carries every
    # verdict; a pass with nothing pending is `pending: 0`, never silence.
    retry_report = cpr.retry_pending(agent, apply=args.apply)

    # INTERVAL GATE, deliberately checked BEFORE the enumeration, because the
    # enumeration IS the cost: ~7.0s of authoritative fleet-wide carrier reads,
    # measured twice on cc-05 against 78 carriers. Gating after it saves nothing.
    # Opt-in -- default 0.0 leaves every hand-run ungated, so this can never
    # silently swallow a deliberate manual pass.
    # WHY A WINDOW AT ALL: selection requires DEFAULT_MIN_AGE_DAYS = 3.0, so a
    # per-iteration cadence oversamples the detected condition by ~500x and
    # cannot surface anything an hourly pass would miss.
    # The marker records the ATTEMPT and is written BEFORE the work, so a crash
    # mid-pass cannot produce a hot retry loop (embedding-index-freshness.py
    # uses this shape for the same reason). Every failure path here FALLS
    # THROUGH TO THE REPAIR: an unreadable or unwritable marker must never be
    # able to skip a pass, only to cost an extra one.
    marker = agent_state_dir(agent) / MARKER_NAME
    if args.min_interval_hours > 0:
        age_h = within_interval(marker, args.min_interval_hours, time.time())
        if age_h is not None:
            # The retry lane already ran above and is NOT interval-gated, so an
            # interval skip must still surface what it did -- otherwise the one
            # lane that is allowed to act on every pass would be the only one
            # nobody can see acting.
            if retry_report.get("pending") or retry_report.get("expired"):
                print("orphan-carrier-repair: push-retry " + json.dumps(
                    retry_report, default=str))
            elif not args.quiet:
                print("orphan-carrier-repair: last pass %.1fh ago, within the "
                      "%.1fh window -- skipping"
                      % (age_h, args.min_interval_hours))
            return 0
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n",
                encoding="utf-8")
        except OSError:
            pass

    agents_dir = agents_root()
    rows, meta = ws.enumerate_carriers(agents_dir)
    # reading_is_valid reads two terms from `enumeration` and two from the top
    # level; the top-level pair is scan()'s and does not apply to a bare
    # enumeration, so they are supplied at their clean values and the gate is
    # doing real work only on the nested pair. Routed through the SSOT helper
    # rather than hand-compared, because every hand-parse of it so far has been
    # wrong in the permissive direction (units 34 and 35).
    gate = ws.reading_is_valid({"enumeration": meta,
                                "degraded_read": False, "rows_dropped": 0})
    if not gate["valid"]:
        print("orphan-carrier-repair: enumeration is NOT a measurement -- refusing:",
              file=sys.stderr)
        for reason in gate["reasons"]:
            print("   " + reason, file=sys.stderr)
        return 4

    stores = [Path(WORLD_DIR) / _QUEUE_BASENAME] + [
        agents_dir / a / _QUEUE_BASENAME
        for a in sorted({r["agent"] for r in rows if r.get("agent")})]
    claims, _via = ws.read_claims_union(*stores)
    facts = facts_from_carriers(rows, claims, dt.datetime.now())
    selected, excluded = select_rows(facts, args.min_age_days * 1440.0, agent)

    out = {"agent": agent, "carriers_found": len(rows), "enumeration": meta,
           "min_age_days": args.min_age_days, "selected": len(selected),
           "applied": bool(args.apply), "results": [],
           "push_retry": retry_report}

    if args.apply and selected:
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location(
            "body_manifest_for_repair",
            Path(__file__).resolve().parent / "body-manifest.py")
        bm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bm)
        for f in selected:
            _, _, state_dir = bm._agent_paths(f["agent"], f["sid"], None)
            res = repair_one(f["sid"], f["doc"], state_dir)
            # G4 RETRY BREADCRUMB (). repair_one's own local write
            # clears `body_state: active`, so select_rows' live-state clause can
            # never re-select this row and a refused push is permanent. Record
            # the independently-written refusal signal the retry lane keys on.
            # Fail-open -- telemetry must not change the repair verdict.
            if res.get("verdict") == cpr.REFUSAL_VERDICT:
                try:
                    cpr.record_push_failure(
                        f["agent"], f["sid"], ts=(f["doc"] or {}).get("ts"),
                        body_state=REPAIR_STATE, error=res.get("push_error"),
                        state_dir=state_dir)
                except Exception:  # noqa: BLE001
                    pass
            out["results"].append(res)

    # QUIET belongs to the wired call site, which runs on a cadence and is
    # EXPECTED to find nothing on almost every pass. A summary line on every
    # clean pass trains the reader to filter the stream, so the one pass that
    # actually repairs something arrives somewhere already ignored
    # (guard-2418 class). Silence is safe ONLY because the marker above is the
    # durable record that the pass ran; without it this would recreate the very
    # silent-all-clear the module exists to end.
    if (args.quiet and not selected and not out["results"]
            and not retry_report.get("pending")
            and not retry_report.get("expired")):
        return 0

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print("orphan-carrier-repair agent=%s carriers=%d selected=%d apply=%s (>%.1fd)"
              % (agent, len(rows), len(selected), args.apply, args.min_age_days))
        for f in sorted(selected, key=lambda x: -(x["age_minutes"] or 0)):
            print("  %-40s %-18s %6.2fd %s"
                  % (f["sid"], f["host"], (f["age_minutes"] or 0) / 1440.0,
                     f["body_state"]))
        reasons = {}
        for f in excluded:
            for reason in f["exclusion_reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
        print("  EXCLUDED: "
              + ", ".join("%s=%d" % (k, reasons[k]) for k in sorted(reasons)))
        for r in out["results"]:
            print("  -> %s %s" % (r["sid"], r["verdict"]))
        print("  PUSH-RETRY: pending=%d retired=%d kept=%d expired=%d%s"
              % (retry_report.get("pending", 0), retry_report.get("retired", 0),
                 retry_report.get("kept", 0), len(retry_report.get("expired") or []),
                 "" if not retry_report.get("error")
                 else " error=%s" % retry_report["error"]))
        for r in retry_report.get("results") or []:
            print("    -> %s %s" % (r.get("sid"), r.get("verdict")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
