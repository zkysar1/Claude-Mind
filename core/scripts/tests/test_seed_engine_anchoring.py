"""test_seed_engine_anchoring.py — exclude_always pattern semantics for the seed engine.

Background (2026-05-20, Bug E / testy-incident):
  The /seed transplant pipeline excludes top-level domain state (world/, meta/,
  agents/) so destinations get their own copies. But bare-name `world/` matched
  ANYWHERE in the path — over-matching the legitimate Python packages
  `mind_api/src/world/` and `mind_api/src/meta/` that the daemon imports. A
  destination repo transplanted with the old manifest got a broken daemon at
  startup ("No module named '..world'"). The strategic fix is gitignore-style
  anchoring: a leading `/` restricts the pattern to top-level only.

This test pins both semantics:
  - Anchored patterns (`/foo/`) match top-level ONLY
  - Bare-name patterns (`foo/`) match basename ANYWHERE in tree (legacy; still
    correct for cache dirs like `__pycache__/`)
  - Glob and exact patterns are unaffected by the new branch
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Load _seed_engine via its dashed filename via spec loader. The module has
# no MIND_WORLD / MIND_AGENT dependency at import time, so the conftest
# env-restore guard isn't needed here.
ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
is_excluded = _engine.is_excluded_always


def _manifest(patterns):
    return {"exclude_always": patterns}


# ============================================================================
# Anchored patterns — top-level only (the headline fix for Bug E)
# ============================================================================

def test_anchored_world_excludes_top_level():
    m = _manifest(["/world/"])
    assert is_excluded("world/program.md", m) is True
    assert is_excluded("world/knowledge/tree/_tree.yaml", m) is True
    assert is_excluded("world", m) is True


def test_anchored_world_does_not_exclude_nested_python_package():
    """The headline test: /world/ MUST NOT match mind_api/src/world/."""
    m = _manifest(["/world/"])
    assert is_excluded("mind_api/src/world/__init__.py", m) is False
    assert is_excluded("mind_api/src/world/tree.py", m) is False
    assert is_excluded("mind_api/src/world/pipeline.py", m) is False


def test_anchored_meta_does_not_exclude_nested_python_package():
    m = _manifest(["/meta/"])
    assert is_excluded("meta/spark-questions.jsonl", m) is True
    assert is_excluded("mind_api/src/meta/__init__.py", m) is False
    assert is_excluded("mind_api/src/meta/spark_questions.py", m) is False


def test_anchored_agents_does_not_exclude_arbitrary_nested_agents_dir():
    m = _manifest(["/agents/"])
    assert is_excluded("agents/alpha/journal.jsonl", m) is True
    # Defensive: a contrived nested "agents" dir shouldn't be excluded
    assert is_excluded("core/scripts/tests/fixtures/agents/sample.yaml", m) is False


def test_anchored_dotgit_excludes_top_level_only():
    m = _manifest(["/.git/"])
    assert is_excluded(".git/HEAD", m) is True
    assert is_excluded(".git/objects/abc123", m) is True
    # A hypothetical nested .git in a fixture is not excluded
    assert is_excluded("core/scripts/tests/fixtures/.git/HEAD", m) is False


# ============================================================================
# Bare-name patterns — must still match basename anywhere (legacy semantics)
# ============================================================================

def test_bare_pycache_matches_at_every_depth():
    """Cache dirs MUST still be excluded anywhere — they have no anchored form."""
    m = _manifest(["__pycache__/"])
    assert is_excluded("__pycache__/foo.pyc", m) is True
    assert is_excluded("core/scripts/__pycache__/bar.pyc", m) is True
    assert is_excluded("mind_api/src/__pycache__/baz.pyc", m) is True
    assert is_excluded("a/b/c/d/__pycache__/deep.pyc", m) is True


def test_bare_pytest_cache_matches_at_every_depth():
    m = _manifest([".pytest_cache/"])
    assert is_excluded(".pytest_cache/lastfailed", m) is True
    assert is_excluded("core/.pytest_cache/lastfailed", m) is True
    assert is_excluded("mind_api/.pytest_cache/cachedir", m) is True


def test_bare_world_still_matches_anywhere_legacy_semantics():
    """Backward-compat check: bare `world/` (without leading /) STILL matches
    anywhere. We didn't change the legacy branch — manifests opting out of
    anchoring keep their old behavior."""
    m = _manifest(["world/"])
    assert is_excluded("world/program.md", m) is True
    assert is_excluded("mind_api/src/world/tree.py", m) is True


# ============================================================================
# Other pattern shapes — unaffected by the new branch
# ============================================================================

def test_prefix_pattern_unchanged():
    m = _manifest(["mind_api/state/"])
    assert is_excluded("mind_api/state/db.sqlite", m) is True
    assert is_excluded("mind_api/src/server.py", m) is False
    # Prefix is NOT anchored — but it includes a slash, so it acts as prefix
    # match from the root anyway. This is a pre-existing semantic, just
    # checking it didn't regress.
    assert is_excluded("docs/mind_api/state/foo", m) is False


def test_basename_glob_unchanged():
    m = _manifest(["*.stackdump"])
    assert is_excluded("bash.exe.stackdump", m) is True
    assert is_excluded("nested/path/crash.stackdump", m) is True


def test_exact_path_with_slash_unchanged():
    """A path with a slash hits the fnmatch full-path branch — it's an exact
    path match, not a basename match (Shape 5 in the engine docstring)."""
    m = _manifest([".claude/settings.local.json"])
    assert is_excluded(".claude/settings.local.json", m) is True
    assert is_excluded("nested/.claude/settings.local.json", m) is False


def test_bare_filename_acts_as_basename_glob_legacy():
    """Legacy semantic: a pattern without a slash AND without a trailing /
    falls into the basename-glob branch (Shape 4) — matches the basename
    anywhere in the tree. Documented here so the next reader understands
    why `.env.local` in exclude_always covers all depths, not just root.
    To restrict to top-level only, use the anchored form `/.env.local`."""
    m = _manifest([".env.local"])
    assert is_excluded(".env.local", m) is True
    assert is_excluded("subdir/.env.local", m) is True


def test_anchored_bare_filename_top_level_only():
    """Counterpart: `/.env.local` (anchored) restricts to top-level."""
    m = _manifest(["/.env.local"])
    assert is_excluded(".env.local", m) is True
    assert is_excluded("subdir/.env.local", m) is False


def test_active_agent_glob_unchanged():
    m = _manifest([".active-agent-*"])
    assert is_excluded(".active-agent-abc123", m) is True
    # `.active-agent-*` has no slash and no leading /, so it's a basename
    # glob — matches the basename anywhere in tree.
    assert is_excluded("agents/.active-agent-deep", m) is True


# ============================================================================
# Mixed manifest — the real-world shape
# ============================================================================

def test_realistic_mixed_manifest():
    """Mirror the seed-manifest.yaml mix: anchored domain dirs + bare caches."""
    m = _manifest([
        "/agents/",
        "/world/",
        "/meta/",
        "/.git/",
        "/.history/",
        "mind_api/state/",
        "__pycache__/",
        ".pytest_cache/",
        ".env.local",
    ])
    # Domain dirs excluded at top-level
    assert is_excluded("world/knowledge/tree/_tree.yaml", m) is True
    assert is_excluded("agents/alpha/journal.jsonl", m) is True
    assert is_excluded("meta/spark-questions.jsonl", m) is True
    assert is_excluded(".git/HEAD", m) is True
    # Python packages with the same basenames PRESERVED
    assert is_excluded("mind_api/src/world/__init__.py", m) is False
    assert is_excluded("mind_api/src/meta/__init__.py", m) is False
    assert is_excluded("mind_api/src/meta/spark_questions.py", m) is False
    # Cache dirs still excluded everywhere
    assert is_excluded("core/scripts/__pycache__/foo.pyc", m) is True
    assert is_excluded("mind_api/src/world/__pycache__/x.pyc", m) is True
    # Prefix and exact path semantics preserved
    assert is_excluded("mind_api/state/db.sqlite", m) is True
    assert is_excluded(".env.local", m) is True
    # Regular framework files pass through (not excluded)
    assert is_excluded("core/scripts/_seed_engine.py", m) is False
    assert is_excluded("mind_api/src/server.py", m) is False
    assert is_excluded(".claude/skills/start/SKILL.md", m) is False
