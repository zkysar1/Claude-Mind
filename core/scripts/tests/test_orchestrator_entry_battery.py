"""Tests for orchestrator-entry-battery.py ().

Covers: clean run (nothing actionable), WM-slot detection, file-presence
detection (compact-checkpoint / pending-agents), fail-open on missing agent
binding, and JSON emit shape.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location(
    "orchestrator_entry_battery", SCRIPTS / "orchestrator-entry-battery.py"
)
oeb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oeb)


def _run(capsys, agent=None, wm_path=None, as_json=True):
    rc = oeb.run(agent, wm_path, as_json)
    out = capsys.readouterr().out
    assert rc == 0
    if as_json:
        return json.loads(out.splitlines()[0])
    return out


@pytest.fixture()
def fake_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "agents" / "testagent" / "session"
    state.mkdir(parents=True)
    import _paths

    monkeypatch.setattr(_paths, "agent_state_dir", lambda name: state)
    return state


def _empty_wm(tmp_path):
    p = tmp_path / "wm.yaml"
    p.write_text("slots: {}\n", encoding="utf-8")
    return str(p)


def test_clean_run_no_actionable(fake_state_dir, tmp_path, capsys):
    rep = _run(capsys, agent="testagent", wm_path=_empty_wm(tmp_path))
    assert rep["checks"] == 4
    assert rep["actionable"] == []
    assert "error" not in rep


def test_wm_slot_detection(fake_state_dir, tmp_path, capsys):
    wm = tmp_path / "wm.yaml"
    wm.write_text(
        'slots:\n  pending_phase_6_spark: {"goal_id": "g-x", "outcome": "deep"}\n'
        '  blocked_sleep_until: "2026-07-18T09:00:00"\n',
        encoding="utf-8",
    )
    rep = _run(capsys, agent="testagent", wm_path=str(wm))
    names = [e["name"] for e in rep["actionable"]]
    assert names == ["pending_phase_6_spark", "blocked_sleep_until"]
    spark = rep["actionable"][0]
    assert spark["phase"] == "-0.5c.2"
    assert spark["payload"]["goal_id"] == "g-x"


def test_file_presence_detection(fake_state_dir, tmp_path, capsys):
    (fake_state_dir / "compact-checkpoint.yaml").write_text("slots: {}\n")
    (fake_state_dir / "pending-agents.yaml").write_text("agents: []\n")
    rep = _run(capsys, agent="testagent", wm_path=_empty_wm(tmp_path))
    names = [e["name"] for e in rep["actionable"]]
    # Protocol order: pending-agents (-0.5a) before compact-checkpoint (-0.5c)
    assert names == ["pending_agents", "compact_checkpoint"]
    assert rep["actionable"][1]["payload"]["path"].endswith("compact-checkpoint.yaml")


def test_null_string_slot_not_actionable(fake_state_dir, tmp_path, capsys):
    wm = tmp_path / "wm.yaml"
    wm.write_text('slots:\n  pending_phase_6_spark: "null"\n', encoding="utf-8")
    rep = _run(capsys, agent="testagent", wm_path=str(wm))
    assert rep["actionable"] == []


def test_no_agent_binding_fails_open(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    rep = _run(capsys, agent=None, wm_path=_empty_wm(tmp_path))
    assert "no_agent_binding" in rep.get("error", "")
    assert rep["actionable"] == []


def test_human_output_footer(fake_state_dir, tmp_path, capsys):
    out = _run(capsys, agent="testagent", wm_path=_empty_wm(tmp_path), as_json=False)
    assert "all 4 entry checks clean" in out
    assert "always-run entry calls" in out
    assert "idle-tick.sh (-0.5e)" in out


def test_payload_truncation_preserves_short_machine_keys(fake_state_dir, tmp_path, capsys):
    """: a long `summary` written BEFORE set_at/expires_at must not
    push them past the 400-char human-mode cap — Phase -0.5a0's contract is
    that the payload on the line IS the read (no wm re-read)."""
    wm = tmp_path / "wm.yaml"
    long_summary = "x" * 600
    wm.write_text(
        "slots:\n"
        '  pending_phase_6_spark: {"goal_id": "g-y", "outcome": "deep",'
        f' "source": "world", "summary": "{long_summary}",'
        ' "expires_at": "2026-07-18T02:00:00", "set_at": "2026-07-18T01:00:00"}\n',
        encoding="utf-8",
    )
    out = _run(capsys, agent="testagent", wm_path=str(wm), as_json=False)
    line = next(l for l in out.splitlines() if "pending_phase_6_spark" in l)
    assert '"set_at": "2026-07-18T01:00:00"' in line
    assert '"expires_at": "2026-07-18T02:00:00"' in line
    assert '"goal_id": "g-y"' in line
    assert "…" in line  # the cap still fires — it eats only the prose tail


def test_payload_str_short_dict_order_stable():
    """All-short dicts pass through untruncated with keys intact."""
    s = oeb._payload_str({"a": 1, "b": "short"})
    assert s == '{"a": 1, "b": "short"}'


# --- the composed-caller seam (2026-08-18, ) -----------------------
#
# Every test above pins what the battery COMPUTES into `actionable`. None pinned
# whether that reaches iteration-open.py, which composes this script as its
# `entry-checks` stage and lifts payload["findings"] / payload["blind"] BY NAME.
# Emitting only `actionable` made this stage structurally silent inside
# `iteration-open.sh --apply`. It was one of TWO such stages (the other being
# precheck-sentinel-battery, fixed in the same pass); only
# precheck-always-run-battery already emitted `findings`, and that one working
# sibling kept the wrapper's FINDINGS section non-empty on most runs, so nothing
# ever looked dead — guard-4243.


def _load_iteration_open():
    s = importlib.util.spec_from_file_location(
        "iteration_open", SCRIPTS / "iteration-open.py"
    )
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_actionable_checks_reach_the_composed_caller(fake_state_dir, tmp_path, capsys):
    """An ACTIONABLE entry check must survive the hop into iteration-open."""
    wm = tmp_path / "wm.yaml"
    wm.write_text('slots:\n  blocked_sleep_until: "2026-08-18T09:00:00"\n',
                  encoding="utf-8")
    report = _run(capsys, wm_path=str(wm))
    assert report["actionable"], "precondition: the slot is actionable"

    itopen = _load_iteration_open()
    lifted = itopen._findings_from("entry-checks", report)
    assert len(lifted) == len(report["actionable"]), (
        "every actionable check must reach the composed caller -- if this is "
        "empty the stage is invisible inside iteration-open again and its run "
        "renders as an all-clear"
    )
    assert all(f["detail"] for f in lifted), "a finding with no detail is unactionable"

    # NEGATIVE CONTROL: the pre-fix payload must lift NOTHING. Without it this
    # test passes for a trivially green reason and cannot detect the regression
    # it exists for (guard-2903/guard-2536: assert the path was REACHED).
    pre_fix = {k: v for k, v in report.items() if k != "findings"}
    assert itopen._findings_from("entry-checks", pre_fix) == [], (
        "the pre-fix shape must lift ZERO, or this test cannot fail for the "
        "right reason"
    )


def test_fail_open_reports_blind_rather_than_clean(tmp_path, capsys, monkeypatch):
    """Fail-open must not read as fail-SILENT to the composed caller.

    Zero findings AND zero blind is indistinguishable from a genuinely clean
    run, which would defeat iteration-open's own "N lane(s) blind" branch
    (guard-4093).
    """
    monkeypatch.delenv("MIND_AGENT", raising=False)
    report = _run(capsys, agent="", wm_path=str(tmp_path / "nope.yaml"))
    assert report.get("error"), "no agent binding is an error condition"

    itopen = _load_iteration_open()
    assert itopen._blind_from("entry-checks", report), (
        "an errored battery must surface as BLIND, else its failure renders "
        "as an all-clear"
    )
    assert itopen._findings_from("entry-checks", report) == [], (
        "and it must not manufacture findings it never computed"
    )
