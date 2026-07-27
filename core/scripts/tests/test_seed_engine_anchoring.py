"""test_seed_engine_anchoring.py — exclude_always pattern semantics for the seed engine.

Background (2026-05-20, Bug E / testy-incident):
  The /seed plant pipeline excludes top-level domain state (world/, meta/,
  agents/) so destinations get their own copies. But bare-name `world/` matched
  ANYWHERE in the path — over-matching the legitimate Python packages
  `mind_api/src/world/` and `mind_api/src/meta/` that the daemon imports. A
  destination repo planted with the old manifest got a broken daemon at
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


# ============================================================================
# Orphan removal — destination-owned forged/domain skill protection
# ----------------------------------------------------------------------------
# Background (2026-06-06, omni orphan-removal blocker):
#   seed-transplant's do_remove_orphans mirrors (source ∩ manifest) onto the
#   destination — files at dest absent from the source include set are deleted.
#   The destination (e.g. zds-mind) carries forged/domain skills under
#   `.claude/skills/<name>/` that DO NOT exist in the source (claude-mind seed),
#   because the frontier->seed promotion strips forged skills. Those domain
#   skill dirs were therefore classified as orphans and DELETED — destroying
#   the downstream world's domain capability (11 skills: sam-gov-search,
#   build-landing-page, etc.).
#
#   Fix: read the destination's OWN forged-skills.yaml and protect any
#   `.claude/skills/<name>/` it registers. Fail-safe toward preservation — when
#   no registry is locatable or it is unparseable, preserve EVERY skill dir.
# ============================================================================

def _w(path: Path, content: str = "x") -> None:
    """Create a file (and parents) with `content`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# Minimal forged-skills.yaml: skill names are the keys under `skills:`.
_REGISTRY = "skills:\n  {name}:\n    type: domain\n    parent: null\n"


def _src_with_base_skill(src: Path) -> dict:
    """Source carries ONE base skill. Manifest includes the whole skills dir,
    so the resolved include set is {.claude/skills/start/SKILL.md} — the
    domain skills the destination owns are deliberately absent from source.
    """
    _w(src / ".claude" / "skills" / "start" / "SKILL.md", "base start skill")
    return {"include": [{"path": ".claude/skills", "type": "dir"}],
            "exclude_always": []}


def test_orphan_removal_preserves_registered_dest_forged_skill(tmp_path):
    """Headline: a dest forged skill absent from source survives because the
    dest's own registry lists it; a genuine non-skill orphan is still removed."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    manifest = _src_with_base_skill(src)

    _w(dest / ".claude" / "skills" / "start" / "SKILL.md", "old base")          # in expected
    _w(dest / ".claude" / "skills" / "sam-gov-search" / "SKILL.md", "domain")   # protected
    _w(dest / ".claude" / "orphan-note.md", "removed upstream")                 # genuine orphan
    _w(dest / "world" / "forged-skills.yaml", _REGISTRY.format(name="sam-gov-search"))

    result = _engine.do_remove_orphans(dest, manifest, src)

    assert (dest / ".claude" / "skills" / "start" / "SKILL.md").exists()
    assert (dest / ".claude" / "skills" / "sam-gov-search" / "SKILL.md").exists()
    assert not (dest / ".claude" / "orphan-note.md").exists()
    assert ".claude/orphan-note.md" in result["removed"]
    assert ".claude/skills/sam-gov-search/SKILL.md" not in result["removed"]


def test_orphan_removal_preserves_unregistered_skill_with_skillmd(tmp_path):
    """Root cause A (/): a skill dir carrying a SKILL.md is a
    REAL skill and is PRESERVED even when absent from BOTH the source include set
    AND the dest registry — because forged-skill REGISTRATION is external/gitignored
    and does NOT travel with a git promotion, so a legitimately-promoted skill
    arrives unregistered (the notify-user deletion, twice). Protection is
    SKILL.md-presence-based, not registry-membership-based (guard-1271). A skill
    dir WITHOUT a SKILL.md, and a genuine non-skill orphan, are still removed —
    so the protection is scoped to real skills, not a blanket `.claude/skills/` keep.

    (Supersedes the retired test_orphan_removal_removes_unregistered_skill_when_
    registry_present, which asserted the pre-fix behavior that DELETED such a skill.)"""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    manifest = _src_with_base_skill(src)

    _w(dest / ".claude" / "skills" / "start" / "SKILL.md", "old base")
    # unregistered but REAL (has SKILL.md) — the promoted-but-unregistered shape
    _w(dest / ".claude" / "skills" / "ghost-skill" / "SKILL.md", "not registered")
    # skill DIR without a SKILL.md — not a real skill, still a true orphan
    _w(dest / ".claude" / "skills" / "empty-shell" / "notes.txt", "no SKILL.md here")
    # genuine non-skill orphan
    _w(dest / ".claude" / "orphan-note.md", "removed upstream")
    _w(dest / "world" / "forged-skills.yaml", _REGISTRY.format(name="sam-gov-search"))

    result = _engine.do_remove_orphans(dest, manifest, src)

    # base skill in include set: kept
    assert (dest / ".claude" / "skills" / "start" / "SKILL.md").exists()
    # unregistered-but-real skill: NOW PRESERVED (root cause A fix)
    assert (dest / ".claude" / "skills" / "ghost-skill" / "SKILL.md").exists()
    assert ".claude/skills/ghost-skill/SKILL.md" not in result["removed"]
    # no-SKILL.md skill dir: still an orphan, removed
    assert not (dest / ".claude" / "skills" / "empty-shell" / "notes.txt").exists()
    assert ".claude/skills/empty-shell/notes.txt" in result["removed"]
    # non-skill orphan: removed
    assert not (dest / ".claude" / "orphan-note.md").exists()
    assert ".claude/orphan-note.md" in result["removed"]


def test_orphan_removal_fail_safe_preserves_all_skills_when_no_registry(tmp_path):
    """No registry locatable at dest → protect_all_skills=True → EVERY skill
    dir survives, registered or not. Non-skill orphans are still removed (the
    fail-safe is scoped to `.claude/skills/`)."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    manifest = _src_with_base_skill(src)

    _w(dest / ".claude" / "skills" / "start" / "SKILL.md", "old base")
    _w(dest / ".claude" / "skills" / "ghost-skill" / "SKILL.md", "unregistered")
    _w(dest / ".claude" / "orphan-note.md", "removed upstream")
    # NO registry: no world/forged-skills.yaml, no agents/*/local-paths.conf

    result = _engine.do_remove_orphans(dest, manifest, src)

    assert (dest / ".claude" / "skills" / "start" / "SKILL.md").exists()
    assert (dest / ".claude" / "skills" / "ghost-skill" / "SKILL.md").exists()
    assert not (dest / ".claude" / "orphan-note.md").exists()


def test_orphan_removal_protects_via_external_world_path(tmp_path):
    """End-to-end: registry lives at an EXTERNAL world path (the normal layout),
    discovered through agents/*/local-paths.conf. The registered domain skill
    survives orphan-removal."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    ext_world = tmp_path / "ExternalWorld"
    manifest = _src_with_base_skill(src)

    _w(ext_world / "forged-skills.yaml", _REGISTRY.format(name="build-landing-page"))
    _w(dest / "agents" / "omni" / "local-paths.conf",
       f"WORLD_PATH={ext_world.as_posix()}\nMETA_PATH={(tmp_path / 'M').as_posix()}\n")
    _w(dest / ".claude" / "skills" / "start" / "SKILL.md", "old base")
    _w(dest / ".claude" / "skills" / "build-landing-page" / "SKILL.md", "domain")

    result = _engine.do_remove_orphans(dest, manifest, src)

    assert (dest / ".claude" / "skills" / "build-landing-page" / "SKILL.md").exists()
    assert ".claude/skills/build-landing-page/SKILL.md" not in result["removed"]


def test_dest_forged_skill_names_none_when_unlocatable(tmp_path):
    """No registry anywhere → None (NOT empty set), so callers fail safe."""
    dest = tmp_path / "dest"
    dest.mkdir()
    assert _engine._dest_forged_skill_names(dest) is None


def test_dest_forged_skill_names_empty_set_when_registry_lists_none(tmp_path):
    """A registry that IS found but lists zero skills → empty set (distinct from
    None): base-skill orphan removal then proceeds normally."""
    dest = tmp_path / "dest"
    _w(dest / "world" / "forged-skills.yaml", "skills: {}\n")
    assert _engine._dest_forged_skill_names(dest) == set()


def test_dest_forged_skill_names_none_on_unparseable_registry(tmp_path):
    """A located-but-unparseable registry → None (fail safe), never a crash."""
    dest = tmp_path / "dest"
    _w(dest / "world" / "forged-skills.yaml", "skills: {unterminated: [\n")
    assert _engine._dest_forged_skill_names(dest) is None


def test_read_world_path_from_conf_strips_quotes_and_comments(tmp_path):
    conf = tmp_path / "local-paths.conf"
    conf.write_text(
        "# a comment\n"
        "META_PATH=/tmp/meta\n"
        'WORLD_PATH="/tmp/ext world"\n',
        encoding="utf-8",
    )
    assert _engine._read_world_path_from_conf(conf) == "/tmp/ext world"


# ============================================================================
# Cruft cleanup — living-prod preservation guard
# ----------------------------------------------------------------------------
# Background (2026-06-25, FM-2 incident / seed-transplant-reconcile-gaps):
#   do_clean_cruft iterates cruft_patterns from the manifest and deletes
#   unconditionally. Unlike do_remove_orphans, it had NO _is_preserved_at_dest
#   guard. If a cruft_pattern matches a deployment-local file or a domain
#   forged-skill directory (e.g. .claude/skills/notify-user/), the function
#   destroys it. Fix: when preserve_deployment_local=True (--living-prod mode),
#   do_clean_cruft guards each deletion with _is_preserved_at_dest and
#   _is_protected_dest_skill. Skipped items are reported in skipped_preserved.
# ============================================================================

def _cruft_manifest(cruft_patterns, extra_include=None):
    """Build a minimal manifest with cruft_patterns."""
    m = {"cruft_patterns": cruft_patterns, "include": [], "exclude_always": []}
    if extra_include:
        m["include"] = extra_include
    return m


def test_clean_cruft_preserves_forged_skill_dir_with_living_prod(tmp_path):
    """Headline: a domain forged-skill directory listed as cruft is NOT deleted
    when preserve_deployment_local=True and the dest registry lists it."""
    dest = tmp_path / "dest"
    _w(dest / ".claude" / "skills" / "notify-user" / "SKILL.md", "forged domain skill")
    _w(dest / "world" / "forged-skills.yaml", _REGISTRY.format(name="notify-user"))
    # Also create a genuine cruft file that SHOULD be removed
    _w(dest / ".active-agent-abc123", "stale binding")

    manifest = _cruft_manifest([
        ".claude/skills/notify-user/",   # domain skill — should be preserved
        ".active-agent-*",               # genuine cruft — should be removed
    ])

    result = _engine.do_clean_cruft(dest, manifest, preserve_deployment_local=True)

    # Domain skill preserved
    assert (dest / ".claude" / "skills" / "notify-user" / "SKILL.md").exists()
    assert ".claude/skills/notify-user/" in result["skipped_preserved"]
    # Genuine cruft removed
    assert not (dest / ".active-agent-abc123").exists()
    assert ".active-agent-abc123" in result["removed"]


def test_clean_cruft_preserves_unregistered_skill_with_skillmd_living_prod(tmp_path):
    """Root cause A (): a skill dir with a SKILL.md matched by a cruft
    pattern is preserved under --living-prod even when the dest registry does NOT
    list it (registration is external/gitignored, so a git-promoted skill arrives
    unregistered). SKILL.md-presence protection, not registry membership
    (guard-1271) — the cruft-lane twin of the orphan-lane fix. A genuine cruft
    file is still removed."""
    dest = tmp_path / "dest"
    _w(dest / ".claude" / "skills" / "notify-user" / "SKILL.md", "domain transport")
    # registry lists a DIFFERENT skill — notify-user is unregistered at dest
    _w(dest / "world" / "forged-skills.yaml", _REGISTRY.format(name="something-else"))
    _w(dest / ".active-agent-abc123", "stale binding")

    manifest = _cruft_manifest([
        ".claude/skills/notify-user/",   # unregistered real skill — preserved via SKILL.md
        ".active-agent-*",               # genuine cruft — removed
    ])

    result = _engine.do_clean_cruft(dest, manifest, preserve_deployment_local=True)

    assert (dest / ".claude" / "skills" / "notify-user" / "SKILL.md").exists()
    assert ".claude/skills/notify-user/" in result["skipped_preserved"]
    assert not (dest / ".active-agent-abc123").exists()
    assert ".active-agent-abc123" in result["removed"]


def test_clean_cruft_preserves_deployment_local_file_with_living_prod(tmp_path):
    """A deployment-local file (.env.local) matched by a cruft pattern is NOT
    deleted when preserve_deployment_local=True."""
    dest = tmp_path / "dest"
    _w(dest / ".env.local", "SECRET=foo")
    _w(dest / ".stale-lockfile", "cruft")

    manifest = _cruft_manifest([
        ".env.local",        # deployment-local — should be preserved
        ".stale-lockfile",   # genuine cruft — should be removed
    ])

    result = _engine.do_clean_cruft(dest, manifest, preserve_deployment_local=True)

    assert (dest / ".env.local").exists()
    assert ".env.local" in result["skipped_preserved"]
    assert not (dest / ".stale-lockfile").exists()
    assert ".stale-lockfile" in result["removed"]


def test_clean_cruft_removes_everything_without_living_prod(tmp_path):
    """Without preserve_deployment_local, cruft cleanup removes everything
    (backward-compatible behavior)."""
    dest = tmp_path / "dest"
    _w(dest / ".claude" / "skills" / "notify-user" / "SKILL.md", "domain skill")
    _w(dest / "world" / "forged-skills.yaml", _REGISTRY.format(name="notify-user"))
    _w(dest / ".active-agent-abc123", "stale binding")

    manifest = _cruft_manifest([
        ".claude/skills/notify-user/",
        ".active-agent-*",
    ])

    result = _engine.do_clean_cruft(dest, manifest, preserve_deployment_local=False)

    # Everything removed — no preservation
    assert not (dest / ".claude" / "skills" / "notify-user" / "SKILL.md").exists()
    assert not (dest / ".active-agent-abc123").exists()
    assert ".claude/skills/notify-user/" in result["removed"]
    assert result["skipped_preserved"] == []


def test_clean_cruft_preserves_operational_dir_with_living_prod(tmp_path):
    """Gitignored operational directories (core/logs/) are preserved when
    preserve_deployment_local=True and matched by a glob cruft pattern."""
    dest = tmp_path / "dest"
    _w(dest / "core" / "logs" / "watchdog-alpha.jsonl", "log data")
    _w(dest / "core" / "stale-cruft.tmp", "cruft")

    manifest = _cruft_manifest([
        "core/logs/",         # operational dir — should be preserved
        "core/stale-cruft.tmp",  # genuine cruft — should be removed
    ])

    result = _engine.do_clean_cruft(dest, manifest, preserve_deployment_local=True)

    assert (dest / "core" / "logs" / "watchdog-alpha.jsonl").exists()
    assert "core/logs/" in result["skipped_preserved"]
    assert not (dest / "core" / "stale-cruft.tmp").exists()
    assert "core/stale-cruft.tmp" in result["removed"]
