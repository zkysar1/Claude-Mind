"""test_target_state_g248119_intents.py — RUN / ADD-TO-SURFACE / CODE-TARGET-LEAD
carve-outs (g-248-119, target_state FP classes 2-3).

Background:
  After g-248-118 expanded MODIFY_INTENT_VERBS, a ledger analysis of
  world/goal-duplication-overrides.jsonl (142 SOLO / 565 ANY target_state
  overrides) isolated THREE more distinct FP classes a verb-list extension
  cannot reach — each an ORTHOGONAL detector wired as its own DEMOTE branch in
  gates/goal_duplication.py::_check_target_state:

  - CLASS 3a RUN-intent (is_run_intent, 4 SOLO / 16 ANY): the named script's
    presence is a precondition for running it, not run-completion. DEMOTE.
  - CLASS 2 ADD-TO-SURFACE (is_add_to_surface_intent, 14 SOLO / 48 ANY):
    "add X <prep> <cited surface>" — new deliverable integrated INTO an existing
    surface; the surface identifiers are context. 'add' stays excluded from
    MODIFY/BUILD verb sets; the INTEGRATION PREPOSITION is the discriminator so a
    bare "Add: new flag" stays blockable. DEMOTE.
  - CLASS 3b CODE-TARGET-LEAD (is_code_target_lead_intent, 48 of 93 noun-led
    SOLO — the LARGEST uncovered class, correcting rb-4732's smallest-tail
    estimate): the title LEADS with a code identifier (the file/symbol edited),
    present pre- and post-change. Create-guarded. DEMOTE.

  Combined with the prior detectors these raise target_state coverage to
  78% SOLO / 73% ANY (measured).

CRITICAL invariant (regression guard): is_build_or_test_authoring_intent's
"add X to Y -> None" boundary contract is UNCHANGED — the add-to-surface
detector is orthogonal, not a modification of is_build. The two
test_add_*_not_build tests in test_target_state_build_test_intent.py stay valid.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

# Neutralize gate telemetry so test runs do not append synthetic firings to the
# production meta/gate-firings.jsonl (same leak-prevention as the sibling tests).
ts_mod._gate_log = lambda *args, **kwargs: None

is_run = ts_mod.is_run_intent
is_add = ts_mod.is_add_to_surface_intent
is_code = ts_mod.is_code_target_lead_intent
is_build = ts_mod.is_build_or_test_authoring_intent
is_modify = ts_mod.is_modify_intent
is_read = ts_mod.is_read_intent
is_removal = ts_mod.is_removal_intent


# ─── CLASS 3a: RUN-intent → True ────────────────────────────────────────────

def test_run_execute_colonless():
    assert is_run("Execute provision_aws.py to stand up own-cloud AWS resources") is True


def test_run_leading_post_colon():
    assert is_run("Apply: run g-350-27-style forced-action verification in a DEV world") is True


def test_run_full_verb_set_leading():
    for verb in ts_mod.RUN_INTENT_VERBS:
        assert is_run(f"Apply: {verb} the benchmark") is True, verb


def test_run_launch_colonless():
    assert is_run("Launch the nightly regression sweep") is True


def test_run_negative_read():
    assert is_run("Investigate: why accuracy dropped") is False


def test_run_negative_add():
    assert is_run("Add: new feature flag") is False


def test_run_mid_clause_not_matched():
    # colon-less: run mid-clause is a subordinate action, leading word is 'scan'.
    assert is_run("Scan and run the orphaned-row check") is False


# ─── CLASS 2: ADD-TO-SURFACE → True (needs integration preposition) ─────────

def test_add_to_surface_composite():
    assert is_add("Idea: Add active-movement naturalness composite to MovementAnalyzer") is True


def test_add_flag_to_script():
    assert is_add("Apply: add --outcome to recurring-close.sh verify call") is True


def test_add_validation_for_field():
    assert is_add("Idea: add write-time validation for pipeline surprise field") is True


def test_add_support_to_store():
    assert is_add("Add: soft delete support to store.py") is True


def test_append_register_verbs():
    assert is_add("Apply: append a retry hook into the loader") is True
    assert is_add("Register characters in the DEV world") is True


def test_add_bare_no_preposition_stays_blockable():
    """The load-bearing negative: a bare add with NO integration preposition has
    no cited surface, so it stays blockable (create-that-may-be-a-dup)."""
    assert is_add("Add: new feature flag") is False


def test_add_negative_no_add_verb():
    assert is_add("Fix: the retry backoff to be exponential") is False   # 'to' present, no add verb


def test_add_negative_read():
    assert is_add("Investigate: why accuracy dropped") is False


# ─── CLASS 3b: CODE-TARGET-LEAD → True ──────────────────────────────────────

def test_code_lead_file_ext():
    assert is_code("Apply: blocker-recheck.py _wm_read_blockers to corruption-tolerant decode") is True


def test_code_lead_sh_ext():
    assert is_code("Idea: iteration-close.sh probe fills category='uncategorized' for some goals") is True


def test_code_lead_dotted_path():
    assert is_code("Idea: self_drift_gate.target_aspiration_id points at non-existent asp-244") is True


def test_code_lead_hyphenated():
    assert is_code("Apply: tree-fm-backfill --apply after the dry-run audit passed") is True


def test_code_lead_snake_case():
    assert is_code("Apply: fragment_archive CRUD wiring needs the index rebuild") is True


def test_code_lead_camelcase():
    assert is_code("Apply: MovementAnalyzer composite miscounts idle frames") is True


def test_code_lead_create_guard():
    """Create-guard: a create verb before the colon means the leading code-id is
    the NEW deliverable, not an edit target — stays blockable."""
    assert is_code("Build: new_module.py") is False
    assert is_code("Create: fragment_archive.py CRUD layer") is False


def test_code_lead_plain_word_negative():
    assert is_code("Apply: fix the retry backoff") is False


def test_code_lead_read_intent_negative():
    assert is_code("Investigate: why accuracy dropped") is False


def test_code_lead_colonless_plain():
    assert is_code("Reduce latency on the request path") is False


# ─── BOUNDARY: is_build_or_test contract UNCHANGED (regression guard) ────────

def test_build_add_to_y_contract_unchanged():
    """is_build_or_test_authoring MUST still return None for "add X to Y" — the
    add-to-surface detector is orthogonal, not a modification of is_build. If
    this fails, the two test_add_*_not_build tests would also break."""
    assert is_build("Idea: add rate limiting to login.py") is None
    assert is_build("Add: soft delete support to store.py") is None


# ─── Independence: the classifiers do not bleed into each other ─────────────

def test_new_detectors_independent_of_read_removal_modify():
    t_add = "Idea: add validation to the parser"
    t_run = "Execute provision_aws.py"
    t_code = "Apply: blocker-recheck.py corruption-tolerant decode"
    for t in (t_add, t_run, t_code):
        assert is_read(t) is False, t
        assert is_removal(t) is False, t
    # add-to-surface / code-lead titles are not modify (create-exclusion holds)
    assert is_modify(t_add) is False
    assert is_modify(t_code) is False


def test_empty_and_none():
    for fn in (is_run, is_add, is_code):
        assert fn("") is False
        assert fn(None) is False
