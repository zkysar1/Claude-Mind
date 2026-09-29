"""The own-cloud mirror must survive a held file and a long path.

g-115-7257 — on Windows os.replace raises PermissionError (WinError 5) while
another process holds the target open. The sweep's push re-wrote the local file
with the bytes it had just read from it; when a scheduled job was appending to
that file, the rewrite raised AFTER the PUT succeeded, the baseline was never
advanced, and the file sat both-diverged for good (daily-cost-report.log, 595
and 994 sweeps). Now the push never rewrites a file it read from, and every
other local publish retries through storage_backend.replace_with_retry.

g-115-11323 — on a Windows box without long paths, a plain path of 248+ chars
cannot be written (the mkstemp sibling and the parent dir both overflow) and
one of 260+ cannot be read at all. 14 deep tree nodes never reached
DESKTOP-O91DLK2. Local I/O now goes through _long_path.long_path.

Negative controls: every test marked PRE-FIX below was run against the HEAD
modules before the fix and failed there (recorded in the g-115-7257 and
g-115-11323 goal outcomes).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from _long_path import long_path  # noqa: E402

ENV_ID = "test-env"
BUCKET = "test-bucket"
LOCKS = "test-locks"
SESSIONS = "test-sessions"
REGION = "us-west-2"

_real_replace = os.replace


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _deep_file(base: Path, total_len: int) -> Path:
    """A path under `base` whose absolute form is exactly `total_len` chars:
    40-char directories, then a filename padded to land on the length."""
    cur = Path(os.path.abspath(base))
    i = 0
    while total_len - len(str(cur)) - 1 > 48:      # room for "\" + a <=48-char name
        cur = cur / f"d{i:02d}{'x' * 37}"
        i += 1
    pad = total_len - len(str(cur)) - 1 - len("node.md")
    p = cur / f"node{'y' * pad}.md"
    assert pad >= 0 and len(str(p)) == total_len, (len(str(p)), total_len)
    return p


@pytest.fixture
def no_sleep(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)


def _replace_failing(n_failures, exc=None):
    """os.replace that raises `exc` (default PermissionError) n times, then works."""
    calls = {"n": 0}

    def fake(src, dst):
        calls["n"] += 1
        if calls["n"] <= n_failures:
            raise exc or PermissionError(13, "Access is denied")
        return _real_replace(src, dst)
    return fake, calls


# ── : the local publish retries a held target ────────────────────

def test_atomic_write_local_retries_a_held_target(tmp_path, monkeypatch, no_sleep):
    """PRE-FIX: raised on the first PermissionError."""
    import owncloud_backend as ob
    target = tmp_path / "world" / "store.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old\n")
    fake, calls = _replace_failing(2)
    monkeypatch.setattr(os, "replace", fake)
    ob._atomic_write_local(target, b"new\n")
    assert target.read_bytes() == b"new\n"
    assert calls["n"] == 3
    assert [p.name for p in target.parent.iterdir()] == ["store.jsonl"], \
        "a retried publish left its temp file behind"


def test_atomic_write_local_gives_up_cleanly(tmp_path, monkeypatch, no_sleep):
    """Retries run out: raise, keep the old bytes, leave no temp. No in-place
    fallback, because that is the truncate window the helper exists to close."""
    import owncloud_backend as ob
    target = tmp_path / "store.jsonl"
    target.write_bytes(b"old\n")
    fake, calls = _replace_failing(10 ** 6)
    monkeypatch.setattr(os, "replace", fake)
    with pytest.raises(PermissionError):
        ob._atomic_write_local(target, b"new\n")
    assert target.read_bytes() == b"old\n"
    assert calls["n"] == 10
    assert [p.name for p in tmp_path.iterdir()] == ["store.jsonl"]


def test_runtime_file_write_retries_a_held_target(tmp_path, monkeypatch, no_sleep):
    """The manifest and the conflict streaks are read by other processes while
    the sweep replaces them. PRE-FIX: the manifest save warned and dropped the
    whole sweep's baselines on the first PermissionError."""
    import owncloud_sync as ocs
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    fake, calls = _replace_failing(2)
    monkeypatch.setattr(os, "replace", fake)
    ocs._save_manifest({"world/a.md": {"mtime": 1, "md5": "x"}})
    assert ocs._load_manifest() == {"world/a.md": {"mtime": 1, "md5": "x"}}
    assert calls["n"] == 3
    assert [p.name for p in tmp_path.iterdir()] == ["owncloud-sync-manifest.json"]


# ── : the sweep's push never rewrites the file it read ──────────

@pytest.fixture
def cloud(monkeypatch, tmp_path):
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("MACHINE_ID", "test-machine-ci")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "_owncloud_rt"))
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb.create_table(
            TableName=LOCKS,
            KeySchema=[{"AttributeName": "lock_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "lock_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        ddb.create_table(
            TableName=SESSIONS,
            KeySchema=[{"AttributeName": "session_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        yield {"s3": s3, "ddb": ddb, "root": tmp_path}


def _backend(cloud):
    from owncloud_backend import OwnCloudBackend
    return OwnCloudBackend(
        env_id=ENV_ID, bucket=BUCKET, lock_table=LOCKS,
        sessions_table=SESSIONS, cache_root=cloud["root"],
        machine_id="m1", region=REGION, s3=cloud["s3"], ddb=cloud["ddb"])


def _s3_bytes(cloud, b, p) -> bytes:
    return cloud["s3"].get_object(Bucket=BUCKET, Key=b._s3_key(p))["Body"].read()


@pytest.fixture
def hold(monkeypatch):
    """Call it to make every local-mirror publish fail the way Windows fails
    it while a scheduled job holds the file open for appending."""
    import owncloud_backend as ob

    def locked(local, body):
        raise PermissionError(13, "Access is denied (held by an appender)")
    return lambda: monkeypatch.setattr(ob, "_atomic_write_local", locked)


def test_the_simulated_hold_bites_a_rewriting_put(cloud, hold):
    """Positive control for `hold`: a put that DOES rewrite the local file
    fails under it, exactly as the pre-fix sweep push did."""
    hold()
    b = _backend(cloud)
    p = cloud["root"] / "world" / "scripts" / "job.log"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"line 1\n")
    with pytest.raises(PermissionError):
        b.mirror_put(p, b"line 1\n")


def test_mirror_put_from_the_sweep_leaves_the_local_file_alone(cloud, hold):
    """local_is_source=True: S3 gets the bytes, the file is not touched, and
    the baseline carries no mtime (an mtime read now could post-date `body`)."""
    import owncloud_sync as ocs
    hold()
    b = _backend(cloud)
    p = cloud["root"] / "world" / "scripts" / "job.log"
    p.parent.mkdir(parents=True)
    read = b"line 1\n"
    p.write_bytes(read + b"line 2 appended after the read\n")
    b.mirror_put(p, read, local_is_source=True)
    assert _s3_bytes(cloud, b, p) == read
    assert p.read_bytes() == read + b"line 2 appended after the read\n", \
        "the push reverted an append that landed after the read"
    entry = ocs._load_manifest()[b._rel(p)]
    assert entry["md5"] == _md5(read) and entry.get("etag")
    assert "mtime" not in entry


def test_mirror_put_default_still_writes_the_local_file(cloud):
    """The hand-made-union repair (guard-4778, rb-9443) pushes bytes that
    DIFFER from the local file and relies on them landing locally too."""
    b = _backend(cloud)
    p = cloud["root"] / "world" / "knowledge" / "tree" / "n.md"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"local only\n")
    union = b"local only\npeer only\n"
    b.mirror_put(p, union)
    assert p.read_bytes() == union
    assert _s3_bytes(cloud, b, p) == union


def test_sweep_push_of_a_held_file_advances_the_baseline(cloud, hold):
    """THE WEDGE. S3 at baseline, local appended to -> local-changed-only ->
    push. PRE-FIX: the post-PUT rewrite raised, _sync_one returned None, the
    baseline stayed put while S3 moved, and the next append made the file
    both-diverged forever. Now the push lands and returns the pushed md5."""
    import owncloud_sync as ocs
    b = _backend(cloud)
    p = cloud["root"] / "world" / "scripts" / "job.log"
    v1 = b"run 1\n"
    b.write_bytes(p, v1)                                # S3 == local == v1
    assert not b._machine_local(p)
    v2 = v1 + b"run 2\n"
    p.write_bytes(v2)                                   # the scheduled append
    hold()                                              # ...still holding it
    stats = {"scanned": 0, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    got = ocs._sync_one(b, p, dry_run=False, stats=stats, baseline_md5=_md5(v1),
                        multi_machine=True, own_cloud_authority=True)
    assert got == _md5(v2), f"push did not land: {stats}"
    assert stats["pushed"] == 1 and stats["errors"] == 0
    assert _s3_bytes(cloud, b, p) == v2


# ── : long paths ────────────────────────────────────────────────

def test_long_path_is_identity_below_the_threshold(tmp_path):
    p = tmp_path / "short.md"
    assert long_path(p) == p


@pytest.mark.skipif(os.name != "nt", reason="the prefix is Windows-only")
def test_long_path_prefixes_long_windows_paths(tmp_path):
    p = _deep_file(tmp_path, 270)
    lp = long_path(p)
    assert str(lp) == "\\\\?\\" + str(p)
    assert long_path(lp) == lp, "not idempotent"
    unc = "\\\\server\\share\\" + "d" * 250 + "\\x.md"
    assert str(long_path(unc)) == "\\\\?\\UNC\\server\\share\\" + "d" * 250 + "\\x.md"


@pytest.mark.skipif(os.name == "nt", reason="identity off Windows")
def test_long_path_is_identity_off_windows(tmp_path):
    p = _deep_file(tmp_path, 300)
    assert long_path(p) == p


@pytest.fixture
def deep_root(tmp_path):
    """tmp_path, removed through the long-path spelling afterwards: pytest's
    own cleanup cannot delete what it cannot address."""
    yield tmp_path
    shutil.rmtree(long_path(tmp_path), ignore_errors=True)


@pytest.mark.parametrize("length", [252, 275])
def test_atomic_write_local_materializes_past_max_path(deep_root, length):
    """252: the target itself is under 260 but its mkstemp sibling is not (11
    of the 14 stuck nodes). 275: the target is past 260 too. PRE-FIX: errno 2
    on Windows without long paths."""
    import owncloud_backend as ob
    target = _deep_file(deep_root, length)
    ob._atomic_write_local(target, b"# node\n")
    assert long_path(target).read_bytes() == b"# node\n"


class _PullBackend:
    """What _pull_one touches: stat() (the HEAD) and refresh() (the GET),
    materializing through the real backend writer."""

    def __init__(self, body: bytes):
        self.body = body

    def stat(self, full):
        class _St:
            version = '"' + _md5(self.body) + '"'
            plain_md5 = None
        return _St()

    def refresh(self, full):
        import owncloud_backend as ob
        ob._atomic_write_local(Path(full), self.body)


def test_pull_one_lands_and_baselines_a_long_path(deep_root):
    """PRE-FIX: the refresh failed errno 2 (the pull's `errors 14`), and even a
    landed file read back as unreadable, so no baseline was ever recorded."""
    import owncloud_sync as ocs
    full = _deep_file(deep_root, 275)
    stats = {"pulled": 0, "in_sync": 0, "s3_absent": 0, "would_pull": 0,
             "errors": 0}
    got = ocs._pull_one(_PullBackend(b"# deep node\n"), full, dry_run=False,
                        stats=stats)
    assert got == _md5(b"# deep node\n"), stats
    assert stats["pulled"] == 1 and stats["errors"] == 0
    # A second pull is a no-op: the local copy is visible and current.
    stats2 = {"pulled": 0, "in_sync": 0, "s3_absent": 0, "errors": 0}
    ocs._pull_one(_PullBackend(b"# deep node\n"), full, dry_run=False,
                  stats=stats2, baseline_md5=got)
    assert stats2["in_sync"] == 1 and stats2["pulled"] == 0


class _PushBackend:
    def __init__(self):
        self.puts = {}

    def stat(self, full):
        return None                      # absent in the store -> push

    def mirror_put(self, path, content, *, expected_version=None,
                   local_is_source=False):
        self.puts[str(path)] = content


def test_sync_one_reads_and_pushes_a_long_path(deep_root):
    """PRE-FIX: 'unreadable' on the first read, so a local edit to a deep node
    never pushed."""
    import owncloud_sync as ocs
    full = _deep_file(deep_root, 275)
    ob_body = b"# edited on this box\n"
    long_path(full).parent.mkdir(parents=True, exist_ok=True)
    long_path(full).write_bytes(ob_body)
    be = _PushBackend()
    stats = {"scanned": 0, "in_sync": 0, "pushed": 0, "would_push": 0,
             "conflicts": 0, "errors": 0, "push_paths": []}
    got = ocs._sync_one(be, full, dry_run=False, stats=stats)
    assert got == _md5(ob_body), stats
    assert be.puts == {str(full): ob_body}, "the store key must use the plain path"


# ── : the pull records its errors for mirror-health ─────────────

class _ListBackend:
    def __init__(self, root: Path, rels):
        self._roots = [(str(root), "world")]
        self._rels = list(rels)

    def list_objects(self, root_path):
        return [(r, f"etag-{i}", 10) for i, r in enumerate(self._rels)]


def _pull(tmp_path, monkeypatch, failing, **kw):
    import owncloud_sync as ocs

    def fake_pull_one(be, full, *, dry_run, stats, baseline_md5=None):
        if Path(full).name in failing:
            ocs._record_error(stats, full, FileNotFoundError(2, "No such file"),
                              phase="pull-refresh")
            return None
        stats["pulled"] += 1
        return None

    monkeypatch.setattr(ocs, "_pull_one", fake_pull_one)
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    return ocs.pull_sweep(_ListBackend(world, ["a.md", "deep/b.md"]), **kw)


def test_pull_sweep_records_and_then_clears_its_errors(tmp_path, monkeypatch):
    import json
    import owncloud_sync as ocs
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    _pull(tmp_path, monkeypatch, failing={"b.md"})
    doc = json.loads(ocs._pull_errors_path().read_text(encoding="utf-8"))
    assert doc["errors"] == 1
    assert [e["path"] for e in doc["error_paths"]] == [
        str(tmp_path / "world" / "deep" / "b.md")]
    _pull(tmp_path, monkeypatch, failing=set())
    doc = json.loads(ocs._pull_errors_path().read_text(encoding="utf-8"))
    assert doc["errors"] == 0 and doc["error_paths"] == [], \
        "a clean pull must clear the previous pull's errors"


@pytest.mark.parametrize("kw", [{"dry_run": True}, {"only_root": "world"}])
def test_partial_or_dry_pull_leaves_the_errors_file_alone(tmp_path, monkeypatch, kw):
    """A dry run changes nothing, and a --root pull cannot vouch for the other
    root, so neither may overwrite (and so clear) the record."""
    import owncloud_sync as ocs
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path / "rt"))
    _pull(tmp_path, monkeypatch, failing=set(), **kw)
    assert not ocs._pull_errors_path().exists()
