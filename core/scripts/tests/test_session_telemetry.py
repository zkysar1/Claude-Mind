"""Unit tests for _session_telemetry.py (session telemetry Phase 1).

All tests inject world_dir=tmp_path so they never touch the real WORLD_DIR.
No daemon dependency; pure filesystem + tmp_path.
"""
import os
import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/scripts
import _session_telemetry as st  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_machine_id():
    """Reset the module-level machine_id cache between tests."""
    st._MACHINE_ID = None
    yield
    st._MACHINE_ID = None


def _read(tmp_path, agent, sid):
    p = tmp_path / "telemetry" / "session-records" / agent / (sid + ".json")
    return json.loads(p.read_text(encoding="utf-8"))


def test_record_path_structure(tmp_path):
    p = st._record_path("alpha", "sid-1", world_dir=tmp_path)
    assert p == tmp_path / "telemetry" / "session-records" / "alpha" / "sid-1.json"


def test_records_subdir_is_not_sessions(tmp_path):
    # owncloud_sync._EXCLUDE_DIRS walk-prunes "sessions"; the telemetry subdir
    # MUST avoid that basename or records never reach S3. (rb 2026-06-03)
    p = st._record_path("alpha", "sid-1", world_dir=tmp_path)
    assert "sessions" not in p.parts
    assert st._RECORDS_SUBDIR != "sessions"


def test_write_open_creates_record(tmp_path):
    out = st.write_open("sid-1", "alpha", "autonomous", "claude-code", world_dir=tmp_path)
    assert out is not None
    r = _read(tmp_path, "alpha", "sid-1")
    assert r["status"] == "active"
    assert r["session_id"] == "sid-1"
    assert r["agent"] == "alpha"
    assert r["mode"] == "autonomous"
    assert r["started_by"] == "claude-code"
    assert r["ended_at"] is None
    assert r["duration_seconds"] is None
    assert r["iterations_completed"] == 0
    assert r["goals_completed"] == 0
    assert r["goals_filed"] == 0
    assert r["tree_writes"] == 0
    assert r["started_at"] and isinstance(r["started_at"], str)


def test_write_open_idempotent_skip(tmp_path):
    st.write_open("sid-1", "alpha", "autonomous", "claude-code", world_dir=tmp_path)
    before = _read(tmp_path, "alpha", "sid-1")
    out2 = st.write_open("sid-1", "alpha", "reader", "someone-else", world_dir=tmp_path)
    assert out2 is None  # second open does not write
    after = _read(tmp_path, "alpha", "sid-1")
    assert after == before  # unchanged — not clobbered


def test_write_close_with_existing_open(tmp_path):
    st.write_open("sid-1", "alpha", "autonomous", "claude-code", world_dir=tmp_path)
    out = st.write_close("sid-1", "alpha", status="completed", ended_reason="graceful-stop",
                         mode_at_end="assistant", iterations_completed=4, goals_completed=3,
                         goals_filed=2, tree_writes=5, world_dir=tmp_path)
    assert out is not None
    r = _read(tmp_path, "alpha", "sid-1")
    assert r["status"] == "completed"
    assert r["ended_reason"] == "graceful-stop"
    assert r["mode_at_end"] == "assistant"
    assert r["ended_at"] and isinstance(r["ended_at"], str)
    assert r["end_machine_id"] is not None
    assert r["iterations_completed"] == 4
    assert r["goals_completed"] == 3
    assert r["goals_filed"] == 2
    assert r["tree_writes"] == 5
    assert "wp1_missing" not in r  # open existed; not a synthesized record


def test_close_preserves_open_machine_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "machine-A")
    st.write_open("sid-1", "alpha", "autonomous", "claude-code", world_dir=tmp_path)
    st._MACHINE_ID = None  # simulate a different process for the close
    monkeypatch.setenv("MACHINE_ID", "machine-B")
    st.write_close("sid-1", "alpha", status="completed", ended_reason="graceful-stop",
                   world_dir=tmp_path)
    r = _read(tmp_path, "alpha", "sid-1")
    assert r["machine_id"] == "machine-A"      # preserved from open
    assert r["end_machine_id"] == "machine-B"  # close machine recorded separately


def test_write_close_synthesizes_missing_open(tmp_path):
    # binding.yaml exists but no open record was ever written
    agent, sid = "bravo", "sid-x"
    binding_dir = tmp_path / "agents" / agent / "sessions" / sid
    binding_dir.mkdir(parents=True)
    (binding_dir / "binding.yaml").write_text(
        "session_id: sid-x\nagent: bravo\nmode: assistant\n"
        "started_at: '2026-06-03T05:00:00'\nstarted_by: claude-code\n",
        encoding="utf-8")
    out = st.write_close(sid, agent, status="completed", ended_reason="user-stop",
                         world_dir=tmp_path, project_root=tmp_path)
    assert out is not None
    r = _read(tmp_path, agent, sid)
    assert r["wp1_missing"] is True
    assert r["status"] == "completed"
    assert r["started_at"] == "2026-06-03T05:00:00"  # recovered from binding
    assert r["mode"] == "assistant"


def test_write_close_no_open_no_binding(tmp_path):
    out = st.write_close("sid-none", "charlie", status="completed", ended_reason="user-stop",
                         world_dir=tmp_path, project_root=tmp_path)
    assert out is not None
    r = _read(tmp_path, "charlie", "sid-none")
    assert r["wp1_missing"] is True
    assert r["started_at"] == "unknown"
    assert r["duration_seconds"] == -1  # uncomputable


def test_write_crash_convenience(tmp_path):
    st.write_open("sid-c", "delta", "autonomous", "claude-code", world_dir=tmp_path)
    out = st.write_crash("sid-c", "delta", iterations_completed=7, world_dir=tmp_path)
    assert out is not None
    r = _read(tmp_path, "delta", "sid-c")
    assert r["status"] == "crashed"
    assert r["ended_reason"] == "recovery-gate"
    assert r["goals_completed"] == -1
    assert isinstance(r["goals_completed"], int)
    assert r["iterations_completed"] == 7


def test_duration_seconds_computed():
    assert st._duration_seconds("2026-06-03T05:00:00", "2026-06-03T05:00:10") == 10
    assert st._duration_seconds("2026-06-03T05:00:00", "2026-06-03T06:00:00") == 3600
    assert st._duration_seconds("unknown", "2026-06-03T05:00:10") == -1
    assert st._duration_seconds(None, "2026-06-03T05:00:10") == -1


def test_schema_version_literal(tmp_path):
    st.write_open("sid-1", "alpha", "reader", "claude-code", world_dir=tmp_path)
    r = _read(tmp_path, "alpha", "sid-1")
    assert isinstance(r["schema_version"], int)
    assert r["schema_version"] == 1


def test_machine_id_env_override(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "my-stable-id")
    st._MACHINE_ID = None
    assert st._machine_id() == "my-stable-id"


def test_machine_id_hostname_fallback(monkeypatch):
    monkeypatch.delenv("MACHINE_ID", raising=False)
    st._MACHINE_ID = None
    import socket
    assert st._machine_id() == socket.gethostname()


def test_machine_id_unknown_falls_back(monkeypatch):
    monkeypatch.setenv("MACHINE_ID", "unknown")
    st._MACHINE_ID = None
    import socket
    assert st._machine_id() == socket.gethostname()


def test_reader_session_zero_counters(tmp_path):
    st.write_open("sid-r", "echo", "reader", "claude-code", world_dir=tmp_path)
    st.write_close("sid-r", "echo", status="completed", ended_reason="user-stop",
                   world_dir=tmp_path)
    r = _read(tmp_path, "echo", "sid-r")
    assert r["iterations_completed"] == 0
    assert r["goals_completed"] == 0
    assert r["goals_filed"] == 0
    assert r["tree_writes"] == 0


def test_env_id_default_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT_ID", raising=False)
    st.write_open("sid-1", "alpha", "reader", "claude-code", world_dir=tmp_path)
    assert _read(tmp_path, "alpha", "sid-1")["env_id"] == "ayoai-mind"
    st.write_open("sid-2", "alpha", "reader", "claude-code", world_dir=tmp_path, env_id="other-env")
    assert _read(tmp_path, "alpha", "sid-2")["env_id"] == "other-env"


def test_write_failure_isolated(monkeypatch, tmp_path):
    # _resolve_world_dir returns None -> public functions return None, never raise
    monkeypatch.setattr(st, "_resolve_world_dir", lambda world_dir=None: None)
    assert st.write_open("sid-1", "alpha", "reader", "claude-code") is None
    assert st.write_close("sid-1", "alpha", "completed", "user-stop") is None
    assert st.write_crash("sid-1", "alpha") is None


def test_empty_sid_or_agent_returns_none(tmp_path):
    assert st.write_open("", "alpha", "reader", "x", world_dir=tmp_path) is None
    assert st.write_open("sid-1", "", "reader", "x", world_dir=tmp_path) is None
    assert st.write_close("", "alpha", "completed", "user-stop", world_dir=tmp_path) is None


def test_all_status_and_reason_values(tmp_path):
    cases = [
        ("completed", "user-stop"), ("completed", "graceful-stop"),
        ("completed", "session-replaced"), ("crashed", "recovery-gate"),
    ]
    for i, (status, reason) in enumerate(cases):
        sid = "sid-%d" % i
        st.write_close(sid, "alpha", status=status, ended_reason=reason, world_dir=tmp_path)
        r = _read(tmp_path, "alpha", sid)
        assert r["status"] == status
        assert r["ended_reason"] == reason


def test_atomic_dump_no_history_or_changelog(tmp_path):
    # The write must not create .history/ or changelog artifacts (no _fileops).
    st.write_open("sid-1", "alpha", "reader", "claude-code", world_dir=tmp_path)
    assert not (tmp_path / ".history").exists()
    assert not (tmp_path / "changelog.jsonl").exists()
    # No leftover tmp file
    d = tmp_path / "telemetry" / "session-records" / "alpha"
    assert not any(p.name.endswith(".tmp") for p in d.iterdir())


def test_double_close_is_idempotent(tmp_path):
    # First close wins; a second close on an already-finalized record is a no-op
    # (the record is immutable once closed). (adversarial review 2026-06-03)
    st.write_open("sid-d", "alpha", "autonomous", "claude-code", world_dir=tmp_path)
    st.write_close("sid-d", "alpha", status="completed", ended_reason="graceful-stop",
                   iterations_completed=4, world_dir=tmp_path)
    first = _read(tmp_path, "alpha", "sid-d")
    # Second close with DIFFERENT reason/counters must NOT clobber the first.
    st.write_close("sid-d", "alpha", status="completed", ended_reason="user-stop",
                   iterations_completed=99, world_dir=tmp_path)
    second = _read(tmp_path, "alpha", "sid-d")
    assert second == first
    assert second["ended_reason"] == "graceful-stop"
    assert second["iterations_completed"] == 4
    # A crashed record is likewise immutable.
    st.write_crash("sid-c2", "alpha", iterations_completed=2, world_dir=tmp_path)
    crashed = _read(tmp_path, "alpha", "sid-c2")
    st.write_close("sid-c2", "alpha", status="completed", ended_reason="user-stop",
                   world_dir=tmp_path)
    assert _read(tmp_path, "alpha", "sid-c2") == crashed


def test_active_record_can_still_be_closed(tmp_path):
    # The guard only blocks finalized->finalized; active->completed still works.
    st.write_open("sid-a2", "alpha", "reader", "claude-code", world_dir=tmp_path)
    out = st.write_close("sid-a2", "alpha", status="completed", ended_reason="user-stop",
                         world_dir=tmp_path)
    assert out is not None
    assert _read(tmp_path, "alpha", "sid-a2")["status"] == "completed"


def test_duration_clamped_nonnegative():
    # A parseable computation that goes negative (clock skew) floors at 0 so it
    # is never confused with the -1 uncomputable sentinel.
    assert st._duration_seconds("2026-06-03T06:00:00", "2026-06-03T05:00:00") == 0
    assert st._duration_seconds("2026-06-03T05:00:01", "2026-06-03T05:00:00") == 0
    # Truly uncomputable stays -1.
    assert st._duration_seconds("unknown", "2026-06-03T05:00:10") == -1
    assert st._duration_seconds(None, "2026-06-03T05:00:10") == -1


def test_path_traversal_rejected(tmp_path):
    # sid/agent with separators or .. must not escape the telemetry tree.
    for bad in ("../../../evil", "a/b", "a\\b", "..", ".", ""):
        assert st._record_path("alpha", bad, world_dir=tmp_path) is None
        assert st._record_path(bad, "sid-1", world_dir=tmp_path) is None
        assert st.write_open(bad, "alpha", "reader", "x", world_dir=tmp_path) is None
    # A traversal sid never writes a file outside the telemetry tree.
    st.write_open("../../../../evil_traversal_test", "alpha", "reader", "x", world_dir=tmp_path)
    assert not (tmp_path.parent.parent.parent.parent / "evil_traversal_test.json").exists()
