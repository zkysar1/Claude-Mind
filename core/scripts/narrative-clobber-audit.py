#!/usr/bin/env python3
"""Detect narrative-field CLOBBERS in the goal store (guard-5228).

`aspirations-update-goal.sh <id> progress_note|outcome_note` REPLACES the field.
It does not append and it warns about nothing, so a write meant as an addendum
silently destroys the prior note. Four such losses were measured in a single
session on 2026-08-26 (zeta, cc-02); three of the four destroyed PEER findings,
including a safety correction against a command that would have emptied a live
production place.

THE PREDICATE IS CONTAINMENT, NOT LENGTH. A length delta detects only the
shrinking case: one measured clobber read +11 chars because the replacing note
happened to be the same size as the one it destroyed. This audit asks whether
the pre-write content still appears in the post-write content, which is the
question that actually matters.

MECHANISM. `.history` snapshots are PRE-write: the snapshot whose manifest
summary names an operation holds the state BEFORE that operation. So for a
snapshot at index i (newest-first), the post-state is snapshot i-1. New-store
snapshots carry their summary in the MANIFEST, not in a sibling `.meta` file —
reading `.meta` returns empty for every one of them and yields a confident
zero hits (measured; the first version of this audit did exactly that).

SCOPE — THIS AUDIT IS BOX-LOCAL AND CANNOT SEE PEER AGENTS. `.history/` is in
`owncloud_sync._EXCLUDE_NAMES` and is pruned from the sync walk, so it is never
pushed: it records only writes made ON THIS MACHINE. A fleet whose other agents
run on other boxes therefore reports as a single-agent population here. Measured
2026-08-26 (zeta, cc-02): 4000 manifests spanning 2026-08-12..08-26 yielded 691
narrative writes and `by_agent: {zeta: 691}` — that is the instrument's blind
spot, NOT evidence that peers do not clobber. A clean run means "no clobber by
the resident agent on this box"; it can never mean "the fleet is clean"
(guard-1715: a scan with an empty population reports clean identically to one
that examined everything). To cover the fleet, run this on each box.

Read-only. Never writes, never mutates the store.
"""
import argparse, json, re, sys, pathlib

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import history as H
import _history_store
from _history_store import _read_manifest

FIELD_RE = re.compile(r"update-goal\s+(g-\d+-\d+)\s+(progress_note|outcome_note)")


def _summary(snap):
    try:
        return (_read_manifest(snap).get("summary") or "").strip()
    except Exception:
        return ""


def _note(content, gid, field):
    for line in content.splitlines():
        if gid not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for g in rec.get("goals", []):
            if g.get("id") == gid:
                return g.get(field) or ""
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", type=int, default=400,
                    help="manifests to scan for narrative writes (cheap; default 400)")
    ap.add_argument("--examine", type=int, default=20,
                    help="most-recent narrative writes to materialize (expensive; default 20)")
    ap.add_argument("--file", default="world/aspirations.jsonl")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    target = H.resolve_target(a.file)
    base = H.resolve_base_dir(target)
    snaps = H._find_history_snapshots(target)[:a.scan]

    non_empty = 0
    hits = []
    for i, p in enumerate(snaps):
        s = _summary(p)
        if s:
            non_empty += 1
        if i > 0:
            m = FIELD_RE.search(s)
            if m:
                hits.append((i, p, m.group(1), m.group(2)))

    # POSITIVE CONTROL. Zero non-empty summaries across a non-empty snapshot list
    # means the manifest read is broken, not that nothing was written — the exact
    # failure that made this audit's first version report 0 hits (guard-2298).
    if snaps and non_empty == 0:
        print("[narrative-clobber-audit] MANIFEST READ IS BROKEN: %d snapshots, 0 with a "
              "summary. This is NOT a clean result — hits below are meaningless."
              % len(snaps), file=sys.stderr)
        return 2

    cache = {}

    def load(p):
        if p.name not in cache:
            c = _history_store.restore(target, p.name, base)
            cache[p.name] = c.decode("utf-8") if isinstance(c, bytes) else c
        return cache[p.name]

    rows = []
    for i, p, gid, field in hits[:a.examine]:
        _ts, agent = H.parse_snapshot_name(p.name)
        pre = _note(load(p), gid, field)
        post = _note(load(snaps[i - 1]), gid, field)
        if pre is None or post is None:
            continue
        if not pre:
            verdict = "new"
        elif pre.strip() in post:
            verdict = "preserved"
        else:
            verdict = "CLOBBERED"
        rows.append({"snapshot": p.name[:19], "agent": agent, "goal": gid,
                     "field": field, "pre_chars": len(pre), "post_chars": len(post),
                     "verdict": verdict})

    clob = [r for r in rows if r["verdict"] == "CLOBBERED"]
    by_agent = {}
    for r in clob:
        by_agent[r["agent"]] = by_agent.get(r["agent"], 0) + 1

    if a.json:
        print(json.dumps({"scanned": len(snaps), "narrative_writes_found": len(hits),
                          "examined": len(rows), "clobbered": len(clob),
                          "by_agent": by_agent, "rows": rows}, indent=1))
    else:
        print("scanned=%d manifests | narrative writes found=%d | examined=%d"
              % (len(snaps), len(hits), len(rows)))
        print("%-20s %-9s %-13s %-13s %6s %6s  %s"
              % ("snapshot", "agent", "goal", "field", "pre", "post", "verdict"))
        for r in rows:
            mark = "  <==" if r["verdict"] == "CLOBBERED" else ""
            print("%-20s %-9s %-13s %-13s %6d %6d  %s%s"
                  % (r["snapshot"], r["agent"], r["goal"], r["field"],
                     r["pre_chars"], r["post_chars"], r["verdict"], mark))
        print("\nCLOBBERED=%d  by agent: %s" % (len(clob), by_agent or "none"))
        print("SCOPE: box-local. .history is never synced, so only writes made ON THIS "
              "MACHINE are visible — a single-agent result is expected and is NOT a "
              "fleet all-clear. Run on each box to cover the fleet.")
        if clob:
            print("Recover per guard-5228: the snapshot named in each row IS the pre-write "
                  "state; _history_store.restore() RETURNS content instead of writing it.")
    return 1 if clob else 0


if __name__ == "__main__":
    sys.exit(main())
