#!/usr/bin/env python3
"""goal-resolve.py — answer "what happened to goal g-NNN-NN?" across EVERY store.

WHY THIS EXISTS (g-115-6818). `aspirations-query.sh --goal-field id <gid>` returns
EMPTY for a goal that was EVICTED, and empty is indistinguishable from
never-existed. That single ambiguity produced three separate investigations
(g-115-6818, g-115-7282, g-115-6916) and an escalation to HIGH on a "silent
record loss" premise. None of those records had vanished.

CORRECTED 2026-08-23, same day, by measuring with this script: an earlier draft
of this docstring also claimed "33 live goals deferred against ids their authors
believed had vanished." The COUNT reproduces exactly — 33 of the 199 blocked
goals cite an evicted id (94 cite any id; 147 distinct; live 66 / evicted 50 /
unknown 30 / archived 1) — but the CAUSAL reading was wrong. In those 33 the
citation is contextual (a sibling goal, a superseded premise, a handoff
pointer), and a separate scan for defers actually premised on an id being
UNRESOLVABLE found none: every candidate matched on "does not exist yet" said of
an OUTPUT, a CHANNEL or a REPORT, never of a goal record. So this script removes
a real ambiguity that cost three duplicate investigations; it does not unblock
anything, and no defer should be cleared on its account. The overstatement was a
co-occurrence count carried forward as a cause (guard-1659).

`aspirations-evict-completed.py` removes AGED TERMINAL goals from the live
aspiration by design and records their ids in `archived_census.evicted_ids`.
Eviction re-homes nothing, so the record is in NEITHER aspirations.jsonl NOR
aspirations-archive.jsonl — exactly the "phantom" signature. The disposition
survives in the census; the VERDICT TEXT survives only in `world/.history`.

The framework already knew this failure mode and fixed it in exactly ONE place:
`retrieve.py::_is_goal_terminal` consults `census_evicted_ids` because "a
long-stale in_flight naming an evicted goal would otherwise read as not found".
That awareness was never generalised to the lookup path — `aspirations.py`
imports `all_evicted_ids` only at its MINT sites (so max+1 never re-mints an
evicted id), never at query. This script generalises it, read-only.

FOUR DISPOSITIONS, in resolution order:
  live      — a record in aspirations.jsonl
  archived  — a record in aspirations-archive.jsonl
  evicted   — no record, but the owning aspiration's census holds the id
  unknown   — no record and no census entry (never minted, or pre-census loss)

`unknown` IS NOT "never existed" — say so out loud. 1,294 of 8,321 measured
interior gaps resolve `unknown` and their mechanism is UNATTRIBUTED; some
aspirations carry a LEGACY counts-only census (`by_status` with no `evicted_ids`)
which is structurally unenumerable, so it is a known blind lane (guard-4093: a
zero with a blind lane is UNREACHABLE, not EMPTY).

--recover walks `world/.history` for the newest snapshot blob that still holds
the record, returning its status, title and full outcome_note. Recovery is
possible today only because the CAS history store is UNPRUNED (`_prune_to_cap`
is called from the legacy path only). Do not assume that is permanent — it is a
20GB liability someone will fix (guard-4085: ask whether the store RETAINS what
you are diffing against).

Read-only. Touches no store, takes no lock, writes nothing.

    py -3 core/scripts/goal-resolve.py g-335-860 --recover --json
"""

import argparse
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _goal_census import census_evicted_ids  # noqa: E402

GOAL_ID_RE = re.compile(r"^g-\d+-\d+$")


def _world():
    w = os.environ.get("WORLD_PATH") or os.environ.get("WORLD_DIR")
    if not w:
        raise SystemExit("WORLD_PATH unset — source core/scripts/_paths.sh first")
    return w


def _iter_aspirations(world, fname):
    p = os.path.join(world, fname)
    if not os.path.isfile(p):
        return
    with open(p, "rb") as fh:
        for line in fh.read().decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def resolve(goal_id, world=None):
    """-> dict with disposition/status/source. Never raises on a missing store."""
    world = world or _world()
    out = {"goal_id": goal_id, "disposition": "unknown", "status": None,
           "aspiration_id": None, "source": None, "title": None}

    for fname, disp in (("aspirations.jsonl", "live"),
                        ("aspirations-archive.jsonl", "archived")):
        for asp in _iter_aspirations(world, fname):
            for g in (asp.get("goals") or []):
                if g.get("id") == goal_id:
                    out.update(disposition=disp, status=g.get("status"),
                               aspiration_id=asp.get("id"), source=fname,
                               title=g.get("title"),
                               outcome_note=g.get("outcome_note") or "")
                    return out

    # No record anywhere. The census is the tombstone store.
    for fname in ("aspirations.jsonl", "aspirations-archive.jsonl"):
        for asp in _iter_aspirations(world, fname):
            for status, ids in census_evicted_ids(asp).items():
                if goal_id in ids:
                    out.update(disposition="evicted", status=status,
                               aspiration_id=asp.get("id"),
                               source="%s:archived_census" % fname)
                    return out
    return out


def _snapshots(world):
    """(timestamp, hash) for aspirations.jsonl, oldest -> newest."""
    d = os.path.join(world, ".history", "snapshots", "aspirations.jsonl")
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not name.endswith(".yaml"):
            continue
        h = None
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("hash:"):
                        h = line.split(":", 1)[1].strip()
                        break
        except Exception:
            continue
        if h:
            out.append((name[:-5], h))
    out.sort()
    return out


def _blob_has(world, blob_hash, goal_id):
    """(present_as_record, record_or_None). Decompresses one CAS blob."""
    p = os.path.join(world, ".history", "blobs", blob_hash[:2], blob_hash[2:] + ".gz")
    if not os.path.isfile(p):
        return False, None
    try:
        raw = gzip.decompress(open(p, "rb").read())
    except Exception:
        return False, None
    txt = raw.decode("utf-8", "replace")
    if goal_id not in txt:          # cheap prefilter; a hit may still be prose
        return False, None
    for line in txt.splitlines():
        if goal_id not in line:
            continue
        try:
            asp = json.loads(line)
        except Exception:
            continue
        for g in (asp.get("goals") or []):
            if g.get("id") == goal_id:
                return True, {"status": g.get("status"),
                              "title": g.get("title"),
                              "outcome_note": g.get("outcome_note") or "",
                              "aspiration_id": asp.get("id")}
    return False, None


def recover(goal_id, world=None, max_blobs=40):
    """Newest snapshot still holding the record, via binary search.

    Containment is MONOTONE IN TIME under eviction (a goal present at t is
    present at every t' < t), so bisection finds the newest hit in ~log2(N)
    decompressions instead of N. That assumption is the one thing that could
    make this wrong, so it is REPORTED (`assumption`) rather than hidden: a
    resurrection could in principle re-add a record after removal, which would
    make containment non-monotone and could hide a later copy.
    """
    world = world or _world()
    snaps = _snapshots(world)
    result = {"searched_snapshots": len(snaps), "blobs_read": 0,
              "recovered": False, "assumption": "containment monotone in time"}
    if not snaps:
        return result

    lo, hi = 0, len(snaps) - 1
    best = None
    reads = 0
    while lo <= hi and reads < max_blobs:
        mid = (lo + hi) // 2
        reads += 1
        ok, rec = _blob_has(world, snaps[mid][1], goal_id)
        if ok:
            best = (snaps[mid][0], rec)
            lo = mid + 1          # a newer copy may exist
        else:
            hi = mid - 1
    result["blobs_read"] = reads
    if best:
        result.update(recovered=True, snapshot=best[0], **best[1])
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resolve a goal id across live, archive, eviction census and history.")
    ap.add_argument("goal_id", nargs="+")
    ap.add_argument("--recover", action="store_true",
                    help="also search world/.history for the record's last surviving copy")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-blobs", type=int, default=40)
    a = ap.parse_args(argv)

    world = _world()
    results = []
    for gid in a.goal_id:
        if not GOAL_ID_RE.match(gid):
            results.append({"goal_id": gid, "disposition": "invalid-id",
                            "error": "expected g-NNN-NN"})
            continue
        r = resolve(gid, world)
        if a.recover and r["disposition"] in ("evicted", "unknown"):
            r["recovery"] = recover(gid, world, a.max_blobs)
        results.append(r)

    if a.json:
        print(json.dumps(results, indent=1))
        return 0
    for r in results:
        print("%-14s %-9s %-12s %s" % (r["goal_id"], r["disposition"],
                                       r.get("status") or "-",
                                       r.get("aspiration_id") or ""))
        if r.get("title"):
            print("               title: %s" % r["title"][:100])
        rec = r.get("recovery")
        if rec:
            if rec.get("recovered"):
                print("               RECOVERED from %s (%d blobs read): status=%s, outcome_note=%d chars"
                      % (rec["snapshot"], rec["blobs_read"], rec.get("status"),
                         len(rec.get("outcome_note") or "")))
                print("               title: %s" % (rec.get("title") or "")[:100])
            else:
                print("               not recoverable from history (%d snapshots, %d blobs read)"
                      % (rec["searched_snapshots"], rec["blobs_read"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
