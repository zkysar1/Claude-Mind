"""test_stop_checkpoint.py — graceful-stop checkpoint sentinel ( / FW-11).

The stop-checkpoint.json sentinel is the persistent detection signal for
resuming an autocompact-interrupted graceful stop. Its presence means "a stop
is in progress / was interrupted"; it is written at GS-0, preserved across
resume re-entries, and cleared only at clean completion (D7.1).

Lanes:
  1. write then read round-trips; fresh write has resume_count=0
  2. write twice (re-entry): stop_started_at preserved, target_mode updated,
     resume_count increments
  3. resume_info on empty dir → resume_needed False (no_checkpoint)
  4. resume_info after write → resume_needed True (checkpoint_present)
  5. clear removes the file (existed True), idempotent second clear (False)
  6. resume_info after clear → resume_needed False
  7. breaker: resume_count >= MAX_RESUME_ATTEMPTS → resume_needed False
     (breaker_tripped) — a persistently-failing stop escalates, never auto-loops
  8. invalid target_mode defaults to assistant (defensive — GS-0 only emits
     assistant/reader)
  9. corrupt checkpoint file → read_checkpoint returns None and resume_info is
     safe (won't auto-resume on garbage)
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import stop_checkpoint as sc  # noqa: E402


def test_write_read_roundtrip(tmp_path):
    rec = sc.write_checkpoint(tmp_path, "assistant")
    assert rec["target_mode"] == "assistant"
    assert rec["resume_count"] == 0
    assert rec["stop_started_at"]
    assert sc.read_checkpoint(tmp_path) == rec


def test_reentry_preserves_start_and_increments(tmp_path):
    first = sc.write_checkpoint(tmp_path, "assistant")
    second = sc.write_checkpoint(tmp_path, "reader")
    assert second["stop_started_at"] == first["stop_started_at"]  # preserved
    assert second["target_mode"] == "reader"  # updated
    assert second["resume_count"] == 1  # incremented on re-entry


def test_resume_info_empty(tmp_path):
    info = sc.resume_info(tmp_path)
    assert info["resume_needed"] is False
    assert info["reason"] == "no_checkpoint"


def test_resume_info_after_write(tmp_path):
    sc.write_checkpoint(tmp_path, "reader")
    info = sc.resume_info(tmp_path)
    assert info["resume_needed"] is True
    assert info["reason"] == "checkpoint_present"
    assert info["target_mode"] == "reader"


def test_clear_idempotent(tmp_path):
    sc.write_checkpoint(tmp_path, "assistant")
    assert sc.clear_checkpoint(tmp_path) is True   # existed
    assert sc.read_checkpoint(tmp_path) is None
    assert sc.clear_checkpoint(tmp_path) is False  # already gone


def test_resume_info_after_clear(tmp_path):
    sc.write_checkpoint(tmp_path, "assistant")
    sc.clear_checkpoint(tmp_path)
    assert sc.resume_info(tmp_path)["resume_needed"] is False


def test_breaker_trips_at_max(tmp_path):
    sc.write_checkpoint(tmp_path, "assistant")          # resume_count 0
    for _ in range(sc.MAX_RESUME_ATTEMPTS):             # re-entries -> 1,2,3
        sc.write_checkpoint(tmp_path, "assistant")
    cp = sc.read_checkpoint(tmp_path)
    assert cp["resume_count"] == sc.MAX_RESUME_ATTEMPTS
    info = sc.resume_info(tmp_path)
    assert info["resume_needed"] is False
    assert info["reason"] == "breaker_tripped"


def test_invalid_mode_defaults_assistant(tmp_path):
    rec = sc.write_checkpoint(tmp_path, "autonomous")  # not a valid post-stop mode
    assert rec["target_mode"] == "assistant"


def test_corrupt_checkpoint_returns_none(tmp_path):
    (tmp_path / sc.CHECKPOINT_NAME).write_text("{not valid json", encoding="utf-8")
    assert sc.read_checkpoint(tmp_path) is None
    # safe: garbage must not trigger an auto-resume
    assert sc.resume_info(tmp_path)["resume_needed"] is False
