"""test_cells_archive.py --  (BRD Gap 17, child 1/4 of ).

Pins the Go-Explore cell archive substrate (core/scripts/cells-archive.py): the
replace-on-strictly-better write rule, the K^D=256 per-category cap with
weakest-incumbent eviction, visits accounting, per-category isolation, the
filesystem-safe category stem, and load/get round-trips.

MIND-INTERNAL: the archive stores the framework's OWN goal-selection cells
(state_signature + trajectory + score), NOT any NPC/product artifact.

Daemon-safe (no daemon_integration marker): every test passes an explicit
cells_dir=tmp_path, so the archive never touches the real agents/<agent>/cells/
tree and no daemon is spawned. Pure file-ops on a tmp directory.

Cross-references:
  - Go-Explore 1901.10995 -- the cell-archive + return-to-promising-cell algorithm
  - g-306-13 (Gap 2) -- the K=D=4 contract that fixes the per-category cap at 4**4=256
  - g-306-48 (child 2/4) -- KG-similarity cell matching that consumes this store
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# Hyphen-named script -> importlib load (the test_knowledge_graph_ppr pattern).
_CELLS_PATH = CORE_SCRIPTS / "cells-archive.py"
_spec = importlib.util.spec_from_file_location("cells_archive", _CELLS_PATH)
cells = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cells)


# --- helpers ----------------------------------------------------------------

def _upsert(tmp, cell_id, category, score, trajectory=None, sig=None, now="2026-06-21T00:00:00"):
    return cells.upsert_cell(
        cell_id, category,
        state_signature=(sig if sig is not None else f"sig-{cell_id}"),
        trajectory=(trajectory if trajectory is not None else [cell_id]),
        score=score, cells_dir=tmp, now=now,
    )


# --- cap constant -----------------------------------------------------------

def test_cap_is_k_to_the_d_256():
    assert cells.CELL_CAP_K == 4
    assert cells.CELL_CAP_D == 4
    assert cells.CELL_CAP == 256


# --- basic add / round-trip -------------------------------------------------

def test_new_cell_added(tmp_path):
    r = _upsert(tmp_path, "c1", "planning", 3.0, trajectory=["g-1", "g-2"])
    assert r["verdict"] == "added"
    assert r["evicted"] is None
    assert r["size"] == 1
    assert r["cap"] == 256

    rec = cells.get_cell("c1", "planning", cells_dir=tmp_path)
    assert rec is not None
    assert rec["cell_id"] == "c1"
    assert rec["category"] == "planning"
    assert rec["trajectory"] == ["g-1", "g-2"]
    assert rec["score"] == 3.0
    assert rec["visits"] == 1
    assert rec["created_at"] == "2026-06-21T00:00:00"
    assert rec["updated_at"] == "2026-06-21T00:00:00"


def test_get_missing_returns_none(tmp_path):
    assert cells.get_cell("nope", "planning", cells_dir=tmp_path) is None


def test_load_missing_category_returns_empty(tmp_path):
    assert cells.load_category("never-written", cells_dir=tmp_path) == {}


# --- replace-on-strictly-better ---------------------------------------------

def test_strictly_better_replaces_trajectory(tmp_path):
    _upsert(tmp_path, "c1", "planning", 3.0, trajectory=["slow"], now="2026-06-21T00:00:00")
    r = _upsert(tmp_path, "c1", "planning", 5.0, trajectory=["fast"],
                sig="better", now="2026-06-21T01:00:00")
    assert r["verdict"] == "replaced"
    rec = cells.get_cell("c1", "planning", cells_dir=tmp_path)
    assert rec["score"] == 5.0
    assert rec["trajectory"] == ["fast"]
    assert rec["state_signature"] == "better"
    # visits bumped, created_at preserved, updated_at advanced
    assert rec["visits"] == 2
    assert rec["created_at"] == "2026-06-21T00:00:00"
    assert rec["updated_at"] == "2026-06-21T01:00:00"


def test_equal_score_keeps_not_replace(tmp_path):
    _upsert(tmp_path, "c1", "planning", 3.0, trajectory=["orig"])
    r = _upsert(tmp_path, "c1", "planning", 3.0, trajectory=["equal"], now="2026-06-21T02:00:00")
    assert r["verdict"] == "kept"
    rec = cells.get_cell("c1", "planning", cells_dir=tmp_path)
    # equal score does NOT replace the stored trajectory
    assert rec["trajectory"] == ["orig"]
    assert rec["score"] == 3.0
    # but the return is still recorded
    assert rec["visits"] == 2
    assert rec["updated_at"] == "2026-06-21T02:00:00"


def test_worse_score_keeps_not_replace(tmp_path):
    _upsert(tmp_path, "c1", "planning", 5.0, trajectory=["best"])
    r = _upsert(tmp_path, "c1", "planning", 1.0, trajectory=["worse"])
    assert r["verdict"] == "kept"
    rec = cells.get_cell("c1", "planning", cells_dir=tmp_path)
    assert rec["trajectory"] == ["best"]
    assert rec["score"] == 5.0
    assert rec["visits"] == 2


def test_visits_increment_on_repeated_upsert(tmp_path):
    for i in range(4):
        _upsert(tmp_path, "c1", "planning", 1.0)
    rec = cells.get_cell("c1", "planning", cells_dir=tmp_path)
    assert rec["visits"] == 4


# --- K^D cap behavior -------------------------------------------------------

def _fill_to_cap(tmp_path, category="planning"):
    # Fill with scores 0..255 so cell-i has score i (c000 is the weakest).
    for i in range(cells.CELL_CAP):
        _upsert(tmp_path, f"c{i:03d}", category, float(i), trajectory=[f"t{i}"])


def test_cap_full_at_256(tmp_path):
    _fill_to_cap(tmp_path)
    stats = cells.category_stats("planning", cells_dir=tmp_path)
    assert stats["count"] == 256
    assert stats["cap"] == 256
    assert stats["min_score"] == 0.0
    assert stats["max_score"] == 255.0


def test_better_new_cell_evicts_weakest_at_cap(tmp_path):
    _fill_to_cap(tmp_path)
    # New cell scoring 1000 > weakest (c000 @ 0.0): admit + evict c000.
    r = _upsert(tmp_path, "cNEW", "planning", 1000.0, trajectory=["new"])
    assert r["verdict"] == "evicted-and-added"
    assert r["evicted"] == "c000"
    assert r["size"] == 256  # still capped
    assert cells.get_cell("c000", "planning", cells_dir=tmp_path) is None
    assert cells.get_cell("cNEW", "planning", cells_dir=tmp_path) is not None
    stats = cells.category_stats("planning", cells_dir=tmp_path)
    assert stats["count"] == 256
    assert stats["max_score"] == 1000.0
    assert stats["min_score"] == 1.0  # c001 is now the weakest


def test_worse_new_cell_rejected_at_cap(tmp_path):
    _fill_to_cap(tmp_path)
    # New cell scoring -5 < weakest (0.0): rejected, archive unchanged.
    r = _upsert(tmp_path, "cBAD", "planning", -5.0, trajectory=["bad"])
    assert r["verdict"] == "rejected-full"
    assert r["evicted"] is None
    assert r["size"] == 256
    assert cells.get_cell("cBAD", "planning", cells_dir=tmp_path) is None
    # weakest incumbent still present
    assert cells.get_cell("c000", "planning", cells_dir=tmp_path) is not None


def test_equal_to_weakest_new_cell_rejected_at_cap(tmp_path):
    # Strictly-better is required for admission: equal to the weakest is rejected.
    _fill_to_cap(tmp_path)
    r = _upsert(tmp_path, "cEQ", "planning", 0.0, trajectory=["eq"])
    assert r["verdict"] == "rejected-full"
    assert cells.get_cell("cEQ", "planning", cells_dir=tmp_path) is None


def test_existing_cell_update_at_cap_does_not_evict(tmp_path):
    # Upserting an EXISTING cell at full cap is a replace/keep, never an eviction.
    _fill_to_cap(tmp_path)
    r = _upsert(tmp_path, "c100", "planning", 9999.0, trajectory=["boost"])
    assert r["verdict"] == "replaced"
    assert r["evicted"] is None
    assert r["size"] == 256
    assert cells.category_stats("planning", cells_dir=tmp_path)["count"] == 256


# --- per-category isolation -------------------------------------------------

def test_per_category_isolation(tmp_path):
    _upsert(tmp_path, "c1", "planning", 1.0)
    _upsert(tmp_path, "c1", "retrieval", 2.0)  # same id, different category
    plan = cells.get_cell("c1", "planning", cells_dir=tmp_path)
    retr = cells.get_cell("c1", "retrieval", cells_dir=tmp_path)
    assert plan["score"] == 1.0
    assert retr["score"] == 2.0
    cats = cells.list_categories(cells_dir=tmp_path)
    assert "planning" in cats
    assert "retrieval" in cats


def test_cap_is_per_category(tmp_path):
    # Filling one category to cap leaves another category free.
    _fill_to_cap(tmp_path, "planning")
    r = _upsert(tmp_path, "x1", "retrieval", 0.5)
    assert r["verdict"] == "added"
    assert cells.category_stats("retrieval", cells_dir=tmp_path)["count"] == 1
    assert cells.category_stats("planning", cells_dir=tmp_path)["count"] == 256


# --- safe category stem -----------------------------------------------------

def test_safe_category_normalizes_filename(tmp_path):
    _upsert(tmp_path, "c1", "Planning/Sub Step!", 1.0)
    # "Planning/Sub Step!" -> lowercased, '/'->'-', non-[a-z0-9_-] -> '_'
    files = sorted(p.name for p in tmp_path.glob("*.yaml"))
    assert files == ["planning-sub_step_.yaml"]
    # round-trips through the same normalization
    assert cells.get_cell("c1", "Planning/Sub Step!", cells_dir=tmp_path) is not None


def test_empty_category_falls_back_to_uncategorized(tmp_path):
    _upsert(tmp_path, "c1", "   ", 1.0)
    assert (tmp_path / "uncategorized.yaml").exists()


# --- stats / categories on empty store --------------------------------------

def test_stats_empty_category(tmp_path):
    s = cells.category_stats("nothing", cells_dir=tmp_path)
    assert s["count"] == 0
    assert s["min_score"] is None
    assert s["max_score"] is None
    assert s["total_visits"] == 0


def test_list_categories_empty(tmp_path):
    assert cells.list_categories(cells_dir=tmp_path) == []


def test_total_visits_aggregates(tmp_path):
    _upsert(tmp_path, "c1", "planning", 1.0)
    _upsert(tmp_path, "c1", "planning", 1.0)  # visits -> 2
    _upsert(tmp_path, "c2", "planning", 1.0)  # visits -> 1
    s = cells.category_stats("planning", cells_dir=tmp_path)
    assert s["count"] == 2
    assert s["total_visits"] == 3
