"""test_health_attribution.py — unit tests for the health-ledger attribution
engine (Phase 2). Spec: core/config/conventions/health-ledger.md §9, §11.

HERMETIC: the canonical ring classifier, the pure scoring functions, the
NUL-delimited git-log parser (fed synthetic stdout — no real repo), and the
attribute() orchestrator (fed injected changed_files) are all exercised without
touching git or the live config. Safe with a live daemon present (guard-672).
"""

from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent


def _load_module():
    spec_path = CORE_SCRIPTS / "health-attribution.py"
    spec = importlib.util.spec_from_file_location("health_attribution", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_module()


# --- _classify_ring (the canonical constitutional-ring classifier) ----------
class TestClassifyRing(unittest.TestCase):
    def c(self, p):
        return _MOD._classify_ring(p)

    def test_ring0_anchor(self):
        self.assertEqual(self.c(".claude/settings.local.json"), "0")
        self.assertEqual(self.c("core/scripts/settings-structural-validator.py"), "0")
        self.assertEqual(self.c("core/scripts/settings-structural-validator.sh"), "0")

    def test_ring1_mission(self):
        self.assertEqual(self.c("world/program.md"), "1")
        self.assertEqual(self.c("agents/alpha/self.md"), "1")
        self.assertEqual(self.c("core/config/conventions/health-ledger.md"), "1")
        self.assertEqual(self.c("core/config/modes/autonomous.md"), "1")
        self.assertEqual(self.c(".claude/rules/verify-before-assuming.md"), "1")

    def test_ring3_protocols(self):
        self.assertEqual(self.c("meta/goal-selection-strategy.yaml"), "3")
        self.assertEqual(self.c("meta/reflection-strategy.yaml"), "3")
        self.assertEqual(self.c("agents/alpha/developmental-stage.yaml"), "3")

    def test_ring15_critical_infra(self):
        self.assertEqual(self.c("core/scripts/goal-selector.py"), "1.5")
        self.assertEqual(self.c("core/scripts/iteration-close.sh"), "1.5")
        self.assertEqual(self.c("mind_api/src/agent_paths.py"), "1.5")

    def test_ring15_skill_pseudocode(self):
        # Skill pseudocode is executable framework behavior — explicit 1.5,
        # never the vague 'unknown' fall-through. Authority stays agent (never-auto).
        self.assertEqual(self.c(".claude/skills/tree/SKILL.md"), "1.5")
        self.assertEqual(self.c(".claude/skills/aspirations-execute/SKILL.md"), "1.5")
        self.assertEqual(_MOD._ring_authority(self.c(".claude/skills/tree/SKILL.md")), "agent")

    def test_ring2_standards(self):
        self.assertEqual(self.c("core/config/aspirations.yaml"), "2")
        self.assertEqual(self.c("core/config/curriculum.yaml"), "2")
        self.assertEqual(self.c("world/guardrails.jsonl"), "2")
        self.assertEqual(self.c("world/reasoning-bank.jsonl"), "2")
        self.assertEqual(self.c("world/conventions/capability-routing.md"), "2")

    def test_unknown(self):
        self.assertEqual(self.c("world/knowledge/tree/system/node.md"), "unknown")
        self.assertEqual(self.c("README.md"), "unknown")

    def test_ring3_beats_ring2_for_meta_yaml(self):
        # meta/*.yaml is Ring 3 (auto), NOT caught by any broad config rule.
        self.assertEqual(self.c("meta/encoding-strategy.yaml"), "3")

    def test_backslash_and_leading_slash_normalized(self):
        self.assertEqual(self.c("core\\config\\conventions\\x.md"), "1")
        self.assertEqual(self.c("/world/program.md"), "1")
        self.assertEqual(self.c("./meta/evolution-strategy.yaml"), "3")


# --- _ring_authority --------------------------------------------------------
class TestRingAuthority(unittest.TestCase):
    def test_ring0_never(self):
        self.assertEqual(_MOD._ring_authority("0"), "never")

    def test_ring1_user(self):
        self.assertEqual(_MOD._ring_authority("1"), "user")

    def test_ring15_and_2_and_unknown_agent(self):
        self.assertEqual(_MOD._ring_authority("1.5"), "agent")
        self.assertEqual(_MOD._ring_authority("2"), "agent")
        self.assertEqual(_MOD._ring_authority("unknown"), "agent")

    def test_ring3_auto_above_gate(self):
        self.assertEqual(_MOD._ring_authority("3", score=0.75, confidence_gate=0.70), "auto")

    def test_ring3_demoted_below_gate(self):
        self.assertEqual(_MOD._ring_authority("3", score=0.50, confidence_gate=0.70), "agent")

    def test_ring3_exactly_at_gate_is_auto(self):
        self.assertEqual(_MOD._ring_authority("3", score=0.70, confidence_gate=0.70), "auto")


# --- pure scoring -----------------------------------------------------------
class TestScoring(unittest.TestCase):
    def test_temporal_proximity_position(self):
        # commit at window end (before) → 1.0; at start (after) → 0.0; mid → 0.5
        self.assertAlmostEqual(_MOD._temporal_proximity(200, 100, 200), 1.0)
        self.assertAlmostEqual(_MOD._temporal_proximity(100, 100, 200), 0.0)
        self.assertAlmostEqual(_MOD._temporal_proximity(150, 100, 200), 0.5)

    def test_temporal_proximity_degenerate_window(self):
        self.assertEqual(_MOD._temporal_proximity(100, 100, 100), 1.0)

    def test_temporal_proximity_clamped(self):
        self.assertEqual(_MOD._temporal_proximity(50, 100, 200), 0.0)   # before window
        self.assertEqual(_MOD._temporal_proximity(300, 100, 200), 1.0)  # after window

    def test_recency_decay_half_life(self):
        hl = _MOD._RECENCY_HALF_LIFE_S
        now = 1_000_000_000
        self.assertAlmostEqual(_MOD._recency_decay(now, now, hl), 1.0)
        self.assertAlmostEqual(_MOD._recency_decay(now - hl, now, hl), 0.5, places=4)
        self.assertAlmostEqual(_MOD._recency_decay(now - 2 * hl, now, hl), 0.25, places=4)

    def test_recency_decay_future_clamped(self):
        self.assertEqual(_MOD._recency_decay(2000, 1000), 1.0)  # commit in the future

    def test_correlation_weight_direct_match(self):
        rules = [{"signal": "deep_ratio", "weight": 1.0,
                  "globs": ["meta/goal-selection-strategy.yaml", "core/scripts/goal-selector.py"]}]
        self.assertEqual(_MOD._correlation_weight("meta/goal-selection-strategy.yaml",
                                                  "deep_ratio", 0.3, rules), 1.0)

    def test_correlation_weight_glob_star(self):
        rules = [{"signal": "encoding_ratio", "weight": 1.0,
                  "globs": [".claude/skills/aspirations-state-update/**"]}]
        self.assertEqual(_MOD._correlation_weight(
            ".claude/skills/aspirations-state-update/SKILL.md", "encoding_ratio", 0.3, rules), 1.0)

    def test_correlation_weight_default_when_no_match(self):
        rules = [{"signal": "deep_ratio", "weight": 1.0, "globs": ["meta/x.yaml"]}]
        self.assertEqual(_MOD._correlation_weight("core/scripts/unrelated.py",
                                                  "deep_ratio", 0.3, rules), 0.3)

    def test_correlation_weight_signal_scoped(self):
        # a glob registered for a DIFFERENT signal must not match → default.
        rules = [{"signal": "tree_writes", "weight": 1.0, "globs": [".claude/skills/tree/**"]}]
        self.assertEqual(_MOD._correlation_weight(".claude/skills/tree/SKILL.md",
                                                  "deep_ratio", 0.3, rules), 0.3)

    def test_correlation_weight_takes_max(self):
        rules = [
            {"signal": "x", "weight": 0.5, "globs": ["a/**"]},
            {"signal": "x", "weight": 1.0, "globs": ["a/b/**"]},
        ]
        self.assertEqual(_MOD._correlation_weight("a/b/c.py", "x", 0.3, rules), 1.0)

    def test_score_candidate_combines_factors(self):
        rules = [{"signal": "deep_ratio", "weight": 1.0, "globs": ["meta/goal-selection-strategy.yaml"]}]
        now = 1_000_000_000
        # commit at window-end, direct correlation, fresh → ~1.0
        s = _MOD.score_candidate("meta/goal-selection-strategy.yaml", now, "deep_ratio",
                                 after_ts=now - 1000, before_ts=now, now_ts=now,
                                 default_weight=0.3, rules=rules)
        self.assertAlmostEqual(s, 1.0, places=3)
        # same file but at window start → temporal_proximity 0 → score 0
        s0 = _MOD.score_candidate("meta/goal-selection-strategy.yaml", now - 1000, "deep_ratio",
                                  after_ts=now - 1000, before_ts=now, now_ts=now,
                                  default_weight=0.3, rules=rules)
        self.assertAlmostEqual(s0, 0.0, places=4)


# --- _parse_git_log (synthetic NUL-delimited stdout) ------------------------
class TestParseGitLog(unittest.TestCase):
    def _stdout(self, commits):
        # commits: list of (hash, ts, revert_value, [files])
        out = []
        for h, ts, rev, files in commits:
            out.append("\x00C\x00" + h + "\x00" + str(ts) + "\x00" + rev + "\n"
                       + "\n".join(files) + "\n")
        return "".join(out)

    def test_basic_parse(self):
        s = self._stdout([
            ("aaaa1111", 8000, "", ["meta/x.yaml", "core/scripts/y.py"]),
        ])
        recs = _MOD._parse_git_log(s)
        by_path = {r["path"]: r for r in recs}
        self.assertEqual(set(by_path), {"meta/x.yaml", "core/scripts/y.py"})
        self.assertEqual(by_path["meta/x.yaml"]["ts"], 8000)
        self.assertEqual(by_path["meta/x.yaml"]["commit"], "aaaa1111")
        self.assertFalse(by_path["meta/x.yaml"]["health_revert"])

    def test_newest_commit_per_file_wins(self):
        s = self._stdout([
            ("old00001", 7000, "", ["meta/x.yaml"]),
            ("new00002", 9000, "", ["meta/x.yaml"]),
        ])
        recs = _MOD._parse_git_log(s)
        rec = next(r for r in recs if r["path"] == "meta/x.yaml")
        self.assertEqual(rec["ts"], 9000)
        self.assertEqual(rec["commit"], "new00002")

    def test_health_revert_trailer_flagged(self):
        s = self._stdout([
            ("rev00001", 9000, "deep_ratio", ["meta/x.yaml"]),
        ])
        rec = _MOD._parse_git_log(s)[0]
        self.assertTrue(rec["health_revert"])

    def test_empty_stdout(self):
        self.assertEqual(_MOD._parse_git_log(""), [])


class TestGitChangedFilesArgvSafety(unittest.TestCase):
    """Regression guard: the git --format argument must contain NO literal NUL
    byte (Python rejects argv with embedded NUL → 'embedded null character').
    Git emits NULs in OUTPUT via the printable %x00 escape. This bug shipped and
    was caught only by manual real-git smoke — hermetic _parse_git_log tests
    bypass argv. Monkeypatches subprocess.run to inspect the argv and feed
    synthetic stdout through the full _git_changed_files → _parse_git_log path."""

    def test_no_nul_in_argv_and_roundtrips(self):
        import subprocess as _sp
        captured = {}

        class _FakeCompleted:
            returncode = 0
            stderr = ""
            stdout = ("\x00C\x00abc12345\x009000\x00\n"
                      "meta/x.yaml\ncore/scripts/y.py\n")

        orig = _MOD.subprocess.run

        def _fake_run(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            return _FakeCompleted()

        _MOD.subprocess.run = _fake_run
        try:
            recs = _MOD._git_changed_files("2026-06-01T00:00:00",
                                           "2026-06-03T00:00:00", ".")
        finally:
            _MOD.subprocess.run = orig

        # 1) every argv element is NUL-free (the actual regression)
        for arg in captured["cmd"]:
            self.assertNotIn("\x00", arg, f"NUL in git argv element: {arg!r}")
        # 2) the format arg uses git's printable %x00 escape, not a literal NUL
        fmt_args = [a for a in captured["cmd"] if a.startswith("--format=")]
        self.assertTrue(fmt_args and "%x00" in fmt_args[0])
        # 3) the synthetic NUL-delimited output round-trips through the parser
        by_path = {r["path"]: r for r in recs}
        self.assertEqual(set(by_path), {"meta/x.yaml", "core/scripts/y.py"})
        self.assertEqual(by_path["meta/x.yaml"]["ts"], 9000)


# --- attribute() orchestrator (injected changed_files) ----------------------
def _write_min_config(config_dir):
    """A minimal aspirations.yaml carrying the health_regression correlation map."""
    (config_dir / "aspirations.yaml").write_text(
        "health_regression:\n"
        "  signal_file_correlation:\n"
        "    default_weight: 0.3\n"
        "    rules:\n"
        "      - signal: deep_ratio\n"
        "        weight: 1.0\n"
        "        globs:\n"
        "          - \"meta/goal-selection-strategy.yaml\"\n"
        "          - \"core/scripts/goal-selector.py\"\n",
        encoding="utf-8")


class TestAttribute(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config_dir = self.tmp / "config"
        self.agent_dir = self.tmp / "agent"
        self.config_dir.mkdir()
        self.agent_dir.mkdir()
        _write_min_config(self.config_dir)
        self.now = 1_000_000_000

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _attr(self, changed_files, signal="deep_ratio", gate=0.70):
        return _MOD.attribute(
            self.now - 1000, self.now, signal,
            config_dir=self.config_dir, repo_root=self.tmp, agent_dir=self.agent_dir,
            now_ts=self.now, confidence_gate=gate, changed_files=changed_files)

    def test_filters_health_revert(self):
        cf = [
            {"path": "meta/goal-selection-strategy.yaml", "ts": self.now, "commit": "r1", "health_revert": True},
            {"path": "core/scripts/goal-selector.py", "ts": self.now, "commit": "c1", "health_revert": False},
        ]
        cands = self._attr(cf)
        paths = [c["path"] for c in cands]
        self.assertNotIn("meta/goal-selection-strategy.yaml", paths)  # revert filtered
        self.assertIn("core/scripts/goal-selector.py", paths)

    def test_skips_ring0(self):
        cf = [{"path": ".claude/settings.local.json", "ts": self.now, "commit": "x", "health_revert": False}]
        self.assertEqual(self._attr(cf), [])

    def test_classifies_and_sets_authority(self):
        cf = [{"path": "meta/goal-selection-strategy.yaml", "ts": self.now, "commit": "m1", "health_revert": False}]
        c = self._attr(cf)[0]
        self.assertEqual(c["ring"], "3")
        # direct correlation (1.0) × proximity 1.0 × recency ~1.0 ≈ 1.0 ≥ gate → auto
        self.assertEqual(c["authority"], "auto")
        self.assertGreaterEqual(c["score"], 0.70)

    def test_ring15_never_auto_even_high_score(self):
        cf = [{"path": "core/scripts/goal-selector.py", "ts": self.now, "commit": "g1", "health_revert": False}]
        c = self._attr(cf)[0]
        self.assertEqual(c["ring"], "1.5")
        self.assertEqual(c["authority"], "agent")  # critical infra never auto

    def test_sorted_by_score_desc(self):
        cf = [
            # direct-correlation, at onset → high score
            {"path": "meta/goal-selection-strategy.yaml", "ts": self.now, "commit": "hi", "health_revert": False},
            # unknown-correlation (0.3), at window start → low score
            {"path": "world/knowledge/tree/x.md", "ts": self.now - 1000, "commit": "lo", "health_revert": False},
        ]
        cands = self._attr(cf)
        self.assertEqual(cands[0]["path"], "meta/goal-selection-strategy.yaml")
        self.assertGreater(cands[0]["score"], cands[-1]["score"])

    def test_dead_end_filtered(self):
        (self.agent_dir / "health-dead-ends.yaml").write_text(
            "dead_ends:\n"
            "  - file: meta/goal-selection-strategy.yaml\n"
            "    signal: deep_ratio\n"
            "    expires_at: '2999-01-01T00:00:00'\n",
            encoding="utf-8")
        cf = [{"path": "meta/goal-selection-strategy.yaml", "ts": self.now, "commit": "m1", "health_revert": False}]
        self.assertEqual(self._attr(cf), [])  # active dead end filtered out

    def test_expired_dead_end_not_filtered(self):
        (self.agent_dir / "health-dead-ends.yaml").write_text(
            "dead_ends:\n"
            "  - file: meta/goal-selection-strategy.yaml\n"
            "    signal: deep_ratio\n"
            "    expires_at: '2000-01-01T00:00:00'\n",  # long expired
            encoding="utf-8")
        cf = [{"path": "meta/goal-selection-strategy.yaml", "ts": self.now, "commit": "m1", "health_revert": False}]
        self.assertEqual(len(self._attr(cf)), 1)  # expired → candidate survives

    def test_dead_end_signal_scoped(self):
        # a dead end for a DIFFERENT signal must not filter this signal's candidate.
        (self.agent_dir / "health-dead-ends.yaml").write_text(
            "dead_ends:\n"
            "  - file: meta/goal-selection-strategy.yaml\n"
            "    signal: encoding_ratio\n"
            "    expires_at: '2999-01-01T00:00:00'\n",
            encoding="utf-8")
        cf = [{"path": "meta/goal-selection-strategy.yaml", "ts": self.now, "commit": "m1", "health_revert": False}]
        self.assertEqual(len(self._attr(cf)), 1)


if __name__ == "__main__":
    unittest.main()
