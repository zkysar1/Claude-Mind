"""Core default `inbound-drain` slot + the engine's destination fence ().

WHY THESE EXIST. The feature shipped as a world-only slot, and `world/` is
excluded from the seed and is not in git — so the environment hosts the drain
exists to serve were the exact population that could never carry it (measured 0
of 45 live workspaces). Moving the engine into core fixes reachability; these
tests pin the two properties that move must not break:

  1. ORDER. A world that fills the hook still wins. If that inverts, the default
     silently replaces every domain override and nothing fails.
  2. THE FENCE. The slot guard is SOURCE-side (which spool is read) and cannot
     constrain where the engine FILES, because filing goes through the local
     runtime, which takes no destination argument. A box with the two env vars
     set passes the slot guard and would file another member's instructions into
     its own queue — silently, because a low-numbered aspiration id resolves
     almost everywhere. Every fence test therefore has a NEGATIVE control: a
     fence that always refuses would pass the refusal tests alone and would have
     disabled the feature.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CORE / filename)
    assert spec and spec.loader, f"cannot load {filename}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine = _load("inbound_drain_under_test", "inbound_drain.py")
runner = _load("inbound_drain_run_under_test", "inbound-drain-run.py")


class SlotResolutionOrder(unittest.TestCase):
    """The override must outrank the default, and absence must stay distinguishable."""

    def setUp(self):
        self._orig = runner._world_path

    def tearDown(self):
        runner._world_path = self._orig

    def _world_with_slot(self, tmp: Path) -> Path:
        d = tmp / "scripts"
        d.mkdir(parents=True, exist_ok=True)
        (d / "inbound-drain.sh").write_text("#!/usr/bin/env bash\necho '{}'\n", encoding="utf-8")
        return tmp

    def test_world_slot_wins_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            w = self._world_with_slot(Path(td))
            runner._world_path = lambda: w
            slot, source = runner._resolve_slot(None)
            self.assertEqual(source, "world")
            self.assertEqual(slot, w / "scripts" / "inbound-drain.sh")

    def test_falls_back_to_core_default_when_world_slot_absent(self):
        with tempfile.TemporaryDirectory() as td:
            runner._world_path = lambda: Path(td)      # exists, but fills no slot
            slot, source = runner._resolve_slot(None)
            self.assertEqual(source, "core-default")
            self.assertEqual(slot, runner.DEFAULT_SLOT)
            self.assertTrue(slot.is_file(), "the core default must ship with core")

    def test_falls_back_when_world_path_unresolvable(self):
        """An unresolvable world is the seeded-host case — it must NOT disable the drain."""
        runner._world_path = lambda: None
        _, source = runner._resolve_slot(None)
        self.assertEqual(source, "core-default")

    def test_no_slot_only_when_the_core_default_is_also_gone(self):
        """NEGATIVE CONTROL for the two above: no-slot must still be reachable, or
        'falls back' is indistinguishable from 'never returns no-slot'."""
        runner._world_path = lambda: None
        orig = runner.DEFAULT_SLOT
        try:
            runner.DEFAULT_SLOT = orig.parent / "does-not-exist-inbound-drain-default.sh"
            slot, source = runner._resolve_slot(None)
            self.assertIsNone(slot)
            self.assertEqual(source, "none")
            res = runner.run()
            self.assertEqual(res["status"], "no-slot")
            self.assertIn("missing from this install", res["note"])
        finally:
            runner.DEFAULT_SLOT = orig

    def test_explicit_override_outranks_both(self):
        with tempfile.TemporaryDirectory() as td:
            w = self._world_with_slot(Path(td))
            runner._world_path = lambda: w
            explicit = Path(td) / "explicit.sh"
            slot, source = runner._resolve_slot(explicit)
            self.assertEqual(source, "override")
            self.assertEqual(slot, explicit)


class DestinationFence(unittest.TestCase):
    """The engine's own guard against a misconfigured box stealing instructions."""

    def setUp(self):
        self._orig = engine._own_world_root

    def tearDown(self):
        engine._own_world_root = self._orig

    def test_refuses_when_own_world_is_outside_the_spool_root(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            engine._own_world_root = lambda: Path(a).resolve()
            reason = engine._destination_fence(Path(b))
            self.assertIsNotNone(reason)
            self.assertIn("not under the spool root", reason)

    def test_allows_when_own_world_is_inside_the_spool_root(self):
        """NEGATIVE CONTROL. Without this the fence could be an unconditional
        refusal and every test above would still pass."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            own = root / "envkey" / "workspace" / "world"
            own.mkdir(parents=True)
            engine._own_world_root = lambda: own
            self.assertIsNone(engine._destination_fence(root))

    def test_fails_closed_when_own_world_is_unresolvable(self):
        with tempfile.TemporaryDirectory() as td:
            engine._own_world_root = lambda: None
            reason = engine._destination_fence(Path(td))
            self.assertIsNotNone(reason)
            self.assertIn("cannot resolve", reason)

    def test_directive_is_FAILED_not_REJECTED_when_the_fence_refuses(self):
        """Disposition matters: REJECTED is terminal and would CONSUME a member's
        instruction over what is only a local misconfiguration."""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            engine._own_world_root = lambda: Path(a).resolve()
            disp, detail = engine._apply_directive(
                {"kind": "directive", "text": "please do the thing"},
                "world", "asp-001", spool_root=Path(b))
            self.assertEqual(disp, engine.FAILED)
            self.assertIn("destination fence", detail)


class VerbVocabularyIsDerived(unittest.TestCase):
    def test_known_verbs_come_from_the_registry_not_a_restatement(self):
        from planned_verbs import PLANNED_VERBS
        self.assertEqual(engine.KNOWN_VERBS, tuple(sorted(PLANNED_VERBS)))
        self.assertTrue(engine.KNOWN_VERBS, "registry read must be non-empty here")


class CoreDefaultSlotDeclines(unittest.TestCase):
    """`not_a_vessel`, never a zero count — absence is not zero."""

    def _run(self, env):
        e = dict(os.environ)
        e.pop("INBOUND_ENVIRONMENT_KEY", None)
        e.pop("INBOUND_SPOOL_ROOT", None)
        e.update(env)
        return subprocess.run(
            [BASH, (CORE / "inbound-drain-default.sh").as_posix(), "--json"],
            capture_output=True, text=True, env=e, timeout=60)

    def test_declines_without_an_environment_key(self):
        p = self._run({})
        self.assertEqual(p.returncode, 0, p.stderr)
        payload = json.loads(p.stdout)
        self.assertTrue(payload["not_a_vessel"])
        self.assertIn("INBOUND_ENVIRONMENT_KEY", payload["reason"])

    def test_declines_with_a_key_but_no_spool_root(self):
        p = self._run({"INBOUND_ENVIRONMENT_KEY": "env-a"})
        payload = json.loads(p.stdout)
        self.assertTrue(payload["not_a_vessel"])
        self.assertIn("INBOUND_SPOOL_ROOT", payload["reason"])

    def test_declines_when_this_environment_has_no_spool_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._run({"INBOUND_ENVIRONMENT_KEY": "env-a", "INBOUND_SPOOL_ROOT": td})
            payload = json.loads(p.stdout)
            self.assertTrue(payload["not_a_vessel"])
            self.assertIn("no spool dir", payload["reason"])

    def test_instance_token_is_split_on_the_left_half(self):
        """`<envId>+<instance>` addresses a PER-ENVIRONMENT spool."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "env-a").mkdir()
            p = self._run({"INBOUND_ENVIRONMENT_KEY": "env-a+char7", "INBOUND_SPOOL_ROOT": td})
            self.assertEqual(p.returncode, 0, p.stderr)
            # Past every guard, so it reached the engine rather than declining.
            self.assertNotIn("not_a_vessel", p.stdout)


if __name__ == "__main__":
    unittest.main()
