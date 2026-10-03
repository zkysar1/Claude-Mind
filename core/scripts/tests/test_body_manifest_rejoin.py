"""Park rejoin (): a parked worker Body rejoins when its reducer's claim
turns live again, not at its next park-orbit wakeup (up to 4h later).

`rejoin-wait` runs in the background from a reducer-gone first park. It reads the
claim with the canonical `runner-claim.sh status`, judges each read with the
Phase 0.5 poll's own decide() on the poll's own state (read, never written), and
writes `park-rejoin.json` once a LIVE claim's heartbeat is later than the park.
`park-due` answers DUE on that signal. Controls: a user-stop park and a
still-stale claim stay parked, and a takeover onto another box keeps waiting.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parent.parent
sys.path.insert(0, str(SCRIPTS))

import worker_reducer_liveness as wrl  # noqa: E402

SID = "worker-sid-0001"
AGENT = "alpha"
FMT = "%Y-%m-%dT%H:%M:%S"


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


def _session(pr: Path) -> Path:
    return bm._agent_paths(AGENT, SID, pr)[1]


def _stamp(delta_s: float = 0) -> str:
    return (dt.datetime.now() + dt.timedelta(seconds=delta_s)).strftime(FMT)


def _set(pr: Path, **fields) -> None:
    """Rewrite manifest fields through the module's own renderer; None removes one."""
    data = bm.read_manifest(SID, AGENT, project_root=pr)
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (_session(pr) / "body-manifest.yaml").write_text(bm._render_manifest(data), encoding="utf-8")


def _signal(pr: Path, at: str) -> None:
    (_session(pr) / bm.REJOIN_SIGNAL_FILENAME).write_text(json.dumps({"at": at}) + "\n",
                                                          encoding="utf-8")


def _signal_exists(pr: Path) -> bool:
    return (_session(pr) / bm.REJOIN_SIGNAL_FILENAME).exists()


def _poll_state(pr: Path, **state) -> Path:
    path = wrl._state_path(bm._agent_paths(AGENT, SID, pr)[0], SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


# runner-claim.sh's LIVE and STALE lines in its own format (guard-920: the
# production shape; test_the_heartbeat_clause_is_what_the_emitter_prints joins
# the LIVE one to the emitter's source).
def _live(machine="cc-02", age=30, fp="1f4c0a9b2e6d8035"):
    return (f"[runner-claim] status: LIVE (backend=own-cloud) — '{AGENT}' is RUNNING on "
            f"'{machine}', heartbeat {age}s old (threshold 3900s), token-fp {fp}\n")


def _stale(machine="cc-02", age=5000):
    return (f"[runner-claim] status: STALE (backend=own-cloud) — '{AGENT}' claim on "
            f"'{machine}' is RUNNING but its heartbeat is {age}s old (~{age // 60}m), past "
            f"the 3900s threshold. A /start would reclaim this as a crashed runner; treat "
            f"it as NOT live.\n")


class _Done(Exception):
    """Raised by a test sleep to end a wait that has no designed exit yet."""


def _sleeper(limit, calls):
    def sleep(seconds):
        calls.append(seconds)
        if len(calls) > limit:
            raise _Done()
    return sleep


def _prober(*reads):
    """Replay claim reads in order; the last one repeats."""
    calls = []

    def probe():
        calls.append(len(calls))
        return reads[min(len(calls), len(reads)) - 1]
    probe.calls = calls
    return probe


# ── park-due reads the signal ────────────────────────────────────────────────

def test_a_signal_after_the_latest_park_makes_the_park_due(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False, "control: the orbit alone is not due"
    _signal(pr, _stamp())
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)


def test_a_signal_older_than_the_latest_park_does_not(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    _signal(pr, _stamp(-900))
    due, remaining = bm.park_due(SID, AGENT, project_root=pr)
    assert due is False and remaining > 0


@pytest.mark.parametrize("body", ["", "not json", "[1, 2]", '{"at": "yesterday"}',
                                  '{"at": null}', "{}"])
def test_an_unreadable_signal_falls_back_to_the_schedule(tmp_path, body):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    (_session(pr) / bm.REJOIN_SIGNAL_FILENAME).write_text(body, encoding="utf-8")
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False


def test_a_re_park_retires_the_signal(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    _signal(pr, _stamp(-5))
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)
    assert bm.park_body(SID, AGENT, project_root=pr) == "already-parked"
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False


def test_a_manifest_without_last_parked_at_measures_from_parked_at(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, parked_at=_stamp(-600), last_parked_at=None)
    _signal(pr, _stamp())
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)
    _signal(pr, _stamp(-900))
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False


# ── rejoin_verdict: one claim read ───────────────────────────────────────────

def _verdict(output, rc=0, state=None, park_offset=-600):
    return bm.rejoin_verdict(rc, output, state if state is not None else {},
                             _stamp(park_offset))


def test_verdict_fires_on_a_live_claim_renewed_after_the_park():
    fire, reason, machine = _verdict(_live(age=30), state={"expected_machine": "cc-02"})
    assert fire is True and machine == "cc-02", reason


def test_verdict_waits_on_a_live_claim_whose_heartbeat_predates_the_park():
    fire, reason, _ = _verdict(_live(age=900))
    assert fire is False and "not later than the park" in reason


def test_verdict_waits_on_a_stale_claim():
    assert _verdict(_stale(), rc=4)[0] is False


def test_verdict_waits_on_rc0_without_a_live_line():
    assert _verdict("", rc=0)[0] is False
    assert _verdict("[runner-claim] status: something new\n", rc=0)[0] is False


def test_verdict_waits_on_a_takeover_onto_another_box():
    fire, reason, machine = _verdict(_live(machine="cc-07"), state={"expected_machine": "cc-02"})
    assert fire is False and machine == "cc-07" and "takeover" in reason


def test_verdict_fires_on_a_same_box_restart_seen_while_parked():
    # : a re-minted token seen while parked is CONTINUE, not a wind-down.
    state = {"expected_machine": "cc-02", "expected_token_fp": "aaaaaaaaaaaaaaaa"}
    fire, reason, _ = _verdict(_live(fp="bbbbbbbbbbbbbbbb"), state=state)
    assert fire is True, reason


def test_verdict_waits_when_the_heartbeat_age_is_unreadable():
    fire, reason, _ = _verdict(_live().replace("heartbeat 30s old", "heartbeat recently"))
    assert fire is False and "unreadable" in reason


def test_verdict_reads_the_age_off_the_live_line_only():
    line = (_live().replace("heartbeat 30s old", "heartbeat recently")
            + "[warn] peer heartbeat 5s old\n")
    assert _verdict(line)[0] is False


def test_verdict_fires_when_the_reducer_clock_runs_ahead():
    assert _verdict(_live(age=-3))[0] is True


def test_verdict_survives_a_malformed_poll_state():
    assert bm.rejoin_verdict(0, _live(), [1, 2], _stamp(-600))[0] is True
    assert _verdict(_live(), state={"consecutive_errors": "x"})[0] is True


def test_verdict_waits_on_an_unreadable_park_stamp():
    fire, reason, _ = bm.rejoin_verdict(0, _live(), {}, "not-a-stamp")
    assert fire is False and "park stamp unreadable" in reason


# ── rejoin_wait: the loop ────────────────────────────────────────────────────

def test_the_waiter_signals_a_returned_reducer_and_park_due_answers_due(tmp_path):
    """POSITIVE CONTROL: stale, stale, then LIVE with a beat after the park."""
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    _poll_state(pr, consecutive_errors=0, expected_machine="cc-02",
                expected_token_fp="1f4c0a9b2e6d8035")
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False
    probe = _prober((4, _stale()), (4, _stale()), (0, _live(age=20)))
    sleeps = []
    action, detail = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe,
                                    sleep=_sleeper(10, sleeps))
    assert action == "rejoin", detail
    assert len(probe.calls) == 3 and sleeps == [bm.REJOIN_POLL_SECONDS] * 3
    signal = json.loads((_session(pr) / bm.REJOIN_SIGNAL_FILENAME).read_text(encoding="utf-8"))
    assert set(signal) == {"at", "parked_at", "machine", "reason"}
    assert signal["machine"] == "cc-02"
    assert signal["parked_at"] == bm.read_manifest(SID, AGENT, project_root=pr)["parked_at"]
    assert bm.park_due(SID, AGENT, project_root=pr) == (True, 0)


def test_a_still_stale_claim_stays_parked(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    probe = _prober((4, _stale()))
    with pytest.raises(_Done):
        bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe, sleep=_sleeper(5, []))
    assert len(probe.calls) == 5
    assert not _signal_exists(pr)
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False


def test_a_user_stop_park_stays_parked_with_the_claim_live(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    (_session(pr) / "stop-requested").write_text("", encoding="utf-8")
    probe = _prober((0, _live(age=5)))
    action, _ = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe,
                               sleep=_sleeper(10, []))
    assert action == "stop" and probe.calls == []
    assert not _signal_exists(pr)
    assert bm.park_due(SID, AGENT, project_root=pr)[0] is False


def test_a_stop_armed_mid_wait_ends_it_with_no_signal(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    calls = []

    def sleep(_seconds):
        calls.append(_seconds)
        if len(calls) == 3:
            (_session(pr) / "stop-requested").write_text("", encoding="utf-8")
    probe = _prober((4, _stale()), (4, _stale()), (0, _live(age=5)))
    action, _ = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe, sleep=sleep)
    assert action == "stop" and len(probe.calls) == 2
    assert not _signal_exists(pr)


def test_a_takeover_onto_another_box_keeps_waiting(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    _poll_state(pr, consecutive_errors=0, expected_machine="cc-02")
    probe = _prober((0, _live(machine="cc-07", age=5)))
    with pytest.raises(_Done):
        bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe, sleep=_sleeper(3, []))
    assert len(probe.calls) == 3 and not _signal_exists(pr)


def test_a_body_not_parked_at_launch_exits_without_waiting(tmp_path):
    pr = _project(tmp_path)
    bm.write_manifest(SID, AGENT, project_root=pr, role="worker")
    sleeps = []
    probe = _prober((0, _live()))
    action, _ = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe,
                               sleep=_sleeper(5, sleeps))
    assert action == "gone" and sleeps == [] and probe.calls == []


def _acting_sleeper(act, limit=5):
    """A sleep that changes the world, bounded so a regression fails, not hangs."""
    calls = []

    def sleep(_seconds):
        calls.append(_seconds)
        if len(calls) > limit:
            raise _Done()
        act()
    return sleep


def test_a_resume_mid_wait_ends_it(tmp_path):
    pr = _parked(tmp_path)
    probe = _prober((4, _stale()))
    action, detail = bm.rejoin_wait(
        SID, AGENT, project_root=pr, probe=probe,
        sleep=_acting_sleeper(lambda: bm.resume_body(SID, AGENT, project_root=pr)))
    assert action == "gone" and "no longer parked" in detail and probe.calls == []


def test_a_new_park_mid_wait_ends_it(tmp_path):
    pr = _parked(tmp_path)
    probe = _prober((4, _stale()))
    action, detail = bm.rejoin_wait(
        SID, AGENT, project_root=pr, probe=probe,
        sleep=_acting_sleeper(lambda: _set(pr, parked_at=_stamp(5))))
    assert action == "gone" and "new park" in detail and probe.calls == []


def test_the_park_cap_ends_it(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, parked_at=_stamp(-61 * 3600))
    probe = _prober((0, _live(age=5)))
    action, _ = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe,
                               sleep=_sleeper(5, []))
    assert action == "expired" and probe.calls == [] and not _signal_exists(pr)


def test_the_waiter_never_writes_the_poll_state(tmp_path):
    pr = _parked(tmp_path)
    _set(pr, last_parked_at=_stamp(-600))
    state = _poll_state(pr, consecutive_errors=2, expected_machine="cc-02",
                        expected_token_fp="aaaaaaaaaaaaaaaa")
    before = state.read_bytes()
    probe = _prober((1, "daemon unreachable"), (0, _live(age=5, fp="bbbbbbbbbbbbbbbb")))
    action, detail = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=probe,
                                    sleep=_sleeper(5, []))
    assert action == "rejoin", detail
    assert state.read_bytes() == before
    state.unlink()
    _set(pr, last_parked_at=_stamp(-600))
    (_session(pr) / bm.REJOIN_SIGNAL_FILENAME).unlink()
    action, _ = bm.rejoin_wait(SID, AGENT, project_root=pr, probe=_prober((0, _live(age=5))),
                               sleep=_sleeper(5, []))
    assert action == "rejoin" and not state.exists()


# ── the canonical probe ──────────────────────────────────────────────────────

def test_the_waiter_reads_the_claim_with_the_poll_argv(tmp_path, monkeypatch):
    """guard-920: the waiter's read must be the call the Phase 0.5 poll makes."""
    pr = _parked(tmp_path)
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs.get("timeout")))
        return subprocess.CompletedProcess(argv, 0, _live(), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, output = bm._claim_status(AGENT)
    wrl.poll(AGENT, bm._agent_paths(AGENT, SID, pr)[0], SID, SCRIPTS)
    (waiter_argv, timeout), (poll_argv, _) = seen
    assert waiter_argv == poll_argv
    assert waiter_argv[-4:] == [(SCRIPTS / "runner-claim.sh").as_posix(), "status", "--agent", AGENT]
    assert rc == 0 and output == _live() and timeout == bm.REJOIN_PROBE_TIMEOUT_SECONDS


def test_a_hung_claim_read_is_not_live(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, output = bm._claim_status(AGENT)
    assert rc is None and "claim read failed" in output
    assert bm.rejoin_verdict(rc, output, {}, _stamp(-600))[0] is False


def test_the_heartbeat_clause_is_what_the_emitter_prints():
    """The waiter keys on runner-claim.sh's LIVE line; pin the emitter's source."""
    text = (SCRIPTS / "runner-claim.sh").read_text(encoding="utf-8")
    i = text.find("status: LIVE")
    assert i >= 0, "runner-claim.sh no longer has a 'status: LIVE' branch"
    live_stmt = text[i:text.find("sys.exit(", i)]
    assert wrl.LIVE_MARKER in live_stmt
    assert "heartbeat {age}s old" in live_stmt, (
        "runner-claim.sh's LIVE line no longer prints 'heartbeat {age}s old', so the "
        "rejoin waiter can never see a returned reducer. Re-derive _HEARTBEAT_AGE_RE "
        f"against the new format.\nLIVE statement now reads:\n{live_stmt[:240]}")
    assert bm._parse_heartbeat_age(_live(age=42)) == 42


# ── CLI and launch site ──────────────────────────────────────────────────────

def test_rejoin_wait_cli_contract(monkeypatch, capsys):
    """rc 0 = signal written; 1 = stood down. Facts only, never a skill request (rb-12040)."""
    monkeypatch.setattr(bm, "rejoin_wait", lambda sid, agent: ("rejoin", "claim LIVE on 'cc-02'"))
    assert bm.main(["rejoin-wait", "--sid", SID, "--agent", AGENT]) == 0
    out = capsys.readouterr().out
    last = out.strip().splitlines()[-1]
    assert last.startswith("rejoin: ") and "park-due now answers due" in last
    assert "/worker-loop" not in out
    for action in ("stop", "gone", "expired"):
        monkeypatch.setattr(bm, "rejoin_wait", lambda sid, agent, a=action: (a, "why"))
        assert bm.main(["rejoin-wait", "--sid", SID, "--agent", AGENT]) == 1
        assert capsys.readouterr().out.strip().splitlines()[-1] == f"{action}: why"


def test_the_waiter_launches_from_the_reducer_gone_park_only():
    """The manifest records no park reason, so the launch site is the discriminator:
    a supply-gap park sits beside a live reducer, and a stopped Body stays stopped."""
    skill = (ROOT / ".claude/skills/worker-loop/SKILL.md").read_text(encoding="utf-8")
    assert skill.count("rejoin-wait") == 1
    at = skill.find("rejoin-wait")
    assert skill.find("THE PARK SEQUENCE") < at < skill.find("# Phase 1 — SELECT")
    for path in (".claude/skills/stop/SKILL.md", "core/scripts/loop-exhaustion-fence.sh"):
        assert "rejoin-wait" not in (ROOT / path).read_text(encoding="utf-8")
