"""The unit-claim board read must be FRESH, not cached ().

WHY THIS FILE EXISTS SEPARATELY FROM test_unit_claim.py. That file drives the
pure logic and is CORRECT -- `test_second_body_same_unit_is_refused` has passed
since the module shipped. It was green throughout the 2026-09-03 incident in
which two alpha Bodies claimed the SAME unit of g-368-77 nineteen seconds apart,
both rc=0, and both built the same production IAM role.

`decide()` never failed. It was handed records that did not contain the peer's
claim and returned the only verdict those records support. The board is
merge-registered and served to readers from a LOCAL cache while writes are
write-through, so a peer's just-written claim is structurally invisible to a
cached read. Measured that day (cc-08, own-cloud): the local copy of
coordination.jsonl was missing 3 records that existed in the store of record,
seconds old, from two other Bodies; force_fresh returned them in 0.30s.

So these tests pin the WIRING, which is the half no pure-logic test can reach
(guard-1943: pinning the writer says nothing about the wiring).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_backend  # noqa: E402
import unit_claim  # noqa: E402


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def trace(monkeypatch):
    """Record the ORDER of (cache refresh, board read), and the force_fresh flag."""
    seen = []

    class FakeBackend:
        def read_text(self, path, encoding="utf-8", *, force_fresh=False):
            seen.append(("refresh", force_fresh))
            return ""

    monkeypatch.setattr(storage_backend, "get_backend", lambda: FakeBackend())

    def fake_run(*_a, **_k):
        seen.append(("board-read", None))
        return _FakeProc()

    monkeypatch.setattr(unit_claim.subprocess, "run", fake_run)
    return seen


def test_cache_is_refreshed_before_the_board_is_read(trace):
    """The refresh must come FIRST -- after the read it protects nothing."""
    unit_claim._read_board(4)
    kinds = [k for k, _ in trace]
    assert kinds, "neither a refresh nor a board read happened"
    assert kinds[0] == "refresh", f"first I/O was {kinds[0]!r}, not the refresh"
    assert "board-read" in kinds, "the canonical board-read.sh call was skipped"


def test_the_refresh_actually_passes_force_fresh(trace):
    """A refresh that omits force_fresh re-reads the same cache: a no-op gate."""
    unit_claim._read_board(4)
    flags = [f for k, f in trace if k == "refresh"]
    assert flags == [True], f"force_fresh flags were {flags!r}, expected [True]"


def test_release_does_not_pay_the_refresh(trace):
    """`main` discards records for a release, so it must not need the network.

    Making a release depend on remote reachability lets a network blip wedge a
    unit for a full lease -- failing in the duplicate-producing direction this
    module exists to avoid.
    """
    unit_claim._read_board(4, fresh=False)
    assert [k for k, _ in trace] == ["board-read", "board-read"], (
        "fresh=False must skip the refresh and still read both message types"
    )


def test_a_failed_refresh_refuses_rather_than_deciding_from_a_stale_cache(monkeypatch):
    """Fail direction: exit 2, never a silent 'the unit is free'.

    Mirrors _read_board's own posture for a failed board-read. A swallowed
    refresh error would present as a board with no peer claim -- the exact
    wrong answer (verify-before-assuming rule 4).
    """
    def boom():
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(storage_backend, "get_backend", boom)
    monkeypatch.setattr(unit_claim.subprocess, "run",
                        lambda *a, **k: pytest.fail("decided from a stale cache"))

    with pytest.raises(SystemExit) as exc:
        unit_claim._read_board(4)
    assert exc.value.code == 2, f"exited {exc.value.code}, expected 2"
