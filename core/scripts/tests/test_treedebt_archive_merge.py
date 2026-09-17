"""Regression tests for the  merge registrations in
coordination_merge._HANDLERS:

  * world/tree-debt.jsonl        -> merge_append_only_jsonl (line-union)
  * world/aspirations-archive.jsonl -> merge_aspirations   (by-id union)

tree-debt.jsonl is PURE append-only (verified per rb-245 by reading both
writers: CLI tree.py _write_tree_debt_entry + daemon
tree_write.py _write_tree_debt_entry; zero rewriters, no
hygiene cap, no drain), so the both-diverged reconcile is the line-union.
BOTH writers now take the LOCKED append. The CLI one was a bare
open(..., "a") until g-358-174 rerouted it onto _fileops.locked_append_jsonl
— that reroute is what registers the audit-trail row, and it is pinned below
by test_tree_debt_write_registers_audit_trail_row (g-358-178).
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
import os
import subprocess
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


# --- tree-debt WRITE path: the audit-trail row () --------------------
#
# Everything above exercises the MERGE handler; nothing above reaches the
# WRITER. So until this section existed the store's reconcile was pinned while
# the property that makes a write reconcilable AT ALL — that it enters the
# locked read-modify-write cycle and registers an audit-trail row — was not.
# A regression to a bare open(..., "a") would be SILENT in exactly the way the
# original  defect was: the debt store keeps filling, its rows keep
# parsing, and only the trail goes quiet.
#
# The write runs in a SUBPROCESS against an isolated tmp world. tree.py
# resolves WORLD_DIR at IMPORT time, so the redirect has to be in place before
# the import — not arrangeable in-process without reloading tree into this
# pytest process, which would additionally expose the memoized-backend
# poisoning class (). STORAGE_BACKEND is pinned local EXPLICITLY
# rather than inherited: a subprocess doing os.environ.copy() under own-cloud
# derives its S3 key from the customer prefix, NOT from the MIND_WORLD
# override, so an "isolated" tmp write would land on the PRODUCTION key
# (guard-955, rb-2983 — that is how world/aspirations.jsonl was truncated on
# 2026-07-09).

_DEBT_WRITE_PROBE = r'''
import os, sys
from pathlib import Path

world, meta, scripts = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
os.environ["MIND_WORLD"] = str(world)
os.environ["MIND_META"] = str(meta)
os.environ["STORAGE_BACKEND"] = "local"
sys.path.insert(0, scripts)

import tree  # env MUST be set before this line — WORLD_DIR resolves at import

# Refuse to proceed if the redirect did not take. Without this guard a broken
# redirect would not fail the test, it would append to the REAL debt store.
if str(tree.WORLD_DIR) != str(world):
    print("ABORT: tmp world did not take effect, refusing to write",
          file=sys.stderr)
    raise SystemExit(2)

tree._write_tree_debt_entry({
    "timestamp": "2026-09-17T00:00:00",
    "parent": "g-358-178-probe",
    "capability_level": "CALIBRATE",
    "limit": 7,
    "current": 8,
    "context": "audit-trail-pin",
    "justification": "isolated tmp-world probe",
    "source_agent": "test",
})
'''


def _write_one_debt_entry(tmp_path):
    """Run the REAL CLI writer once against an isolated tmp world."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    scripts = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"  # guard-955 — never inherit own-cloud
    proc = subprocess.run(
        [sys.executable, "-c", _DEBT_WRITE_PROBE,
         str(world), str(meta), scripts],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"debt-write probe failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return world


def test_tree_debt_write_registers_audit_trail_row(tmp_path):
    """'s fix, pinned: a debt write must register an audit-trail row.

    This is the assertion that goes RED when the writer reverts to a bare
    open(..., "a"). Proved non-vacuous with core/scripts/mutation-proof-test.sh
    rather than by inference (g-358-178, guard-1595).
    """
    world = _write_one_debt_entry(tmp_path)
    debt_name = "tree-debt.jsonl"
    debt = world / debt_name
    trail = world / "changelog.jsonl"

    # Positive control FIRST: prove the write happened at all. Without it an
    # empty trail below is ambiguous between "no trail row was registered"
    # (the regression) and "no write occurred" (a broken probe) — two causes,
    # one symptom.
    assert debt.exists(), "the debt store was never written — probe is broken"
    rows = [ln for ln in debt.read_text(encoding="utf-8").splitlines()
            if ln.strip()]
    assert len(rows) == 1, f"expected exactly 1 debt row, got {len(rows)}"
    # guard-5469: verify by TYPE, not by count alone. A list handed to the
    # append writes ONE valid JSON line that every per-record consumer then
    # mis-handles, so a line count cannot tell the two apart.
    assert [type(json.loads(ln)).__name__ for ln in rows] == ["dict"]

    # THE PINNED PROPERTY.
    assert trail.exists(), (
        "no audit trail was created — the debt write never entered the locked "
        "read-modify-write cycle (g-358-174 regression)"
    )
    mentions = [ln for ln in trail.read_text(encoding="utf-8").splitlines()
                if debt_name in ln]
    assert mentions, (
        "the audit trail exists but carries no row naming the debt store — "
        "the write bypassed the locked append (g-358-174 regression)"
    )


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
    # Rule 3 mirror in _merge_goal, )
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
