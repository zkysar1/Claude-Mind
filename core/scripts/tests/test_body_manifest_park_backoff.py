"""Park-orbit backoff ( part 4): a parked worker Body must not wake
hourly into a full ~25-iteration liveness cycle forever. `park` advances
`park_count` + `park_next_poll_at` on every consecutive park; `park-due` tells
Phase -0 whether the FULL re-poll is due or a cheap re-arm suffices.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
PY = sys.executable
SID = "worker-sid-0001"
AGENT = "alpha"


def _load():
    spec = importlib.util.spec_from_file_location("body_manifest", SCRIPTS / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load()


def _project(tmp_path: Path) -> Path:
    adir = tmp_path / "agents" / AGENT
    (adir / "session").mkdir(parents=True)
    (adir / "session" / "running-session-id").write_text("reducer-sid-9\n", encoding="utf-8")
    (adir / "session" / "working-memory.yaml").write_bytes(b"slot: x\n")
    return tmp_path


def _parked(tmp_path: Path) -> Path:
    pr = _project(tmp_path)
    bm.write_manifest(SID, AGENT, project_root=pr, role="worker")
    assert bm.park_body(SID, AGENT, project_root=pr) == "parked"
    return pr


def test_backoff_schedule_doubles_and_caps(monkeypatch):
    monkeypatch.delenv("PARK_BACKOFF_BASE_SECONDS", raising=False)
    monkeypatch.delenv("PARK_BACKOFF_MAX_SECONDS", raising=False)
    assert bm.park_backoff_seconds(0) == 3600
    assert bm.park_backoff_seconds(1) == 3600
    assert bm.park_backoff_seconds(2) == 7200
    assert bm.park_backoff_seconds(3) == 14400
    assert bm.park_backoff_seconds(9) == 14400  # capped at 4h
    monkeypatch.setenv("PARK_BACKOFF_BASE_SECONDS", "10")
    monkeypatch.setenv("PARK_BACKOFF_MAX_SECONDS", "25")
    assert [bm.park_backoff_seconds(n) for n in (1, 2, 3, 4)] == [10, 20, 25, 25]


def test_first_park_starts_the_orbit_and_is_not_due(tmp_path):
    pr = _parked(tmp_path)
    d = bm.read_manifest(SID, AGENT, project_root=pr)
    assert d["body_state"] == "parked" and int(d["park_count"]) == 1
    due, remaining = bm.park_due(SID, AGENT, project_root=pr)
    assert due is False and 3500 < remaining <= 3600


def test_consecutive_parks_advance_the_orbit_and_keep_parked_at(tmp_path):
    pr = _parked(tmp_path)
    first = bm.read_manifest(SID, AGENT, project_root=pr)
    assert bm.park_body(SID, AGENT, project_root=pr) == "already-parked"
    assert bm.park_body(SID, AGENT, project_root=pr) == "already-parked"
    d = bm.read_manifest(SID, AGENT, project_root=pr)
    assert int(d["park_count"]) == 3
    assert d["parked_at"] == first["parked_at"], "the 60h cap still measures the WHOLE park"
    _, remaining = bm.park_due(SID, AGENT, project_root=pr)
    assert 14000 < remaining <= 14400


def test_resume_clears_the_orbit_so_a_re_park_restarts_at_base(tmp_path):
    pr = _parked(tmp_path)
    bm.park_body(SID, AGENT, project_root=pr)
    assert bm.resume_body(SID, AGENT, project_root=pr) == "resumed"
    d = bm.read_manifest(SID, AGENT, project_root=pr)
    assert "park_count" not in d and "park_next_poll_at" not in d and "parked_at" not in d
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)
    assert bm.park_body(SID, AGENT, project_root=pr) == "parked"
    assert int(bm.read_manifest(SID, AGENT, project_root=pr)["park_count"]) == 1


def test_park_due_fails_toward_polling(tmp_path):
    pr = _parked(tmp_path)
    path = bm._agent_paths(AGENT, SID, pr)[1] / "body-manifest.yaml"
    text = path.read_text(encoding="utf-8")
    broken = "\n".join(
        ("park_next_poll_at: 'not-a-timestamp'" if line.startswith("park_next_poll_at:") else line)
        for line in text.splitlines()) + "\n"
    path.write_text(broken, encoding="utf-8")
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)
    past = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(broken.replace("'not-a-timestamp'", f"'{past}'"), encoding="utf-8")
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)


def test_park_due_cli_contract(monkeypatch, capsys):
    """rc is the verdict; the LAST stdout line is the integer seconds remaining."""
    monkeypatch.setattr(bm, "park_due", lambda sid, agent: (False, 1234))
    assert bm.main(["park-due", "--sid", SID, "--agent", AGENT]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "not-due" and out[-1] == "1234"
    monkeypatch.setattr(bm, "park_due", lambda sid, agent: (True, 0))
    assert bm.main(["park-due", "--sid", SID, "--agent", AGENT]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "due" and out[-1] == "0"
