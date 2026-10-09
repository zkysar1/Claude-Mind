"""Tests for core/scripts/body-claims-release.py (worker /stop release step).

The four I/O legs are injected, so every verdict branch runs without a daemon.
The invariant that matters most is the error branch: a claim query that could
not run must never read as "nothing held", because that is the answer that
ends the stop's release check with claims still locked.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "body_claims_release_ut", SCRIPTS / "body-claims-release.py")
bcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcr)

AGENT = "agent-a"
SID = "0123456789abcdef0123456789abcdef"


class Fakes:
    """Scripted I/O: `claims` is consumed one list per query call."""

    def __init__(self, claims, row_after=None, query_error_on=None):
        self.claims = list(claims)
        self.row_after = row_after
        self.query_error_on = query_error_on
        self.query_calls = 0
        self.released = []
        self.cleared = 0

    def query(self, agent, sid):
        self.query_calls += 1
        if self.query_error_on == self.query_calls:
            raise bcr.QueryError("daemon unreachable")
        return self.claims.pop(0) if self.claims else []

    def release(self, agent, sid, goal_id, source):
        self.released.append((goal_id, source))
        return {"goal_id": goal_id, "source": source, "rc": 0}

    def clear(self, agent, sid):
        self.cleared += 1
        return {"verdict": "absent", "body_row": "cleared"}

    def read_row(self, agent, sid):
        return self.row_after

    def run(self, **kw):
        return bcr.release_all(AGENT, SID, query=self.query, release=self.release,
                               clear=self.clear, read_row=self.read_row, **kw)


def _goal(gid, source="world", status="pending"):
    return {"goal_id": gid, "source": source, "status": status, "title": "t"}


def test_nothing_held_still_clears_rows():
    # The cc-15 case: the goal is already terminal, so there is no claim to
    # release, but the body row still reads busy. The clear must still run.
    f = Fakes([[], []])
    out = f.run()
    assert out["verdict"] == "nothing-held"
    assert f.released == []
    assert f.cleared == 1
    assert bcr.EXIT[out["verdict"]] == 0


def test_releases_each_claim_on_its_own_queue():
    f = Fakes([[_goal("g-1-01"), _goal("g-2-02", source="agent", status="in-progress")], []])
    out = f.run()
    assert out["verdict"] == "released"
    assert f.released == [("g-1-01", "world"), ("g-2-02", "agent")]
    assert bcr.EXIT[out["verdict"]] == 0


def test_claim_surviving_release_is_residue():
    f = Fakes([[_goal("g-1-01")], [_goal("g-1-01")]])
    out = f.run()
    assert out["verdict"] == "residue"
    assert out["still_claimed"] == ["g-1-01"]
    assert bcr.EXIT[out["verdict"]] == 1


def test_body_row_surviving_clear_is_residue():
    row = {"goal_id": "g-9-09", "phase": "4"}
    f = Fakes([[], []], row_after=row)
    out = f.run()
    assert out["verdict"] == "residue"
    assert out["body_row"] == row


def test_unreadable_query_is_error_never_nothing_held():
    f = Fakes([[]], query_error_on=1)
    out = f.run()
    assert out["verdict"] == "error"
    assert out["stage"] == "query"
    assert f.released == [] and f.cleared == 0
    assert bcr.EXIT[out["verdict"]] == 2


def test_unreadable_read_back_is_error_not_released():
    f = Fakes([[_goal("g-1-01")]], query_error_on=2)
    out = f.run()
    assert out["verdict"] == "error"
    assert out["stage"] == "read-back"
    assert f.released == [("g-1-01", "world")]


def test_dry_run_changes_nothing():
    f = Fakes([[_goal("g-1-01")]])
    out = f.run(dry_run=True)
    assert out["verdict"] == "dry-run"
    assert out["would_release"][0]["goal_id"] == "g-1-01"
    assert f.released == [] and f.cleared == 0


def test_empty_sid_refused_before_any_io(capsys):
    assert bcr.main(["--agent", AGENT, "--sid", "  "]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "error"


class _Proc:
    def __init__(self, stdout, rc=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


def test_query_normalizes_rows(monkeypatch):
    rows = [{"id": "g-1-01", "source": "world", "status": "pending", "title": "a"},
            {"goal_id": "g-2-02", "source": "agent", "status": "blocked"},
            "not-a-row"]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(json.dumps(rows)))
    held = bcr.query_claims(AGENT, SID)
    assert [(h["goal_id"], h["source"]) for h in held] == [("g-1-01", "world"),
                                                          ("g-2-02", "agent")]


@pytest.mark.parametrize("proc", [
    _Proc("", rc=1, stderr="no daemon"),
    _Proc("not json"),
    _Proc(json.dumps({"error": "filter required"})),
])
def test_query_failure_shapes_raise(monkeypatch, proc):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(bcr.QueryError):
        bcr.query_claims(AGENT, SID)


def test_body_row_read_null_is_absent_and_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc("null"))
    assert bcr.read_body_row(AGENT, SID) is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc("", rc=3, stderr="x"))
    with pytest.raises(bcr.QueryError):
        bcr.read_body_row(AGENT, SID)
