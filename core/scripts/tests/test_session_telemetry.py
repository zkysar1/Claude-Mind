"""Unit tests for _session_telemetry.py (session telemetry Phase 1).

All tests inject world_dir=tmp_path so they never touch the real WORLD_DIR.
No daemon dependency; pure filesystem + tmp_path.
"""
import os
import sys
import json
import datetime
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


# ── Phase 1.5 stale-active reaper ─────────────────────────────────────────────
# A fixed "now" so reaper tests never depend on the real wall clock; a stale
# started_at 7 days before it (comfortably past the 24h freshness window).
_NOW = datetime.datetime(2026, 6, 8, 0, 0, 0)
_STALE_START = "2026-06-01T00:00:00"


def _seed_record(tmp_path, agent, sid, **overrides):
    """Write a telemetry record straight to disk (bypassing write_open) so reaper
    tests control started_at / machine_id / status precisely. Defaults to a stale
    active record on machine-A."""
    rec = {
        "schema_version": st.SCHEMA_VERSION,
        "session_id": sid,
        "agent": agent,
        "status": "active",
        "ended_reason": None,
        "started_at": _STALE_START,
        "ended_at": None,
        "machine_id": "machine-A",
        "duration_seconds": None,
    }
    rec.update(overrides)
    p = tmp_path / "telemetry" / "session-records" / agent / (sid + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return p


def _set_heartbeat(project_root, agent, when):
    """Create agents/<agent>/session/runner-heartbeat with mtime == `when`
    (a naive-local datetime). Round-trips exactly: `when` has zero sub-second
    component so utime/fromtimestamp lose no precision."""
    hb = project_root / "agents" / agent / "session" / "runner-heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("tick", encoding="utf-8")
    ts = when.timestamp()
    os.utime(hb, (ts, ts))
    return hb


def test_parse_local_iso():
    assert st._parse_local_iso("2026-06-01T00:00:00") == datetime.datetime(2026, 6, 1, 0, 0, 0)
    assert st._parse_local_iso("unknown") is None
    assert st._parse_local_iso(None) is None
    assert st._parse_local_iso("") is None
    assert st._parse_local_iso("not-a-timestamp") is None


def test_runner_recently_active_branches(tmp_path):
    # project_root None -> True (cannot verify -> treat as alive -> do NOT reap)
    assert st._runner_recently_active("alpha", _NOW, None) is True
    # heartbeat absent -> False (no autonomous runner -> reapable)
    assert st._runner_recently_active("alpha", _NOW, tmp_path) is False
    cutoff = _NOW - datetime.timedelta(hours=6)
    # heartbeat fresh (mtime >= cutoff) -> True (alive)
    _set_heartbeat(tmp_path, "alpha", _NOW)
    assert st._runner_recently_active("alpha", cutoff, tmp_path) is True
    # heartbeat stale (mtime < cutoff) -> False (idle -> reapable)
    _set_heartbeat(tmp_path, "bravo", _NOW - datetime.timedelta(hours=12))
    assert st._runner_recently_active("bravo", cutoff, tmp_path) is False


def _set_body_heartbeat(project_root, agent, sid, when):
    """Create agents/<agent>/sessions/<sid>/body-heartbeat with mtime == `when`.

    The SAME-BOX per-Body signal (heartbeat-tick.sh), deliberately NOT the
    syncable `session/body-heartbeat-<sid>.json` carrier — that one's mtime does
    not survive the sync and it can be written by another machine."""
    hb = (project_root / st.AGENTS_PARENT_DIR / agent / st.SESSIONS_DIRNAME
          / sid / "body-heartbeat")
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("tick", encoding="utf-8")
    ts = when.timestamp()
    os.utime(hb, (ts, ts))
    return hb


def test_body_recently_active_branches(tmp_path):
    """. A worker Body never writes the agent-wide runner-heartbeat
    (it is agent-state=IDLE by design and heartbeat-tick.sh refuses the write),
    so its liveness has to be read from the per-session heartbeat instead."""
    cutoff = _NOW - datetime.timedelta(hours=6)
    # project_root None -> True (cannot verify -> treat as alive -> do NOT reap)
    assert st._body_recently_active("alpha", _NOW, None) is True
    # no sessions dir at all -> False (nothing claims to be alive)
    assert st._body_recently_active("alpha", cutoff, tmp_path) is False
    # a STALE body heartbeat is not liveness
    _set_body_heartbeat(tmp_path, "alpha", "sid-old",
                        _NOW - datetime.timedelta(hours=12))
    assert st._body_recently_active("alpha", cutoff, tmp_path) is False
    # ANY fresh Body makes the agent live on this box
    _set_body_heartbeat(tmp_path, "alpha", "sid-live", _NOW)
    assert st._body_recently_active("alpha", cutoff, tmp_path) is True
    # scoped to the named agent — bravo's Body must not vouch for charlie
    _set_body_heartbeat(tmp_path, "bravo", "sid-b", _NOW)
    assert st._body_recently_active("charlie", cutoff, tmp_path) is False


def test_agent_recently_active_ors_runner_and_body(tmp_path):
    """The predicate the reaper actually needs. Either signal alone is enough;
    absence of BOTH is what makes an agent reapable."""
    cutoff = _NOW - datetime.timedelta(hours=6)
    assert st._agent_recently_active("alpha", cutoff, tmp_path) is False
    # runner only (a reducer)
    _set_heartbeat(tmp_path, "alpha", _NOW)
    assert st._agent_recently_active("alpha", cutoff, tmp_path) is True
    # body only (a worker) — the case that was broken
    assert st._runner_recently_active("worker", cutoff, tmp_path) is False
    _set_body_heartbeat(tmp_path, "worker", "sid-w", _NOW)
    assert st._agent_recently_active("worker", cutoff, tmp_path) is True


def test_live_worker_record_survives_a_reap(tmp_path):
    """THE OUTCOME-2 PIN, and the one that discriminates.

    Reproduces the measured incident (cc-08 2026-08-19, again on cc-07
    2026-08-20 with a live Body): a worker's ACTIVE record, older than the 24h
    freshness window, with a fresh per-Body heartbeat and NO agent-wide
    runner-heartbeat. Before the fix `reaped_ids` contained the EXECUTING SID
    and the record was flipped to status=unknown mid-execution.

    Note the freshness window is deliberately expired here. That window was the
    only thing incidentally protecting live workers, and it protects nothing
    once a session runs longer than a day — the live specimen that motivated
    this was 115.7h old."""
    _seed_record(tmp_path, "worker", "sid-executing", machine_id="machine-A")
    _set_body_heartbeat(tmp_path, "worker", "sid-executing", _NOW)
    # the agent-wide heartbeat is ABSENT, exactly as on a real worker box
    assert not (tmp_path / st.AGENTS_PARENT_DIR / "worker" / st.SESSION_DIRNAME
                / "runner-heartbeat").exists()

    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")

    assert summary["reaped"] == 0, (
        "a live worker Body's in-flight record was reaped — this is the "
        "g-115-6939 corruption, not a stale-orphan cleanup"
    )
    assert "sid-executing" not in summary["reaped_ids"]
    assert summary["skipped_live"] == 1
    assert _read(tmp_path, "worker", "sid-executing")["status"] == "active"


def test_a_genuinely_dead_worker_is_still_reaped(tmp_path):
    """The negative control. Without this, a fix that simply never reaps would
    pass the test above while disabling the reaper entirely."""
    _seed_record(tmp_path, "worker", "sid-dead", machine_id="machine-A")
    _set_body_heartbeat(tmp_path, "worker", "sid-dead",
                        _NOW - datetime.timedelta(hours=48))

    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")

    assert summary["reaped"] == 1
    assert "sid-dead" in summary["reaped_ids"]
    assert _read(tmp_path, "worker", "sid-dead")["status"] == "unknown"


def test_reap_stale_active_flips_to_unknown(tmp_path):
    _seed_record(tmp_path, "zeta", "sid-orphan", machine_id="machine-A")
    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")
    assert summary["scanned"] == 1
    assert summary["reaped"] == 1
    assert "sid-orphan" in summary["reaped_ids"]
    r = _read(tmp_path, "zeta", "sid-orphan")
    assert r["status"] == "unknown"
    assert r["ended_reason"] == "unknown"
    assert r["ended_at"] == "2026-06-08T00:00:00"
    assert r["end_machine_id"] == "machine-A"
    assert r["duration_seconds"] == 7 * 86400  # 2026-06-01 -> 2026-06-08


def test_reap_skips_fresh(tmp_path):
    # started 4h before _NOW -> within the 24h freshness window
    _seed_record(tmp_path, "zeta", "sid-fresh", started_at="2026-06-07T20:00:00")
    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")
    assert summary["reaped"] == 0
    assert summary["skipped_fresh"] == 1
    assert _read(tmp_path, "zeta", "sid-fresh")["status"] == "active"


def test_reap_skips_live_runner(tmp_path):
    # stale record, but the agent's runner ticked its heartbeat recently
    _seed_record(tmp_path, "zeta", "sid-live")
    _set_heartbeat(tmp_path, "zeta", _NOW)  # fresh heartbeat -> alive
    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")
    assert summary["reaped"] == 0
    assert summary["skipped_live"] == 1
    assert _read(tmp_path, "zeta", "sid-live")["status"] == "active"


def test_reap_skips_completed_and_crashed(tmp_path):
    # Non-active records are scanned but never reaped (idempotent short-circuit).
    _seed_record(tmp_path, "zeta", "sid-done", status="completed",
                 ended_reason="user-stop")
    _seed_record(tmp_path, "zeta", "sid-crash", status="crashed",
                 ended_reason="recovery-gate")
    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")
    assert summary["scanned"] == 2
    assert summary["reaped"] == 0
    assert summary["skipped_fresh"] == 0
    assert summary["skipped_live"] == 0
    assert _read(tmp_path, "zeta", "sid-done")["status"] == "completed"
    assert _read(tmp_path, "zeta", "sid-crash")["status"] == "crashed"


def test_reap_skips_other_machine(tmp_path):
    # A record from machine-B is left for THAT machine's reaper (its runner
    # liveness can't be checked from here).
    _seed_record(tmp_path, "zeta", "sid-elsewhere", machine_id="machine-B")
    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")
    assert summary["reaped"] == 0
    assert summary["skipped_other_machine"] == 1
    assert _read(tmp_path, "zeta", "sid-elsewhere")["status"] == "active"


def test_reap_missing_records_dir_total(tmp_path):
    # No telemetry dir at all -> zero summary, never raises.
    summary = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                   now=_NOW, machine_id="machine-A")
    assert summary == {"scanned": 0, "reaped": 0, "skipped_fresh": 0,
                       "skipped_live": 0, "skipped_other_machine": 0,
                       "reaped_ids": []}


def test_reap_idempotent(tmp_path):
    _seed_record(tmp_path, "zeta", "sid-once")
    first = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                 now=_NOW, machine_id="machine-A")
    assert first["reaped"] == 1
    # Second pass: the record is now status=unknown -> not active -> not reaped.
    second = st.reap_stale_active(world_dir=tmp_path, project_root=tmp_path,
                                  now=_NOW, machine_id="machine-A")
    assert second["scanned"] == 1
    assert second["reaped"] == 0
    assert _read(tmp_path, "zeta", "sid-once")["status"] == "unknown"
