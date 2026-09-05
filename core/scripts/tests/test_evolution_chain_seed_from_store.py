"""previous_revision_id is seeded FROM THE STORE, not from per-run state ().

Both writers derived the pointer from state that does not outlive the write:
evolution-git-sweep.py reset it to None per file per --since window, and
evolution-record.py copied it from a sidecar that is empty for the file kinds
carrying no front-matter revision chain. Measured before the fix: 239 git-sweep
rows and 11,419 live rows recorded a null predecessor although an earlier row
for the same file_path already existed.

Pins: (1) chain_index shape — ts ordering, path normalization, tolerant skips;
(2) latest_rid newest-wins; (3) latest_rid_before is STRICTLY older, so a
re-sweep of an old window cannot chain forward; (4) the sweep seeds a new
entry from an existing store row; (5) an empty store still yields None
(fresh-world behavior unchanged); (6) a store holding only NEWER rows still
yields None end-to-end; (7) evolution-record.py's null-fill runs BEFORE the
event-stream write.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_evolution_chain_seed_from_store.py -q
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

from _evolution_chain import chain_index, latest_rid, latest_rid_before  # noqa: E402


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

def test_chain_index_orders_by_ts_normalizes_path_and_skips_bad_rows():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "skill-evolution.jsonl"
        p.write_text(
            # deliberately out of ts order — the index must sort, not trust order
            json.dumps({"revision_id": "r2", "ts": "2026-08-02T00:00:00",
                        "file_path": ".claude/skills/a/SKILL.md"}) + "\n"
            + json.dumps({"revision_id": "r1", "ts": "2026-08-01T00:00:00",
                          "file_path": ".claude\\skills\\a\\SKILL.md"}) + "\n"
            + "{ not json at all\n"
            + json.dumps({"revision_id": "no-ts", "file_path": "x"}) + "\n"
            + json.dumps({"ts": "2026-08-03T00:00:00", "file_path": "x"}) + "\n"
            + "\n",
            encoding="utf-8")
        idx = chain_index(p)
        # backslash row joined onto the same posix key
        assert [rid for _ts, rid in idx[".claude/skills/a/SKILL.md"]] == ["r1", "r2"]
        # rows missing revision_id or ts never enter the index
        assert "x" not in idx


def test_chain_index_missing_file_is_empty_not_an_error():
    with tempfile.TemporaryDirectory() as d:
        assert chain_index(Path(d) / "absent-evolution.jsonl") == {}


def test_latest_rid_returns_newest_and_none_for_unknown_path():
    with tempfile.TemporaryDirectory() as d:
        p = _write_stream(d, "s.jsonl", [
            {"revision_id": "old", "ts": "2026-08-01T00:00:00", "file_path": "f"},
            {"revision_id": "new", "ts": "2026-08-09T00:00:00", "file_path": "f"},
        ])
        idx = chain_index(p)
        assert latest_rid(idx, "f") == "new"
        assert latest_rid(idx, "other") is None


def test_latest_rid_before_is_strictly_older_so_a_resweep_cannot_chain_forward():
    with tempfile.TemporaryDirectory() as d:
        p = _write_stream(d, "s.jsonl", [
            {"revision_id": "a", "ts": "2026-08-01T00:00:00", "file_path": "f"},
            {"revision_id": "b", "ts": "2026-08-05T00:00:00", "file_path": "f"},
            {"revision_id": "c", "ts": "2026-08-09T00:00:00", "file_path": "f"},
        ])
        idx = chain_index(p)
        assert latest_rid_before(idx, "f", "2026-08-07T00:00:00") == "b"
        # exact equality is NOT "before" — an entry never chains to itself
        assert latest_rid_before(idx, "f", "2026-08-05T00:00:00") == "a"
        # every stored row is newer than the entry being written -> no seed,
        # which is the case that must not silently chain backwards
        assert latest_rid_before(idx, "f", "2026-07-01T00:00:00") is None
        assert latest_rid_before(idx, "unknown", "2026-08-07T00:00:00") is None


# ---------------------------------------------------------- integration layer

def _git(repo, *args, date=None):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if date:
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    r = subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


def _mk_repo_with_skill(d, content, date="2026-07-18T01:00:00+00:00"):
    repo = Path(d) / "repo"
    (repo / ".claude" / "skills" / "tskill").mkdir(parents=True)
    _git(repo, "init", "-q")
    skill = repo / ".claude" / "skills" / "tskill" / "SKILL.md"
    skill.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add tskill", date=date)
    return repo


def _sweep(m, world):
    return m.sweep_file_kind(
        "skill_edit", [".claude/skills/**/SKILL.md"], None, None, world,
        dry_run=True, verbose=False, live_dedup=True)


def test_sweep_seeds_previous_revision_id_from_an_existing_store_row():
    """THE REGRESSION: a file with prior rows must not restart at None."""
    m = load_mod()
    with tempfile.TemporaryDirectory() as d:
        repo = _mk_repo_with_skill(d, "# tskill\n\n## Steps\n\nv1\n")
        world = Path(d) / "world"
        world.mkdir()
        m.PROJECT_ROOT = repo
        # an EARLIER row for this file already exists (a prior --since window).
        # after_hash deliberately does not match, so live-dedup does not skip.
        _write_stream(world, "skill-evolution.jsonl", [
            {"revision_id": "skill-20260101T000000-zeta-ab12",
             "ts": "2026-01-01T00:00:00",
             "file_path": ".claude/skills/tskill/SKILL.md",
             "after_hash": "sha256:something-else", "signal_source": "git-sweep"},
        ])
        new, _skipped, _skipped_live = _sweep(m, world)
        assert len(new) == 1
        assert new[0]["previous_revision_id"] == "skill-20260101T000000-zeta-ab12"


def test_sweep_on_empty_store_still_starts_a_null_root():
    m = load_mod()
    with tempfile.TemporaryDirectory() as d:
        repo = _mk_repo_with_skill(d, "# tskill\n\n## Steps\n\nv1\n")
        world = Path(d) / "world"
        world.mkdir()
        m.PROJECT_ROOT = repo
        new, _s, _sl = _sweep(m, world)
        assert len(new) == 1
        assert new[0]["previous_revision_id"] is None


def test_sweep_does_not_chain_to_a_row_newer_than_the_commit():
    m = load_mod()
    with tempfile.TemporaryDirectory() as d:
        repo = _mk_repo_with_skill(d, "# tskill\n\n## Steps\n\nv1\n",
                                   date="2026-07-18T01:00:00+00:00")
        world = Path(d) / "world"
        world.mkdir()
        m.PROJECT_ROOT = repo
        _write_stream(world, "skill-evolution.jsonl", [
            {"revision_id": "skill-20991231T000000-zeta-ffff",
             "ts": "2099-12-31T00:00:00",
             "file_path": ".claude/skills/tskill/SKILL.md",
             "after_hash": "sha256:nope", "signal_source": "git-sweep"},
        ])
        new, _s, _sl = _sweep(m, world)
        assert len(new) == 1
        assert new[0]["previous_revision_id"] is None, \
            "a stored row NEWER than this commit must never become its predecessor"


# --------------------------------------------------------- wiring (structural)

def test_record_null_fill_runs_before_the_event_stream_write():
    """Ordering pin: a correct helper called after the write repairs nothing
    (guard-1943 — pinning the writer says nothing about the wiring)."""
    src = (SCRIPT_DIR / "evolution-record.py").read_text(encoding="utf-8")
    fill = src.index('if entry.get("previous_revision_id") is None:')
    write = src.index("write_stub_entry(world_dir, file_kind, entry)", fill)
    assert fill < write
    assert "_evolution_chain" in src[fill:write]
