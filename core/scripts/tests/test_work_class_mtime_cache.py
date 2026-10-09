"""test_work_class_mtime_cache.py — regression pin for  / guard-7253.

Background (measured 2026-09-26, fresh-eyes-code):
  `_work_class._load()` was `@lru_cache(maxsize=1)` with NO key, so a mapping
  edit (core or world overlay) was invisible to every process that had already
  called `resolve()` until that process restarted. The 2026-09-26
  environment-mind-bridge re-class (hygiene -> product) read green from a fresh
  CLI process while this box's daemon kept stamping the stale class until
  mind-api-start.sh --restart — a daemon-read config behaving like code
  (guard-7253), with the symptom invisible to any check that spawned a fresh
  process.

Fix (g-115-11062):
  `_work_class._load()` computes a (core_path, core_mtime_ns, overlay_path,
  overlay_mtime_ns) signature and memoizes the parse via `_load_keyed`
  (lru_cache(maxsize=8)). The world overlay's OWN per-process cache
  (`_world_config._CACHE`) became mtime-aware in the same change: it stores
  (path, mtime_ns, data) per name and invalidates on file change. Keying only
  the outer loader would re-parse while the overlay loader still served its
  stale cached overlay, so the two halves land together.

This test pins the properties a future refactor must not silently break:

  1. Same-process overlay edit is seen by resolve() WITHOUT a restart
     (the goal's verification outcome 1, verbatim).
  2. Multi-flip: two successive edits in one process are both seen.
  3. Deletion of the overlay reverts to the core value in the same process.
  4. A CORE mapping edit is also seen in the same process (core half of the
     signature; monkeypatched _CONFIG_PATH so the real core file is untouched).
  5. The parse stays memoized per signature: with no file change, repeated
     resolve() hits the lru_cache (no re-parse) — the single-parse-per-
     signature behaviour the goal asks to keep.

Run: py -3 -m pytest core/scripts/tests/test_work_class_mtime_cache.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# A category the CORE mapping does not carry (checked against
# core/config/work-class-mapping.yaml at pin time), so the overlay is the only
# thing that can map it — the test's signal is unambiguous.
PROBE_CATEGORY = "zc-g11511062-probe"


def _load_work_class_module(world_dir: Path) -> "object":
    """Import a FRESH _work_class module object per test.

    A fresh module gets a fresh `_load_keyed` lru_cache and a fresh module
    namespace for `_CONFIG_PATH` (test 4 monkeypatches it), so tests never
    share memo state. MIND_WORLD is pinned to the sandbox so the overlay
    loader reads the sandbox file, never the deployment's real world overlay.
    """
    spec = importlib.util.spec_from_file_location(
        f"_work_class_test_{id(world_dir)}_{time.monotonic_ns()}",
        CORE_SCRIPTS / "_work_class.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_overlay(world_dir: Path, text: str, bump: float) -> None:
    """Write the sandbox overlay and force a DISTINCT mtime_ns.

    Two writes within the same nanosecond are theoretically possible on
    coarse filesystems; an explicit utime keeps the signature flip
    deterministic (the fix keys on st_mtime_ns).
    """
    overlay = world_dir / "config" / "work-class-mapping.yaml"
    overlay.write_text(text, encoding="utf-8")
    now = time.time()
    os.utime(overlay, (now + bump, now + bump))


class _WorldSandbox:
    """Temp world dir with config/ and MIND_WORLD pinned for its lifetime."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="work-class-mtime-")
        self.world = Path(self._tmp.name)
        (self.world / "config").mkdir()
        self._old_env = os.environ.get("MIND_WORLD")

    def __enter__(self) -> "Path":
        os.environ["MIND_WORLD"] = str(self.world)
        return self.world

    def __exit__(self, *exc) -> None:
        if self._old_env is None:
            os.environ.pop("MIND_WORLD", None)
        else:
            os.environ["MIND_WORLD"] = self._old_env
        self._tmp.cleanup()


def test_overlay_edit_seen_by_resolve_same_process():
    """Outcome 1 (verbatim): resolve() returns the new value in the SAME
    process after the overlay file changes — no restart. Pre-fix this test
    fails on the second assertion: the keyless lru_cache serves the first
    parse forever."""
    with _WorldSandbox() as world:
        _write_overlay(world, f"mapping:\n  {PROBE_CATEGORY}: hygiene\n", 0.0)
        mod = _load_work_class_module(world)

        first = mod.resolve(PROBE_CATEGORY)
        assert first == "hygiene", f"sanity: expected 'hygiene', got {first!r}"

        # Edit the overlay between two resolve() calls in ONE process.
        _write_overlay(world, f"mapping:\n  {PROBE_CATEGORY}: product\n", 1.0)
        second = mod.resolve(PROBE_CATEGORY)
        assert second == "product", (
            f"same-process overlay edit NOT seen: expected 'product', "
            f"got {second!r}. Did _load() lose its (path, mtime) key, or the "
            f"overlay loader lose its mtime-aware cache entry? (g-115-11062)"
        )


def test_overlay_multi_flip_and_removal():
    """Two successive edits are both seen, then deleting the overlay reverts
    to the core value in the same process. Covers the deletion half of the
    signature (missing file -> constant (None, -1) key part) and rules out a
    fix that only handles one direction."""
    with _WorldSandbox() as world:
        _write_overlay(world, f"mapping:\n  {PROBE_CATEGORY}: hygiene\n", 0.0)
        mod = _load_work_class_module(world)

        assert mod.resolve(PROBE_CATEGORY) == "hygiene"

        _write_overlay(world, f"mapping:\n  {PROBE_CATEGORY}: product\n", 1.0)
        assert mod.resolve(PROBE_CATEGORY) == "product", (
            "second flip in one process not seen"
        )

        (world / "config" / "work-class-mapping.yaml").unlink()
        assert mod.resolve(PROBE_CATEGORY) == "unclassified", (
            "overlay deletion not seen in the same process — the probe "
            "category is core-unmapped, so core-only must give the default "
            "'unclassified'. The missing-file signature part regressed."
        )


def test_core_mapping_edit_seen_same_process(tmp_path: Path):
    """Outcome 1's core half: a CORE mapping edit is seen in the same
    process. _CONFIG_PATH is monkeypatched to a temp file so the real core
    mapping is never touched by the test."""
    with _WorldSandbox() as world:
        (world / "config" / "work-class-mapping.yaml").write_text(
            "mapping: {}\n", encoding="utf-8"
        )
        core_yaml = tmp_path / "work-class-mapping.yaml"
        core_yaml.write_text(
            "default: unclassified\nmapping:\n  "
            f"{PROBE_CATEGORY}: research\n",
            encoding="utf-8",
        )
        mod = _load_work_class_module(world)
        mod._CONFIG_PATH = core_yaml  # sandbox the core half of the signature

        assert mod.resolve(PROBE_CATEGORY) == "research"

        now = time.time()
        core_yaml.write_text(
            "default: unclassified\nmapping:\n  "
            f"{PROBE_CATEGORY}: framework\n",
            encoding="utf-8",
        )
        os.utime(core_yaml, (now + 2.0, now + 2.0))

        assert mod.resolve(PROBE_CATEGORY) == "framework", (
            "core-mapping edit not seen in the same process — the core "
            "mtime half of the signature regressed (g-115-11062)."
        )


def test_parse_stays_memoized_per_signature():
    """The single-parse-per-signature behaviour the goal asks to keep: with
    no file change, repeated resolve() hits the lru_cache instead of
    re-parsing. The fix must not degrade to re-reading both files on every
    resolve() call (that would be a perf regression on the daemon's
    add-goal path)."""
    with _WorldSandbox() as world:
        _write_overlay(world, f"mapping:\n  {PROBE_CATEGORY}: hygiene\n", 0.0)
        mod = _load_work_class_module(world)

        mod.resolve(PROBE_CATEGORY)
        after_first = mod._load_keyed.cache_info()
        assert after_first.misses == 1, (
            f"expected exactly one parse after the first resolve, got "
            f"misses={after_first.misses}"
        )

        for _ in range(5):
            mod.resolve(PROBE_CATEGORY)
        after_seventh = mod._load_keyed.cache_info()
        assert after_seventh.misses == 1, (
            f"parse re-ran on unchanged files (misses={after_seventh.misses}) "
            f"— the memoization regressed to per-call re-read."
        )
        assert after_seventh.hits == after_first.hits + 5


