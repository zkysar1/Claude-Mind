"""test_paths_mind_data_resolution.py -- asp-330 M1 () regression test.

Verifies the .mind-data/ local-storage-root resolution added to the resolver
layers (core/scripts/_paths.py and mind_api/src/agent_paths.py; the _paths.sh
shell layer is exercised by an isolated bash harness during execution, not here).

Priority chain (per tier, world/meta):
  1. MIND_{WORLD,META} env var
  2. .mind-data/.env.local {WORLD,META}_PATH   (only when .mind-data/ exists)
  3. .mind-data/{world,meta} bare default      (only when .mind-data/ exists)
  4. local-paths.conf {WORLD,META}_PATH        (legacy fallback)
  5. None (2026-05-19 hard-cut preserved when nothing configured; guard-551)

The .mind-data/ tier is GATED on the dir existing, so a configured repo (live
agents resolve via external local-paths.conf, no .mind-data/ dir) is byte-for-
byte unchanged: the new tier is skipped and resolution reaches the legacy conf.

Tests use INJECTED roots (tmp dirs) so the real PROJECT_ROOT/.mind-data is never
created -- a .mind-data/ in the shared working tree would shadow EVERY live
agent's external paths (guard-741). Env vars are snapshot/restored per the
g-115-889 polluter-fix discipline.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


# parents[1] of core/scripts/tests/<file> is core/scripts; the REPO ROOT is two
# .parent hops up, not one — `CORE_SCRIPTS.parent` is core/, which left mind_api
# unimportable (ModuleNotFoundError; the  `.parent`-arithmetic bug
# class from CLAUDE.md "Agent-dir Resolution" third-grep triage). .
CORE_SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _norm(p) -> str:
    """Normalize a path to forward slashes, no trailing slash, for tail compare
    (robust to absolutize() drive/separator normalization across platforms)."""
    return str(p).replace("\\", "/").rstrip("/")


class _EnvIsolatedTest(unittest.TestCase):
    """Snapshot/restore MIND_WORLD/MIND_META + a per-test tmp root."""

    def setUp(self):
        self._env_snapshot = {
            k: os.environ.get(k) for k in ("MIND_WORLD", "MIND_META")
        }
        for v in ("MIND_WORLD", "MIND_META"):
            os.environ.pop(v, None)
        self._tmproot = Path(tempfile.mkdtemp(prefix="mind_data_test_"))
        self.addCleanup(shutil.rmtree, self._tmproot, ignore_errors=True)

    def tearDown(self):
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestResolveTier(_EnvIsolatedTest):
    """core/scripts/_paths.py::_resolve_tier (the pure, injected-roots helper)."""

    def setUp(self):
        super().setUp()
        import _paths  # noqa: E402
        self._paths = _paths

    def _resolve(self, **kw):
        return self._paths._resolve_tier(**kw)

    def test_env_var_wins_over_everything(self):
        md = self._tmproot / ".mind-data"
        md.mkdir()
        os.environ["MIND_WORLD"] = str(self._tmproot / "envw" / "world")
        result = self._resolve(
            env_key="MIND_WORLD", conf_key="WORLD_PATH",
            mind_data_dir=md,
            mind_data_env={"WORLD_PATH": str(self._tmproot / "mdoverride")},
            local_conf={"WORLD_PATH": str(self._tmproot / "legacy")},
        )
        self.assertTrue(_norm(result).endswith("envw/world"))

    def test_mind_data_env_local_override_beats_legacy_conf(self):
        md = self._tmproot / ".mind-data"
        md.mkdir()
        result = self._resolve(
            env_key="MIND_WORLD", conf_key="WORLD_PATH",
            mind_data_dir=md,
            mind_data_env={"WORLD_PATH": str(self._tmproot / "mdoverride" / "world")},
            local_conf={"WORLD_PATH": str(self._tmproot / "legacy" / "world")},
        )
        self.assertTrue(_norm(result).endswith("mdoverride/world"))

    def test_mind_data_bare_default_when_dir_exists_no_envlocal(self):
        # The verification outcome: "resolves .mind-data/world when no .env.local".
        # Bare default also wins over a legacy conf entry once .mind-data/ exists.
        md = self._tmproot / ".mind-data"
        md.mkdir()
        result = self._resolve(
            env_key="MIND_WORLD", conf_key="WORLD_PATH",
            mind_data_dir=md, mind_data_env={},
            local_conf={"WORLD_PATH": str(self._tmproot / "legacy" / "world")},
        )
        self.assertEqual(result, md / "world")

    def test_meta_bare_default_uses_meta_subdir(self):
        md = self._tmproot / ".mind-data"
        md.mkdir()
        result = self._resolve(
            env_key="MIND_META", conf_key="META_PATH",
            mind_data_dir=md, mind_data_env={}, local_conf={},
        )
        self.assertEqual(result, md / "meta")

    def test_legacy_conf_when_no_mind_data_dir(self):
        # Live-agent path: no .mind-data/ dir -> fall through to local-paths.conf.
        md = self._tmproot / ".mind-data"  # intentionally NOT created
        result = self._resolve(
            env_key="MIND_WORLD", conf_key="WORLD_PATH",
            mind_data_dir=md, mind_data_env={},
            local_conf={"WORLD_PATH": str(self._tmproot / "legacy" / "world")},
        )
        self.assertTrue(_norm(result).endswith("legacy/world"))

    def test_none_when_nothing_configured(self):
        # Hard-cut preserved (guard-551): no env, no .mind-data/, no conf -> None.
        md = self._tmproot / ".mind-data"  # intentionally NOT created
        result = self._resolve(
            env_key="MIND_WORLD", conf_key="WORLD_PATH",
            mind_data_dir=md, mind_data_env={}, local_conf={},
        )
        self.assertIsNone(result)

    def test_mind_data_env_unknown_key_falls_to_bare_default(self):
        # .env.local present but without the requested key -> bare default.
        md = self._tmproot / ".mind-data"
        md.mkdir()
        result = self._resolve(
            env_key="MIND_WORLD", conf_key="WORLD_PATH",
            mind_data_dir=md,
            mind_data_env={"STORAGE_BACKEND": "own-cloud"},  # no WORLD_PATH key
            local_conf={},
        )
        self.assertEqual(result, md / "world")


class TestDaemonResolverMindData(_EnvIsolatedTest):
    """mind_api/src/agent_paths.py::AgentPathResolver -- symmetric .mind-data/."""

    def setUp(self):
        super().setUp()
        from mind_api.src.agent_paths import AgentPathResolver  # noqa: E402
        self._Resolver = AgentPathResolver
        # One agent dir with an EMPTY conf (no WORLD_PATH/META_PATH entries).
        agent = self._tmproot / "agents" / "testagent"
        agent.mkdir(parents=True)
        (agent / "local-paths.conf").write_text("", encoding="utf-8")

    def test_daemon_resolves_mind_data_bare_default(self):
        (self._tmproot / ".mind-data").mkdir()
        resolver = self._Resolver(self._tmproot)
        paths = resolver.resolve("testagent")
        self.assertTrue(_norm(paths.world).endswith(".mind-data/world"))
        self.assertTrue(_norm(paths.meta).endswith(".mind-data/meta"))

    def test_daemon_mind_data_env_local_override(self):
        md = self._tmproot / ".mind-data"
        md.mkdir()
        (md / ".env.local").write_text(
            "WORLD_PATH=" + str(self._tmproot / "dcustom" / "world") + "\n",
            encoding="utf-8",
        )
        resolver = self._Resolver(self._tmproot)
        paths = resolver.resolve("testagent")
        self.assertTrue(_norm(paths.world).endswith("dcustom/world"))
        # META has no override -> bare default
        self.assertTrue(_norm(paths.meta).endswith(".mind-data/meta"))

    def test_daemon_no_mind_data_raises_when_unconfigured(self):
        # No .mind-data/, empty conf, no env -> RuntimeError (hard-cut preserved).
        resolver = self._Resolver(self._tmproot)
        with self.assertRaises(RuntimeError):
            resolver.resolve("testagent")


if __name__ == "__main__":
    unittest.main()
