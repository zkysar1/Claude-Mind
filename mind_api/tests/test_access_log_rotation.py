"""Access-log self-rotation (2026-08-21, user-reported 100 MB unbounded log).

The writer (`_Handler._access_log`) appends one JSONL line per request with
no bound — measured 100 MB on one dev box. `_rotate_access_log_if_oversize`
caps it: at ACCESS_LOG_MAX_BYTES the file renames to `<name>.1` (clobbering
the prior generation) and the append continues against a fresh file, so the
worst case is ~2x the cap per box, forever.

The kept `.1` generation is load-bearing, not tidiness: the access log is a
documented forensic source (rb-3277 greps recent team-state PUT statuses to
split write-path failure from cross-box pull lag), so rotation must preserve
a recent-history window and the old generation must stay grep-able JSONL —
which a rename gives and a truncate would not.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

from mind_api.src import server as _server


def _fill(path: Path, size: int) -> None:
    path.write_bytes(b'{"ts": "2026-08-21T00:00:00"}\n' * (size // 30 + 1))


# --- the helper -------------------------------------------------------------


def test_under_cap_is_a_noop(tmp_path):
    p = tmp_path / "access.log"
    _fill(p, 500)
    assert _server._rotate_access_log_if_oversize(p, max_bytes=10_000) is False
    assert p.exists()
    assert not (tmp_path / "access.log.1").exists()


def test_over_cap_rotates_to_dot_one(tmp_path):
    p = tmp_path / "access.log"
    _fill(p, 20_000)
    original = p.read_bytes()
    assert _server._rotate_access_log_if_oversize(p, max_bytes=10_000) is True
    assert not p.exists()                       # fresh file starts on next append
    assert (tmp_path / "access.log.1").read_bytes() == original


def test_second_rotation_clobbers_the_prior_generation(tmp_path):
    p = tmp_path / "access.log"
    gen1 = tmp_path / "access.log.1"
    gen1.write_text("old generation\n", encoding="utf-8")
    _fill(p, 20_000)
    newer = p.read_bytes()
    assert _server._rotate_access_log_if_oversize(p, max_bytes=10_000) is True
    assert gen1.read_bytes() == newer           # bounded: exactly one kept generation


def test_missing_file_is_a_noop_not_an_error(tmp_path):
    p = tmp_path / "access.log"
    assert _server._rotate_access_log_if_oversize(p, max_bytes=10_000) is False
    assert not p.exists()


# --- through the real writer ------------------------------------------------
#
# _access_log is self-contained enough to call with a stub `self` (it reads
# only self.path and self.access_log_path; the stats-collector call is
# wrapped in its own try/except). This exercises the production write path:
# lock -> rotate check -> append.


def _log_one(path: Path) -> None:
    stub = types.SimpleNamespace(path="/v1/admin/health", access_log_path=path)
    _server._Handler._access_log(stub, "GET", 1.234, None, "test-agent")


def test_writer_rotates_then_appends_to_a_fresh_file(tmp_path, monkeypatch):
    monkeypatch.setattr(_server, "ACCESS_LOG_MAX_BYTES", 10_000)
    p = tmp_path / "access.log"
    _fill(p, 20_000)
    _log_one(p)
    assert (tmp_path / "access.log.1").stat().st_size >= 20_000
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1                      # fresh file: exactly the new line
    rec = json.loads(lines[0])
    assert rec["method"] == "GET" and rec["agent"] == "test-agent"


def test_writer_under_cap_appends_in_place(tmp_path, monkeypatch):
    """Control (guard-3534): proves the rotation above is what produced `.1` —
    the identical call under the cap rotates nothing and appends in place."""
    monkeypatch.setattr(_server, "ACCESS_LOG_MAX_BYTES", 10_000)
    p = tmp_path / "access.log"
    _fill(p, 500)
    before_lines = len(p.read_text(encoding="utf-8").splitlines())
    _log_one(p)
    assert not (tmp_path / "access.log.1").exists()
    assert len(p.read_text(encoding="utf-8").splitlines()) == before_lines + 1
