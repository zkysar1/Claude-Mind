"""test_owncloud_sync_merge_lanes.py — sync-one merge-lane engagement ().

The dedicated sync-one merge tests were dropped by the ac3730ea31d7 sync merge
ALONG WITH the superseded impl they targeted (`_merge_diverged` — coherent-pair
drop, so the suite stayed green while the CURRENT `_try_merge_put` architecture
ran with zero direct sync-one coverage; found by the g-115-2464 evil-merge
adjudication). These tests target the CURRENT lanes:

  A  diverged + registered store    -> diverged_merged lane engages (union on
     BOTH sides, merged md5 becomes the baseline, no conflict skip)
  B  no-baseline + own-cloud auth   -> nobaseline_merged lane engages (union,
     NOT the wholesale S3 pull that drops a local-authored tail)
  C1 local change, NO baseline      -> pushed_merged lane engages (union, never
     a blind whole-object PUT when S3 holds an object). This is the sync_file /
     PostToolUse shape and the lane the 2026-07-16 03:09:14 gate-firings
     head-replacement came from.
  C2 local change, S3 AT baseline   -> fenced plain PUT, NOT the union (g-115-7944).
     Classification has already proven no peer wrote, so there are no peer bytes
     for the union to preserve — and on a local REMOVAL the union re-adds the
     record and the file never converges (the lane-B wedge).
     C was ONE test until 2026-08-27. Its prose described C1 (the incident) while
     its fixture supplied a baseline, i.e. C2 — so it pinned a lane its own
     rationale did not describe, and its S3 side held no peer-only record, so it
     could not have caught the loss it guarded. Split, and the C1 half is now
     strictly stronger: it asserts the peer record SURVIVES.
  D  multipart ETag + registered    -> pins the CURRENT defer. The pre-merge
     lineage merged here ("multipart classification is unnecessary for a
     commutative merge"); the current lineage defers unconditionally — the
     enhancement is tracked separately, this test documents live behavior.
  E  unregistered store, diverged   -> clobber-safe skip preserved (negative
     control: merge lanes must not leak past the registry)

Transport is faked (FakeBackend from test_owncloud_sync); the merge itself is
REAL — MergeBackend.merge_put routes through the live registry
(owncloud_backend._coordination_merge_handler) and the live union handler, so
a registry regression or handler signature change fails here, not just in the
commutativity property test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

import owncloud_sync as _mod  # noqa: E402
from storage_backend import FileStat  # noqa: E402
from test_owncloud_sync import FakeBackend, _md5, _new_stats  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Mirror test_owncloud_sync's module-local autouse env clean — autouse
    fixtures do not cross module boundaries, so this module needs its own pin
    against a runner shell exporting backend/multi-machine env."""
    monkeypatch.delenv("MACHINE_MULTI", raising=False)
    monkeypatch.delenv("OWNERSHIP_STALE_SECONDS", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)


@pytest.fixture(autouse=True)
def _redirect_merge_events_log(tmp_path, monkeypatch):
    """: _try_merge_put (reached via _sync_one merge lanes) now
    appends a durable merge-event line via _persist_merge_event on every
    successful union merge. Keep every merge-lane test's write inside tmp so
    it never touches the real mind_api/state/owncloud-merge-events.jsonl."""
    monkeypatch.setattr(
        _mod, "_merge_events_path",
        lambda: tmp_path / "owncloud-merge-events.jsonl")


def _jl(*records) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


class MergeBackend(FakeBackend):
    """FakeBackend + the real merge_put contract (OwnCloudBackend.merge_put):
    union via the REAL registered handler for the basename, merged bytes land
    on BOTH fake-S3 and the local file, non-None on success, None when the
    store has no registered handler."""

    def __init__(self, roots):
        super().__init__(roots)
        self.merge_puts = []

    def merge_put(self, path, content):
        from owncloud_backend import _coordination_merge_handler
        handler = _coordination_merge_handler(path)
        if handler is None:
            return None
        remote = self.s3.get(str(path)) or b""
        merged = handler(content, remote)  # handler(local, remote) -> bytes
        self.s3[str(path)] = merged
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(merged)
        self.merge_puts.append(str(path))
        return "merged"


BASE = {"date": "2026-07-01", "event": "base", "details": "d"}
MINE = {"date": "2026-07-02", "event": "local-append", "details": "x"}
PEER = {"date": "2026-07-03", "event": "peer-append", "details": "y"}


def _lines(b: bytes) -> set:
    return {ln for ln in b.splitlines() if ln.strip()}


def test_sync_one_diverged_merges_registered(tmp_path):
    """Both moved since baseline + registered store -> union, not the conflict
    skip (the g-115-2006 sweep-side reconcile; without it one peer append
    between flushes wedged append-only stores forever — the cc-02 g-115-2001
    class)."""
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"  # registered: line-union
    f.write_bytes(_jl(BASE, MINE))
    be.s3[str(f)] = _jl(BASE, PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(_jl(BASE)), multi_machine=True)
    assert stats.get("diverged_merged") == 1
    assert stats.get("diverged_skipped") in (None, 0)
    assert "conflict_paths" not in stats
    assert be.merge_puts == [str(f)]
    # Union: no side's records dropped; both sides converged byte-identical.
    assert _lines(be.s3[str(f)]) == _lines(_jl(BASE, MINE, PEER))
    assert f.read_bytes() == be.s3[str(f)]
    # Merged md5 becomes the new baseline -> next sweep reads in-sync.
    assert out == _md5(f.read_bytes())
    assert stats["merge_events"] == [{"file": str(f), "lane": "diverged_merged"}]


def test_sync_one_nobaseline_owncloud_authority_merges(tmp_path):
    """No baseline + own-cloud authority + registered store -> UNION, not the
    wholesale S3 pull (g-115-2297: adopting S3 drops a locally-authored-but-
    unpushed tail — the LocalBackend-degraded-append lane behind the cc-02
    gate-firings franken-copy)."""
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"
    f.write_bytes(_jl(MINE))
    be.s3[str(f)] = _jl(PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=None, multi_machine=True,
                         own_cloud_authority=True)
    assert stats.get("nobaseline_merged") == 1
    assert stats.get("nobaseline_reconciled") in (None, 0)  # merge, not pull
    assert stats.get("nobaseline_skipped") in (None, 0)
    assert _lines(f.read_bytes()) == _lines(_jl(MINE, PEER))
    assert f.read_bytes() == be.s3[str(f)]
    assert out == _md5(f.read_bytes())
    assert stats["merge_events"] == [
        {"file": str(f), "lane": "nobaseline_merged"}]


class _MultipartMergeBackend(MergeBackend):
    def stat(self, path):
        st = super().stat(path)
        if st is None:
            return None
        return FileStat(version='"deadbeef-3"', size=st.size, mtime_ns=0)


def test_sync_one_multipart_merges_registered(tmp_path):
    """Multipart S3 ETag + registered store -> union instead of the
    forever-defer (g-115-2474: the baseline classification multipart denies is
    unnecessary for a commutative merge, safe in every divergence sub-case —
    the capability the pre-ac3730ea31d7 lineage had, restored through
    _try_merge_put)."""
    be = _MultipartMergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"
    f.write_bytes(_jl(MINE))
    be.s3[str(f)] = _jl(PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=True)
    assert stats.get("multipart_merged") == 1
    assert stats.get("multipart_deferred") in (None, 0)
    assert be.merge_puts == [str(f)]
    assert _lines(f.read_bytes()) == _lines(_jl(MINE, PEER))
    assert f.read_bytes() == be.s3[str(f)]
    assert out == _md5(f.read_bytes())
    assert stats["merge_events"] == [
        {"file": str(f), "lane": "multipart_merged"}]


def test_sync_one_multipart_unregistered_still_defers(tmp_path):
    """Negative control for : multipart + UNREGISTERED store keeps
    the defer exactly (no merge attempt, neither side touched)."""
    be = _MultipartMergeBackend([(tmp_path, "world")])
    f = tmp_path / "some-unregistered.jsonl"
    f.write_bytes(_jl(MINE))
    be.s3[str(f)] = _jl(PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=True)
    assert out is None
    assert stats.get("multipart_deferred") == 1
    assert stats.get("multipart_merged") in (None, 0)
    assert be.merge_puts == []              # merge lane did NOT engage
    assert be.s3[str(f)] == _jl(PEER)       # neither side touched
    assert f.read_bytes() == _jl(MINE)


def test_sync_one_unregistered_diverged_keeps_skip(tmp_path):
    """Negative control: an UNREGISTERED store with both-moved divergence keeps
    the clobber-safe conflict skip — the merge lanes must not leak past the
    coordination_merge registry."""
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "some-unregistered.jsonl"
    f.write_bytes(_jl(BASE, MINE))
    be.s3[str(f)] = _jl(BASE, PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(_jl(BASE)), multi_machine=True)
    assert out is None
    assert stats.get("diverged_skipped") == 1
    assert stats.get("conflict_paths") == [str(f)]
    assert stats.get("diverged_merged") in (None, 0)
    assert be.merge_puts == []
    assert be.s3[str(f)] == _jl(BASE, PEER)  # S3 untouched
    assert f.read_bytes() == _jl(BASE, MINE)  # local untouched


def test_diverged_no_merge_put_backend_is_distinguishable(tmp_path, capsys):
    """: a backend WITHOUT merge_put reaching a registered both-diverged
    file records a DISTINCT counter + a labeled CONFLICT line — no longer
    indistinguishable from a genuinely-unregistered skip (the undiagnosable
    2026-07-20 cc-02 shape)."""
    be = FakeBackend([(tmp_path, "world")])  # no merge_put attribute
    assert not hasattr(be, "merge_put")
    f = tmp_path / "evolution-log.jsonl"  # registered store
    f.write_bytes(_jl(BASE, MINE))
    be.s3[str(f)] = _jl(BASE, PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(_jl(BASE)), multi_machine=True)
    assert out is None
    assert stats.get("diverged_skipped") == 1
    # Distinct counter + reason set by _try_merge_put (not a silent bail).
    assert stats.get("merge_na_no_merge_put") == 1
    assert stats.get("last_merge_na_reason") == "backend-lacks-merge_put"
    # The CONFLICT line now NAMES why the merge lane bailed.
    err = capsys.readouterr().err
    assert "merge-lane NA: backend-lacks-merge_put" in err
    assert "CONFLICT" in err


def test_diverged_unregistered_records_distinct_reason(tmp_path, capsys):
    """: an unregistered store gets a DIFFERENT reason label
    ('not-merge-registered') than a merge-eligible bail — 'merge-attempt-failed
    vs unregistered print different lines' (the deliverable). merge_put EXISTS
    here (MergeBackend) but the basename has no handler."""
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "some-unregistered.jsonl"
    f.write_bytes(_jl(BASE, MINE))
    be.s3[str(f)] = _jl(BASE, PEER)
    stats = _new_stats()
    _mod._sync_one(be, f, dry_run=False, stats=stats,
                   baseline_md5=_md5(_jl(BASE)), multi_machine=True)
    assert stats.get("merge_na_unregistered") == 1
    assert stats.get("last_merge_na_reason") == "not-merge-registered"
    err = capsys.readouterr().err
    assert "merge-lane NA: not-merge-registered" in err
    # A registered-but-no-merge_put bail (other test) says a DIFFERENT reason,
    # so the two failure modes are now distinguishable from the log alone.


def test_sync_print_carries_iso_timestamp(capsys):
    """ / : every [sync] line carries a naive-UTC ISO
    timestamp prefix so forensics can reconstruct arrival/sweep timing."""
    import re
    _mod._sync_print("[sync] canary line")
    out = capsys.readouterr().out
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[sync\] canary line",
                    out.strip()), f"missing ISO prefix: {out!r}"
    # stderr routing preserved (the merge/conflict lines use file=sys.stderr).
    _mod._sync_print("[sync] err canary", file=sys.stderr)
    err = capsys.readouterr().err
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[sync\] err canary",
                    err.strip())



# ── : the merge lane must not swallow a NON-diverged push ──────────
#
# THE LANE SPLIT THIS SECTION PINS. `_try_merge_put` at the final push block ran
# on `if st is not None:` alone, so EVERY push with an object on S3 took the
# union — including the case where classification had just PROVEN S3 sits at the
# baseline and nothing diverged. The  rationale it was written for (a
# stale local TAIL while peers appended to S3) is real, but strictly NARROWER
# than the guard: a peer append moves S3 off the baseline, so the case it
# protects is `s3_at_baseline == False` and is untouched here.
#
# WHICH CALLER REACHES WHICH LANE (measured, owncloud_sync.py):
#   sync_file (PostToolUse, L2118)  -> _sync_one WITHOUT baseline_md5 -> None
#                                      -> s3_at_baseline False -> MERGE (kept)
#   periodic sweep (L1752 / L1803)  -> baseline_md5 from the manifest
#                                      -> s3_at_baseline may be True -> fenced put
# The 2026-07-16 03:09:14 gate-firings incident the merge was written for came
# from the sync_file lane, which passes no baseline — so it keeps the union.
# test_sync_file_lane_without_baseline_still_merges below is its regression pin.


def test_sync_one_s3_at_baseline_takes_fenced_put_not_merge(tmp_path):
    """S3 proven AT BASELINE + local changed -> fenced plain PUT, not the union.

    s3_at_baseline is True exactly when S3's content equals the baseline this
    box last reconciled, i.e. no peer has written since. The merge handler is a
    BYTE-SURVIVAL guarantee (guard-2471) and here there are no peer bytes to
    survive, so the union has nothing to reconcile and the CAS put is correct.
    """
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"          # registered: line-union
    s3_content = _jl(BASE)
    f.write_bytes(_jl(BASE, MINE))                # local changed
    be.s3[str(f)] = s3_content                    # S3 still at baseline
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(s3_content), multi_machine=True)
    assert stats["pushed"] == 1
    assert stats.get("pushed_merged") in (None, 0)
    assert be.merge_puts == []                    # union lane did NOT engage
    assert be.puts == [str(f)]
    # Same bytes either way in this fixture — only the LANE differs.
    assert _lines(be.s3[str(f)]) == _lines(_jl(BASE, MINE))
    assert out == _md5(f.read_bytes())


def test_s3_at_baseline_local_deletion_is_not_resurrected(tmp_path):
    """THE WEDGE ITSELF: a local REMOVAL must survive the push.

    When local's change is a removal (prune / vacuum / cap-roll) rather than an
    append, the union re-adds the removed record from S3, so local never
    converges — the merged md5 becomes the baseline while local still differs,
    and the SAME divergence re-evaluates every sweep forever. This is the
    own-cloud lane-B wedge measured on cc-08 2026-08-26.
    """
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"
    s3_content = _jl(BASE, PEER)                  # baseline == S3
    f.write_bytes(_jl(BASE))                      # local PRUNED the PEER record
    be.s3[str(f)] = s3_content
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(s3_content), multi_machine=True)
    # The removal STICKS on both sides...
    assert _lines(be.s3[str(f)]) == _lines(_jl(BASE))
    assert _lines(f.read_bytes()) == _lines(_jl(BASE))
    # ...and the returned baseline equals local, so the next sweep reads
    # in-sync instead of re-evaluating the same divergence forever.
    assert out == _md5(f.read_bytes())
    assert stats["pushed"] == 1


def test_sync_file_lane_without_baseline_still_merges(tmp_path):
    """REGRESSION PIN for 2026-07-16 03:09:14 ().

    The PostToolUse sync_file lane calls _sync_one with NO baseline_md5 and
    multi_machine=False — the exact shape that replaced a newer gate-firings S3
    head. s3_at_baseline is False there, so the union MUST still engage and the
    peer's records must survive. If this ever goes red, the g-115-7944 gate has
    been widened past the lane it was scoped to.
    """
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"
    f.write_bytes(_jl(BASE, MINE))                # local (stale tail)
    be.s3[str(f)] = _jl(BASE, PEER)               # peer appended
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=None, multi_machine=False)
    assert stats.get("pushed_merged") == 1
    assert stats["pushed"] == 0
    assert be.merge_puts == [str(f)]
    assert _lines(be.s3[str(f)]) == _lines(_jl(BASE, MINE, PEER))  # PEER survives
    assert out == _md5(f.read_bytes())


def test_multipart_single_machine_reaches_push_without_unbound_name(tmp_path):
    """PATH-A TRAP: s3_at_baseline is assigned ONLY inside the `elif st is not
    None` branch, so the single-machine multipart fall-through reaches the push
    block with that name never bound. A gate written as `not s3_at_baseline`
    without a pre-initialisation raises UnboundLocalError on exactly this path.
    Behaviour must be UNCHANGED from before the gate: the union still engages.
    """
    be = _MultipartMergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"
    f.write_bytes(_jl(MINE))
    be.s3[str(f)] = _jl(PEER)
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(b"v1"), multi_machine=False)
    assert stats.get("pushed_merged") == 1
    assert be.merge_puts == [str(f)]
    assert _lines(f.read_bytes()) == _lines(_jl(MINE, PEER))
    assert out == _md5(f.read_bytes())


def test_merge_and_fenced_put_lanes_are_not_collapsed(tmp_path):
    """Anti-vacuity: the two PUSH-BLOCK arms must not collapse into one.

    Both arms below reach the final push block with identical local bytes and an
    object on S3; the ONLY thing varied is what the gate reads — whether the
    baseline proves S3 has not moved. A gate flattened in either direction
    (always merge, or never merge) passes each single-lane test above on its own
    while being useless; this is the assertion that fails if they are collapsed.

    Note the comparison is deliberately WITHIN the push block. Varying "has S3
    moved?" with a baseline present does NOT work as the contrast: that case is
    caught earlier by the both-diverged branch (`diverged_merged`) and never
    reaches this gate at all — measured, and the reason the first draft of this
    test was wrong.
    """
    lanes = []
    for baseline_present in (True, False):
        be = MergeBackend([(tmp_path, "world")])
        f = tmp_path / "evolution-log.jsonl"
        f.write_bytes(_jl(BASE, MINE))
        be.s3[str(f)] = _jl(BASE)
        stats = _new_stats()
        _mod._sync_one(be, f, dry_run=False, stats=stats,
                       baseline_md5=_md5(_jl(BASE)) if baseline_present else None,
                       multi_machine=False)
        lanes.append(("merged" if be.merge_puts else "fenced_put",
                      stats["pushed"], stats.get("pushed_merged") or 0))
    assert lanes[0] != lanes[1], f"lanes collapsed: {lanes}"
    assert lanes[0] == ("fenced_put", 1, 0)   # S3 proven at baseline -> CAS put
    assert lanes[1] == ("merged", 0, 1)       # no baseline -> union (preserved)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
