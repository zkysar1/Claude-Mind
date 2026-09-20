""" — the SECOND unbounded thread-lock acquire, at
meta/skill_quality_score.py:270, and the `thread_locked()` helper that fixes it.

TWO DEFECTS, AND THE SECOND IS THE ONE THAT NEEDED ITS OWN GOAL:
  1. UNBOUNDED — a bare `thread_lock.acquire()` parks every later scorer
     behind one stuck holder forever (the defect g-115-10161 fixed inside
     file_locks.locked(); this site bypasses locked() entirely because it
     takes a custom `skill-quality.yaml.lock`).
  2. INVISIBLE — that acquire registered NO hold, so write_path_status(),
     the surface /v1/admin/health reports, could not see a wedge on this path
     at all. A reader checking /health got a clean verdict over a wedged write
     path. The invisibility half carries its OWN assertions here rather than
     riding on the bounding half, because a fix that bounded without
     registering would satisfy every test of defect 1 and leave the detector
     just as dead.

Paired sensitivity/specificity per guard-1660; the specificity half is what
keeps a write-path bound from becoming a write-REFUSING bug of its own.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from mind_api.src import file_locks

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Harness. Mirrors test_runtime_skill_quality_score.py's fakes deliberately —
# same shapes, so the two files agree about what a request IS.
# --------------------------------------------------------------------------

_FORGED = {"skills": {"tq-skill": {"forged_date": "2026-01-01T00:00:00",
                                   "quality_at_forge": "average"}}}


class _FakePaths:
    def __init__(self, meta, world, agent):
        self.meta, self.world, self.agent = meta, world, agent
        # Skill-name canonicalization reads the REAL .claude/skills off
        # project_root (see skill_quality_score._skills_dir); only the
        # meta/world/agent roots are redirected into tmp.
        self.project_root = REPO_ROOT


class _FakeCtx:
    def __init__(self, meta, world, agent, body=None):
        self.paths = _FakePaths(meta, world, agent)
        self.query = {}
        self.body = body
        self.headers = {}


def _setup(tmp_path):
    meta, world = tmp_path / "meta", tmp_path / "world"
    agent = tmp_path / "agents" / "alpha"
    for d in (meta, world, agent):
        d.mkdir(parents=True, exist_ok=True)
    (world / "forged-skills.yaml").write_text(
        yaml.safe_dump(_FORGED, sort_keys=False), encoding="utf-8")
    return meta, world, agent


def _body(**over):
    b = {"skill": "tq-skill", "goal": "g-1", "outcomes_met": 1,
         "outcomes_total": 3, "episode_chain_count": 2,
         "guardrail_violations": 3, "cost_awareness": "poor"}
    b.update(over)
    return json.dumps(b).encode("utf-8")


def _key(path):
    return str(path.resolve())


@pytest.fixture(autouse=True)
def _isolate_telemetry():
    """_HOLDS/_WAITING are module globals — a leak across tests would make a
    later test read an earlier test's wedge. Snapshot and restore."""
    with file_locks._TELEMETRY_LOCK:
        holds = dict(file_locks._HOLDS)
        waiting = dict(file_locks._WAITING)
    yield
    with file_locks._TELEMETRY_LOCK:
        file_locks._HOLDS.clear()
        file_locks._HOLDS.update(holds)
        file_locks._WAITING.clear()
        file_locks._WAITING.update(waiting)


@pytest.fixture
def fast_giveup(monkeypatch):
    """Drive the thresholds instead of sleeping past the real 240s."""
    monkeypatch.setattr(file_locks, "ACQUIRE_GIVEUP_SECONDS", 0.05)
    monkeypatch.setattr(file_locks, "ACQUIRE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(file_locks, "WEDGE_SECONDS", 0.05)


class _Holder:
    """Hold thread_locked(path) until released — the live wedge shape."""

    def __init__(self, path, max_hold=30):
        self.path, self._max_hold = path, max_hold
        self._release, self._held = threading.Event(), threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with file_locks.thread_locked(self.path):
            self._held.set()
            self._release.wait(self._max_hold)

    def __enter__(self):
        self._t.start()
        assert self._held.wait(10), "holder never acquired the lock"
        return self

    def __exit__(self, *exc):
        self._release.set()
        self._t.join(15)
        return False


# ─── SENSITIVITY: outcome 1 — the acquire is BOUNDED ────────────────────────

class TestBounded:
    def test_a_wedged_holder_is_refused_not_awaited_forever(self, tmp_path,
                                                            fast_giveup):
        p = tmp_path / "skill-quality.yaml"
        p.write_text("{}\n", encoding="utf-8")
        with _Holder(p):
            time.sleep(0.2)  # age the hold past the 0.05s give-up
            with pytest.raises(file_locks.WritePathWedged):
                with file_locks.thread_locked(p):
                    pytest.fail("acquired a lock the holder still owns")

    def test_the_refusal_names_the_path_age_and_the_remedy(self, tmp_path,
                                                           fast_giveup):
        p = tmp_path / "skill-quality.yaml"
        p.write_text("{}\n", encoding="utf-8")
        with _Holder(p):
            time.sleep(0.2)
            with pytest.raises(file_locks.WritePathWedged) as ei:
                with file_locks.thread_locked(p):
                    pass
        msg = str(ei.value)
        assert "skill-quality.yaml" in msg, "must name the wedged path"
        assert "held for" in msg, "must report the hold age"
        assert "--restart" in msg, "must name the remedy (guard-6895)"

    def test_wedged_refusal_is_a_TimeoutError_subclass(self):
        """The 503-mapping contract at the endpoint depends on this."""
        assert issubclass(file_locks.WritePathWedged, TimeoutError)


# ─── THE INVISIBILITY HALF: outcome 2 — the hold is REGISTERED ──────────────

class TestTheWedgeIsVisible:
    def test_thread_locked_registers_the_hold_so_health_can_see_it(
            self, tmp_path, fast_giveup):
        p = tmp_path / "skill-quality.yaml"
        p.write_text("{}\n", encoding="utf-8")
        assert file_locks.write_path_status()["wedged"] is False
        with _Holder(p):
            time.sleep(0.2)
            status = file_locks.write_path_status()
            assert status["wedged"] is True, (
                "a hold aged past WEDGE_SECONDS must report wedged — this is "
                "the /health surface the pre-fix site was invisible to")
            assert status["longest_hold_path"] == _key(p), (
                "the wedge must be attributed to the skill-quality path")

    def test_PRE_FIX_bare_acquire_is_INVISIBLE_to_write_path_status(
            self, tmp_path, fast_giveup):
        """Mutation proof of defect 2 (guard-385 shape).

        Reconstructs the EXACT pre-fix two lines and asserts the wedge
        surface cannot see them. This pins the DEFECT, not merely the fix:
        if someone reverts thread_locked() to a bare manager().get().acquire(),
        the test above goes green-to-red and this one goes red-to-green, so
        the pair cannot both pass on a regression.
        """
        p = tmp_path / "skill-quality.yaml"
        p.write_text("{}\n", encoding="utf-8")
        thread_lock = file_locks.manager().get(p)   # pre-fix line 270
        thread_lock.acquire()                        # pre-fix line 271
        try:
            time.sleep(0.2)
            status = file_locks.write_path_status()
            assert status["wedged"] is False, (
                "PRE-FIX SHAPE: the bare acquire registers no hold, so the "
                "wedge surface reports clean over a genuinely held lock. "
                "That false-clean IS the defect g-115-10378 fixed.")
            assert status["holds_in_flight"] == 0
            assert file_locks._holder_age(_key(p)) is None
        finally:
            thread_lock.release()


# ─── SPECIFICITY: outcome 3 — a young holder is still waited on ─────────────

class TestDoesNotRefuseALegitimateHolder:
    def test_a_young_holder_is_awaited_and_the_write_succeeds(self, tmp_path,
                                                              monkeypatch):
        """A slow-but-legitimate hold must behave exactly as before the fix."""
        monkeypatch.setattr(file_locks, "ACQUIRE_GIVEUP_SECONDS", 30.0)
        monkeypatch.setattr(file_locks, "ACQUIRE_POLL_SECONDS", 0.02)
        p = tmp_path / "skill-quality.yaml"
        p.write_text("{}\n", encoding="utf-8")
        holder = _Holder(p)
        holder.__enter__()

        got = []

        def _waiter():
            with file_locks.thread_locked(p):
                got.append(True)

        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        time.sleep(0.3)
        assert not got, "waiter took a lock that was still held"
        holder.__exit__()
        t.join(10)
        assert got == [True], (
            "a young holder must be WAITED ON, never refused — refusing a "
            "write that would have succeeded is the expensive error here")

    def test_absent_telemetry_KEEPS_WAITING_and_never_refuses(self, tmp_path,
                                                              fast_giveup):
        """Named after the BIAS it pins, not the behaviour it observes
        (guard-2373): _holder_age() returns None for an unheld key, and the
        fail-safe direction on UNKNOWN is to WAIT. A future author who
        'harmonizes' this to refuse-on-unknown turns every un-instrumented
        hold into a spurious 503.
        """
        p = tmp_path / "skill-quality.yaml"
        p.write_text("{}\n", encoding="utf-8")
        assert file_locks._holder_age(_key(p)) is None
        # Unheld + no telemetry: must acquire immediately, not raise.
        with file_locks.thread_locked(p) as key:
            assert key == _key(p)


# ─── CHECK 1: the helper is not a single-use abstraction ────────────────────

def test_locked_routes_through_thread_locked_second_call_site(tmp_path,
                                                              monkeypatch):
    """guard-2015/guard-4591: locked() must USE the extracted helper, not keep
    its own copy. If someone re-inlines the acquire into locked(), this reds —
    which is what stops the two implementations drifting apart again."""
    seen = []
    real = file_locks.thread_locked
    monkeypatch.setattr(file_locks, "thread_locked",
                        lambda *a, **k: seen.append(a[0]) or real(*a, **k))
    p = tmp_path / "victim.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    with file_locks.locked(p):
        pass
    assert seen, "locked() did not call thread_locked() — the copy is back"


# ─── CHECK 2: the endpoint answers 503, not 500 ─────────────────────────────

def test_wedged_skill_quality_write_returns_503_not_500(tmp_path, fast_giveup):
    """The pre-existing `except TimeoutError -> 503` wrapped ONLY acquire_lock;
    the thread acquire sat OUTSIDE it, so a wedge would have escaped as a 500.
    """
    from mind_api.src.meta import skill_quality_score
    meta, world, agent = _setup(tmp_path)
    quality_path = meta / "skill-quality.yaml"
    quality_path.write_text("{}\n", encoding="utf-8")

    with _Holder(quality_path):
        time.sleep(0.2)
        resp = skill_quality_score.score(_FakeCtx(meta, world, agent,
                                                  body=_body()))
    assert resp.status == 503, (
        f"a wedged write path must degrade to 503 lock_timeout, got "
        f"{resp.status} — a 500 here is the un-caught WritePathWedged")
    assert b"held for" in resp.body, (
        "the 503 should carry the wedge diagnosis, not a generic message")
