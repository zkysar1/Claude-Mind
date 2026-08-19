"""test_paths_read_local_paths_fail_loud.py —  regression test.

Verifies that _paths.py:_read_local_paths() fails loud (stderr warning) AND
falls through to first-available local-paths.conf when MIND_AGENT names an
agent with no conf file. This prevents the silent {} return that caused
WORLD_DIR/META_DIR to resolve to PROJECT_ROOT/world,meta cruft (g-115-953
root cause).

Two cases:
  (1) MIND_AGENT=<bogus> + at least one real agent conf exists →
      _read_local_paths returns the first-available conf (the cruft fallback
      is NOT taken), stderr emits a WARN line naming the bogus agent.
      WORLD_DIR resolves to the OneDrive/external path (NOT PROJECT_ROOT/world).
  (2) MIND_AGENT unset → unchanged behavior (first-available conf used,
      no warning).

The test snapshots+restores env vars and sys.modules entries per g-115-889
polluter-fix discipline. Without that snapshot, this test itself becomes a
polluter that breaks subsequent tests sharing the same Python process.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


class TestReadLocalPathsFailLoud(unittest.TestCase):
    """Verifies fail-loud behavior on bogus MIND_AGENT."""

    def setUp(self):
        # Snapshot env vars + sys.modules entries we will touch.
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("MIND_AGENT", "MIND_WORLD", "MIND_META", "MIND_AGENT_DIR")
        }
        self._modules_snapshot = {
            name: sys.modules.get(name)
            for name in ("_paths", "_path_helpers")
        }
        # Force a fresh import on each test.
        for name in ("_paths", "_path_helpers"):
            sys.modules.pop(name, None)
        # Ensure no env-var override leaks in.
        for v in ("MIND_WORLD", "MIND_META", "MIND_AGENT_DIR"):
            os.environ.pop(v, None)
        # Make sure core/scripts is importable.
        core_scripts = Path(__file__).resolve().parents[1]
        if str(core_scripts) not in sys.path:
            sys.path.insert(0, str(core_scripts))

    def tearDown(self):
        # Restore env vars exactly as they were.
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Restore sys.modules.
        for name, mod in self._modules_snapshot.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def _fresh_import_paths(self):
        """Import _paths fresh and return the module."""
        sys.modules.pop("_paths", None)
        sys.modules.pop("_path_helpers", None)
        import _paths  # noqa: E402
        return _paths

    def test_bogus_agent_falls_through_with_warning(self):
        """ core repro: MIND_AGENT=bogus → first-available conf,
        WORLD_DIR != PROJECT_ROOT/world, stderr emits WARN."""
        os.environ["MIND_AGENT"] = "test-bogus-zzz"
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            paths = self._fresh_import_paths()

        # Stderr must mention the bogus agent + the fall-through behavior.
        err = stderr_buf.getvalue()
        self.assertIn("test-bogus-zzz", err, "WARN must name the bogus agent")
        self.assertIn("[_paths] WARN", err, "WARN must use the canonical prefix")
        self.assertIn("g-115-960", err, "WARN must cite the fix goal-id")

        # WORLD_DIR must NOT be PROJECT_ROOT/world (the cruft path).
        cruft_world = paths.PROJECT_ROOT / "world"
        self.assertNotEqual(
            paths.WORLD_DIR,
            cruft_world,
            f"WORLD_DIR fell through to cruft path {cruft_world!r} — "
            f"the fail-loud fallback did NOT pick up a real conf. "
            f"Verify at least one PROJECT_ROOT/*/local-paths.conf exists with WORLD_PATH set.",
        )

        # META_DIR symmetric check.
        cruft_meta = paths.PROJECT_ROOT / "meta"
        self.assertNotEqual(
            paths.META_DIR,
            cruft_meta,
            f"META_DIR fell through to cruft path {cruft_meta!r}",
        )

    def test_unset_agent_warns_only_when_ambiguous(self):
        """MIND_AGENT unset → first-available conf used; warn iff 2+ confs.

        This assertion is CONF-COUNT DEPENDENT and must stay that way
        (g-115-6417). It previously asserted a flat "no WARN on unset", which
        was correct only because every box it had ever run on carries exactly
        one conf — the standard fleet topology (measured cc-07: 6 agent dirs,
        1 conf). When _paths.py gained the unset-case warning to reach parity
        with _paths.sh, that flat assertion would have gone RED on the first
        2+-conf box to run it, and it would have read as a real regression on
        that box alone — precisely the cross-box portability red this fleet
        keeps rediscovering. Branch on the count instead of pinning one box's
        topology.
        """
        os.environ.pop("MIND_AGENT", None)
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            paths = self._fresh_import_paths()

        err = stderr_buf.getvalue()
        conf_count = len(list(paths.agents_root().glob("*/local-paths.conf")))
        if conf_count > 1:
            self.assertIn(
                "[_paths] WARN", err,
                f"{conf_count} confs make the unset fall-through ambiguous — "
                f"it must be logged, not silent; got: {err!r}",
            )
        else:
            self.assertNotIn(
                "[_paths] WARN", err,
                f"a single-conf fall-through is unambiguous and must stay "
                f"silent; got: {err!r}",
            )
        # WORLD_DIR still resolves to a real conf.
        cruft_world = paths.PROJECT_ROOT / "world"
        self.assertNotEqual(paths.WORLD_DIR, cruft_world)


if __name__ == "__main__":
    unittest.main()
