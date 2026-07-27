""": the generic meta-YAML write path re-fences and retries.

THE BUG. `set_field` / `append_item` in mind_api/src/meta/meta_yaml.py did a
read-modify-write with the read OUTSIDE the lock and the write under a bare
`file_locks.locked()` — no `refresh()` and no conflict retry. On own-cloud that
is the per-object stale-IfMatch DEADLOCK (rb-2639), not transient contention:
a peer's write from another box stales this box's mirror, every PUT fences
against an etag the remote no longer has, and the 412 repeats forever against
a remote that never changes. Nothing in the old code could recover.

Measured (g-335-275, zeta, cc-04): 5/5 `meta-set.sh skill-gaps.yaml gaps.<idx>`
attempts returned write_conflict while two reads 5s apart were byte-identical,
and a write to a DIFFERENT meta file succeeded in the same minute. While wedged,
skill-gaps.yaml — the write target of aspirations-spark Phase 6.5 — accepted no
gap registrations, so no skill could be forged there from a detected capability
gap. Scope is per-object AND per-box, not fleet-wide: only the box observing the
stale etag is wedged (rb-3280 force-freshes the mirror on that box's OWN
write-RMW), and five gaps did register elsewhere the same day. Where it lands,
though, it never recovers on its own.

Two sibling meta write paths (meta_impk._read_yaml, spark_questions_write)
already carried the fix; this one had been left behind.

WHAT THESE TESTS PIN. Reverting either half re-arms the wedge, and neither half
is observable on LocalBackend (where conflict_error is the empty tuple, so the
retry wrapper is a transparent single pass) — which is exactly why the defect
survived: the whole suite runs green under STORAGE_BACKEND=local either way.
A stub backend supplies the conflict type without S3.

  1. the cycle calls refresh() (not ensure_local) — re-taking the If-Match
     fence per attempt is what breaks the deadlock; a retry on a stale token
     conflicts identically forever.
  2. the cycle is wrapped in locked_rmw — so a conflict retries at all.

Stub-backend pattern mirrors test_retrieve_bump_conflict_retry.py /
test_fileops_conflict_retry.py.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]        # core/scripts
_ROOT = _SCRIPTS.parents[1]                           # PROJECT_ROOT
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.meta import meta_yaml as MY  # noqa: E402


def _fileops_mod():
    """The CURRENT _fileops module object (sibling suites reload it)."""
    return importlib.import_module("_fileops")


class _Conflict(Exception):
    """Stand-in for OwnCloudBackend's optimistic-concurrency exception."""


class _StubBackend:
    """Identity ensure_local/refresh with call counters, plus a conflict type.

    refresh_calls is the load-bearing assertion: it separates "re-read the
    local mirror" (ensure_local — the wedged shape) from "force-pull the
    remote and re-take the If-Match fence" (refresh — the fix).
    """

    def __init__(self):
        self.conflict_error = _Conflict
        self.refresh_calls = 0
        self.ensure_local_calls = 0
        self._held = set()

    def ensure_local(self, p):
        self.ensure_local_calls += 1
        return p

    def refresh(self, p):
        self.refresh_calls += 1
        return p

    # _fileops routes locking through the backend; in-memory no-op locks
    # suffice for a single-threaded unit test (same as the sibling suites').
    def acquire_lock(self, lock_path, timeout=10, stale_seconds=30):
        self._held.add(str(lock_path))

    def release_lock(self, lock_path):
        self._held.discard(str(lock_path))

    def atomic_write(self, target, write_to_handle, *, max_retries=10):
        class _Result:
            fallback_used = False
            retry_count = 0
            error_msg = ""
        with open(target, "w", encoding="utf-8") as h:
            write_to_handle(h)
        return _Result()


def _retry_globals():
    """The namespace `_rmw_with_conflict_retry` actually reads at call time.

    mind_api/src/file_locks.py does `from _fileops import _rmw_with_conflict_retry`
    at module level, binding the FUNCTION OBJECT once. That function resolves
    `get_backend` and `_conflict_backoff` from its own __globals__ — the
    _fileops module dict it was DEFINED in. Patching the module object returned
    by importlib.import_module("_fileops") is therefore not reliably the same
    namespace: sibling suites in this directory sandbox-reload _fileops, after
    which the imported function still reads the ORIGINAL dict while
    import_module hands back the NEW module. The patch lands somewhere the code
    under test never looks, `conflict_cls` falls back to the REAL backend's
    empty-tuple conflict type, `except conflict_cls` matches nothing, and the
    injected _Conflict escapes.

    That is not hypothetical: the two conflict-injecting tests below passed in
    isolation and FAILED in the full suite until this helper existed — the same
    signature test_retrieve_bump_conflict_retry.py documents in its
    MODULE-IDENTITY NOTE. Going through __globals__ is exact and reload-proof.
    """
    return MY.file_locks._rmw_with_conflict_retry.__globals__


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retry sleeps would make these tests slow for no signal."""
    monkeypatch.setitem(_retry_globals(), "_conflict_backoff", lambda *_: 0)


@pytest.fixture()
def meta_dir(tmp_path):
    d = tmp_path / "meta"
    d.mkdir()
    (d / "skill-gaps.yaml").write_text(
        yaml.safe_dump({"last_updated": "2026-07-26",
                        "gaps": [{"id": "gap-001", "type": "utility",
                                  "encounter_log": [{"note": "seed"}]}]},
                       sort_keys=False),
        encoding="utf-8")
    return d


@pytest.fixture()
def backend(monkeypatch):
    be = _StubBackend()
    # Three namespaces resolve get_backend on this path, and they are NOT
    # guaranteed to be the same dict (see _retry_globals):
    #   1. storage_backend — _read_yaml imports it at CALL time
    #   2. the retry primitive's own __globals__ — where conflict_cls comes from
    #   3. the current _fileops module object — belt-and-braces for any other
    #      helper (_atomic_write_with_fallback's lock path) resolving there
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: be)
    monkeypatch.setitem(_retry_globals(), "get_backend", lambda: be)
    monkeypatch.setattr(_fileops_mod(), "get_backend", lambda: be, raising=False)
    return be


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    """history/changelog write into the meta root; not under test here."""
    monkeypatch.setattr(MY.history, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(MY.changelog, "append", lambda *a, **k: None)
    monkeypatch.setattr(MY, "_append_log", lambda *a, **k: "mc-test")


def _ctx(meta_dir: Path, body: dict):
    return SimpleNamespace(
        headers={"x-mind-agent": "bravo"},
        body=json.dumps(body).encode("utf-8"),
        paths=SimpleNamespace(meta=meta_dir, project_root=_ROOT),
    )


def _gaps(meta_dir: Path):
    return yaml.safe_load((meta_dir / "skill-gaps.yaml").read_text())["gaps"]


# ---------------------------------------------------------------------------
# set_field
# ---------------------------------------------------------------------------

def test_set_field_refreshes_the_fence(meta_dir, backend):
    """The write cycle force-pulls the remote instead of trusting the mirror.

    Regression pin for the wedge: with ensure_local the If-Match token comes
    from a possibly-stale mirror and a peer's write wedges the object
    permanently. refresh_calls >= 1 is what makes the retry able to converge.
    """
    resp = MY.set_field(_ctx(meta_dir, {
        "file": "skill-gaps.yaml", "dotpath": "gaps.1",
        "value": {"id": "gap-002", "type": "utility"}}))

    assert backend.refresh_calls >= 1, (
        "write cycle never called refresh() — it is fencing against the local "
        "mirror, which is the rb-2639 stale-IfMatch wedge")
    assert '"status": "set"' in resp.body if isinstance(resp.body, str) \
        else b'"status": "set"' in resp.body
    ids = [g["id"] for g in _gaps(meta_dir)]
    assert ids == ["gap-001", "gap-002"], "append-at-len branch did not land"


def test_set_field_retries_a_conflict_and_lands(meta_dir, backend, monkeypatch):
    """A conflict is absorbed: attempt 2 re-reads, re-fences, and writes.

    Before the fix this raised straight out to the caller as a 409 with nothing
    written, and — because the retry never re-read — repeating the call could
    not help either.
    """
    real_write = MY._atomic_write_with_fallback
    calls = {"n": 0}

    def flaky_write(path, write_fn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Conflict("412 stale If-Match")
        return real_write(path, write_fn, **kw)

    # meta_yaml binds the helper at import time (`from _fileops import ...`),
    # so the live reference is meta_yaml's own global — patching the _fileops
    # attribute is invisible to the code under test.
    monkeypatch.setattr(MY, "_atomic_write_with_fallback", flaky_write)

    MY.set_field(_ctx(meta_dir, {
        "file": "skill-gaps.yaml", "dotpath": "gaps.1",
        "value": {"id": "gap-002", "type": "utility"}}))

    assert calls["n"] == 2, "conflict was not retried"
    assert backend.refresh_calls >= 2, (
        "the retry did not RE-fence — retrying against the same stale token "
        "conflicts identically forever (the deadlock, not a transient)")
    assert [g["id"] for g in _gaps(meta_dir)] == ["gap-001", "gap-002"]


def test_set_field_reraises_when_retries_exhaust(meta_dir, backend, monkeypatch):
    """Persistent conflict still surfaces — it must reach the server's 409
    floor as a safe-to-retry write_conflict, never be swallowed as success."""
    monkeypatch.setattr(MY, "_atomic_write_with_fallback",
                        lambda *a, **k: (_ for _ in ()).throw(_Conflict("412")))

    with pytest.raises(_Conflict):
        MY.set_field(_ctx(meta_dir, {
            "file": "skill-gaps.yaml", "dotpath": "gaps.1",
            "value": {"id": "gap-002", "type": "utility"}}))

    assert [g["id"] for g in _gaps(meta_dir)] == ["gap-001"], \
        "a failed write must leave the store untouched"


# ---------------------------------------------------------------------------
# append_item — the shape most exposed to the wedge (every agent appends to
# the same shared encounter_log lists)
# ---------------------------------------------------------------------------

def test_append_item_refreshes_and_retries(meta_dir, backend, monkeypatch):
    real_write = MY._atomic_write_with_fallback
    calls = {"n": 0}

    def flaky_write(path, write_fn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Conflict("412 stale If-Match")
        return real_write(path, write_fn, **kw)

    # meta_yaml binds the helper at import time (`from _fileops import ...`),
    # so the live reference is meta_yaml's own global — patching the _fileops
    # attribute is invisible to the code under test.
    monkeypatch.setattr(MY, "_atomic_write_with_fallback", flaky_write)

    MY.append_item(_ctx(meta_dir, {
        "file": "skill-gaps.yaml", "dotpath": "gaps.0.encounter_log",
        "item": {"note": "second"}}))

    assert calls["n"] == 2
    assert backend.refresh_calls >= 2
    log = _gaps(meta_dir)[0]["encounter_log"]
    assert [e["note"] for e in log] == ["seed", "second"], \
        "the retry must re-apply the append exactly once, not zero or twice"


def test_append_item_rejects_non_list_as_400(meta_dir, backend):
    """The not_a_list guard moved inside the cycle — it must still map to 400,
    not escape as an unhandled exception."""
    resp = MY.append_item(_ctx(meta_dir, {
        "file": "skill-gaps.yaml", "dotpath": "last_updated",
        "item": {"note": "x"}}))
    body = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    assert "not_a_list" in body
