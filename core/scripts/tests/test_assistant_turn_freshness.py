#!/usr/bin/env python3
# domain-leak-exempt: framework recovery infra; fixture rows are Claude Code transcript literals
"""Tests for assistant-turn-freshness.py — recovery-gate Path D liveness veto (g-115-6253).

Every case below was verified RED by mutating the implementation before being
committed green. The two that matter most are the ones a reader would not
predict from the feature description:

  * ``test_no_transcript_does_not_suppress`` — absence is NOT evidence of
    liveness. Inverting this one turns a narrowing of Path D into a DELETION of
    it on every box where the agent's runner does not live (measured cc-02: 4 of
    5 fleet agents).
  * ``test_tz_aware_timestamp_is_parsed`` — transcript rows are tz-AWARE while
    the sibling diary's are naive. A hand-rolled ``fromisoformat`` + naive
    ``now`` comparison raises TypeError, which the caller would swallow as
    ``unreadable`` -> permanent suppression. phase-wedge-check.py shipped exactly
    that regression once already.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "assistant_turn_freshness", str(SCRIPT_DIR / "assistant-turn-freshness.py"))
atf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(atf)

NOW = datetime(2026, 8, 15, 12, 0, 0)
SID = "caeb1579-54b2-4fdc-b99f-fd23b4ebbba2"


def _row(kind, ts, **extra):
    d = {"type": kind}
    if ts is not None:
        d["timestamp"] = ts
    d.update(extra)
    return json.dumps(d)


def _mkagent(tmp_path, sid=SID):
    ad = tmp_path / "agents" / "zeta"
    (ad / "session").mkdir(parents=True)
    if sid is not None:
        (ad / "session" / "running-session-id").write_text(sid, encoding="utf-8")
    return ad


def _mktranscript(tmp_path, lines, sid=SID):
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    (td / ("%s.jsonl" % sid)).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return td


def _check(tmp_path, lines, sid=SID, agent_sid=SID, now=NOW, threshold=60.0):
    ad = _mkagent(tmp_path, sid=agent_sid)
    td = _mktranscript(tmp_path, lines, sid=sid) if lines is not None else tmp_path / "projects" / "empty"
    td.mkdir(parents=True, exist_ok=True)
    return atf.check(agent_dir=ad, transcripts_dir=td, now=now, threshold=threshold)


# --- core behaviour -------------------------------------------------------

def test_recent_assistant_turn_suppresses(tmp_path):
    v, rc = _check(tmp_path, [_row("assistant", "2026-08-15T11:55:00.000Z")])
    assert rc == 0
    assert v["suppress"] is True
    assert v["verdict"] == "recent_assistant_turn"
    assert v["age_minutes"] == pytest.approx(5.0)


def test_old_assistant_turn_does_not_suppress(tmp_path):
    v, rc = _check(tmp_path, [_row("assistant", "2026-08-15T10:30:00.000Z")])
    assert rc == 1
    assert v["suppress"] is False
    assert v["verdict"] == "no_recent_assistant_turn"
    assert v["age_minutes"] == pytest.approx(90.0)


def test_exactly_at_threshold_suppresses(tmp_path):
    """<= threshold, not <. Boundary pinned so a future refactor cannot flip it."""
    v, rc = _check(tmp_path, [_row("assistant", "2026-08-15T11:00:00.000Z")])
    assert rc == 0 and v["suppress"] is True


def test_newest_wins_not_first_seen(tmp_path):
    """Scan is backwards; an OLD row after a NEW one must not win."""
    v, rc = _check(tmp_path, [
        _row("assistant", "2026-08-15T09:00:00.000Z"),
        _row("assistant", "2026-08-15T11:58:00.000Z"),
    ])
    assert rc == 0
    assert v["age_minutes"] == pytest.approx(2.0)


# --- absence is not liveness (the load-bearing pair) ----------------------

def test_no_transcript_does_not_suppress(tmp_path):
    """THE case that keeps Path D alive off-box. Inverting it deletes Path D
    everywhere the agent's runner session does not live."""
    ad = _mkagent(tmp_path)
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    v, rc = atf.check(agent_dir=ad, transcripts_dir=td, now=NOW)
    assert rc == 1
    assert v["suppress"] is False
    assert v["verdict"] == "no_transcript"


def test_no_running_session_id_does_not_suppress(tmp_path):
    ad = _mkagent(tmp_path, sid=None)
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    v, rc = atf.check(agent_dir=ad, transcripts_dir=td, now=NOW)
    assert rc == 1
    assert v["suppress"] is False
    assert v["verdict"] == "no_running_session_id"


def test_empty_running_session_id_does_not_suppress(tmp_path):
    ad = _mkagent(tmp_path, sid="   ")
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    v, rc = atf.check(agent_dir=ad, transcripts_dir=td, now=NOW)
    assert rc == 1 and v["verdict"] == "no_running_session_id"


# --- guard-487: present-but-unreadable fails CLOSED as suppressed ---------

def test_unreadable_transcript_suppresses(tmp_path, monkeypatch):
    monkeypatch.setattr(atf, "newest_assistant_timestamp",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    v, rc = _check(tmp_path, [_row("assistant", "2026-08-15T11:55:00.000Z")])
    assert rc == 2
    assert v["verdict"] == "unreadable"
    assert "OSError" in v["error"]


def test_main_never_tracebacks(tmp_path, monkeypatch, capsys):
    """The outer belt-and-braces guard: a probe that tracebacks inside a
    SessionStart hook is worse than one that returns a verdict."""
    monkeypatch.setattr(atf, "check",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    monkeypatch.setattr(sys, "argv", ["assistant-turn-freshness.py"])
    rc = atf.main()
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "unreadable"


# --- row-level hazards ----------------------------------------------------

def test_tz_aware_timestamp_is_parsed(tmp_path):
    """Transcript rows are AWARE ('Z'); the diary's are naive. Parsing must
    normalise to UTC-naive rather than raising on the comparison."""
    v, rc = _check(tmp_path, [_row("assistant", "2026-08-15T11:55:00.000Z")])
    assert rc == 0 and v["newest_assistant_at"] == "2026-08-15T11:55:00"


def test_offset_timestamp_normalised_to_utc(tmp_path):
    """07:55-04:00 == 11:55Z -> 5 min old, suppressing. If the offset were
    dropped instead of applied this reads 4h05m old and does NOT suppress."""
    v, rc = _check(tmp_path, [_row("assistant", "2026-08-15T07:55:00.000-04:00")])
    assert rc == 0
    assert v["age_minutes"] == pytest.approx(5.0)


def test_future_dated_row_ignored(tmp_path):
    """A future row is not evidence anything is alive; admitting one would
    permanently suppress Path D for this agent."""
    v, rc = _check(tmp_path, [
        _row("assistant", "2026-08-15T10:30:00.000Z"),
        _row("assistant", "2026-08-16T09:00:00.000Z"),
    ])
    assert rc == 1
    assert v["age_minutes"] == pytest.approx(90.0)


def test_non_dict_row_skipped(tmp_path):
    """A bare JSON scalar/array raises AttributeError on .get otherwise."""
    v, rc = _check(tmp_path, [
        _row("assistant", "2026-08-15T11:55:00.000Z"),
        "42", '["a","b"]', 'null',
    ])
    assert rc == 0 and v["age_minutes"] == pytest.approx(5.0)


def test_malformed_json_row_skipped(tmp_path):
    v, rc = _check(tmp_path, [
        _row("assistant", "2026-08-15T11:55:00.000Z"),
        '{"type": "assistant", broken',
    ])
    assert rc == 0


def test_non_assistant_rows_ignored(tmp_path):
    """A user turn or a tool attachment is not an assistant turn. If these
    counted, any inbound row would suppress recovery."""
    v, rc = _check(tmp_path, [
        _row("assistant", "2026-08-15T10:30:00.000Z"),
        _row("user", "2026-08-15T11:59:00.000Z"),
        _row("attachment", "2026-08-15T11:59:30.000Z"),
    ])
    assert rc == 1
    assert v["age_minutes"] == pytest.approx(90.0)


def test_assistant_row_without_timestamp_ignored(tmp_path):
    v, rc = _check(tmp_path, [
        _row("assistant", "2026-08-15T10:30:00.000Z"),
        _row("assistant", None),
    ])
    assert rc == 1 and v["age_minutes"] == pytest.approx(90.0)


def test_no_assistant_turn_in_tail(tmp_path):
    v, rc = _check(tmp_path, [_row("user", "2026-08-15T11:59:00.000Z")])
    assert rc == 1 and v["verdict"] == "no_assistant_turn_in_tail"


def test_partial_first_line_discarded(tmp_path):
    """A byte-offset seek lands mid-line. Half a JSON object is not a row —
    and if the truncation happened to leave valid JSON it would be a row from
    the wrong position."""
    ad = _mkagent(tmp_path)
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    p = td / ("%s.jsonl" % SID)
    filler = _row("assistant", "2026-08-15T09:00:00.000Z", pad="x" * 400)
    body = "\n".join([filler] * 60 + [_row("assistant", "2026-08-15T11:55:00.000Z")])
    p.write_text(body + "\n", encoding="utf-8")
    ts = atf.newest_assistant_timestamp(p, NOW, tail_bytes=500)
    assert ts == datetime(2026, 8, 15, 11, 55, 0)


def test_tail_smaller_than_file_still_finds_recent(tmp_path):
    """Cost independence: the newest row is found from a tail far smaller than
    the file, which is the whole reason this is hook-budget-safe."""
    ad = _mkagent(tmp_path)
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    p = td / ("%s.jsonl" % SID)
    old = "\n".join(_row("assistant", "2026-08-15T09:00:00.000Z", pad="y" * 200)
                    for _ in range(500))
    p.write_text(old + "\n" + _row("assistant", "2026-08-15T11:50:00.000Z") + "\n",
                 encoding="utf-8")
    assert p.stat().st_size > 50_000
    assert atf.newest_assistant_timestamp(p, NOW, tail_bytes=2_000) == \
        datetime(2026, 8, 15, 11, 50, 0)


# --- threshold resolution -------------------------------------------------

def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_TURN_FRESH_MINUTES", "10")
    assert atf.fresh_threshold_minutes() == 10.0


def test_garbage_env_falls_back_to_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_TURN_FRESH_MINUTES", "not-a-number")
    cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml").read_text())
    assert atf.fresh_threshold_minutes() == \
        float(cfg["runner_heartbeat"]["assistant_turn_fresh_minutes"])


def test_config_value_is_read(monkeypatch):
    monkeypatch.delenv("ASSISTANT_TURN_FRESH_MINUTES", raising=False)
    cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml").read_text())
    assert atf.fresh_threshold_minutes() == \
        float(cfg["runner_heartbeat"]["assistant_turn_fresh_minutes"])


def test_config_invariant_fresh_below_wedge_stale():
    """assistant_turn_fresh_minutes < wedge_stale_minutes.

    If the window were >= the wedge threshold, an assistant turn from the very
    START of the wedge window — plausibly the turn that WEDGED — could suppress
    the recovery of its own wedge. Sibling of
    test_phase_wedge_check.py::test_config_invariant_wedge_exceeds_heartbeat_stale.
    """
    cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml").read_text())
    rh = cfg["runner_heartbeat"]
    assert float(rh["assistant_turn_fresh_minutes"]) < float(rh["wedge_stale_minutes"])


def test_direction_is_not_the_guard_3802_shape(tmp_path):
    """guard-3802: a suppressor whose window length IS the severity metric
    silences the worst cases most. This predicate is point-freshness against a
    FIXED threshold, so P(suppress) must DECREASE as the wedge lengthens.
    Asserted behaviourally: holding the turn fixed and advancing `now`
    (a longer wedge) can only move suppress True->False, never back."""
    lines = [_row("assistant", "2026-08-15T12:00:00.000Z")]
    seen = [_check(tmp_path / str(i), lines, now=NOW + timedelta(minutes=m))[0]["suppress"]
            for i, m in enumerate([0, 30, 59, 61, 120, 600])]
    assert seen == [True, True, True, False, False, False]
    assert not any(seen[i + 1] and not seen[i] for i in range(len(seen) - 1))


# --- transcripts dir resolution ------------------------------------------

def test_default_transcripts_dir_dashifies_project_root():
    d = atf.default_transcripts_dir(Path("/opt/ayoai-mind"))
    assert d.name == "-opt-ayoai-mind"
    assert d.parent == Path(os.path.expanduser("~/.claude/projects"))


# --- zakcode runner: a different transcript store on THIS box (2026-08-30) ------
# Measured on coach@zc-03: the reducer's running-session-id IS its zakcode session id,
# the Claude Code transcript is absent by construction, the probe said no_transcript,
# and Path D recovered a live reducer (heartbeat fresh, mid-select) to IDLE.

ZSID = "ed651c6e2b6f4d428a962269ce5fee63"


def _zmsg(role, created_at):
    return {"role": role, "created_at": created_at, "blocks": [{"type": "text", "text": "x"}]}


def _mkzakcode(tmp_path, messages, sid=ZSID, home="zakcode-home"):
    zh = tmp_path / home
    (zh / "sessions").mkdir(parents=True)
    (zh / "sessions" / ("%s.json" % sid)).write_text(
        json.dumps({"id": sid, "build": "bbd620e67969", "messages": messages}), encoding="utf-8")
    return zh


def _zcheck(tmp_path, messages, now=NOW, threshold=60.0, sid=ZSID):
    ad = _mkagent(tmp_path, sid=sid)
    td = tmp_path / "projects" / "-opt-ayoai-mind"   # no Claude Code transcript here
    td.mkdir(parents=True)
    zh = _mkzakcode(tmp_path, messages, sid=sid) if messages is not None else tmp_path / "no-zakcode"
    return atf.check(agent_dir=ad, transcripts_dir=td, now=now, threshold=threshold,
                     zakcode_home=str(zh))


def test_zakcode_recent_assistant_message_suppresses(tmp_path):
    """The incident's shape: no Claude Code transcript, a live zakcode session."""
    v, rc = _zcheck(tmp_path, [_zmsg("user", "2026-08-15T11:50:00+00:00"),
                               _zmsg("assistant", "2026-08-15T11:57:00.123456+00:00"),
                               _zmsg("tool", "2026-08-15T11:58:00+00:00")])
    assert rc == 0 and v["suppress"] is True
    assert v["verdict"] == "recent_assistant_turn"
    assert v["transcript_kind"] == "zakcode" and v["transcript"].endswith("%s.json" % ZSID)
    assert v["age_minutes"] == pytest.approx(3.0, abs=0.01)


def test_zakcode_old_assistant_message_does_not_suppress(tmp_path):
    v, rc = _zcheck(tmp_path, [_zmsg("assistant", "2026-08-15T10:00:00+00:00"),
                               _zmsg("tool", "2026-08-15T11:59:00+00:00")])  # tool rows are not turns
    assert rc == 1 and v["suppress"] is False
    assert v["verdict"] == "no_recent_assistant_turn"
    assert v["age_minutes"] == pytest.approx(120.0)


def test_zakcode_newest_wins_regardless_of_order_and_future_rows_ignored(tmp_path):
    v, rc = _zcheck(tmp_path, [_zmsg("assistant", "2026-08-15T11:58:00+00:00"),
                               _zmsg("assistant", "2026-08-15T09:00:00+00:00"),
                               _zmsg("assistant", "2026-08-15T13:00:00+00:00")])
    assert rc == 0 and v["age_minutes"] == pytest.approx(2.0)


def test_zakcode_doc_without_assistant_rows_does_not_suppress(tmp_path):
    v, rc = _zcheck(tmp_path, [_zmsg("user", "2026-08-15T11:59:00+00:00")])
    assert rc == 1 and v["verdict"] == "no_assistant_turn_in_tail"


def test_zakcode_unreadable_doc_suppresses_like_an_unreadable_transcript(tmp_path):
    ad = _mkagent(tmp_path, sid=ZSID)
    td = tmp_path / "projects" / "-opt-ayoai-mind"
    td.mkdir(parents=True)
    zh = tmp_path / "zakcode-home"
    (zh / "sessions").mkdir(parents=True)
    (zh / "sessions" / ("%s.json" % ZSID)).write_text("{not json", encoding="utf-8")
    v, rc = atf.check(agent_dir=ad, transcripts_dir=td, now=NOW, zakcode_home=str(zh))
    assert rc == 2 and v["verdict"] == "unreadable"


def test_no_transcript_in_either_store_still_does_not_suppress(tmp_path):
    """The off-box case survives the fallback: absence in BOTH stores is still absence."""
    v, rc = _zcheck(tmp_path, None)
    assert rc == 1 and v["suppress"] is False and v["verdict"] == "no_transcript"


def test_claude_code_transcript_wins_over_a_zakcode_doc(tmp_path):
    ad = _mkagent(tmp_path, sid=ZSID)
    td = _mktranscript(tmp_path, [_row("assistant", "2026-08-15T11:55:00.000Z")], sid=ZSID)
    zh = _mkzakcode(tmp_path, [_zmsg("assistant", "2026-08-15T09:00:00+00:00")])
    v, rc = atf.check(agent_dir=ad, transcripts_dir=td, now=NOW, zakcode_home=str(zh))
    assert rc == 0 and "transcript_kind" not in v


def test_zakcode_home_resolution_order(tmp_path, monkeypatch):
    """$ZAKCODE_HOME, then ~/.zakcode, then <project>/.zakcode — the served-workspace store."""
    monkeypatch.delenv("ZAKCODE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert atf.zakcode_session_doc(ZSID, tmp_path / "proj") is None
    served = tmp_path / "proj" / ".zakcode" / "sessions"
    served.mkdir(parents=True)
    (served / ("%s.json" % ZSID)).write_text("{}", encoding="utf-8")
    assert atf.zakcode_session_doc(ZSID, tmp_path / "proj") == served / ("%s.json" % ZSID)
    monkeypatch.setenv("ZAKCODE_HOME", str(tmp_path / "etc-zakcode"))
    (tmp_path / "etc-zakcode" / "sessions").mkdir(parents=True)
    (tmp_path / "etc-zakcode" / "sessions" / ("%s.json" % ZSID)).write_text("{}", encoding="utf-8")
    assert atf.zakcode_session_doc(ZSID, tmp_path / "proj").parent.parent == tmp_path / "etc-zakcode"
