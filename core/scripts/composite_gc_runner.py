#!/usr/bin/env python3
"""composite_gc_runner.py — the scheduled pass of orphan collection for the composite goal-queue store ( U14).

THE CALLER U2e LEFT OPEN. `OwnCloudBackend.composite_gc_apply` deletes, `composite_gc_enumerate` reads and
`composite_gc_restore` repairs; until this file nothing ran them. One `run_pass` is one scheduled pass: take the lease,
read the cadence stamp, enumerate (or apply), persist the first-seen ledger, give every recent archive run its late
restore, write the stamp, return one result. It runs as its own process and never inside the daemon: the daemon
restarted eight times on 2026-10-03 between 00:41 and 18:28 UTC (mind_api/state/daemon.log on cc-08: 00:41, 03:26, 08:10,
10:23, 11:47, 14:25, 17:20, 18:28; gaps from 68 minutes to 4 h 44 min, 2.5 h on average), and one of them cut a claim write
off mid-request. A pass hosted there would be killed that often.
Nothing here schedules it: the caller is the next unit, and until one exists nothing runs this file.

THE DECISIONS (the record, with the numbers: core/config/rationale/aspirations-store-segmentation.md, "The scheduled GC pass (U14)").
  WHO. Any caller on any box. Exclusivity is a lease, not a role: `backend.acquire_lock` is a conditional put in the lock
  table with a TTL (rb-9975), tried once and never waited on; a caller that does not get it returns `lease-held`. The
  lock path is the store's path plus `.gc.lock`, so it never contends with the store's own write lock.
  WHEN. At most once per INTERVAL_S, read from `state.json` in the object store. The cadence test and the action are
  both fleet-wide (one store, every box), so N callers cost N small reads and one pass (guard-2585; the cold-snapshot
  tick is the precedent). A stamp in the future is treated as due and overwritten, never as a stall.
  WHERE THE LEDGER LIVES. In the object store beside the archive, never in `world/` (a synced file is a read-through
  cache with merge semantics) and never on one box (the next sweeper would start every clock over). A ledger that
  cannot be read is NOT an empty one: the pass stops, because a caller that read it as empty would overwrite 14 days of
  first sightings with a fresh start.
  WHAT IT MAY DELETE. Only what `composite_gc_apply` licenses. This file computes no licence of its own (guard-3925): it
  asks the backend, and reads `gc-not-enabled` as "observe instead". Its own flag check only decides whether to touch
  the store at all, and can only cause fewer calls, never a delete.

DRY BY DEFAULT (guard-1301). A pass OBSERVES: it enumerates, records first sightings and reports what would go. Deletion
needs BOTH `apply=True` (`--apply`) AND the backend's own delete flag (OWNCLOUD_COMPOSITE_GC); either one alone deletes
nothing. The late restore needs neither, because recovery must not depend on the switch that enables deletion.

THE LATE RESTORE. The runs it visits are listed from the archive, not remembered: a pass that died before it could write
its own record still gets its sweep. A run is visited while its age is under TRUST_WINDOW_S plus one interval, so the
last visit falls after the window closes. A run directory with no receipt deleted nothing (the receipt is written
before the first delete) and is skipped without complaint.

WHAT THE RESULT CARRIES. One dict; `post` is None unless a delete or an anomaly needs a human-visible line. The runner
never posts: a caller that posts owns the channel and the de-duplication.

NOT ESTABLISHED. Pass duration at the delete cap (the 30-minute lease is an estimate with a wide margin, not a
measurement at 500 objects); the head-moved retry count (3 is a choice; the rate of a writer commit during a listing
has not been measured); and the live lifecycle config of the archive prefix (guard-1301 asks for it before any
environment is named for deletion).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable, List, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _owncloud_composite as comp  # noqa: E402

STORE_REL = comp.ALLOWLIST[0]  # the one store the layout covers
INTERVAL_S = 4 * 3600  # my value, not a measurement: orphans arrive at ~1,540 to ~1,950 a day (U9, U11), the cap is 500 a pass, so 6 passes a day give 1.5x to 1.9x headroom
TRUST_WINDOW_S = 24 * 3600  # my value, not a measurement: the gap a late head commit can still land in is seconds to minutes (U6); a day is two orders of margin and the sweep costs two GETs a run
LEASE_TTL_S = 1800  # my value, not a measurement: a lock older than this may be taken from a dead holder; a pass at the cap is estimated at minutes
HEAD_MOVED_ATTEMPTS = 3  # my choice: a writer commit during the listing abandons that attempt, and one more commit is unlikely within seconds
HEAD_MOVED_BACKOFF_S = 2.0
STATE_FORMAT = 1

_BACKEND_METHODS = ("composite_gc_enumerate", "composite_gc_apply", "composite_gc_restore", "composite_gc_runs",
                    "composite_gc_state_get", "composite_gc_state_put", "acquire_lock", "release_lock")
_NOT_LICENSED = ("gc-not-enabled", "grace-below-floor")
_ROUTINE_REFUSALS = {"head-moved-during-enumeration": "busy", "not-a-head": "not-yet-composite"}
_ROUTINE_SKIPS = frozenset({"re-referenced", "gone-since-listing", "gone-since-archive", "rewritten-since-listing"})
_NOT_FOUND = frozenset({"404", "NoSuchKey", "NotFound"})
_FAILED_VERDICTS = ("error", "refused", "ledger-unreadable", "state-unreadable", "lease-unavailable")


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _code(exc: BaseException) -> Optional[str]:
    resp = getattr(exc, "response", None)
    err = resp.get("Error") if isinstance(resp, dict) else None
    code = err.get("Code") if isinstance(err, dict) else None
    return str(code) if code is not None else None


def _why(exc: BaseException) -> str:
    code = _code(exc)
    return "%s (%s)" % (type(exc).__name__, code) if code else type(exc).__name__


def _new_result(apply: bool, now: float) -> dict:
    return {"store": STORE_REL, "mode": "apply" if apply else "observe", "at": _iso(now), "verdict": None,
            "attempts": 0, "counts": {}, "unknown": 0, "would_delete": 0, "run_id": None, "deleted": [],
            "skipped": {}, "restored": [], "late_restored": {}, "ledger_repaired": 0, "anomalies": [],
            "elapsed_s": None, "post": None}


class LedgerUnreadable(Exception):
    """The stored ledger is not a ledger document."""


def clean_ledger(doc, now: float):
    """(ledger, dropped) from the stored ledger document; None (never written) is an empty ledger.

    A document that is not an object holding a `ledger` object raises LedgerUnreadable: unreadable is not empty. An entry
    whose name is not a string or whose first sighting is not a finite number in (0, now] is dropped and counted: the
    planner then sees the name as new and its grace starts over, the safe direction. Zero and negative are dropped
    because the planner's clock is max(first sighting, the object's own last_modified), so a bogus zero would date a
    long-lived object as old on the day it first became an orphan. A first sighting in the future is dropped too:
    it would only delay, but it is not a measurement."""
    if doc is None:
        return {}, 0
    if not isinstance(doc, dict) or not isinstance(doc.get("ledger"), dict):
        raise LedgerUnreadable("the ledger document holds no ledger object")
    good, dropped = {}, 0
    for name, first in doc["ledger"].items():
        if isinstance(name, str) and _finite(first) and 0 < first <= now:
            good[name] = float(first)
        else:
            dropped += 1
    return good, dropped


def build_post(res: dict) -> Optional[dict]:
    """The one line a human should see for this result, or None when there is nothing to say."""
    lines: List[str] = []
    if res["deleted"]:
        lines.append("deleted %d orphan segment object(s) in run %s; each is archived and the run has a receipt" % (
            len(res["deleted"]), res["run_id"]))
    lines.extend(res["anomalies"])
    if not lines:
        return None
    kind = "anomaly" if res["anomalies"] else "deleted"
    subject = "composite GC %s on %s: %s" % (kind, res["store"], res["verdict"])
    return {"severity": kind, "subject": subject, "body": subject + "\n" + "\n".join("- " + ln for ln in lines)}


def summary(res: dict) -> dict:
    """The result as one compact JSON-safe dict: the long lists become counts."""
    out = dict(res)
    out["deleted"] = len(res["deleted"])
    out["restored"] = len(res["restored"])
    out["late_restored"] = {k: len(v) for k, v in res["late_restored"].items()}
    out["skipped"] = dict(Counter(res["skipped"].values()))
    return out


def run_pass(be, path, *, apply: bool = False, now: Optional[float] = None, interval_s: float = INTERVAL_S,
             force: bool = False, grace_s: float = comp.GC_GRACE_S, max_delete: int = comp.GC_MAX_DELETE,
             sleep: Callable[[float], None] = time.sleep) -> dict:
    """One scheduled pass over the composite store at `path` on backend `be`. See the module docstring."""
    now = time.time() if now is None else now
    res = _new_result(apply, now)
    t0 = time.monotonic()
    if not all(callable(getattr(be, n, None)) for n in _BACKEND_METHODS):
        res["verdict"] = "not-own-cloud"  # the backend in hand cannot host the layout: ask the object (guard-3925)
        return res
    env_id = getattr(be, "env_id", None)
    if not (comp.should_composite(STORE_REL, env_id) or comp.should_gc(STORE_REL, env_id)):
        res["verdict"] = "inactive"  # neither flag names this environment: no S3 call, no lock call
        return res
    lock = Path(str(path) + ".gc.lock")
    try:
        be.acquire_lock(lock, timeout=0, stale_seconds=LEASE_TTL_S)
    except TimeoutError:
        res["verdict"] = "lease-held"
        return res
    except Exception as exc:
        res["verdict"] = "lease-unavailable"
        res["anomalies"].append("lease-unavailable: %s" % _why(exc))
        res["post"] = build_post(res)
        return res
    try:
        _locked_pass(be, path, res, apply=apply, now=now, interval_s=interval_s, force=force, grace_s=grace_s,
                     max_delete=max_delete, sleep=sleep)
    finally:
        try:
            be.release_lock(lock)
        except Exception as exc:
            res["anomalies"].append("lease-release-failed: %s" % _why(exc))
    res["elapsed_s"] = round(time.monotonic() - t0, 3)
    res["post"] = build_post(res)
    return res


def _locked_pass(be, path, res: dict, *, apply, now, interval_s, force, grace_s, max_delete, sleep) -> None:
    try:
        state = be.composite_gc_state_get(path, "state")
        if state is not None and not isinstance(state, dict):
            raise ValueError("the state document is not an object")
    except Exception as exc:
        res["verdict"] = "state-unreadable"
        res["anomalies"].append("state-unreadable: %s" % _why(exc))
        return
    last = (state or {}).get("last_pass_at")
    if _finite(last) and last > now:
        res["anomalies"].append("the cadence stamp is in the future (%s): treated as due and overwritten" % _iso(last))
    elif not force and _finite(last) and now - last < interval_s:
        res["verdict"] = "not-due"
        res["next_due_in_s"] = int(interval_s - (now - last))
        return
    ledger, stored = None, False
    try:
        doc = be.composite_gc_state_get(path, "ledger")
        stored = doc is not None
        ledger, repaired = clean_ledger(doc, now)
    except Exception as exc:
        res["verdict"] = "ledger-unreadable"
        res["anomalies"].append("ledger-unreadable: %s" % _why(exc))
    if ledger is not None:
        res["ledger_repaired"] = repaired
        if repaired:
            res["anomalies"].append("%d ledger entr%s dropped as invalid; the grace of each starts over" % (
                repaired, "y" if repaired == 1 else "ies"))
        _collect(be, path, res, ledger, stored, apply=apply, now=now, grace_s=grace_s, max_delete=max_delete,
                 sleep=sleep)
    _late_restore(be, path, res, now, interval_s)
    try:
        be.composite_gc_state_put(path, "state", {"format": STATE_FORMAT, "store": STORE_REL, "last_pass_at": now,
                                                  "last_pass": _iso(now), "last_verdict": res["verdict"],
                                                  "last_counts": res["counts"]})
    except Exception as exc:
        res["anomalies"].append("state-write-failed: %s" % _why(exc))


def _collect(be, path, res: dict, ledger: dict, stored: bool, *, apply, now, grace_s, max_delete, sleep) -> None:
    applied = enum = plan = reason = None
    try:
        for attempt in range(1, HEAD_MOVED_ATTEMPTS + 1):
            res["attempts"] = attempt
            applied = enum = None
            if apply:
                applied = be.composite_gc_apply(path, ledger, now, grace_s=grace_s, max_delete=max_delete)
                if applied.stopped in _NOT_LICENSED:
                    res["apply_refused"] = applied.stopped
                    applied = None
            if applied is None:
                enum = be.composite_gc_enumerate(path, ledger, now, grace_s=grace_s, max_delete=max_delete)
            plan = applied.plan if applied is not None else enum.plan
            reason = applied.stopped if applied is not None and applied.stopped else None
            if reason is None and plan is not None and plan.refused:
                reason = plan.refused[0]
            if reason == "head-moved-during-enumeration" and attempt < HEAD_MOVED_ATTEMPTS:
                sleep(HEAD_MOVED_BACKOFF_S)
                continue
            break
    except Exception as exc:
        res["verdict"] = "error: %s" % type(exc).__name__
        res["anomalies"].append("pass failed: %s; any run archive it began is swept by the late restore" % _why(exc))
        return
    if plan is None:
        res["verdict"] = "error: no plan"
        res["anomalies"].append("pass returned no plan")
        return
    res["counts"] = dict(plan.counts)
    res["unknown"] = len(plan.unknown)
    if applied is not None:
        res.update(run_id=applied.run_id, deleted=list(applied.deleted), skipped=dict(applied.skipped),
                   restored=list(applied.restored))
    else:
        res["would_delete"] = len(plan.delete)
    new_ledger = dict(applied.ledger if applied is not None else plan.ledger)
    if reason is None:
        res["verdict"] = "applied" if applied is not None else "observed"
    elif reason in _ROUTINE_REFUSALS:
        res["verdict"] = _ROUTINE_REFUSALS[reason]
    else:
        res["verdict"] = "refused: %s" % reason
        res["anomalies"].append("pass refused: %s" % reason)
    if res["restored"]:
        res["anomalies"].append("the pass put back %d object(s) the head named after it deleted them "
                                "(the 412 window was met)" % len(res["restored"]))
    for why, n in sorted(Counter(res["skipped"].values()).items()):
        if why not in _ROUTINE_SKIPS:
            res["anomalies"].append("kept %d object(s): %s" % (n, why))
    if new_ledger != ledger or res["ledger_repaired"] or not stored:
        try:
            be.composite_gc_state_put(path, "ledger", {"format": STATE_FORMAT, "store": STORE_REL, "updated": _iso(now),
                                                       "updated_epoch": now, "ledger": new_ledger})
        except Exception as exc:
            res["anomalies"].append("ledger-write-failed: %s" % _why(exc))


def _late_restore(be, path, res: dict, now: float, interval_s: float) -> None:
    try:
        runs = be.composite_gc_runs()
    except Exception as exc:
        res["anomalies"].append("archive-listing-failed: %s" % _why(exc))
        return
    horizon = TRUST_WINDOW_S + interval_s
    for run_id in runs:
        at = comp.gc_run_time(run_id)
        if at is None or now - at >= horizon:
            continue
        try:
            put_back = be.composite_gc_restore(path, run_id)
        except Exception as exc:
            if _code(exc) in _NOT_FOUND:
                continue  # no receipt: the receipt is written before the first delete, so this run deleted nothing
            res["anomalies"].append("late-restore-failed: %s: %s" % (run_id, _why(exc)))
            continue
        if put_back:
            res["late_restored"][run_id] = list(put_back)
            res["anomalies"].append("late restore put back %d object(s) of run %s" % (len(put_back), run_id))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One scheduled orphan-collection pass over the composite goal-queue store. "
                                             "Observes by default; --apply is still inert unless the backend's delete flag names this environment.")
    ap.add_argument("--apply", action="store_true", help="run the delete pass (needs OWNCLOUD_COMPOSITE_GC too)")
    ap.add_argument("--force", action="store_true", help="skip the interval check (the lease and the flags still apply)")
    ap.add_argument("--interval-s", type=float, default=INTERVAL_S, help="minimum seconds between passes (default %(default)s)")
    args = ap.parse_args(argv)
    try:
        import _paths  # noqa: PLC0415
        from storage_backend import get_backend  # noqa: PLC0415
        be = get_backend()
        path = Path(_paths.WORLD_DIR) / "aspirations.jsonl"
    except Exception as exc:
        print(json.dumps({"verdict": "setup-failed", "error": _why(exc)}))
        return 2
    res = run_pass(be, path, apply=args.apply, force=args.force, interval_s=args.interval_s)
    print(json.dumps(summary(res), sort_keys=True))
    failed = str(res["verdict"]).startswith(_FAILED_VERDICTS)
    return 1 if (failed or res["anomalies"]) else 0


if __name__ == "__main__":
    sys.exit(main())
