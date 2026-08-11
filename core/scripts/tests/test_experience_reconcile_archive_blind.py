"""test_experience_reconcile_archive_blind.py -- regression for .

experience-reconcile.py took its orphan difference against the LIVE index alone
(`md_stems - jsonl_ids`) and never opened experience-archive.jsonl. Archival
MOVES a row rather than copying it -- measured fleet-wide, |live n archive| is
0-2 per agent -- so "absent from live" means "archived", not "unindexed", and
every aged trace was reported as an orphan awaiting backfill.

Measured 2026-08-02 (alpha, hostname cc-04, uname -r 6.8.0-136-generic):

    agent    orphan (live-only)   orphan (both)   wrongly reported
    alpha            517               136              381
    bravo            548                43              505
    echo             183                42              141
    foxtrot          294                82              212
    zeta             299                72              227

1,841 reported vs 375 real. --apply would have appended 1,466 index rows for
traces that were already indexed -- and the daemon write path's
_check_no_duplicate_id already spans BOTH stores and returns 409 duplicate_id
for exactly those appends, but --apply calls locked_append_jsonl directly and
routes around it.

WHAT IS PINNED HERE, and why each half is needed:
  1. An archive-only stem is not an orphan and is not backfilled (the fix).
  2. A stem in NEITHER store still IS an orphan and still IS backfilled (the
     positive control -- without it, a test that broke orphan detection
     entirely would pass every assertion in half 1).
  3. The excluded population is REPORTED as `archived_indexed`, so a run that
     drops thousands of records cannot print the same shape as a clean one.
  4. INDEX_FILES names both stores (structural guard against re-narrowing).

The live index deliberately remains the sole SOURCE for the missing_md /
stem_mismatch direction; test_archive_rows_do_not_drive_md_regeneration pins
that the fix did not widen into stub-regeneration for archived rows.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "experience_reconcile",
    Path(__file__).resolve().parents[1] / "experience-reconcile.py",
)
reconcile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconcile)


STEM = "exp-g-999-01"

TRACE_MD = """---
type: goal_execution
category: framework-hygiene
goal_id: g-999-01
date: 2026-08-02
---

# A trace whose index row has aged into the archive

Body text.
"""


def _mk_agent(tmp_path, monkeypatch, *, live="", archive=None):
    """Throwaway agent dir carrying one .md trace, with configurable indexes.

    `archive=None` omits experience-archive.jsonl entirely (the pre-archive
    agent shape); `archive=""` creates it empty.
    """
    agent_dir = tmp_path / "agents" / "testagent"
    (agent_dir / "experience").mkdir(parents=True)
    (agent_dir / "experience.jsonl").write_text(live, encoding="utf-8")
    if archive is not None:
        (agent_dir / "experience-archive.jsonl").write_text(archive, encoding="utf-8")
    (agent_dir / "experience" / f"{STEM}.md").write_text(TRACE_MD, encoding="utf-8")
    monkeypatch.setattr(reconcile, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(reconcile, "_agent_dir", lambda name: agent_dir)
    monkeypatch.setattr(reconcile, "load_goal_index", lambda: {})
    return agent_dir


def _archive_row(stem=STEM):
    return json.dumps({
        "id": stem,
        "type": "goal_execution",
        "category": "framework-hygiene",
        "summary": "aged trace",
        "content_path": f"agents/testagent/experience/{stem}.md",
        "archived": True,
    }) + "\n"


# --------------------------------------------------------------------------
# 1. The fix
# --------------------------------------------------------------------------

def test_archive_only_stem_is_not_an_orphan(tmp_path, monkeypatch):
    _mk_agent(tmp_path, monkeypatch, live="", archive=_archive_row())
    r = reconcile.reconcile_agent("testagent", apply=False)
    assert r["before"]["orphan_md"] == 0
    assert r["actions"]["jsonl_records_backfilled"] == 0
    assert r["actions"]["deferred"] == 0


def test_archive_only_stem_is_reported_as_archived_indexed(tmp_path, monkeypatch):
    """The excluded population stays visible -- guard-1760.

    Dropping it from orphan_md without reporting it anywhere would make a run
    that silently excluded 505 records indistinguishable from a clean one.
    """
    _mk_agent(tmp_path, monkeypatch, live="", archive=_archive_row())
    r = reconcile.reconcile_agent("testagent", apply=False)
    assert r["before"]["archived_indexed"] == 1


def test_apply_does_not_append_a_row_for_an_archived_stem(tmp_path, monkeypatch):
    """The actual hazard: --apply appending a duplicate id into the live index.

    locked_append_jsonl is stubbed rather than exercised so the assertion is
    about WHAT would be written, independent of history/changelog side effects.
    """
    appended = []
    monkeypatch.setattr(reconcile, "locked_append_jsonl",
                        lambda path, rec: appended.append((Path(path).name, rec)))
    _mk_agent(tmp_path, monkeypatch, live="", archive=_archive_row())
    reconcile.reconcile_agent("testagent", apply=True)
    assert [a for a in appended if a[0] == "experience.jsonl"] == []


# --------------------------------------------------------------------------
# 2. The positive control -- orphan detection still works
# --------------------------------------------------------------------------

def test_stem_in_neither_store_is_still_an_orphan(tmp_path, monkeypatch):
    """Without this, breaking orphan detection outright would pass part 1.

    Same corpus, same code path; only the archive row is removed.
    """
    _mk_agent(tmp_path, monkeypatch, live="", archive="")
    r = reconcile.reconcile_agent("testagent", apply=False)
    assert r["before"]["orphan_md"] == 1
    assert r["before"]["archived_indexed"] == 0
    assert r["actions"]["jsonl_records_backfilled"] == 1


def test_apply_does_append_a_row_for_a_genuine_orphan(tmp_path, monkeypatch):
    """Mutation-side control for test_apply_does_not_append_a_row_...

    An assertion that a list is empty is satisfied by a stub that never fires;
    this proves the stub fires on the case that should write.
    """
    appended = []
    monkeypatch.setattr(reconcile, "locked_append_jsonl",
                        lambda path, rec: appended.append((Path(path).name, rec)))
    _mk_agent(tmp_path, monkeypatch, live="", archive="")
    reconcile.reconcile_agent("testagent", apply=True)
    live_appends = [a for a in appended if a[0] == "experience.jsonl"]
    assert len(live_appends) == 1
    assert live_appends[0][1]["id"] == STEM


def test_absent_archive_file_degrades_to_live_only(tmp_path, monkeypatch):
    """A missing archive must not crash, and must fail toward OVER-reporting.

    Over-reporting is the pre-fix behavior and is recoverable by a human
    reading the number; silently suppressing an orphan is not.
    """
    _mk_agent(tmp_path, monkeypatch, live="", archive=None)
    r = reconcile.reconcile_agent("testagent", apply=False)
    assert r["before"]["orphan_md"] == 1
    assert r["before"]["archived_indexed"] == 0


# --------------------------------------------------------------------------
# 3. Scope pins -- the fix must not widen
# --------------------------------------------------------------------------

def test_index_files_names_both_stores():
    assert reconcile.INDEX_FILES == ("experience.jsonl", "experience-archive.jsonl")


def test_archive_index_is_named_not_positional():
    """archived_ids must not reach the archive as INDEX_FILES[1].

    A positional read survives a reorder of the tuple silently: archived_ids
    would return LIVE ids, the orphan difference would subtract live twice,
    and the fix would revert with every behavioral test still green (they all
    build their fixtures from the same two filenames). Pinning the named
    constant is what makes the reorder detectable at all.
    """
    assert reconcile.ARCHIVE_INDEX == "experience-archive.jsonl"
    assert reconcile.LIVE_INDEX == "experience.jsonl"
    src = (Path(__file__).resolve().parents[1] / "experience-reconcile.py").read_text(
        encoding="utf-8")
    # Non-comment lines ONLY. The unanchored form fails on its own
    # documentation: the comment above INDEX_FILES explains why the
    # positional read is wrong and therefore contains the literal, so a
    # whole-file scan reports the very prose that prevents the defect.
    # Same shape as guard-1099 (a /verify-learning check that counted the
    # comments quoting a deleted glob as live code) and guard-1238 (a probe
    # matching its own command text). Caught here by this test failing on
    # its first run, which is the cheap version of learning it.
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert "INDEX_FILES[1]" not in "\n".join(code)


# --------------------------------------------------------------------------
# 4. The guard must not disarm itself (fresh-eyes review of this same fix)
# --------------------------------------------------------------------------

def test_unreadable_archive_raises_rather_than_disarming_the_guard(tmp_path, monkeypatch):
    """An archive that EXISTS but cannot be read must not read as "no ids".

    This is the one place the script's defer-rather-than-crash posture is
    wrong: swallowing the OSError returns an empty guard set, every archived
    record becomes an orphan candidate again, and --apply re-arms the exact
    duplicate-append hazard this fix removes. Absence is legitimate and stays
    quiet (test_absent_archive_file_degrades_to_live_only); unreadability is
    an error and must be loud.
    """
    agent_dir = _mk_agent(tmp_path, monkeypatch, live="", archive=_archive_row())

    real_open = Path.open

    def boom(self, *a, **kw):
        if self.name == reconcile.ARCHIVE_INDEX:
            raise OSError("simulated unreadable archive")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom)
    try:
        reconcile.archived_ids(agent_dir)
    except OSError:
        return
    raise AssertionError("archived_ids swallowed an OSError on an existing archive")


def test_malformed_archive_line_is_counted_not_hidden(tmp_path, monkeypatch):
    """A corrupt archive row drops one id from the guard -- report it.

    Same consequence as an unreadable file, one record at a time. The row is
    skipped (matching how the live reader treats the shape) but the count is
    surfaced, so a run cannot print a clean orphan number while having
    quietly failed to guard N of them.
    """
    _mk_agent(tmp_path, monkeypatch, live="",
              archive=_archive_row() + "{not valid json\n")
    r = reconcile.reconcile_agent("testagent", apply=False)
    assert r["before"]["archive_unparsed"] == 1
    # The intact row still guards its stem -- one bad line must not void the set.
    assert r["before"]["orphan_md"] == 0


def test_archive_rows_do_not_drive_md_regeneration(tmp_path, monkeypatch):
    """Archive rows subtract from orphans; they must not become missing_md.

    An archived row pointing at a nonexistent .md is out of scope for this
    script's stub-regeneration branch. Merging the archive into `jsonl_ids`
    -- the obvious shortcut implementation -- would start writing stubs for
    every archived trace whose file is gone, which nobody asked for.
    """
    ghost = json.dumps({
        "id": "exp-g-999-99",
        "type": "goal_execution",
        "category": "framework-hygiene",
        "summary": "an archived row whose .md no longer exists",
        "content_path": "agents/testagent/experience/exp-g-999-99.md",
        "archived": True,
    }) + "\n"
    _mk_agent(tmp_path, monkeypatch, live="", archive=_archive_row() + ghost)
    r = reconcile.reconcile_agent("testagent", apply=False)
    assert r["before"]["missing_md"] == 0
    assert r["before"]["stem_mismatch"] == 0
    assert r["actions"]["md_files_backfilled"] == 0
