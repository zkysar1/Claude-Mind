"""aspirations-increment-sessions-active.py writes the store in its CANONICAL form ().

The script used to re-read the local world store and os.replace it with json.dumps(ensure_ascii=False)
records. The composite writer's split() requires the ensure_ascii=True round trip, so it refused the
sync push of that file and the backend PUT the whole object instead (the daily "flip"; every flip since
2026-10-10 differs from the version before it by exactly one sessions_active += 1). These tests pin the
invariant that matters, not the serialization flag: what the script leaves behind is a body split() accepts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _owncloud_composite as C  # noqa: E402

SCRIPT = SCRIPTS / "aspirations-increment-sessions-active.py"

RECORDS = [
    {"id": "asp-001", "title": "plain", "sessions_active": 3,
     "goals": [{"id": "g-001-01", "title": "ascii only", "status": "pending"}]},
    {"id": "asp-002", "title": "em dash — title", "sessions_active": 7,
     "goals": [{"id": "g-002-01", "title": "café — résumé", "status": "pending",
                "progress_note": "a → b ≥ c"},
               {"id": "g-002-02", "title": "smile \U0001F600", "status": "completed"}]},
    {"id": "asp-003", "title": "no counter yet",
     "goals": [{"id": "g-003-01", "title": "t", "status": "pending"}]},
]


def _canonical(records) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in records).encode("ascii")


def _run(world: Path, asp_id: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"STORAGE_BACKEND": "local", "MIND_WORLD": str(world), "ASP_ID": asp_id, "SOURCE": "world"})
    return subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)


def _world(tmp_path: Path) -> Path:
    world = tmp_path / "world"
    world.mkdir()
    (world / "aspirations.jsonl").write_bytes(_canonical(RECORDS))
    return world


def test_increment_leaves_a_body_the_composite_splitter_accepts(tmp_path: Path) -> None:
    world = _world(tmp_path)
    proc = _run(world, "asp-002")
    assert proc.returncode == 0, proc.stderr
    assert "sessions_active incremented for asp-002 -> 8" in proc.stdout
    out = (world / "aspirations.jsonl").read_bytes()
    after = [dict(RECORDS[0]), {**RECORDS[1], "sessions_active": 8}, dict(RECORDS[2])]
    assert out == _canonical(after)  # exactly one counter moved; every other byte is the input's
    assert out.isascii()
    C.split(out)  # raises NotSplittable on a body the layout refuses


def test_the_raw_utf8_form_this_script_used_to_write_is_refused_by_the_splitter(tmp_path: Path) -> None:
    after = [dict(RECORDS[0]), {**RECORDS[1], "sessions_active": 8}, dict(RECORDS[2])]
    raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in after).encode("utf-8")
    assert not raw.isascii()
    with pytest.raises(C.NotSplittable):
        C.split(raw)
    C.split(_canonical(after))


def test_unknown_aspiration_reports_not_found_and_leaves_the_store_alone(tmp_path: Path) -> None:
    world = _world(tmp_path)
    before = (world / "aspirations.jsonl").read_bytes()
    proc = _run(world, "asp-404")
    assert proc.returncode == 0
    assert "not found" in proc.stderr
    assert (world / "aspirations.jsonl").read_bytes() == before
