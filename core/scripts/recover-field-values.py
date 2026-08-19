#!/usr/bin/env python3
"""Recover clobbered field values in a governed JSONL store — gap-078 engine.

TWO DAMAGE-SOURCE ADAPTERS OVER ONE RECOVERY ENGINE. The two encounters that
opened this gap arrived from different triggers, and that is a design input
rather than a mismatch:

  journal  A per-operation repair log written by the destructive pass, one row
           per removed value. STRICTLY RICHER WHERE IT EXISTS (rb-7384): it
           found 63 (file, record, field) triples against 57 for a null-based
           state diff, agreeing on all 57 and disagreeing on none. The 6 extra
           were PARTIALLY-stripped lists, which no sentinel test can see at any
           snapshot granularity — a value that lost 2 of 5 refs is not null and
           is not malformed, so only a per-operation log knows it changed.

  shape    A regex over a prose-bearing field, finding structurally-malformed
           values (bare filesystem path, unexpanded shell artifact, empty or
           whitespace-only). The fallback for damage with no journal. Its
           blindness is the partial class above; it is not a substitute.

Prefer `journal` whenever a journal exists. Reach for `shape` only when it does
not, and say so in the report — a shape scan that returns 0 is NOT evidence the
store is clean.

THREE INVARIANTS, each measured, each of which a naive implementation violates:

  1. RESTORE PER-RECORD THROUGH THE FENCED WRITER, never one bulk rewrite.
     rb-2084 recommends the bulk path on cost grounds and does not name what it
     costs. Measured: 25 of 63 writes were REFUSED by the store's own record
     validator, and those refusals localized a real defect blocking 29.5% of the
     corpus. A bulk write would have written past all 25 silently. N fenced
     writes are N PROBES of the write contract; the bulk write is blind to it
     (rb-7387, guard-2475).

  2. READ BACK EVERY WRITE. The refusals above were visible only because each
     result was checked. A fenced loop without read-back is the bulk path's
     blindness at N times the cost (guard-1902).

  3. CORROBORATE BEFORE WRITING. Cross-check each recovered value against a
     second source and report agreement AND disagreement counts, plus an
     aggregate reconciliation. A recovery that cannot be reconciled against an
     independently-measured delta is a claim, not a result.

EARLIEST-ROW SEMANTICS: a record may carry MULTIPLE journal rows (8 of 63 did).
The restore target is the EARLIEST row's old_value — the state before the first
destructive pass. Taking the latest restores an already-damaged intermediate.

Default is --dry-run. Writing requires --apply.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash" argv[0])

SCRIPTS = Path(__file__).resolve().parent

# Per-store fenced writer. The engine NEVER writes a store file directly — it
# shells to the store's own single-field endpoint so the store's validator,
# lock and history hooks all run. A store absent from this table is REFUSED
# rather than written by a generic path (guard-2860: never relax an ownership
# predicate into a pattern).
FENCED_WRITERS = {
    "experience": {
        "script": "experience-update-field.sh",
        "argv": lambda rid, field, value: [rid, field, value],
    },
}


def _run(argv, timeout=60):
    """Run a wrapper, returning (rc, stdout, stderr). Never raises on rc."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001 — a spawn failure is a refusal, not a crash
        return 1, "", str(e)


# ---------------------------------------------------------------- adapters


def adapter_journal(journal_paths, store_filter=None, field_filter=None):
    """Adapter (b) — read per-operation repair rows.

    Returns {(file, record_id, field): {"old_value":..., "rows": n, "at":...}}
    keyed on the EARLIEST row per triple. Rows are sorted by `repaired_at`
    ASCENDING, and the first one wins; later rows only bump the count.
    """
    rows = []
    for jp in journal_paths:
        p = Path(jp)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if store_filter and r.get("store") != store_filter:
                continue
            if field_filter and r.get("field") != field_filter:
                continue
            rows.append(r)

    rows.sort(key=lambda r: str(r.get("repaired_at") or ""))
    out = {}
    for r in rows:
        key = (r.get("file"), r.get("record_id"), r.get("field"))
        if None in key:
            continue
        if key in out:
            out[key]["rows"] += 1          # later row — count only, never overwrite
            # A later row may remove a DIFFERENT element of the same list. Collect
            # them: restoring only the earliest leaves the rest still missing.
            rr = r.get("removed_ref")
            if rr is not None and rr not in out[key]["removed_refs"]:
                out[key]["removed_refs"].append(rr)
            continue
        # RECOVERY TARGET IS NOT ALWAYS `old_value` — MEASURED, NOT ASSUMED.
        # The journal writer populates the two fields differently by field
        # CARDINALITY, and sampling one row from one store hides it:
        #   scalar field -> old_value holds the whole prior value
        #                   (and happens to equal removed_ref)
        #   list field   -> old_value is NULL and removed_ref holds the single
        #                   ELEMENT that was dropped from the list
        # Measured on the live experience rows: 32 of 38 carry old_value=None
        # while removed_ref is populated. Treating a null old_value as
        # "unrecoverable" discards 84% of the recoverable population — which is
        # exactly what this engine did on its first dry-run against real data.
        # guard-1902 says sample the schema; the sharper form is: sample it in
        # EVERY store you will write, because the same writer populates the same
        # columns differently per field shape.
        old = r.get("old_value")
        removed = r.get("removed_ref")
        out[key] = {
            "old_value": old,
            "removed_ref": removed,
            "removed_refs": [removed] if removed is not None else [],
            "new_value": r.get("new_value"),
            "store": r.get("store"),
            "rows": 1,
            "at": r.get("repaired_at"),
        }
    return out


def adapter_shape(store_file, field, pattern):
    """Adapter (a) — regex a prose-bearing field for malformed values.

    Finds damage; it does NOT know the correct value. Every hit is reported
    with recovered=None so the caller must supply a recovery source. Emitting
    a hit as if it were a restore candidate is how a scan becomes a corruptor.
    """
    rx = re.compile(pattern)
    p = Path(store_file)
    hits = {}
    if not p.is_file():
        return hits
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        val = rec.get(field)
        if isinstance(val, str) and rx.search(val):
            rid = rec.get("id") or rec.get("record_id")
            hits[(str(p), rid, field)] = {
                "old_value": None,        # UNKNOWN — the scan cannot recover it
                "removed_ref": None,
                "removed_refs": [],
                "observed": val,
                "rows": 1,
                "store": None,
            }
    return hits


def recovery_target(info):
    """The value to restore, and whether the restore is ADDITIVE.

    Returns (value, additive) or (None, False) when nothing is recoverable.
    `additive` is the load-bearing half: a list field's journal row names the
    ELEMENT that was dropped, not the prior list, so writing that element as
    the field value would replace the whole list with one string and destroy
    every surviving sibling. An additive restore appends to what survived.
    """
    if info.get("old_value") not in (None, ""):
        return info["old_value"], False        # whole prior value — replace
    refs = [r for r in (info.get("removed_refs") or []) if r not in (None, "")]
    if refs:
        return refs, True                      # dropped element(s) — append
    return None, False


# ------------------------------------------------------------ corroboration


def corroborate(store_file, record_id, field, recovered):
    """Invariant 3 — check the recovered value against a SECOND source.

    The second source here is the live record's own surviving state: if the
    field currently holds a value that already contains the recovered one, the
    damage was partial and the recovered value is corroborated by what
    survived. Returns (verdict, detail) where verdict is one of
    agree / disagree / unevaluable. `unevaluable` is deliberately NOT `agree`
    — an unreadable second source is a missing check, not a passing one.
    """
    p = Path(store_file)
    if not p.is_file():
        return "unevaluable", "store file not readable"
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get("id") or rec.get("record_id")) != record_id:
            continue
        cur = rec.get(field)
        if cur is None:
            return "agree", "field is currently null — full strip, recovery is additive"
        if isinstance(cur, list) and isinstance(recovered, str):
            if recovered in cur:
                return "disagree", f"value already present in surviving list ({len(cur)} entries)"
            return "agree", f"partial strip — {len(cur)} sibling entries survived"
        if cur == recovered:
            return "disagree", "current value already equals the recovered value"
        return "agree", f"current value differs (len {len(str(cur))})"
    return "unevaluable", "record id not found in store"


# ---------------------------------------------------------------- restore


def read_current(store_file, record_id, field):
    """Current on-disk value of one field, or (False, None) when unreadable."""
    p = Path(store_file)
    if not p.is_file():
        return False, None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get("id") or rec.get("record_id")) == record_id:
            return True, rec.get(field)
    return False, None


def restore_one(store, store_file, record_id, field, value, additive):
    """Invariant 1+2 — ONE fenced write, then READ BACK.

    Returns (ok, detail). A refusal is a first-class result, never an
    exception: refusals are the signal this engine exists to surface.

    ADDITIVE mode composes the new value from what SURVIVED plus the dropped
    element(s). Writing the dropped element as the field value would replace
    the whole list with one entry — a repair that destroys more than the damage
    did. The surviving state is re-read here rather than carried from the
    corroboration step, so a concurrent writer cannot be silently overwritten.
    """
    spec = FENCED_WRITERS.get(store)
    if not spec:
        return False, f"no fenced writer registered for store {store!r} — refusing to write"
    script = SCRIPTS / spec["script"]
    if not script.is_file():
        return False, f"fenced writer missing at {script}"

    if additive:
        ok, cur = read_current(store_file, record_id, field)
        if not ok:
            return False, "cannot read current value — refusing an additive write blind"
        survivors = cur if isinstance(cur, list) else ([] if cur is None else [cur])
        merged = list(survivors) + [v for v in value if v not in survivors]
        if merged == survivors:
            return False, "nothing to add — every dropped element already present"
        payload = json.dumps(merged)
        expect = merged
    else:
        payload = str(value)
        expect = value

    argv = bash_cmd(script.as_posix(), *spec["argv"](record_id, field, payload))
    rc, out, err = _run(argv)
    if rc != 0:
        return False, f"REFUSED rc={rc}: {(err or out).strip()[:200]}"

    # READ BACK — the write returning 0 is not evidence the value landed.
    try:
        rec = json.loads(out) if out.strip().startswith("{") else None
    except json.JSONDecodeError:
        rec = None
    if rec is None:
        return False, "write returned rc=0 but no readable record — cannot confirm"
    got = rec.get(field)
    if got == expect:
        return True, f"confirmed by read-back ({'additive' if additive else 'replace'})"
    if additive and isinstance(got, list) and all(v in got for v in value):
        return True, "confirmed by read-back (additive, store normalised the list)"
    return False, f"read-back MISMATCH: field holds {str(got)[:120]!r}"


# ------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["journal", "shape"], required=True)
    ap.add_argument("--journal", action="append", default=[],
                    help="repair-journal path (repeatable); required for --source journal")
    ap.add_argument("--store-file", help="store JSONL path; required for --source shape")
    ap.add_argument("--field", help="field name to scan/restore")
    ap.add_argument("--pattern", help="regex marking a malformed value (--source shape)")
    ap.add_argument("--store", help="store key for the fenced writer (e.g. experience)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; default is dry-run")
    ap.add_argument("--limit", type=int, default=0, help="cap candidates (0 = no cap)")
    ap.add_argument("--output", choices=["json", "human"], default="human")
    a = ap.parse_args()

    if a.source == "journal":
        if not a.journal:
            ap.error("--source journal requires at least one --journal")
        cands = adapter_journal(a.journal, store_filter=a.store, field_filter=a.field)
    else:
        if not (a.store_file and a.field and a.pattern):
            ap.error("--source shape requires --store-file, --field and --pattern")
        cands = adapter_shape(a.store_file, a.field, a.pattern)

    total_found = len(cands)
    items = sorted(cands.items())
    truncated = 0
    if a.limit and len(items) > a.limit:
        truncated = len(items) - a.limit
        items = items[:a.limit]

    results = []
    agree = disagree = unevaluable = 0
    written = refused = skipped = 0

    for (sfile, rid, field), info in items:
        target, additive = recovery_target(info)
        rec = {"file": sfile, "record_id": rid, "field": field,
               "journal_rows": info.get("rows", 1), "recovered": target,
               "mode": "additive" if additive else "replace"}

        if target is None:
            rec["action"] = "skipped"
            # Source-aware: a journal row with neither old_value nor removed_ref
            # is a different fault from a shape hit, and reporting the shape
            # story for a journal run sends the reader to the wrong place.
            rec["reason"] = (
                "shape scan located damage but cannot recover the value — supply a "
                "recovery source (prefer --source journal)"
                if a.source == "shape" else
                "journal row carries neither old_value nor removed_ref — nothing to restore"
            )
            skipped += 1
            results.append(rec)
            continue

        # Corroborate against the FIRST dropped element for an additive restore:
        # that is the value whose presence/absence in the surviving list is the
        # meaningful check.
        probe = target[0] if additive else target
        verdict, detail = corroborate(sfile, rid, field, probe)
        rec["corroboration"] = verdict
        rec["corroboration_detail"] = detail
        agree += verdict == "agree"
        disagree += verdict == "disagree"
        unevaluable += verdict == "unevaluable"

        if verdict != "agree":
            rec["action"] = "skipped"
            rec["reason"] = f"corroboration={verdict} — not writing"
            skipped += 1
            results.append(rec)
            continue

        if not a.apply:
            rec["action"] = "would-restore"
            results.append(rec)
            continue

        store_key = a.store or info.get("store")
        ok, wdetail = restore_one(store_key, sfile, rid, field, target, additive)
        rec["action"] = "restored" if ok else "refused"
        rec["write_detail"] = wdetail
        written += ok
        refused += not ok
        results.append(rec)

    summary = {
        "source": a.source,
        "applied": a.apply,
        "found": total_found,
        "examined": len(items),
        "truncated": truncated,
        "corroboration": {"agree": agree, "disagree": disagree, "unevaluable": unevaluable},
        "written": written,
        "refused": refused,
        "skipped": skipped,
        "would_restore": sum(1 for r in results if r.get("action") == "would-restore"),
        "multi_row_records": sum(1 for _, i in items if i.get("rows", 1) > 1),
    }
    # Aggregate reconciliation (invariant 3). State it even when it balances —
    # a recovery whose parts do not sum is a claim, not a result. `would_restore`
    # is a bucket in its own right: omitting it made a correct dry-run report
    # reconciles=False, which is a false alarm that trains a reader to ignore
    # the one check that would catch a real accounting hole.
    wr = summary["would_restore"]
    summary["reconciliation"] = (
        f"{summary['examined']} examined = {written} written + {refused} refused "
        f"+ {skipped} skipped + {wr} would-restore"
    )
    summary["reconciles"] = (written + refused + skipped + wr) == summary["examined"]

    if a.output == "json":
        print(json.dumps({"summary": summary, "results": results}, indent=2))
    else:
        print(f"source={a.source} apply={a.apply}")
        print(f"  found={total_found} examined={len(items)}"
              + (f" TRUNCATED={truncated}" if truncated else ""))
        print(f"  corroboration: agree={agree} disagree={disagree} unevaluable={unevaluable}")
        print(f"  written={written} refused={refused} skipped={skipped}")
        print(f"  multi-row records (earliest-row semantics applied): "
              f"{summary['multi_row_records']}")
        print(f"  {summary['reconciliation']}  reconciles={summary['reconciles']}")
        for r in results:
            if r.get("action") in ("refused",) or r.get("corroboration") == "disagree":
                print(f"    [{r['action']}] {r['record_id']}.{r['field']}: "
                      f"{r.get('write_detail') or r.get('corroboration_detail')}")
        if a.source == "shape" and total_found == 0:
            print("  NOTE: a shape scan returning 0 is NOT evidence the store is clean — "
                  "it is blind to partially-stripped values (rb-7384). "
                  "Prefer --source journal where a journal exists.")

    # Exit 3 when writes were refused: a refusal is a finding, and collapsing it
    # onto 0 (success) or 1 (broken) makes it unreadable at the call site.
    return 3 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
