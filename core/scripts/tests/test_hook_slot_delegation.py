"""F4 (): hook-slot-contract-check must not grade PSEUDOCODE against a
BASH syntax rule.

Requirement 2 of the Pattern B hook-slot contract is written as bash
(`test -f "$WORLD_DIR/conventions/<slot>.md"`), but most consumers are markdown
— SKILL.md pseudocode and digests — which express the same absence-handling in
three other legitimate forms. Grading those against the bash rule reported 3 of
3 such rows as "breaks every fresh world" when all three were MEASURED returning
rc=0 with the slot absent (zeta, cc-02, 2026-09-07):

    domain-calendar      generation_phase_gate.py     -> decision:"fail-open", rc=0
    commons-retrieval    load-conventions.sh <absent> -> rc=0, 0 bytes
    outcome-observation  outcome-observation-run.sh   -> rc=0 on a bare fresh world

The negative controls below are the load-bearing half: a widening that accepts
everything is not a fix, it is the same defect pointing the other way. They pin
that a bare reference, an ungated read, a far-away fail-open note, an untied
script and a nonexistent callee all still fail (guard-5889, guard-2201).
"""
import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_TARGET = _ROOT / "core" / "scripts" / "hook-slot-contract-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("hs_contract_check", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hs():
    return _load()


# --- negative controls: these MUST stay `broken` -------------------------
@pytest.mark.parametrize("label,body,slot", [
    ("bare reference", "Read world/conventions/myslot.md and use it.\n", "myslot"),
    ("ungated cat", 'Bash: cat "$WORLD_DIR/conventions/myslot.md"\n', "myslot"),
    ("slot absent", "nothing here at all\n", "myslot"),
    ("fail-open outside the window",
     "reference myslot here\n" + "filler\n" * 20 + "this fails open\n", "myslot"),
    ("script not tied to the slot",
     "myslot is read here\nBash: tree-update.sh --something\n", "myslot"),
    ("tied script does not exist",
     "myslot here\nBash: no-such-script-xyz.sh myslot\n", "myslot"),
])
def test_no_false_acceptance(hs, label, body, slot):
    assert hs.delegation_evidence(body, slot, hs.PROJECT_ROOT) is None, label


# --- positive controls: the three real forms ----------------------------
@pytest.mark.parametrize("label,body,slot", [
    ("pseudocode existence gate",
     "Bash: load-conventions.sh myslot -> IF path returned: Read it\n", "myslot"),
    ("documented fail-open",
     "the myslot hook fires here\nWrapper exits 0 unconditionally - fail-open.\n",
     "myslot"),
    ("delegation, slot on the invocation line",
     "myslot referenced\nBash: bash core/scripts/load-conventions.sh myslot\n",
     "myslot"),
    ("delegation, slot in the callee name",
     "the outcome-observation hook\nbash core/scripts/outcome-observation-run.sh a b\n",
     "outcome-observation"),
])
def test_real_forms_accepted(hs, label, body, slot):
    assert hs.delegation_evidence(body, slot, hs.PROJECT_ROOT) is not None, label


def test_window_is_sized_from_measured_gaps(hs):
    """The window is 12 because the largest MEASURED ref->evidence gap is 5
    (domain-calendar ref@116 -> "Fail-open in both directions"@121). Widening it
    without a new measurement makes the check unfalsifiable (guard-1451)."""
    assert hs.DELEGATION_WINDOW == 12


def test_live_table_has_no_contract_violations(hs):
    """The live registry must produce zero `broken` rows. This is the regression
    pin for the 3/3 false-positive state: if a future edit re-breaks the
    pseudocode forms, this goes red instead of quietly filing goals."""
    assert hs.main() == 0
