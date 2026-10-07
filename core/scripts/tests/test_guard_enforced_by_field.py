"""test_guard_enforced_by_field.py -- .

`enforced_by` is an optional guardrail field: the gate(s) that MECHANICALLY check a
rule (a string or a list of strings; null / absent / "" / [] = honor-system). It lives
in two hand-kept-in-sync validators -- the daemon's (mind_api/src/store_registry.py,
the path every production write takes) and the CLI's (core/scripts/reasoning-bank.py)
-- so this pins, for BOTH of them:

  * the name is allowlisted and is NOT defaulted: a default would backfill a null
    onto every historical guardrail any later path rewrites, changing records the
    field has nothing to say about (the encoded_by precedent);
  * a never-set field and every CLEARED shape validate (rb-10037: a check gated on
    key presence makes a deliberately cleared value invalid while a never-set one
    stays valid);
  * a wrong TYPE is refused loudly and the stored value is never coerced (guard-3433);
  * the unknown-field gate is still live in the same call shape, so the acceptance
    above is not a disabled gate (negative control);
  * the two validators agree case by case -- on this axis only; parity is not global
    (severity normalisation is daemon-only);
  * every value shape the update-field wrapper can send reaches the validator as the
    type this table assumes (guard-3130: a field no writer can set is dead).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Daemon side -- a plain package import, no module-load side effects.
from mind_api.src import store_registry as daemon  # noqa: E402

# CLI side -- core/scripts/reasoning-bank.py is hyphen-named, so it is loaded by
# path. Importing it bootstraps WORLD/path resolution, so MIND_WORLD/MIND_AGENT
# are stashed FIRST and restored IMMEDIATELY after the load (guard-588: a
# module-level os.environ mutation must not leak into other tests of the session).
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
os.environ["MIND_WORLD"] = tempfile.mkdtemp(prefix="guard-enforced-by-test-")
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location("reasoning_bank", CORE_SCRIPTS / "reasoning-bank.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _base(**extra):
    """A guardrail that passes both validators with no optional field set."""
    rec = {
        "id": "guard-900",
        "rule": "Never do X without first doing Y.",
        "category": "framework",
        "trigger_condition": "before doing X",
        "source": "g-000-00",
        "status": "active",
    }
    rec.update(extra)
    return rec


def _daemon_validate(rec):
    daemon.validate_guard_record(None, rec)


def _cli_validate(rec):
    cli.validate_guard_record(rec)


VALIDATORS = (("daemon", _daemon_validate), ("cli", _cli_validate))

# label -> (extra fields, expected to validate)
CASES = {
    "never set":              ({}, True),
    "null":                   ({"enforced_by": None}, True),
    "empty string":           ({"enforced_by": ""}, True),
    "empty list":             ({"enforced_by": []}, True),
    "one gate":               ({"enforced_by": "core/scripts/example-gate.py"}, True),
    "several gates":          ({"enforced_by": ["core/scripts/a-gate.py", "hook:before-write"]}, True),
    "int":                    ({"enforced_by": 5}, False),
    "bool":                   ({"enforced_by": True}, False),
    "dict":                   ({"enforced_by": {"gate": "x"}}, False),
    "list with a non-string": ({"enforced_by": ["a-gate.py", 3]}, False),
    "nested list":            ({"enforced_by": [["a-gate.py"]]}, False),
}


def _outcome(validate, extra):
    """'ok', 'refused: <message head>' or 'crashed: <ExcType>' -- never raises."""
    try:
        validate(_base(**extra))
    except ValueError as e:
        return "refused: " + str(e)[:60]
    except Exception as e:  # a TypeError here would be a 500 in the daemon
        return "crashed: " + type(e).__name__
    return "ok"


def test_allowlisted_in_both_validators():
    assert "enforced_by" in daemon.GUARD_KNOWN_FIELDS, "daemon allowlist lacks enforced_by"
    assert "enforced_by" in cli.GUARD_KNOWN_FIELDS, "CLI allowlist lacks enforced_by"


def test_not_defaulted_in_either_validator():
    # A default flows into the allowlist AND backfills a null onto every record a
    # later path rewrites. The field must be allowed without being defaulted.
    assert "enforced_by" not in daemon.GUARD_DEFAULT_FIELDS
    assert "enforced_by" not in cli.GUARD_DEFAULT_FIELDS


@pytest.mark.parametrize("name,validate", VALIDATORS, ids=[n for n, _ in VALIDATORS])
@pytest.mark.parametrize("label", sorted(CASES))
def test_case_matrix(name, validate, label):
    extra, expect_ok = CASES[label]
    got = _outcome(validate, extra)
    if expect_ok:
        assert got == "ok", f"{name} refused a valid enforced_by ({label}): {got}"
    else:
        assert got.startswith("refused: Invalid enforced_by"), (
            f"{name} must refuse the wrong type ({label}) with the field's own "
            f"message, got: {got}")


@pytest.mark.parametrize("name,validate", VALIDATORS, ids=[n for n, _ in VALIDATORS])
def test_unknown_field_gate_is_still_live(name, validate):
    # NEGATIVE CONTROL: same call shape, a field that is NOT allowlisted. If this
    # validated, the acceptance cases above would be vacuous.
    got = _outcome(validate, {"not_a_guardrail_field": 1})
    assert got.startswith("refused: Unknown field"), f"{name}: {got}"


@pytest.mark.parametrize("name,validate", VALIDATORS, ids=[n for n, _ in VALIDATORS])
def test_validation_never_coerces_the_stored_value(name, validate):
    for label, (extra, expect_ok) in CASES.items():
        if not expect_ok or "enforced_by" not in extra:
            continue
        rec = _base(**extra)
        before = repr(rec["enforced_by"])
        validate(rec)
        assert repr(rec["enforced_by"]) == before, f"{name} changed enforced_by ({label})"


def test_validators_agree_case_by_case():
    # A one-sided edit (a type added to one validator only) fails here.
    for label, (extra, _) in CASES.items():
        got = {name: _outcome(validate, extra) for name, validate in VALIDATORS}
        assert got["daemon"] == got["cli"], f"validators disagree on {label!r}: {got}"


# --- the writer side -------------------------------------------------------
# guardrails-update-field.sh sends the value as a STRING and the daemon runs it
# through _parse_value. A reader of a field nothing can write is dead (guard-3130),
# so pin the type each shape the wrapper can send arrives as -- including the two
# it can send by accident, which the validator must then refuse rather than store.

WRAPPER_SHAPES = (
    ("null", None, True),
    ("[]", [], True),
    ("core/scripts/example-gate.py", "core/scripts/example-gate.py", True),
    ('["core/scripts/a-gate.py", "core/scripts/b-gate.py"]',
     ["core/scripts/a-gate.py", "core/scripts/b-gate.py"], True),
    ("5", 5, False),        # a numeric-looking token is parsed to int
    ("true", True, False),  # and a boolean word to bool
)


@pytest.mark.parametrize("text,parsed,valid", WRAPPER_SHAPES, ids=[s[0][:24] for s in WRAPPER_SHAPES])
def test_update_field_wrapper_shapes_reach_the_validator_as_assumed(text, parsed, valid):
    store_ep = pytest.importorskip("mind_api.src.endpoints.store")
    got = store_ep._parse_value(text)
    assert got == parsed and type(got) is type(parsed), (
        f"_parse_value({text!r}) -> {got!r}; the table assumes {parsed!r}")
    for name, validate in VALIDATORS:
        outcome = _outcome(validate, {"enforced_by": got})
        if valid:
            assert outcome == "ok", f"{name}: {outcome}"
        else:
            assert outcome.startswith("refused: Invalid enforced_by"), f"{name}: {outcome}"
