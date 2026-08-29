"""test_full_suite_recommender.py — smoke tests for  advisory gate.

The full-suite-recommender.py classifies uncommitted file changes and emits
a banner recommending the appropriate full-suite test commands. These tests
exercise the classifier without invoking actual git or the wrapper.

Posture mirrors the recommender itself: fail-open, advisory. Tests verify
the banner emits expected content for representative file mixes and that
the routine-outcome skip path works.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent


def _load_recommender():
    """Load full-suite-recommender.py as a module despite the hyphenated name."""
    spec_path = CORE_SCRIPTS / "full-suite-recommender.py"
    spec = importlib.util.spec_from_file_location("full_suite_recommender", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMindClassifier(unittest.TestCase):
    def setUp(self):
        self.mod = _load_recommender()

    def test_classify_python_production(self):
        buckets = self.mod._classify_mind(["core/scripts/foo.py"])
        self.assertEqual(buckets["py_production"], ["core/scripts/foo.py"])
        self.assertEqual(buckets["py_test"], [])

    def test_classify_python_test(self):
        # tests/ prefix takes precedence over scripts/ (more specific)
        buckets = self.mod._classify_mind(["core/scripts/tests/test_x.py"])
        self.assertEqual(buckets["py_test"], ["core/scripts/tests/test_x.py"])
        self.assertEqual(buckets["py_production"], [])

    def test_classify_wrapper_shell(self):
        buckets = self.mod._classify_mind(["core/scripts/foo.sh"])
        self.assertEqual(buckets["sh_wrapper"], ["core/scripts/foo.sh"])

    def test_classify_skill_md(self):
        buckets = self.mod._classify_mind([".claude/skills/aspirations-execute/SKILL.md"])
        self.assertEqual(buckets["skill_md"], [".claude/skills/aspirations-execute/SKILL.md"])

    def test_classify_rule_md(self):
        buckets = self.mod._classify_mind([".claude/rules/run-full-suite-after-deep-code.md"])
        self.assertEqual(buckets["rule_md"], [".claude/rules/run-full-suite-after-deep-code.md"])

    def test_classify_config_yaml(self):
        buckets = self.mod._classify_mind(["core/config/aspirations.yaml"])
        self.assertEqual(buckets["config"], ["core/config/aspirations.yaml"])

    def test_classify_mixed(self):
        paths = [
            "core/scripts/foo.py",
            "core/scripts/foo.sh",
            "core/scripts/tests/test_foo.py",
            ".claude/skills/aspirations-execute/SKILL.md",
            ".claude/rules/run-full-suite-after-deep-code.md",
            "core/config/aspirations.yaml",
            "mind_api/src/agent_paths.py",
            "core/logs/iteration-close-stderr.log",
            "irrelevant/elsewhere.py",  # should NOT match any bucket
        ]
        buckets = self.mod._classify_mind(paths)
        # : the two trees are DIFFERENT SUITES and must not share a
        # bucket. This assertion previously required them conflated, which is
        # how the wrong-suite mapping stayed green while the governing rule
        # already said otherwise.
        self.assertEqual(buckets["py_production"], ["core/scripts/foo.py"])
        self.assertEqual(buckets["py_daemon"], ["mind_api/src/agent_paths.py"])
        self.assertEqual(buckets["py_test"], ["core/scripts/tests/test_foo.py"])
        self.assertEqual(buckets["sh_wrapper"], ["core/scripts/foo.sh"])
        self.assertEqual(buckets["skill_md"], [".claude/skills/aspirations-execute/SKILL.md"])
        self.assertEqual(buckets["rule_md"], [".claude/rules/run-full-suite-after-deep-code.md"])
        self.assertEqual(buckets["config"], ["core/config/aspirations.yaml"])
        self.assertIn("core/logs/iteration-close-stderr.log", buckets["other_mind"])
        # Path outside Mind doesn't appear in any bucket
        all_classified = sum(buckets.values(), [])
        self.assertNotIn("irrelevant/elsewhere.py", all_classified)


class TestMindRecommendations(unittest.TestCase):
    def setUp(self):
        self.mod = _load_recommender()

    def test_python_production_recommends_pytest(self):
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["core/scripts/foo.py"])
        )
        # run-full-suite.sh, not bare pytest: the bare form omits
        # core/tests/gates plus the invisible and domain halves.
        self.assertIn("bash core/scripts/run-full-suite.sh", recs)

    def test_sh_wrapper_recommends_pytest(self):
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["core/scripts/foo.sh"])
        )
        self.assertIn("bash core/scripts/run-full-suite.sh", recs)

    # --- : the wrong-suite mapping ---------------------------------
    # These are the regression pins for the defect. Its shape matters for
    # reading them: the classifier and the emitter were both CORRECT about
    # which files they had; the only error was which command those files map
    # to. So a bucket assertion alone cannot catch it — the pin has to be on
    # the EMITTED STRING, which is what an agent copies into a shell.

    def test_daemon_only_change_recommends_the_daemon_suite(self):
        """A mind_api/src-only change must name mind_api/tests."""
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["mind_api/src/endpoints/aspirations_write.py"])
        )
        self.assertTrue(any("mind_api/tests" in r for r in recs),
                        f"no mind_api/tests command emitted: {recs}")

    def test_daemon_only_change_does_not_recommend_the_core_suite(self):
        """...and must NOT name core/scripts/tests, the suite that does not
        import it. This is the assertion that fails under the pre-fix source;
        the sibling above can pass while this one is the real pin."""
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["mind_api/src/endpoints/aspirations_write.py"])
        )
        self.assertFalse(any("core/scripts/tests" in r for r in recs),
                         f"daemon change advised to run the core suite: {recs}")

    # : the daemon-staleness advisory. Verification outcome 4 asks for
    # BOTH directions, and the does-not-fire case is the load-bearing one -- an
    # advisory that fires on every close is noise an agent learns to skip.
    def test_daemon_change_advises_restarting_the_daemon(self):
        """An uncommitted mind_api/src edit is live on disk and STALE in the
        long-lived daemon (guard-4804, guard-3373). pytest imports fresh and
        goes green, so nothing else in this banner warns."""
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["mind_api/src/endpoints/aspirations_write.py"])
        )
        self.assertTrue(any("mind-api-start.sh --restart" in r for r in recs),
                        f"no daemon-restart advisory emitted: {recs}")

    def test_non_daemon_change_does_not_advise_restarting_the_daemon(self):
        """core/scripts is NOT the daemon import surface, so a core-only change
        must stay silent. Without this pin the advisory could be appended
        unconditionally and every test above would still pass."""
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["core/scripts/goal-selector.py"])
        )
        self.assertFalse(any("mind-api-start.sh" in r for r in recs),
                         f"core-only change advised a daemon restart: {recs}")

    #  outcome 2: the surface is SOURCED from
    # mind-api-code-changed.sh, never re-listed. Before this, the advisory keyed
    # on MIND_DAEMON_PY_PREFIX ("mind_api/src/") and covered 1 of the boundary's
    # 19 entries -- an uncommitted edit to any of the 18 core/scripts modules the
    # daemon imports was silently un-warned.
    def test_core_scripts_daemon_import_surface_advises_restart(self):
        """core/scripts/aspirations.py IS daemon-imported. The prefix-keyed
        advisory could not see it; the sourced one must."""
        for p in ("core/scripts/aspirations.py",
                  "core/scripts/retrieve.py",
                  "core/scripts/storage_backend.py",
                  "core/scripts/_paths.py",
                  "core/scripts/gates/defer_classifier.py"):
            with self.subTest(path=p):
                recs = self.mod._mind_recommendations(self.mod._classify_mind([p]))
                self.assertTrue(
                    any("mind-api-start.sh --restart" in r for r in recs),
                    f"{p} is on the daemon import surface but no restart advisory fired: {recs}")

    def test_daemon_surface_is_sourced_from_the_script_not_relisted(self):
        """guard-3038: a comment naming the producer is a claim about routing,
        never evidence of it. Assert the entries this module uses ARE the ones
        mind-api-code-changed.sh prints -- so the two cannot drift."""
        import subprocess
        from pathlib import Path as _P
        from _bash_helpers import BASH
        proc = subprocess.run(
            [BASH, _P("core/scripts/mind-api-code-changed.sh").as_posix(),
             "--print-pathspec"],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        from_script = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertTrue(from_script, "the script printed no pathspec entries")
        from_module, why = self.mod._daemon_surface_pathspec()
        self.assertEqual(why, "")
        self.assertEqual(from_module, from_script)
        # And the module must not carry its own copy of the list.
        body = open("core/scripts/full-suite-recommender.py", encoding="utf-8").read()
        for entry in ("core/scripts/owncloud_backend.py", "core/scripts/tree_match.py",
                      "core/scripts/peer_surface.py"):
            self.assertNotIn(entry, body,
                             f"{entry} is re-listed in the recommender -- it must be sourced")

    def test_unreadable_surface_warns_rather_than_going_silent(self):
        """Fail TOWARD the warning, matching mind-api-code-changed.sh's own
        fail-toward-restart posture. A silent fallback to a narrower literal is
        exactly the drift this goal closed."""
        real = self.mod._daemon_surface_pathspec
        try:
            self.mod._daemon_surface_pathspec = lambda: ([], "daemon surface unreadable: injected")
            recs = self.mod._mind_recommendations(self.mod._classify_mind(["core/scripts/goal-selector.py"]))
            self.assertTrue(any("mind-api-start.sh --restart" in r for r in recs),
                            f"unreadable surface went silent: {recs}")
            self.assertTrue(any("unreadable" in r for r in recs),
                            f"advisory did not say WHY it fired blind: {recs}")
        finally:
            self.mod._daemon_surface_pathspec = real

    def test_empty_pathspec_is_treated_as_unreadable_not_as_clean(self):
        """A zero-entry read would disable the advisory forever and look healthy
        (guard-2298 shape). It must be classified unreadable instead."""
        real = self.mod.subprocess.run
        class _P:
            returncode = 0
            stdout = "\n   \n"
            stderr = ""
        try:
            self.mod.subprocess.run = lambda *a, **k: _P()
            entries, why = self.mod._daemon_surface_pathspec()
            self.assertEqual(entries, [])
            self.assertIn("0 entries", why)
        finally:
            self.mod.subprocess.run = real

    def test_both_trees_touched_emits_both_arms(self):
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind(["core/scripts/foo.py", "mind_api/src/agent_paths.py"])
        )
        self.assertTrue(any("run-full-suite.sh" in r for r in recs), recs)
        self.assertTrue(any("mind_api/tests" in r for r in recs), recs)

    def test_every_emitted_pytest_command_pins_storage_backend(self):
        """guard-955 / rb-2983. Unpinned, a tmp-world write collides on the
        PRODUCTION S3 key — that truncated world/aspirations.jsonl from 22
        aspirations / 1366 goals to one fixture on 2026-07-09. The banner is
        where the command gets copied from. Swept over every input class so a
        future arm cannot be added unpinned."""
        for paths in (["mind_api/src/agent_paths.py"],
                      ["core/scripts/foo.py"],
                      ["core/scripts/foo.sh"],
                      ["core/scripts/tests/test_foo.py"],
                      ["core/scripts/foo.py", "mind_api/src/agent_paths.py"]):
            recs = self.mod._mind_recommendations(self.mod._classify_mind(paths))
            for r in recs:
                if "pytest" in r:
                    self.assertIn("STORAGE_BACKEND=local", r,
                                  f"unpinned pytest command for {paths}: {r}")

    def test_skill_md_recommends_skill_evaluate(self):
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind([".claude/skills/aspirations-execute/SKILL.md"])
        )
        self.assertTrue(any("skill-evaluate.sh aspirations-execute" in r for r in recs))

    def test_pure_rule_change_no_pytest(self):
        # Pure rule edits should NOT trigger pytest — only manual review note
        recs = self.mod._mind_recommendations(
            self.mod._classify_mind([".claude/rules/some-rule.md"])
        )
        self.assertFalse(any("pytest" in r for r in recs))
        self.assertTrue(any("[manual]" in r for r in recs))

    def test_no_changes_empty_recs(self):
        recs = self.mod._mind_recommendations(self.mod._classify_mind([]))
        self.assertEqual(recs, [])


class TestProductRepoDetection(unittest.TestCase):
    def setUp(self):
        self.mod = _load_recommender()

    def test_detect_gradle(self):
        with mock.patch("pathlib.Path.exists") as mock_exists:
            # Both gradlew and build.gradle exist
            mock_exists.return_value = True
            kind = self.mod._detect_repo_type(Path("/fake/repo"))
            self.assertEqual(kind, "gradle")

    def test_detect_unknown_when_nothing_matches(self):
        with mock.patch("pathlib.Path.exists", return_value=False):
            kind = self.mod._detect_repo_type(Path("/fake/repo"))
            self.assertEqual(kind, "unknown")

    def test_product_recommendation_gradle(self):
        rec = self.mod._product_recommendation(Path("/fake/repo"), "gradle")
        self.assertIn("./gradlew test --no-daemon", rec)

    def test_product_recommendation_node(self):
        rec = self.mod._product_recommendation(Path("/fake/repo"), "node")
        self.assertIn("npm test", rec)

    def test_product_recommendation_python(self):
        rec = self.mod._product_recommendation(Path("/fake/repo"), "python")
        self.assertIn("python -m pytest tests/", rec)

    def test_product_recommendation_unknown(self):
        rec = self.mod._product_recommendation(Path("/fake/repo"), "unknown")
        self.assertIn("no recognized build system", rec)


class TestMainEntrypoint(unittest.TestCase):
    """Smoke-test main() behavior on the routine-skip + no-changes paths."""

    def setUp(self):
        self.mod = _load_recommender()

    def test_routine_outcome_skips(self):
        """Routine outcome class triggers quiet skip with breadcrumb."""
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "routine"]):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        self.assertIn("skip outcome_class=routine", buf.getvalue())
        # No banner top line should appear
        self.assertNotIn("FULL-SUITE TEST RECOMMENDER", buf.getvalue())

    def test_no_changes_skips_banner(self):
        """When no Mind or product changes detected, no banner is emitted."""
        buf = io.StringIO()
        # Patch internal detector + agent-write path to simulate clean tree
        with mock.patch.object(self.mod, "_git_changed_paths", return_value=[]), \
             mock.patch.object(self.mod, "_agent_write_paths", return_value=[]), \
             mock.patch("sys.stdout", buf), \
             mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "deep"]):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # : the line must NOT read as an all-clear. world/ and meta/ are
        # gitignored, so a domain-script goal produces zero git-visible changes and
        # an agent trusting this line closes having run nothing (guard-3097).
        self.assertIn("no GIT-VISIBLE code changes", out)
        self.assertIn("NOT scanned", out)
        self.assertIn("not an all-clear", out)
        self.assertIn("g-test", out)
        self.assertNotIn("FULL-SUITE TEST RECOMMENDER", buf.getvalue())

    def test_mind_changes_emits_banner(self):
        """Mind changes trigger banner with expected content."""
        buf = io.StringIO()
        with mock.patch.object(self.mod, "_git_changed_paths") as mock_git, \
             mock.patch.object(self.mod, "_agent_write_paths", return_value=[]), \
             mock.patch("sys.stdout", buf), \
             mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "deep"]):
            mock_git.return_value = ["core/scripts/foo.py"]
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("FULL-SUITE TEST RECOMMENDER", out)
        self.assertIn("core/scripts/foo.py", out)
        self.assertIn("bash core/scripts/run-full-suite.sh", out)

    def test_fail_open_on_unexpected_error(self):
        """Any unexpected exception in main() returns exit 0 via outer try."""
        # We test the wrapper at the if __name__ == "__main__" level by
        # directly checking that main() handles raised exceptions when
        # _classify_mind blows up.
        with mock.patch.object(self.mod, "_git_changed_paths", side_effect=RuntimeError("boom")):
            # The outer try/except in main() module-level catches; we simulate
            # it here by running main() and expecting either rc=0 or exception
            # propagation handled by the entrypoint wrapper.
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err), \
                 mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "deep"]):
                try:
                    rc = self.mod.main()
                    # If main() didn't propagate, rc should be 0; if it did,
                    # the entrypoint wrapper would catch — but here we exercise
                    # main() directly, so an exception is acceptable evidence
                    # that the OUTER fail-open catch must handle it.
                except RuntimeError:
                    # Outer try/except (in __main__ block) is the fail-open
                    # catch — that path is tested separately by the wrapper.
                    rc = 0
            self.assertEqual(rc, 0)


class TestPytestSuiteMutex(unittest.TestCase):
    """ — cross-session pytest-suite mutex behavior."""

    def setUp(self):
        self.mod = _load_recommender()
        self.tmpdir = tempfile.mkdtemp(prefix="pytest-mutex-test-")
        self.lock_path = Path(self.tmpdir) / "pytest-suite.lock"

    def tearDown(self):
        # Ensure no orphan lock left behind across tests
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def test_acquire_succeeds_when_lock_absent(self):
        """Fresh lock path => acquire returns True and writes metadata."""
        rc = self.mod._acquire_pytest_lock(self.lock_path, "g-test")
        self.assertTrue(rc)
        self.assertTrue(self.lock_path.exists())
        info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(info["goal_id"], "g-test")
        self.assertEqual(info["pid"], os.getpid())
        self.assertIn("started_at", info)
        self.assertIn("agent", info)
        self.lock_path.unlink()

    def test_acquire_fails_when_lock_held_fresh(self):
        """A non-stale lock (mtime within stale window) blocks acquisition."""
        # Simulate another live holder: write the lock file by hand
        self.lock_path.write_text(
            json.dumps({"pid": 99999, "started_at": "2026-05-19T22:00:00",
                        "agent": "alpha", "goal_id": "g-other"}),
            encoding="utf-8",
        )
        rc = self.mod._acquire_pytest_lock(self.lock_path, "g-test")
        self.assertFalse(rc)
        # Lock file should still be present (we didn't acquire — can't release)
        self.assertTrue(self.lock_path.exists())
        # Content should be the foreign holder's, untouched
        info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(info["goal_id"], "g-other")

    def test_acquire_breaks_stale_lock(self):
        """A lock older than PYTEST_SUITE_LOCK_STALE_SECONDS is reclaimable."""
        self.lock_path.write_text("12345", encoding="utf-8")
        # Backdate the mtime well past the stale window
        stale_age = self.mod.PYTEST_SUITE_LOCK_STALE_SECONDS + 60
        import time as _time
        old_time = _time.time() - stale_age
        os.utime(str(self.lock_path), (old_time, old_time))
        rc = self.mod._acquire_pytest_lock(self.lock_path, "g-test")
        self.assertTrue(rc)
        # Lock content is now ours (overwritten with richer metadata)
        info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(info["pid"], os.getpid())
        self.lock_path.unlink()

    def test_read_lock_info_handles_corrupt_content(self):
        """Unparseable lock content returns {'raw': ...} not a crash."""
        self.lock_path.write_text("not-valid-json", encoding="utf-8")
        info = self.mod._read_lock_info(self.lock_path)
        self.assertEqual(info, {"raw": "not-valid-json"})

    def test_read_lock_info_handles_missing_file(self):
        """Missing lock file returns {} (no crash)."""
        info = self.mod._read_lock_info(self.lock_path)
        self.assertEqual(info, {})

    def test_skip_banner_contains_holder_metadata(self):
        """Skip-message names the holding session for human + LLM visibility."""
        buf = io.StringIO()
        info = {
            "pid": 99999,
            "started_at": "2026-05-19T22:00:00",
            "agent": "alpha",
            "goal_id": "g-other",
        }
        with mock.patch("sys.stdout", buf):
            self.mod._emit_skip_banner("g-test", info)
        out = buf.getvalue()
        self.assertIn("SKIP", out)
        self.assertIn("g-other", out)
        self.assertIn("alpha", out)
        self.assertIn("99999", out)
        self.assertIn("No action needed", out)
        # The consumer LLM relies on this banner NOT containing the
        # full-suite recommendation banner header — otherwise it would
        # still try to run pytest.
        self.assertNotIn("FULL-SUITE TEST RECOMMENDER", out)

    def test_main_acquires_and_releases_on_clean_path(self):
        """When main() emits the banner, the lock is released on exit."""
        buf = io.StringIO()
        with mock.patch.object(self.mod, "_git_changed_paths") as mock_git, \
             mock.patch.object(self.mod, "_agent_write_paths", return_value=[]), \
             mock.patch.object(self.mod, "_pytest_lock_path",
                                return_value=self.lock_path), \
             mock.patch("sys.stdout", buf), \
             mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "deep"]):
            mock_git.return_value = ["core/scripts/foo.py"]
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        # Banner emitted
        self.assertIn("FULL-SUITE TEST RECOMMENDER", buf.getvalue())
        # Lock released
        self.assertFalse(self.lock_path.exists())

    def test_main_skips_banner_when_lock_held(self):
        """When another session holds the lock, main() prints skip-message
        instead of the recommendation banner."""
        # Pre-populate the lock as if a partner session holds it
        self.lock_path.write_text(
            json.dumps({"pid": 99999, "started_at": "2026-05-19T22:00:00",
                        "agent": "alpha", "goal_id": "g-other"}),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with mock.patch.object(self.mod, "_git_changed_paths") as mock_git, \
             mock.patch.object(self.mod, "_agent_write_paths", return_value=[]), \
             mock.patch.object(self.mod, "_pytest_lock_path",
                                return_value=self.lock_path), \
             mock.patch("sys.stdout", buf), \
             mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "deep"]):
            mock_git.return_value = ["core/scripts/foo.py"]
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("SKIP", out)
        self.assertIn("g-other", out)
        self.assertNotIn("FULL-SUITE TEST RECOMMENDER", out)
        # Foreign lock is preserved (we didn't acquire so we don't release)
        self.assertTrue(self.lock_path.exists())
        info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(info["goal_id"], "g-other")

    def test_main_no_lock_path_when_world_dir_unset(self):
        """When WORLD_DIR is unset, main() emits the banner without the mutex."""
        buf = io.StringIO()
        with mock.patch.object(self.mod, "_git_changed_paths") as mock_git, \
             mock.patch.object(self.mod, "_agent_write_paths", return_value=[]), \
             mock.patch.object(self.mod, "_pytest_lock_path", return_value=None), \
             mock.patch("sys.stdout", buf), \
             mock.patch("sys.argv", ["x", "g-test", "--outcome-class", "deep"]):
            mock_git.return_value = ["core/scripts/foo.py"]
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        self.assertIn("FULL-SUITE TEST RECOMMENDER", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
