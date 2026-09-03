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


# ------------- the skill-less branch must not read as a PASS () -------------

def test_skill_less_branch_declares_non_evaluation_not_a_pass():
    """A SKILL-keyed bridge cannot answer "is this GOAL reducer-only?" for a goal
    that names no skill -- it has no key at all. The branch used to return
    "no skill named on the goal -- ordinary worker-eligible work", rendering a
    structurally-unanswerable question as a CLEARED CHECK. That string was the
    whole danger: 919 of 938 live candidates take this branch, and two
    reducer-only goals reached worker Bodies behind it -- g-306-284 (which pushes
    main, and which the drain lane affirmatively instructed a worker to claim)
    and g-115-6886 (which clears the agent-wide working memory).

    guard-1760 class: a checker must not report what it DECLINED to look at as a
    pass. The fix is a MESSAGE change and deliberately NOT a flip of the default
    -- fail-closed would refuse a worker essentially everything and strand the
    role outright.
    """
    v = we.skill_eligibility(None)

    # The load-bearing half (guard-2860): fail-open is preserved. A wrong refusal
    # strands real worker work silently, which is the worse direction.
    assert v.eligible is True
    assert v.skill is None

    # The honesty half: it must declare non-evaluation ...
    assert "NOT EVALUATED" in v.reason
    # ... must never again read as a cleared check ...
    assert "ordinary worker-eligible work" not in v.reason
    # ... and must hand the reader the check it could not perform.
    assert "verification outcomes" in v.reason


@pytest.mark.parametrize("skill", [None, "", "   "])
def test_every_skill_less_shape_takes_the_declared_branch(skill):
    """All three shapes normalize to None, so all three must get the honest
    message -- not only the literal None a test author reaches for first."""
    assert "NOT EVALUATED" in we.skill_eligibility(skill).reason


def test_non_evaluation_message_is_scoped_to_the_skill_less_branch():
    """POSITIVE CONTROL (guard-4166). A pin whose effect is that a STRING STOPS
    APPEARING proves nothing unless something still produces the contrasting
    values: had this fix leaked "NOT EVALUATED" into every branch, the test above
    would pass while the bridge stopped distinguishing anything at all.

    So a NAMED skill must never take the non-evaluation branch, a genuine
    reducer-only skill must still REFUSE, and a pinned-eligible skill must still
    pass -- each with its own distinct reason.
    """
    unknown = we.skill_eligibility("/never-heard-of-it")
    assert "NOT EVALUATED" not in unknown.reason
    assert unknown.eligible is True

    refused = we.skill_eligibility("/replay")
    assert "NOT EVALUATED" not in refused.reason
    assert refused.eligible is False        # the dangerous direction still fails

    pinned = we.skill_eligibility("/tree")
    assert "NOT EVALUATED" not in pinned.reason
    assert pinned.eligible is True


# ----------------- : the GOAL-level role declaration -----------------
#
# The skill bridge above is SKILL-keyed and 1,411 of 1,447 live candidates carry
# no skill (97.5%, cc-09 2026-09-03), so for ~98% of the queue it answers "NOT
# EVALUATED". `goal_eligibility` reads a GOAL-level `executable_by_role` first.
#
# Weighted the same way as the block above (guard-2860): the load-bearing tests
# are the ones that must not fail in the dangerous direction -- the CONTRADICTION
# case (metadata must never unlock a reducer-only skill) and the UNSET case (the
# existing corpus must behave exactly as it did before this field existed).


def test_unset_role_preserves_the_skill_bridge_verdict_exactly():
    """THE REGRESSION PIN. The field is new, so the entire existing corpus is
    unset; if the unset path diverged from skill_eligibility in either
    direction this change would silently re-route the whole live queue."""
    for skill in (None, "", "/replay --sharp-wave", "/tree", "/reflect",
                  "/never-heard-of-it", "/agent-completion-report"):
        assert (we.goal_eligibility(skill, None).eligible
                is we.skill_eligibility(skill).eligible), skill


def test_declared_reducer_is_refused_even_with_no_skill():
    """The gap this goal exists to close: a skill-LESS reducer-only goal."""
    v = we.goal_eligibility(None, "reducer")
    assert v.eligible is False
    assert "executable_by_role='reducer'" in v.reason


def test_declared_worker_is_eligible_and_routes_positively():
    """The BIDIRECTIONAL half. Of the 3 measured role-unsatisfiable defers, TWO
    need a worker -- a boolean `reducer_only` could not express them."""
    v = we.goal_eligibility(None, "worker")
    assert v.eligible is True
    assert "executable_by_role='worker'" in v.reason


def test_worker_declaration_cannot_unlock_a_reducer_only_skill():
    """MOST LOAD-BEARING. Filer-supplied metadata must never relax a structural
    fence: running /replay on a worker encodes from unmerged state (the
    Nth-reducer defect). The contradiction must refuse AND name both halves so
    the mis-filed record gets corrected rather than silently honoured."""
    v = we.goal_eligibility("/replay --sharp-wave", "worker")
    assert v.eligible is False
    assert "CONTRADICTION" in v.reason
    assert "unmerged" in v.reason      # the skill verdict is still carried through


def test_any_defers_to_the_skill_bridge_in_both_directions():
    assert we.goal_eligibility("/tree", "any").eligible is True
    assert we.goal_eligibility("/replay", "any").eligible is False


def test_unrecognised_role_falls_back_and_says_it_is_not_a_cleared_check():
    """A typo must not fence a goal in EITHER direction, and must not read as a
    pass -- the guard-1760 class (a checker must not report what it declined to
    look at as a pass)."""
    v = we.goal_eligibility(None, "reduser")
    assert v.eligible is we.skill_eligibility(None).eligible
    assert "UNRECOGNISED" in v.reason
    assert "NOT a cleared check" in v.reason
    # ...and it must still not unlock a refused skill
    assert we.goal_eligibility("/replay", "reduser").eligible is False


def test_role_value_is_case_and_whitespace_insensitive():
    for raw in ("reducer", "Reducer", "  REDUCER  "):
        assert we.goal_eligibility(None, raw).eligible is False, raw


def test_declared_values_are_the_documented_set():
    assert we.EXECUTABLE_BY_ROLE_VALUES == ("worker", "reducer", "any")


def test_positive_control_the_field_actually_changes_something():
    """POSITIVE CONTROL (guard-4166). Every assertion above would still pass if
    `goal_eligibility` ignored its role argument entirely and forwarded to the
    skill bridge -- except this one. On a skill-LESS goal the bridge says
    ELIGIBLE, so a role of 'reducer' must flip the verdict; that flip is the
    whole capability being added."""
    skill_less = we.skill_eligibility(None)
    assert skill_less.eligible is True, "precondition: the bridge fails open"
    assert we.goal_eligibility(None, "reducer").eligible is False, \
        "the role field had no effect -- the fix is inert"


def test_schema_registers_the_field_so_a_writer_exists():
    """guard-167: a new tracked field must wire BOTH producer and consumer. The
    consumer is goal_eligibility; the producer is the generic goal-field write
    path, which refuses any name absent from GOAL_KNOWN_FIELDS."""
    import _goal_fields
    assert _goal_fields.is_known("executable_by_role")


# ─── : MSYS argv conversion defeats the gate on Windows ────────────
# Git-Bash rewrites ANY argv beginning with "/" into a Windows path rooted at
# the Git prefix, so worker-loop Phase 1's own documented invocation
# (`skill-eligible /reflect`) arrives as "C:/Program Files/Git/reflect". The
# rewrite embeds a SPACE, so normalize_skill's whitespace split saw
# "C:/Program", which maps to no lifecycle stage and returned the deliberate
# fail-open GREEN -- the gate inverted to PERMISSIVE for every reducer-only
# skill, as a pass rather than an error.
#
# SCOPE OF WHAT THESE TESTS PROVE, stated plainly because the goal that filed
# this asked for something they are NOT: the filer requires a regression test
# driven through a SHELL on MINGW64, since only a real MSYS shell performs the
# rewrite. These tests instead pin the LITERAL post-rewrite string measured on
# DESKTOP-O91DLK2 and assert the module handles it. That is the production
# ARGUMENT shape (guard-920) but not the production SHELL, so it runs and means
# the same thing on every platform -- and it does NOT discharge the MINGW64
# end-to-end check, which remains open and can only be run on a Windows box.

MSYS_GIT_PREFIX = "C:/Program Files/Git/"


def _mangled(skill: str) -> str:
    """The argv a Git-Bash shell actually delivers for `skill`.

    Measured form, not a guess: `py -3 -c "print(sys.argv[1:])" "/reflect"` on
    MINGW64 printed ['C:/Program Files/Git/reflect'].
    """
    return MSYS_GIT_PREFIX + skill.lstrip("/")


def test_every_reducer_only_skill_is_still_refused_when_msys_mangles_it():
    """THE REGRESSION. Before the fix all ten returned eligible=True.

    Parameterised over the table rather than a hand-listed set, so a skill added
    to SKILL_LIFECYCLE_STAGE later is covered without editing this test -- the
    hand-listed variant would have gone stale silently, which is the shape
    guard-1760 warns about (a checker must not report what it declined to look
    at as a pass).
    """
    reducer_only = [s for s in we.SKILL_LIFECYCLE_STAGE
                    if we.skill_eligibility(s).eligible is False]
    assert reducer_only, "precondition: the bridge refuses at least one skill"
    for skill in reducer_only:
        raw = _mangled(skill)
        assert we.normalize_skill(raw) == skill, \
            f"{raw!r} did not recover to {skill!r}"
        assert we.skill_eligibility(raw).eligible is False, \
            f"MSYS-mangled {skill!r} read as worker-eligible -- the gate is inverted"


def test_mangled_skill_keeps_its_arguments_and_still_resolves():
    """The goal's skill field carries the whole invocation, not the bare name,
    so the mangled value has trailing args after the rewritten prefix."""
    raw = _mangled("/review-hypotheses --resolve")
    assert we.normalize_skill(raw) == "/review-hypotheses"
    assert we.skill_eligibility(raw).eligible is False


def test_CONTROL_a_pinned_eligible_skill_stays_eligible_when_mangled():
    """LOAD-BEARING EXCLUSION (this file's own weighting, guard-2860).

    `/tree` is pinned worker-eligible despite encoding. Recovery must return it
    to the bridge as `/tree` and the bridge must still say YES -- a recovery
    that refused everything it recognised would strand sanctioned worker work,
    which is the expensive direction.
    """
    raw = _mangled("/tree")
    assert we.normalize_skill(raw) == "/tree"
    assert we.skill_eligibility(raw).eligible is True


def test_CONTROL_an_unrecognised_mangled_value_is_left_alone():
    """Recovery is a POSITIVE LIST, not heuristic path-stripping.

    A path naming no known skill must NOT be rewritten into one, and the
    fail-open default must survive untouched. This is what stops the fix
    inventing a skill nobody asked for.
    """
    raw = MSYS_GIT_PREFIX + "notaskill"
    assert we.normalize_skill(raw) == "/C:/Program"      # unchanged, pre-fix shape
    assert we.skill_eligibility(raw).eligible is True


def test_CONTROL_posix_and_bare_forms_are_unchanged_by_the_recovery():
    """INVARIANCE (guard-2903: an invariance test is green by default when it is
    broken, so assert the values rather than merely calling the function).

    On Linux no rewrite happens, and goal records carry both `/replay` and
    `replay`. All three pre-existing shapes must resolve exactly as before.
    """
    assert we.normalize_skill("/reflect") == "/reflect"
    assert we.normalize_skill("replay") == "/replay"
    assert we.normalize_skill("/replay --sharp-wave") == "/replay"
    assert we.skill_eligibility("/reflect").eligible is False
    assert we.skill_eligibility("replay").eligible is False
    assert we.normalize_skill("") is None
    assert we.skill_eligibility("").eligible is True


def test_recovery_is_reachable_through_the_CLI_argument_shape():
    """guard-920: pin the production ARG shape, not the contract-ideal one.

    `goal-eligible`/`skill-eligible` take the skill via argparse.REMAINDER and
    join it with spaces, so the mangled value arrives as several argv entries
    that re-join to one string. Exercise that join rather than calling the
    helper with an already-joined literal.
    """
    import subprocess
    script = str(CORE_SCRIPTS / "worker_execute.py")
    # The shell splits the mangled value on its embedded space, so the CLI
    # receives TWO argv entries that REMAINDER re-joins -- exactly what a
    # MINGW64 shell delivers. Reproduce that split rather than passing a
    # pre-joined literal.
    argv = [sys.executable, script, "skill-eligible", "C:/Program", "Files/Git/reflect"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, (
        f"CLI returned rc={proc.returncode} for an MSYS-mangled /reflect; "
        f"rc=0 is the inverted gate this goal exists to close. "
        f"stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}")
    assert "reducer-only" in proc.stdout

    # POSITIVE CONTROL through the same entry point: an unrecognised value must
    # still exit 0, so the rc=1 above is attributable to the recovery and not to
    # the CLI erroring on a multi-token argument.
    ctrl = subprocess.run(
        [sys.executable, script, "skill-eligible", "C:/Program", "Files/Git/notaskill"],
        capture_output=True, text=True, timeout=120)
    assert ctrl.returncode == 0, (
        f"control flipped: rc={ctrl.returncode}, stderr={ctrl.stderr[-300:]!r}")
