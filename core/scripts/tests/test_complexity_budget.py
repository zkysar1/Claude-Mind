"""Tests for complexity_budget.py (Phase 2 — the subtractive gradient's instrument)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import complexity_budget as cb  # noqa: E402


def _make_tree(root: Path, *, gates=0, rules=0, skills=0, scripts=0, conventions=0,
               orchestrator_lines=0, yaml_lines=0):
    (root / "core/scripts").mkdir(parents=True, exist_ok=True)
    (root / ".claude/rules").mkdir(parents=True, exist_ok=True)
    (root / ".claude/skills/aspirations").mkdir(parents=True, exist_ok=True)
    (root / "core/config/conventions").mkdir(parents=True, exist_ok=True)
    for i in range(gates):
        (root / f"core/scripts/x{i}-gate.py").write_text("x", encoding="utf-8")
    for i in range(rules):
        (root / f".claude/rules/r{i}.md").write_text("x", encoding="utf-8")
    for i in range(skills):
        d = root / f".claude/skills/s{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("x", encoding="utf-8")
    for i in range(scripts):
        (root / f"core/scripts/u{i}.py").write_text("x", encoding="utf-8")
    for i in range(conventions):
        (root / f"core/config/conventions/c{i}.md").write_text("x", encoding="utf-8")
    if orchestrator_lines:
        (root / ".claude/skills/aspirations/SKILL.md").write_text(
            "\n".join("l" for _ in range(orchestrator_lines)), encoding="utf-8")
    if yaml_lines:
        (root / "core/config/aspirations.yaml").write_text(
            "\n".join("k: v" for _ in range(yaml_lines)), encoding="utf-8")


def test_measure_counts(tmp_path):
    _make_tree(tmp_path, gates=3, rules=5, skills=2, scripts=4, conventions=6,
               orchestrator_lines=100, yaml_lines=42)
    m = cb.measure(tmp_path)
    # gate scripts are *-gate.py; the 4 plain scripts (u*.py) are NOT gates but
    # ARE core_scripts; core_scripts counts ALL *.py incl. the gate files.
    assert m["gate_scripts"] == 3
    assert m["rules"] == 5
    # s0, s1 have SKILL.md AND orchestrator_lines wrote aspirations/SKILL.md -> 3
    assert m["skills"] == 3
    assert m["core_scripts"] == 3 + 4  # 3 gate.py + 4 u.py
    assert m["conventions"] == 6
    assert m["orchestrator_lines"] == 100
    assert m["aspirations_yaml_lines"] == 42


def test_measure_missing_file_is_none_not_zero(tmp_path):
    _make_tree(tmp_path)  # no orchestrator / yaml written
    m = cb.measure(tmp_path)
    assert m["orchestrator_lines"] is None
    assert m["aspirations_yaml_lines"] is None


def test_append_and_delta_baseline(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    out = cb.append_and_delta({"gate_scripts": 10}, ledger, timestamp="2026-01-01T00:00:00")
    assert out["verdict"] == "baseline" and out["had_previous"] is False
    assert ledger.exists() and len(ledger.read_text().splitlines()) == 1


def test_append_and_delta_growing(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"gate_scripts": 10, "rules": 5}, ledger, timestamp="t1")
    out = cb.append_and_delta({"gate_scripts": 12, "rules": 5}, ledger, timestamp="t2")
    assert out["verdict"] == "growing" and out["deltas"]["gate_scripts"] == 2
    assert out["deltas"]["rules"] == 0


def test_append_and_delta_shrinking_is_celebrated(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"gate_scripts": 12}, ledger, timestamp="t1")
    out = cb.append_and_delta({"gate_scripts": 9}, ledger, timestamp="t2")
    assert out["verdict"] == "shrinking" and out["deltas"]["gate_scripts"] == -3


def test_append_and_delta_mixed_and_flat(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"a": 5, "b": 5}, ledger, timestamp="t1")
    assert cb.append_and_delta({"a": 6, "b": 4}, ledger, timestamp="t2")["verdict"] == "mixed"
    assert cb.append_and_delta({"a": 6, "b": 4}, ledger, timestamp="t3")["verdict"] == "flat"


def test_none_to_int_is_a_transition_not_silent_flat(tmp_path):
    # HIGH fresh-eyes finding: a metric appearing/disappearing (None<->int) must be
    # surfaced as a transition, not swallowed into a "flat" verdict.
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"orchestrator_lines": None, "rules": 5}, ledger, timestamp="t1")
    out = cb.append_and_delta({"orchestrator_lines": 100, "rules": 5}, ledger, timestamp="t2")
    assert "orchestrator_lines" not in out["deltas"]          # not a numeric delta
    assert "orchestrator_lines" in out["transitions"]         # but recorded as a transition
    assert out["verdict"] == "changed"                        # NOT "flat"


def test_disappearing_metric_is_a_transition(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"rules": 5}, ledger, timestamp="t1")
    out = cb.append_and_delta({"rules": None}, ledger, timestamp="t2")  # file moved/gone
    assert out["transitions"]["rules"] == "5->None"
    assert out["verdict"] == "changed"


def test_growth_alongside_transition_still_flags_growth(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"gate_scripts": 10, "orchestrator_lines": None}, ledger, timestamp="t1")
    out = cb.append_and_delta({"gate_scripts": 12, "orchestrator_lines": 100}, ledger, timestamp="t2")
    assert out["deltas"]["gate_scripts"] == 2 and out["verdict"] == "growing"
    assert "orchestrator_lines" in out["transitions"]


def test_corrupt_trailing_ledger_line_is_skipped(tmp_path):
    # MED fresh-eyes finding: one corrupt/partial trailing line must not brick all
    # future appends (fail-open scan backward for the last valid row).
    ledger = tmp_path / "ledger.jsonl"
    cb.append_and_delta({"gate_scripts": 10}, ledger, timestamp="t1")
    with open(ledger, "a", encoding="utf-8") as f:
        f.write("this is not json\n")          # interrupted/garbage append
    out = cb.append_and_delta({"gate_scripts": 11}, ledger, timestamp="t2")  # must not raise
    assert out["had_previous"] is True and out["deltas"]["gate_scripts"] == 1


def test_cli_measure_only(tmp_path, capsys):
    _make_tree(tmp_path, gates=2, rules=3)
    rc = cb.main(["--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["metrics"]["gate_scripts"] == 2 and out["metrics"]["rules"] == 3


def test_precheck_skill_is_a_tracked_line_count_target():
    """: precheck_lines must stay in _LINE_COUNT_TARGETS.

    The two original entries measured the surface already under deliberate
    DOWNWARD pressure (the orchestrator is small because the loop-digest
    extraction slimmed it), so the instrument read healthy for 26 days while
    aspirations-precheck/SKILL.md grew +651 lines (+29.4%) against the
    orchestrator's +54 (+6.6%) — 12x the lines at 4.5x the rate.

    Nothing else pins this. hot-path-size-gate.sh also sees the file, but by
    BYTES and as a commit-time PASS/FAIL on a CORPUS total, so it cannot say
    which of 131 skills grew. Removing this key would silently restore the
    blind spot that took three agents on three boxes to notice.
    """
    assert cb._LINE_COUNT_TARGETS.get("precheck_lines") == (
        ".claude/skills/aspirations-precheck/SKILL.md"
    )
