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
  C  local-authoritative change     -> pushed_merged lane engages (union, never
     a blind whole-object PUT when S3 holds an object)
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


def test_sync_one_local_change_pushes_via_merge(tmp_path):
    """Local-authoritative change (S3 still at baseline) + registered store ->
    the push routes through the union, never a blind whole-object PUT: even a
    'confirmed local change' can carry a stale TAIL while peers appended to S3
    (the PostToolUse sync_file lane that replaced the newer gate-firings S3
    head, 2026-07-16 03:09:14)."""
    be = MergeBackend([(tmp_path, "world")])
    f = tmp_path / "evolution-log.jsonl"
    s3_content = _jl(BASE)
    f.write_bytes(_jl(BASE, MINE))          # local changed
    be.s3[str(f)] = s3_content              # S3 at baseline
    stats = _new_stats()
    out = _mod._sync_one(be, f, dry_run=False, stats=stats,
                         baseline_md5=_md5(s3_content), multi_machine=True)
    assert stats.get("pushed_merged") == 1
    assert stats["pushed"] == 0             # no blind mirror_put
    assert be.puts == []
    assert _lines(be.s3[str(f)]) == _lines(_jl(BASE, MINE))
    assert out == _md5(f.read_bytes())
    assert stats["merge_events"] == [{"file": str(f), "lane": "pushed_merged"}]


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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
