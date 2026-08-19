""" — the mirror-wedge probe must CLOSE the goal it filed.

MirrorWedgeProbe filed a box-scoped Investigate goal when the own-cloud mirror
wedged, and nothing ever closed it. Measured 2026-08-11 (echo, hostname cc-03,
uname -r 6.8.0-137-generic, own-cloud): three such goals were open fleet-wide at
7, 14 and 17 days, and this box's own watchdog log shows the condition it
described had cleared 4h after filing —

    2026-08-04T03:50:37  mirror_wedged        (filed g-115-4868)
    2026-08-04T07:50:52  mirror_wedge_cleared (after_ticks 5)

Detection was never lost — the critical `mirror_wedged` Event still emits on the
dedup path — so the harm is narrower than "the detector is disarmed": the queue
carries a HIGH goal whose description is a snapshot of the OLD episode's file
list and sweep counts, and a later episode silently reuses it, handing whoever
picks it up evidence from a different incident.

These tests pin the guards, because the close mutates shared queue state from a
background probe and every guard here is what bounds that.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pointer_freshness as pf  # noqa: E402


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "watchdog_under_test", SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wd():
    return _load_watchdog()


# ---------------------------------------------------------------------------
# pointer_freshness.open_goal_records — the id-returning half
# ---------------------------------------------------------------------------

# An OPAQUE key for the pointer_freshness tests below, which are format-agnostic
# — they only need the same string on both sides. This is deliberately NOT the
# live signal format (that gained a box component in  and is built by
# MirrorWedgeProbe._origin_signal); do not read it as the current shape.
SIGNAL = "investigate:mirror-wedge-detected-echo"


def _write_aspirations(root: Path, goals: list) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "aspirations.jsonl"
    p.write_text(json.dumps({"id": "asp-115", "goals": goals}) + "\n",
                 encoding="utf-8")
    return p


def test_records_return_ids_and_source(tmp_path):
    """A filer that cannot NAME the goal it deduped against cannot close that
    goal when the condition clears — which is the whole defect. `_source` must
    ride along because every downstream aspirations-*.sh call needs --source."""
    _write_aspirations(tmp_path / "world", [
        {"id": "g-1", "origin_signal": SIGNAL, "status": "pending"},
    ])
    got = pf.open_goal_records(SIGNAL, tmp_path / "world", None)
    assert [g["id"] for g in got] == ["g-1"]
    assert got[0]["_source"] == "world"


def test_records_span_both_queues_with_correct_source(tmp_path):
    _write_aspirations(tmp_path / "world", [
        {"id": "g-w", "origin_signal": SIGNAL, "status": "pending"}])
    _write_aspirations(tmp_path / "agent", [
        {"id": "g-a", "origin_signal": SIGNAL, "status": "in-progress"}])
    got = pf.open_goal_records(SIGNAL, tmp_path / "world", tmp_path / "agent")
    assert {g["id"]: g["_source"] for g in got} == {"g-w": "world", "g-a": "agent"}


def test_records_exclude_terminal_goals(tmp_path):
    """Only OPEN_STATUSES qualify — a completed/skipped goal is not a live
    dedup key and must not be re-closed."""
    _write_aspirations(tmp_path / "world", [
        {"id": "g-done", "origin_signal": SIGNAL, "status": "completed"},
        {"id": "g-skip", "origin_signal": SIGNAL, "status": "skipped"},
    ])
    assert pf.open_goal_records(SIGNAL, tmp_path / "world", None) == []


def test_exists_still_agrees_with_records(tmp_path):
    """open_goal_exists delegates now; the two must never disagree, because the
    filing path reads one and the closing path reads the other."""
    _write_aspirations(tmp_path / "world", [
        {"id": "g-1", "origin_signal": SIGNAL, "status": "blocked"}])
    for w, a in ((tmp_path / "world", None), (None, None)):
        assert (pf.open_goal_exists(SIGNAL, w, a)
                is bool(pf.open_goal_records(SIGNAL, w, a)))


def test_records_are_fail_open_on_bad_json(tmp_path):
    """A corrupt line must not raise — one broken pointer cannot kill a tick."""
    root = tmp_path / "world"
    root.mkdir(parents=True)
    (root / "aspirations.jsonl").write_text(
        '{"origin_signal": NOT-JSON\n'
        + json.dumps({"id": "asp-115", "goals": [
            {"id": "g-ok", "origin_signal": SIGNAL, "status": "pending"}]}) + "\n",
        encoding="utf-8")
    assert [g["id"] for g in pf.open_goal_records(SIGNAL, root, None)] == ["g-ok"]


# ---------------------------------------------------------------------------
# MirrorWedgeProbe._close_wedge_goal — the guards
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, tmp_path):
        self.agent_name = "echo"
        self.agent_dir = tmp_path / "agent"
        self.project_root_path = tmp_path


def _probe(wd, tmp_path, monkeypatch, goals, runner):
    """Build a MirrorWedgeProbe whose queue reads `goals` and whose subprocess
    calls are captured by `runner`."""
    monkeypatch.setattr(pf, "open_goal_records",
                        lambda sig, w, a: [dict(g, _source="world") for g in goals])
    monkeypatch.setitem(sys.modules, "_paths",
                        types.SimpleNamespace(WORLD_DIR=str(tmp_path / "world")))
    monkeypatch.setitem(sys.modules, "_runtime_bash",
                        types.SimpleNamespace(BASH="bash"))
    monkeypatch.setattr(wd.subprocess, "run", runner)
    p = wd.MirrorWedgeProbe(_Ctx(tmp_path))
    p.consecutive_wedged = 5
    return p


def _ok(*a, **k):
    return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")


def test_closes_a_pending_unclaimed_goal(wd, tmp_path, monkeypatch):
    calls = []

    def runner(argv, **k):
        calls.append(argv)
        return _ok()

    p = _probe(wd, tmp_path, monkeypatch,
               [{"id": "g-115-4868", "status": "pending"}], runner)
    out = p._close_wedge_goal()

    assert out["closed"] == ["g-115-4868"]
    fields = [(c[2], c[3]) for c in calls]
    assert [f for f, _ in fields] == ["g-115-4868", "g-115-4868"]
    assert [v for _, v in fields] == ["outcome_note", "status"], \
        "outcome_note MUST be written before status: if the second write fails "\
        "the goal stays open WITH an explanation, never closed without one"
    assert calls[-1][4] == "skipped", \
        "never `completed` — no investigation happened, and claiming completion "\
        "buys a completion-rate point with a false claim (guard-1213/guard-2541)"


def test_refuses_to_close_a_claimed_goal(wd, tmp_path, monkeypatch):
    """guard-1007: never mutate a partner's claimed goal. A partner was live on
    one of these three goals at the time this fix was written."""
    calls = []
    p = _probe(wd, tmp_path, monkeypatch,
               [{"id": "g-115-3711", "status": "pending", "claimed_by": "zeta"}],
               lambda argv, **k: (calls.append(argv), _ok())[1])
    out = p._close_wedge_goal()
    assert out["closed"] == []
    assert calls == [], "a claimed goal must not be touched at all"
    assert "g-115-3711" in out["detail"] and "claimed" in out["detail"]


def test_refuses_to_close_an_in_progress_goal(wd, tmp_path, monkeypatch):
    calls = []
    p = _probe(wd, tmp_path, monkeypatch,
               [{"id": "g-x", "status": "in-progress"}],
               lambda argv, **k: (calls.append(argv), _ok())[1])
    out = p._close_wedge_goal()
    assert out["closed"] == [] and calls == []


def test_no_open_goal_makes_no_subprocess_call(wd, tmp_path, monkeypatch):
    """The close runs on EVERY healthy tick (it is deliberately not gated on
    self.fired, whose state is box-local and ephemeral). The common case must
    therefore cost one local read and nothing else."""
    calls = []
    p = _probe(wd, tmp_path, monkeypatch, [],
               lambda argv, **k: (calls.append(argv), _ok())[1])
    out = p._close_wedge_goal()
    assert out == {"attempted": False, "detail": None}
    assert calls == []


def test_close_failure_is_fail_open(wd, tmp_path, monkeypatch):
    """A failed close degrades to a stale goal — the status quo ante — and must
    never raise into the tick."""
    p = _probe(wd, tmp_path, monkeypatch, [{"id": "g-x", "status": "pending"}],
               lambda argv, **k: types.SimpleNamespace(
                   returncode=1, stdout="", stderr="daemon down"))
    out = p._close_wedge_goal()
    assert out["closed"] == [] and "g-x:close-failed" in out["detail"]


def test_exception_is_swallowed(wd, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("queue unreadable")
    monkeypatch.setattr(pf, "open_goal_records", boom)
    monkeypatch.setitem(sys.modules, "_paths",
                        types.SimpleNamespace(WORLD_DIR=str(tmp_path / "world")))
    p = wd.MirrorWedgeProbe(_Ctx(tmp_path))
    out = p._close_wedge_goal()  # must not raise
    assert out["closed"] == [] and out["detail"].startswith("error: RuntimeError")


# ---------------------------------------------------------------------------
# check() wiring
# ---------------------------------------------------------------------------

def _healthy_probe(wd, tmp_path, monkeypatch, closed_result):
    monkeypatch.setitem(sys.modules, "mirror_health",
                        types.SimpleNamespace(probe=lambda: {"verdict": "healthy"}))
    p = wd.MirrorWedgeProbe(_Ctx(tmp_path))
    monkeypatch.setattr(p, "_close_wedge_goal", lambda: closed_result)
    return p


def test_healthy_tick_attempts_close_even_when_not_fired(wd, tmp_path, monkeypatch):
    """THE self-healing property. `fired` lives in watchdog-prev-state.json,
    which is box-local and ephemeral; a reset would otherwise leave an already
    filed goal permanently unclosable — the same "filed, never closed" defect
    one level up."""
    seen = []
    p = _healthy_probe(wd, tmp_path, monkeypatch, {"attempted": True,
                                                   "closed": ["g-1"],
                                                   "held": [], "detail": "closed g-1"})
    monkeypatch.setattr(p, "_close_wedge_goal",
                        lambda: (seen.append(1), {"attempted": True,
                                                  "closed": ["g-1"], "held": [],
                                                  "detail": "closed g-1"})[1])
    p.fired = False
    events = p.check()
    assert seen == [1], "close must be attempted with fired=False"
    assert [e.event for e in events] == ["mirror_wedge_cleared"], \
        "a close that actually happened is worth an event even without fired"


def test_healthy_tick_stays_quiet_when_nothing_to_close(wd, tmp_path, monkeypatch):
    """No event spam on the overwhelmingly common healthy-and-clean tick."""
    p = _healthy_probe(wd, tmp_path, monkeypatch,
                       {"attempted": False, "detail": None})
    p.fired = False
    assert p.check() == []


def test_fired_episode_still_emits_and_resets(wd, tmp_path, monkeypatch):
    p = _healthy_probe(wd, tmp_path, monkeypatch,
                       {"attempted": False, "detail": None})
    p.fired = True
    p.consecutive_wedged = 5
    events = p.check()
    assert [e.event for e in events] == ["mirror_wedge_cleared"]
    assert events[0].payload["after_ticks"] == 5
    assert p.fired is False and p.consecutive_wedged == 0


def test_unknown_verdict_never_closes(wd, tmp_path, monkeypatch):
    """`unknown` means no live signal. Closing on it would retire a goal on the
    strength of a probe that measured nothing — the vacuous-zero trap (rb-245)."""
    seen = []
    monkeypatch.setitem(sys.modules, "mirror_health",
                        types.SimpleNamespace(probe=lambda: {"verdict": "unknown"}))
    p = wd.MirrorWedgeProbe(_Ctx(tmp_path))
    monkeypatch.setattr(p, "_close_wedge_goal",
                        lambda: (seen.append(1), {"attempted": False})[1])
    p.fired = True
    p.consecutive_wedged = 3
    assert p.check() == []
    assert seen == [], "unknown must hold state unchanged, not close"
    assert p.fired is True and p.consecutive_wedged == 3


def test_filed_goal_is_routed_to_the_filing_box(wd, tmp_path, monkeypatch):
    """ defect 2. Without intended_agent, routing falls through to
    `category`, which is box-agnostic: measured 2026-08-11, goals filed by
    bravo/alpha/echo/foxtrot ALL landed on zeta — who can only repair zeta's
    box, because every deciding signal is box-local. They also topped that
    agent's selector (HIGH + role_affinity), so they kept winning selection and
    kept not closing. That is why these aged 7-17 days rather than draining."""
    sent = {}

    def runner(argv, **k):
        sent["body"] = json.loads(k["input"])
        return types.SimpleNamespace(returncode=0, stdout='{"id": "g-new"}', stderr="")

    monkeypatch.setattr(pf, "open_goal_exists", lambda *a: False)
    monkeypatch.setitem(sys.modules, "_paths",
                        types.SimpleNamespace(WORLD_DIR=str(tmp_path / "world")))
    monkeypatch.setitem(sys.modules, "_runtime_bash",
                        types.SimpleNamespace(BASH="bash"))
    monkeypatch.setattr(wd.subprocess, "run", runner)

    p = wd.MirrorWedgeProbe(_Ctx(tmp_path))
    p.consecutive_wedged = 2
    out = p._file_wedge_goal({"wedged_count": 1, "files": {"a.md": 9}})

    assert out["filed"] is True
    assert sent["body"]["intended_agent"] == "echo", \
        "the goal must route to the box that filed it — the only box that can repair it"
    # Pin the INVARIANT (the filed key is the same key dedup and close use), not
    # a literal format. Derived from the probe's own builder so it cannot go
    # stale again: this line asserted a hardcoded agent-only string and had to
    # be updated when  added the box component. A goal filed under a
    # key the probe does not dedup or close against is unreachable by both.
    assert sent["body"]["origin_signal"] == p._origin_signal()
    # ...and it is BOX-scoped, which is the property  bought: an
    # agent-only key let one box's open goal suppress filing on every other box
    # the same agent occupies.
    assert wd._box_id() in sent["body"]["origin_signal"]


def test_outcome_note_does_not_assert_a_close_that_may_not_land(wd, tmp_path, monkeypatch):
    """Fresh-eyes finding (). outcome_note is written BEFORE status
    precisely so a partial failure leaves the goal open WITH an explanation. A
    note that asserts "Auto-closed by ..." is therefore a FALSE claim sitting on
    a still-open goal in exactly the case the write-order exists to protect. The
    note must state what is verifiably true at write time — the mirror-health
    observation — and let `status` carry the close."""
    calls = []
    p = _probe(wd, tmp_path, monkeypatch, [{"id": "g-x", "status": "pending"}],
               lambda argv, **k: (calls.append(argv), _ok())[1])
    p._close_wedge_goal()

    note = calls[0][4]
    assert "observed mirror-health" in note
    assert not note.lower().startswith("auto-closed"), \
        "the note must not assert a close that has not been written yet"
    assert "if the status still reads open" in note, \
        "a reader finding this note on an OPEN goal needs to know the write failed"
