"""test_partner_uncommitted_age_cutoff.py —  regression.

Verifies the age-cutoff staleness filter on partner uncommitted-edits.jsonl
entries in _cross_agent_attribution_filter.py (and, by SSOT import, the
iteration-commit.sh partner-uncommitted-log heredoc — rb-1405).

Canonical incident (msg-1904, commit 33cad03d): charlie's uncommitted-edits.jsonl
held a 3-week-stale entry for `.claude/settings.json` (edit_ts 2026-05-20). Both
consumers of that log (iteration-commit.sh and filter_paths via
_read_uncommitted_log) built their partner-uncommitted path set from EVERY entry
with no age check, so the stale charlie entry dropped alpha's *legitimate
same-session* edit to settings.json. The committer had to re-commit the file via
an explicit pathspec to route around the false drop.

The fix: an entry whose edit_ts/mtime proves it older than PARTNER_UNCOMMITTED_
MAX_AGE_HOURS (default 48h) is STALE and excluded from the partner path set —
it can no longer suppress the committer's own edit. The cutoff ONLY eliminates
proven-stale false drops; an entry with a missing/unparseable timestamp keeps the
pre-g-115-752 behavior (treated as fresh → still drops), preserving the module's
standing bias: never silently drop self-authored work, but never *widen* a true
partner drop either.

Layers:
  - _entry_is_stale unit tests: deterministic (now_epoch + max_age passed
    explicitly) — edit_ts stale/fresh, mtime fallback, edit_ts precedence,
    bool-mtime guard, missing/unparseable timestamp, disabled cutoff.
  - _max_age_sec unit tests: default, env override, invalid fallback, zero.
  - _read_uncommitted_log integration: a stale partner entry is excluded; a
    fresh one is retained.
  - filter_paths integration: the canonical settings.json incident — a stale
    partner-log entry no longer drops the committer's candidate; a fresh one
    still drops it (no regression to the g-115-697 attribution behavior).

Run: py -3 -m pytest core/scripts/tests/test_partner_uncommitted_age_cutoff.py -v
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _cross_agent_attribution_filter import (  # noqa: E402
    _entry_is_stale,
    _iso_to_epoch,
    _max_age_sec,
    _read_uncommitted_log,
    filter_paths,
)

CUTOFF = 48 * 3600  # default age cutoff in seconds


# ── _entry_is_stale unit tests (deterministic — explicit now_epoch/max_age) ──


def test_entry_stale_mtime_just_past_cutoff():
    # mtime older than the cutoff by 1s → stale.
    assert _entry_is_stale({"mtime": 1000}, CUTOFF, 1000 + CUTOFF + 1) is True


def test_entry_fresh_mtime_just_inside_cutoff():
    # mtime younger than the cutoff by 1s → not stale.
    assert _entry_is_stale({"mtime": 1000}, CUTOFF, 1000 + CUTOFF - 1) is False


def test_entry_stale_edit_ts():
    # edit_ts 100h old (> 48h cutoff) → stale. now_epoch derived from the same
    # _iso_to_epoch parser so the test is wall-clock independent.
    iso = "2026-05-20T17:25:38"
    ep = _iso_to_epoch(iso)
    assert ep > 0
    assert _entry_is_stale({"edit_ts": iso}, CUTOFF, ep + 100 * 3600) is True


def test_entry_fresh_edit_ts():
    iso = "2026-05-20T17:25:38"
    ep = _iso_to_epoch(iso)
    assert _entry_is_stale({"edit_ts": iso}, CUTOFF, ep + 1 * 3600) is False


def test_entry_edit_ts_takes_precedence_over_mtime():
    # edit_ts is FRESH; mtime is ANCIENT. edit_ts must win → not stale.
    iso = "2026-06-10T12:00:00"
    ep = _iso_to_epoch(iso)
    assert _entry_is_stale({"edit_ts": iso, "mtime": 1}, CUTOFF, ep + 3600) is False


def test_entry_falls_back_to_mtime_when_edit_ts_null():
    # edit_ts == "null" (the sentinel the writer emits for an unset value) →
    # skip to mtime, which is ancient → stale.
    assert (
        _entry_is_stale({"edit_ts": "null", "mtime": 1000}, CUTOFF, 1000 + 200 * 3600)
        is True
    )


def test_entry_unparseable_edit_ts_no_mtime_is_not_stale():
    # Garbage edit_ts, no mtime → cannot prove staleness → preserve prior
    # behavior (treated as fresh).
    assert _entry_is_stale({"edit_ts": "not-a-timestamp"}, CUTOFF, 9_999_999_999) is False


def test_entry_missing_all_timestamps_is_not_stale():
    assert _entry_is_stale({"file": "x.py"}, CUTOFF, 9_999_999_999) is False


def test_entry_bool_mtime_is_ignored():
    # bool is a subclass of int; the guard must reject True/False as mtime.
    assert _entry_is_stale({"mtime": True}, CUTOFF, 9_999_999_999) is False
    assert _entry_is_stale({"mtime": False}, CUTOFF, 9_999_999_999) is False


def test_entry_cutoff_zero_disables():
    # max_age_sec <= 0 disables the cutoff → nothing is stale (pre-).
    assert _entry_is_stale({"mtime": 1000}, 0, 9_999_999_999) is False
    assert _entry_is_stale({"mtime": 1000}, -5, 9_999_999_999) is False


# ── _max_age_sec unit tests ──────────────────────────────────────────────────


def test_max_age_sec_default(monkeypatch):
    monkeypatch.delenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", raising=False)
    assert _max_age_sec() == 48 * 3600.0


def test_max_age_sec_env_override(monkeypatch):
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "12")
    assert _max_age_sec() == 12 * 3600.0


def test_max_age_sec_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "not-a-number")
    assert _max_age_sec() == 48 * 3600.0


def test_max_age_sec_zero_disables(monkeypatch):
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "0")
    assert _max_age_sec() == 0.0


# ── integration helpers (mirror test_attribution_filter_path_normalize.py) ───


def _stage_project(tmp_path, self_agent, partners):
    project_root = tmp_path / "repo"
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    for agent in [self_agent, *partners]:
        adir = project_root / "agents" / agent
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=" + world.as_posix() + "\n", encoding="utf-8"
        )
        (adir / "session").mkdir(parents=True, exist_ok=True)
    return project_root, world


def _write_team_state(world, agent_status):
    import yaml

    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": agent_status}), encoding="utf-8"
    )


def _write_log_entries(agent_dir, entries):
    """entries: list of full record dicts (file + edit_ts/mtime)."""
    (agent_dir / "session" / "uncommitted-edits.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries), encoding="utf-8"
    )


def _iso_ago(hours):
    """ISO local naive timestamp `hours` before now (matches the writer schema:
    no timezone suffix, second precision)."""
    return (
        datetime.datetime.now() - datetime.timedelta(hours=hours)
    ).isoformat(timespec="seconds")


# ── _read_uncommitted_log integration ────────────────────────────────────────


def test_read_log_excludes_stale_retains_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "48")
    project_root, _world = _stage_project(tmp_path, "alpha", ["charlie"])
    charlie = project_root / "agents" / "charlie"

    stale_path = "core/scripts/stale-helper.py"
    fresh_path = "core/scripts/fresh-helper.py"
    _write_log_entries(
        charlie,
        [
            {"file": stale_path, "edit_ts": _iso_ago(100)},  # > 48h → stale
            {"file": fresh_path, "edit_ts": _iso_ago(1)},    # < 48h → fresh
        ],
    )

    result = _read_uncommitted_log(charlie)
    assert fresh_path in result, f"fresh entry must be retained; got {result}"
    assert stale_path not in result, f"stale entry must be excluded; got {result}"


def test_read_log_retains_entry_without_timestamp(tmp_path, monkeypatch):
    # Legacy entry with no edit_ts/mtime → cannot prove stale → retained
    # (preserves pre- behavior for malformed/legacy records).
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "48")
    project_root, _world = _stage_project(tmp_path, "alpha", ["charlie"])
    charlie = project_root / "agents" / "charlie"

    legacy_path = "core/config/legacy.yaml"
    _write_log_entries(charlie, [{"file": legacy_path}])

    assert legacy_path in _read_uncommitted_log(charlie)


# ── filter_paths integration: the canonical settings.json incident ───────────


def test_stale_partner_entry_does_not_drop_committer_edit(tmp_path, monkeypatch):
    """ core fix: a 3-week-stale charlie entry for settings.json must NOT
    drop alpha's legitimate same-session candidate. Mirrors msg-1904: charlie's
    log stored an ABSOLUTE path (legacy writer), alpha's candidate is RELATIVE —
    so this also exercises the g-115-1180 normalization composing with the new
    staleness filter."""
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "48")
    project_root, world = _stage_project(tmp_path, "alpha", ["charlie"])
    _write_team_state(world, {"alpha": {"in_flight": None}})

    rel = ".claude/settings.json"
    absolute = (project_root / rel).as_posix()  # legacy absolute log form
    _write_log_entries(
        project_root / "agents" / "charlie",
        [{"file": absolute, "edit_ts": _iso_ago(100)}],  # stale
    )

    kept, dropped = filter_paths([rel], "alpha", str(project_root), str(world))

    assert rel in kept, (
        f"stale partner entry must NOT suppress the committer's edit; "
        f"got kept={kept}, dropped={dropped}"
    )
    assert dropped == []


def test_fresh_partner_entry_still_drops_candidate(tmp_path, monkeypatch):
    """No-regression: a FRESH partner entry still drops the candidate as
    partner-uncommitted-log (the g-115-697 attribution behavior is preserved for
    genuine between-claim partner work)."""
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "48")
    project_root, world = _stage_project(tmp_path, "alpha", ["charlie"])
    _write_team_state(world, {"alpha": {"in_flight": None}})

    rel = ".claude/settings.json"
    absolute = (project_root / rel).as_posix()
    _write_log_entries(
        project_root / "agents" / "charlie",
        [{"file": absolute, "edit_ts": _iso_ago(1)}],  # fresh
    )

    kept, dropped = filter_paths([rel], "alpha", str(project_root), str(world))

    assert kept == [], f"fresh partner entry must still drop the candidate; got {kept}"
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "partner-uncommitted-log"
    assert dropped[0]["owner"] == "charlie"
    assert dropped[0]["path"] == rel


def test_cutoff_disabled_restores_pre_fix_drop(tmp_path, monkeypatch):
    """With the cutoff disabled (PARTNER_UNCOMMITTED_MAX_AGE_HOURS=0), even a
    stale entry drops the candidate — the exact pre-g-115-752 behavior, proving
    the env knob fully reverts the change."""
    monkeypatch.setenv("PARTNER_UNCOMMITTED_MAX_AGE_HOURS", "0")
    project_root, world = _stage_project(tmp_path, "alpha", ["charlie"])
    _write_team_state(world, {"alpha": {"in_flight": None}})

    rel = ".claude/settings.json"
    absolute = (project_root / rel).as_posix()
    _write_log_entries(
        project_root / "agents" / "charlie",
        [{"file": absolute, "edit_ts": _iso_ago(100)}],  # stale, but cutoff disabled
    )

    kept, dropped = filter_paths([rel], "alpha", str(project_root), str(world))

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "partner-uncommitted-log"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
