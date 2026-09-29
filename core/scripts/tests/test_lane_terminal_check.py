# Regression tests for the lane-terminal gate
# (core/scripts/lane_terminal_check.py) — the supply governor must not mark
# a terminal aspiration's lane STARVED (). Runnable two ways:
#   py -3 core/scripts/tests/test_lane_terminal_check.py     (standalone)
#   py -3 -m pytest core/scripts/tests/test_lane_terminal_check.py -q
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]          # project root
SKILL = ROOT / ".claude" / "skills" / "generate-domain-goals" / "SKILL.md"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lane_terminal_check as L  # noqa: E402

# Domain-free fixture lanes (domain-free-examples.md — the incident's real
# aspiration IDs stay in the goal record, not in a core test).
LIVE = ["asp-011", "asp-012", "asp-013"]
TERMINAL = ["asp-014", "asp-015"]


def _reader(map_):
    def reader(lane, source="world"):
        if lane in map_:
            return map_[lane], None
        return None, "reader error not_found: Aspiration " + lane + " not found"
    return reader


def test_terminal_lanes_are_excluded_from_the_floor_set():
    verdict = L.classify_lanes(LIVE + TERMINAL,
                               reader=_reader({**dict(zip(LIVE, ["active"] * 3)),
                                               **dict(zip(TERMINAL, ["completed", "retired"]))}))
    assert verdict["in_floor"] == LIVE
    assert verdict["excluded_terminal"] == TERMINAL
    assert verdict["degraded"] == []
    assert verdict["degraded_flag"] is False
    by_lane = {e["lane"]: e for e in verdict["lanes"]}
    assert by_lane["asp-014"]["terminal"] is True
    assert by_lane["asp-011"]["terminal"] is False


def test_paused_is_live_not_terminal():
    # A paused lane still holds its pending goals; its floor still means
    # something (CLAUDE.md aspiration status vocabulary).
    verdict = L.classify_lanes(["asp-011", "asp-014"],
                               reader=_reader({"asp-011": "paused", "asp-014": "completed"}))
    assert verdict["in_floor"] == ["asp-011"]
    assert verdict["excluded_terminal"] == ["asp-014"]


def test_unreadable_lane_fails_open_into_the_floor_set():
    # A detectable read failure must NOT exclude the lane (guard-1084:
    # silence is not an all-clear — but it is not a terminal status either).
    verdict = L.classify_lanes(["asp-011", "asp-014"],
                               reader=_reader({"asp-011": "active"}))
    assert verdict["in_floor"] == ["asp-011", "asp-014"]
    assert verdict["degraded"] == ["asp-014"]
    assert verdict["degraded_flag"] is True
    by_lane = {e["lane"]: e for e in verdict["lanes"]}
    assert by_lane["asp-014"]["read_error"] is not None
    assert by_lane["asp-014"]["terminal"] is False


def test_all_unreadable_is_degraded_not_fatal():
    verdict = L.classify_lanes(["asp-011", "asp-012"], reader=_reader({}))
    assert verdict["in_floor"] == ["asp-011", "asp-012"]
    assert verdict["degraded_flag"] is True
    assert verdict["excluded_terminal"] == []


def test_cli_check_emits_verdict_and_exits_zero():
    # Fixture-free (save/restore, not pytest's monkeypatch) so the standalone
    # runner below — which passes at most one argument — can run this too.
    orig = L.shell_read_status
    L.shell_read_status = _reader(
        {**dict(zip(LIVE, ["active", "active", "active"])),
         **dict(zip(TERMINAL, ["completed", "retired"]))})
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = L.main(["check", "--lanes", ",".join(LIVE + TERMINAL), "--json"])
    finally:
        L.shell_read_status = orig
    assert rc == 0
    verdict = json.loads(buf.getvalue())
    assert verdict["in_floor"] == LIVE
    assert verdict["excluded_terminal"] == TERMINAL


def test_cli_refuses_malformed_lane_tokens():
    with pytest.raises(SystemExit):
        L.main(["check", "--lanes", "asp-011,not-a-lane"])
    with pytest.raises(SystemExit):
        L.main(["check", "--lanes", "asp-11a"])
    with pytest.raises(SystemExit):
        L.main(["check", "--lanes", "   "])


# ---- the wiring itself (the outcome-observation rot lesson, domain-hooks.md) --
def test_supply_governor_actually_calls_the_gate():
    """A gate wired only in prose rots invisibly. Pin the call site."""
    text = SKILL.read_text(encoding="utf-8")
    assert "lane_terminal_check.py" in text, (
        "generate-domain-goals SKILL.md must CALL the lane-terminal gate, "
        "not describe it (guard-399)")
    assert "terminal" in text.lower()


def test_gate_is_registered_in_gates_yaml():
    text = (ROOT / "core" / "config" / "gates.yaml").read_text(encoding="utf-8")
    assert "lane-terminal-gate" in text


if __name__ == "__main__":
    import tempfile, traceback
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td)) if fn.__code__.co_argcount else fn()
                print(f"  PASS {name}")
            except Exception:
                failed += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
