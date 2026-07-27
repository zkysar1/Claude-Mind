"""test_fixture_leak_scan.py -- regression for the guard-955 fixture-leak
detective scanner (g-115-2055).

Bug shape: a pytest suite run on an own-cloud box whose conftest
STORAGE_BACKEND=local pin is absent/stale mints the world-writing fixtures of
test_asp_id_auto_allocation.py (asp-338..343, title "auto-minted"/"parallel
filer N", motivation "g-328-29 fixture") AGAINST the PRODUCTION aspirations store
-- the guard-955 S3-key-collision class (canonical live incident: g-115-2054,
2026-07-12). guard-955 + the g-115-1875 conftest autouse pin are the
authoring-time gate; fixture-leak-scan.py is the missing DETECTIVE layer that
surfaces fixtures which ALREADY leaked.

These tests pin: (1) a seeded fixture (asp + nested goals) is detected, (2) a
clean store returns 0 (no false positive on a real aspiration), (3) the
"parallel filer N" regex is anchored (does not match "parallel processing..."),
(4) the archive file is scanned too, (5) --exit-on-hits exit codes (advisory=0,
enforced=1).

STORAGE_BACKEND=local is load-bearing: the scanner's _snapshot force-refreshes
through the backend, and OwnCloudBackend derives the S3 key from the BASENAME
(aspirations.jsonl) -- so under own-cloud a refresh of the tmp seed file would
pull the PRODUCTION store over it (guard-955). The conftest autouse pin
(g-115-1875) sets local for pytest; the __main__ block pins it for direct runs.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_scanner():
    """Load fixture-leak-scan.py (hyphenated name -> importlib)."""
    spec = importlib.util.spec_from_file_location(
        "fixture_leak_scan", CORE_SCRIPTS / "fixture-leak-scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")


# Verbatim shape of a leaked test_asp_id_auto_allocation.py fixture (the
#  incident record).
FIXTURE_ASP = {
    "id": "asp-338", "title": "auto-minted", "motivation": "g-328-29 fixture",
    "status": "active", "goals": [
        {"id": "g-338-01", "title": "first goal", "description": "auto-minted goal"},
        {"id": "g-338-02", "title": "second goal", "description": "auto-minted goal"},
    ],
}
# A genuine production aspiration -- must NEVER be flagged.
CLEAN_ASP = {
    "id": "asp-115", "title": "Framework robustness + hygiene",
    "motivation": "Keep the Mind framework correct and self-improving",
    "status": "active", "goals": [
        {"id": "g-115-2055", "title": "Idea: fixture-title tripwire scan",
         "description": "Build a detective layer for the guard-955 leak class."},
    ],
}


def _seed_world(tmp_path: Path, *, live=None, archive=None, pipeline=None) -> Path:
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    _write_jsonl(world / "aspirations.jsonl", live or [])
    _write_jsonl(world / "aspirations-archive.jsonl", archive or [])
    _write_jsonl(world / "pipeline.jsonl", pipeline or [])
    return world


def test_detects_seeded_fixture(tmp_path):
    scanner = _load_scanner()
    world = _seed_world(tmp_path, live=[CLEAN_ASP, FIXTURE_ASP])
    suspects = scanner.scan(world_dir=str(world))
    ids = {s["record_id"] for s in suspects}
    # The fixture aspiration is flagged on both title + motivation.
    assert "asp-338" in ids
    assert any(s["record_id"] == "asp-338" and s["matched"] == "g-328-29 fixture"
               for s in suspects), suspects
    # Nested goals are flagged too.
    assert "asp-338/g-338-01" in ids
    assert "asp-338/g-338-02" in ids
    # The clean aspiration + goal are NOT flagged (no false positive).
    assert not any(s["record_id"].startswith("asp-115") for s in suspects), \
        [s for s in suspects if s["record_id"].startswith("asp-115")]


def test_clean_store_returns_zero(tmp_path):
    scanner = _load_scanner()
    world = _seed_world(tmp_path, live=[CLEAN_ASP])
    assert scanner.scan(world_dir=str(world)) == []


def test_parallel_filer_regex_is_anchored(tmp_path):
    scanner = _load_scanner()
    world = _seed_world(tmp_path, live=[
        {"id": "asp-340", "title": "parallel filer 0",
         "motivation": "g-328-29 fixture", "status": "active", "goals": []},
        {"id": "asp-999", "title": "parallel processing pipeline design",
         "motivation": "real backend work", "status": "active", "goals": []},
    ])
    ids = {s["record_id"] for s in scanner.scan(world_dir=str(world))}
    assert "asp-340" in ids           # matches /^parallel filer \d+$/
    assert "asp-999" not in ids       # anchored regex must NOT match a real title


def test_archive_is_scanned(tmp_path):
    scanner = _load_scanner()
    world = _seed_world(tmp_path, live=[CLEAN_ASP], archive=[FIXTURE_ASP])
    suspects = scanner.scan(world_dir=str(world))
    assert any(s["store"].endswith("aspirations-archive.jsonl")
               for s in suspects), suspects


def test_retired_and_archived_tombstones_are_skipped(tmp_path):
    """: a retired/archived aspiration is a GOVERNED tombstone -- a
    leak already handled via aspirations status (the g-328-29 asp-338..349 batch
    retired 2026-07-14). Re-flagging it every 24h run is pure re-triage noise.
    A FRESH leak never carries retired/archived status, so live detection is
    preserved: an active-status fixture in the SAME store still flags, and its
    nested goals are skipped along with the tombstone aspiration."""
    scanner = _load_scanner()
    retired_asp = {**FIXTURE_ASP, "id": "asp-338", "status": "retired"}
    archived_asp = {**FIXTURE_ASP, "id": "asp-339", "status": "archived"}
    live_asp = {**FIXTURE_ASP, "id": "asp-350", "status": "active"}
    world = _seed_world(tmp_path, archive=[retired_asp, archived_asp, live_asp])
    ids = {s["record_id"] for s in scanner.scan(world_dir=str(world))}
    # Governed tombstones (and their nested goals) are NOT flagged.
    assert not any(i.startswith("asp-338") for i in ids), ids
    assert not any(i.startswith("asp-339") for i in ids), ids
    # A fresh (active-status) leak in the same archive store IS still detected,
    # nested goals included -- live-leak detection is preserved.
    assert "asp-350" in ids
    assert "asp-350/g-338-01" in ids


def test_exit_on_hits_codes(tmp_path, monkeypatch):
    scanner = _load_scanner()
    world = _seed_world(tmp_path, live=[FIXTURE_ASP])
    monkeypatch.setattr(
        sys, "argv",
        ["fixture-leak-scan.py", "--world-dir", str(world), "--exit-on-hits"])
    assert scanner.main() == 1          # hits + enforce => 1
    monkeypatch.setattr(
        sys, "argv", ["fixture-leak-scan.py", "--world-dir", str(world)])
    assert scanner.main() == 0          # advisory default => 0 even with hits
    # Clean store => 0 under both.
    clean = _seed_world(tmp_path / "c", live=[CLEAN_ASP])
    monkeypatch.setattr(
        sys, "argv",
        ["fixture-leak-scan.py", "--world-dir", str(clean), "--exit-on-hits"])
    assert scanner.main() == 0


if __name__ == "__main__":
    import os
    # guard-955: pin local so a direct run never refreshes the tmp seed over the
    # production store (the conftest pin only covers pytest-collected runs).
    os.environ["STORAGE_BACKEND"] = "local"
    test_detects_seeded_fixture(Path(__import__("tempfile").mkdtemp()))
    test_clean_store_returns_zero(Path(__import__("tempfile").mkdtemp()))
    test_parallel_filer_regex_is_anchored(Path(__import__("tempfile").mkdtemp()))
    test_archive_is_scanned(Path(__import__("tempfile").mkdtemp()))
    test_retired_and_archived_tombstones_are_skipped(
        Path(__import__("tempfile").mkdtemp()))
    print("ok (run under pytest for the monkeypatch exit-code test)")
