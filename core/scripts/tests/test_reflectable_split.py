#!/usr/bin/env python3
"""Pins for the reflectable-vs-backlog split ().

g-115-5358 widened --unreflected to the full never-reflected backlog
(live+archive union, stage resolved/archived). That number is dominated by
records nothing can ever reflect on (UNRESOLVABLE / EXPIRED / no-outcome --
g-115-4558; measured 2026-08-14: 383 total, 5 reflectable), so every consumer
gating ACTION on it broke silently: consolidation-precheck's lean fast path
went structurally dead (data_total nonzero forever), quiescence drain
targeting could select an all-unreflectable target, and the iteration-close
reflect nudge fired on every close. The fix routes action-gating consumers
through core/scripts/_reflectable.py (outcome in CONFIRMED/CORRECTED).

Pinned here:
  1. _reflectable unit behavior -- the SSOT predicate every consumer imports.
  2. consolidation-precheck.py end-to-end on a hermetic tmp world
     (MIND_WORLD + MIND_AGENT_DIR overrides, STORAGE_BACKEND=local per
     guard-955): live+archive union, dedup by id with the LIVE copy winning,
     stage filter, reflectable filter, and the revived lean fast path
     (verdict FAST on a world whose backlog is entirely unreflectable --
     the exact state the pre-fix code could never reach).

The mixed fixture DISCRIMINATES old vs new behavior by construction: the
pre-fix hand-rolled count (live-only, stage=="resolved", not reflected, no
outcome filter) yields 3 on it (hyp-a, hyp-b, hyp-j); the fixed count yields
4 (hyp-a, hyp-e, hyp-f, hyp-j). A regression to either the old predicate or
a broken union direction cannot reproduce 4.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
SCRIPTS = _TESTS_DIR.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
PROJECT_ROOT = SCRIPTS.parent.parent
PRECHECK = SCRIPTS / "consolidation-precheck.py"

import _reflectable  # noqa: E402


# ── 1. SSOT predicate pins ──────────────────────────────────────────────

def test_is_reflectable_confirmed_and_corrected():
    assert _reflectable.is_reflectable({"outcome": "CONFIRMED"})
    assert _reflectable.is_reflectable({"outcome": "CORRECTED"})


def test_is_reflectable_case_insensitive():
    assert _reflectable.is_reflectable({"outcome": "corrected"})
    assert _reflectable.is_reflectable({"outcome": "Confirmed"})


def test_is_reflectable_rejects_unreflectable_outcomes():
    assert not _reflectable.is_reflectable({"outcome": "UNRESOLVABLE"})
    assert not _reflectable.is_reflectable({"outcome": "EXPIRED"})
    assert not _reflectable.is_reflectable({"outcome": None})
    assert not _reflectable.is_reflectable({})


def test_is_reflectable_rejects_non_dict():
    assert not _reflectable.is_reflectable("CONFIRMED")
    assert not _reflectable.is_reflectable(None)
    assert not _reflectable.is_reflectable(["CONFIRMED"])


def test_count_reflectable_sums_and_tolerates_junk():
    records = [
        {"outcome": "CONFIRMED"},
        {"outcome": "EXPIRED"},
        "junk",
        {"outcome": "corrected"},
        None,
    ]
    assert _reflectable.count_reflectable(records) == 2
    assert _reflectable.count_reflectable("not-a-list") == 0
    assert _reflectable.count_reflectable(None) == 0
    assert _reflectable.count_reflectable([]) == 0


# ── 2. consolidation-precheck end-to-end ────────────────────────────────

def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _run_precheck(world_dir: Path, agent_dir: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "MIND_WORLD": str(world_dir),
        "MIND_AGENT_DIR": str(agent_dir),  # documented test override (_paths.py:324)
        "MIND_AGENT": "testagent",
        "STORAGE_BACKEND": "local",  # guard-955: never let a test touch own-cloud
    })
    # Force the agent-wide WM fallback path (absent in the tmp agent dir) so
    # every non-pipeline counter is deterministically 0.
    for key in ("BODY_WM_PATH", "MIND_SID", "BODY_ROLE"):
        env.pop(key, None)
    r = subprocess.run(
        [sys.executable, str(PRECHECK)],
        capture_output=True, text=True, env=env,
        cwd=str(PROJECT_ROOT), timeout=30,
    )
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    out = json.loads(r.stdout)
    assert "error" not in out, out
    return out


def test_precheck_counts_reflectable_over_union(tmp_path):
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    live = [
        # counted by old AND new predicates (baseline)
        {"id": "hyp-a", "stage": "resolved", "outcome": "CONFIRMED"},
        # old counts it (resolved, unreflected); new excludes: UNRESOLVABLE
        {"id": "hyp-b", "stage": "resolved", "outcome": "UNRESOLVABLE"},
        # excluded by both: already reflected
        {"id": "hyp-c", "stage": "resolved", "outcome": "CONFIRMED", "reflected": True},
        # excluded by both: not resolved, no outcome
        {"id": "hyp-d", "stage": "active"},
        # dedup-direction pin: live copy unreflected, archive copy reflected.
        # LIVE must win (endpoint parity) -> counted exactly once. A broken
        # union direction excludes it; a missing dedup counts it twice.
        {"id": "hyp-e", "stage": "archived", "outcome": "CORRECTED"},
        # stage-filter pin: reflectable outcome on an un-resolved stage
        {"id": "hyp-h", "stage": "discovered", "outcome": "CONFIRMED"},
        # id-less record: dropped by the union (endpoint parity)
        {"stage": "resolved", "outcome": "CONFIRMED"},
        # case-insensitivity travels through the subprocess too
        {"id": "hyp-j", "stage": "resolved", "outcome": "corrected"},
    ]
    archive = [
        {"id": "hyp-e", "stage": "archived", "outcome": "CORRECTED", "reflected": True},
        # archive-visibility pin: old live-only code was blind to this one
        {"id": "hyp-f", "stage": "archived", "outcome": "CORRECTED"},
        {"id": "hyp-g", "stage": "archived", "outcome": "EXPIRED"},
    ]
    _write_jsonl(world / "pipeline.jsonl", live)
    _write_jsonl(world / "pipeline-archive.jsonl", archive)

    out = _run_precheck(world, agent)
    # a, e (live wins), f (archive), j (lowercase) -- see module docstring for
    # why 4 discriminates the old predicate (3) and both union failure modes.
    assert out["unreflected"] == 4, out
    assert out["total"] == 4, out
    assert out["verdict"] == "FULL", out


def test_precheck_lean_path_reachable_on_unreflectable_backlog(tmp_path):
    """The defect this goal exists to fix: a backlog of structurally
    unreflectable records must NOT hold the verdict at FULL forever."""
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    live = [
        {"id": "hyp-1", "stage": "resolved", "outcome": "UNRESOLVABLE"},
        {"id": "hyp-2", "stage": "resolved", "outcome": "EXPIRED"},
        {"id": "hyp-3", "stage": "resolved"},  # no outcome
    ]
    archive = [
        {"id": "hyp-4", "stage": "archived", "outcome": "EXPIRED"},
    ]
    _write_jsonl(world / "pipeline.jsonl", live)
    _write_jsonl(world / "pipeline-archive.jsonl", archive)

    out = _run_precheck(world, agent)
    assert out["unreflected"] == 0, out
    assert out["total"] == 0, out
    assert out["verdict"] == "FAST", out


def test_precheck_missing_stores_yield_zero(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    agent = tmp_path / "agent"
    agent.mkdir()
    out = _run_precheck(world, agent)
    assert out["unreflected"] == 0, out
    assert out["verdict"] == "FAST", out
