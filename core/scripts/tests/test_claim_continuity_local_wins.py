"""test_claim_continuity_local_wins.py — LOCAL-WINS must not push bytes this box
authored while a PEER legitimately held the runner claim (g-306-379).

THE DEFECT. `_is_single_writer_session_file`'s docstring justified the
both-diverged LOCAL-WINS auto-resolve with "reaching this branch PROVES this box
authored the local change". g-306-378 measured that and found it holds only
while the claim never moves. `owned` is recomputed per sweep, so a box that
LOSES the claim stops PUSHING within one sweep but keeps WRITING its local
session files; a peer legitimately advances S3 meanwhile; and if the first box
later RE-ACQUIRES, `_sync_one` reaches both-diverged with a STALE local file and
LOCAL-WINS pushes it over the interim holder's real writes. Not hypothetical —
stop-hook-compliance.md records cc-04 executing as reducer 2.5h+ after losing its
claim, which is the window that feeds this.

WHY THE OBVIOUS FIX WAS REJECTED, since these tests encode the replacement. The
goal proposed comparing the file mtime against the claim's ACQUISITION time.
Measured on the live tree (bravo, cc-05): 22 of 73 files in that agent's session
dir are older than the current runner-token, so ~30% of the directory would lose
its auto-resolve and fall back to the conservative skip — precisely the
451-consecutive-skip wedge (g-115-2820) the LOCAL-WINS lane exists to prevent.
A single acquisition timestamp also cannot tell continuous tenure from
re-acquisition-after-a-peer: both produce the same value.

WHAT IS TESTED INSTEAD. `holder_since` marks when the CURRENT machine became the
holder and is NOT bumped by a same-machine re-acquire, so a same-box restart
keeps its whole history admissible while a genuine handover invalidates it.

POSITIVE CONTROLS (guard-4166). The headline assertions are REFUSALS, and a gate
that refused everything would satisfy every one of them. So each refusal test is
paired with an admission test on the same machinery: the classifier still admits
a fresh file (A), a legacy 0 still admits (C), an absent map still admits (D),
and the end-to-end LOCAL-WINS push still fires under continuous tenure (G). A
mutant that hard-returns False flips A/C/D/G red; one that hard-returns True
flips B/E/F red.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

import owncloud_sync as _mod  # noqa: E402
from test_owncloud_sync import FakeBackend, _md5, _new_stats  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Autouse fixtures do not cross module boundaries — pin a clean env against
    a runner shell exporting backend / multi-machine vars."""
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


def _session_wm(root: Path, agent: str = "bravo") -> Path:
    f = root / agent / "session" / "working-memory.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def _write_at(path: Path, data: bytes, mtime: float) -> None:
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


# ── A: admission control — continuous tenure still passes ───────────────────
def test_file_written_during_tenure_is_admitted(tmp_path):
    """POSITIVE CONTROL. Written AFTER this box became holder -> continuity holds."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    held_since = time.time() - 3600
    _write_at(f, b"x", held_since + 60)
    assert _mod._claim_held_continuously(f, be, {"bravo": int(held_since)}) is True


# ── B: the defect — a write from before this tenure is refused ──────────────
def test_file_written_before_tenure_is_refused(tmp_path):
    """THE ASSERTION UNDER TEST. The file predates this box's current tenure, so a
    DIFFERENT machine may have held the claim in between and advanced S3."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    held_since = time.time() - 3600
    _write_at(f, b"x", held_since - 600)          # written BEFORE re-acquisition
    assert _mod._claim_held_continuously(f, be, {"bravo": int(held_since)}) is False


# ── C: legacy row (holder_since == 0) fails OPEN ────────────────────────────
def test_zero_holder_since_fails_open(tmp_path):
    """guard-1562: enumerate what is NEWLY refused. Every pre-existing claim row
    lacks the attribute and projects as 0, so on the day this ships the refused
    set is EMPTY and the LOCAL-WINS lane behaves exactly as before."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    _write_at(f, b"x", time.time() - 99_999)      # ancient — still admitted
    assert _mod._claim_held_continuously(f, be, {"bravo": 0}) is True


# ── D: no continuity data at all fails OPEN ────────────────────────────────
def test_absent_map_fails_open(tmp_path):
    """The local backend and the conservative-degrade ownership paths pass {}/None.
    Neither has an interim peer holder to defend against."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    _write_at(f, b"x", time.time() - 99_999)
    assert _mod._claim_held_continuously(f, be, {}) is True
    assert _mod._claim_held_continuously(f, be, None) is True


# ── E: unreadable mtime fails CLOSED ───────────────────────────────────────
def test_unreadable_mtime_fails_closed(tmp_path):
    """Distinct from C/D: there the DATA is absent, so there is nothing to defend
    against; here the data exists and we cannot prove continuity, so the
    conservative both-diverged skip is the correct answer."""
    be = FakeBackend([(tmp_path, "agents")])
    missing = tmp_path / "bravo" / "session" / "gone.yaml"
    missing.parent.mkdir(parents=True, exist_ok=True)
    assert _mod._claim_held_continuously(
        missing, be, {"bravo": int(time.time())}) is False


# ── F: end-to-end — a stale local file is NOT pushed over the peer's S3 ─────
def test_sync_one_withholds_local_wins_after_handover(tmp_path):
    """The clobber itself. Both sides moved since baseline, the file classifies as
    single-writer, this box holds the claim NOW — but it re-acquired after the
    local write, so LOCAL-WINS must be withheld and S3 left alone."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    held_since = time.time() - 600
    local = b"active_context:\n  session_id: session-500\n"
    _write_at(f, local, held_since - 3600)        # authored while NOT holding
    peer = b"active_context:\n  session_id: session-777\n"
    be.s3[str(f)] = peer                          # interim holder's real write
    stats = _new_stats()

    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"neither-side"), multi_machine=True,
                         own_cloud_authority=True,
                         holder_since_by_agent={"bravo": int(held_since)})

    assert out is None                            # no baseline adopted
    assert be.s3[str(f)] == peer                  # PEER'S BYTES SURVIVE
    assert be.puts == []                          # nothing was pushed
    assert stats.get("local_wins_resolved") in (None, 0)
    # The refusal is COUNTED, not silent — a gate whose firings are invisible
    # cannot be calibrated against the wedge it must not re-arm.
    assert stats.get("local_wins_blocked_claim_gap") == 1


# ── G: end-to-end control — LOCAL-WINS still fires under continuous tenure ──
def test_sync_one_still_local_wins_under_continuous_tenure(tmp_path):
    """POSITIVE CONTROL for F, and the anti-wedge guarantee. Same setup, except
    the local write happened DURING this box's tenure: the g-115-2820 self-heal
    must still fire, or this change has traded a clobber for a permanent skip."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    held_since = time.time() - 3600
    local = b"active_context:\n  session_id: session-500\n"
    _write_at(f, local, held_since + 600)         # authored WHILE holding
    be.s3[str(f)] = b"active_context:\n  session_id: session-499\n"
    stats = _new_stats()

    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"neither-side"), multi_machine=True,
                         own_cloud_authority=True,
                         holder_since_by_agent={"bravo": int(held_since)})

    assert stats.get("local_wins_resolved") == 1
    assert be.s3[str(f)] == local
    assert out == _md5(local)
    assert stats.get("local_wins_blocked_claim_gap") in (None, 0)


# ── H: the 2-tuple projection contract its callers depend on ───────────────
def test_owned_agents_with_provenance_still_returns_two_tuple(monkeypatch):
    """`_owned_claims` is now the one implementation; this delegate keeps the
    long-standing 2-tuple contract (owncloud_backend unpacks exactly two)."""
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    result = _mod._owned_agents_with_provenance()
    assert isinstance(result, tuple) and len(result) == 2
    owned, provenance = result
    assert owned is None and provenance == "local-backend"


def test_owned_claims_returns_three_tuple_with_empty_map_on_local(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    owned, provenance, holder_since = _mod._owned_claims()
    assert (owned, provenance) == (None, "local-backend")
    assert holder_since == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
