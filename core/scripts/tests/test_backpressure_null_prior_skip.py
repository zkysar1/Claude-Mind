"""Regression pins for the rb-4024 null-prior rollback class ().

Defect chain (2026-07-18): meta-backpressure emitted a rollback_action whose
rollback_to was None (the tracked change mc-078 had old_value null — the field
did not exist before the change). cmd_backpressure did
str(action.get("rollback_to", "")) — str(None) == "None" — and meta-set wrote
the literal STRING "None" into weights.opportunity_boost (mc-081), crashing
goal-selector.load_weights float() for every agent on the box.

Fixes pinned here (g-115-2677 supersedes the g-115-2609/2611 "null" mapping —
zeta g-115-2642 root-cause rb-4159):
  - cmd_backpressure maps a rollback_to of None OR the corrupt strings
    "None"/"null" to the NUMERIC floor "0.0" — a type-safe revert-to-opted-out
    for an add-from-absent criterion. 0.0 is kept by load_weights (numeric) and
    contributes raw*0.0 = 0 to the weighted sum, opting the criterion out
    IDENTICALLY to an absent key — but with NO recurring non-numeric warning,
    and every future monitor records a numeric old_value (never None), so the
    restore->re-null thrash (mc-081/083, 2 rollbacks = dead-end) is broken at
    the root. The prior "null" mapping fixed the fleet crash but not the thrash.
    The literal string "None" must never reach meta-set.
  - A numeric rollback_to still rolls back normally (guard is surgical).
  - goal_selector.load_weights drops a non-numeric weight value loudly
    instead of crashing (defense-in-depth for any other poisoning writer).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "state-update-audit.py"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "state_update_audit_np", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_update_audit_np"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


def _args(**kw):
    base = dict(learning_value=0.5)
    base.update(kw)
    return argparse.Namespace(**base)


def _bp_payload(rollback_to):
    return json.dumps({
        "rollback_actions": [{
            "strategy_file": "goal-selection-strategy.yaml",
            "field": "weights.opportunity_boost",
            "rollback_to": rollback_to,
            "failed_value": 0.5,
            "meta_change_id": "mc-test",
            "reason": "test regression",
        }],
        "dead_end_candidates": [],
        "graduated": [],
    })


def _patched_run(bp_payload, calls):
    """Record every _run argv; answer the backpressure check with bp_payload."""
    def fake_run(argv, input_text=None):
        calls.append(list(argv))
        if argv[0] == "meta-backpressure.sh":
            return bp_payload, "", 0
        return "", "", 0
    return fake_run


def _meta_sets(calls):
    return [c for c in calls if c[0] == "meta-set.sh"]


def test_null_prior_rolls_back_to_numeric_floor(monkeypatch):
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload(None), calls))
    result = MOD.cmd_backpressure(_args())
    assert result["rollbacks_applied"] == ["weights.opportunity_boost"]
    meta_sets = _meta_sets(calls)
    assert len(meta_sets) == 1
    # add-key rollback -> NUMERIC floor 0.0 (): type-safe, opts out
    # identically to an absent key, breaks the re-null thrash. Neither the
    # literal string "None" nor a YAML null may be the value argument.
    assert meta_sets[0][3] == "0.0"


def test_string_none_prior_maps_to_numeric_floor(monkeypatch):
    # A monitor created while the bug was live may carry the corrupt string
    # "None" as old_value; rolling back TO it verbatim would re-poison the
    # field forever. The mapping heals it to the numeric floor 0.0 instead.
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload("None"), calls))
    MOD.cmd_backpressure(_args())
    meta_sets = _meta_sets(calls)
    assert len(meta_sets) == 1
    assert meta_sets[0][3] == "0.0"


def test_string_null_prior_maps_to_numeric_floor(monkeypatch):
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload("null"), calls))
    MOD.cmd_backpressure(_args())
    meta_sets = _meta_sets(calls)
    assert len(meta_sets) == 1
    assert meta_sets[0][3] == "0.0"


def test_numeric_prior_rollback_still_applies(monkeypatch):
    calls = []
    monkeypatch.setattr(
        MOD, "_run", _patched_run(_bp_payload(0.3), calls))
    result = MOD.cmd_backpressure(_args())
    assert result["rollbacks_applied"] == ["weights.opportunity_boost"]
    meta_sets = _meta_sets(calls)
    assert len(meta_sets) == 1
    assert meta_sets[0][3] == "0.3"


# ── load_weights non-numeric drop (goal-selector.py) ─────────────────────────

def test_load_weights_drops_non_numeric(tmp_path, monkeypatch, capsys):
    import yaml as _yaml

    strategy = tmp_path / "goal-selection-strategy.yaml"
    strategy.write_text(_yaml.safe_dump({
        "weights": {"priority": 3.0, "opportunity_boost": "None"}
    }), encoding="utf-8")

    gs_path = SCRIPTS_DIR / "goal-selector.py"
    src = gs_path.read_text(encoding="utf-8")
    # Execute only up to (and including) load_weights + the module-level
    # WEIGHTS assignment's dependencies by extracting the function — the full
    # module import runs the selector against live agent state. Instead, pull
    # load_weights out with its KNOWN_CRITERIA/META_GOAL_SELECTION globals
    # stubbed.
    ns = {}
    fn_start = src.index("def load_weights():")
    fn_end = src.index("WEIGHTS = load_weights()")
    exec(compile(src[fn_start:fn_end], str(gs_path), "exec"), {
        "yaml": _yaml,
        "sys": sys,
        "META_GOAL_SELECTION": strategy,
        "KNOWN_CRITERIA": {"priority", "opportunity_boost"},
    }, ns)
    weights = ns["load_weights"]()
    assert weights == {"priority": 3.0}
    err = capsys.readouterr().err
    assert "non-numeric" in err
    assert "opportunity_boost" in err
