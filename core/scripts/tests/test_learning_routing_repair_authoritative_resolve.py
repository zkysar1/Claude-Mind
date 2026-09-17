"""Pins : learning-routing-repair's experience resolver reads the STORE,
not this box's mirror — and builds its index exactly once.

WHY THIS EXISTS. `agents/` sits outside `owncloud_sync._EAGER_PULL_ROOTS`, so a
per-agent store is never eager-pulled at all; a peer-owned mirror on this box can
sit short of the store indefinitely. Measured 2026-09-17 on one own-cloud box:
of 15 per-agent objects probed across three store names, 5 were short of the
store (skill-invocations 3 of 5, journal 2 of 5), every one of them on the
backend's `no_clobber` branch.

The asymmetry this closes was introduced by its own sibling fix. g-358-131 routed
`learning-routing-audit.load_all_experiences` through the authoritative bytes,
while THIS module's `_resolve_store_path` kept scanning the local mirror — and
this module imports that audit function. So the audit could report a dangling ref
on a record the resolver could not find, and the caller printed
`WARN: could not resolve file for experience/<id>`: a mirror lag rendered as if it
were a corrupt record. Same store, two readers, two answers.

NOT `refresh_for_read`, and that is measured rather than preferred. The helper
wraps `backend.refresh()`, which has two silent skips — `refresh_would_clobber`
ahead of it and the backend's `no_clobber` overwrite decision inside it. Under
g-358-131 `refresh()` left all five divergent peer objects byte-identical for
exactly that reason. `read_authoritative_bytes` is the call that returns store
bytes on a frozen mirror, which is why `_read_agent_jsonl_fresh` uses it.

Each test is written to fail on a specific mutation:
  - revert the resolver to a local scan   -> store-only-record test fails
  - drop the memo                         -> built-once test fails
  - reverse archive/live insertion order  -> live-precedence test fails
  - route non-experience stores through
    the index                             -> world-path test fails
  - let an unknown id resolve to anything -> unknown-id test fails
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

REPAIR_PY = SCRIPTS / "learning-routing-repair.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(rec_id, **extra):
    d = {"id": rec_id, "category": "test"}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


class _FakeBackend:
    """Returns bytes that DIFFER from the on-disk mirror, which is the whole point.

    `calls` records every path read so the memoization test can count reads
    rather than assert the absence of a network call it cannot observe.
    """

    def __init__(self, store_contents):
        self.store = {str(k): v for k, v in store_contents.items()}
        self.calls = []

    def read_authoritative_bytes(self, path):
        self.calls.append(str(path))
        key = str(path)
        if key not in self.store:
            raise FileNotFoundError(key)
        return self.store[key].encode("utf-8")


@pytest.fixture
def repair(monkeypatch):
    mod = _load(REPAIR_PY, "learning_routing_repair_under_test")
    # The index is a module global; a leaked one would make every later test
    # read a previous test's fixture and pass for the wrong reason.
    mod._EXPERIENCE_INDEX = None
    return mod


def _wire(monkeypatch, repair, agents_dir, store_contents):
    backend_mod = types.SimpleNamespace()
    fake = _FakeBackend(store_contents)
    backend_mod.get_backend = lambda: fake
    monkeypatch.setitem(sys.modules, "storage_backend", backend_mod)
    monkeypatch.setattr(repair, "agents_root", lambda: agents_dir)
    return fake


def _make_agent(tmp_path, name, live_lines, archive_lines=None):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    live = d / "experience.jsonl"
    live.write_text("\n".join(live_lines) + ("\n" if live_lines else ""), encoding="utf-8")
    arch = None
    if archive_lines is not None:
        arch = d / "experience-archive.jsonl"
        arch.write_text("\n".join(archive_lines) + ("\n" if archive_lines else ""),
                        encoding="utf-8")
    return live, arch


def test_index_sees_a_record_only_present_in_the_store(tmp_path, monkeypatch, repair):
    """The mirror is SHORT by one record; the resolver must still find it."""
    live, _ = _make_agent(tmp_path, "peer", [_rec("exp-local-only")])
    store_text = _rec("exp-local-only") + "\n" + _rec("exp-store-only") + "\n"
    _wire(monkeypatch, repair, tmp_path, {live: store_text})

    assert repair._resolve_store_path("experience", "exp-store-only") == live
    assert repair._resolve_store_path("experience", "exp-local-only") == live


def test_mutation_control_local_read_misses_the_store_only_record(tmp_path):
    """Reproduce the PRE-CHANGE local scan and show it returns None.

    Without this the pin above could pass against a resolver that never
    changed, because a mirror that happens to match the store is
    indistinguishable from an authoritative read.
    """
    live, _ = _make_agent(tmp_path, "peer", [_rec("exp-local-only")])

    def _old_local_scan(record_id):
        for line in live.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") == record_id:
                return live
        return None

    assert _old_local_scan("exp-local-only") == live
    assert _old_local_scan("exp-store-only") is None


def test_live_file_wins_over_archive_for_a_duplicate_id(tmp_path, monkeypatch, repair):
    live, arch = _make_agent(tmp_path, "peer", [_rec("exp-dup")], [_rec("exp-dup")])
    _wire(monkeypatch, repair, tmp_path, {
        live: _rec("exp-dup") + "\n",
        arch: _rec("exp-dup") + "\n",
    })
    assert repair._resolve_store_path("experience", "exp-dup") == live


def test_index_is_built_once_across_many_lookups(tmp_path, monkeypatch, repair):
    """An authoritative read is a NETWORK read; per-lookup rebuilding would turn
    one local pass into hundreds of remote GETs."""
    live, _ = _make_agent(tmp_path, "peer", [_rec("a"), _rec("b"), _rec("c")])
    fake = _wire(monkeypatch, repair, tmp_path,
                 {live: "\n".join(_rec(x) for x in ("a", "b", "c")) + "\n"})

    for rec_id in ("a", "b", "c", "a", "b", "missing"):
        repair._resolve_store_path("experience", rec_id)

    assert fake.calls.count(str(live)) == 1, fake.calls


def test_non_experience_store_returns_the_world_path(tmp_path, monkeypatch, repair):
    """The index is for per-agent files only — world stores keep their direct path."""
    _wire(monkeypatch, repair, tmp_path, {})
    for store in ("reasoning_bank", "guardrails", "pipeline"):
        got = repair._resolve_store_path(store, "any-id")
        assert got == repair.STORE_PATHS[store]
    # and building the index was never needed for that
    assert repair._EXPERIENCE_INDEX is None


def test_index_is_rebuilt_when_the_agents_root_changes(tmp_path, monkeypatch, repair):
    """The memo is keyed on the root, not a bare one-shot flag.

    A bare flag is correct for this script's production shape (main() resolves
    one root, once) and silently wrong for every other caller: the module-level
    memo outlives a re-pointed agents_root and returns the FIRST root's answers
    forever. That is not hypothetical — the bare-flag version turned two
    pre-existing resolver pins in test_learning_routing_glob_routing.py red,
    because they share a process with an earlier test that had already built an
    index against a different tmp root.
    """
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    a_live, _ = _make_agent(root_a, "peer", [_rec("exp-a")])
    b_live, _ = _make_agent(root_b, "peer", [_rec("exp-b")])
    _wire(monkeypatch, repair, root_a,
          {a_live: _rec("exp-a") + "\n", b_live: _rec("exp-b") + "\n"})

    assert repair._resolve_store_path("experience", "exp-a") == a_live
    assert repair._resolve_store_path("experience", "exp-b") is None

    monkeypatch.setattr(repair, "agents_root", lambda: root_b)
    assert repair._resolve_store_path("experience", "exp-b") == b_live


def test_unknown_record_id_resolves_to_none(tmp_path, monkeypatch, repair):
    """None is load-bearing: the caller WARNs and SKIPS on it, so a resolver that
    guessed a path would turn a skip into a write against the wrong file."""
    live, _ = _make_agent(tmp_path, "peer", [_rec("known")])
    _wire(monkeypatch, repair, tmp_path, {live: _rec("known") + "\n"})
    assert repair._resolve_store_path("experience", "nope") is None


def test_multiple_agents_are_all_indexed(tmp_path, monkeypatch, repair):
    """A cross-agent glob that silently scans one agent is the  class."""
    a_live, _ = _make_agent(tmp_path, "alpha", [_rec("exp-a")])
    b_live, _ = _make_agent(tmp_path, "bravo", [_rec("exp-b")])
    _wire(monkeypatch, repair, tmp_path,
          {a_live: _rec("exp-a") + "\n", b_live: _rec("exp-b") + "\n"})
    assert repair._resolve_store_path("experience", "exp-a") == a_live
    assert repair._resolve_store_path("experience", "exp-b") == b_live


def test_backend_failure_degrades_to_the_local_mirror(tmp_path, monkeypatch, repair):
    """`_read_agent_jsonl_fresh` falls back to the local read on any exception.

    Pinned here because the fallback is what keeps a credential expiry from
    turning the repair into a crash — the read degrades to the old behaviour
    rather than resolving everything to None, which would silently skip every
    repair while reporting success.
    """
    live, _ = _make_agent(tmp_path, "peer", [_rec("exp-local-only")])
    backend_mod = types.SimpleNamespace()

    def _boom():
        raise RuntimeError("no credentials")

    backend_mod.get_backend = _boom
    monkeypatch.setitem(sys.modules, "storage_backend", backend_mod)
    monkeypatch.setattr(repair, "agents_root", lambda: tmp_path)

    assert repair._resolve_store_path("experience", "exp-local-only") == live
