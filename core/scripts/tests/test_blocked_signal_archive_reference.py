#!/usr/bin/env python3
""" (1/2) — blocked-signal-resolution-check must resolve a blocker_ref
against the ARCHIVE, not just the live queues.

`_read_goals` reads active=True, so it sees only aspirations-*.jsonl. When an
aspiration COMPLETES it moves to aspirations-archive.jsonl and its goals leave
that view. A blocker_ref naming such a goal then fell through `_classify_ref` to
the `rid.startswith("g-")` branch and was reported `dangling` — which the sweep's
own docstring calls "a real defect to surface".

That verdict is INVERTED, not merely missing: the STRONGEST possible resolution
(referent completed AND its whole initiative archived, guard-1555) was reported
as a broken reference, so a reader acting on it goes and repairs a reference that
is perfectly satisfied. Strictly worse than the silent skip its sibling
defer-recheck.py had (g-115-3916), because a wrong verdict gets acted on.

Sibling audit of g-115-3916; test shape deliberately mirrors
test_defer_recheck_archive_dependency.py rather than inventing a new one.

WHY EACH CASE EXISTS (do not delete one as redundant):
  1 — the defect itself: archived-completed referent resolves terminal, NOT dangling.
  2 — the preserved-correct case: an id in NEITHER store is STILL dangling. Without
      this, "fix" could mean "never report dangling again", which would destroy the
      sweep's actual purpose rather than fix it.
  3 — live-wins precedence: an id in BOTH stores must resolve from the LIVE record.
      Without this pin, a stale archive snapshot can shadow a re-opened goal.
  4 — a genuinely unreachable archive degrades LOUDLY (archive_degraded true). A
      silent degradation reinstates the original invisibility, which IS the defect.
  5 — a merely EMPTY archive does NOT flag degradation. Stubbing this as
      json.dumps([]) instead of "" makes the case VACUOUS (the decoder returns []
      for "[]" but None for ""), the exact trap the sibling test recorded.

Run: py -3 -m pytest core/scripts/tests/test_blocked_signal_archive_reference.py -v
"""
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load():
    spec = importlib.util.spec_from_file_location(
        "blocked_signal_archive_module",
        SCRIPT_DIR / "blocked-signal-resolution-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _envelope(asps):
    return json.dumps({"aspirations": asps})


class _Stub:
    """Stubs _rt: live vs archive selected by the `archive` kwarg.

    Mirrors the REAL `aspirations_read` signature rather than a convenience
    shape, so a future signature change breaks this test instead of silently
    exercising a branch production never takes
    (probe-with-canonical-code-path.md "canonical BINARY is not canonical
    INVOCATION").
    """

    live = {}
    archive = {}
    archive_raises = ()
    # "" is a DIFFERENT state from "[]": tolerant_decode_aggregate returns None
    # for "" and [] for "[]", and they reach different branches. Case 5 needs the
    # None path; stubbing it as json.dumps([]) would make that case vacuous.
    archive_empty_body = ()

    class RtError(Exception):
        def __init__(self, body=""):
            self.body = body
            super().__init__(body)

    @classmethod
    def aspirations_read(cls, source="world", active=False,
                         active_compact=False, asp_id=None, limit=None,
                         archive=False):
        if archive:
            if source in cls.archive_raises:
                raise cls.RtError("simulated archive read failure")
            if source in cls.archive_empty_body:
                return ""
            # ?archive=1 returns a BARE list — production shape, not the envelope.
            return json.dumps(cls.archive.get(source, []))
        return _envelope(cls.live.get(source, []))

    @staticmethod
    def tolerant_decode_aggregate(source, raw):
        import importlib
        return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)

    @staticmethod
    def tolerant_decode_list(source, raw):
        import importlib
        return importlib.import_module("_rt").tolerant_decode_list(source, raw)


def run_main(live, archive, archive_raises=(), archive_empty_body=()):
    """Drive main() and return (parsed_json, stderr)."""
    stub = type("_S", (_Stub,), {"live": live, "archive": archive,
                                 "archive_raises": archive_raises,
                                 "archive_empty_body": archive_empty_body})
    orig_rt, orig_pq, orig_argv = M._rt, M._load_pq_index, sys.argv
    M._rt = stub
    # The pq lane reads the store of record; stub it so this test exercises the
    # GOAL-ID lane only and performs no network/filesystem IO. pq_complete stays
    # true so nothing is withheld for corpus-incompleteness reasons.
    M._load_pq_index = lambda: ({}, [])
    sys.argv = ["blocked-signal-resolution-check.py", "--output", "json"]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            M.main()
    finally:
        M._rt, M._load_pq_index, sys.argv = orig_rt, orig_pq, orig_argv
    return json.loads(out.getvalue()), err.getvalue()


def _blocked_goal(gid, ref_id, days=9):
    """A blocked goal whose ONLY block signal is blocker_ref -> ref_id."""
    import datetime as dt
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(
        timespec="seconds")
    return {
        "id": gid,
        "title": f"blocked goal {gid}",
        "status": "blocked",
        "participants": ["agent"],
        "intended_agent": "either",
        "blocked_since": since,
        "blocker_ref": ref_id,
    }


def _entry(res, goal_id):
    for bucket in ("all_resolved", "disagreement", "dangling_ref", "undecidable"):
        for e in res.get(bucket) or []:
            if e["goal_id"] == goal_id:
                return bucket, e
    return None, None


# ── Case 1 — THE DEFECT ────────────────────────────────────────────────────
def test_archived_completed_referent_is_terminal_not_dangling():
    live = {"world": [{"id": "asp-900", "goals": [_blocked_goal("g-900-01",
                                                                "g-800-07")]}],
            "agent": []}
    archive = {"world": [{"id": "asp-800",
                          "goals": [{"id": "g-800-07", "title": "archived dep",
                                     "status": "completed"}]}],
               "agent": []}
    res, _ = run_main(live, archive)

    bucket, e = _entry(res, "g-900-01")
    assert e is not None, "the blocked goal was not classified at all"
    assert bucket != "dangling_ref", (
        "REGRESSION: an archived-COMPLETED referent is reported as a broken "
        f"reference. bucket={bucket} why={e.get('blocker_ref_why')!r}")
    assert e["resolution_basis"] == "referent_terminal", (
        f"expected basis referent_terminal, got {e['resolution_basis']!r} "
        f"(why={e.get('blocker_ref_why')!r})")
    # Origin must remain visible — "found in archive" is a STRONGER fact than
    # "found live" and must not be flattened into it (guard-1555).
    assert "ARCHIVED" in (e.get("blocker_ref_why") or ""), (
        "the archive origin is not reported, so 'found in archive' is "
        f"indistinguishable from 'found live': {e.get('blocker_ref_why')!r}")


# ── Case 2 — the preserved-correct case ────────────────────────────────────
def test_referent_in_neither_store_is_still_dangling():
    """The fix must not mean 'never report dangling again'."""
    live = {"world": [{"id": "asp-900", "goals": [_blocked_goal("g-900-02",
                                                                "g-777-77")]}],
            "agent": []}
    res, _ = run_main(live, {"world": [], "agent": []})
    bucket, e = _entry(res, "g-900-02")
    assert e is not None
    assert bucket == "dangling_ref", (
        "a referent present in NEITHER store must still be dangling; "
        f"got bucket={bucket} basis={e.get('resolution_basis')!r}")


# ── Case 3 — live-wins precedence ──────────────────────────────────────────
def test_live_record_wins_over_stale_archive_snapshot():
    """An id in BOTH stores must resolve from the LIVE record.

    The archive copy is a stale snapshot (re-opened goal, or a mid-archive
    race). If it shadowed the live record, a re-opened blocker would read as
    completed and the dependent would be wrongly reported resolved.
    """
    live = {"world": [{"id": "asp-900",
                       "goals": [_blocked_goal("g-900-03", "g-800-09"),
                                 # SAME id as the archive entry, but re-opened.
                                 {"id": "g-800-09", "title": "re-opened dep",
                                  "status": "in-progress"}]}],
            "agent": []}
    archive = {"world": [{"id": "asp-800",
                          "goals": [{"id": "g-800-09",
                                     "title": "stale archived snapshot",
                                     "status": "completed"}]}],
               "agent": []}
    res, _ = run_main(live, archive)
    bucket, e = _entry(res, "g-900-03")
    # The live record is in-progress, so the ref is NOT resolved -> the goal is
    # still_blocked and appears in no bucket at all.
    assert bucket is None, (
        "REGRESSION: a stale archive snapshot shadowed the LIVE record, so a "
        f"re-opened blocker read as resolved. bucket={bucket} "
        f"basis={(e or {}).get('resolution_basis')!r}")


# ── Case 4 — loud degradation ──────────────────────────────────────────────
def test_unreachable_archive_degrades_loudly():
    live = {"world": [{"id": "asp-900", "goals": [_blocked_goal("g-900-04",
                                                                "g-800-11")]}],
            "agent": []}
    res, err = run_main(live, {"world": [], "agent": []},
                        archive_raises=("world",))
    assert res["archive_degraded"] is True, (
        "an unreachable archive degraded SILENTLY — that reinstates the "
        "original invisibility, which is the defect itself")
    assert "world" in res["archive_read_failed"]
    assert "archive read failed" in err, (
        "no diagnostic on stderr for a failed archive read")


# ── Case 5 — empty archive is NOT degradation ──────────────────────────────
def test_empty_archive_body_is_not_a_failure():
    """A source with nothing archived yet must not be flagged degraded.

    Reaches the decoder's None path via an EMPTY BODY (""), not "[]". Stubbing
    this as json.dumps([]) exercises a different branch and makes the case
    vacuous.
    """
    live = {"world": [{"id": "asp-900", "goals": [_blocked_goal("g-900-05",
                                                                "g-777-78")]}],
            "agent": []}
    res, _ = run_main(live, {"world": [], "agent": []},
                      archive_empty_body=("world", "agent"))
    assert res["archive_degraded"] is False, (
        "an EMPTY archive was reported as a read FAILURE, so the sweep disowns "
        "its own correct verdicts on any world with nothing archived yet")
    assert res["archive_read_failed"] == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
