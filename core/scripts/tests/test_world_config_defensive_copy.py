"""test_world_config_defensive_copy.py — regression pin for  / rb-1050.

Background (2026-05-18):
  Fresh-eyes-code finding msg-20260518-013711-bravo-1289 identified that
  `_world_config.load_world_config()` had an asymmetric defensive-copy
  pattern: three default paths used `dict(default)` (shallow copy of
  fallback) but the success path set `result = data` directly with no
  copy. The brief proposed a one-line fix (change `result = data` to
  `result = dict(data)`).

  In implementation (g-115-901), test-driven exploration revealed that
  the one-line fix was necessary but NOT sufficient: `result` and
  `_CACHE[name]` share identity on every path, AND `return _CACHE[name]`
  on cache hit also shares identity. Caller mutation still polluted the
  cache for subsequent in-process callers.

  Fix scope expanded to a defensive-copy-on-every-return pattern:
    - Cache hit (line 104): `return dict(_CACHE[name])` (was `return _CACHE[name]`)
    - First-store return (line ~153): `return dict(result)` (was `return result`)
    - Intermediate `result = dict(data)` on success path kept for
      protection of yaml.safe_load's output from the cache slot.

  This preserves the cache's value (parse once per process) while making
  caller-level mutations truly local — caller A cannot pollute caller B.

This test pins three properties so a future refactor cannot silently
re-introduce any of the regression paths:

  1. Mutate returned dict from success path -> fresh load returns unmutated
  2. Mutate returned dict from default path -> fresh load returns unmutated
  3. Two callers receive DISTINCT dict objects with equal values
     (defensive copy on every cache hit)

Run: py -3 -m pytest core/scripts/tests/test_world_config_defensive_copy.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_module(world_dir):
    """Import _world_config with MIND_WORLD pinned to a sandbox.

    Module-level _CACHE persists across reloads of the same module object,
    so we use importlib spec_from_file_location with a fresh module name
    per call to guarantee isolation per-test.
    """
    spec = importlib.util.spec_from_file_location(
        f"_world_config_test_{id(world_dir)}",
        CORE_SCRIPTS / "_world_config.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_success_path_returns_defensive_copy():
    """Case 1: caller mutation of returned dict from a present YAML file
    must NOT pollute the cache. Fresh load returns unmutated values."""
    with tempfile.TemporaryDirectory(prefix="world-defensive-copy-") as tmp:
        world = Path(tmp)
        (world / "config").mkdir()
        (world / "config" / "test-fixture.yaml").write_text(
            "fruit: apple\ncount: 1\n",
            encoding="utf-8",
        )

        old_env = os.environ.get("MIND_WORLD")
        os.environ["MIND_WORLD"] = str(world)
        try:
            mod = _load_module(world)
            cfg = mod.load_world_config("test-fixture", default={})
            assert cfg["fruit"] == "apple", f"sanity check failed: {cfg}"

            # Mutate the caller-visible dict. Without the defensive copy at
            # line 128, this assignment leaks into _CACHE["test-fixture"].
            cfg["fruit"] = "POLLUTED"
            cfg["new_key"] = "INJECTED"

            # Fresh load — must show original values, not polluted ones.
            cfg2 = mod.load_world_config("test-fixture", default={})
            assert cfg2["fruit"] == "apple", (
                f"cache polluted by caller mutation — expected 'apple', "
                f"got {cfg2['fruit']!r}. Did line 128 lose its defensive "
                f"copy?"
            )
            assert "new_key" not in cfg2, (
                f"cache polluted by injected key — _CACHE leaked caller's "
                f"new_key. Did line 128 lose its defensive copy?"
            )
        finally:
            if old_env is None:
                os.environ.pop("MIND_WORLD", None)
            else:
                os.environ["MIND_WORLD"] = old_env


def test_default_path_returns_defensive_copy():
    """Case 2: caller mutation of returned dict from the default path (no
    overlay file present) must NOT pollute the cache. This worked before
    the fix; pinned for symmetry so any future asymmetry-flip catches it."""
    with tempfile.TemporaryDirectory(prefix="world-defensive-copy-default-") as tmp:
        world = Path(tmp)
        (world / "config").mkdir()  # exists but no overlay file inside

        old_env = os.environ.get("MIND_WORLD")
        os.environ["MIND_WORLD"] = str(world)
        try:
            mod = _load_module(world)
            default = {"k": "default-value"}
            cfg = mod.load_world_config("missing-fixture", default=default)
            assert cfg["k"] == "default-value"

            cfg["k"] = "POLLUTED"
            cfg["new_key"] = "INJECTED"

            cfg2 = mod.load_world_config("missing-fixture", default=default)
            assert cfg2["k"] == "default-value", (
                f"default-path cache polluted — expected 'default-value', "
                f"got {cfg2['k']!r}"
            )
            assert "new_key" not in cfg2

            # Also: caller's `default` dict must not be the same object as
            # the returned dict (this is the whole point of dict(default)).
            assert cfg is not default
            assert cfg2 is not default
        finally:
            if old_env is None:
                os.environ.pop("MIND_WORLD", None)
            else:
                os.environ["MIND_WORLD"] = old_env


def test_subsequent_calls_return_distinct_objects():
    """Case 3: per-call defensive copy — two subsequent calls return
    DISTINCT dict objects (same values, different identity). This is the
    contract that makes Case 1 and Case 2 work: the cache holds the
    canonical snapshot, every caller gets a fresh shallow copy so two
    callers cannot pollute each other.

    Pre-fix (g-115-901), two subsequent calls returned the SAME dict
    object — caller A's mutations were visible to caller B and persisted
    in the cache forever. The fix moves defensive copying from
    parse-time-only (which only protected yaml.safe_load's output) to
    every return path (which protects subsequent in-process callers).

    Equality is preserved; identity is not.
    """
    with tempfile.TemporaryDirectory(prefix="world-defensive-copy-distinct-") as tmp:
        world = Path(tmp)
        (world / "config").mkdir()
        (world / "config" / "shared.yaml").write_text("x: 1\n", encoding="utf-8")

        old_env = os.environ.get("MIND_WORLD")
        os.environ["MIND_WORLD"] = str(world)
        try:
            mod = _load_module(world)
            cfg_a = mod.load_world_config("shared", default={})
            cfg_b = mod.load_world_config("shared", default={})

            # Values equal — same canonical snapshot.
            assert cfg_a == cfg_b, f"values diverge: {cfg_a} vs {cfg_b}"
            # Identity distinct — defensive copy per call.
            assert cfg_a is not cfg_b, (
                "two callers got the SAME dict object — defensive copy on "
                "cache hit regressed. Caller A's mutations would now leak "
                "into caller B."
            )

            # Cross-caller isolation: mutating A must not affect B.
            cfg_a["x"] = 999
            assert cfg_b["x"] == 1, (
                f"caller A's mutation leaked into caller B's dict; "
                f"defensive copy on cache hit regressed. cfg_b['x']={cfg_b['x']}"
            )
            # And a fresh third call must also see the canonical value.
            cfg_c = mod.load_world_config("shared", default={})
            assert cfg_c["x"] == 1, (
                f"caller A polluted the cache; defensive copy regressed. "
                f"cfg_c['x']={cfg_c['x']}"
            )
        finally:
            if old_env is None:
                os.environ.pop("MIND_WORLD", None)
            else:
                os.environ["MIND_WORLD"] = old_env


def main():
    import traceback
    tests = [
        ("success_path_returns_defensive_copy",
         test_success_path_returns_defensive_copy),
        ("default_path_returns_defensive_copy",
         test_default_path_returns_defensive_copy),
        ("subsequent_calls_share_cache_object",
         test_subsequent_calls_share_cache_object),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failed.append(name)

    if failed:
        print(f"\n{len(failed)}/{len(tests)} test(s) failed: {failed}")
        return 1
    print(f"\n{len(tests)}/{len(tests)} test(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
