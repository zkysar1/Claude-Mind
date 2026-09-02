"""Seed-file integrity for world/agent aspiration bootstraps ().

A corrupt or drifted seed silently breaks EVERY future world init, and until
this file existed NO test read the seed files at all (re-verified 2026-08-31:
0 hits for the seed filenames across core/scripts/tests, core/tests,
mind_api/tests, against a positive control of 1141 test files present).

Deliberately in core/scripts/tests rather than as a /verify-learning check so
it runs on every deep closure via run-full-suite.sh, not only when a user
invokes verify-learning.
"""
import json
import pathlib

import pytest

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config"
SEEDS = ("world-aspirations-initial.jsonl", "agent-aspirations-initial.jsonl")

# Fields every seeded goal must carry for a world init to produce a usable
# queue. Named explicitly (not derived from the current files) so that DROPPING
# a field from the seed is what fails, rather than silently redefining the
# contract to whatever the seed happens to contain today.
REQUIRED_GOAL_FIELDS = (
    "id", "title", "description", "status",
    "participants", "verification", "priority",
)


def _load(name):
    path = CONFIG / name
    assert path.is_file(), f"seed file missing: {path}"
    raw = path.read_text(encoding="utf-8")
    # Byte count beside the record count: an empty parse and an unreadable
    # file are otherwise textually identical (guard-2298).
    assert raw.strip(), f"seed file is empty: {path} ({len(raw)} bytes)"
    records = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append((lineno, json.loads(line)))
        except json.JSONDecodeError as exc:
            pytest.fail(f"{name}:{lineno} is not valid JSON: {exc}")
    assert records, f"{name} parsed to zero records ({len(raw)} bytes read)"
    return records


@pytest.mark.parametrize("name", SEEDS)
def test_seed_lines_are_valid_json(name):
    recs = _load(name)
    assert all(isinstance(d, dict) for _, d in recs)


@pytest.mark.parametrize("name", SEEDS)
def test_every_seeded_goal_carries_required_fields(name):
    missing = []
    for lineno, asp in _load(name):
        for goal in asp.get("goals") or []:
            absent = [f for f in REQUIRED_GOAL_FIELDS if f not in goal]
            if absent:
                missing.append(
                    f"{name}:{lineno} {asp.get('id')}/{goal.get('id')} missing {absent}"
                )
    assert not missing, "seeded goals missing required fields:\n" + "\n".join(missing)


@pytest.mark.parametrize("name", SEEDS)
def test_recurring_goals_carry_interval_hours(name):
    bad = []
    for lineno, asp in _load(name):
        for goal in asp.get("goals") or []:
            if goal.get("recurring") is True and not goal.get("interval_hours"):
                bad.append(f"{name}:{lineno} {goal.get('id')}")
    assert not bad, "recurring seeded goals without interval_hours: " + ", ".join(bad)


@pytest.mark.parametrize("name", SEEDS)
def test_aspiration_ids_are_unique(name):
    seen = {}
    for lineno, asp in _load(name):
        aid = asp.get("id")
        assert aid, f"{name}:{lineno} aspiration has no id"
        assert aid not in seen, (
            f"{name}: duplicate aspiration id {aid} at lines {seen[aid]} and {lineno}"
        )
        seen[aid] = lineno


@pytest.mark.parametrize("name", SEEDS)
def test_goal_ids_are_unique_within_file(name):
    seen = {}
    for lineno, asp in _load(name):
        for goal in asp.get("goals") or []:
            gid = goal.get("id")
            assert gid, f"{name}:{lineno} goal in {asp.get('id')} has no id"
            assert gid not in seen, (
                f"{name}: duplicate goal id {gid} (lines {seen[gid]}, {lineno})"
            )
            seen[gid] = lineno
