"""Tests for _fleet_diary — roster-based fleet diary enumeration ().

The defect this module replaced was invisible to every existing test because a
filesystem glob over a read-through cache is CORRECT on a warm box and blind on
a cold one. So these tests pin the two properties that distinguish the roster
approach from the glob, neither of which the glob had:

  1. An agent whose diary is ABSENT from the local mirror is still ENUMERATED
     (the glob could not enumerate what was not on disk).
  2. The roster is a UNION of team-state shards and agent dirs, so an agent
     named by only one source is still covered. Under-enumeration is the whole
     defect; over-enumeration costs one fail-open skip.
"""

from __future__ import annotations

import sys
import unittest.mock
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _fleet_diary  # noqa: E402


def _mk_agent(root: Path, name: str, lines: list[str] | None = None) -> Path:
    d = root / name / "session"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "execution-diary.jsonl"
    if lines is not None:
        # lines == [] means a genuinely 0-byte diary (an agent whose session
        # started but wrote nothing), NOT a file holding a lone newline.
        p.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# base= seam (used by --agents-root and hermetic tests)
# --------------------------------------------------------------------------

def test_base_enumerates_agent_dirs_and_yields_text(tmp_path):
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])
    _mk_agent(tmp_path, "bravo", ['{"b": 2}'])
    got = dict(_fleet_diary.read_fleet_diaries(tmp_path))
    assert sorted(got) == ["alpha", "bravo"]
    assert '{"a": 1}' in got["alpha"]
    assert '{"b": 2}' in got["bravo"]


def test_agent_dir_without_a_diary_is_skipped_not_fatal(tmp_path):
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])
    (tmp_path / "charlie").mkdir()  # retired: dir exists, no session/ at all
    _mk_agent(tmp_path, "delta")  # session/ exists, diary never written
    got = dict(_fleet_diary.read_fleet_diaries(tmp_path))
    assert sorted(got) == ["alpha"], "one absent diary must not cost the sweep"


def test_empty_diary_yields_nothing_for_that_agent(tmp_path):
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])
    _mk_agent(tmp_path, "bravo", [])  # exists but empty
    got = dict(_fleet_diary.read_fleet_diaries(tmp_path))
    assert sorted(got) == ["alpha"]


def test_missing_base_is_empty_not_an_exception(tmp_path):
    assert list(_fleet_diary.read_fleet_diaries(tmp_path / "nope")) == []


# --------------------------------------------------------------------------
# Roster union (the property the glob did not have)
# --------------------------------------------------------------------------

def test_roster_unions_shards_and_agent_dirs(tmp_path):
    agents = tmp_path / "agents"
    (agents / "alpha").mkdir(parents=True)      # dir only, no shard
    (agents / "bravo").mkdir()
    shards = tmp_path / "world" / "team-state" / "agents"
    shards.mkdir(parents=True)
    (shards / "bravo.yaml").write_text("{}", encoding="utf-8")
    (shards / "echo.yaml").write_text("{}", encoding="utf-8")   # shard only, no dir

    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", tmp_path / "world"):
        names = _fleet_diary.fleet_agent_names()

    assert names == ["alpha", "bravo", "echo"], (
        "roster must UNION agent dirs and team-state shards — an agent named by "
        "only one source is exactly what the old glob dropped"
    )


def test_agent_absent_from_local_mirror_is_still_enumerated(tmp_path):
    """The cold-box case, which the glob could not express.

    `echo` has a shard but NO local dir and NO local diary — precisely a peer
    whose diary has never been pulled to this box. The old glob enumerated by
    on-disk files, so echo was invisible; the roster must still name it, which
    is what gives the backend read a path to fetch.
    """
    agents = tmp_path / "agents"
    (agents / "alpha").mkdir(parents=True)
    shards = tmp_path / "world" / "team-state" / "agents"
    shards.mkdir(parents=True)
    (shards / "echo.yaml").write_text("{}", encoding="utf-8")

    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", tmp_path / "world"):
        names = _fleet_diary.fleet_agent_names()
        # Old-glob equivalent, for contrast: it sees nobody.
        glob_saw = [p.parent.parent.name
                    for p in agents.glob("*/session/execution-diary.jsonl")]

    assert "echo" in names
    assert glob_saw == [], "control: the replaced glob enumerates zero agents here"


# --------------------------------------------------------------------------
# The relative-path trap (owncloud_backend.py:890-892)
# --------------------------------------------------------------------------

def test_diary_paths_handed_to_the_backend_are_absolute(tmp_path, monkeypatch):
    """`read_authoritative_bytes` silently returns LOCAL bytes when `_s3_key`
    raises ValueError, which is what a RELATIVE path produces. A relative path
    would therefore downgrade the store-of-record read to a cache read with no
    error and no warning — so assert the paths are absolute at the call.
    """
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])
    seen: list[Path] = []

    class _Spy:
        def read_authoritative_bytes(self, path):
            seen.append(Path(path))
            return Path(path).read_bytes()

    monkeypatch.chdir(tmp_path)
    with unittest.mock.patch.dict(
        sys.modules,
        {"storage_backend": unittest.mock.Mock(get_backend=lambda: _Spy())},
    ):
        list(_fleet_diary.read_fleet_diaries(tmp_path))

    assert seen, "backend was never consulted — the read path is not exercised"
    assert all(p.is_absolute() for p in seen), f"non-absolute paths reached the backend: {seen}"


def test_backend_failure_falls_back_to_local_read(tmp_path):
    """A backend hiccup must degrade to the local mirror, never kill the sweep."""
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])

    class _Broken:
        def read_authoritative_bytes(self, path):
            raise RuntimeError("S3 unavailable")

    with unittest.mock.patch.dict(
        sys.modules,
        {"storage_backend": unittest.mock.Mock(get_backend=lambda: _Broken())},
    ):
        got = dict(_fleet_diary.read_fleet_diaries(tmp_path))

    assert '{"a": 1}' in got["alpha"]


def test_backend_is_preferred_over_stale_local_bytes(tmp_path):
    """The staleness half: when the mirror and the store disagree, the caller
    must receive the STORE's bytes. Measured on cc-02 2026-07-31: 4 of 5 agents
    had diverged mirrors while the glob reported full coverage."""
    _mk_agent(tmp_path, "alpha", ['{"stale": true}'])

    class _Fresh:
        def read_authoritative_bytes(self, path):
            return b'{"authoritative": true}\n'

    with unittest.mock.patch.dict(
        sys.modules,
        {"storage_backend": unittest.mock.Mock(get_backend=lambda: _Fresh())},
    ):
        got = dict(_fleet_diary.read_fleet_diaries(tmp_path))

    assert "authoritative" in got["alpha"]
    assert "stale" not in got["alpha"]


# --------------------------------------------------------------------------
# read_agent_diary provenance ()
#
# `read_fleet_diaries` discards provenance because its callers only ANALYSE
# content. A caller about to take a DESTRUCTIVE action on the strength of an
# ABSENCE cannot: "the store of record says no work is happening" and "I could
# not reach the store of record and the cache is cold" are the same empty
# string and license opposite decisions. The stranded-claim sweep patches this
# function in its own tests, so these are the only tests that pin what it
# actually returns.
# --------------------------------------------------------------------------

def _with_backend(backend):
    return unittest.mock.patch.dict(
        sys.modules,
        {"storage_backend": unittest.mock.Mock(get_backend=lambda: backend)},
    )


def test_read_agent_diary_reports_authoritative(tmp_path):
    _mk_agent(tmp_path, "alpha", ['{"stale": true}'])

    class _Fresh:
        def read_authoritative_bytes(self, path):
            return b'{"authoritative": true}\n'

    with _with_backend(_Fresh()):
        text, prov = _fleet_diary.read_agent_diary("alpha", tmp_path)

    assert prov == "authoritative"
    assert "authoritative" in text and "stale" not in text


def test_read_agent_diary_absent_does_not_fall_back_to_the_mirror(tmp_path):
    """A positive 'not there' from the store must NOT be second-guessed locally.

    This is the semantic the whole provenance split exists for. Falling back
    here would let cache bytes answer a question the store already answered,
    re-installing the mirror as an authority — and a caller that keys a
    destructive decision on `absent` would be reading the cache while believing
    it read the store of record.
    """
    _mk_agent(tmp_path, "alpha", ['{"local": "leftover"}'])

    class _Gone:
        def read_authoritative_bytes(self, path):
            raise FileNotFoundError(path)

    with _with_backend(_Gone()):
        text, prov = _fleet_diary.read_agent_diary("alpha", tmp_path)

    assert prov == "absent"
    assert text is None, "local mirror bytes leaked into an 'absent' verdict"


def test_read_agent_diary_backend_error_is_labelled_local_mirror(tmp_path):
    """Degrading to the cache is allowed; SILENTLY degrading is not."""
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])

    class _Broken:
        def read_authoritative_bytes(self, path):
            raise RuntimeError("S3 unavailable")

    with _with_backend(_Broken()):
        text, prov = _fleet_diary.read_agent_diary("alpha", tmp_path)

    assert prov == "local-mirror"
    assert '{"a": 1}' in text


def test_read_agent_diary_no_backend_is_local_mirror(tmp_path):
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])
    text, prov = _fleet_diary.read_agent_diary("alpha", tmp_path, backend=None)
    assert prov == "local-mirror"
    assert '{"a": 1}' in text


def test_read_agent_diary_error_when_neither_path_yields_bytes(tmp_path):
    (tmp_path / "alpha").mkdir()  # no session/, no diary
    text, prov = _fleet_diary.read_agent_diary("alpha", tmp_path, backend=None)
    assert (text, prov) == (None, "error")


def test_read_agent_diary_hands_the_backend_an_absolute_path(tmp_path, monkeypatch):
    """Same relative-path trap as the fleet reader — pinned on this entry point too,
    because a relative path here would silently return cache bytes LABELLED
    `authoritative`, which is strictly worse than the unlabelled version."""
    _mk_agent(tmp_path, "alpha", ['{"a": 1}'])
    seen: list[Path] = []

    class _Spy:
        def read_authoritative_bytes(self, path):
            seen.append(Path(path))
            return Path(path).read_bytes()

    monkeypatch.chdir(tmp_path)
    with _with_backend(_Spy()):
        _, prov = _fleet_diary.read_agent_diary("alpha", tmp_path)

    assert prov == "authoritative"
    assert seen and seen[0].is_absolute(), f"non-absolute path reached backend: {seen}"
