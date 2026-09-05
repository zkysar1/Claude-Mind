"""peer_liveness: the decision table behind the reducer's peer-liveness page.

Owner directive 2026-09-05 after foxtrot sat ~9h dark on an API error with no
alert. The load-bearing negatives here are the guard-4180 ones: a stale
heartbeat ALONE never yields `stalled`, a peer whose diary / board / goal
records moved is `slow` (never paged), and a probe that could not READ any
corroborating signal reports `unknown`, never a page.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peer_liveness as pl  # noqa: E402

NOW = dt.datetime(2026, 9, 5, 8, 0, 0)
THR = 3.0


def ago(hours):
    return NOW - dt.timedelta(hours=hours)


def iso(hours):
    return ago(hours).strftime("%Y-%m-%dT%H:%M:%S")


def row(**kw):
    base = {"last_active": iso(0.5), "session_ended": False,
            "live_phase": "phase-4-execute g-1", "in_flight": {"goal_id": "g-1", "title": "x"},
            "row_updated_by": "peer"}
    base.update(kw)
    return base


def classify(r, prov=pl.PROV_AUTHORITATIVE, **signals):
    return pl.classify_peer("peer", r, prov, now=NOW, stale_hours=THR, **signals)


# ── classify_peer ────────────────────────────────────────────────────────────

def test_fresh_heartbeat_is_alive_without_any_corroboration():
    v = classify(row())
    assert v["verdict"] == pl.V_ALIVE
    assert v["corroboration_needed"] is False
    assert v["in_flight"] == "g-1"


def test_boundary_age_equal_to_threshold_is_still_alive():
    v = classify(row(last_active=iso(THR)))
    assert v["verdict"] == pl.V_ALIVE


def test_stale_heartbeat_alone_is_not_a_verdict():
    """guard-4180: a fixed-point stamp's age measures cycle length, not liveness."""
    v = classify(row(last_active=iso(9)))
    assert v["verdict"] == pl.V_UNKNOWN
    assert v["corroboration_needed"] is True
    assert not pl.is_alerting(v["verdict"])


@pytest.mark.parametrize("which", ["diary", "board", "goals"])
def test_any_fresh_independent_signal_makes_it_slow_not_stalled(which):
    sigs = {"diary": pl.signal(ago(9), True, "diary"), "board": pl.signal(ago(9), True, "board"),
            "goals": pl.signal(ago(9), True, "goals")}
    sigs[which] = pl.signal(ago(1), True, which)
    v = classify(row(last_active=iso(9)), **sigs)
    assert v["verdict"] == pl.V_SLOW
    assert not pl.is_alerting(v["verdict"])
    assert v["signals"][which]["fresh"] is True


def test_fresh_signal_from_a_mirror_still_counts_as_life():
    """A mirror cannot invent activity that did not happen -- positive evidence is fine."""
    v = classify(row(last_active=iso(9)),
                 diary=pl.signal(ago(1), False, "diary:local-mirror"),
                 board=pl.signal(None, True, "board"), goals=pl.signal(None, True, "goals"))
    assert v["verdict"] == pl.V_SLOW


def test_every_readable_signal_frozen_is_stalled():
    v = classify(row(last_active=iso(9)),
                 diary=pl.signal(ago(9.5), True, "diary:authoritative"),
                 board=pl.signal(ago(12), True, "board"),
                 goals=pl.signal(ago(13), True, "goals"))
    assert v["verdict"] == pl.V_STALLED
    assert pl.is_alerting(v["verdict"])
    assert "diary" in v["reason"] and "board" in v["reason"] and "goals" in v["reason"]


def test_readable_but_empty_signals_count_as_frozen():
    """`none found` from the store of record IS evidence (it was read and had nothing)."""
    v = classify(row(last_active=iso(9)),
                 diary=pl.signal(None, True, "diary:absent-in-store"),
                 board=pl.signal(None, True, "board"), goals=pl.signal(None, True, "goals"))
    assert v["verdict"] == pl.V_STALLED


def test_no_readable_corroboration_is_unknown_never_a_page():
    """guard-1753 / guard-1977: blindness must not render as death."""
    v = classify(row(last_active=iso(9)),
                 diary=pl.signal(ago(9), False, "diary:local-mirror"),
                 board=pl.signal(None, False, "board"), goals=pl.signal(None, False, "goals:none"))
    assert v["verdict"] == pl.V_UNKNOWN
    assert "blind" in v["reason"]
    assert not pl.is_alerting(v["verdict"])


def test_stale_mirror_signal_is_not_evidence_but_readable_stale_sibling_is():
    v = classify(row(last_active=iso(9)),
                 diary=pl.signal(ago(9), False, "diary:local-mirror"),
                 board=pl.signal(ago(20), True, "board"), goals=pl.signal(None, False, "goals"))
    assert v["verdict"] == pl.V_STALLED
    assert v["reason"].endswith("(board) is frozen too")


def test_mirror_row_is_unknown():
    v = classify(row(last_active=iso(9)), prov=pl.PROV_LOCAL_MIRROR,
                 diary=pl.signal(ago(9), True), board=pl.signal(ago(9), True), goals=pl.signal(ago(9), True))
    assert v["verdict"] == pl.V_UNKNOWN
    assert "guard-980" in v["reason"]


def test_missing_row_is_unknown():
    assert classify(None, prov=pl.PROV_NONE)["verdict"] == pl.V_UNKNOWN
    assert classify({}, prov=pl.PROV_AUTHORITATIVE)["verdict"] == pl.V_UNKNOWN


@pytest.mark.parametrize("ended", [True, "true", "True"])
def test_session_ended_is_stopped_even_when_stale(ended):
    v = classify(row(last_active=iso(40), session_ended=ended),
                 diary=pl.signal(None, True), board=pl.signal(None, True), goals=pl.signal(None, True))
    assert v["verdict"] == pl.V_STOPPED
    assert not pl.is_alerting(v["verdict"])


def test_retired_tombstone_is_retired_not_stalled():
    v = classify(row(last_active=iso(40), retired=True, retired_at=iso(30)),
                 diary=pl.signal(None, True), board=pl.signal(None, True), goals=pl.signal(None, True))
    assert v["verdict"] == pl.V_RETIRED


def test_revived_after_retirement_is_classified_on_freshness():
    """_team_state's revival rule: a heartbeat newer than retired_at un-retires."""
    v = classify(row(last_active=iso(0.2), retired=True, retired_at=iso(5)))
    assert v["verdict"] == pl.V_ALIVE


def test_unparsable_last_active_is_unknown():
    v = classify(row(last_active="not-a-stamp"))
    assert v["verdict"] == pl.V_UNKNOWN
    assert v["corroboration_needed"] is False


# ── threshold parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [("4.5", 4.5), ("0", pl.DEFAULT_STALE_HOURS),
                                          ("-2", pl.DEFAULT_STALE_HOURS), ("x", pl.DEFAULT_STALE_HOURS),
                                          ("", pl.DEFAULT_STALE_HOURS)])
def test_stale_hours_env(monkeypatch, raw, expected):
    monkeypatch.setenv(pl.STALE_HOURS_ENV, raw)
    assert pl.stale_hours() == expected


# ── pure parsers behind the readers ───────────────────────────────────────────

def test_diary_head_is_the_last_line_timestamp():
    text = ('{"entry_type":"phase_start","timestamp":"2026-09-04T19:21:00","goal_id":"g-326-85"}\n'
            '{"entry_type":"phase_end","timestamp":"2026-09-04T22:25:11","goal_id":"g-326-85"}\n'
            'garbage tail with no stamp\n\n')
    assert pl._last_timestamp_in_text(text) == dt.datetime(2026, 9, 4, 22, 25, 11)
    assert pl._last_timestamp_in_text("") is None
    assert pl._last_timestamp_in_text("no stamps here") is None


def test_board_signals_use_self_timestamping_ids_per_agent():
    texts = {
        "coordination": ('{"id":"msg-20260904-192100-foxtrot-5","author":"foxtrot"}\n'
                         '{"id":"msg-20260905-071930-alpha-5004","author":"alpha"}\n'
                         '{"id":"msg-20260905-060102-zeta-5825","author":"zeta"}\n', pl.PROV_AUTHORITATIVE),
        "findings": ('{"id":"msg-20260904-174724-foxtrot-4","author":"foxtrot"}\n', pl.PROV_AUTHORITATIVE),
        "general": (None, "absent"),
    }
    sig = pl._board_signals_from_texts(texts, ["foxtrot", "alpha"])
    assert sig["foxtrot"]["ts"] == dt.datetime(2026, 9, 4, 19, 21, 0)
    assert sig["alpha"]["ts"] == dt.datetime(2026, 9, 5, 7, 19, 30)
    assert sig["foxtrot"]["readable"] is True
    assert "zeta" not in sig


def test_board_signals_mirror_only_is_not_readable():
    texts = {"coordination": ('{"id":"msg-20260904-192100-foxtrot-5"}\n', pl.PROV_LOCAL_MIRROR)}
    sig = pl._board_signals_from_texts(texts, ["foxtrot"])
    assert sig["foxtrot"]["ts"] is not None and sig["foxtrot"]["readable"] is False


def test_board_signals_do_not_match_a_longer_name_prefix():
    texts = {"coordination": ('{"id":"msg-20260905-010000-alphabet-1"}\n', pl.PROV_AUTHORITATIVE)}
    sig = pl._board_signals_from_texts(texts, ["alpha"])
    assert sig["alpha"]["ts"] is None


def test_goal_signals_take_the_newest_claim_or_completion_per_agent():
    lines = [
        json.dumps({"id": "asp-1", "goals": [
            {"id": "g-1", "completed_by": "foxtrot", "completed_date": "2026-09-04T10:00:00"},
            {"id": "g-2", "claimed_by": "foxtrot", "claimed_at": "2026-09-04T19:21:00"},
            {"id": "g-3", "claimed_by": "alpha", "claimed_at": "2026-09-05T06:58:07"},
            {"id": "g-4", "lastAchievedBy": "foxtrot", "lastAchievedAt": "2026-09-03T00:00:00"},
        ]}),
        "# a banner line the parser must skip",
        "{not json",
    ]
    sig = pl._goal_signals_from_lines(lines, pl.PROV_AUTHORITATIVE, ["foxtrot", "alpha", "ghost"])
    assert sig["foxtrot"]["ts"] == dt.datetime(2026, 9, 4, 19, 21, 0)
    assert sig["alpha"]["ts"] == dt.datetime(2026, 9, 5, 6, 58, 7)
    assert sig["ghost"]["ts"] is None and sig["ghost"]["readable"] is True


def test_goal_signals_unreadable_store_is_not_readable():
    sig = pl._goal_signals_from_lines(None, pl.PROV_NONE, ["foxtrot"])
    assert sig["foxtrot"] == pl.signal(None, False, "goals")


# ── scan wiring ───────────────────────────────────────────────────────────────

def _readers(rows, prov_by_agent=None, roster=pl.PROV_AUTHORITATIVE, calls=None):
    calls = calls if calls is not None else {"diary": [], "board": [], "goals": []}
    prov_by_agent = prov_by_agent or {a: pl.PROV_AUTHORITATIVE for a in rows}

    def rows_reader(_wd):
        return rows, prov_by_agent, roster

    def diary_reader(agent):
        calls["diary"].append(agent)
        return pl.signal(ago(9), True, "diary:authoritative")

    def board_reader(_wd, agents):
        calls["board"].append(sorted(agents))
        return {a: pl.signal(ago(12), True, "board") for a in agents}

    def goal_reader(_wd, agents):
        calls["goals"].append(sorted(agents))
        return {a: pl.signal(ago(13), True, "goals") for a in agents}

    return dict(rows_reader=rows_reader, diary_reader=diary_reader,
                board_reader=board_reader, goal_reader=goal_reader), calls


def test_scan_excludes_self_and_corroborates_only_stale_peers(tmp_path):
    rows = {"alpha": row(), "bravo": row(), "foxtrot": row(last_active=iso(9)), "echo": row()}
    readers, calls = _readers(rows)
    rep = pl.scan(tmp_path, "alpha", now=NOW, stale_hours_override=THR, **readers)
    names = [p["agent"] for p in rep["peers"]]
    assert names == ["bravo", "echo", "foxtrot"]
    by = {p["agent"]: p for p in rep["peers"]}
    assert by["bravo"]["verdict"] == pl.V_ALIVE and by["echo"]["verdict"] == pl.V_ALIVE
    assert by["foxtrot"]["verdict"] == pl.V_STALLED
    assert calls["diary"] == ["foxtrot"]
    assert calls["board"] == [["foxtrot"]] and calls["goals"] == [["foxtrot"]]
    assert rep["blind"] is False and rep["stale_hours"] == THR


def test_scan_skips_the_expensive_reads_when_nobody_is_stale(tmp_path):
    readers, calls = _readers({"alpha": row(), "bravo": row()})
    rep = pl.scan(tmp_path, "alpha", now=NOW, stale_hours_override=THR, **readers)
    assert [p["verdict"] for p in rep["peers"]] == [pl.V_ALIVE]
    assert calls == {"diary": [], "board": [], "goals": []}


def test_scan_reports_blind_when_the_rows_read_raises(tmp_path):
    def boom(_wd):
        raise RuntimeError("s3 down")
    rep = pl.scan(tmp_path, "alpha", now=NOW, stale_hours_override=THR, rows_reader=boom)
    assert rep["blind"] is True and "s3 down" in rep["blind_cause"]
    assert rep["peers"] == []


def test_scan_reports_blind_on_a_mirror_roster_but_still_classifies(tmp_path):
    readers, _ = _readers({"alpha": row(), "bravo": row()}, roster=pl.PROV_LOCAL_MIRROR)
    rep = pl.scan(tmp_path, "alpha", now=NOW, stale_hours_override=THR, **readers)
    assert rep["blind"] is True
    assert [p["agent"] for p in rep["peers"]] == ["bravo"]


def test_scan_survives_a_raising_corroboration_reader(tmp_path):
    readers, _ = _readers({"alpha": row(), "foxtrot": row(last_active=iso(9))})

    def bad_board(_wd, _agents):
        raise OSError("board unreadable")
    readers["board_reader"] = bad_board
    rep = pl.scan(tmp_path, "alpha", now=NOW, stale_hours_override=THR, **readers)
    fox = rep["peers"][0]
    assert fox["agent"] == "foxtrot"
    # diary + goals still read and frozen -> stalled; the board is recorded as unreadable
    assert fox["verdict"] == pl.V_STALLED
    assert fox["signals"]["board"]["readable"] is False


# ─────────────────────────────────────────────────────────────────────────────
# CLI contract (). The zakbox1 host sweeper has no Mind checkout and
# cannot import this module, so it runs THIS CLI inside a container over
# `lxc exec` and parses the JSON. That makes the contract below a CROSS-REPO
# dependency: world/scripts/fleet-liveness-sweep.py's shard lane is defined over
# these keys, and its own tests mock `lxc`, so nothing else pins them.
# ─────────────────────────────────────────────────────────────────────────────

def test_cli_stamps_the_alerting_flag_on_every_peer(monkeypatch, capsys):
    """`alerting` is the whole point: the host must not re-derive ALERTING."""
    def fake_scan(world, self_agent, **kw):
        return {"self": self_agent, "peers": [
            {"agent": "a", "verdict": pl.V_STALLED},
            {"agent": "b", "verdict": pl.V_SLOW},
            {"agent": "c", "verdict": pl.V_UNKNOWN},
            {"agent": "d", "verdict": pl.V_ALIVE},
        ], "blind": False, "roster_provenance": pl.PROV_AUTHORITATIVE}

    monkeypatch.setattr(pl, "scan", fake_scan)
    assert pl.main(["--world", "/tmp/w"]) == 0
    report = json.loads(capsys.readouterr().out)
    got = {p["agent"]: p["alerting"] for p in report["peers"]}
    # Exactly the ALERTING set — only `stalled` pages.
    assert got == {"a": True, "b": False, "c": False, "d": False}


def test_cli_emits_a_report_and_exits_zero_even_when_blind(monkeypatch, capsys):
    """The verdict is in the payload, never in the exit code (guard-1150).

    The sweeper's reader-fallthrough keys on `blind`, and treats EMPTY STDOUT as
    the only 'this told me nothing' signal — so a blind scan must still print.
    """
    monkeypatch.setattr(pl, "scan", lambda *a, **k: {
        "peers": [], "blind": True, "blind_cause": "rows read failed"})
    assert pl.main(["--world", "/tmp/w"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["blind"] is True and report["blind_cause"]


def test_cli_defaults_to_classifying_the_whole_roster(monkeypatch, capsys):
    """`--self` defaults to '' — the sweeper is nobody's peer and subtracts its
    own container lane afterwards, so it needs every agent, not a peer set."""
    seen = {}

    def fake_scan(world, self_agent, **kw):
        seen["self_agent"] = self_agent
        seen["stale_hours"] = kw.get("stale_hours_override")
        return {"peers": [], "blind": False}

    monkeypatch.setattr(pl, "scan", fake_scan)
    pl.main(["--world", "/tmp/w"])
    assert seen["self_agent"] == ""
    assert seen["stale_hours"] is None
    pl.main(["--world", "/tmp/w", "--stale-hours", "0.01"])
    assert seen["stale_hours"] == 0.01
    capsys.readouterr()


def test_cli_says_blind_rather_than_raising_when_the_world_is_unresolvable(
        monkeypatch, capsys):
    """A path failure must degrade to a readable blind report, never a traceback
    — the sweeper parses stdout, and a traceback goes to stderr as empty JSON."""
    monkeypatch.setitem(sys.modules, "_paths", None)   # import raises ModuleNotFoundError
    assert pl.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["blind"] is True
    assert "world dir unresolved" in report["blind_cause"]


def test_zero_stale_hours_is_honoured_not_swallowed_by_truthiness(monkeypatch):
    """`--stale-hours 0` must mean 0, not the 3.0 default.

    A falsy test here inverts a forced-stale positive control: the operator asks
    for "treat everything as stale" and gets back a calm "nothing is stalled",
    which reads as a working detector. Found by fresh-eyes on g-115-9030.
    """
    seen = {}

    def rows(_world):
        return ({"a": {"last_active": "2026-09-05T07:59:00", "session_ended": False}},
                {"a": pl.PROV_AUTHORITATIVE}, pl.PROV_AUTHORITATIVE)

    def spy(*a, **kw):
        seen["thr"] = kw["stale_hours"]
        return {"agent": a[0], "verdict": pl.V_ALIVE, "corroboration_needed": False}

    monkeypatch.setattr(pl, "classify_peer", spy)
    rep = pl.scan(Path("/tmp/w"), "", now=NOW, stale_hours_override=0, rows_reader=rows)
    assert rep["stale_hours"] == 0.0
    assert seen["thr"] == 0.0
    # Positive control: None still falls through to the module default.
    pl.scan(Path("/tmp/w"), "", now=NOW, stale_hours_override=None, rows_reader=rows)
    assert seen["thr"] == pl.stale_hours() != 0.0
