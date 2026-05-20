"""test_bash_inject_misses_recent.py — smoke test for the recent-miss counter.

Paired with g-115-897 (Apply: surface .bash-inject-misses.jsonl growth in
/verify-learning). Verifies the helper script:

  * counts only entries within the time window
  * emits the three verdicts at the right thresholds
  * fails open when the log file is absent
  * returns valid JSON on stdout, always exit 0
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
SCRIPT = CORE_SCRIPTS / "bash-inject-misses-recent.sh"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _write_miss_log(path: Path, ages_hours: list[float]) -> None:
    """Write a synthetic miss log with entries at the given ages (in hours)."""
    now = datetime.now()
    lines = []
    for i, age_h in enumerate(ages_hours):
        ts = now - timedelta(hours=age_h)
        rec = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "sid": f"test-sid-{i:04d}",
            "reason": "no_active_agent_binding",
            "hint": "synthetic test fixture",
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(miss_log_path: Path | None, **env_overrides) -> dict:
    """Invoke the script with the given log path and env overrides."""
    env = os.environ.copy()
    if miss_log_path is not None:
        env["MISS_LOG_PATH"] = str(miss_log_path)
    env.update({k: str(v) for k, v in env_overrides.items()})

    proc = subprocess.run(
        [BASH, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"script exited {proc.returncode}: stderr={proc.stderr!r}"
    assert proc.stdout.strip(), f"empty stdout: stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


def test_missing_log_fails_open():
    """An absent log file produces count=0, verdict=clean, miss_log_exists=false."""
    with tempfile.TemporaryDirectory() as tmpd:
        nonexistent = Path(tmpd) / "does-not-exist.jsonl"
        result = _run(nonexistent)
        assert result["count"] == 0
        assert result["verdict"] == "clean"
        assert result["miss_log_exists"] is False
        # Default thresholds
        assert result["threshold_advisory"] == 5
        assert result["threshold_alert"] == 20
        assert result["window_hours"] == 24


def test_old_entries_filtered_by_window():
    """Entries older than the window do NOT count."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        # All entries 100h old — outside default 24h window
        _write_miss_log(log, [100, 110, 120, 130, 140, 150, 160])
        result = _run(log)
        assert result["count"] == 0, f"old entries should not count: {result}"
        assert result["verdict"] == "clean"
        assert result["miss_log_exists"] is True


def test_recent_entries_count():
    """Entries within the window count toward the verdict."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        # 3 entries inside 24h, 2 outside
        _write_miss_log(log, [1.5, 5.0, 12.0, 25.0, 48.0])
        result = _run(log)
        assert result["count"] == 3, f"expected 3 in-window: {result}"
        assert result["verdict"] == "clean"  # below advisory=5


def test_advisory_threshold():
    """Hitting the advisory threshold flips the verdict."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        # 6 entries inside 24h — at advisory threshold (5)
        _write_miss_log(log, [0.5, 1.0, 2.0, 4.0, 8.0, 16.0])
        result = _run(log)
        assert result["count"] == 6
        assert result["verdict"] == "advisory"


def test_alert_threshold():
    """Hitting the alert threshold flips again."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        ages = [0.1 * i for i in range(1, 22)]  # 21 entries within 2.1h
        _write_miss_log(log, ages)
        result = _run(log)
        assert result["count"] == 21
        assert result["verdict"] == "alert"


def test_custom_thresholds():
    """Env-tunable thresholds flip verdict at different counts."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        _write_miss_log(log, [1.0, 2.0])
        result = _run(log,
                      MISS_THRESHOLD_ADVISORY="2",
                      MISS_THRESHOLD_ALERT="100")
        assert result["count"] == 2
        assert result["verdict"] == "advisory"
        assert result["threshold_advisory"] == 2
        assert result["threshold_alert"] == 100


def test_custom_window():
    """A larger window picks up older entries."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        # 5 entries 48h-72h old
        _write_miss_log(log, [48, 54, 60, 66, 72])
        # Default 24h window — count=0
        result_default = _run(log)
        assert result_default["count"] == 0
        # 96h window — count=5
        result_wider = _run(log, MISS_WINDOW_HOURS="96")
        assert result_wider["count"] == 5
        assert result_wider["window_hours"] == 96


def test_malformed_lines_skipped():
    """Non-JSON lines and lines missing 'ts' are silently skipped."""
    with tempfile.TemporaryDirectory() as tmpd:
        log = Path(tmpd) / "misses.jsonl"
        now = datetime.now()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        log.write_text(
            "this is not json\n"
            "{}\n"  # valid JSON but no ts
            f'{{"ts":"{ts}","sid":"a"}}\n'  # one good entry
            "{not valid json either}\n"
            f'{{"ts":"not-a-timestamp","sid":"b"}}\n'  # bad ts
            "\n"  # blank line
            f'{{"ts":"{ts}","sid":"c"}}\n',  # one more good entry
            encoding="utf-8",
        )
        result = _run(log)
        assert result["count"] == 2, f"expected 2 good entries: {result}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
