"""test_loop_state_save_init_self_sufficient.py —  regression.

`loop-state-save.sh init` had two defects that pointed the same direction:
the remedy it PRINTED could not be run as printed, and one of its refusals
announced itself with a word that meant the opposite of what it did.

  1. The only init form demanded a hand-built JSON object carrying all five
     required keys, so `_warn_checkpoint_missing`'s "Re-anchor with ..." line
     was a template, not a command. `--goal-id <id>` now infers the whole
     payload, and the printed remedy is literally what these tests run.

  2. An unknown key produced `WARN[...] unknown key 'x'` AND rc=1 AND no
     write. The caller (aspirations-claim.sh) assumes the anchor landed, so a
     stray key silently removed the checkpoint for the REST of the session —
     the exact outage `_warn_checkpoint_missing` exists to report. Unknown
     keys are now dropped + warned (non-fatal, which is what "WARN" promises);
     genuine schema violations still refuse and now SAY "REFUSED ... wrote
     NOTHING".

Every test below asserts rc AND message text together, because the whole
defect in (2) was that those two disagreed and each looked right alone.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

os.environ.setdefault("MIND_AGENT", "alpha")


def _import_module():
    """Hyphenated filename — import through importlib, fresh per test so a
    SCHEMA mutation in one test cannot leak into the next."""
    spec = importlib.util.spec_from_file_location(
        "loop_state_save", str(CORE_SCRIPTS / "loop-state-save.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wire(tmp_path, monkeypatch):
    """Redirect the checkpoint at a tmpdir so no test touches live session state."""
    module = _import_module()
    agent_dir = tmp_path / "agents" / "alpha"
    session_dir = agent_dir / "session"
    session_dir.mkdir(parents=True)
    cp = session_dir / "iteration-checkpoint.json"
    monkeypatch.setattr(module, "_agent_dir", lambda: agent_dir)
    monkeypatch.setattr(module, "_checkpoint_path", lambda: cp)
    return module, cp


def _args(**kw):
    """cmd_init reads json/goal_id/source/phase; default them all to None so a
    test states only the flag it is about."""
    base = {"json": None, "goal_id": None, "source": None, "phase": None}
    base.update(kw)
    return argparse.Namespace(**base)


# --- 1. the remedy is runnable AS PRINTED --------------------------------

def test_goal_id_alone_anchors_on_the_first_attempt(tmp_path, monkeypatch):
    """The whole payload is optional: a goal id is enough, first try.

    This is g-115-3704's check 1. Before the fix the minimum viable command
    was a five-key JSON object typed by hand."""
    module, cp = _wire(tmp_path, monkeypatch)

    rc = module.cmd_init(_args(goal_id="g-115-3704"))

    assert rc == 0
    assert cp.exists(), "goal-id form must WRITE the checkpoint, not just validate"
    data = json.loads(cp.read_text(encoding="utf-8"))
    for req in module.REQUIRED_INIT_KEYS:
        assert req in data, f"inferred payload is missing required key {req!r}"
    assert data["goal_id"] == "g-115-3704"
    assert data["aspiration_id"] == "asp-115"
    assert data["source"] == "world"
    assert data["phase"] == "selected"
    # The inferred payload must survive the validator it will face on update.
    assert module._validate_keys(data, "init") == []


def test_the_printed_remedy_names_the_flag_this_file_tests(tmp_path, monkeypatch):
    """`_warn_checkpoint_missing` is where an operator learns how to recover.
    If its text stops naming --goal-id, the remedy silently reverts to the
    un-runnable template that motivated this goal — so pin the text to the
    capability, not just the capability to itself."""
    import contextlib
    import io

    module, cp = _wire(tmp_path, monkeypatch)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        module._warn_checkpoint_missing(cp, argparse.Namespace(set=["phase=executed"]))
    printed = buf.getvalue()

    assert "--goal-id" in printed, "the printed remedy must name the self-sufficient form"
    assert "loop-state-save.sh init --goal-id" in printed


# --- 2. unknown key: dropped, warned, and the anchor SURVIVES -------------

def test_unknown_key_is_dropped_and_the_checkpoint_is_still_written(
    tmp_path, monkeypatch, capsys
):
    """rc AND text together: rc=0, the word WARN, and a file on disk.

    The regression this guards is not the warning — it is losing the anchor.
    A typo'd key used to cost the whole checkpoint for the rest of the
    session, which is strictly worse than the typo."""
    module, cp = _wire(tmp_path, monkeypatch)

    payload = {
        "goal_id": "g-115-3704",
        "aspiration_id": "asp-115",
        "source": "world",
        "phase": "selected",
        "selected_at": "2026-08-31T04:00:00",
        "bogus_key_probe": "x",
    }
    rc = module.cmd_init(_args(json=json.dumps(payload)))
    err = capsys.readouterr().err

    assert rc == 0, "an unknown key must NOT cost the anchor"
    assert cp.exists()
    assert "WARN[loop-state-save:init]" in err
    assert "bogus_key_probe" in err
    assert "DROPPED" in err
    assert "REFUSED" not in err, "a dropped key is not a refusal"

    data = json.loads(cp.read_text(encoding="utf-8"))
    assert "bogus_key_probe" not in data, "the unknown key must not be persisted"
    for req in module.REQUIRED_INIT_KEYS:
        assert req in data, "dropping an unknown key must not disturb known keys"


# --- 3. a real violation REFUSES, and says so ----------------------------

def test_a_genuine_violation_says_REFUSED_never_WARN(tmp_path, monkeypatch, capsys):
    """rc=1 announced as WARN is the defect. rc and word must agree."""
    module, cp = _wire(tmp_path, monkeypatch)

    rc = module.cmd_init(_args(json=json.dumps({"goal_id": "g-115-3704"})))
    err = capsys.readouterr().err

    assert rc == 1
    assert "REFUSED[loop-state-save:init]" in err
    assert "wrote NOTHING" in err
    assert "WARN[loop-state-save:init]" not in err, (
        "rc=1 + 'WARN' is the exact disagreement g-115-3704 removed"
    )
    assert not cp.exists(), "'wrote NOTHING' must be literally true"


def test_a_refusal_leaves_an_existing_checkpoint_byte_identical(
    tmp_path, monkeypatch
):
    """'checkpoint unchanged' is a claim about an EXISTING file, which the
    absent-file assertion above cannot test."""
    module, cp = _wire(tmp_path, monkeypatch)
    assert module.cmd_init(_args(goal_id="g-115-3704")) == 0
    before = cp.read_bytes()

    rc = module.cmd_init(_args(json=json.dumps({"goal_id": "g-115-9999"})))

    assert rc == 1
    assert cp.read_bytes() == before


def test_every_violation_is_reported_in_one_pass(tmp_path, monkeypatch, capsys):
    """One refusal line per problem, from a single attempt — so an operator
    fixes the payload once instead of rediscovering it key by key."""
    module, cp = _wire(tmp_path, monkeypatch)

    rc = module.cmd_init(
        _args(json=json.dumps({"goal_id": "g-115-3704", "intent_state": "bogus_enum"}))
    )
    err = capsys.readouterr().err

    assert rc == 1
    for missing in ("aspiration_id", "source", "phase", "selected_at"):
        assert missing in err, f"{missing} not reported in the same pass"
    assert "bogus_enum" in err, "the enum violation must be reported alongside the missing keys"


# --- 4. inference covers exactly the shapes SCHEMA accepts ---------------

@pytest.mark.parametrize(
    "goal_id,expected",
    [
        ("g-115-3704", "asp-115"),
        ("g-115-37", "asp-115"),            # 2-digit tail
        ("g-115-3704-a", "asp-115"),        # decomposed child
        ("g-xw-20260830T041617-01", "asp-xw-20260830T041617"),  # cross-world
        ("not-a-goal", None),
        ("g-115", None),
        ("", None),
    ],
)
def test_inference_covers_every_shape_the_schema_accepts(goal_id, expected):
    module = _import_module()
    assert module._infer_aspiration_id(goal_id) == expected


def test_inference_output_satisfies_the_aspiration_id_pattern():
    """The inferred value must pass the very SCHEMA check it is written to
    satisfy — otherwise the goal-id form would refuse its own payload."""
    import re

    module = _import_module()
    pattern = module.SCHEMA["aspiration_id"]["pattern"]
    for goal_id in ("g-115-3704", "g-115-3704-a", "g-xw-20260830T041617-01"):
        asp = module._infer_aspiration_id(goal_id)
        assert asp is not None
        assert re.match(pattern, asp), f"{asp!r} from {goal_id!r} fails SCHEMA pattern"


def test_an_unrecognised_goal_id_refuses_rather_than_inventing_an_aspiration(
    tmp_path, monkeypatch, capsys
):
    """Guessing an aspiration for an id we cannot parse would write a
    plausible-looking anchor pointing at nothing. Omit the key and let the
    required-key check refuse, loudly."""
    module, cp = _wire(tmp_path, monkeypatch)

    rc = module.cmd_init(_args(goal_id="not-a-goal"))
    err = capsys.readouterr().err

    assert rc == 1
    assert "aspiration_id" in err
    assert not cp.exists()


# --- 5. composition with the explicit form ------------------------------

def test_explicit_json_overrides_inferred_fields(tmp_path, monkeypatch):
    """--json beside --goal-id refines rather than conflicts: explicit wins."""
    module, cp = _wire(tmp_path, monkeypatch)

    rc = module.cmd_init(
        _args(goal_id="g-115-3704", json=json.dumps({"phase": "executed"}))
    )

    assert rc == 0
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["phase"] == "executed"
    assert data["aspiration_id"] == "asp-115", "inference still fills what --json omits"


def test_source_and_phase_flags_refine_the_inferred_payload(tmp_path, monkeypatch):
    module, cp = _wire(tmp_path, monkeypatch)

    rc = module.cmd_init(_args(goal_id="g-115-3704", source="agent", phase="executed"))

    assert rc == 0
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["source"] == "agent"
    assert data["phase"] == "executed"
