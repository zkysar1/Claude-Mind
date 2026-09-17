"""learning-routing-audit must load each agent's experience stores from the
AUTHORITATIVE bytes, not from this box's local mirror (g-358-131).

WHY A SEPARATE PIN FROM test_learning_routing_segment_reads.py. That file pins
the SHARED world/meta stores, which reach the backend through
`backend.ensure_local`. Per-agent stores cannot use that route and be current:

  - the periodic pull sweep's roots are world and meta, so nothing sweeps an
    agent dir; the per-read refresh is the ONLY path, and
  - that path does not fire here. MEASURED 2026-09-17 on one own-cloud box,
    12 peer objects: 5 local mirrors were SHORT of the store by 97-4,728 bytes,
    and `backend.refresh()` left all 5 unchanged because the backend's
    overwrite decision returned `no_clobber` for every one. That guard protects
    unpushed local writes; a box never writes a PEER's experience store, so it
    is really seeing a stale baseline — but it cannot tell those apart.

So the remedy this module needs is the authoritative READ, not a refresh. The
same measurement run showed the fix recovering 6 experience records that the
mirror could not see, every one of them written that same day.

THE LOAD-BEARING ASSERTION IS THE SAME AS THE SIBLING FILE'S: the corpus is
NEVER SHORTER than the plain local read. This module's output feeds a writer
that fires automatically — `learning-routing-repair.py --apply`, invoked by
`tree.py::_post_remove_sweep_dangling` — and that writer NULLS whatever the
audit calls dangling. A record the loader cannot see is not merely unreported:
every reference pointing AT it reads as dangling and gets destroyed. That is
how g-115-5646 nulled 17,466 fields, 94.7% of them valid.

Every failure direction is pinned, because the safe direction is the whole
design: unavailable backend, raising read, and malformed bytes must all degrade
to the local read, never to a short corpus.

Mutation-verified: `test_mutation_control_local_reader_misses_the_store_only_record`
reproduces the pre-change read inline and asserts it WOULD miss the record, so
these pins are known able to fail.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

AUDIT_PY = SCRIPTS / "learning-routing-audit.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load(AUDIT_PY, "_lr_agent_store_audit")


def _rec(rid):
    return {"id": rid}


def _jsonl(records):
    return "".join(json.dumps(r) + "\n" for r in records)


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_jsonl(records), encoding="utf-8")


class _FakeBackend:
    """Stands in for the real backend. `payload` is what the STORE holds; the
    local file is written separately, so the two can deliberately disagree."""

    def __init__(self, payload=None, raises=None):
        self._payload = payload
        self._raises = raises

    def read_authoritative_bytes(self, path):
        if self._raises is not None:
            raise self._raises
        return self._payload


def _install_backend(monkeypatch, backend, *, import_fails=False):
    """Patch the `storage_backend` module the helper imports AT CALL TIME."""
    if import_fails:
        monkeypatch.setitem(sys.modules, "storage_backend", None)
        return
    mod = types.ModuleType("storage_backend")
    mod.get_backend = lambda: backend
    monkeypatch.setitem(sys.modules, "storage_backend", mod)


# ---------------------------------------------------------------------------
# The defect: a local mirror that is SHORT of the store.
# ---------------------------------------------------------------------------


def test_stale_mirror_reads_the_store_not_the_local_copy(tmp_path, monkeypatch):
    local = [_rec("exp-a"), _rec("exp-b")]
    store = [_rec("exp-a"), _rec("exp-b"), _rec("exp-c-written-by-a-peer")]
    path = tmp_path / "zeta" / "experience.jsonl"
    _write(path, local)
    _install_backend(monkeypatch, _FakeBackend(payload=_jsonl(store).encode("utf-8")))

    got = audit._read_agent_jsonl_fresh(path)

    assert [r["id"] for r in got] == [r["id"] for r in store]
    # The load-bearing direction: never shorter than the local read.
    assert len(got) >= len(audit._read_jsonl(path))


def test_mutation_control_local_reader_misses_the_store_only_record(tmp_path):
    """The pre-change read, reproduced inline. If this ever returns 3 records
    the pin above has stopped being able to fail."""
    local = [_rec("exp-a"), _rec("exp-b")]
    path = tmp_path / "zeta" / "experience.jsonl"
    _write(path, local)

    assert [r["id"] for r in audit._read_jsonl(path)] == ["exp-a", "exp-b"]


def test_store_copy_of_an_edited_record_wins_over_the_mirror(tmp_path, monkeypatch):
    """Record COUNT is not the only way a mirror goes stale — one of the five
    measured objects diverged by 97 bytes at an unchanged record count, i.e. a
    record was edited in place. Pin content, not just length."""
    path = tmp_path / "alpha" / "experience.jsonl"
    _write(path, [{"id": "exp-a", "outcome": "stale"}])
    _install_backend(monkeypatch, _FakeBackend(
        payload=_jsonl([{"id": "exp-a", "outcome": "current"}]).encode("utf-8")))

    got = audit._read_agent_jsonl_fresh(path)

    assert got == [{"id": "exp-a", "outcome": "current"}]


# ---------------------------------------------------------------------------
# Failure directions — every one degrades to the local read, never to [].
# ---------------------------------------------------------------------------


def test_backend_import_failure_falls_back_to_the_local_read(tmp_path, monkeypatch):
    local = [_rec("exp-a"), _rec("exp-b")]
    path = tmp_path / "bravo" / "experience.jsonl"
    _write(path, local)
    _install_backend(monkeypatch, None, import_fails=True)

    got = audit._read_agent_jsonl_fresh(path)

    assert [r["id"] for r in got] == ["exp-a", "exp-b"]


def test_authoritative_read_raising_falls_back_to_the_local_read(tmp_path, monkeypatch):
    local = [_rec("exp-a"), _rec("exp-b")]
    path = tmp_path / "foxtrot" / "experience.jsonl"
    _write(path, local)
    _install_backend(monkeypatch, _FakeBackend(raises=OSError("network down")))

    got = audit._read_agent_jsonl_fresh(path)

    assert [r["id"] for r in got] == ["exp-a", "exp-b"]


def test_absent_local_and_absent_store_is_empty_not_an_error(tmp_path, monkeypatch):
    path = tmp_path / "delta" / "experience.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _install_backend(monkeypatch, _FakeBackend(raises=FileNotFoundError("no key")))

    assert audit._read_agent_jsonl_fresh(path) == []


def test_malformed_authoritative_lines_are_skipped_like_the_local_reader(
        tmp_path, monkeypatch):
    """Both readers share `_parse_jsonl_text`, so a record cannot move in or out
    of the corpus depending only on which reader ran."""
    body = '{"id": "exp-a"}\nnot json at all\n\n{"id": "exp-b"}\n'
    path = tmp_path / "charlie" / "experience.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _install_backend(monkeypatch, _FakeBackend(payload=body.encode("utf-8")))

    fresh = audit._read_agent_jsonl_fresh(path)
    local = audit._read_jsonl(path)

    assert [r["id"] for r in fresh] == ["exp-a", "exp-b"]
    assert fresh == local


# ---------------------------------------------------------------------------
# Every fallback SAYS so on stderr (). The local read is the safe
# direction, but a silent one lets an expired credential restore the 
# defect while the code, tests and tree node all still say it is fixed.
# One case per ROUTE into the local read, not one per except block (guard-2521),
# plus quiet controls so a note printed unconditionally cannot pass (guard-1836).
# ---------------------------------------------------------------------------

NOTE_PREFIX = "[learning-routing-audit]"


class _CredentialExpired(Exception):
    """A class name no other route produces, so matching it proves the note
    carries the REAL exception class rather than a fixed string (guard-2336)."""


def _notes(capsys):
    return [line for line in capsys.readouterr().err.splitlines()
            if line.startswith(NOTE_PREFIX)]


@pytest.mark.parametrize("route, expected_class", [
    ("backend-import-fails", "ModuleNotFoundError"),
    ("authoritative-read-raises", "_CredentialExpired"),
    ("store-returns-non-bytes", "AttributeError"),
])
def test_every_fallback_route_notes_the_path_and_exception_class(
        route, expected_class, tmp_path, monkeypatch, capsys):
    path = tmp_path / "echo" / "experience.jsonl"
    _write(path, [_rec("exp-a"), _rec("exp-b")])
    monkeypatch.setattr(audit, "AUTHORITATIVE_READ_FALLBACKS", [])
    if route == "backend-import-fails":
        _install_backend(monkeypatch, None, import_fails=True)
    elif route == "authoritative-read-raises":
        _install_backend(monkeypatch, _FakeBackend(raises=_CredentialExpired("token expired")))
    else:
        _install_backend(monkeypatch, _FakeBackend(payload=None))

    got = audit._read_agent_jsonl_fresh(path)

    assert [r["id"] for r in got] == ["exp-a", "exp-b"], \
        "the note must not change what the fallback returns"
    notes = _notes(capsys)
    assert len(notes) == 1, notes
    assert str(path) in notes[0]
    assert expected_class in notes[0]
    # The note and the record repair refuses on () are one event; a
    # route that says it degraded but is not recorded would let --apply write.
    assert audit.AUTHORITATIVE_READ_FALLBACKS == [str(path)]


def test_no_note_when_the_store_reports_absent_but_the_mirror_exists(
        tmp_path, monkeypatch, capsys):
    """The mirror cannot be SHORT of an object the store does not hold: it is
    this box's own unpushed write, the superset (g-358-145). Quiet, unrecorded."""
    path = tmp_path / "echo" / "experience.jsonl"
    _write(path, [_rec("exp-a"), _rec("exp-b")])
    monkeypatch.setattr(audit, "AUTHORITATIVE_READ_FALLBACKS", [])
    _install_backend(monkeypatch, _FakeBackend(raises=FileNotFoundError("no key")))

    assert [r["id"] for r in audit._read_agent_jsonl_fresh(path)] == ["exp-a", "exp-b"]
    assert _notes(capsys) == []
    assert audit.AUTHORITATIVE_READ_FALLBACKS == []


def test_a_failing_store_with_no_mirror_still_notes(tmp_path, monkeypatch, capsys):
    """The [] here may be SHORT of the store, which is the dangerous direction
    (see load_all_experiences), so only a store that reports the object ABSENT
    earns the quiet case below."""
    path = tmp_path / "zeta" / "experience.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _install_backend(monkeypatch, _FakeBackend(raises=_CredentialExpired("token expired")))

    assert audit._read_agent_jsonl_fresh(path) == []
    notes = _notes(capsys)
    assert len(notes) == 1 and str(path) in notes[0] and "_CredentialExpired" in notes[0], notes


def test_no_note_when_the_authoritative_read_succeeds(tmp_path, monkeypatch, capsys):
    path = tmp_path / "echo" / "experience.jsonl"
    _write(path, [_rec("exp-a")])
    _install_backend(monkeypatch, _FakeBackend(
        payload=_jsonl([_rec("exp-a"), _rec("exp-b")]).encode("utf-8")))

    assert [r["id"] for r in audit._read_agent_jsonl_fresh(path)] == ["exp-a", "exp-b"]
    assert _notes(capsys) == []


def test_no_note_when_store_and_mirror_are_both_absent(tmp_path, monkeypatch, capsys):
    """Store and mirror AGREE the object does not exist, so nothing is stale and
    [] is the correct corpus. A note here would be a false alarm, and false alarms
    teach readers to skip the line that matters."""
    path = tmp_path / "delta" / "experience-archive.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _install_backend(monkeypatch, _FakeBackend(raises=FileNotFoundError("no key")))

    assert audit._read_agent_jsonl_fresh(path) == []
    assert _notes(capsys) == []


# ---------------------------------------------------------------------------
# Wiring — the production caller must actually use the fresh reader
# (guard-920: pin the real call path, not only the helper).
# ---------------------------------------------------------------------------


def test_load_all_experiences_routes_through_the_fresh_reader(tmp_path, monkeypatch):
    for agent, rid in (("alpha", "exp-alpha"), ("zeta", "exp-zeta")):
        _write(tmp_path / agent / "experience.jsonl", [_rec(rid + "-local")])
        _write(tmp_path / agent / "experience-archive.jsonl", [_rec(rid + "-arch-local")])

    seen = []

    def _fake_fresh(path):
        seen.append(path)
        return [_rec(path.parent.name + "-" + path.name + "-from-store")]

    monkeypatch.setattr(audit, "agents_root", lambda: tmp_path)
    monkeypatch.setattr(audit, "world_owns_agent_corpus", lambda: True)
    monkeypatch.setattr(audit, "_read_agent_jsonl_fresh", _fake_fresh)

    got = audit.load_all_experiences()

    assert len(seen) == 4, "every live AND archive store must go through the fresh reader"
    assert all(r["id"].endswith("-from-store") for r in got)
    assert not any(r["id"].endswith("-local") for r in got), \
        "load_all_experiences still reached the local mirror"


def test_load_all_experiences_skips_a_foreign_world(tmp_path, monkeypatch):
    """The foreign-world short-circuit must keep precedence over the new read —
    returning another world's records would make every ref dangle."""
    _write(tmp_path / "alpha" / "experience.jsonl", [_rec("exp-alpha")])
    called = []
    monkeypatch.setattr(audit, "agents_root", lambda: tmp_path)
    monkeypatch.setattr(audit, "world_owns_agent_corpus", lambda: False)
    monkeypatch.setattr(audit, "_read_agent_jsonl_fresh",
                        lambda p: called.append(p) or [])

    assert audit.load_all_experiences() == []
    assert called == [], "no store should be read at all for a foreign world"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
