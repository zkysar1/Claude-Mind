"""Unit tests for 8 auto-reconcile in fresh-eyes-cadence-check.py.

The fresh-eyes rituals write their archive report (Phase 4) BEFORE stamping the
cadence WM slot (Phase 8). Autocompact between those two steps leaves a report on
disk with the slot un-stamped, so the next iteration's cadence "fires" again even
though the review already ran. Option (b) auto-reconcile: when a report whose
embedded FILENAME timestamp is newer than the slot stamp exists, re-stamp the slot
and noop instead of wastefully re-running the ritual.

Coverage:
  - _report_filename_epoch: parses all three ritual filename shapes; None on junk
  - newest_reconcilable_report: returns newest report newer than the stamp; None
    when none newer; the review glob excludes program/tree siblings
  - main() reconcile path: returns noop (rc=1) + stamps the slot when a newer
    report exists and cadence would otherwise fire
  - main() fire path preserved: no newer report → fire (rc=0), no stamp written
  - backward-compat: a block without report_glob never reconciles
  - missing slot timestamp → no reconcile (fire normally)

Tests use monkeypatch + tmp_path so no live state is touched; subprocess.run
(the slot write) is stubbed so the suite is safe to run against a live daemon.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load_cadence_module():
    """Load fresh-eyes-cadence-check.py as a module despite the hyphenated name."""
    spec = importlib.util.spec_from_file_location(
        "fresh_eyes_cadence_check",
        str(SCRIPT_DIR / "fresh-eyes-cadence-check.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_cadence_module()


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


# ── _report_filename_epoch ────────────────────────────────────────────────────

def test_report_filename_epoch_parses_all_three_shapes(mod):
    assert mod._report_filename_epoch("fresh-eyes-2026-06-01T03-28-21.md") == \
        _epoch("2026-06-01T03:28:21")
    assert mod._report_filename_epoch("fresh-eyes-program-2026-06-01T01-29-08.md") == \
        _epoch("2026-06-01T01:29:08")
    assert mod._report_filename_epoch("fresh-eyes-tree-2026-05-26T06-42-48.md") == \
        _epoch("2026-05-26T06:42:48")


def test_report_filename_epoch_none_on_bad_shapes(mod):
    assert mod._report_filename_epoch("random.md") is None
    assert mod._report_filename_epoch("fresh-eyes-2026-06-01.md") is None  # no time
    assert mod._report_filename_epoch("fresh-eyes-notes.txt") is None


def test_parse_iso_epoch_rejects_sentinel_and_empty(mod):
    assert mod._parse_iso_epoch(None) is None
    assert mod._parse_iso_epoch("") is None
    assert mod._parse_iso_epoch("0000-00-00T00:00:00") is None  # seed-stagger sentinel
    assert mod._parse_iso_epoch("2026-06-01T03:28:21") == _epoch("2026-06-01T03:28:21")


# ── newest_reconcilable_report ─────────────────────────────────────────────────

def _touch(reports_dir: Path, name: str):
    (reports_dir / name).write_text("x", encoding="utf-8")


def test_newest_reconcilable_report_finds_newer(mod, tmp_path):
    rd = tmp_path / "reports"
    rd.mkdir()
    _touch(rd, "fresh-eyes-2026-05-20T00-00-00.md")  # older than stamp
    _touch(rd, "fresh-eyes-2026-06-01T03-28-21.md")  # newer than stamp
    slot_epoch = _epoch("2026-05-26T17:12:17")
    hit = mod.newest_reconcilable_report(rd, "fresh-eyes-[0-9]*.md", slot_epoch)
    assert hit is not None
    path, iso = hit
    assert path.name == "fresh-eyes-2026-06-01T03-28-21.md"
    assert iso == "2026-06-01T03:28:21"


def test_newest_reconcilable_report_none_when_all_older(mod, tmp_path):
    rd = tmp_path / "reports"
    rd.mkdir()
    _touch(rd, "fresh-eyes-2026-05-20T00-00-00.md")
    slot_epoch = _epoch("2026-05-26T17:12:17")
    assert mod.newest_reconcilable_report(rd, "fresh-eyes-[0-9]*.md", slot_epoch) is None


def test_review_glob_excludes_program_and_tree_siblings(mod, tmp_path):
    """The [0-9] glob must NOT match a NEWER program/tree report — otherwise a
    program review would wrongly satisfy the review cadence."""
    rd = tmp_path / "reports"
    rd.mkdir()
    _touch(rd, "fresh-eyes-2026-05-28T00-00-00.md")          # review, newer than stamp
    _touch(rd, "fresh-eyes-program-2026-06-01T06-00-00.md")  # program, even newer
    _touch(rd, "fresh-eyes-tree-2026-06-01T07-00-00.md")     # tree, newest of all
    slot_epoch = _epoch("2026-05-26T17:12:17")
    hit = mod.newest_reconcilable_report(rd, "fresh-eyes-[0-9]*.md", slot_epoch)
    assert hit is not None
    assert hit[0].name == "fresh-eyes-2026-05-28T00-00-00.md"  # NOT the program/tree


def test_program_glob_matches_only_program(mod, tmp_path):
    rd = tmp_path / "reports"
    rd.mkdir()
    _touch(rd, "fresh-eyes-2026-06-01T09-00-00.md")          # review (must be ignored)
    _touch(rd, "fresh-eyes-program-2026-05-30T00-00-00.md")  # program, newer than stamp
    slot_epoch = _epoch("2026-05-26T05:15:55")
    hit = mod.newest_reconcilable_report(rd, "fresh-eyes-program-*.md", slot_epoch)
    assert hit is not None
    assert hit[0].name == "fresh-eyes-program-2026-05-30T00-00-00.md"


# ── main() reconcile path ──────────────────────────────────────────────────────

def _stub_config(report_glob=None, min_session_goals=0, goal_cadence=25):
    block = {
        "goal_cadence": goal_cadence,
        "wm_slot": "last_fresh_eyes_review",
        "min_session_goals": min_session_goals,
    }
    if report_glob is not None:
        block["report_glob"] = report_glob
    return {"fresh_eyes_review": block}


def _wire(monkeypatch, mod, *, config, current, slot_value, loop_state=None):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: config)
    monkeypatch.setattr(mod, "count_completed_goals", lambda: current)

    def fake_wm(slot_name):
        if slot_name == "loop_state":
            return loop_state or {}
        if slot_name == "last_fresh_eyes_review":
            return slot_value
        return None

    monkeypatch.setattr(mod, "wm_slot_value", fake_wm)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])


def _wire_reports(monkeypatch, mod, tmp_path, names):
    # Briefings live under temp/ now (file-model normalization moved fresh-eyes
    # briefings reports/ -> temp/; cadence-check auto-reconcile scans temp/).
    rd = tmp_path / "temp"
    rd.mkdir()
    for n in names:
        (rd / n).write_text("x", encoding="utf-8")
    monkeypatch.setenv("MIND_AGENT", "testagent")
    monkeypatch.setattr(mod._paths, "agent_dir", lambda _name: tmp_path)
    return rd


def _stub_subprocess(monkeypatch, mod):
    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "input": kw.get("input")})

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


def test_main_reconciles_when_newer_report_exists(mod, monkeypatch, tmp_path, capsys):
    """Cadence would fire (diff>=cadence) but a newer report exists → noop + stamp."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(report_glob="fresh-eyes-[0-9]*.md"),
        current=100,
        slot_value={"timestamp": "2026-05-26T17:12:17", "goals_count_at_last_fire": 70},
    )
    _wire_reports(monkeypatch, mod, tmp_path, ["fresh-eyes-2026-06-01T03-28-21.md"])
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, f"expected reconcile noop (rc=1), got rc={rc}; out={out!r}"
    assert "auto-reconciled" in out
    assert len(calls) == 1, "slot stamp must be written exactly once"
    assert any("wm.py" in str(c) for c in calls[0]["cmd"])  # wm.py is a path element
    assert "last_fresh_eyes_review" in calls[0]["cmd"]
    assert '"auto_reconciled": true' in calls[0]["input"]
    assert '"goals_count_at_last_fire": 100' in calls[0]["input"]


def test_main_fires_when_no_newer_report(mod, monkeypatch, tmp_path, capsys):
    """Only older reports present → cadence fires normally (rc=0), no stamp."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(report_glob="fresh-eyes-[0-9]*.md"),
        current=100,
        slot_value={"timestamp": "2026-05-26T17:12:17", "goals_count_at_last_fire": 70},
    )
    _wire_reports(monkeypatch, mod, tmp_path, ["fresh-eyes-2026-05-20T00-00-00.md"])
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected fire (rc=0), got rc={rc}; out={out!r}"
    assert "fire" in out
    assert len(calls) == 0, "fire path must not write the slot"


def test_main_no_reconcile_without_report_glob(mod, monkeypatch, tmp_path, capsys):
    """Backward-compat: a block with no report_glob never reconciles even when a
    newer report sits on disk — it fires as before."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(report_glob=None),  # no report_glob
        current=100,
        slot_value={"timestamp": "2026-05-26T17:12:17", "goals_count_at_last_fire": 70},
    )
    _wire_reports(monkeypatch, mod, tmp_path, ["fresh-eyes-2026-06-01T03-28-21.md"])
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    assert rc == 0, "no report_glob → fire, never reconcile"
    assert len(calls) == 0


def test_main_no_reconcile_when_slot_timestamp_missing(mod, monkeypatch, tmp_path):
    """A slot with no prior 'timestamp' (first-fire / unstamped) must not be
    reconciled against a pre-existing report — it fires normally."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(report_glob="fresh-eyes-[0-9]*.md"),
        current=100,
        slot_value={"goals_count_at_last_fire": 70},  # no 'timestamp' key
    )
    _wire_reports(monkeypatch, mod, tmp_path, ["fresh-eyes-2026-06-01T03-28-21.md"])
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    assert rc == 0, "missing slot timestamp → fire (no reconcile)"
    assert len(calls) == 0


def test_main_does_not_reconcile_below_cadence(mod, monkeypatch, tmp_path):
    """diff < cadence → plain noop, reconcile branch not reached (no stamp)."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(report_glob="fresh-eyes-[0-9]*.md"),
        current=80,
        slot_value={"timestamp": "2026-05-26T17:12:17", "goals_count_at_last_fire": 70},
    )
    _wire_reports(monkeypatch, mod, tmp_path, ["fresh-eyes-2026-06-01T03-28-21.md"])
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    assert rc == 1, "diff=10 < cadence=25 → noop"
    assert len(calls) == 0, "below-cadence noop must not stamp"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
