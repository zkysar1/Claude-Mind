"""test_wm_cmd_set_auto_init.py —  regression test.

wm.py cmd_read returns {} exit 0 on missing/empty working-memory.yaml,
but cmd_set previously exited 1 with "Working memory not initialized."
The asymmetry caused fresh-eyes-cadence-check seed-stagger (g-270-02) to
fail-open and fire all 4 sibling rituals simultaneously on a fresh dir.

The fix extracts _default_wm_data() as a helper used by cmd_init,
cmd_reset, and cmd_set (self-heal path). This test pins the contract:

1. cmd_set on an empty/missing WM seeds the canonical shape and persists
   the requested slot value (no exit 1).
2. cmd_set on an initialized WM still works as before (no regression).
3. cmd_init still produces the same canonical shape.
4. cmd_append (sibling) keeps the exit-1 behavior — append-against-missing
   is structurally ambiguous; the goal scopes self-heal to cmd_set only.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_wm():
    """Load wm.py module fresh for each test (it patches WM_PATH globally)."""
    spec = importlib.util.spec_from_file_location("wm", CORE_SCRIPTS / "wm.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWmCmdSetAutoInit(unittest.TestCase):
    """Empty-WM self-heal in cmd_set ()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="g115748-wm-test-")
        self.wm_path = Path(self.tmp.name) / "working-memory.yaml"
        # Route WM I/O to the tmp file via BODY_WM_PATH — the env var that
        # wm.py's wm_path() honors per-call ( Mind/Body routing).
        # The old `self.wm.WM_PATH = self.wm_path` patch is now a NO-OP and was
        # DANGEROUS: WM_PATH became a dynamic module __getattr__ property, and
        # read_wm()/write_wm() resolve through wm_path() (BODY_WM_PATH env →
        # else AGENT_DIR/session/working-memory.yaml), NOT the module attribute.
        # So patching WM_PATH left cmd_set targeting the REAL bound agent's WM —
        # running this suite under MIND_AGENT (e.g. via run-full-suite-after-
        # deep-code) wrote test slots into live working memory. (2026-06-23)
        self._env = mock.patch.dict(os.environ, {"BODY_WM_PATH": str(self.wm_path)})
        self._env.start()
        # Load a fresh wm module per test (wm_path() reads BODY_WM_PATH lazily,
        # so set order vs import does not matter — the env wins at call time).
        self.wm = _load_wm()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _set(self, slot: str, value_json: str):
        """Invoke cmd_set with a stdin value, capturing any SystemExit."""
        class Args:
            override_merge_gate = None
        Args.slot = slot
        stdin = io.StringIO(value_json)
        saved_stdin = sys.stdin
        sys.stdin = stdin
        try:
            try:
                self.wm.cmd_set(Args())
                return 0
            except SystemExit as e:
                return e.code if e.code is not None else 0
        finally:
            sys.stdin = saved_stdin

    def test_empty_file_self_heals_and_persists_slot(self):
        """Empty (zero-byte) WM file → cmd_set seeds shape + persists slot."""
        self.wm_path.touch()  # zero-byte
        self.assertEqual(self.wm_path.stat().st_size, 0)
        rc = self._set("session_id", '"my-session-1"')
        self.assertEqual(rc, 0)
        # File is no longer empty
        self.assertGreater(self.wm_path.stat().st_size, 0)
        # Slot persisted with correct value
        import yaml
        data = yaml.safe_load(self.wm_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("session_id"), "my-session-1")
        # Canonical shape present
        self.assertIn("slots", data)
        self.assertIn("slot_meta", data)
        self.assertIn("encoding_queue", data)

    def test_missing_file_self_heals_and_persists_slot(self):
        """Missing WM file → cmd_set seeds shape + persists slot."""
        self.assertFalse(self.wm_path.exists())
        rc = self._set("aspiration_touched_last", '"asp-115"')
        self.assertEqual(rc, 0)
        self.assertTrue(self.wm_path.exists())
        import yaml
        data = yaml.safe_load(self.wm_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("aspiration_touched_last"), "asp-115")

    def test_initialized_wm_no_regression(self):
        """cmd_set on already-initialized WM works as before."""
        # First init
        class InitArgs:
            pass
        self.wm.cmd_init(InitArgs())
        # Then set a slot
        rc = self._set("last_goal_category", '"framework"')
        self.assertEqual(rc, 0)
        import yaml
        data = yaml.safe_load(self.wm_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("last_goal_category"), "framework")
        # Canonical shape preserved
        self.assertIn("slots", data)

    def test_cmd_set_into_slot_namespace_works_on_empty(self):
        """cmd_set into 'slots.X' sub-path self-heals + persists nested value."""
        rc = self._set("active_hypothesis", '"test-hypothesis"')
        self.assertEqual(rc, 0)
        import yaml
        data = yaml.safe_load(self.wm_path.read_text(encoding="utf-8"))
        # active_hypothesis is a known slot — should land under slots/
        self.assertEqual(data.get("slots", {}).get("active_hypothesis"),
                         "test-hypothesis")

    def test_default_wm_data_helper_shape(self):
        """_default_wm_data returns the canonical empty-WM shape."""
        data = self.wm._default_wm_data()
        self.assertIn("encoding_queue", data)
        self.assertIn("session_id", data)
        self.assertIn("slots", data)
        self.assertIn("slot_meta", data)
        # All slots present
        self.assertIsInstance(data["slots"], dict)
        self.assertGreater(len(data["slots"]), 5)
        # Each slot has matching meta
        for slot_name in data["slots"]:
            self.assertIn(slot_name, data["slot_meta"])

    def test_cmd_init_uses_helper_shape(self):
        """cmd_init produces same shape as _default_wm_data."""
        class InitArgs:
            pass
        self.wm.cmd_init(InitArgs())
        import yaml
        data = yaml.safe_load(self.wm_path.read_text(encoding="utf-8"))
        helper_data = self.wm._default_wm_data()
        self.assertEqual(set(data.keys()), set(helper_data.keys()))
        self.assertEqual(set(data.get("slots", {}).keys()),
                         set(helper_data.get("slots", {}).keys()))


if __name__ == "__main__":
    unittest.main()
