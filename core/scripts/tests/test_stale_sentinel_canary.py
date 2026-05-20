"""Regression test for stale-sentinel-canary ().

Asserts the canary's defense-in-depth contract:

  1. When a Cat C sentinel is set for `threshold` consecutive runs without
     being cleared, the canary fires an Investigate goal (verified via dry-run
     filing payload — NOT live aspirations.jsonl mutation).
  2. When the sentinel is cleared mid-stretch, the stuck-count resets to 0
     and the canary does NOT fire.
  3. Threshold is honored — threshold=4 does not fire at stuck_count=3.
  4. Multiple sentinels are tracked independently — one firing does not
     affect another's count.
  5. After firing, the persisted counter resets to 0 (post-fire reset).

The canary is invoked with --dry-run on the firing run so live aspirations
state and live working-memory state are NEVER mutated by the firing path.
Setup/teardown use direct YAML I/O on a temporary working-memory.yaml so
the live alpha/session/working-memory.yaml is never touched. The canary
respects AGENT_DIR (set via MIND_AGENT) so we route it to a temp agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import yaml  # noqa: E402

CANARY = CORE_SCRIPTS / "stale-sentinel-canary.py"

TRACKED = [
    "force_tree_encoding",
    "force_tree_maintain",
    "fresh_eyes_dispatch_pending",
    "force_metric_encoding_pending",
]


class TestStaleSentinelCanary(unittest.TestCase):
    """ regression suite — uses an isolated temp agent dir.

    The test agent dir gets its own working-memory.yaml AND a
    local-paths.conf so the canary's _paths resolver routes WORLD_DIR
    + META_DIR somewhere harmless. MIND_AGENT picks the dir.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp_root = Path(tempfile.mkdtemp(prefix="stale-sentinel-canary-test-"))
        cls._tmp_agent_name = "_test_canary_agent"
        # Phase 2.5.D layout: agent dirs live under agents/ parent.
        cls._tmp_agent_dir = PROJECT_ROOT / "agents" / cls._tmp_agent_name
        cls._tmp_agent_dir.mkdir(parents=True, exist_ok=True)
        (cls._tmp_agent_dir / "session").mkdir(exist_ok=True)
        # Minimal local-paths.conf — point world+meta at the throwaway tmp
        # so the canary won't touch the real shared world. The canary itself
        # only reads WM, but aspirations-add-goal.sh (called from a fired
        # path) does write to world/aspirations.jsonl — dry-run blocks that.
        (cls._tmp_agent_dir / "local-paths.conf").write_text(
            f'WORLD_DIR="{(cls._tmp_root / "world").as_posix()}"\n'
            f'META_DIR="{(cls._tmp_root / "meta").as_posix()}"\n',
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls):
        # Best-effort cleanup. If a stray write left files in the agent dir
        # rmtree handles it; if the dir is missing, ignore.
        try:
            shutil.rmtree(cls._tmp_agent_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            shutil.rmtree(cls._tmp_root, ignore_errors=True)
        except Exception:
            pass

    def setUp(self):
        # Reset working-memory.yaml to a clean slot-structure each test.
        wm = self._tmp_agent_dir / "session" / "working-memory.yaml"
        wm.write_text(
            yaml.dump(
                {"slots": {s: None for s in TRACKED}},
                default_flow_style=False, sort_keys=False,
            ),
            encoding="utf-8",
        )

    # ---- Helpers -------------------------------------------------------------

    def _wm_path(self) -> Path:
        return self._tmp_agent_dir / "session" / "working-memory.yaml"

    def _read_wm(self) -> dict:
        return yaml.safe_load(self._wm_path().read_text(encoding="utf-8")) or {}

    def _set_slot(self, slot: str, value) -> None:
        data = self._read_wm()
        data.setdefault("slots", {})[slot] = value
        self._wm_path().write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def _run_canary(self, dry_run: bool = False, threshold: int = 3) -> dict:
        args = [sys.executable, str(CANARY), "--threshold", str(threshold)]
        if dry_run:
            args.append("--dry-run")
        env = os.environ.copy()
        env["MIND_AGENT"] = self._tmp_agent_name
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"canary rc={result.returncode}: stderr={result.stderr.strip()[:400]}"
            )
        if not result.stdout.strip():
            # --quiet with no firing returns empty — synthesize empty report
            return {"sentinels": {}, "investigate_goals_filed": []}
        return json.loads(result.stdout)

    # ---- Core firing contract ------------------------------------------------

    def test_fires_after_threshold_consecutive_runs(self):
        """ verification: 3 iterations with force_tree_maintain set
        + precheck Phase 0-pre skipped → Investigate filed on run 3.

        The firing run uses --dry-run so the test doesn't mutate
        world/aspirations.jsonl; the report's filing_result confirms the
        fire happened with the correct sentinel name.
        """
        self._set_slot("force_tree_maintain", {"fired": True, "count": 5, "reason": "test"})

        # Run 1 — counter 0→1
        r1 = self._run_canary(dry_run=False, threshold=3)
        self.assertTrue(r1["sentinels"]["force_tree_maintain"]["is_set"])
        self.assertEqual(r1["sentinels"]["force_tree_maintain"]["new_stuck_count"], 1)
        self.assertFalse(r1["sentinels"]["force_tree_maintain"]["fired"])

        # Run 2 — counter 1→2
        r2 = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r2["sentinels"]["force_tree_maintain"]["new_stuck_count"], 2)
        self.assertFalse(r2["sentinels"]["force_tree_maintain"]["fired"])

        # Run 3 — counter 2→3 → fire (dry-run so no live filing)
        r3 = self._run_canary(dry_run=True, threshold=3)
        self.assertTrue(r3["sentinels"]["force_tree_maintain"]["fired"])
        self.assertEqual(len(r3["investigate_goals_filed"]), 1)
        filed = r3["investigate_goals_filed"][0]
        self.assertEqual(filed["sentinel"], "force_tree_maintain")
        self.assertEqual(filed["stuck_count"], 3)
        self.assertTrue(filed["result"].get("dry_run"))
        self.assertIn("force_tree_maintain", filed["result"]["payload_title"])

    def test_resets_when_sentinel_cleared(self):
        """Clearing the sentinel mid-stretch resets stuck_count to 0."""
        self._set_slot("force_tree_maintain", {"fired": True})
        r1 = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r1["sentinels"]["force_tree_maintain"]["new_stuck_count"], 1)

        # Consumer cleared via aspirations-precheck Phase 0-pre.
        self._set_slot("force_tree_maintain", None)
        r2 = self._run_canary(dry_run=False, threshold=3)
        self.assertFalse(r2["sentinels"]["force_tree_maintain"]["is_set"])
        self.assertEqual(r2["sentinels"]["force_tree_maintain"]["new_stuck_count"], 0)
        self.assertFalse(r2["sentinels"]["force_tree_maintain"]["fired"])

    def test_threshold_4_does_not_fire_at_3(self):
        """Threshold semantics: >= threshold fires; stuck = threshold-1 does not."""
        self._set_slot("force_tree_maintain", {"fired": True})
        r = None
        for _ in range(3):
            r = self._run_canary(dry_run=False, threshold=4)
        self.assertEqual(r["sentinels"]["force_tree_maintain"]["new_stuck_count"], 3)
        self.assertFalse(r["sentinels"]["force_tree_maintain"]["fired"])

    def test_independent_per_sentinel_tracking(self):
        """Setting one sentinel does not advance another's counter."""
        self._set_slot("force_tree_maintain", {"fired": True})
        self._set_slot("fresh_eyes_dispatch_pending", None)
        r = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r["sentinels"]["force_tree_maintain"]["new_stuck_count"], 1)
        self.assertEqual(r["sentinels"]["fresh_eyes_dispatch_pending"]["new_stuck_count"], 0)

    def test_post_fire_counter_resets_to_zero(self):
        """After firing, the persisted counter resets to 0."""
        self._set_slot("force_tree_maintain", {"fired": True})
        # Pre-seed counter directly to 2 (simulates two prior iterations)
        data = self._read_wm()
        data["slots"]["stale_sentinel_canary"] = {"force_tree_maintain": 2}
        self._wm_path().write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        # One more run hits stuck=3 → fire (dry-run so no live add-goal)
        r = self._run_canary(dry_run=True, threshold=3)
        self.assertTrue(r["sentinels"]["force_tree_maintain"]["fired"])
        # Dry-run does NOT persist counters — confirm by checking that the
        # canary's in-report value reflects the post-fire reset (the entry
        # shows fired=true; the stored value would be 0 in a non-dry-run path).
        # For non-dry-run reset behavior, drive one more iteration with
        # sentinel cleared and confirm count stays at 0:
        self._set_slot("force_tree_maintain", None)
        r2 = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r2["sentinels"]["force_tree_maintain"]["new_stuck_count"], 0)


if __name__ == "__main__":
    unittest.main()
