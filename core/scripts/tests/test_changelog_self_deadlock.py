""" regression: store-hygiene cap on a changelog.jsonl store must NOT
self-deadlock.

Root cause (confirmed rb-2736, verified 4/4 during g-115-1651): every
locked_{modify,append,write}_jsonl holds ``path.with_suffix('.lock')`` across
its read-modify-write, then calls ``append_changelog(base_dir, agent, path,
'edit')`` post-write (7 sites). When ``path`` IS ``base_dir/changelog.jsonl``
that held lock IS ``base_dir/changelog.lock``, which ``append_changelog``
re-acquires — non-reentrant (locked_modify_jsonl docstring) → self-deadlock →
``acquire_lock`` 10s timeout → the cap never persists.

Fix: ``append_changelog`` early-returns when the changed file IS the changelog
itself (self-referential noise a cap/rotate would drop anyway; single-source fix
for all lock-holding call sites, incl. the world/changelog rotate path).
"""
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _fileops  # noqa: E402
from storage_backend import LocalBackend  # noqa: E402


@pytest.fixture
def _temp_dirs(tmp_path, monkeypatch):
    # guard-652: a fixture that sets MIND_WORLD/WORLD_DIR to a temp dir MUST
    # also set META_DIR — resolve_base_dir walks WORLD > META > AGENT and a
    # stale real META_DIR would make base-dir resolution nondeterministic.
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    world.mkdir()
    meta.mkdir()
    monkeypatch.setattr(_fileops, "WORLD_DIR", world)
    monkeypatch.setattr(_fileops, "META_DIR", meta)
    # Force LocalBackend — real acquire_lock/release_lock (so an un-fixed
    # self-deadlock genuinely blocks) + no-op refresh (no S3/OwnCloud, keeps
    # the test hermetic even when a live own-cloud daemon is present).
    monkeypatch.setattr(_fileops, "get_backend", lambda: LocalBackend())
    return world, meta


def test_locked_modify_on_changelog_does_not_self_deadlock(_temp_dirs):
    """The exact store-hygiene cap scenario: locked_modify_jsonl caps
    base_dir/changelog.jsonl. Before the fix this blocks on acquire_lock's 10s
    timeout (self-deadlock); after the fix it completes fast and caps."""
    world, _meta = _temp_dirs
    changelog = world / "changelog.jsonl"
    changelog.write_text(
        "".join(json.dumps({"n": i}) + "\n" for i in range(5)), encoding="utf-8"
    )

    start = time.monotonic()
    result = _fileops.locked_modify_jsonl(changelog, lambda items: items[-3:])
    elapsed = time.monotonic() - start

    # acquire_lock timeout is 10s; a self-deadlock regression would take ~10s.
    assert elapsed < 5.0, (
        f"locked_modify_jsonl on changelog took {elapsed:.1f}s — "
        "self-deadlock regression (append_changelog re-acquiring the held lock)"
    )
    assert result == [{"n": 2}, {"n": 3}, {"n": 4}]
    lines = [ln for ln in changelog.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3, "cap did not persist"


def test_append_changelog_skips_self_referential_entry(_temp_dirs):
    """append_changelog must NOT log an edit to the changelog INTO the changelog
    (self-referential + the deadlock source)."""
    world, _meta = _temp_dirs
    changelog = world / "changelog.jsonl"
    _fileops.append_changelog(str(world), "alpha", str(changelog), "edit")
    assert not changelog.exists() or changelog.read_text().strip() == "", (
        "append_changelog wrote a self-referential changelog.jsonl entry"
    )


def test_append_changelog_still_logs_normal_file(_temp_dirs):
    """Regression guard: the self-skip must NOT suppress logging for a normal
    (non-changelog) file edit."""
    world, _meta = _temp_dirs
    other = world / "reasoning-bank.jsonl"
    _fileops.append_changelog(str(world), "alpha", str(other), "edit")
    assert (world / "changelog.jsonl").exists(), "normal-file changelog entry was dropped"
    entry = json.loads((world / "changelog.jsonl").read_text().strip().splitlines()[-1])
    assert entry["file"] == "reasoning-bank.jsonl"
    assert entry["action"] == "edit"
