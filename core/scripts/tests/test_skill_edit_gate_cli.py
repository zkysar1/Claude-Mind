"""skill_edit_gate.py `gate` CLI: a malformed call is exit 2, never a verdict.

Measured 2026-08-30 on a coach worker: forge-skill Step 3.5 was called with
`"safety":"g"` (copied from the SKILL.md's own `<g|a|p>` placeholder). The
validator raised ValueError, the process died with a traceback and exit 1 --
the BLOCK code -- so the caller read its typo as a quality rejection. These
tests pin: full words reach the gate; abbreviations, bad JSON and a missing
dimension are refused on exit 2 with the vocabulary named, no JSON verdict on
stdout, no traceback, and nothing handed to telemetry.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("skill_edit_gate_under_test", SCRIPTS / "skill_edit_gate.py")
seg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seg)

FULL_WORDS = {d: "good" for d in seg.DIMS}


class _Verdict:
    def __init__(self, passed):
        self.passed = passed

    def as_dict(self):
        return {"passed": self.passed, "stub": True}


def _run(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        seg._cli_gate(argv)
    captured = capsys.readouterr()
    return exc.value.code, captured.out, captured.err


@pytest.fixture
def telemetry_spy(monkeypatch):
    """Stub the lazily-imported _gate_log so a refusal provably logs nothing."""
    calls = []
    stub = types.ModuleType("_gate_log")
    stub.log = lambda *a, **k: calls.append((a, k))
    monkeypatch.setitem(sys.modules, "_gate_log", stub)
    return calls


def test_abbreviated_judgment_is_refused_on_exit_2_not_read_as_block(capsys, telemetry_spy):
    abbreviated = dict(FULL_WORDS, safety="g")
    rc, out, err = _run(["--new-judgments", json.dumps(abbreviated)], capsys)
    assert rc == 2
    assert out == ""                       # no verdict JSON for a malformed call
    assert "Traceback" not in err
    assert "not a verdict" in err
    assert "good|average|poor" in err and "'g'" in err
    assert telemetry_spy == []             # nothing logged as a gate firing


def test_bad_json_is_refused_on_exit_2(capsys, telemetry_spy):
    rc, out, err = _run(["--new-judgments", "{not json"], capsys)
    assert rc == 2
    assert out == "" and "Traceback" not in err and "not a verdict" in err
    assert telemetry_spy == []


def test_missing_dimension_is_refused_on_exit_2(capsys, telemetry_spy):
    partial = {k: v for k, v in FULL_WORDS.items() if k != "cost_awareness"}
    rc, out, err = _run(["--new-judgments", json.dumps(partial)], capsys)
    assert rc == 2
    assert "cost_awareness" in err and "not a verdict" in err
    assert telemetry_spy == []


def test_non_object_judgments_are_refused_on_exit_2(capsys, telemetry_spy):
    rc, out, err = _run(["--new-judgments", '["good", "good"]'], capsys)
    assert rc == 2
    assert "not a verdict" in err
    assert telemetry_spy == []


def test_full_words_reach_the_gate_and_pass_exits_0(capsys, monkeypatch):
    seen = {}

    def fake_run_gate(old_j, new_j, **kw):
        seen.update(old=old_j, new=new_j, kw=kw)
        return _Verdict(True)

    monkeypatch.setattr(seg, "run_gate", fake_run_gate)
    rc, out, err = _run(["--new-judgments", json.dumps(FULL_WORDS),
                         "--skill-name", "widget-helper", "--caller", "test"], capsys)
    assert rc == 0
    assert json.loads(out)["passed"] is True
    assert seen["new"] == FULL_WORDS and seen["old"] == seg.BASELINE_JUDGMENT
    assert seen["kw"]["skill_name"] == "widget-helper"


def test_block_verdict_still_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(seg, "run_gate", lambda *a, **k: _Verdict(False))
    rc, out, err = _run(["--new-judgments", json.dumps(FULL_WORDS)], capsys)
    assert rc == 1
    assert json.loads(out)["passed"] is False
