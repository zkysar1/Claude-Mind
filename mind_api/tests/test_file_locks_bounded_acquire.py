""": a wedged holder must refuse new writers, not park them forever.

WHY THIS EXISTS. g-115-10019 made the wedge OBSERVABLE and deliberately left
`thread_lock.acquire()` unbounded; its own comment says so. Observability is
not recovery. Measured 2026-09-17 (alpha, cc-04, own-cloud): a wm-prune
request timed out client-side at RT_CURL_TIMEOUT=90 and from that instant
EVERY WM write hung forever while reads returned in 1s through the same
daemon, which answered curl in 31ms. The client giving up does not release a
server-side lock, so one timeout wedged the whole box until
`mind-api-start.sh --restart` (guard-6895).

THE SAFETY PROPERTY UNDER TEST, and the reason this is shippable on a
111-call-site write path: a waiter gives up ONLY when the CURRENT holder has
itself already outlived the give-up threshold. Every younger holder — however
slow — is waited on exactly as before. So in every case where behaviour
changes, the pre-existing behaviour was an unbounded hang.

guard-1660 governs the shape: each direction is paired. The SPECIFICITY half
(a young or absent holder must NOT be refused) is the half that keeps this
from becoming a write-refusing bug of its own, so it carries the most cases.
"""
from __future__ import annotations

import threading
import time

import pytest

from mind_api.src import file_locks


class _Holder:
    """Hold file_locks.locked(path) until released — the measured failure.

    Mirrors _Wedge in test_write_path_wedge_probe.py deliberately: same
    mechanism, so the two files agree about what a wedge IS.
    """

    def __init__(self, path, max_hold=60):
        self.path = path
        self._release = threading.Event()
        self._held = threading.Event()
        self._max_hold = max_hold
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with file_locks.locked(self.path):
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


@pytest.fixture
def victim(tmp_path):
    p = tmp_path / "victim.jsonl"
    p.write_text("{}\n", encoding="utf-8")
    return p


def _key(path):
    return str(path.resolve())


# ─── SENSITIVITY: a wedged holder refuses new writers ────────────────────────

class TestRefusesAWedgedHolder:
    def test_raises_instead_of_hanging(self, victim):
        with _Holder(victim):
            time.sleep(0.3)
            lock = file_locks.manager().get(victim)
            started = time.monotonic()
            with pytest.raises(file_locks.WritePathWedged):
                file_locks.acquire_or_wedge(
                    lock, _key(victim),
                    giveup_seconds=0.2, poll_seconds=0.05)
            # The point is not merely that it raised — it is that it raised
            # PROMPTLY. A bound that took a minute to report would leave the
            # same pile-up this exists to prevent.
            assert time.monotonic() - started < 5.0

    def test_message_names_holder_age_and_the_remedy(self, victim):
        with _Holder(victim):
            time.sleep(0.3)
            lock = file_locks.manager().get(victim)
            with pytest.raises(file_locks.WritePathWedged) as ei:
                file_locks.acquire_or_wedge(
                    lock, _key(victim),
                    giveup_seconds=0.2, poll_seconds=0.05)
            msg = str(ei.value)
            # Whoever reads this message is looking at a dead box and needs
            # WHICH file, HOW LONG, and WHAT TO DO — nothing else.
            assert _key(victim) in msg
            assert "held for" in msg
            assert "--restart" in msg

    def test_is_a_timeout_error_so_existing_503_handlers_still_work(self):
        # Load-bearing, not cosmetic: 111 call sites reach this lock, and
        # several already map a lock TimeoutError to a 503 "lock busy".
        # A bare Exception subclass would have turned those into 500s.
        assert issubclass(file_locks.WritePathWedged, TimeoutError)


# ─── SPECIFICITY: everything else must be waited on, exactly as before ───────

class TestDoesNotRefuseALegitimateHolder:
    def test_uncontended_acquire_is_immediate(self, victim):
        lock = file_locks.manager().get(victim)
        file_locks.acquire_or_wedge(lock, _key(victim),
                                    giveup_seconds=0.2, poll_seconds=0.05)
        try:
            assert lock.locked()
        finally:
            lock.release()

    def test_young_holder_is_waited_for_and_then_acquired(self, victim):
        """A slow-but-legitimate write must still be waited on.

        This is the case that would break the fleet if the bound were a plain
        deadline: alpha's real holds are multi-megabyte own-cloud PUTs whose
        duration is set by object size and network, not by this module.
        """
        holder = _Holder(victim)
        holder.__enter__()
        released = threading.Event()

        def _release_soon():
            time.sleep(0.4)
            holder.__exit__()
            released.set()

        threading.Thread(target=_release_soon, daemon=True).start()
        lock = file_locks.manager().get(victim)
        # give-up is 30s and the holder lets go at ~0.4s, so the ONLY correct
        # behaviour is to block and then succeed.
        file_locks.acquire_or_wedge(lock, _key(victim),
                                    giveup_seconds=30.0, poll_seconds=0.05)
        try:
            assert released.wait(5)
        finally:
            lock.release()

    def test_absent_holder_telemetry_never_refuses(self, victim):
        """_holder_age None means "nobody holds it" -> keep waiting, never raise.

        The fail-safe direction: refusing a write that could have succeeded is
        the expensive error. A site that takes the thread lock WITHOUT going
        through locked() records no hold, and must not be refused on that
        basis alone.
        """
        lock = file_locks.manager().get(victim)
        lock.acquire()  # held, but NOT registered in _HOLDS
        try:
            assert file_locks._holder_age(_key(victim)) is None
            done = threading.Event()

            def _try():
                try:
                    file_locks.acquire_or_wedge(
                        lock, _key(victim),
                        giveup_seconds=0.1, poll_seconds=0.05)
                except file_locks.WritePathWedged:
                    done.set()  # would be the BUG

            t = threading.Thread(target=_try, daemon=True)
            t.start()
            # Well past giveup_seconds: if the absent-telemetry branch raised,
            # this would be set.
            assert not done.wait(1.0), \
                "refused a write on absent holder telemetry (fail-open broken)"
        finally:
            lock.release()

    def test_waiter_count_does_not_drift_after_a_refusal(self, victim,
                                                         monkeypatch):
        """A give-up must still decrement _WAITING.

        A count that only decremented on success would drift upward forever
        and eventually make write_path_status report a wedge that is not
        there — re-introducing, through the telemetry, the false positive the
        telemetry exists to avoid.

        Driven through the REAL contextmanager rather than acquire_or_wedge
        directly, because the decrement lives in locked()'s finally clause and
        that is the code under test.
        The constants are monkeypatched rather than passed: locked() takes no
        threshold argument by design (one policy, one place), and
        acquire_or_wedge reads both globals at CALL time so patching works.
        """
        monkeypatch.setattr(file_locks, "ACQUIRE_GIVEUP_SECONDS", 0.2)
        monkeypatch.setattr(file_locks, "ACQUIRE_POLL_SECONDS", 0.05)
        with _Holder(victim):
            time.sleep(0.3)
            before = file_locks.write_path_status()["blocked_writers"]
            with pytest.raises(file_locks.WritePathWedged):
                with file_locks.locked(victim):  # via the real contextmanager
                    pass
            after = file_locks.write_path_status()["blocked_writers"]
            assert after == before

    def test_locked_itself_honours_the_bound(self, victim, monkeypatch):
        """The contextmanager — not just the helper — must refuse a wedge.

        acquire_or_wedge passing in isolation proves nothing about the 111
        call sites, which all arrive through locked(). This is the path they
        take.
        """
        monkeypatch.setattr(file_locks, "ACQUIRE_GIVEUP_SECONDS", 0.2)
        monkeypatch.setattr(file_locks, "ACQUIRE_POLL_SECONDS", 0.05)
        with _Holder(victim):
            time.sleep(0.3)
            started = time.monotonic()
            with pytest.raises(file_locks.WritePathWedged):
                with file_locks.locked(victim):
                    pass
            assert time.monotonic() - started < 5.0


# ─── MUTATION PROOF (guard-385): the old code really did hang ────────────────

def test_pre_fix_unbounded_acquire_would_have_hung(victim):
    """Reconstruct the PRE-FIX call and assert it does NOT complete.

    Without this, every test above would still pass if someone quietly
    reverted acquire_or_wedge to a bare acquire() — the refusal tests would
    fail, but nothing would show that the *old* behaviour was the unbounded
    hang this goal is about. This pins the defect, not just the fix.
    """
    with _Holder(victim):
        lock = file_locks.manager().get(victim)
        acquired = threading.Event()

        def _old_way():
            lock.acquire()          # the exact pre-fix line
            acquired.set()
            lock.release()

        threading.Thread(target=_old_way, daemon=True).start()
        assert not acquired.wait(1.5), \
            "the bare acquire() returned while the lock was held — the " \
            "mutation proof is no longer measuring the pre-fix behaviour"
