""" outcome 1 — the DAEMON endpoint's _write_wm must not derive a SHARED temp path.

The daemon twin of ``core/scripts/tests/test_wm_write_yaml_temp_path.py``.

WHY A SECOND COPY OF THAT TEST IS NOT REDUNDANT. g-115-9983 outcome 1 replaced
the deterministic ``path.with_suffix('.yaml.tmp')`` in ``core/scripts/wm.py``
with ``mkstemp`` — and that is a path PRODUCTION DOES NOT TAKE. ``wm-set.sh`` and
``wm-append.sh`` are daemon-only (``rt_call POST /v1/wm/set``, no Python CLI
fallback — ``.claude/rules/no-python-cli-fallback.md``, 35 wrappers migrated and
the CLI deleted), so every production working-memory write resolves HERE, to
``mind_api/src/endpoints/wm_write.py::_write_wm``. The repaired file was the
test-side writer; the live one kept the defect byte-identical. Found by
/fresh-eyes-code (echo, cc-03, 2026-09-18; board msg-20260918-033104-echo-5764),
re-verified at HEAD by alpha (cc-04, 2026-09-20) — the line had drifted 517→571
and the defect was unchanged.

WHY THE LOCK IS NOT THE DEFENCE. ``_wm_lock(ctx)`` is
``file_locks.locked(_wm_path(ctx), stale_seconds=10)`` — ADVISORY, with a
TEN-SECOND staleness break. A 13.9 MB ``yaml.dump`` can exceed that window under
load, at which point a second writer enters legitimately, both truncate the same
inode, and whichever ``replace()`` wins publishes the splice. That is not
hypothetical: all 8 wm.py write call sites WERE lock-protected and the
corruption happened anyway, the suite log carrying a ``FileExistsError`` from
the lock itself. An advisory lock with a staleness break can fail to exclude; a
per-writer temp name cannot.

Deliberately deterministic: a barrier holds every writer with its temp open and
unpublished, so a shared name is observed AS a shared name rather than raced for.

FIXTURE NAMING: the target is ``wm-under-test.yaml``, never the real store's
basename. ``_write_wm`` takes an arbitrary path, so the name is free — and using
a neutral one keeps this test provably independent of the live working memory.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mind_api.src.endpoints import wm_write  # noqa: E402

TARGET_NAME = "wm-under-test.yaml"


def _observed_temp_paths(target: Path, n: int):
    """Run n concurrent _write_wm calls, returning the temp path each used.

    Instruments the PUBLISH step (``Path.replace``), not the open. That is what
    makes the probe implementation-agnostic: the pre-fix code opens its temp with
    ``builtins.open`` and the fixed code with ``os.fdopen(mkstemp())``, but both
    end at ``tmp.replace(target)``. Patching ``open`` would have measured only the
    old implementation and reported zero temps for the new one — which reads
    exactly like the test failing to reproduce.
    """
    real_replace = Path.replace
    seen: list[Path] = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(n, timeout=20)

    def tracking_replace(self, dst):
        if str(dst) == str(target):
            with seen_lock:
                seen.append(Path(self))
            # Every writer has finished its temp and none has published. If the
            # name is shared, all n temps are ONE inode at this instant.
            barrier.wait()
        return real_replace(self, dst)

    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            wm_write._write_wm(target, {"writer": i, "payload": ["x"] * (i + 1)})
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(Path, "replace", tracking_replace)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        monkey.undo()

    assert len(seen) == n, (
        f"expected {n} publish attempts, observed {len(seen)} — the instrument "
        "did not see _write_wm's rename, so this run measured nothing"
    )
    return seen, errors


def test_concurrent_daemon_writers_do_not_share_a_temp_file(tmp_path):
    """The outcome-1 property: n concurrent writers, n DISTINCT temp paths.

    FAILS against pre-fix HEAD, where every writer picks ``<target>.yaml.tmp``.
    """
    target = tmp_path / TARGET_NAME
    seen, errors = _observed_temp_paths(target, 3)

    assert len(set(seen)) == len(seen), (
        "the daemon endpoint's _write_wm derived a SHARED temp path for "
        f"concurrent writers: {[p.name for p in seen]}. Concurrent writers "
        "truncate and write into one inode, so a short document's bytes and a "
        "long document's tail coexist and whichever replace() wins publishes the "
        f"splice (g-115-9983 D3). Corroborating writer errors: {errors!r}"
    )
    assert not errors, f"_write_wm raised under concurrency: {errors!r}"


def test_daemon_temp_path_is_not_the_deterministic_sibling(tmp_path):
    """The specific name the defect uses must not be the one chosen."""
    target = tmp_path / TARGET_NAME
    seen, _errors = _observed_temp_paths(target, 2)
    deterministic = target.with_suffix(".yaml.tmp")

    assert deterministic not in seen, (
        f"_write_wm still uses the deterministic sibling {deterministic.name}; "
        "it is shared by every writer of this target."
    )


def test_daemon_result_parses_and_is_exactly_one_writers_document(tmp_path):
    """No splice survives: the published document is one writer's, whole."""
    target = tmp_path / TARGET_NAME
    _observed_temp_paths(target, 3)

    assert target.exists(), (
        "no document was published at all — with a shared temp name the winning "
        "writer renames the one inode away and the losers raise FileNotFoundError"
    )
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"published file did not parse to a mapping: {loaded!r}"
    writer = loaded.get("writer")
    assert writer in (0, 1, 2), f"published file is not one writer's document: {loaded!r}"
    assert loaded.get("payload") == ["x"] * (writer + 1), (
        f"published document mixes fields from different writers — a splice: {loaded!r}"
    )


def test_daemon_fsync_before_replace_is_preserved(tmp_path):
    """ durability must survive the temp-name change.

    Asserts ORDERING, not merely presence: the fsync must happen while the temp
    handle is open and BEFORE the rename publishes it.
    """
    import os as os_mod

    target = tmp_path / TARGET_NAME
    events: list[str] = []

    real_fsync = os_mod.fsync
    real_replace = Path.replace

    def tracking_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def tracking_replace(self, dst):
        events.append("replace")
        return real_replace(self, dst)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(os_mod, "fsync", tracking_fsync)
        monkey.setattr(Path, "replace", tracking_replace)
        wm_write._write_wm(target, {"a": 1})
    finally:
        monkey.undo()

    assert "fsync" in events, "_write_wm no longer fsyncs before publishing (g-001-44)"
    assert "replace" in events, "_write_wm no longer publishes via an atomic replace"
    assert events.index("fsync") < events.index("replace"), (
        f"fsync must precede replace; observed order {events}"
    )


def test_daemon_no_temp_orphan_is_left_behind(tmp_path):
    """A per-writer temp name must still be consumed by the rename, not leaked.

    The failure direction that matters: unlike the old shared name, a leaked
    per-writer temp is never reused, so a leak accumulates one orphan per write.
    """
    target = tmp_path / TARGET_NAME
    wm_write._write_wm(target, {"a": 1})

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert not leftovers, f"_write_wm left temp orphan(s) behind: {leftovers}"


def test_daemon_published_file_is_readable_by_other_uids(tmp_path):
    """mkstemp creates 0600; a governed file that becomes unreadable is a regression.

    rb-4790: a cross-uid reader (a peer Body, a sweep running as another user)
    silently loses access if the publish inherits mkstemp's private mode instead
    of the umask default or the existing target's.
    """
    import stat as stat_mod

    target = tmp_path / TARGET_NAME
    wm_write._write_wm(target, {"a": 1})
    mode = stat_mod.S_IMODE(target.stat().st_mode)
    assert mode & stat_mod.S_IRGRP and mode & stat_mod.S_IROTH, (
        f"published file is mode {oct(mode)} — mkstemp's 0600 leaked through "
        "instead of the umask default (rb-4790)"
    )


def test_daemon_preserves_an_existing_targets_mode(tmp_path):
    """A rewrite must not silently widen or narrow an existing file's mode."""
    import stat as stat_mod

    target = tmp_path / TARGET_NAME
    wm_write._write_wm(target, {"a": 1})
    target.chmod(0o640)
    wm_write._write_wm(target, {"a": 2})
    mode = stat_mod.S_IMODE(target.stat().st_mode)
    assert mode == 0o640, (
        f"_write_wm changed an existing target's mode to {oct(mode)}; it must "
        "copy the mode it found (0o640)"
    )
