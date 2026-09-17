"""_fresh_read: a reader of a store the eager pull never re-pulls must refresh
on EVERY read (g-358-125, guard-6878).

The load-bearing test is `test_stale_local_archive_is_refreshed_before_read`:
the fake backend holds a newer store copy than the local mirror, exactly the
measured cc-02 state (local guardrails-archive 28 records behind for 11 days).
It carries a positive control so a helper that stops refreshing fails it
instead of passing on stale bytes.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/scripts
# Import _fileops BEFORE any test patches storage_backend.get_backend. It binds
# `get_backend` by name at import, so a first import from inside a patched test
# keeps the fake backend for every later test in the process (measured: 13
# test_jsonl_hygiene locks failed with "'_StoreBackend' has no acquire_lock").
import _fileops  # noqa: E402,F401
import _fresh_read  # noqa: E402


def _write(p, records):
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class _StoreBackend:
    """Own-cloud stand-in: carries _roots so the REAL
    owncloud_sync.refresh_would_clobber classifies the path, and holds a
    store copy per file name that refresh() pulls over the local mirror."""
    def __init__(self, roots, store=None, fail=None):
        self._roots = roots
        self.store = store or {}
        self.fail = fail
        self.refreshed = []

    def refresh(self, path):
        path = Path(path)
        self.refreshed.append(path)
        if self.fail is not None:
            raise self.fail
        if path.name in self.store:
            _write(path, self.store[path.name])


def _world(tmp_path, monkeypatch, **kw):
    import storage_backend
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    be = _StoreBackend([(str(world.resolve()), "world")], **kw)
    monkeypatch.setattr(storage_backend, "get_backend", lambda: be)
    return world, be


def test_stale_local_archive_is_refreshed_before_read(tmp_path, monkeypatch):
    local_recs = [{"id": "guard-1"}, {"id": "guard-2"}]
    store_recs = local_recs + [{"id": "guard-3"}, {"id": "guard-4"}]
    world, be = _world(tmp_path, monkeypatch,
                       store={"guardrails-archive.jsonl": store_recs})
    f = world / "guardrails-archive.jsonl"
    _write(f, local_recs)

    # POSITIVE CONTROL: the local mirror really is stale, so a reader that
    # skipped the refresh would return exactly this.
    stale = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
    assert stale == local_recs

    out = _fresh_read.read_jsonl_fresh(f, label="t")
    assert out == store_recs
    assert be.refreshed == [f]


def test_refresh_for_read_pulls_the_store_copy_for_a_caller_own_parser(
        tmp_path, monkeypatch):
    # Routed readers keep their own parse and call refresh_for_read first.
    world, be = _world(tmp_path, monkeypatch,
                       store={"aspirations-archive.jsonl": [{"id": "asp-9"}]})
    f = world / "aspirations-archive.jsonl"
    _write(f, [])
    _fresh_read.refresh_for_read(f, label="t")
    assert json.loads(f.read_text().strip()) == {"id": "asp-9"}


def test_remote_only_archive_is_materialized_and_read(tmp_path, monkeypatch):
    world, be = _world(tmp_path, monkeypatch,
                       store={"pipeline-archive.jsonl": [{"id": "p-1"}]})
    f = world / "pipeline-archive.jsonl"
    assert not f.exists()
    assert _fresh_read.read_jsonl_fresh(f, label="t") == [{"id": "p-1"}]


def test_absent_everywhere_reads_empty(tmp_path, monkeypatch):
    world, be = _world(tmp_path, monkeypatch)
    assert _fresh_read.read_jsonl_fresh(world / "x-archive.jsonl", label="t") == []


def test_per_machine_store_is_not_refreshed(tmp_path, monkeypatch):
    # presence/ is never pushed, so a refresh would clobber the only good copy
    # with the store's stale/empty one (guard-881 / ).
    world, be = _world(tmp_path, monkeypatch,
                       store={"alpha.jsonl": [{"i": "remote"}]})
    (world / "presence").mkdir()
    f = world / "presence" / "alpha.jsonl"
    _write(f, [{"i": "local"}])
    assert _fresh_read.read_jsonl_fresh(f, label="t") == [{"i": "local"}]
    assert be.refreshed == []


def test_refresh_failure_is_reported_and_the_local_read_proceeds(
        tmp_path, monkeypatch, capsys):
    world, be = _world(tmp_path, monkeypatch, fail=OSError("remote down"))
    f = world / "reasoning-bank-archive.jsonl"
    _write(f, [{"id": "rb-1"}])
    assert _fresh_read.read_jsonl_fresh(f, label="my-reader") == [{"id": "rb-1"}]
    err = capsys.readouterr().err
    assert "[my-reader] (refresh skipped for reasoning-bank-archive.jsonl: remote down)" in err


def test_malformed_lines_are_skipped(tmp_path, monkeypatch):
    world, be = _world(tmp_path, monkeypatch)
    f = world / "findings-archive.jsonl"
    f.write_text('{"id": "a"}\nnot json\n\n{"id": "b"}\n', encoding="utf-8")
    assert _fresh_read.read_jsonl_fresh(f, label="t") == [{"id": "a"}, {"id": "b"}]


def test_local_backend_reads_the_local_copy(tmp_path, monkeypatch):
    # STORAGE_BACKEND=local: refresh is a no-op and nothing is printed.
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    import storage_backend
    monkeypatch.setattr(storage_backend, "_ACTIVE_BACKEND", None, raising=False)
    f = tmp_path / "world" / "aspirations-archive.jsonl"
    f.parent.mkdir()
    _write(f, [{"id": "asp-1"}])
    assert _fresh_read.read_jsonl_fresh(f, label="t") == [{"id": "asp-1"}]
