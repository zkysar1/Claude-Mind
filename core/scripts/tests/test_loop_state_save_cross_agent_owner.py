"""test_loop_state_save_cross_agent_owner.py —  Option 3 regression.

Pins the loop-state-save.py SCHEMA contract that cross_agent_owner is an
optional field accepted at init time. Closes Step 1 of g-115-978
(verification outcome: "loop-state-save.py SCHEMA accepts cross_agent_owner
field (test passes)").

Tested invariants:
  1. SCHEMA exposes cross_agent_owner as required=False, type=str with the
     lowercase agent-dir name pattern.
  2. init payload with cross_agent_owner='bravo' validates and writes the
     field into iteration-checkpoint.json.
  3. init payload without cross_agent_owner still validates (field is
     optional) and does not inject a null/empty value.
  4. cross_agent_owner with an uppercase or invalid character violates the
     pattern and is rejected loud.
  5. source enum stays strict at ('world', 'agent') — cross_agent_owner is
     the ONLY escape hatch for sibling-queue routing; widening source would
     defeat the orchestrator-entry design.
  6. init payload with cross-world ids (g-xw-<ts>-NN / asp-xw-<ts>) validates —
     the g-115-2757 fix (canonical widened them via g-115-1641; this SCHEMA is
     a duplicated copy that must mirror canonical).
  7. goal_id / aspiration_id SCHEMA patterns stay STRING-EQUAL to aspirations.py
     GOAL_ID_RE / ASP_ID_RE (SSOT) — this test fails loud on any future drift.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# loop-state-save resolves AGENT_DIR via MIND_AGENT; tests own this env
# capture/restore for the same Layer-1 pollution defense as
# test_goal_selector_cross_agent_pull.py.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")


def _import_module():
    """Re-import loop_state_save fresh so SCHEMA mutations between tests
    do not leak. The hyphen in the filename means we go through importlib."""
    spec = importlib.util.spec_from_file_location(
        "loop_state_save",
        str(CORE_SCRIPTS / "loop-state-save.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_exposes_cross_agent_owner_field():
    """SCHEMA must declare cross_agent_owner with the documented contract."""
    module = _import_module()
    schema = module.SCHEMA
    assert "cross_agent_owner" in schema, (
        "loop-state-save.py SCHEMA missing cross_agent_owner — g-115-978 Step 1 regression"
    )
    spec = schema["cross_agent_owner"]
    assert spec["required"] is False, "cross_agent_owner must be optional"
    assert spec["type"] is str, "cross_agent_owner must be str"
    assert "pattern" in spec, "cross_agent_owner must enforce the lowercase agent-dir pattern"


def test_source_enum_stays_strict():
    """source enum must NOT widen to accept cross-agent: — that's why
    cross_agent_owner exists (orchestrator-entry conversion, not source-widening)."""
    module = _import_module()
    schema = module.SCHEMA
    source_spec = schema["source"]
    assert "enum" in source_spec, "source must be enum-constrained"
    enum_values = set(source_spec["enum"])
    assert enum_values == {"world", "agent"}, (
        f"source enum widened to {enum_values} — g-115-978 contract violated"
    )


def test_init_accepts_cross_agent_owner(tmp_path, monkeypatch):
    """init payload with cross_agent_owner='bravo' lands in the checkpoint."""
    # Redirect AGENT_DIR to a tmpdir so we don't touch real session state.
    agent_dir = tmp_path / "agents" / "alpha"
    session_dir = agent_dir / "session"
    session_dir.mkdir(parents=True)

    module = _import_module()
    monkeypatch.setattr(module, "_agent_dir", lambda: agent_dir)
    monkeypatch.setattr(module, "_checkpoint_path", lambda: session_dir / "iteration-checkpoint.json")

    payload = {
        "goal_id": "g-115-978",
        "aspiration_id": "asp-115",
        "source": "agent",
        "phase": "selected",
        "selected_at": "2026-05-20T09:00:00",
        "cross_agent_owner": "bravo",
    }

    warns = module._validate_keys(payload, "init")
    assert warns == [], f"Validation failed for valid cross_agent_owner payload: {warns}"


def test_init_without_cross_agent_owner_still_valid(tmp_path, monkeypatch):
    """Normal (non-cross-agent) init payloads MUST continue to work — the
    cross_agent_owner field is optional, not required."""
    agent_dir = tmp_path / "agents" / "alpha"
    session_dir = agent_dir / "session"
    session_dir.mkdir(parents=True)

    module = _import_module()
    monkeypatch.setattr(module, "_agent_dir", lambda: agent_dir)
    monkeypatch.setattr(module, "_checkpoint_path", lambda: session_dir / "iteration-checkpoint.json")

    payload = {
        "goal_id": "g-115-817",
        "aspiration_id": "asp-115",
        "source": "world",
        "phase": "selected",
        "selected_at": "2026-05-20T09:00:00",
        # No cross_agent_owner — this is normal world-queue execution
    }

    warns = module._validate_keys(payload, "init")
    assert warns == [], f"Plain non-cross-agent payload rejected: {warns}"


def test_cross_agent_owner_pattern_rejects_uppercase():
    """The pattern restricts to lowercase agent-dir names (defensive — this
    field controls MIND_AGENT env injection, so an uppercase or
    special-character value could cause downstream subprocess oddities)."""
    module = _import_module()
    payload = {
        "goal_id": "g-115-978",
        "aspiration_id": "asp-115",
        "source": "agent",
        "phase": "selected",
        "selected_at": "2026-05-20T09:00:00",
        "cross_agent_owner": "Bravo",   # uppercase B violates ^[a-z]
    }

    warns = module._validate_keys(payload, "init")
    assert any("cross_agent_owner" in w and "pattern" in w for w in warns), (
        f"Uppercase cross_agent_owner should fail pattern validation; got warns={warns}"
    )


def test_init_accepts_cross_world_ids():
    """: cross-world ids (g-xw-<ts>-NN / asp-xw-<ts>) must validate.
    Canonical widened GOAL_ID_RE/ASP_ID_RE for them (g-115-1641) but this
    SCHEMA's duplicated copy wasn't mirrored, so every cross-world goal's
    checkpoint init/update was rejected (stuck at 0/1 forever)."""
    module = _import_module()
    payload = {
        "goal_id": "g-xw-20260719T110333-01",
        "aspiration_id": "asp-xw-20260719T110333",
        "source": "world",
        "phase": "selected",
        "selected_at": "2026-07-20T08:00:00",
    }
    warns = module._validate_keys(payload, "init")
    assert warns == [], f"cross-world id payload rejected: {warns}"


def test_id_patterns_match_canonical():
    """SSOT guard: the SCHEMA goal_id/aspiration_id patterns MUST stay
    string-equal to the canonical GOAL_ID_RE/ASP_ID_RE in aspirations.py.
    This SCHEMA is a duplicated copy; the g-115-2757 drift (canonical widened
    for the xw branch, this copy not) is exactly what this test prevents from
    recurring silently."""
    import aspirations  # canonical SSOT (core/scripts on sys.path via line 33)
    module = _import_module()
    assert module.SCHEMA["goal_id"]["pattern"] == aspirations.GOAL_ID_RE.pattern, (
        "loop-state-save goal_id pattern drifted from canonical GOAL_ID_RE "
        "(aspirations.py) — re-sync per g-115-2757"
    )
    assert module.SCHEMA["aspiration_id"]["pattern"] == aspirations.ASP_ID_RE.pattern, (
        "loop-state-save aspiration_id pattern drifted from canonical ASP_ID_RE "
        "(aspirations.py) — re-sync per g-115-2757"
    )


def teardown_module(module):
    """Restore MIND_AGENT if the test polluted it."""
    if _SAVED_AGENT is None:
        os.environ.pop("MIND_AGENT", None)
    else:
        os.environ["MIND_AGENT"] = _SAVED_AGENT
