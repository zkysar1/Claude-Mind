#!/usr/bin/env python3
"""test_loop_state_cas_stale_steal.py —  regression test.

Pins the ROBUST tier of the g-115-1349 fix hierarchy: optimistic concurrency
on slot_meta.loop_state.update_count guards the local working-memory
stale-lock-steal race (mechanism B, guard-685). When a loop_state writer stalls
>10s mid-RMW under OneDrive/daemon latency, acquire_lock STALE-BREAKS its lock,
a peer reads + writes, and the original writer's later write would clobber the
peer's counter increment (observed bravo session 85). The CAS retry in
_fileops.loop_state_cas_retry detects the update_count change between a writer's
read and its write, re-reads fresh, and re-applies — so neither increment is
lost.

These tests exercise the shared helper directly with in-memory read/write
closures that REPRODUCE the overlap (a peer write injected between the helper's
top-read and its verify-read), which is the cleanest way to assert "no lost
update" without spawning two genuinely-racing OS processes. The end-to-end
happy path through the refactored CLI writer is covered by
test_loop_state_counter_advance.py (unchanged behaviour); the daemon path by
test_compact_restore_loop_state_shape.py.

Refs: g-115-1394 (this fix), g-115-1349 (mechanism analysis), guard-685,
guard-540, loop-state-integrity tree node, rb-1536 (DDB CAS analogue),
core/scripts/_fileops.py::loop_state_cas_retry.

Run: py -3 core/scripts/tests/test_loop_state_cas_stale_steal.py
"""
import copy
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from _fileops import loop_state_cas_retry, _slot_update_count
except Exception as e:  # pragma: no cover - import guard
    print(f"FAIL: cannot import helper from _fileops ({e})", file=sys.stderr)
    sys.exit(2)

# No-op backoff so the tests do not sleep on simulated conflicts.
_NO_BACKOFF = lambda _attempt: 0


def _wm(goals_completed, update_count):
    """Minimal wm dict with loop_state + slot_meta.loop_state.update_count."""
    return {
        "slots": {"loop_state": {"goals_completed": goals_completed}},
        "slot_meta": {"loop_state": {"update_count": update_count}},
    }


def _bump_mutate(wm):
    """A loop_state mutate that bumps goals_completed by 1 and advances
    update_count (the inline _update_modified contract every real writer uses)."""
    ls = wm["slots"]["loop_state"]
    ls["goals_completed"] = int(ls.get("goals_completed", 0)) + 1
    m = wm.setdefault("slot_meta", {}).setdefault("loop_state", {"update_count": 0})
    m["update_count"] = int(m.get("update_count", 0)) + 1
    return True


def test_slot_update_count_extraction():
    """guard-502 branch coverage for the token extractor."""
    cases = [
        (_wm(0, 7), 7),                                   # present int
        ({}, 0),                                          # no slot_meta
        ({"slot_meta": {}}, 0),                           # slot_meta but no slot
        ({"slot_meta": {"loop_state": {}}}, 0),           # slot meta but no counter
        ({"slot_meta": {"loop_state": {"update_count": "x"}}}, 0),  # non-int
        ("not-a-dict", 0),                                # not a dict
    ]
    for wm, expected in cases:
        got = _slot_update_count(wm, "loop_state")
        if got != expected:
            print(f"FAIL: _slot_update_count({wm!r}) = {got}, expected {expected}",
                  file=sys.stderr)
            return False
    print("PASS: _slot_update_count handles present/missing/non-int/non-dict")
    return True


def test_no_contention_single_pass():
    """No peer writes -> verify matches on attempt 0 -> single write, no retry."""
    disk = {"v": _wm(10, 5)}
    reads = {"n": 0}

    def _read():
        reads["n"] += 1
        return copy.deepcopy(disk["v"])

    def _write(wm):
        disk["v"] = copy.deepcopy(wm)

    res = loop_state_cas_retry(_read, _bump_mutate, _write,
                               backoff_fn=_NO_BACKOFF)
    gc = disk["v"]["slots"]["loop_state"]["goals_completed"]
    uc = disk["v"]["slot_meta"]["loop_state"]["update_count"]
    if not (gc == 11 and uc == 6 and res["attempts"] == 1
            and res["conflicted"] is False and res["exhausted"] is False
            and res["noop"] is False):
        print(f"FAIL: no-contention — gc={gc} uc={uc} res={res} reads={reads['n']}",
              file=sys.stderr)
        return False
    print("PASS: no-contention single pass (gc 10->11, uc 5->6, 1 attempt)")
    return True


def test_stale_steal_reapplies():
    """THE regression: a peer write injected between the helper's top-read and
    its verify-read (the stale-lock-steal overlap) must NOT silently drop an
    increment. Both the peer's +1 and our +1 must land."""
    disk = {"v": _wm(10, 5)}
    calls = {"n": 0}

    def _read():
        # Call sequence per attempt: [top-read, verify-read].
        # On the verify-read of attempt 0 (call index 1) a peer steals the lock
        # and commits goals_completed 10->11, update_count 5->6 BEFORE we read,
        # so our verify sees update_count=6 != our token 5 -> conflict.
        if calls["n"] == 1:
            peer = disk["v"]
            peer["slots"]["loop_state"]["goals_completed"] += 1
            peer["slot_meta"]["loop_state"]["update_count"] += 1
        calls["n"] += 1
        return copy.deepcopy(disk["v"])

    def _write(wm):
        disk["v"] = copy.deepcopy(wm)

    res = loop_state_cas_retry(_read, _bump_mutate, _write,
                               backoff_fn=_NO_BACKOFF)
    gc = disk["v"]["slots"]["loop_state"]["goals_completed"]
    # 10 (orig) + 1 (peer stale-steal) + 1 (our re-applied bump) = 12. A lost
    # update would leave gc=11 (our write clobbering the peer, or vice versa).
    if gc != 12:
        print(f"FAIL: stale-steal LOST AN UPDATE — gc={gc}, expected 12 "
              f"(orig 10 + peer 1 + ours 1); res={res}", file=sys.stderr)
        return False
    if not (res["conflicted"] is True and res["attempts"] == 2
            and res["exhausted"] is False):
        print(f"FAIL: stale-steal — expected conflicted/2-attempts/not-exhausted, "
              f"got {res}", file=sys.stderr)
        return False
    print("PASS: stale-steal re-applied — both increments survive (gc=12, "
          "conflict detected, 2 attempts)")
    return True


def test_idempotent_noop_skips_write():
    """mutate_fn returning falsy => no write, noop=True."""
    disk = {"v": _wm(10, 5)}
    wrote = {"n": 0}

    def _read():
        return copy.deepcopy(disk["v"])

    def _write(wm):
        wrote["n"] += 1
        disk["v"] = copy.deepcopy(wm)

    res = loop_state_cas_retry(_read, lambda wm: False, _write,
                               backoff_fn=_NO_BACKOFF)
    gc = disk["v"]["slots"]["loop_state"]["goals_completed"]
    if not (res["noop"] is True and wrote["n"] == 0 and gc == 10):
        print(f"FAIL: idempotent no-op — res={res} writes={wrote['n']} gc={gc}",
              file=sys.stderr)
        return False
    print("PASS: idempotent no-op skips the write entirely")
    return True


def test_exhaustion_fail_open_commits():
    """Persistent conflict (peer writes before EVERY verify) -> retries spent ->
    fail-open commit of the last attempt, exhausted=True (a re-applied increment
    is better than a silently-dropped one; writers' own idempotency dedupes)."""
    disk = {"v": _wm(10, 5)}
    calls = {"n": 0}

    def _read():
        # Every verify-read (odd call index) gets a fresh peer bump, so the
        # token never matches -> conflict on all 3 attempts -> exhaustion.
        if calls["n"] % 2 == 1:
            peer = disk["v"]
            peer["slots"]["loop_state"]["goals_completed"] += 1
            peer["slot_meta"]["loop_state"]["update_count"] += 1
        calls["n"] += 1
        return copy.deepcopy(disk["v"])

    def _write(wm):
        disk["v"] = copy.deepcopy(wm)

    res = loop_state_cas_retry(_read, _bump_mutate, _write,
                               retry_cap=3, backoff_fn=_NO_BACKOFF)
    if not (res["exhausted"] is True and res["attempts"] == 3
            and res["conflicted"] is True):
        print(f"FAIL: exhaustion — expected exhausted/3-attempts/conflicted, "
              f"got {res}", file=sys.stderr)
        return False
    # The last attempt is still committed (a write happened), so the final
    # state reflects the last mutate (not a dropped no-op).
    gc = disk["v"]["slots"]["loop_state"]["goals_completed"]
    if gc < 11:
        print(f"FAIL: exhaustion did not commit the last attempt — gc={gc}",
              file=sys.stderr)
        return False
    print("PASS: exhaustion fail-open commits the last attempt (exhausted=True)")
    return True


def main():
    results = [
        test_slot_update_count_extraction(),
        test_no_contention_single_pass(),
        test_stale_steal_reapplies(),
        test_idempotent_noop_skips_write(),
        test_exhaustion_fail_open_commits(),
    ]
    if all(results):
        print("\n════════════════════════════════════════════")
        print(f"  ALL {len(results)} TESTS PASS — loop_state CAS stale-steal pinned")
        print("════════════════════════════════════════════")
        return 0
    fail_count = sum(1 for r in results if not r)
    print(f"\nFAIL: {fail_count}/{len(results)} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
