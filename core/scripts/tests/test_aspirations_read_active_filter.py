"""test_aspirations_read_active_filter.py -  regression test.

Pins the status filter on the /v1/aspirations/read ?active=1 branch
(mind_api/src/endpoints/aspirations.py). Before g-115-2604 that branch
returned the ENTIRE live file (no status filter) while the sibling
?active_compact=1 branch filtered status == "active" — so retired-but-
unarchived records (e.g. the g-328-29 fixture tombstones asp-344..349)
surfaced in every `aspirations-read.sh --active` consumer (boot,
backlog-report, decompose) as if active: 31 shown vs 20 genuinely active
on 2026-07-18. Found by the g-115-1651 store-hygiene sweep's rb-3382
cross-check; per-id reads returned status=retired for the same records,
proving fresh data + filter defect (rb-4014 two-reader triage).

Contract pinned here:
  1. ?active=1 returns ONLY records with status == "active" — retired and
     paused records are excluded.
  2. ?active=1 and ?active_compact=1 agree on the id set (branch parity).
  3. A retired record REMAINS readable per-id (?id=...) — the
     discrimination-probe path rb-4014 depends on must not regress.

Live-daemon safe (guard-672): uses the in-process DaemonFixture against a
temp world (ephemeral OS port, temp mind_api/state/) - never touches the
live daemon or world directory.
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

ACTIVE_ID = "asp-af-act"
RETIRED_ID = "asp-af-ret"
PAUSED_ID = "asp-af-pau"


def _seed_world(tmp: Path) -> Path:
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        "nodes: {}\n", encoding="utf-8")
    records = [
        {"id": ACTIVE_ID, "title": "active aspiration", "status": "active",
         "goals": [{"id": "g-af-01", "title": "live goal", "status": "pending"}]},
        {"id": RETIRED_ID, "title": "retired fixture tombstone", "status": "retired",
         "goals": []},
        {"id": PAUSED_ID, "title": "paused aspiration", "status": "paused",
         "goals": [{"id": "g-af-02", "title": "parked goal", "status": "pending"}]},
    ]
    (world / "aspirations.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return world


def _http_get(port: int, path: str, agent: str = "alpha"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Mind-Agent": agent},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_active_excludes_non_active_statuses():
    with tempfile.TemporaryDirectory(prefix="af-read-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, body = _http_get(df.port, "/v1/aspirations/read?active=1")
    assert status == 200, f"expected HTTP 200, got {status}: {body[:200]}"
    items = json.loads(body)
    ids = sorted(a["id"] for a in items)
    assert ids == [ACTIVE_ID], (
        f"?active=1 must return ONLY status==active records; got ids={ids} "
        f"(retired/paused leaking through is the g-115-2604 defect)")
    statuses = {a.get("status") for a in items}
    assert statuses == {"active"}, statuses


def test_active_and_active_compact_agree_on_id_set():
    with tempfile.TemporaryDirectory(prefix="af-parity-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            s1, b1 = _http_get(df.port, "/v1/aspirations/read?active=1")
            s2, b2 = _http_get(df.port, "/v1/aspirations/read?active_compact=1")
    assert s1 == 200 and s2 == 200, (s1, s2)
    full_ids = sorted(a["id"] for a in json.loads(b1))
    compact_ids = sorted(a["id"] for a in json.loads(b2))
    assert full_ids == compact_ids, (
        f"branch parity broken: active={full_ids} active_compact={compact_ids}")


def test_retired_record_still_readable_per_id():
    with tempfile.TemporaryDirectory(prefix="af-perid-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, body = _http_get(
                df.port, f"/v1/aspirations/read?id={RETIRED_ID}")
    assert status == 200, f"per-id read of retired record failed: {status} {body[:200]}"
    rec = json.loads(body)
    assert rec["id"] == RETIRED_ID and rec["status"] == "retired", rec


def _http_post(port: int, path: str, agent: str = "alpha"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"",
        method="POST",
        headers={"X-Mind-Agent": agent},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_archive_sweep_replaces_stale_archive_copy_by_id():
    """ leg 2: a retired live record whose id ALREADY exists in the
    archive (resurrection shape: archived once, reappeared in live via a
    partial write, re-retired) must REPLACE the stale archive copy on sweep —
    not append a duplicate. Pre-fix, archive.extend() appended blindly."""
    with tempfile.TemporaryDirectory(prefix="af-sweep-") as tmpd:
        world = _seed_world(Path(tmpd))
        # Seed the archive with a STALE copy of the retired record (older
        # status "completed" — the live "retired" copy is newer and must win).
        stale = {"id": RETIRED_ID, "title": "retired fixture tombstone",
                 "status": "completed", "goals": []}
        (world / "aspirations-archive.jsonl").write_text(
            json.dumps(stale) + "\n", encoding="utf-8")
        with DaemonFixture(world) as df:
            status, body = _http_post(
                df.port, "/v1/aspirations/archive-sweep?source=world")
            assert status == 200, f"sweep failed: {status} {body[:200]}"
            resp = json.loads(body)
        live = [json.loads(l) for l in
                (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        archive = [json.loads(l) for l in
                   (world / "aspirations-archive.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert resp.get("deduped_replaced") == 1, resp
    live_ids = [a["id"] for a in live]
    assert RETIRED_ID not in live_ids, (
        f"retired record must leave the live file on sweep; live={live_ids}")
    copies = [a for a in archive if a["id"] == RETIRED_ID]
    assert len(copies) == 1, (
        f"archive must hold exactly ONE copy of {RETIRED_ID}, got {len(copies)} "
        f"(extend-without-dedup is the g-115-2604 defect)")
    assert copies[0]["status"] == "retired", (
        f"replace-by-id must keep the NEWER live state; got {copies[0]['status']}")


if __name__ == "__main__":
    test_active_excludes_non_active_statuses()
    print("PASS: active excludes non-active statuses")
    test_active_and_active_compact_agree_on_id_set()
    print("PASS: active/active_compact parity")
    test_retired_record_still_readable_per_id()
    print("PASS: retired record readable per-id")
    test_archive_sweep_replaces_stale_archive_copy_by_id()
    print("PASS: archive-sweep replace-by-id dedup")
    print("4/4 passed")
