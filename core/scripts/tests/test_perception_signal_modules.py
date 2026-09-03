"""Tests for perception_signal_modules — the mind-signal pack (M-11, ).

The load-bearing test in this file is `test_perceive_never_deletes_the_signal`.
Everything else pins behaviour; that one pins a SAFETY property. Wake signals
are one-shot and `interruptible-sleep.sh` is their consumer, so a module that
deleted would race it and swallow wakes silently — a sleeping loop that never
wakes, with no error raised anywhere and nothing in a log to find it by.

The deletion-is-not-an-event pair (`test_disappearance_is_not_an_event` +
`test_touch_after_consumption_is_a_fresh_arrival`) is the other half: together
they say the module tolerates the consumer's delete without either reporting it
as a signal or going deaf to the next one.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _paths  # noqa: E402
import perception_signal_modules as psm  # noqa: E402
from perception_bus import CadenceType, PerceptionBus, ProvenanceTag  # noqa: E402


@pytest.fixture
def signal_dir(tmp_path, monkeypatch):
    """Point agent_state_dir at a tmp session dir.

    Patches `_paths.agent_state_dir` rather than an env var because that is the
    exact symbol `signal_path()` imports, so the test exercises the real
    resolution path instead of a parallel one it invented (guard-920: replicate
    the production shape, not the contract-ideal one).
    """
    session = tmp_path / "agents" / "testagent" / "session"
    session.mkdir(parents=True)
    monkeypatch.setattr(_paths, "agent_state_dir", lambda name: session)
    return session


def _touch(path, mtime=None):
    path.touch()
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# --- registration + shape ---------------------------------------------------

def test_register_signal_modules_registers_both():
    bus = PerceptionBus()
    modules = psm.register_signal_modules(bus, agent="testagent")
    assert [m.module_id for m in modules] == ["board-signal", "email-signal"]
    # Registered means emit() reaches them; an unregistered id raises KeyError.
    for mid in ("board-signal", "email-signal"):
        bus.emit(mid)


def test_both_modules_are_event_driven():
    """The convention's S6.2 table assigns EVENT_DRIVEN to both.

    Not cosmetic: PerceptionBus.emit() REFUSES a non-EVENT_DRIVEN module, so a
    cadence drift here breaks the delivery path rather than merely mislabelling.
    """
    for cls in (psm.BoardSignalModule, psm.EmailSignalModule):
        assert cls.cadence is CadenceType.EVENT_DRIVEN


def test_wake_classes_match_the_convention():
    """email-received is a BLOCKER, board-activity INFORMATIONAL.

    Pinned because the two differ in whether they break a quiescence-approved
    sleep, and swapping them is invisible until a user's email is ignored.
    """
    assert psm.EmailSignalModule.wake_class == psm.WAKE_BLOCKER
    assert psm.BoardSignalModule.wake_class == psm.WAKE_INFORMATIONAL


# --- arrival detection ------------------------------------------------------

def test_arrival_produces_a_percept(signal_dir):
    module = psm.BoardSignalModule(agent="testagent")
    module.start()
    _touch(signal_dir / "board-activity")
    percept = module.perceive(trigger=None)
    assert percept is not None
    assert percept.source_module == "board-signal"
    assert percept.source_pack == "mind-signal"
    assert percept.provenance is ProvenanceTag.DIRECT
    assert percept.payload["signal"] == "board-activity"
    assert percept.payload["agent"] == "testagent"
    assert percept.payload["wake_class"] == psm.WAKE_INFORMATIONAL


def test_unchanged_mtime_reports_nothing(signal_dir):
    module = psm.EmailSignalModule(agent="testagent")
    module.start()
    _touch(signal_dir / "email-received")
    assert module.perceive(trigger=None) is not None
    assert module.perceive(trigger=None) is None


def test_advancing_mtime_is_a_new_arrival(signal_dir):
    """A re-touch while the file still exists is a second event.

    board.py touches on EVERY post, so two posts before the consumer runs must
    not collapse into one — the file's existence is not the signal, its
    freshness is.
    """
    module = psm.BoardSignalModule(agent="testagent")
    module.start()
    path = signal_dir / "board-activity"
    _touch(path, mtime=1_000_000)
    assert module.perceive(trigger=None) is not None
    _touch(path, mtime=1_000_050)
    second = module.perceive(trigger=None)
    assert second is not None
    assert second.payload["previous_mtime"] == 1_000_000


def test_start_baselines_a_preexisting_file(signal_dir):
    """A signal left on disk before start() is not replayed.

    It is not an observation this process made, and emitting it would hand the
    cognition layer a wake it cannot date.
    """
    _touch(signal_dir / "board-activity")
    module = psm.BoardSignalModule(agent="testagent")
    module.start()
    assert module.perceive(trigger=None) is None


# --- the consumer contract (the load-bearing pair) --------------------------

def test_perceive_never_deletes_the_signal(signal_dir):
    """SAFETY: interruptible-sleep.sh is the consumer; this module only stats.

    If this ever fails, a perception module is racing the sleep loop for a
    one-shot file and wakes are being swallowed with no error anywhere.
    """
    path = _touch(signal_dir / "email-received")
    module = psm.EmailSignalModule(agent="testagent")
    module.start()
    _touch(path, mtime=2_000_000)
    assert module.perceive(trigger=None) is not None
    assert path.exists(), "perceive() consumed a signal owned by interruptible-sleep.sh"
    # Still there after a no-op perceive as well.
    module.perceive(trigger=None)
    assert path.exists()


def test_disappearance_is_not_an_event(signal_dir):
    """The consumer deleting the file is not a signal.

    Reporting it would emit a percept for the ABSENCE of news — and, worse,
    one indistinguishable from a real arrival at the payload level.
    """
    path = _touch(signal_dir / "board-activity", mtime=1_000_000)
    module = psm.BoardSignalModule(agent="testagent")
    module.start()
    assert module.perceive(trigger=None) is None  # baselined at start
    path.unlink()
    assert module.perceive(trigger=None) is None


def test_touch_after_consumption_is_a_fresh_arrival(signal_dir):
    """Deletion must not make the module deaf to the next signal.

    The failure this guards is subtle: if the baseline were kept across a
    delete, a recreated file whose mtime happened to match the old one would
    be read as 'unchanged' and the wake lost.
    """
    path = _touch(signal_dir / "board-activity", mtime=1_000_000)
    module = psm.BoardSignalModule(agent="testagent")
    module.start()
    path.unlink()
    module.perceive(trigger=None)          # observes the disappearance
    _touch(path, mtime=1_000_000)          # SAME mtime as the consumed one
    assert module.perceive(trigger=None) is not None


# --- degraded resolution ----------------------------------------------------

def test_unresolvable_agent_is_quiet_not_fatal(monkeypatch):
    """No agent bound -> no percepts, no exception.

    A perception module that raised on an unbound box would take down every
    other module in the pack via the bus's error path.
    """
    monkeypatch.delenv("MIND_AGENT", raising=False)
    module = psm.BoardSignalModule()
    module.start()
    assert module.signal_path() is None
    assert module.perceive(trigger=None) is None


def test_explicit_agent_beats_env(monkeypatch):
    monkeypatch.setenv("MIND_AGENT", "from-env")
    assert psm.BoardSignalModule(agent="explicit").agent == "explicit"
    assert psm.BoardSignalModule().agent == "from-env"


# --- end to end through the bus ---------------------------------------------

def test_percept_reaches_the_bus_queue(signal_dir):
    """The M-11 verification: a live signal produces an Observation end to end."""
    bus = PerceptionBus()
    board, email = psm.register_signal_modules(bus, agent="testagent")
    bus.start()

    _touch(signal_dir / "board-activity", mtime=3_000_000)
    assert bus.emit("board-signal") is True
    assert bus.pending_event_count("board-signal") == 1

    drained = bus.drain("board-signal")
    assert len(drained) == 1
    assert drained[0].payload["signal"] == "board-activity"
    # Draining the bus must not have touched the file either.
    assert (signal_dir / "board-activity").exists()

    # The email module saw nothing, and reports nothing.
    assert bus.emit("email-signal") is False
