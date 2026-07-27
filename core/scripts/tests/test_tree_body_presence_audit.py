"""Targeted tests for tree-body-presence-audit.py.

Covers the two behaviors the goal's verification checks name:
  1. local-backend no-op (never calls stat())
  2. the 4-way local x remote classification (synced / local_only /
     cache_miss / desync) via a fake remote backend.

Fake backends inject stat() results, so no real store is touched. guard-955:
run under STORAGE_BACKEND=local (the conftest autouse pin covers this; the
fake backends make it moot anyway).
"""
import importlib.util
import io
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "tree-body-presence-audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("tree_body_presence_audit", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeStat:
    """Truthy stand-in for a present remote object."""


class _FakeRemoteBackend:
    """Remote-synced backend whose stat() returns None (404) for a configured set."""
    name = "own-cloud"

    def __init__(self, absent_paths):
        self._absent = {str(p) for p in absent_paths}

    def stat(self, path):
        if str(path) in self._absent:
            return None
        return _FakeStat()


class _FakeLocalBackend:
    name = "local"

    def stat(self, path):  # pragma: no cover - must never be reached
        raise AssertionError("stat() must never be called on the local backend (no-op expected)")


class _FakeS3Backend(_FakeRemoteBackend):
    """Remote backend that also exposes the S3 surface the orphan scan uses.

    ``remote_registry=None`` makes get_object raise, exercising the degraded
    path where the authoritative registry is unreadable.
    """

    def __init__(self, absent_paths, tree_dir, s3_md_names=(), remote_registry=None):
        super().__init__(absent_paths)
        self._tree_dir = Path(tree_dir)
        self._s3_md = list(s3_md_names)
        self._remote_registry = remote_registry
        self.bucket = "fake-bucket"
        outer = self

        class _S3:
            @staticmethod
            def get_object(Bucket=None, Key=None):
                if outer._remote_registry is None:
                    raise RuntimeError("AccessDenied")
                return {"Body": io.BytesIO(yaml.safe_dump(outer._remote_registry).encode())}

            @staticmethod
            def list_objects_v2(Bucket=None, Prefix=None, **kw):
                return {"Contents": [{"Key": Prefix + n} for n in outer._s3_md],
                        "IsTruncated": False}

        self.s3 = _S3()

    def _s3_key(self, p):
        p = Path(p)
        if p == self._tree_dir:
            return "tree"
        return "tree/" + str(p.relative_to(self._tree_dir)).replace("\\", "/")


def _write_tree(world_dir, nodes):
    """nodes: dict key -> file_field ('world/...' path) or None for a no-file node."""
    tree_dir = world_dir / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    ndict = {}
    for k, ff in nodes.items():
        ndict[k] = {"file": ff} if ff else {"parent": "root"}
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump({"nodes": ndict}))
    return tree_dir


def test_local_backend_is_noop(tmp_path):
    mod = _load_module()
    _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    result = mod.scan(str(tmp_path), str(tmp_path), backend=_FakeLocalBackend())
    assert result["local_noop"] is True
    assert result["backend"] == "local"
    assert "counts" not in result  # early return — never classified, never HEADed


def test_four_way_classification(tmp_path):
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {
        "synced_node": "world/knowledge/tree/synced.md",
        "localonly_node": "world/knowledge/tree/localonly.md",
        "cachemiss_node": "world/knowledge/tree/cachemiss.md",
        "desync_node": "world/knowledge/tree/desync.md",
        "root": None,  # no file -> no_file_nodes
    })
    # Local bodies present for synced + localonly; absent for cachemiss + desync.
    (tree_dir / "synced.md").write_text("x")
    (tree_dir / "localonly.md").write_text("x")
    # Remote returns 404 for localonly (never pushed) + desync (absent everywhere).
    backend = _FakeRemoteBackend(absent_paths=[tree_dir / "localonly.md", tree_dir / "desync.md"])

    result = mod.scan(str(tmp_path), str(tmp_path), backend=backend)

    assert result["local_noop"] is False
    c = result["counts"]
    assert c["synced"] == 1
    assert c["local_only"] == 1
    assert c["cache_miss"] == 1
    assert c["desync"] == 1
    assert c["probe_error"] == 0
    assert result["no_file_nodes"] == 1
    assert result["total_with_file"] == 4
    assert {r["key"] for r in result["desync"]} == {"desync_node"}
    assert {r["key"] for r in result["local_only"]} == {"localonly_node"}


def test_probe_error_bucket(tmp_path):
    """A backend whose stat() raises lands the node in probe_error, not a false desync."""
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")

    class _RaisingBackend:
        name = "own-cloud"

        def stat(self, path):
            raise RuntimeError("transient HEAD failure")

    result = mod.scan(str(tmp_path), str(tmp_path), backend=_RaisingBackend())
    assert result["counts"]["probe_error"] == 1
    assert result["counts"]["desync"] == 0
    assert result["probe_error"][0]["key"] == "n1"


# ── the MIRROR direction: bodies present on a lane, absent from the registry ──


def test_orphan_scan_runs_on_local_backend(tmp_path):
    """The local no-op skips the 4-way scan but NOT the local orphan diff.

    An index-vs-mirror diff needs no remote, so it stays meaningful here;
    s3_unregistered reports None ("not probeable"), never a misleading [].
    """
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")
    (tree_dir / "stray.md").write_text("x")            # genuine orphan
    (tree_dir / ".archive").mkdir()
    (tree_dir / ".archive" / "snap.md").write_text("x")  # deliberately unregistered

    orph = mod.scan(str(tmp_path), str(tmp_path), backend=_FakeLocalBackend())["orphans"]
    assert orph["local_unregistered"] == ["stray.md"]
    assert orph["local_unregistered_archived"] == 1     # counted, never a finding
    assert orph["s3_unregistered"] is None
    assert orph["registry_source"] == "local"


def test_stale_local_registry_produces_no_phantom_orphan(tmp_path):
    """A body registered ONLY in the AUTHORITATIVE registry is not an orphan.

    Regression guard for the live 2026-07-26 false positive: under read-through
    caching the mirror's _tree.yaml lags the store, so diffing S3 bodies against
    the LOCAL registry alone reported a newly-registered node as an S3 orphan
    for the ~2min until the mirror caught up.
    """
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"old": "world/knowledge/tree/old.md"})
    (tree_dir / "old.md").write_text("x")
    # The store knows a second node this mirror has not pulled into _tree.yaml yet.
    backend = _FakeS3Backend(
        absent_paths=[], tree_dir=tree_dir,
        s3_md_names=["old.md", "fresh.md"],
        remote_registry={"nodes": {"old": {"file": "world/knowledge/tree/old.md"},
                                   "fresh": {"file": "world/knowledge/tree/fresh.md"}}})

    orph = mod.scan(str(tmp_path), str(tmp_path), backend=backend)["orphans"]
    assert orph["registry_source"] == "local+remote"
    assert orph["s3_unregistered"] == []   # fresh.md is registered remotely


def test_genuine_s3_orphan_still_detected(tmp_path):
    """Positive control: widening the baseline must not mask real orphans."""
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")
    backend = _FakeS3Backend(
        absent_paths=[], tree_dir=tree_dir,
        s3_md_names=["n1.md", "ghost.md", ".archive/snap.md"],
        remote_registry={"nodes": {"n1": {"file": "world/knowledge/tree/n1.md"}}})

    orph = mod.scan(str(tmp_path), str(tmp_path), backend=backend)["orphans"]
    assert orph["s3_unregistered"] == ["ghost.md"]
    assert orph["s3_unregistered_archived"] == 1


def test_orphan_failure_preserves_body_presence_result(tmp_path):
    """An additive-direction failure must NOT discard the completed 4-bucket scan.

    Regression guard for the g-115-3237 fresh-eyes finding: a transient
    list_objects_v2 throttle used to propagate out of scan() and throw away
    every successful HEAD (~1250 on the live tree).
    """
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")

    class _ThrottledListing(_FakeS3Backend):
        def __init__(self, tree_dir):
            super().__init__(absent_paths=[], tree_dir=tree_dir, s3_md_names=["n1.md"],
                             remote_registry={"nodes": {}})
            outer_s3 = self.s3

            class _S3:
                get_object = outer_s3.get_object

                @staticmethod
                def list_objects_v2(**kw):
                    raise RuntimeError("SlowDown: rate exceeded")
            self.s3 = _S3()

    result = mod.scan(str(tmp_path), str(tmp_path), backend=_ThrottledListing(tree_dir))

    assert result["counts"]["synced"] == 1           # primary result SURVIVES
    assert "SlowDown" in result["orphans"]["error"]  # failure recorded, not silent
    # the errored block reading as a finding is covered by
    # test_main_errored_orphan_block_is_not_a_clean_zero


def test_registry_source_carries_the_failure_cause(tmp_path):
    """A degraded registry read must say WHY, not just that it degraded."""
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")
    backend = _FakeS3Backend(absent_paths=[], tree_dir=tree_dir,
                             s3_md_names=["n1.md"], remote_registry=None)  # get_object raises

    src = mod.scan(str(tmp_path), str(tmp_path), backend=backend)["orphans"]["registry_source"]
    assert src.startswith("local (remote registry unreadable")
    assert "AccessDenied" in src   # the CAUSE reaches the reader


def test_main_errored_orphan_block_is_not_a_clean_zero(monkeypatch):
    """Suppression fails CLOSED: a failed probe must not exit 0 (guard-980/487)."""
    mod = _load_module()
    monkeypatch.setattr(mod, "assert_world_dir", lambda *a, **k: None)
    monkeypatch.setattr(mod, "scan", lambda *a, **k: {
        "backend": "own-cloud", "local_noop": False,
        "counts": {"synced": 9, "local_only": 0, "cache_miss": 0, "desync": 0, "probe_error": 0},
        "desync": [], "local_only": [], "probe_error": [],
        "orphans": {"error": "RuntimeError: SlowDown"}})
    assert mod.main(["--exit-on-findings", "--quiet"]) == 3


def test_main_exit_code_counts_orphans_but_not_archived(monkeypatch):
    """main() exit 3 includes NON-ARCHIVE orphans; archived hits are never findings.

    Covers the exit-code arithmetic itself (the branch a live run exercises but
    no test did): a local-backend run has no 4-way buckets at all, so its orphan
    findings are the only thing that can raise the exit code.
    """
    mod = _load_module()
    monkeypatch.setattr(mod, "assert_world_dir", lambda *a, **k: None)
    orph = {"registry_source": "local", "local_unregistered": ["stray.md"],
            "local_unregistered_archived": 3,
            "s3_unregistered": None, "s3_unregistered_archived": None}
    monkeypatch.setattr(mod, "scan",
                        lambda *a, **k: {"backend": "local", "local_noop": True, "orphans": orph})

    assert mod.main(["--exit-on-findings", "--quiet"]) == 3   # a local run CAN exit 3
    assert mod.main(["--quiet"]) == 0                          # ...only with the flag

    orph["local_unregistered"] = []                            # archive-only left
    assert mod.main(["--exit-on-findings", "--quiet"]) == 0    # 3 archived != a finding


def test_main_exit_code_sums_desync_atrisk_and_orphans(monkeypatch):
    """On a remote backend the three finding classes are summed, not shadowed."""
    mod = _load_module()
    monkeypatch.setattr(mod, "assert_world_dir", lambda *a, **k: None)
    result = {"backend": "own-cloud", "local_noop": False,
              "counts": {"synced": 5, "local_only": 0, "cache_miss": 0,
                         "desync": 0, "probe_error": 0},
              "desync": [], "local_only": [], "probe_error": [],
              "orphans": {"registry_source": "local+remote",
                          "local_unregistered": [], "local_unregistered_archived": 0,
                          "s3_unregistered": ["ghost.md"], "s3_unregistered_archived": 2}}
    monkeypatch.setattr(mod, "scan", lambda *a, **k: result)
    # orphan-only findings must still trip the exit code (desync/at-risk are 0)
    assert mod.main(["--exit-on-findings", "--quiet"]) == 3

    result["orphans"]["s3_unregistered"] = []
    assert mod.main(["--exit-on-findings", "--quiet"]) == 0


def test_unreadable_remote_registry_degrades_and_says_so(tmp_path):
    """An unreadable authoritative registry falls back to local and LABELS it."""
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")
    backend = _FakeS3Backend(absent_paths=[], tree_dir=tree_dir,
                             s3_md_names=["n1.md"], remote_registry=None)

    orph = mod.scan(str(tmp_path), str(tmp_path), backend=backend)["orphans"]
    # Degraded, and SAYS so (the exact cause string is asserted by
    # test_registry_source_carries_the_failure_cause — not duplicated here).
    assert orph["registry_source"].startswith("local (remote registry unreadable")
    # The point of this test: the S3 lane still ENUMERATES on the degraded path
    # rather than erroring out — n1.md is registered locally, so no orphan.
    assert orph["s3_unregistered"] == []
    assert "error" not in orph
