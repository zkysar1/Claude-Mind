"""test_owncloud_sync_stale_local_guard.py — LOCAL-WINS drain-awareness guard
(g-306-534), the content-level gate the two existing admission gates cannot
provide.

THE MEASURED MECHANISM (g-306-527, alpha fleet): the reducer DRAINS the agent
session working-memory.yaml capture slots oldest-first after consuming the
relays (the drained entries are what the fleet has ALREADY acted on). During
the reducer box-move window (2026-09-26T19:08:32Z -> 2026-09-27T01:38:36Z) a
stale LOCAL copy of the file — still holding every pre-drain entry — was
pushed over the DRAINED S3 through the LOCAL-WINS lane (g-115-2820): one push
restored all 14 tracked ids (spark_capture 50 -> 2,853 entries), and a second
stale push two hours later (2026-09-27T04:23:35Z) did it again. The carrier
was a stale local whole-file overwrite through the LOCAL-WINS lane, not a
merge (the file is deliberately unregistered -> _MERGE_NA).

Neither existing gate could see it:
  * classification (g-115-2820): working-memory.yaml IS a single-writer
    session file -> eligible. It is not a staleness gate.
  * claim continuity (g-306-379): the NEW box legitimately held the claim (it
    holds it NOW) -> admitted. And the gate FAILS OPEN on a pre-field
    handover anyway (holder_since 0/absent — the residual its own docstring
    names), which is exactly the residual that admitted this push. It is a
    gate on authorship, not on content.

The discriminating fact is entry-set SHAPE, which a drain leaves: drains
delete oldest-first, so the drained side (S3) holds a SUBSET of the stale
side (local) whose NEWEST entry is newer than entries the stale side still
retains below it. No recency check works: the stale local file is LIVE-
MAINTAINED (the new box keeps appending to its stale baseline), so its mtime
and max _item_ts are fresh while it still resurrects drained content. Hence
the guard is content-based, per capture slot, keyed by the same content hash
body-merge.py uses for its array union.

THE GUARD (owncloud_sync._local_wins_stale_reason, run by the lane via
_local_wins_stale_probe before mirror_put):
    S3 ⊆ local, local's EXTRA entries OLDER than S3's newest
        -> "stale_resurrection" REFUSE (local_wins_blocked_stale_local)
    S3 ⊆ local, ALL extra entries NEWER than S3's newest
        -> admit (the legitimate forward-append shape — the 451-skip wedge
           g-115-2816/2820 fixed; LOCAL-WINS MUST keep resolving it)
    S3 NOT ⊆ local (S3 holds entries local lacks)
        -> "s3_unique_content" REFUSE (conservative; a push would DELETE
           S3-side content — the reconcile case, not the local-wins case)
    unparseable / not a mapping / no capture slots / S3 object absent
        -> fail OPEN (pre-g-306-534 behavior; guard-1562 enumeration)

These tests pass holder_since_by_agent=None (the continuity gate fails OPEN,
exactly as the pre-field residual does in production), so every test isolates
the CONTENT guard alone. Fixtures carry the MEASURED g-306-527 ids
(g-326-609, g-115-7823, g-326-698, g-335-1348) in the roles they played.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

import owncloud_sync as _mod  # noqa: E402
from test_owncloud_sync import FakeBackend, _md5, _new_stats  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Pin our own clean env (autouse fixtures do not cross module
    boundaries) against a runner shell exporting backend / multi-machine
    env — same posture as test_owncloud_sync_local_wins.py."""
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


def _entry(relay_id: str, ts: str) -> dict:
    return {"relay_id": relay_id, "text": f"capture for {relay_id}",
            "_item_ts": ts}


def _wm_bytes(spark_entries) -> bytes:
    """A working-memory.yaml body: active_context + a spark_capture slot.
    _item_ts is the zero-padded ISO string wm.py stamps via now_iso()
    (strftime %Y-%m-%dT%H:%M:%S) — lexical comparison is the correct
    ordering."""
    doc = {"active_context": {"session_id": "session-500"},
           "spark_capture": list(spark_entries)}
    return yaml.safe_dump(doc, sort_keys=False,
                          default_flow_style=False).encode("utf-8")


# The MEASURED  ids, in the roles they played:
_PRE_DRAIN = (_entry("g-326-609", "2026-09-25T08:00:00"),
              _entry("g-326-698", "2026-09-25T09:00:00"))
_SURVIVORS = (_entry("g-115-7823", "2026-09-26T17:00:00"),
              _entry("g-335-1348", "2026-09-26T18:30:00"))

_BASELINE = _md5(b"stale-baseline-bytes\n")   # neither side ever matches


def _session_wm(root: Path, agent: str = "alpha") -> Path:
    f = root / agent / "session" / "working-memory.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def _run(be, f, local: bytes, s3: bytes, *, dry_run: bool = False):
    f.write_bytes(local)
    be.s3[str(f)] = s3
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=dry_run, stats=stats,
                         baseline_md5=_BASELINE, multi_machine=True,
                         own_cloud_authority=True)
    return out, stats


# ── 1: THE fix — the  measured shape is refused; S3 stays drained ──
def test_stale_resurrection_refused(tmp_path):
    """S ⊆ L, and local's EXTRA (pre-drain) entries are OLDER than S3's
    newest entry -> REFUSE: no push, S3 stays drained, the refusal is
    COUNTED and the file lands on the existing clobber-safe skip
    (conflict_paths -> the g-115-8027 streak/alert surface). This is the
    2026-09-27T01:38:36 push, now blocked."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    local = _wm_bytes([*_PRE_DRAIN, *_SURVIVORS])   # stale baseline + survivors
    s3 = _wm_bytes(list(_SURVIVORS))                # the drained state (truth)

    out, stats = _run(be, f, local, s3)

    assert out is None
    assert be.puts == []                            # NO push
    assert be.s3[str(f)] == s3                      # S3 stays drained
    assert f.read_bytes() == local                  # local untouched
    assert stats.get("local_wins_blocked_stale_local") == 1
    assert stats.get("local_wins_resolved") in (None, 0)
    assert stats.get("diverged_skipped") == 1       # routed to existing skip
    assert stats.get("conflict_paths") == [str(f)]  # -> streak/alert surface


# ── 2: S3 holds entries local lacks -> conservative refuse (no delete) ─────
def test_s3_unique_content_refused(tmp_path):
    """S3 holds a post-divergence entry local lacks (the reducer appended to
    the drained state after the local side forked). S ⊄ L -> pushing local
    would DELETE that S3-side content -> REFUSE (the reconcile case, not the
    local-wins case)."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    s3_extra = _entry("g-115-7736", "2026-09-27T02:00:00")
    local = _wm_bytes([*_PRE_DRAIN, *_SURVIVORS])
    s3 = _wm_bytes([*_SURVIVORS, s3_extra])

    out, stats = _run(be, f, local, s3)

    assert out is None
    assert be.puts == []                            # NO push
    assert be.s3[str(f)] == s3                      # S3-unique entry survives
    assert stats.get("local_wins_blocked_stale_local") == 1
    assert stats.get("local_wins_resolved") in (None, 0)
    assert stats.get("diverged_skipped") == 1
    assert stats.get("conflict_paths") == [str(f)]


# ── 3: THE negative control — forward-append MUST still resolve LOCAL-WINS ──
def test_forward_append_still_local_wins(tmp_path):
    """S ⊆ L with ALL of local's extra entries NEWER than S3's newest entry:
    the legitimate forward-append divergence — the exact shape of the 451-skip
    wedge (g-115-2816/2820). If this test fails, the guard RE-WEDGES the file
    forever (451 skips and counting) — the original incident it must not
    re-arm. LOCAL-WINS must push + adopt the local baseline."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    fresh = (_entry("g-306-534", "2026-09-27T09:00:00"),
             _entry("g-306-535", "2026-09-27T10:00:00"))
    local = _wm_bytes([*_SURVIVORS, *fresh])   # local appended AFTER S3 state
    s3 = _wm_bytes(list(_SURVIVORS))

    out, stats = _run(be, f, local, s3)

    assert stats.get("local_wins_resolved") == 1
    assert stats.get("local_wins_blocked_stale_local") in (None, 0)
    assert be.puts == [str(f)]                        # the push happened
    assert be.s3[str(f)] == local                     # S3 now carries local
    assert out == _md5(local)                         # baseline adopted
    assert stats.get("diverged_skipped") in (None, 0)
    assert "conflict_paths" not in stats


# ── 4: dry-run observes a stale refusal without mutating ────────────────────
def test_dry_run_stale_refusal_observed(tmp_path):
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    local = _wm_bytes([*_PRE_DRAIN, *_SURVIVORS])
    s3 = _wm_bytes(list(_SURVIVORS))

    out, stats = _run(be, f, local, s3, dry_run=True)

    assert out is None
    assert be.puts == []
    assert be.s3[str(f)] == s3
    assert stats.get("local_wins_blocked_stale_local") == 1
    assert stats.get("local_wins_would_resolve") in (None, 0)


# ── 5: fail-open (guard-1562) — unparseable S3 bytes -> pre- ──────
def test_unparseable_s3_fails_open(tmp_path):
    """Guard-1562 enumeration case 1: S3 bytes that are not parseable YAML.
    The predicate returns None, so the lane admits EXACTLY as it did before
    this change — LOCAL-WINS proceeds. A guard that refuses what it cannot
    read is a wedge factory; refusal is only for positively-identified
    staleness."""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    local = _wm_bytes([*_PRE_DRAIN, *_SURVIVORS])
    s3 = b"\x00\x01 not: [yaml: at all"   # unparseable

    out, stats = _run(be, f, local, s3)

    assert stats.get("local_wins_resolved") == 1     # admitted as before
    assert stats.get("local_wins_blocked_stale_local") in (None, 0)
    assert be.puts == [str(f)]


# ── 6: fail-open (guard-1562) — no capture slot -> pre- ───────────
def test_no_capture_slot_fails_open(tmp_path):
    """Guard-1562 enumeration case 2: a single-writer session file with NO
    capture slot at all (a handoff.yaml-shaped mapping — still eligible by
    classification, still reaches the lane). The predicate finds no slot to
    compare -> None -> LOCAL-WINS proceeds, byte-for-byte pre-g-306-534."""
    be = FakeBackend([(tmp_path, "agents")])
    f = tmp_path / "alpha" / "session" / "handoff.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    local = b"active_context:\n  session_id: session-500\n"
    s3 = b"active_context:\n  session_id: session-499\n"
    be.s3[str(f)] = s3
    f.write_bytes(local)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_BASELINE, multi_machine=True,
                         own_cloud_authority=True)

    assert stats.get("local_wins_resolved") == 1
    assert stats.get("local_wins_blocked_stale_local") in (None, 0)
    assert be.puts == [str(f)]
    assert out == _md5(local)


# ── 7: named residual — S3 slot drained to EMPTY -> fail OPEN (deliberate) ─
def test_predicate_empty_s3_slot_fails_open():
    """The docstring's KNOWN RESIDUAL, pinned: S3's spark_capture is EMPTY
    (fully drained) while local holds entries. `max()` over the empty S3 set
    raises -> caught -> None (admit). Deliberate, not a gap in the catch: an
    empty-S3-slot + non-empty-local slot is indistinguishable from a
    legitimate fresh append into an empty slot, and refusing it would risk
    the very re-wedge this guard must not cause. (The MEASURED g-306-527
    carrier was a PARTIAL drain — test 1 above pins that shape.)"""
    local = _wm_bytes([_PRE_DRAIN[0]])
    s3 = _wm_bytes([])          # S3 slot drained to empty
    assert _mod._local_wins_stale_reason(local, s3) is None


# ── 8: predicate unit — S3 object absent -> fail OPEN at the probe ──────────
def test_probe_s3_absent_fails_open(tmp_path):
    """A backend whose S3 object is absent: read_authoritative_bytes raises
    FileNotFoundError -> the probe returns None (admit) instead of an
    unhandled exception killing the sweep. (The real backend mirrors this;
    LocalBackend's machine-local branch reads local, but a session file
    reaching the lane is by construction under a sync root.)"""
    be = FakeBackend([(tmp_path, "agents")])
    f = _session_wm(tmp_path)
    f.write_bytes(_wm_bytes([*_PRE_DRAIN, *_SURVIVORS]))
    assert str(f) not in be.s3          # S3 object genuinely absent
    assert _mod._local_wins_stale_probe(be, f, f.read_bytes()) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
