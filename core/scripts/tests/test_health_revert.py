"""test_health_revert.py — unit tests for the Phase 3 tiered revert executor.
Spec: core/config/conventions/health-ledger.md §10, §11.

Pure logic (routing, trackers, verify decisions) is hermetic. The git
file-granular revert mechanics are exercised against a REAL temp git repo
(git-only — no daemon, no network; guard-672 safe). Git-using classes skip
gracefully if `git init` is unavailable.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "health_revert", CORE_SCRIPTS / "health-revert.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()


# --- route_candidate (pure decision matrix) ---------------------------------
class TestRouteCandidate(unittest.TestCase):
    def r(self, ring, authority, mode="full", calibrated=True):
        return _MOD.route_candidate({"ring": ring, "authority": authority},
                                    mode=mode, calibrated=calibrated)

    def test_not_eligible_when_not_full(self):
        self.assertEqual(self.r("3", "auto", mode="detect-and-report"), "not-eligible")

    def test_not_eligible_when_not_calibrated(self):
        self.assertEqual(self.r("3", "auto", calibrated=False), "not-eligible")

    def test_ring0_skipped(self):
        self.assertEqual(self.r("0", "never"), "skip-ring0")

    def test_ring1_user(self):
        self.assertEqual(self.r("1", "user"), "user-unblock")

    def test_ring3_auto(self):
        self.assertEqual(self.r("3", "auto"), "auto-revert")

    def test_ring3_demoted_to_agent(self):
        # Ring 3 below the confidence gate → authority 'agent' → agent-unblock
        self.assertEqual(self.r("3", "agent"), "agent-unblock")

    def test_ring15_and_2_agent(self):
        self.assertEqual(self.r("1.5", "agent"), "agent-unblock")
        self.assertEqual(self.r("2", "agent"), "agent-unblock")

    def test_unknown_agent(self):
        self.assertEqual(self.r("unknown", "agent"), "agent-unblock")


# --- trackers (pure YAML round-trip) ----------------------------------------
class TestTrackers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pending_append_load_remove(self):
        self.assertEqual(_MOD.load_pending(self.tmp), [])
        _MOD.append_pending(self.tmp, {"key": "k1", "file": "a.py"})
        _MOD.append_pending(self.tmp, {"key": "k2", "file": "b.py"})
        self.assertEqual(len(_MOD.load_pending(self.tmp)), 2)
        _MOD.remove_pending(self.tmp, "k1")
        keys = [e["key"] for e in _MOD.load_pending(self.tmp)]
        self.assertEqual(keys, ["k2"])

    def test_dead_end_append_and_expiry(self):
        _MOD.append_dead_end(self.tmp, "meta/x.yaml", "deep_ratio",
                             now_iso="2026-06-03T10:00:00", expiry_days=30)
        import yaml
        data = yaml.safe_load((self.tmp / "health-dead-ends.yaml").read_text(encoding="utf-8"))
        e = data["dead_ends"][0]
        self.assertEqual(e["file"], "meta/x.yaml")
        self.assertEqual(e["signal"], "deep_ratio")
        self.assertEqual(e["expires_at"], "2026-07-03T10:00:00")  # +30 days


# --- verify_pending decision logic (dry-run, no git) ------------------------
class TestVerifyPendingDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _kw(self, **over):
        kw = dict(current_composite=0.8, current_iteration=20, repo_root=self.tmp,
                  improve_delta=0.05, verification_iterations=5, expiry_days=30,
                  now_iso="2026-06-03T10:00:00", dry_run=True)
        kw.update(over)
        return kw

    def test_window_not_elapsed_is_pending(self):
        _MOD.append_pending(self.tmp, {"key": "k", "file": "f", "signal": "s",
                                       "revert_iteration": 18, "composite_at_revert": 0.3})
        out = _MOD.verify_pending(self.tmp, current_iteration=20, **{k: v for k, v in self._kw().items() if k not in ("current_iteration",)})
        self.assertEqual(out[0]["decision"], "pending")

    def test_improved_keeps(self):
        _MOD.append_pending(self.tmp, {"key": "k", "file": "f", "signal": "s",
                                       "revert_iteration": 10, "composite_at_revert": 0.30})
        out = _MOD.verify_pending(self.tmp, **self._kw(current_composite=0.40))
        self.assertEqual(out[0]["decision"], "keep")  # 0.40-0.30=0.10 >= 0.05

    def test_not_improved_undo(self):
        _MOD.append_pending(self.tmp, {"key": "k", "file": "f", "signal": "s",
                                       "revert_iteration": 10, "composite_at_revert": 0.30})
        out = _MOD.verify_pending(self.tmp, **self._kw(current_composite=0.31))
        self.assertEqual(out[0]["decision"], "undo")   # 0.31-0.30=0.01 < 0.05

    def test_missing_revert_iteration_stays_pending(self):
        # A pending entry WITHOUT revert_iteration must NEVER trigger a premature
        # undo — the window cannot be evaluated, so treat as not-elapsed. (Guards
        # the producer bug where the verdict omitted `iteration`.)
        _MOD.append_pending(self.tmp, {"key": "k", "file": "f", "signal": "s",
                                       "composite_at_revert": 0.30})  # no revert_iteration
        out = _MOD.verify_pending(self.tmp, **self._kw(current_composite=0.31))
        self.assertEqual(out[0]["decision"], "pending")


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def _init_repo(root):
    def g(*a):
        subprocess.run(["git", "-C", str(root), *a], capture_output=True,
                       text=True, check=True, timeout=30)
    g("init", "-q")
    g("config", "user.email", "test@example.com")
    g("config", "user.name", "test")
    g("config", "commit.gpgsign", "false")
    return g


def _commit(g, root, path, content, msg):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content, encoding="utf-8")
    g("add", "--", path)
    g("commit", "--no-verify", "-q", "-m", msg)
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


@unittest.skipUnless(_git_available(), "git not available")
class TestRevertFileGit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.g = _init_repo(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log_body(self):
        r = subprocess.run(["git", "-C", str(self.tmp), "log", "-1", "--format=%B"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout

    def test_revert_restores_previous_content(self):
        _commit(self.g, self.tmp, "foo.txt", "v1-good\n", "c1")
        bad = _commit(self.g, self.tmp, "foo.txt", "v2-bad\n", "c2")
        res = _MOD.revert_file("foo.txt", bad, self.tmp, "deep_ratio",
                               now_iso="2026-06-03T10:00:00")
        self.assertTrue(res["ok"], res.get("msg"))
        self.assertEqual((self.tmp / "foo.txt").read_text(encoding="utf-8"), "v1-good\n")
        body = self._log_body()
        self.assertIn(f"Health-Revert: deep_ratio {bad}", body)

    def test_revert_of_file_created_in_bad_commit_deletes(self):
        _commit(self.g, self.tmp, "a.txt", "a\n", "init")
        bad = _commit(self.g, self.tmp, "new.txt", "created here\n", "add new.txt")
        res = _MOD.revert_file("new.txt", bad, self.tmp, "encoding_ratio",
                               now_iso="2026-06-03T10:00:00")
        self.assertTrue(res["ok"], res.get("msg"))
        self.assertFalse((self.tmp / "new.txt").exists())  # reverted to absence

    def test_undo_reapplies_bad_content(self):
        _commit(self.g, self.tmp, "foo.txt", "v1\n", "c1")
        bad = _commit(self.g, self.tmp, "foo.txt", "v2\n", "c2")
        _MOD.revert_file("foo.txt", bad, self.tmp, "s", now_iso="2026-06-03T10:00:00")
        self.assertEqual((self.tmp / "foo.txt").read_text(encoding="utf-8"), "v1\n")
        res = _MOD.revert_file("foo.txt", bad, self.tmp, "s",
                               now_iso="2026-06-03T10:00:00", undo=True)
        self.assertTrue(res["ok"], res.get("msg"))
        self.assertEqual((self.tmp / "foo.txt").read_text(encoding="utf-8"), "v2\n")

    def test_dry_run_no_change(self):
        _commit(self.g, self.tmp, "foo.txt", "v1\n", "c1")
        bad = _commit(self.g, self.tmp, "foo.txt", "v2\n", "c2")
        res = _MOD.revert_file("foo.txt", bad, self.tmp, "s", dry_run=True)
        self.assertTrue(res["ok"])
        self.assertIn("dryrun", res["action"])
        self.assertEqual((self.tmp / "foo.txt").read_text(encoding="utf-8"), "v2\n")  # unchanged

    def test_commit_contains_only_path_not_concurrent_staged(self):
        # CRITICAL regression: the Health-Revert commit must contain ONLY the
        # reverted file, even when the concurrent autonomous loop has other files
        # staged in the shared index. A bare `git commit` would sweep them in.
        _commit(self.g, self.tmp, "foo.txt", "v1\n", "c1")
        bad = _commit(self.g, self.tmp, "foo.txt", "v2\n", "c2")
        # Simulate the loop staging an unrelated file concurrently:
        (self.tmp / "other.txt").write_text("loop-staged\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.tmp), "add", "other.txt"],
                       capture_output=True, timeout=30)
        res = _MOD.revert_file("foo.txt", bad, self.tmp, "deep_ratio",
                               now_iso="2026-06-03T10:00:00")
        self.assertTrue(res["ok"], res.get("msg"))
        r = subprocess.run(["git", "-C", str(self.tmp), "show", "--name-only",
                            "--format=", "HEAD"], capture_output=True, text=True, timeout=30)
        changed = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(changed, ["foo.txt"], f"revert commit swept extra files: {changed}")
        # other.txt must remain staged (the loop still owns it; not committed by us)
        r2 = subprocess.run(["git", "-C", str(self.tmp), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, timeout=30)
        self.assertIn("other.txt", r2.stdout)

    def test_root_commit_revert_aborts_no_delete(self):
        # bad_commit == repo root → root~1 is invalid. Must ABORT, not delete.
        root = _commit(self.g, self.tmp, "only.txt", "content\n", "root")
        res = _MOD.revert_file("only.txt", root, self.tmp, "s",
                               now_iso="2026-06-03T10:00:00")
        self.assertFalse(res["ok"])
        self.assertIn("does not resolve", res["msg"])
        self.assertTrue((self.tmp / "only.txt").exists())  # NOT wrongly deleted

    def test_commit_failure_restores_working_tree(self):
        # If the commit fails (e.g. index.lock contention), the file must be
        # restored to HEAD so no untrailered revert residue is left for the loop.
        _commit(self.g, self.tmp, "foo.txt", "v1\n", "c1")
        bad = _commit(self.g, self.tmp, "foo.txt", "v2\n", "c2")
        orig = _MOD._git

        def fake_git(args, repo_root, **kw):
            if args and args[0] == "commit":
                class _R:
                    returncode = 1
                    stdout = ""
                    stderr = "fatal: Unable to create index.lock (simulated)"
                return _R()
            return orig(args, repo_root, **kw)

        _MOD._git = fake_git
        try:
            res = _MOD.revert_file("foo.txt", bad, self.tmp, "s", now_iso="2026-06-03T10:00:00")
        finally:
            _MOD._git = orig
        self.assertFalse(res["ok"])
        self.assertIn("residue restored", res["msg"])
        # working tree restored to HEAD (v2); no leftover reverted content staged
        self.assertEqual((self.tmp / "foo.txt").read_text(encoding="utf-8"), "v2\n")
        r = subprocess.run(["git", "-C", str(self.tmp), "diff", "--cached", "--name-only"],
                           capture_output=True, text=True, timeout=30)
        self.assertNotIn("foo.txt", r.stdout)


@unittest.skipUnless(_git_available(), "git not available")
class TestVerdictToActionGit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.agent = self.tmp / "agent"
        self.repo.mkdir()
        self.agent.mkdir()
        self.g = _init_repo(self.repo)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verdict(self, ring, authority):
        bad = _commit(self.g, self.repo, "meta/s.yaml", "v1\n", "c1")
        bad = _commit(self.g, self.repo, "meta/s.yaml", "v2\n", "c2")
        return {"mode": "full", "signal": "deep_ratio", "iteration": 60,
                "composite": 0.30, "dedup_key": "health-regression:deep_ratio:2026-06-03",
                "candidates": [{"path": "meta/s.yaml", "ring": ring,
                                "authority": authority, "commit": bad, "score": 0.85}]}

    def test_auto_revert_executes_and_tracks_pending(self):
        v = self._verdict("3", "auto")
        out = _MOD._verdict_to_action(v, agent_dir=self.agent, repo_root=self.repo,
                                      calibrated=True, now_iso="2026-06-03T10:00:00",
                                      dry_run=False)
        self.assertEqual(out["decision"], "auto-revert")
        self.assertTrue(out["acted"])
        self.assertTrue(out["revert"]["ok"], out["revert"].get("msg"))
        self.assertEqual((self.repo / "meta/s.yaml").read_text(encoding="utf-8"), "v1\n")
        pend = _MOD.load_pending(self.agent)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["file"], "meta/s.yaml")
        self.assertEqual(pend[0]["composite_at_revert"], 0.30)

    def test_agent_unblock_no_git_change(self):
        v = self._verdict("2", "agent")
        out = _MOD._verdict_to_action(v, agent_dir=self.agent, repo_root=self.repo,
                                      calibrated=True, now_iso="2026-06-03T10:00:00",
                                      dry_run=False)
        self.assertEqual(out["decision"], "agent-unblock")
        self.assertEqual(out["unblock"]["participants"], ["agent"])
        self.assertEqual((self.repo / "meta/s.yaml").read_text(encoding="utf-8"), "v2\n")  # untouched
        self.assertEqual(_MOD.load_pending(self.agent), [])

    def test_user_unblock_notifies(self):
        v = self._verdict("1", "user")
        out = _MOD._verdict_to_action(v, agent_dir=self.agent, repo_root=self.repo,
                                      calibrated=True, now_iso="2026-06-03T10:00:00",
                                      dry_run=False)
        self.assertEqual(out["decision"], "user-unblock")
        self.assertEqual(out["unblock"]["participants"], ["user"])
        self.assertTrue(out["unblock"]["notify"])

    def test_not_eligible_when_not_calibrated(self):
        v = self._verdict("3", "auto")
        out = _MOD._verdict_to_action(v, agent_dir=self.agent, repo_root=self.repo,
                                      calibrated=False, now_iso="2026-06-03T10:00:00",
                                      dry_run=False)
        self.assertEqual(out["decision"], "not-eligible")
        self.assertFalse(out["acted"])
        self.assertEqual((self.repo / "meta/s.yaml").read_text(encoding="utf-8"), "v2\n")  # untouched


if __name__ == "__main__":
    unittest.main()
