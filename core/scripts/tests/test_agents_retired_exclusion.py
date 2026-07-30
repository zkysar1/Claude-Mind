"""Retirement-tombstone exclusion in _agents.get_active_agents() ().

Retirement is a TOMBSTONE, not a delete — the store denies the delete right, so
a retired agent's shard SURVIVES and keeps being written. The pre-fix roster was
built from row FILENAMES and never opened the files, so the tombstone was
unreachable BY CONSTRUCTION. Measured on the live repo: meta-tiebreaker was
retired at 17:08:19 and `get_ACTIVE_agents()` still returned it 3h later.

Hermetic: every test builds its own tmp project root and monkeypatches
`_agents._project_root`, so nothing here reads the live world.
"""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _agents  # noqa: E402


RETIRED = {"retired": True, "retired_at": "2026-07-14T09:00:00",
           "last_active": "2026-07-14T08:00:00", "retired_by": "bravo"}
REVIVED = {"retired": True, "retired_at": "2026-07-14T09:00:00",
           "last_active": "2026-07-14T10:00:00", "retired_by": "bravo"}
LIVE = {"last_active": "2026-07-14T10:00:00"}


def _mkworld(tmp_path, core_status=None, rows=None):
    """Build a tmp project root with a world/team-state.yaml + shard rows."""
    root = tmp_path / "proj"
    rows_dir = root / "world" / "team-state" / "agents"
    rows_dir.mkdir(parents=True, exist_ok=True)
    (root / "world" / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": core_status or {}}), encoding="utf-8")
    for name, entry in (rows or {}).items():
        (rows_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(entry), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point _agents at a tmp root and clear its cache around every test."""
    _agents.clear_cache()
    yield
    _agents.clear_cache()


def _bind(monkeypatch, root):
    monkeypatch.setattr(_agents, "_project_root", lambda: root)
    _agents.clear_cache()


# --- The bug: a tombstoned shard was returned as active --------------------

def test_tombstoned_shard_is_excluded(monkeypatch, tmp_path):
    root = _mkworld(tmp_path, rows={"alpha": LIVE, "ghost": RETIRED})
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha",)


def test_live_agents_are_unaffected(monkeypatch, tmp_path):
    root = _mkworld(tmp_path, rows={"alpha": LIVE, "bravo": LIVE})
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha", "bravo")


# --- Self-healing revival must survive ------------------------------------

def test_revived_agent_is_still_returned(monkeypatch, tmp_path):
    # last_active NEWER than retired_at un-retires the row. Losing this would
    # strand a revived agent's work, which is worse than the original bug.
    root = _mkworld(tmp_path, rows={"alpha": LIVE, "phoenix": REVIVED})
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha", "phoenix")


# --- The OTHER bypass site: raw core-file agent_status keys ----------------

def test_retired_core_status_key_is_excluded(monkeypatch, tmp_path):
    # The core-file branch read raw agent_status keys rather than the composed
    # view, so it bypassed the tombstone independently of the shard branch.
    root = _mkworld(tmp_path, core_status={"alpha": LIVE, "ghost": RETIRED})
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha",)


def test_row_file_wins_over_stale_core_residual(monkeypatch, tmp_path):
    # compose_agent_status is whole-row newest-wins with the row winning ties,
    # so a fresh row must re-admit an agent the core file still calls retired.
    root = _mkworld(tmp_path,
                    core_status={"phoenix": RETIRED},
                    rows={"phoenix": REVIVED})
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("phoenix",)


# --- Fail-open direction ---------------------------------------------------

def test_corrupt_shard_reads_as_not_retired(monkeypatch, tmp_path):
    # Too-inclusive only degrades routing; too-exclusive strands a live agent's
    # work. An unparseable shard must therefore stay in the roster.
    root = _mkworld(tmp_path, rows={"alpha": LIVE})
    (root / "world" / "team-state" / "agents" / "weird.yaml").write_text(
        "\tthis: [is not: valid yaml\n", encoding="utf-8")
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha", "weird")


# --- Cache must see a CONTENT-only retirement ------------------------------

def test_cache_invalidates_when_a_shard_is_retired_in_place(monkeypatch, tmp_path):
    """The cache token folds in row-file mtimes, not just the rows-dir mtime.

    Retiring an agent rewrites an EXISTING shard; a content write does not bump
    the parent dir's mtime. Measured on the live repo 2026-07-28: rows dir mtime
    14:55 while the retirement write landed 17:09. With a dir-mtime-only token a
    long-lived daemon keeps serving the retired agent forever — which would
    leave the fix inert in the daemon, its main consumer.
    """
    root = _mkworld(tmp_path, rows={"alpha": LIVE, "ghost": LIVE})
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha", "ghost")  # populates cache

    shard = root / "world" / "team-state" / "agents" / "ghost.yaml"
    dir_mtime_before = (root / "world" / "team-state" / "agents").stat().st_mtime
    shard.write_text(yaml.safe_dump(RETIRED), encoding="utf-8")
    # Force a strictly-newer file mtime, and pin the DIR mtime back to its
    # original value so the test cannot pass via a dir-mtime bump.
    os.utime(shard, (dir_mtime_before + 10, dir_mtime_before + 10))
    os.utime(root / "world" / "team-state" / "agents",
             (dir_mtime_before, dir_mtime_before))

    assert _agents.get_active_agents() == ("alpha",)


def test_cache_still_returns_without_rereading_when_nothing_changed(
        monkeypatch, tmp_path):
    root = _mkworld(tmp_path, rows={"alpha": LIVE})
    _bind(monkeypatch, root)
    first = _agents.get_active_agents()
    assert _agents.get_active_agents() == first == ("alpha",)


# --- SSOT: the merge is imported, not re-derived ---------------------------

def test_merge_is_the_team_state_ssot():
    from _team_state import compose_agent_status
    assert _agents._load_compose() is compose_agent_status


# --- The fallback-resurrection regression (fresh-eyes-code, 2026-07-28) ----

def test_all_retired_roster_does_not_fall_through_to_discovery(
        monkeypatch, tmp_path):
    """An all-retired roster must resolve to EMPTY, not to the fallback chain.

    The first cut of this fix returned () for an all-retired team-state, and
    `_from_team_state(root) or _from_discovery(root) or _from_env()` then
    re-admitted every retired agent — because retirement is a TOMBSTONE and the
    agent's dir plus its local-paths.conf survive it. The filter was undone in
    exactly the case it mattered most. Discovery AND the env fallback are both
    seeded below so this test fails loudly if the chain ever re-fires.
    """
    root = _mkworld(tmp_path, rows={"ghost": RETIRED})
    (root / "agents" / "ghost").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "ghost" / "local-paths.conf").write_text(
        "WORLD_PATH=/nowhere\n", encoding="utf-8")
    monkeypatch.setenv("MIND_AGENT", "ghost")
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ()


def test_unpopulated_team_state_still_falls_through_to_discovery(
        monkeypatch, tmp_path):
    """The other half of the None/empty split: fresh installs must still work.

    team-state.yaml exists but holds no agents at all — that is a fresh install
    before the first /start, and discovery is the correct answer. Distinguishing
    it from all-retired is the whole point of the None return.
    """
    root = _mkworld(tmp_path)
    (root / "agents" / "newbie").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "newbie" / "local-paths.conf").write_text(
        "WORLD_PATH=/nowhere\n", encoding="utf-8")
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("newbie",)


# --- Fidelity to compose_agent_status (fresh-eyes-code, 2026-07-28) --------
# The first cut made the ROW win unconditionally while its comment claimed
# compose's whole-row newest-wins rule. Measured divergence in BOTH stale-core
# directions. These pin the roster to compose itself, so the claim cannot drift
# from the code again.

STALE_ROW_RETIRED = {"retired": True, "retired_at": "2026-07-14T09:00:00",
                     "last_active": "2026-07-10T00:00:00"}
NEWER_CORE_RETIRED = {"retired": True, "retired_at": "2026-07-25T00:00:00",
                      "last_active": "2026-07-24T00:00:00"}


def test_matches_compose_when_core_is_newer_and_live(monkeypatch, tmp_path):
    from _team_state import compose_agent_status
    core, rows = {"x": LIVE}, {"x": STALE_ROW_RETIRED}
    root = _mkworld(tmp_path, core_status=core, rows=rows)
    _bind(monkeypatch, root)
    expected = tuple(sorted(compose_agent_status(core, rows)))
    assert _agents.get_active_agents() == expected == ("x",)


def test_matches_compose_when_core_is_newer_and_retired(monkeypatch, tmp_path):
    from _team_state import compose_agent_status
    core, rows = {"x": NEWER_CORE_RETIRED}, {"x": {"last_active":
                                                   "2026-07-10T00:00:00"}}
    root = _mkworld(tmp_path, core_status=core, rows=rows)
    _bind(monkeypatch, root)
    expected = tuple(sorted(compose_agent_status(core, rows)))
    assert _agents.get_active_agents() == expected == ()


# --- The OTHER corrupt-shard flavor ---------------------------------------

def test_non_mapping_shards_read_as_not_retired(monkeypatch, tmp_path):
    """Valid YAML that is not a mapping takes a DIFFERENT path than a parse
    error — past the inner except, straight into the merge. It is safe only
    because _team_state._is_retired isinstance-guards its argument. Pinned here
    so that guard cannot be dropped silently."""
    root = _mkworld(tmp_path, rows={"alpha": LIVE})
    rows_dir = root / "world" / "team-state" / "agents"
    (rows_dir / "scalar.yaml").write_text("a bare string\n", encoding="utf-8")
    (rows_dir / "listy.yaml").write_text("- a\n- b\n", encoding="utf-8")
    (rows_dir / "empty.yaml").write_text("", encoding="utf-8")
    _bind(monkeypatch, root)
    assert _agents.get_active_agents() == ("alpha", "empty", "listy", "scalar")
