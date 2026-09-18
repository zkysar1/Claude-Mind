""" outcome 1 — wm.write_yaml must not derive a SHARED temp path.

D3 of g-115-9983: ``write_yaml`` computed ``tmp = path.with_suffix('.yaml.tmp')``
— deterministic and shared, not per-writer. Two writers against one target
therefore open the SAME temp file with mode 'w'; each truncates and then writes
at its own offset, so a shorter document's bytes and a longer document's tail
coexist in one temp file and whichever ``replace()`` wins publishes the splice.

Read the failure mode precisely, because the obvious audit misses it:
``tmp.replace()`` IS atomic and fsync-before-rename IS correct (g-001-44). The
atomicity is not the bug. The shared NAME defeats it before the rename is ever
reached, so a reader auditing this function for atomicity finds it correct and
stops.

WHY THIS IS NOT REDUNDANT WITH THE ADVISORY LOCK. Measured 2026-09-18 (echo,
cc-03) by AST over wm.py: all 8 ``write_wm``/``write_yaml`` call sites are
lock-protected — 6 lexically under ``with wm_lock()``, and ``_do_prune`` via its
caller ``cmd_prune``. So the lock contract that
``_fileops._atomic_write_with_fallback`` states ("Caller MUST hold
target_path.with_suffix('.lock') so the partial file cannot collide with another
writer") IS met here. The 13.9 MB corruption happened anyway — the suite log
carries ``lock_stress_b: rc=1 stderr=... FileExistsError`` from the lock itself.
An ADVISORY lock with a staleness break can fail to exclude; a per-writer temp
name cannot. This test pins the property that survives an advisory-lock failure.

Deliberately deterministic: the distinctness test uses a barrier so both writers
are provably inside ``write_yaml`` at once, rather than racing and hoping.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wm  # noqa: E402


def _observed_temp_paths(target: Path, n: int) -> list[Path]:
    """Run n concurrent write_yaml calls, returning the temp path each used.

    ``open`` is wrapped for the duration so every temp path write_yaml chooses
    is recorded. A barrier inside the wrapper holds all n writers with their
    temp file open simultaneously, so a shared name is observed as a shared
    name rather than as two sequential uses of one path.
    """
    real_replace = Path.replace
    seen: list[Path] = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(n, timeout=20)

    def tracking_replace(self, dst):
        # Instrumenting the PUBLISH step, not the open, is what makes this
        # implementation-agnostic: the pre-fix code opens the temp with
        # builtins.open and the fixed code with os.fdopen(mkstemp()), but both
        # end at `tmp.replace(target)`. Patching open would have measured only
        # the old implementation — and silently reported zero temps for the new
        # one, which reads exactly like the test failing to reproduce.
        if str(dst) == str(target):
            with seen_lock:
                seen.append(Path(self))
            # Hold every writer here: each has finished writing its temp and
            # none has published. If the name is shared, all n temps are one
            # inode at this instant.
            barrier.wait()
        return real_replace(self, dst)

    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            wm.write_yaml(target, {"writer": i, "payload": ["x"] * (i + 1)})
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
        "did not see write_yaml's rename, so this run measured nothing"
    )
    return seen, errors


def test_concurrent_writers_do_not_share_a_temp_file(tmp_path):
    """The outcome-1 property: n concurrent writers, n DISTINCT temp paths.

    FAILS against the pre-fix HEAD, where every writer picks
    ``<target>.yaml.tmp``.
    """
    target = tmp_path / "working-memory.yaml"
    seen, errors = _observed_temp_paths(target, 3)

    # Assert the NAME property first: it is the defect, and it is observable
    # before the downstream damage. A shared name also makes the losing
    # writers' replace() raise FileNotFoundError (the winner renamed the one
    # shared inode out from under them) — reported as corroboration, never as
    # the primary signal, so the failure message names the cause not a symptom.
    assert len(set(seen)) == len(seen), (
        "write_yaml derived a SHARED temp path for concurrent writers: "
        f"{[p.name for p in seen]}. Concurrent writers truncate and write into "
        "one inode, so a short document's bytes and a long document's tail "
        "coexist and whichever replace() wins publishes the splice (g-115-9983 D3). "
        f"Corroborating writer errors: {errors!r}"
    )
    assert not errors, f"write_yaml raised under concurrency: {errors!r}"


def test_temp_path_is_not_the_deterministic_sibling(tmp_path):
    """The specific name D3 names must not be the one chosen."""
    target = tmp_path / "working-memory.yaml"
    seen, _errors = _observed_temp_paths(target, 2)
    deterministic = target.with_suffix(".yaml.tmp")

    assert deterministic not in seen, (
        f"write_yaml still uses the deterministic sibling {deterministic.name}; "
        "it is shared by every writer of this target."
    )


def test_result_parses_and_is_exactly_one_writers_document(tmp_path):
    """No splice survives: the published file is one writer's document, whole."""
    target = tmp_path / "working-memory.yaml"
    _observed_temp_paths(target, 3)

    assert target.exists(), (
        "no document was published at all — with a shared temp name the winning "
        "writer renames the one inode away and the losers raise FileNotFoundError "
        "(g-115-9983 D3)"
    )
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"published file did not parse to a mapping: {loaded!r}"
    writer = loaded.get("writer")
    assert writer in (0, 1, 2), f"published file is not one writer's document: {loaded!r}"
    assert loaded.get("payload") == ["x"] * (writer + 1), (
        "published document mixes fields from different writers — a splice: "
        f"{loaded!r}"
    )


def test_fsync_before_replace_is_preserved(tmp_path):
    """Outcome 1 also requires the  durability guarantee to survive.

    Asserts ordering, not merely presence: the fsync must happen while the temp
    handle is open and BEFORE the rename publishes it.
    """
    import os as os_mod

    target = tmp_path / "working-memory.yaml"
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
        wm.write_yaml(target, {"a": 1})
    finally:
        monkey.undo()

    assert "fsync" in events, "write_yaml no longer fsyncs before publishing (g-001-44)"
    assert "replace" in events, "write_yaml no longer publishes via an atomic replace"
    assert events.index("fsync") < events.index("replace"), (
        f"fsync must precede replace; observed order {events}"
    )


def test_no_temp_orphan_is_left_behind(tmp_path):
    """A per-writer temp name must still be consumed by the rename, not leaked."""
    target = tmp_path / "working-memory.yaml"
    wm.write_yaml(target, {"a": 1})

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert not leftovers, f"write_yaml left temp orphan(s) behind: {leftovers}"
