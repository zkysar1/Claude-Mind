""" — purge delete-propagation unit tests.

propagate_temp_deletes removes the remote key for every path a purge deleted
locally, scoped to <agents_root>/<agent>/temp/** — anything else is refused
before touching the backend. These tests pin the scope guard, the counting
contract, dry-run inertness, per-key error isolation, and the no-delete_object
skip with a fake backend; no daemon, no S3.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_sync():
    spec = importlib.util.spec_from_file_location(
        "owncloud_sync_purge_prop_under_test",
        REPO_ROOT / "core" / "scripts" / "owncloud_sync.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


SYNC = _load_sync()


class FakeBackend:
    """Remote store = set of posix-rel-under-agents-root keys."""

    def __init__(self, agents_root: Path, remote: set,
                 raise_on: set | None = None):
        self._roots = [(str(agents_root), "agents")]
        self._agents_root = agents_root
        self.remote = set(remote)
        self.raise_on = set(raise_on or ())
        self.deleted = []
        self.calls = 0

    def _rel(self, path: Path) -> str:
        return Path(path).relative_to(self._agents_root).as_posix()

    def delete_object(self, path: Path) -> bool:
        self.calls += 1
        rel = self._rel(path)
        if rel in self.raise_on:
            raise RuntimeError(f"injected failure for {rel}")
        if rel in self.remote:
            self.remote.discard(rel)
            self.deleted.append(rel)
            return True
        return False

    def iter_paths_under(self, path: Path):
        prefix = self._rel(path) + "/"
        for k in sorted(self.remote):
            if k.startswith(prefix):
                yield self._agents_root / k


class NoDeleteBackend:
    def __init__(self, agents_root: Path):
        self._roots = [(str(agents_root), "agents")]


def _mk_agents(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    (root / "alpha" / "temp" / "drained").mkdir(parents=True)
    return root


def test_deletes_in_scope_keys(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json", "alpha/temp/b.md",
                            "alpha/temp/drained/old.md"})
    paths = [str(root / "alpha" / "temp" / "a.json"),
             str(root / "alpha" / "temp" / "b.md"),
             str(root / "alpha" / "temp" / "drained" / "old.md")]
    stats = SYNC.propagate_temp_deletes(be, paths)
    assert stats["requested"] == 3
    assert stats["deleted"] == 3
    assert stats["errors"] == 0
    assert stats["refused_out_of_scope"] == 0
    assert sorted(be.deleted) == ["alpha/temp/a.json",
                                  "alpha/temp/b.md",
                                  "alpha/temp/drained/old.md"]
    assert stats["deleted_keys"] == ["agents/alpha/temp/a.json",
                                     "agents/alpha/temp/b.md",
                                     "agents/alpha/temp/drained/old.md"]


def test_already_absent_counted_not_error(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    paths = [str(root / "alpha" / "temp" / "a.json"),
             str(root / "alpha" / "temp" / "never-pushed.json")]
    stats = SYNC.propagate_temp_deletes(be, paths)
    assert stats["deleted"] == 1
    assert stats["already_absent"] == 1
    assert stats["errors"] == 0


def test_out_of_scope_paths_refused_before_backend(tmp_path):
    root = _mk_agents(tmp_path)
    (root / "alpha" / "journal").mkdir()
    be = FakeBackend(root, {"alpha/journal.jsonl"})
    outside = tmp_path / "elsewhere" / "x.json"
    paths = [
        str(outside),                                # not under agents_root
        str(root / "alpha" / "journal.jsonl"),       # under agent, NOT temp/
        str(root / "alpha"),                         # too shallow
    ]
    stats = SYNC.propagate_temp_deletes(be, paths)
    assert stats["refused_out_of_scope"] == 3
    assert stats["deleted"] == 0
    assert be.calls == 0, "refused paths must never reach the backend"


def test_dry_run_counts_without_backend_calls(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    paths = [str(root / "alpha" / "temp" / "a.json"),
             str(tmp_path / "outside.json")]
    stats = SYNC.propagate_temp_deletes(be, paths, dry_run=True)
    assert stats["would_delete"] == 1
    assert stats["refused_out_of_scope"] == 1
    assert stats["deleted"] == 0
    assert be.calls == 0


def test_error_isolated_per_key(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json", "alpha/temp/b.json"},
                     raise_on={"alpha/temp/a.json"})
    paths = [str(root / "alpha" / "temp" / "a.json"),
             str(root / "alpha" / "temp" / "b.json")]
    stats = SYNC.propagate_temp_deletes(be, paths)
    assert stats["errors"] == 1
    assert stats["deleted"] == 1, "an error on one key must not stop the rest"
    # Identity travels in error_paths via _record_error — the module-wide
    # counter-vs-identity invariant (guard-1623), never a parallel list.
    assert len(stats["error_paths"]) == 1
    entry = stats["error_paths"][0]
    assert entry["path"].endswith("alpha/temp/a.json")
    assert entry["phase"] == "purge-delete"
    assert entry["exc"] == "RuntimeError"


def test_not_owned_agent_refused_before_backend(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    paths = [str(root / "alpha" / "temp" / "a.json")]
    stats = SYNC.propagate_temp_deletes(be, paths, owned={"bravo"})
    assert stats["refused_not_owned"] == 1
    assert stats["deleted"] == 0
    assert be.calls == 0, "a peer-owned agent's keys must never reach the backend"


def test_empty_owned_set_refuses_everything(tmp_path):
    # The claim-less WORKER box posture: owned resolves to an empty set, so
    # the S3 half of a purge is a structural no-op there.
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "a.json")], owned=set())
    assert stats["refused_not_owned"] == 1
    assert be.calls == 0


def test_owned_gate_applies_in_dry_run_too(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "a.json")],
        dry_run=True, owned={"bravo"})
    assert stats["refused_not_owned"] == 1
    assert stats["would_delete"] == 0, "dry-run must preview the refusal, not the delete"


def test_backend_without_delete_object_skips(tmp_path):
    root = _mk_agents(tmp_path)
    be = NoDeleteBackend(root)
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "a.json")])
    assert stats["skipped"] == "backend has no delete_object"
    assert stats["deleted"] == 0 and stats["requested"] == 0


def test_blank_lines_ignored(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    stats = SYNC.propagate_temp_deletes(
        be, ["", "   ", str(root / "alpha" / "temp" / "a.json")])
    assert stats["requested"] == 1
    assert stats["deleted"] == 1


def test_dir_entry_propagates_by_prefix_listing(tmp_path):
    # A trailing-slash line is a purged Lane-3 dir: only the keys the STORE
    # holds under that prefix are deleted — a local-only dir (nothing pushed)
    # costs one empty listing, never a local walk.
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/probe/a.json",
                            "alpha/temp/probe/sub/b.json",
                            "alpha/temp/keep.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "probe") + "/"])
    assert stats["dirs"] == 1
    assert stats["deleted"] == 2
    assert sorted(be.deleted) == ["alpha/temp/probe/a.json",
                                  "alpha/temp/probe/sub/b.json"]
    assert "alpha/temp/keep.json" in be.remote, "siblings outside the prefix survive"


def test_dir_entry_local_only_dir_deletes_nothing(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/keep.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "npm-junk") + "/"])
    assert stats["dirs"] == 1
    assert stats["deleted"] == 0 and stats["errors"] == 0
    assert be.calls == 0, "an unpushed dir must cost zero delete calls"


def test_dir_entry_dry_run_counts_store_side(tmp_path):
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/probe/a.json",
                            "alpha/temp/probe/b.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "probe") + "/"], dry_run=True)
    assert stats["would_delete"] == 2
    assert be.calls == 0


def test_dir_prefix_listing_failure_is_recorded_and_isolated(tmp_path):
    # A failing prefix LIST (not a failing delete) must be counted with an
    # identity and must not abort the remaining entries.
    root = _mk_agents(tmp_path)

    class ListFailBackend(FakeBackend):
        def iter_paths_under(self, path: Path):
            raise RuntimeError("injected list failure")
            yield  # pragma: no cover — keeps this a generator

    be = ListFailBackend(root, {"alpha/temp/after.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp" / "probe") + "/",
             str(root / "alpha" / "temp" / "after.json")])
    assert stats["errors"] == 1
    assert stats["error_paths"][0]["phase"] == "purge-prefix-list"
    assert stats["deleted"] == 1, "a failed dir listing must not stop later entries"


def test_temp_root_itself_as_dir_is_refused(tmp_path):
    # agents/<agent>/temp/ fed as a dir line must NOT prefix-delete the whole
    # temp store — the scope check requires a component BELOW temp/.
    root = _mk_agents(tmp_path)
    be = FakeBackend(root, {"alpha/temp/a.json"})
    stats = SYNC.propagate_temp_deletes(
        be, [str(root / "alpha" / "temp") + "/"])
    assert stats["refused_out_of_scope"] == 1
    assert be.calls == 0


def test_key_cap_bounds_reported_lists(tmp_path):
    root = _mk_agents(tmp_path)
    n = SYNC._PURGE_PROPAGATE_KEY_CAP + 25
    keys = {f"alpha/temp/f{i}.json" for i in range(n)}
    be = FakeBackend(root, keys)
    paths = [str(root / "alpha" / "temp" / f"f{i}.json") for i in range(n)]
    stats = SYNC.propagate_temp_deletes(be, paths)
    assert stats["deleted"] == n, "the CAP bounds the reported list, not the deletes"
    assert len(stats["deleted_keys"]) == SYNC._PURGE_PROPAGATE_KEY_CAP
