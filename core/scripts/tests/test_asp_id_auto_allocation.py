"""test_asp_id_auto_allocation.py —  race-free aspiration IDs.

Pins the BRD P1 contract: `/v1/aspirations/add` mints asp-NNN INSIDE the
write lock when the caller omits the id (max+1 across live ∪ archive of the
target queue), so two concurrent auto adds get distinct sequential ids —
the asp-334/asp-335 double-mint was client-side max+1 computed OUTSIDE any
lock. Embedded goal ids are minted g-NNN-01.. in array order; goals carrying
explicit ids under auto allocation are refused (auto_id_goal_conflict).
Explicit-id filing stays byte-identical (transplant/migration callers).

Pattern: DaemonFixture + direct HTTP POST (hermetic in-process daemon; NOT
daemon_integration-marked). Mirrors test_prose_verification_drift_daemon_parity.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path, *, archive_max: int | None = None) -> Path:
    """Tempdir world with live asp-100 (one goal) and, optionally, an
    archived aspiration at a HIGHER number — the mint must scan both."""
    world = tmp / "world"
    world.mkdir()

    seed_goal = {
        "id": "g-100-01",
        "title": "Seed goal",
        "description": "Pre-existing goal.",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    live = {
        "id": "asp-100",
        "title": "existing live aspiration",
        "motivation": "occupies the live max",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "archived": False,
        "created": "2026-05-01T00:00:00",
        "goals": [seed_goal],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(live, ensure_ascii=False) + "\n")

    if archive_max is not None:
        arch = {
            "id": f"asp-{archive_max:03d}",
            "title": "archived aspiration at the queue max",
            "status": "completed",
            "priority": "MEDIUM",
            "archived": True,
            "goals": [],
        }
        (world / "aspirations-archive.jsonl").write_text(
            json.dumps(arch, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _asp_body(title: str, *, goals: list | None = None, **extra) -> dict:
    body = {
        "title": title,
        "motivation": "g-328-29 fixture",
        "scope": "sprint",
        "priority": "MEDIUM",
        "status": "active",
        "origin_signal": "user_directive",
        "goals": goals if goals is not None else [],
    }
    body.update(extra)
    return body


def _goal_body(title: str, **extra) -> dict:
    g = {
        "title": title,
        "description": "auto-minted goal",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    g.update(extra)
    return g


def _add(port: int, body: dict, agent: str = "alpha") -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}/v1/aspirations/add?source=world"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def _live_ids(world: Path) -> list[str]:
    out = []
    for line in (world / "aspirations.jsonl").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line)["id"])
    return out


# --- mint scans live ∪ archive; goals minted in array order -------------------

def test_auto_mint_over_live_and_archive_with_goal_ids():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), archive_max=250)
        with DaemonFixture(world) as df:
            body = _asp_body("auto-minted", goals=[
                _goal_body("first goal"), _goal_body("second goal")])
            status, out = _add(df.port, body)
            assert status == 200, f"auto add must land; got {status} {out!r}"
            # Archive max (250) beats live max (100) → asp-251.
            assert out["aspiration_id"] == "asp-251", out
            assert out.get("id_allocated") is True, out
            goals = out["aspiration"]["goals"]
            assert [g["id"] for g in goals] == ["g-251-01", "g-251-02"], goals
            assert "asp-251" in _live_ids(world)


def test_auto_string_forms_and_explicit_untouched():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            # "auto" and "" both trigger the mint.
            s1, o1 = _add(df.port, _asp_body("via auto literal", id="auto"))
            assert s1 == 200 and o1["aspiration_id"] == "asp-101", o1
            s2, o2 = _add(df.port, _asp_body("via empty string", id=""))
            assert s2 == 200 and o2["aspiration_id"] == "asp-102", o2
            # Explicit id lands as-is, no id_allocated marker.
            s3, o3 = _add(df.port, _asp_body("explicit", id="asp-150"))
            assert s3 == 200 and o3["aspiration_id"] == "asp-150", o3
            assert "id_allocated" not in o3, o3
            # Next auto mint continues past the explicit id.
            s4, o4 = _add(df.port, _asp_body("after explicit"))
            assert s4 == 200 and o4["aspiration_id"] == "asp-151", o4


def test_auto_with_embedded_goal_ids_refused():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            body = _asp_body("conflicted", goals=[
                _goal_body("carries an id", id="g-999-01")])
            status, out = _add(df.port, body)
            assert status == 400, f"expected 400; got {status} {out!r}"
            assert "auto_id_goal_conflict" in json.dumps(out), out
            assert _live_ids(world) == ["asp-100"], "nothing must land"


def test_explicit_duplicate_and_archive_reuse_still_refused():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd), archive_max=250)
        with DaemonFixture(world) as df:
            s1, o1 = _add(df.port, _asp_body("dup live", id="asp-100"))
            assert s1 == 400 and "duplicate_id" in json.dumps(o1), (s1, o1)
            s2, o2 = _add(df.port, _asp_body("dup archived", id="asp-250"))
            assert s2 == 400 and "archived_id_reuse" in json.dumps(o2), (s2, o2)


# --- THE goal criterion: concurrent auto adds, distinct sequential ids --------

def test_parallel_auto_adds_get_distinct_sequential_ids():
    """asp-334 incident shape: N writers file simultaneously WITHOUT ids.
    All must land, ids must be exactly {asp-101..asp-10N}, zero retries."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            n = 4
            results: list[tuple[int, dict]] = [None] * n  # type: ignore

            def file_one(i: int):
                results[i] = _add(df.port, _asp_body(f"parallel filer {i}"))

            threads = [threading.Thread(target=file_one, args=(i,), daemon=True)
                       for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert all(r is not None for r in results), "a filer never returned"
            statuses = [r[0] for r in results]
            assert statuses == [200] * n, f"every filer must land: {results}"
            minted = sorted(r[1]["aspiration_id"] for r in results)
            assert minted == [f"asp-{101 + i}" for i in range(n)], (
                f"ids must be distinct AND sequential (no double-mint, no gap); "
                f"got {minted}")
            on_disk = _live_ids(world)
            assert len(on_disk) == len(set(on_disk)) == n + 1, (
                f"disk must hold asp-100 + {n} minted records exactly once "
                f"each; got {on_disk}")


if __name__ == "__main__":
    test_auto_mint_over_live_and_archive_with_goal_ids()
    test_auto_string_forms_and_explicit_untouched()
    test_auto_with_embedded_goal_ids_refused()
    test_explicit_duplicate_and_archive_reuse_still_refused()
    test_parallel_auto_adds_get_distinct_sequential_ids()
    print("ok")
