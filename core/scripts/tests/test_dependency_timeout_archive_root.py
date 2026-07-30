#!/usr/bin/env python3
""" (2/2) — dependency-timeout-check must resolve a blocked_by root
against the ARCHIVE, not just the live queues.

`_read_goal_index` read active=True only. A blocked_by root that COMPLETED inside
a since-archived aspiration was therefore absent from the index, so `_root_of`
hit `root = index.get(rid); if root is None: return rid, None`, the root-walk
STOPPED, and the chain was reported unresolved when it was in fact satisfied —
the blocked_by-edge instance of the class g-115-3916 fixed in defer-recheck.py
and g-115-3933 fixed in blocked-signal-resolution-check.py.

Same family as guard-1715: the index's all-clear was bounded by the population IT
declared (live goals), not the population its readers assumed (all goals).

Tests target `_read_goal_index` and `_root_of` DIRECTLY rather than driving
main(), because main() shells out to goal-selector.sh via subprocess — a unit
test that spawned it would be slow, would depend on live queue state, and would
not localize a failure to the changed code.

WHY EACH CASE EXISTS (do not delete one as redundant):
  1 — the defect: an archive-only root lands in the index, marked `_archived`.
  2 — the consequence: `_root_of` walks PAST an archived-completed root instead of
      halting on it. This is the behavior the sweep actually consumes.
  3 — live-wins precedence: an id in BOTH stores resolves from the LIVE record, so
      a stale archive snapshot cannot make a re-opened root look completed.
  4 — an unreachable archive is FAIL-OPEN but LOUD (stderr diagnostic). Silent
      degradation reinstates the original invisibility.
  5 — an EMPTY archive body is a valid state, not a failure. Reached via "" (the
      decoder's None path), not "[]" — stubbing it as a list makes this vacuous.

Run: py -3 -m pytest core/scripts/tests/test_dependency_timeout_archive_root.py -v
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
        "dependency_timeout_archive_module",
        SCRIPT_DIR / "dependency-timeout-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _envelope(asps):
    return json.dumps({"aspirations": asps})


class _Stub:
    """Stubs _rt: live vs archive selected by the `archive` kwarg.

    Mirrors the REAL `aspirations_read` signature so a future signature change
    breaks this test instead of silently exercising a branch production never
    takes (probe-with-canonical-code-path.md).
    """

    live = {}
    archive = {}
    archive_raises = ()
    # "" and "[]" are DIFFERENT states: tolerant_decode_aggregate returns None for
    # "" and [] for "[]". Case 5 needs the None path.
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
            # ?archive=1 returns a BARE list — production shape.
            return json.dumps(cls.archive.get(source, []))
        return _envelope(cls.live.get(source, []))

    @staticmethod
    def tolerant_decode_aggregate(source, raw):
        import importlib
        return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)


def build_index(live, archive, archive_raises=(), archive_empty_body=()):
    """Call _read_goal_index() under stubbed _rt; return (index, stderr)."""
    stub = type("_S", (_Stub,), {"live": live, "archive": archive,
                                 "archive_raises": archive_raises,
                                 "archive_empty_body": archive_empty_body})
    orig = M._rt
    M._rt = stub
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            idx = M._read_goal_index()
    finally:
        M._rt = orig
    return idx, err.getvalue()


# ── Case 1 — THE DEFECT ────────────────────────────────────────────────────
def test_archive_only_root_is_indexed():
    live = {"world": [{"id": "asp-900", "goals": [
        {"id": "g-900-01", "status": "blocked", "blocked_by": ["g-800-07"]}]}],
        "agent": []}
    archive = {"world": [{"id": "asp-800", "goals": [
        {"id": "g-800-07", "status": "completed", "title": "archived root"}]}],
        "agent": []}
    idx, _ = build_index(live, archive)
    assert "g-800-07" in idx, (
        "REGRESSION: a root that completed inside an ARCHIVED aspiration is "
        "absent from the index, so _root_of halts and reports the chain "
        "unresolved when it is satisfied")
    assert idx["g-800-07"].get("_archived") is True, (
        "the archive origin is not marked, so 'resolved via archive' is "
        "indistinguishable from 'resolved live'")
    assert idx["g-800-07"].get("_source") == "world"


# ── Case 2 — the consumed behavior ─────────────────────────────────────────
def test_root_of_walks_past_archived_completed_root():
    """_root_of must not halt on an archived-completed root.

    This is the behavior the sweep consumes: pre-fix, index.get() returned None
    and _root_of returned (rid, None), reporting the chain unresolved.
    """
    live = {"world": [{"id": "asp-900", "goals": [
        {"id": "g-900-02", "status": "blocked",
         "blocked_by": ["g-800-08", "g-900-99"]},
        {"id": "g-900-99", "status": "pending", "title": "live open root"}]}],
        "agent": []}
    archive = {"world": [{"id": "asp-800", "goals": [
        {"id": "g-800-08", "status": "completed"}]}],
        "agent": []}
    idx, _ = build_index(live, archive)
    goal = idx["g-900-02"]
    rid, root = M._root_of(goal, idx)
    #  is completed (in the archive) so the walk must SKIP it and land on
    # the genuinely-open .
    assert rid == "g-900-99", (
        f"_root_of halted on the archived-completed root instead of walking "
        f"past it: got rid={rid!r} root={(root or {}).get('status')!r}")
    assert root is not None and root.get("status") == "pending"


# ── Case 3 — live-wins precedence ──────────────────────────────────────────
def test_live_record_wins_over_stale_archive_snapshot():
    """An id in BOTH stores must resolve from the LIVE record.

    Without this, a stale archive snapshot makes a RE-OPENED root look
    completed, and _root_of walks past a blocker that is genuinely still open.
    """
    live = {"world": [{"id": "asp-900", "goals": [
        {"id": "g-900-03", "status": "blocked", "blocked_by": ["g-800-09"]},
        {"id": "g-800-09", "status": "in-progress", "title": "re-opened"}]}],
        "agent": []}
    archive = {"world": [{"id": "asp-800", "goals": [
        {"id": "g-800-09", "status": "completed", "title": "stale snapshot"}]}],
        "agent": []}
    idx, _ = build_index(live, archive)
    assert idx["g-800-09"].get("status") == "in-progress", (
        "REGRESSION: the stale ARCHIVE snapshot shadowed the LIVE record")
    assert idx["g-800-09"].get("_archived") is not True, (
        "the live record was overwritten by the archive copy")
    rid, root = M._root_of(idx["g-900-03"], idx)
    assert rid == "g-800-09" and root is not None, (
        "a re-opened root must still be reported as the blocking root")


# ── Case 4 — loud fail-open ────────────────────────────────────────────────
def test_unreachable_archive_is_fail_open_but_loud():
    live = {"world": [{"id": "asp-900", "goals": [
        {"id": "g-900-04", "status": "blocked", "blocked_by": ["g-800-11"]}]}],
        "agent": []}
    idx, err = build_index(live, {"world": [], "agent": []},
                           archive_raises=("world",))
    # FAIL-OPEN: the live half still indexed.
    assert "g-900-04" in idx, "a failed archive read must not empty the index"
    # LOUD: silent degradation reinstates the original invisibility.
    assert "archive read failed" in err, (
        "an unreachable archive degraded SILENTLY — no stderr diagnostic")


# ── Case 5 — empty archive is NOT a failure ────────────────────────────────
def test_empty_archive_body_is_not_a_failure():
    """Reached via an EMPTY BODY (""), the decoder's None path — not "[]"."""
    live = {"world": [{"id": "asp-900", "goals": [
        {"id": "g-900-05", "status": "blocked", "blocked_by": ["g-777-77"]}]}],
        "agent": []}
    idx, err = build_index(live, {"world": [], "agent": []},
                           archive_empty_body=("world", "agent"))
    assert "g-900-05" in idx
    assert "archive read failed" not in err, (
        "an EMPTY archive was reported as a read FAILURE, so the sweep would "
        "disown its own correct verdicts on any world with nothing archived yet")
    # An id in NEITHER store still resolves to None — preserved-correct case.
    rid, root = M._root_of(idx["g-900-05"], idx)
    assert rid == "g-777-77" and root is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
