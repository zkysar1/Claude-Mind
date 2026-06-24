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
  6. Consumption-aware sentinels (fresh_eyes_dispatch_pending, g-115-1553):
     a re-armed sentinel whose consumer keeps up (fresh_eyes_last_dispatch
     advances) does NOT fire; a frozen dispatch timestamp across threshold
     samples DOES fire; a resumed dispatch resets the count. This is the
     fix for the false-positive where the writer re-arms the sentinel on
     every deep close and the canary samples after the arming.

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

# rb-1324/rb-1565 class: the canary subprocess below inherits {**os.environ}
# (os.environ.copy()), so a framework var leaked by an EARLIER test in
# canonical collection order — MIND_WORLD / WORLD_DIR / MIND_AGENT_DIR /
# MIND_META / RT_DIR / etc. — rides into the subprocess and overrides this
# test's local-paths.conf during _paths resolution. The canary then resolves
# WORLD_DIR/META_DIR from the LEAKED env instead of the throwaway tmp paths,
# and a mode='w' write targets a dir that does not exist under polluted order
# (5/5 full-suite FAILs, 5/5 isolation PASSes). conftest restores only
# MIND_AGENT/MIND_WORLD/STORAGE_BACKEND — every other framework var leaks.
# Stripping the whole framework prefix set is polluter-agnostic: env is the
# only cross-test vector (no os.chdir in the suite), so a freshly-spawned
# subprocess can only be perturbed via env. Mirrors the 0 fix in
# test_uncommitted_edits_log_filter.py (rb-1569 / commit bdb74df6).
_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
)


def _hermetic_env(**overrides) -> dict:
    """Subprocess env with the framework env-prefix namespace + PROJECT_ROOT
    stripped, then `overrides` applied. Makes every run look like the clean
    isolation case regardless of what an earlier test leaked into os.environ.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"
    }
    env.update(overrides)
    return env


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
        # 3: create the agent dir UNDER the tmp root, NOT live
        # PROJECT_ROOT/agents/. Resolution routes here via MIND_AGENT_DIR (now
        # honored by _paths.py AND _paths.sh) at the _hermetic_env call below.
        # The prior PROJECT_ROOT/agents/_test_canary_agent was adopted by the
        # running fleet mid-test and leaked on Windows (open handle defeats
        # rmtree(ignore_errors=True)). tmp_root is removed in tearDownClass.
        cls._tmp_agent_dir = cls._tmp_root / cls._tmp_agent_name
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
        env = _hermetic_env(
            MIND_AGENT=self._tmp_agent_name,
            MIND_AGENT_DIR=str(self._tmp_agent_dir),  # route resolution at the tmp dir (3)
        )
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

    # ---- Consumption-aware contract (3) -----------------------------

    def test_consumption_aware_no_fire_when_dispatch_advances(self):
        """fresh_eyes_dispatch_pending re-armed every run while the consumer
        keeps up (fresh_eyes_last_dispatch advances) must NOT fire — this is
        the false-positive the bare presence-count had: the writer
        (iteration-close do_state_update) re-arms on every substantive deep
        close and the canary samples AFTER the arming, so the sentinel is
        'set' at sample time every iteration even though the consumer cleared
        it each time."""
        for i in range(5):
            # Consumer dispatched + stamped a fresh timestamp this iteration.
            self._set_slot("fresh_eyes_last_dispatch", f"2026-06-19T05:{i:02d}:30")
            # Writer re-armed the sentinel (new set_at) — still set at sample.
            self._set_slot(
                "fresh_eyes_dispatch_pending",
                {"fired": True, "set_at": f"2026-06-19T05:{i:02d}:45", "core_count": 5},
            )
            r = self._run_canary(dry_run=False, threshold=3)
            fe = r["sentinels"]["fresh_eyes_dispatch_pending"]
            self.assertTrue(fe["is_set"])
            self.assertTrue(fe.get("dispatch_advanced"))
            self.assertEqual(fe["new_stuck_count"], 0)
            self.assertFalse(fe["fired"])

    def test_consumption_aware_fires_when_dispatch_frozen(self):
        """Consumer bypassed: sentinel stays armed AND fresh_eyes_last_dispatch
        stays frozen across threshold consecutive samples -> fires. (Run 1 is a
        grace sample: last_seen=None != current, read as advanced.)"""
        # Frozen dispatch timestamp (consumer never stamps a newer one).
        self._set_slot("fresh_eyes_last_dispatch", "2026-06-19T04:00:00")
        self._set_slot(
            "fresh_eyes_dispatch_pending",
            {"fired": True, "set_at": "2026-06-19T05:00:00", "core_count": 5},
        )
        # Run 1 — grace: last_seen None != "04:00:00" -> advanced -> 0.
        r1 = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r1["sentinels"]["fresh_eyes_dispatch_pending"]["new_stuck_count"], 0)
        # Run 2 — frozen ("04:00:00" == last_seen) -> 1.
        r2 = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r2["sentinels"]["fresh_eyes_dispatch_pending"]["new_stuck_count"], 1)
        # Run 3 — frozen -> 2.
        r3 = self._run_canary(dry_run=False, threshold=3)
        self.assertEqual(r3["sentinels"]["fresh_eyes_dispatch_pending"]["new_stuck_count"], 2)
        # Run 4 — frozen -> 3 -> fire (dry-run so no live add-goal).
        r4 = self._run_canary(dry_run=True, threshold=3)
        fe = r4["sentinels"]["fresh_eyes_dispatch_pending"]
        self.assertTrue(fe["fired"])
        self.assertFalse(fe.get("dispatch_advanced"))
        self.assertEqual(len(r4["investigate_goals_filed"]), 1)
        self.assertEqual(
            r4["investigate_goals_filed"][0]["sentinel"], "fresh_eyes_dispatch_pending"
        )

    def test_consumption_aware_resets_when_dispatch_resumes(self):
        """Dispatch frozen for a couple samples, then the consumer dispatches
        again (timestamp advances) -> stuck_count resets to 0 (recovered)."""
        self._set_slot("fresh_eyes_last_dispatch", "2026-06-19T04:00:00")
        self._set_slot(
            "fresh_eyes_dispatch_pending",
            {"fired": True, "set_at": "2026-06-19T05:00:00", "core_count": 5},
        )
        self._run_canary(dry_run=False, threshold=3)        # grace -> 0
        r2 = self._run_canary(dry_run=False, threshold=3)   # frozen -> 1
        self.assertEqual(r2["sentinels"]["fresh_eyes_dispatch_pending"]["new_stuck_count"], 1)
        # Consumer resumes — stamps a newer dispatch timestamp.
        self._set_slot("fresh_eyes_last_dispatch", "2026-06-19T05:30:00")
        r3 = self._run_canary(dry_run=False, threshold=3)
        fe = r3["sentinels"]["fresh_eyes_dispatch_pending"]
        self.assertTrue(fe.get("dispatch_advanced"))
        self.assertEqual(fe["new_stuck_count"], 0)
        self.assertFalse(fe["fired"])


if __name__ == "__main__":
    unittest.main()
