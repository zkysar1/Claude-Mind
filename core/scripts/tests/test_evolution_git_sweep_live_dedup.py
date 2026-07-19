"""evolution-git-sweep live-dedup — (file_path, after_hash) skip against
existing stream entries (g-115-2567; investigation g-115-2566).

Pins: (1) a committed live-captured edit is NOT re-appended as a git-sweep
entry, so backfill counts measure real capture misses; (2) live_dedup=False
(--no-live-dedup) restores the pre-g-115-2567 behavior; (3) the skip chains
previous_revision_id to the matched existing entry; (4) null-after_hash
entries and deleted-file blobs never match; (5) the join key normalizes
backslash path drift; (6) any-signal_source entries participate, last-seen
revision_id wins.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_evolution_git_sweep_live_dedup.py -q
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "evo_git_sweep", SCRIPT_DIR / "evolution-git-sweep.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write_stream(world_dir, fname, rows):
    p = Path(world_dir) / fname
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- unit layer

def test_load_existing_after_hashes_any_source_last_seen_wins_null_excluded():
    m = load_mod()
    with tempfile.TemporaryDirectory() as d:
        _write_stream(d, "skill-evolution.jsonl", [
            {"revision_id": "skill-sweep-1", "file_path": ".claude/skills/a/SKILL.md",
             "after_hash": "sha256:aaa", "signal_source": "git-sweep"},
            {"revision_id": "skill-live-1", "file_path": ".claude/skills/a/SKILL.md",
             "after_hash": "sha256:aaa", "signal_source": None},
            {"revision_id": "skill-live-2", "file_path": ".claude/skills/b/SKILL.md",
             "after_hash": None, "signal_source": None},
        ])
        out = m.load_existing_after_hashes(d)
        # last-seen wins (live entry appended after the sweep entry)
        assert out["skill_edit"][(".claude/skills/a/SKILL.md", "sha256:aaa")] == "skill-live-1"
        # null after_hash never enters the map
        assert not any(k[0] == ".claude/skills/b/SKILL.md" for k in out["skill_edit"])


def test_live_dedup_match_hash_path_and_none_semantics():
    m = load_mod()
    content = "# T\n\n## Steps\n\nbody\n"
    ah = m.body_hash(content)
    pair = {(".claude/skills/a/SKILL.md", ah): "skill-live-9"}
    # exact match
    assert m.live_dedup_match(".claude/skills/a/SKILL.md", content, pair) == "skill-live-9"
    # backslash path drift normalizes to the posix key
    assert m.live_dedup_match(".claude\\skills\\a\\SKILL.md", content, pair) == "skill-live-9"
    # CRLF variant of the same body hashes identically (body_hash normalization)
    assert m.live_dedup_match(".claude/skills/a/SKILL.md",
                              content.replace("\n", "\r\n"), pair) == "skill-live-9"
    # same content under a DIFFERENT file never matches (file_path in the key)
    assert m.live_dedup_match(".claude/skills/b/SKILL.md", content, pair) is None
    # deleted blob (None) never matches
    assert m.live_dedup_match(".claude/skills/a/SKILL.md", None, pair) is None


# ---------------------------------------------------------- integration layer

def _git(repo, *args, date=None):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if date:
        # Distinct author dates matter: sweep_file_kind sorts commits by %aI
        # ascending, and two commits in the same second sort in git-log order
        # (newest first) — breaking chain assertions.
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    r = subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


def _mk_repo_with_skill(d, content):
    repo = Path(d) / "repo"
    (repo / ".claude" / "skills" / "tskill").mkdir(parents=True)
    _git(repo, "init", "-q")
    skill = repo / ".claude" / "skills" / "tskill" / "SKILL.md"
    skill.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add tskill", date="2026-07-18T01:00:00+00:00")
    return repo


def _sweep(m, world, live_dedup):
    return m.sweep_file_kind(
        "skill_edit", [".claude/skills/**/SKILL.md"], None, None, world,
        dry_run=True, verbose=False, live_dedup=live_dedup)


def test_sweep_skips_live_captured_commit_and_flag_restores():
    m = load_mod()
    content = "# tskill\n\n## Steps\n\ndo the thing\n"
    with tempfile.TemporaryDirectory() as d:
        repo = _mk_repo_with_skill(d, content)
        world = Path(d) / "world"
        world.mkdir()
        m.PROJECT_ROOT = repo  # module global consulted at call time

        # a live entry already records this exact content-state
        _write_stream(world, "skill-evolution.jsonl", [
            {"revision_id": "skill-20260718T000000-zeta-ab12",
             "file_path": ".claude/skills/tskill/SKILL.md",
             "after_hash": m.body_hash(content), "signal_source": None},
        ])

        new, skipped, skipped_live = _sweep(m, world, live_dedup=True)
        assert new == [], "live-captured commit must not re-enter as a sweep entry"
        assert skipped_live == 1 and skipped == 0

        # forensic escape restores the old behavior
        new2, skipped2, skipped_live2 = _sweep(m, world, live_dedup=False)
        assert len(new2) == 1 and skipped_live2 == 0


def test_sweep_chains_previous_revision_id_across_live_skip():
    m = load_mod()
    v1 = "# tskill\n\n## Steps\n\nv1\n"
    v2 = "# tskill\n\n## Steps\n\nv2\n"
    with tempfile.TemporaryDirectory() as d:
        repo = _mk_repo_with_skill(d, v1)
        skill = repo / ".claude" / "skills" / "tskill" / "SKILL.md"
        skill.write_text(v2, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "edit tskill v2", date="2026-07-18T02:00:00+00:00")
        world = Path(d) / "world"
        world.mkdir()
        m.PROJECT_ROOT = repo

        live_rid = "skill-20260718T000001-zeta-cd34"
        _write_stream(world, "skill-evolution.jsonl", [
            {"revision_id": live_rid,
             "file_path": ".claude/skills/tskill/SKILL.md",
             "after_hash": m.body_hash(v1), "signal_source": None},
        ])

        new, skipped, skipped_live = _sweep(m, world, live_dedup=True)
        # commit1 (v1) live-skipped; commit2 (v2) appended, chained to the live rid
        assert skipped_live == 1
        assert len(new) == 1
        assert new[0]["after_hash"] == m.body_hash(v2)
        assert new[0]["previous_revision_id"] == live_rid


def test_sweep_no_match_appends_normally():
    m = load_mod()
    content = "# tskill\n\n## Steps\n\nunique\n"
    with tempfile.TemporaryDirectory() as d:
        repo = _mk_repo_with_skill(d, content)
        world = Path(d) / "world"
        world.mkdir()
        m.PROJECT_ROOT = repo
        _write_stream(world, "skill-evolution.jsonl", [
            {"revision_id": "skill-x", "file_path": ".claude/skills/tskill/SKILL.md",
             "after_hash": "sha256:doesnotmatch", "signal_source": None},
        ])
        new, skipped, skipped_live = _sweep(m, world, live_dedup=True)
        assert len(new) == 1 and skipped_live == 0
