"""Regression tests for the  merge registrations in
coordination_merge._HANDLERS:

  * world/tree-debt.jsonl        -> merge_append_only_jsonl (line-union)
  * world/aspirations-archive.jsonl -> merge_aspirations   (by-id union)

tree-debt.jsonl is PURE append-only (verified per rb-245 by reading both
writers: CLI tree.py _write_tree_debt_entry unlocked "a" append + daemon
tree_write.py _write_tree_debt_entry locked "a" append; zero rewriters, no
hygiene cap, no drain), so the both-diverged reconcile is the line-union.
The writers dump ensure_ascii=False while the union normalizes to
ensure_ascii=True — dedup keys on the PARSED record, so a raw-UTF-8 line and
its \\uXXXX twin collapse to one.

aspirations-archive.jsonl is NOT append-only: complete / complete_intent /
retire append whole-aspiration records, but archive_sweep REWRITES the file
and normalizes every record (_normalize_terminal_goals_in) — records are
id-keyed and mutated in place, so a line-union would duplicate same-id
copies differing only by normalization drift. It shares the record shape
and flow with aspirations.jsonl, so it takes merge_aspirations (the
pipeline-archive/merge_pipeline precedent). Resurrection-safe because no
restore-from-archive flow exists and goal-eviction never writes here.

Governing invariant stays BYTE commutativity (guard-907):
merge(a, b) == merge(b, a) exactly, plus multiround convergence.
"""
import json
import sys
from pathlib import Path

import pytest  # noqa: F401 — harness parity with sibling suites

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402


def _jsonl(records, ensure_ascii=True):
    return "".join(
        json.dumps(r, ensure_ascii=ensure_ascii) + "\n" for r in records
    ).encode("utf-8")


def _merged_pair(fn, a, b):
    """Merge both orders; assert byte commutativity; return one result."""
    ab = fn(a, b)
    ba = fn(b, a)
    assert ab == ba, "merge must stay byte-commutative (guard-907)"
    return ab


def _parse(out: bytes):
    return [json.loads(l) for l in out.decode("utf-8").splitlines() if l.strip()]


# --- registry dispatch --------------------------------------------------------

def test_tree_debt_registered_line_union():
    h = cm.merge_handler_for("world/tree-debt.jsonl")
    assert h is cm.merge_append_only_jsonl


def test_aspirations_archive_registered_merge_aspirations():
    h = cm.merge_handler_for("/any/prefix/aspirations-archive.jsonl")
    assert h is cm.merge_aspirations


# --- tree-debt line-union -----------------------------------------------------

def _debt(ts, parent, just):
    return {"timestamp": ts, "parent": parent, "capability_level": "CALIBRATE",
            "limit": 4, "current": 4, "context": "add-child",
            "justification": just, "source_agent": "zeta"}


def test_tree_debt_union_keeps_both_sides_new_appends():
    base = _debt("2026-07-10T08:00:00", "system", "baseline")
    a_new = _debt("2026-07-11T09:00:00", "system", "box-a append")
    b_new = _debt("2026-07-11T09:00:30", "intelligence", "box-b append")
    out = _merged_pair(cm.merge_append_only_jsonl,
                       _jsonl([base, a_new]), _jsonl([base, b_new]))
    recs = _parse(out)
    assert len(recs) == 3  # baseline collapsed to one; both appends kept
    assert recs[0] == base  # chronological order preserved
    assert recs[1] == a_new
    assert recs[2] == b_new


def test_tree_debt_raw_utf8_and_escaped_twin_collapse():
    """CLI/daemon writers dump ensure_ascii=False; the union re-emits
    ensure_ascii=True. The SAME record present as raw UTF-8 on one side and
    \\uXXXX-escaped on the other must collapse to ONE (dedup is on the parsed
    record, not the source bytes)."""
    rec = _debt("2026-07-11T09:00:00", "system", "unicode — dash")
    raw = _jsonl([rec], ensure_ascii=False)   # what the writers append
    esc = _jsonl([rec], ensure_ascii=True)    # what a prior merge emitted
    assert raw != esc  # the premise: byte-different encodings of one record
    out = _merged_pair(cm.merge_append_only_jsonl, raw, esc)
    recs = _parse(out)
    assert recs == [rec]


def test_tree_debt_multiround_convergence():
    a = _jsonl([_debt("2026-07-11T08:00:00", "system", "a")])
    b = _jsonl([_debt("2026-07-11T08:01:00", "system", "b")])
    m1 = _merged_pair(cm.merge_append_only_jsonl, a, b)
    m2 = _merged_pair(cm.merge_append_only_jsonl, m1, b)
    m3 = _merged_pair(cm.merge_append_only_jsonl, m1, a)
    assert m1 == m2 == m3


# --- aspirations-archive by-id union ------------------------------------------

def _asp(aid, goals, **kw):
    a = {"id": aid, "title": f"archived {aid}", "status": "completed",
         "archived": True, "goals": goals}
    a.update(kw)
    return a


def _goal(gid, **kw):
    g = {"id": gid, "title": f"goal {gid}", "status": "completed",
         "recurring": False, "created_at": "2026-07-01T00:00:00",
         "last_modified": "2026-07-01T00:00:00"}
    g.update(kw)
    return g


def test_archive_normalization_drift_merges_not_duplicates():
    """The headline reason line-union is WRONG here: box A holds the asp as
    appended by complete(); box B holds the SAME asp after its archive_sweep
    normalized it (e.g. terminal goal's stale claim pair popped, newer
    last_modified). A line-union would keep BOTH copies; the by-id union must
    produce exactly ONE."""
    stale = _goal("g-900-01", claimed_by="zeta",
                  claimed_at="2026-07-01T00:00:00")
    normalized = _goal("g-900-01",
                       last_modified="2026-07-02T00:00:00")  # pair popped
    side_a = _jsonl([_asp("asp-900", [stale])])
    side_b = _jsonl([_asp("asp-900", [normalized])])
    out = _merged_pair(cm.merge_aspirations, side_a, side_b)
    recs = _parse(out)
    assert len(recs) == 1
    goals = recs[0]["goals"]
    assert len(goals) == 1
    # terminal non-recurring goal must not carry a claim pair (write-path
    # Rule 3 mirror in _merge_goal, 8)
    assert "claimed_by" not in goals[0]
    assert "claimed_at" not in goals[0]


def test_archive_one_side_only_aspiration_kept():
    only_a = _asp("asp-901", [_goal("g-901-01")])
    both = _asp("asp-902", [_goal("g-902-01")])
    out = _merged_pair(cm.merge_aspirations,
                       _jsonl([both, only_a]), _jsonl([both]))
    ids = {r["id"] for r in _parse(out)}
    assert ids == {"asp-901", "asp-902"}


def test_archive_multiround_convergence():
    a = _jsonl([_asp("asp-903", [_goal("g-903-01", claimed_by="zeta",
                                       claimed_at="2026-07-01T00:00:00")])])
    b = _jsonl([_asp("asp-903", [_goal("g-903-01")]),
                _asp("asp-904", [_goal("g-904-01")])])
    m1 = _merged_pair(cm.merge_aspirations, a, b)
    m2 = _merged_pair(cm.merge_aspirations, m1, b)
    m3 = _merged_pair(cm.merge_aspirations, m1, a)
    assert m1 == m2 == m3
