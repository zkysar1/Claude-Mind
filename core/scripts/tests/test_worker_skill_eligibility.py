""": a worker Body must not claim a goal whose SKILL is a reducer-only
lifecycle stage.

The defect this pins: goal-selector.sh offered a worker `g-001-05` ("Run
hippocampal replay", skill `/replay --sharp-wave`) as the sanctioned top pick.
`/replay` calls guardrails-add.sh, and LIFECYCLE_DISPOSITIONS["replay"] is
reducer-only-by-design -- so a worker following the banner would have written
guardrails derived from its own UNMERGED state, the Nth-reducer defect the
convergence forbids. Nothing bridged a goal's `skill` field to the disposition
table, so nothing could refuse it.

WHAT THESE TESTS ARE WEIGHTED TOWARD. Per guard-2860, the test proving a
role-gate carve-out WORKS cannot fail in the dangerous direction; the
load-bearing tests are the EXCLUSIONS. So the pinned-eligible cases (`/tree`,
`/agent-completion-report`) and the unknown-skill default carry more weight here
than the refusal cases -- a wrong refusal strands real worker work silently,
and `/tree` is a skill this very Body used for sanctioned goal-directed work.

Daemon-safe (no daemon_integration marker -- pure contract arithmetic, no daemon,
no filesystem writes).

Run:
  python -m pytest core/scripts/tests/test_worker_skill_eligibility.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


we = _load("worker_execute", "worker_execute.py")


# ----------------------- the incident -----------------------

def test_the_incident_case_is_refused():
    """`/replay --sharp-wave` -- the exact skill string from ."""
    v = we.skill_eligibility("/replay --sharp-wave")
    assert v.eligible is False
    assert v.skill == "/replay"
    assert v.stage == "replay"
    assert v.disposition == we.REDUCER_ONLY_BY_DESIGN
    # The reason must name the stage AND the consequence -- a bare "refused"
    # would leave the next worker with nothing to act on.
    assert "replay" in v.reason
    assert "unmerged" in v.reason


def test_the_second_incident_case_is_refused():
    """`/drain-temp` -- , met live 2026-08-11 (alpha, cc-08).

    Pinned EXPLICITLY rather than left to the derived test below, for the reason
    that test cannot cover: it builds its set FROM SKILL_LIFECYCLE_STAGE, so
    deleting the /drain-temp row leaves it green over a smaller set. That is the
    guard-1943 shape -- a test that asserts the current table cannot tell a
    populated table from a depopulated one. This one fails on removal.

    The goal reached rank 2 of 960 carrying starvation_boost 4.00 with an EMPTY
    skill field -- "/drain-temp" appears only in its description ("Invoke
    /drain-temp"), which no bridge reads -- so the fail-open returned green and
    proved nothing. The skill's own front matter is what settles it: it encodes
    into knowledge tree / reasoning bank / guardrails / experience, and it is
    invoked from aspirations-precheck, a phase workers skip.
    """
    v = we.skill_eligibility("/drain-temp")
    assert v.eligible is False
    assert v.skill == "/drain-temp"
    assert v.stage == "reducer-iteration"
    assert v.disposition == we.REDUCER_ONLY_BY_DESIGN
    assert "unmerged" in v.reason


def test_every_mapped_reducer_only_skill_is_refused():
    refused = [s for s, stage in we.SKILL_LIFECYCLE_STAGE.items()
               if we.LIFECYCLE_DISPOSITIONS[stage].kind == we.REDUCER_ONLY_BY_DESIGN]
    assert refused, "the bridge must refuse at least the /replay incident case"
    for skill in refused:
        assert we.skill_eligibility(skill).eligible is False, skill


# ----------------------- the pinned negatives (load-bearing) -----------------------

@pytest.mark.parametrize("skill", ["/tree", "/agent-completion-report"])
def test_pinned_eligible_skills_are_not_refused(skill):
    """The exclusions guard-2860 calls load-bearing.

    A naive "this skill calls an encoding script -> refuse it" rule would refuse
    /tree, which is how a worker does GOAL-DIRECTED artifact creation from
    content supplied in the goal (measured: g-115-5073 encoded a principal
    directive carried in full in the goal record). /agent-completion-report has
    zero encoding calls and is minimum_mode reader; the reducer-only phase is
    complete-review, a DIFFERENT skill (/aspirations-complete-review).
    """
    v = we.skill_eligibility(skill)
    assert v.eligible is True, f"{skill} must stay worker-eligible"
    assert "PINNED" in v.reason


def test_pinned_eligible_and_refused_sets_are_disjoint():
    assert we.SKILL_ELIGIBLE_DESPITE_ENCODING.isdisjoint(we.SKILL_LIFECYCLE_STAGE)


def test_similar_names_do_not_collide():
    """/agent-completion-report vs /aspirations-complete-review.

    Both are 'completion review'-shaped names; exactly one is reducer-only. A
    substring or prefix match would conflate them, which is the specific
    sloppiness the pinned set exists to prevent.
    """
    assert we.skill_eligibility("/agent-completion-report").eligible is True
    assert we.skill_eligibility("/aspirations-complete-review").eligible is False


# ----------------------- the default (fail-open, deliberately) -----------------------

@pytest.mark.parametrize("skill", [None, "", "   "])
def test_no_skill_is_eligible(skill):
    v = we.skill_eligibility(skill)
    assert v.eligible is True
    assert v.skill is None


@pytest.mark.parametrize("skill", ["/run-processor", "/forge-skill", "/decompose",
                                   "bash core/scripts/x.sh", "/never-heard-of-it"])
def test_unknown_skill_is_eligible(skill):
    """919 of 938 live candidates carry no skill at all (cc-08, 2026-08-10).

    A fail-closed default would refuse a worker essentially everything and
    strand the role, so the refusal set is a POSITIVE list. This test is the
    pin on that decision -- flipping the default would break it loudly.
    """
    assert we.skill_eligibility(skill).eligible is True


# ----------------------- normalization -----------------------

@pytest.mark.parametrize("raw,expected", [
    ("/replay --sharp-wave", "/replay"),
    ("/replay", "/replay"),
    ("replay", "/replay"),                    # goal records carry both shapes
    ("  /replay   --selective ", "/replay"),
    ("/review-hypotheses --resolve", "/review-hypotheses"),
    (None, None),
    ("", None),
    ("   ", None),
])
def test_normalize_skill(raw, expected):
    assert we.normalize_skill(raw) == expected


def test_slashless_form_is_refused_too():
    """A bridge that matched only the slashed form would refuse half its
    population and read as working."""
    assert we.skill_eligibility("replay").eligible is False


# ----------------------- derivation, not restatement (guard-2676) -----------------------

def test_disposition_is_derived_from_the_table_not_restated(monkeypatch):
    """Flip the stage's disposition; eligibility MUST follow.

    This is the no-transcription proof. If the refusal were a second hardcoded
    list of reducer-only skills, /replay would stay refused after the stage it
    names is redeclared worker-eligible, and the two facts would drift silently.
    """
    assert we.skill_eligibility("/replay").eligible is False
    flipped = dict(we.LIFECYCLE_DISPOSITIONS)
    flipped["replay"] = we.LifecycleDisposition(
        kind=we.WORKER_ONLY, target="a hypothetical worker-side replay",
        why="test-only redeclaration to prove derivation")
    monkeypatch.setattr(we, "LIFECYCLE_DISPOSITIONS", flipped)
    assert we.skill_eligibility("/replay").eligible is True


# ----------------------- contract completeness -----------------------

def test_bridge_is_clean_on_the_live_table():
    assert we.lifecycle_gaps() == []


def test_gap_reported_for_noncanonical_stage(monkeypatch):
    monkeypatch.setitem(we.SKILL_LIFECYCLE_STAGE, "/bogus", "no-such-stage")
    gaps = we.lifecycle_gaps()
    assert any("/bogus" in g and "not canonical" in g for g in gaps), gaps


def test_gap_reported_for_unnormalized_key(monkeypatch):
    monkeypatch.setitem(we.SKILL_LIFECYCLE_STAGE, "replay-no-slash", "replay")
    gaps = we.lifecycle_gaps()
    assert any("normalized form" in g for g in gaps), gaps


def test_gap_reported_when_a_skill_is_in_both_sets(monkeypatch):
    """The pin that makes the negatives survive a future edit."""
    monkeypatch.setattr(
        we, "SKILL_ELIGIBLE_DESPITE_ENCODING",
        frozenset(we.SKILL_ELIGIBLE_DESPITE_ENCODING | {"/replay"}))
    gaps = we.lifecycle_gaps()
    assert any("BOTH" in g and "/replay" in g for g in gaps), gaps


# ----------------------- CLI (production arg shape) -----------------------

def test_cli_accepts_the_bare_production_arg_shape(capsys):
    """guard-920: the literal production shape, not the contract-ideal one.

    The goal record's skill field is `/replay --sharp-wave`. Under nargs="*"
    argparse claimed `--sharp-wave` as an unknown OPTION and exited 2 on the one
    input this command exists to judge. Caught by the first smoke run; this
    pins it.
    """
    rc = we._main(["skill-eligible", "/replay", "--sharp-wave"])
    assert rc == 1
    out = capsys.readouterr()
    assert out.out.strip() == "reducer-only"
    assert "unmerged" in out.err


def test_cli_quoted_single_arg_form_agrees(capsys):
    rc = we._main(["skill-eligible", "/replay --sharp-wave"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "reducer-only"


def test_cli_eligible_case(capsys):
    rc = we._main(["skill-eligible", "/tree"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "eligible"


def test_cli_no_skill_is_eligible(capsys):
    rc = we._main(["skill-eligible"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "eligible"


def test_cli_verdict_on_stdout_reason_on_stderr(capsys):
    """`$(... skill-eligible ...)` must capture one clean word.

    The reason still has to go SOMEWHERE -- a silent skip is the half of this
    fix that would rot -- so it goes to stderr.
    """
    we._main(["skill-eligible", "/replay"])
    out = capsys.readouterr()
    assert out.out.strip() in ("eligible", "reducer-only")
    assert len(out.out.strip().split()) == 1
    assert len(out.err.strip()) > 40


def test_cli_reducer_only_skills_lists_every_refusal(capsys):
    rc = we._main(["reducer-only-skills"])
    assert rc == 0
    listed = capsys.readouterr().out
    for skill, stage in we.SKILL_LIFECYCLE_STAGE.items():
        if we.LIFECYCLE_DISPOSITIONS[stage].kind == we.REDUCER_ONLY_BY_DESIGN:
            assert skill in listed, f"{skill} missing from the refusal listing"


# ----------------------- the architectural invariant -----------------------

def test_selection_stays_role_blind():
    """LIFECYCLE_DISPOSITIONS["select"] forbids worker-specific selection logic
    in as many words: "There is no worker-specific selection logic and there
    must not be one."

    The obvious fix for g-115-5664 was to filter inside goal-selector when the
    caller is a worker. That would violate the declared disposition and would
    put role-conditional behavior inside a component BOTH roles run
    (guard-2783). The eligibility question is answered in worker_execute and
    consulted by the worker loop instead. This test pins that: goal-selector
    must not reach for this module.
    """
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert "skill_eligibility" not in src
    assert "worker_execute" not in src
    assert we.LIFECYCLE_DISPOSITIONS["select"].kind == we.SHARED_COMPONENT
