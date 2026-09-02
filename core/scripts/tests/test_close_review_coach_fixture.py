#!/usr/bin/env python3
"""Coach  regression fixture for the close-review gate ().

Third leg of the close-review hardening. g-357-40 shipped the tier classifier and
the gate and unit-tested every TRIGGER in isolation; this file pins the INCIDENT
that motivated them, end to end, plus the one contract hole those unit tests left.

THE INCIDENT (coach g-012-02, g-357-39). A goal whose description enumerated 16
named entities was closed GREEN. Its artifact carried 16 entries -- the right
COUNT -- but 6 of them had substituted identities (famous-name priors displacing
the actual entities). The verification criterion was count-based ("all 16 present"),
so it passed, and the author self-graded against it. Nothing in the loop compared
the artifact's identities against the source's.

WHAT THIS FILE PINS, and why each case is not already covered upstream:

  1. The shape reaches tier 2 at all -- via `entities`, on a REALISTIC 16-entity
     description rather than the 3-id minimum the trigger test uses.
  2. THE DEFECT ITSELF: the count-based criterion is GREEN on the mangled artifact
     while identity fidelity is RED. This is the case that makes the incident
     reproducible instead of merely described, and it is asserted with the SHIPPED
     `count_named_entities`, not a stand-in, so it stays honest if that function
     changes.
  3. The gate REFUSES this shape end to end, naming the entities trigger.
  4. A **REJECT** verdict does NOT satisfy the gate. THIS IS THE HOLE: the gate
     tests upstream cover no-verdict (refuse) and APPROVE (pass), so a regression
     that relaxed `== "APPROVE"` to truthiness or to `!= "REJECT"` would keep both
     of them green while silently closing every REJECTED goal. A reviewer that can
     say no is the entire point of the gate; nothing pinned that it is heard.
  5. Cost control: the tier-0 recurring sweep is untouched by all of the above.

FIXTURE IDS ARE NAMESPACED, NOT HIGH ROUND NUMBERS (guard-2282). The goal_id is
the verdict artifact's FILENAME, i.e. a real write path, so it must not be a
"surely absent" numeric literal that production id-space can grow into. The
id-shaped tokens inside the description are inert text -- they are only ever
regex-matched, never used as store keys.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from goal_close_risk_tier import classify, count_named_entities  # noqa: E402

GATE = SCRIPTS / "close-review-gate.py"

# Namespaced, non-numeric (guard-2282). Used as the verdict artifact's filename.
FIXTURE_GOAL_ID = "close-review-fixture-coach-shape"
FIXTURE_AGENT = "pytest-throwaway-coach-fixture"

# The 16 entities the source goal enumerated.
SOURCE_ENTITIES = [
    "g-012-02", "g-012-03", "g-012-04", "g-012-05",
    "guard-8801", "guard-8802", "guard-8803", "guard-8804",
    "rb-7701", "rb-7702", "rb-7703", "rb-7704",
    "asp-4401", "asp-4402", "sq-3301", "sig-2201",
]

# The artifact as it was actually produced: same COUNT, but the last 6 identities
# were displaced by other plausible ids (the famous-name-prior substitution).
ARTIFACT_ENTITIES = SOURCE_ENTITIES[:10] + [
    "rb-7799", "rb-7798", "asp-4499", "asp-4498", "sq-3399", "sig-2299",
]

COACH_DESCRIPTION = (
    "Catalogue each of the following and record its disposition: "
    + ", ".join(SOURCE_ENTITIES)
    + ". Deliverable is one row per entity."
)


def _coach_goal(**kw):
    """The  shape as the classifier and gate see it."""
    goal = {
        "goal_id": FIXTURE_GOAL_ID,
        "title": "Catalogue 16 entities and record dispositions",
        "description": COACH_DESCRIPTION,
        "priority": "MEDIUM",          # deliberately NOT HIGH: isolates `entities`
        "participants": ["agent"],     # ...and keeps `user_truth` from firing
        "verification": {"outcomes": ["all 16 entries present in the artifact"]},
    }
    goal.update(kw)
    return goal


def _run_gate(goal, tmp_path, env_extra=None, extra_args=()):
    """Subprocess, matching test_goal_close_risk_tier.py: iteration-close.sh reads
    this script's rc, so the rc contract is what must be tested (canonical
    INVOCATION, not merely canonical binary)."""
    gj = tmp_path / "goal.json"
    gj.write_text(json.dumps(goal), encoding="utf-8")
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"                 # guard-955
    env["CLOSE_REVIEW_LEDGER_DIR"] = str(tmp_path)   # never touch the real ledger
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(GATE), "--goal", goal["goal_id"],
         "--goal-json", str(gj), *extra_args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def _write_verdict(verdict_value, tmp_path, reviewer="fixture-reviewer"):
    """Write a verdict artifact through the REAL production path resolution.

    WORLD-scoped and GOAL-keyed since g-357-41 — audit-reports/close-reviews/
    under the CLOSE_REVIEW_LEDGER_DIR root that _run_gate isolates to tmp_path.
    It used to write into agent_dir(FIXTURE_AGENT)/session/close-reviews, which
    is precisely the shape that made the gate satisfiable only by self-review:
    the only writer able to reach that path was the closer itself.

    `reviewer` defaults to a name that is NOT FIXTURE_AGENT, because an
    independent reviewer is now part of what the gate checks."""
    d = tmp_path / "audit-reports" / "close-reviews"
    d.mkdir(parents=True, exist_ok=True)
    art = d / f"{FIXTURE_GOAL_ID}.json"
    art.write_text(json.dumps({
        "verdict": verdict_value,
        "reviewer": reviewer,
        "checks": ["source-fidelity: artifact identities vs description identities"],
        "findings": ["6 of 16 entity identities substituted"],
    }), encoding="utf-8")
    return art


# ─── 1. the shape reaches tier 2 ───────────────────────────────────────────

def test_coach_shape_is_tier2_via_entities():
    r = classify(_coach_goal())
    assert r["tier"] == 2, r
    assert r["triggers"]["entities"] is True
    # Isolated: no other trigger is carrying this classification.
    assert r["triggers"]["high_prio"] is False
    assert r["triggers"]["user_truth"] is False


def test_the_source_really_does_enumerate_sixteen():
    """Pins the fixture itself. If the entity regex ever stops matching one of
    these id families the count drops and every case below would weaken
    silently, passing for the wrong reason."""
    assert count_named_entities(COACH_DESCRIPTION) == 16


# ─── 2. THE DEFECT: count-based green, identity fidelity red ───────────────

def test_count_based_criterion_is_GREEN_while_identities_are_WRONG():
    """The incident, reproduced. This is why a count-based verification criterion
    could not catch it, and therefore why an independent close review is the
    remedy rather than a better criterion of the same kind."""
    source_ids = {e.lower() for e in SOURCE_ENTITIES}
    artifact_ids = {e.lower() for e in ARTIFACT_ENTITIES}

    # The criterion the goal actually shipped with: "all 16 entries present".
    assert len(ARTIFACT_ENTITIES) == 16
    assert count_named_entities(" ".join(ARTIFACT_ENTITIES)) == 16   # GREEN

    # What the criterion never looked at.
    substituted = source_ids - artifact_ids
    assert len(substituted) == 6, sorted(substituted)                # RED
    assert artifact_ids - source_ids                                  # invented ids present


# ─── 3. the gate refuses the shape, end to end ─────────────────────────────

def test_gate_REFUSES_the_coach_shape_and_names_the_entities_trigger(tmp_path):
    r = _run_gate(_coach_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert "entities" in r.stderr
    assert "16 distinct named entities" in r.stderr


# ─── 4. THE HOLE: a REJECT verdict must not satisfy the gate ───────────────

def test_a_REJECT_verdict_does_NOT_satisfy_the_gate(tmp_path):
    """The reviewer's `no` must be heard. Upstream tests cover absent-verdict and
    APPROVE only, so a relaxation of the APPROVE comparison would pass both while
    closing every rejected goal.

    guard-1906: assert the subject was PROCESSED, not merely that the rc was 1 --
    the artifact is on disk at the path the gate names, so this cannot pass
    because the gate failed to find a file."""
    art = _write_verdict("REJECT", tmp_path)
    r = _run_gate(_coach_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert art.exists()                    # the artifact WAS present...
    assert str(art) in r.stderr            # ...at exactly the path the gate read


def test_an_APPROVE_verdict_DOES_release_the_same_shape(tmp_path):
    """Paired positive control for the case above (guard-2298): without it, a
    REJECT refusal cannot be distinguished from a gate that always blocks tier 2
    regardless of what the artifact says."""
    _write_verdict("APPROVE", tmp_path)
    r = _run_gate(_coach_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT})
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"decision": "pass"' in r.stdout
    assert "fixture-reviewer" in r.stdout


# ─── 4b. INDEPENDENCE: the half the artifact's old location made unreachable ──
# Until  the verdict lived at agents/<CLOSING agent>/session/close-reviews/,
# so the ONLY writer who could satisfy this gate was the closer itself, and the
# module docstring's "the author must not approve their own close" was a sentence
# with no mechanism behind it. Every one of the 32 upstream tests passed anyway,
# because each built the artifact under the same agent that then closed — the
# fixture shape could not express the defect. These four cases can.

def test_a_SELF_APPROVED_verdict_does_NOT_satisfy_the_gate(tmp_path):
    """THE load-bearing case. An APPROVE whose reviewer IS the closing agent is
    the exact artifact the old per-agent path made the only possible one."""
    art = _write_verdict("APPROVE", tmp_path, reviewer=FIXTURE_AGENT)
    r = _run_gate(_coach_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    assert "closing agent itself" in r.stderr
    assert '"defect": "self-review"' in r.stdout
    # guard-1906: the artifact was PRESENT and said APPROVE — this refusal is a
    # judgement about its reviewer, not a failure to find a file.
    assert art.exists()
    assert "APPROVE" in art.read_text(encoding="utf-8")


def test_an_UNATTRIBUTED_approve_does_NOT_satisfy_the_gate(tmp_path):
    """An approval nobody is accountable for cannot be shown to be independent.
    Absence of review is the one thing this gate must never fail open on."""
    _write_verdict("APPROVE", tmp_path, reviewer="")
    r = _run_gate(_coach_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "names no reviewer" in r.stderr
    assert '"defect": "unattributed"' in r.stdout


def test_self_review_refusal_is_still_OVERRIDABLE(tmp_path):
    """The demotion routes through the SAME override branch as verdict absence.
    A governance gate whose refusal has no reachable remedy manufactures false
    records in the store it protects (guard-1532), so this must stay open."""
    _write_verdict("APPROVE", tmp_path, reviewer=FIXTURE_AGENT)
    r = _run_gate(_coach_goal(), tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT},
                  extra_args=("--override-close-review", "solo deployment, no peer"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"decision": "override"' in r.stdout
    assert '"defect": "self-review"' in r.stdout   # the override RECORDS what it bypassed


def test_the_verdict_path_is_NOT_keyed_by_the_closing_agent(tmp_path):
    """THE structural regression pin for the  defect itself.

    ONE verdict, written once by an independent reviewer, must satisfy the gate
    for ANY closing agent — that is what "an independent reviewer can place a
    verdict" means operationally. Re-key the path by the closer and this case
    goes red while every other test in this file stays green, because every
    other test uses exactly one agent."""
    _write_verdict("APPROVE", tmp_path, reviewer="an-independent-peer")
    for closer in (FIXTURE_AGENT, "some-entirely-other-agent"):
        r = _run_gate(_coach_goal(), tmp_path,
                      {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": closer})
        assert r.returncode == 0, f"closer={closer}: " + r.stdout + r.stderr
        assert '"decision": "pass"' in r.stdout, closer
        assert "an-independent-peer" in r.stdout, closer


# ─── 5. cost control ───────────────────────────────────────────────────────

def test_tier0_recurring_sweep_pays_nothing_even_beside_this_fixture(tmp_path):
    """The whole tier exists so the cadence stays affordable. A recurring routine
    sweep closes with no review demanded even with the gate enabled."""
    r = _run_gate(_coach_goal(recurring=True, outcome_class="routine",
                              description="routine sweep, no entities"),
                  tmp_path,
                  {"CLOSE_REVIEW_GATE_ENABLED": "1", "MIND_AGENT": FIXTURE_AGENT},
                  extra_args=("--artifacts-count", "0"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"tier": 0' in r.stdout
