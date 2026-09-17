#!/usr/bin/env python3
"""One-time max-reconcile of embedded retrieval_count/last_retrieved into the
utilization sidecar (g-358-24).

WHAT THIS HEALS. `g-358-22` spool-routed retrieval bumps, which stops
sidecar-vs-embedded divergence GROWING but does not heal the gap already
accumulated: sidecar entries first-touch-seeded during the flip window froze
while legacy retrieval bumps kept advancing the embedded copy, and
`_utilization_store.utilization_of` prefers the sidecar WHOLESALE. So consumers
(utility_ratio scoring, never-retrieved retirement sweeps) read a stale-LOW
retrieval_count for those ids.

ONLY TWO FIELDS, AND THE NARROWNESS IS THE WHOLE DESIGN — do not widen it to a
max() over the utilization dict. Measured on `guard-2115`, two dated readings of
the same record twelve days apart:

    2026-09-04  embedded times_active 15   sidecar times_active 11
    2026-09-16  embedded times_active 22   sidecar times_active 11

The EMBEDDED side advanced while the sidecar stood still, i.e. for
`times_active` the live surface is the opposite of what it is for
`retrieval_count` (same record, same window: embedded 28 frozen, sidecar
42 -> 49). A max() over the whole dict would therefore be right for one counter
and wrong for the other in the same pass, and "wrong" here means writing a
number that drives a RETIREMENT decision. The `times_active` divergence is real
and much larger (guardrails: sidecar low on 5702 of 6287 ids, total 161,347) but
its live-surface question is NOT settled by this pass and is tracked separately;
this script deliberately leaves it alone.

WHY MAX IS SAFE FOR THE TWO FIELDS IT DOES TOUCH. Both are monotone by
construction — `retrieval_count` only ever increments, `last_retrieved` only
ever advances — so max() can lose nothing and is idempotent: a second run over
an already-reconciled store computes a zero delta. That is the same property
`coordination_merge.merge_utilization_counters` relies on across boxes.

That argument is about the VALUE WRITTEN, so it has to be evaluated under the
flush lock, not against the pre-lock snapshot — `plan()` proposes, and
`merge_counters` decides against the live row (g-358-106).

WHY NOT THE SPOOL. The obvious route is `record_increment(kind, id,
"retrieval_count", gap)`, which is the canonical writer. It is wrong HERE:
`utilization-flush.apply_deltas` stamps `last_retrieved = max(existing, spool ts
date)` on every touched record, so backfilling through the spool would also
stamp TODAY onto records that were not retrieved today — manufacturing freshness
on exactly the field this pass is trying to make honest. A direct locked RMW on
the sidecar, under the same flush lock the drain takes, is the correct shape for
a backfill.

IDS NOT IN THE SIDECAR ARE SKIPPED BY DESIGN. `utilization-flush._seed_from_content`
seeds a first-touch entry from the record's whole embedded block, so an id with no
sidecar row self-heals the moment anything increments it. Writing a row here would
duplicate that path with a second policy.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import _utilization_store as us  # noqa: E402
from _paths import WORLD_DIR  # noqa: E402

KINDS = ("guardrails", "reasoning-bank")

# The complete set. Widening this tuple requires settling the live-surface
# question for the added field first — see the module docstring.
INT_FIELDS = ("retrieval_count",)
DATE_FIELDS = ("last_retrieved",)


def load_embedded(kind, world_dir=None):
    """Embedded utilization blocks for every record of `kind`.

    Reads through the BACKEND via `us.store_paths`, the same contract
    `utilization-flush._seed_from_content` uses: a returned path may name an
    object this box has never materialised locally, where a bare open() raises.
    """
    try:
        from storage_backend import get_backend
        backend = get_backend()
    except Exception:
        backend = None
    out = {}
    for path in us.store_paths(kind, world_dir):
        text = None
        try:
            if backend is not None:
                text = backend.read_bytes(path).decode("utf-8", errors="replace")
        except Exception:
            text = None
        if text is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            counters = rec.get("utilization")
            if isinstance(counters, dict):
                out[rec["id"]] = counters
    return out


def plan(sidecar_map, embedded_map):
    """Per-id {field: new_value} for every field the sidecar reads stale.

    Pure — takes two {id: counters} maps and returns the edits, so the whole
    decision is testable without touching a store.
    """
    edits = {}
    for rec_id, side in sidecar_map.items():
        emb = embedded_map.get(rec_id)
        if not isinstance(emb, dict) or not isinstance(side, dict):
            continue
        changes = {}
        for field in INT_FIELDS:
            s, e = side.get(field), emb.get(field)
            if isinstance(s, bool) or isinstance(e, bool):
                continue
            if isinstance(e, int) and (not isinstance(s, int) or s < e):
                changes[field] = e
        for field in DATE_FIELDS:
            s, e = side.get(field), emb.get(field)
            if isinstance(e, str) and e and (not isinstance(s, str) or s < e):
                changes[field] = e
        if changes:
            edits[rec_id] = changes
    return edits


def merge_counters(live, proposed):
    """Per-field monotone merge of a `plan()` proposal onto the LIVE counters.

    Pure, like `plan()`, and for the same reason: this is where the value that
    actually gets WRITTEN is decided, so it has to be testable without a store.

    `plan()` decides against a snapshot taken BEFORE the flush lock is held, so
    by the time the edit is applied the other writer that shares that lock
    (`utilization-flush.apply_deltas`) may already have raised the row. An
    unconditional `dict.update()` of the proposal would then roll a live
    `retrieval_count` DOWN and `last_retrieved` BACKWARD — manufacturing exactly
    the staleness this pass exists to remove. Taking the max per field makes the
    write monotone under concurrency: `edits` carries a PROPOSAL, not a decision,
    and the module docstring's "max() can lose nothing" argument then holds at
    the write layer and not only at the plan layer.

    A field the live row lacks (or holds at the wrong type) takes the proposal —
    that is the backfill this pass is for. The type guards mirror `plan()`'s at
    BOTH levels, and the top one is the load-bearing half: `plan()` skips a row
    whose counters are not a dict, so this must too. A bare `dict(live or {})`
    raises on a truthy non-dict (`dict("x")` -> ValueError, `dict([1,2])` ->
    TypeError), and because this runs inside `_modifier` inside
    `locked_modify_jsonl`, that raise aborts the ENTIRE kind after the lock is
    taken rather than skipping one row (g-358-114). Below the top level: a bool
    is not an int counter.
    """
    out = dict(live) if isinstance(live, dict) else {}
    for field, value in (proposed or {}).items():
        current = out.get(field)
        if field in INT_FIELDS:
            if (isinstance(current, int) and not isinstance(current, bool)
                    and isinstance(value, int) and not isinstance(value, bool)):
                value = max(current, value)
        elif field in DATE_FIELDS:
            if isinstance(current, str) and isinstance(value, str):
                value = max(current, value)
        out[field] = value
    return out


def reconcile_kind(kind, world_dir, apply_changes):
    sidecar_map = us.load_counters(kind, world_dir)
    embedded_map = load_embedded(kind, world_dir)
    edits = plan(sidecar_map, embedded_map)

    int_gap = sum(
        edits[i][f] - (sidecar_map[i].get(f) if isinstance(sidecar_map[i].get(f), int) else 0)
        for i in edits for f in INT_FIELDS if f in edits[i]
    )
    summary = {
        "kind": kind,
        "sidecar_ids": len(sidecar_map),
        "embedded_ids": len(embedded_map),
        "intersect": len(set(sidecar_map) & set(embedded_map)),
        "records_to_edit": len(edits),
        "int_total_gap": int_gap,
        "int_field_hits": {f: sum(1 for i in edits if f in edits[i]) for f in INT_FIELDS},
        "date_field_hits": {f: sum(1 for i in edits if f in edits[i]) for f in DATE_FIELDS},
        "applied": False,
    }
    if not edits or not apply_changes:
        return summary, edits

    sidecar_path = us.counters_path(kind, world_dir)
    if sidecar_path is None:
        summary["error"] = "no sidecar path"
        return summary, edits

    # REALIZED delta, accumulated under the lock (). `int_total_gap`
    # above is computed from the pre-lock snapshot, so it sizes the PROPOSAL;
    # once merge_counters started refusing rollbacks, the proposal stopped being
    # the outcome. Reporting only the proposal beside `applied: true` reads as an
    # account of what was written.
    applied = {"int_gap": 0, "records": 0}

    def _modifier(items):
        # Reset per invocation: locked_modify_jsonl re-runs the modifier on a
        # backend conflict retry (OwnCloudBackend.conflict_error is non-empty;
        # LocalBackend's is the empty tuple), and an accumulator carried across
        # the retry would double-count the same rows.
        applied["int_gap"] = 0
        applied["records"] = 0
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("id") in edits:
                live = it.get("utilization")
                counters = merge_counters(live, edits[it["id"]])
                live_counters = live if isinstance(live, dict) else {}
                for field in INT_FIELDS:
                    after = counters.get(field)
                    if not isinstance(after, int) or isinstance(after, bool):
                        continue
                    before = live_counters.get(field)
                    if not isinstance(before, int) or isinstance(before, bool):
                        before = 0
                    applied["int_gap"] += after - before
                if counters != live_counters:
                    applied["records"] += 1
                out.append({**it, "utilization": counters})
            else:
                out.append(it)
        return out

    base = Path(world_dir or WORLD_DIR)
    lock_path = base / us.flush_lock_name(kind)
    from storage_backend import LocalBackend
    from _fileops import locked_modify_jsonl
    lb = LocalBackend()
    lb.acquire_lock(lock_path, timeout=30, stale_seconds=120)
    try:
        locked_modify_jsonl(sidecar_path, _modifier, initial=[])
    finally:
        try:
            lb.release_lock(lock_path)
        except Exception:
            pass
    summary["applied"] = True
    summary["int_applied_gap"] = applied["int_gap"]
    summary["records_actually_changed"] = applied["records"]
    return summary, edits


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=KINDS + ("all",), default="all")
    ap.add_argument("--apply", action="store_true",
                    help="write the reconcile (default is a dry run)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show", type=int, default=0,
                    help="print the first N per-id edits")
    args = ap.parse_args(argv)

    kinds = KINDS if args.kind == "all" else (args.kind,)
    results = []
    for kind in kinds:
        summary, edits = reconcile_kind(kind, WORLD_DIR, args.apply)
        results.append(summary)
        if args.show:
            for rec_id in sorted(edits)[: args.show]:
                print(f"  {kind} {rec_id}: {edits[rec_id]}", file=sys.stderr)
    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        for r in results:
            realized = ("; REALIZED int gap {ag} across {ac} record(s)".format(
                ag=r["int_applied_gap"], ac=r["records_actually_changed"])
                if "int_applied_gap" in r else "")
            print("[utilization-sidecar-reconcile] {kind}: {n} record(s) to edit "
                  "(retrieval_count {rc}, last_retrieved {lr}); PROPOSED int gap {g}"
                  "{rz}; applied={a}  [sidecar {s} / embedded {e} / intersect {i}]".format(
                      kind=r["kind"], n=r["records_to_edit"],
                      rc=r["int_field_hits"]["retrieval_count"],
                      lr=r["date_field_hits"]["last_retrieved"],
                      g=r["int_total_gap"], rz=realized, a=r["applied"],
                      s=r["sidecar_ids"], e=r["embedded_ids"], i=r["intersect"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
