"""Tests for retrieve.load_forged_skills — the forged-skill retrieval lane ().

Implementation of the decision g-115-3267 measured its way to: forged-skill
TRIGGERS become a supplementary store in retrieve.sh, so a matching skill
surfaces at goal-execution retrieval rather than being rediscovered by hand.

WHAT THESE TESTS PIN, and why each one is here rather than being obvious:

  * THE TOKEN-OVERLAP BINDING. Forged skills carry NO category field, so the
    combined `_entry_matches` predicate (strict category FIRST, token overlap
    only as fallback) matches nothing and returns an empty store on every
    query. That failure is INVISIBLE: empty is also what a genuine no-match
    returns. `test_negative_control_*` plus the two positive controls are what
    separate the two — an always-empty implementation fails the positives, an
    always-match one fails the negative. Neither alone is sufficient, which is
    why both directions are pinned (guard-1683: a criterion that passes
    identically on the broken shape is vacuous).

  * THE NESTING. The registry has TWO top-level keys, `skills` and
    `skills_pending_commit`; the 80 skill records live one level DOWN under
    `skills`. A loader that iterates the top level finds two keys, no
    triggers, and returns [] while looking perfectly healthy — the same silent
    shape as the category-binding bug above.

  * PENDING-COMMIT EXCLUSION. An uncommitted skill is not reachable
    fleet-wide, so surfacing it would advertise a capability a peer Body
    cannot invoke.

  * RANKING BEFORE THE CAP. 80 live skills is EXACTLY
    SUPPLEMENTARY_CAPS["deep"], so an unranked lane returns everything at
    depth=deep and the cap does nothing.

The hermetic tests use a synthetic registry; the live-registry test at the
bottom exists because every hermetic test here would still pass while
production returned an empty lane (the sibling test_forged_skill_surface.py
carries the same reasoning for the same reason).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_WORLD = os.environ.get("MIND_WORLD")
_ORIG_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="forged-skills-lane-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location(
    "retrieve_forged_mod", CORE_SCRIPTS / "retrieve.py")
_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_R)

if _ORIG_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_WORLD
if _ORIG_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_AGENT


SYNTHETIC = {
    "skills": {
        "land-stranded-pr": {
            "triggers": ["land a stranded PR",
                         "stranded commit needs to reach the default branch",
                         "unmerged PR with a goal-named sha"],
            "type": "procedure",
        },
        "diff-mirrored-sets": {
            # Verbatim from the live registry. An abridged trigger list makes
            # this fixture EASIER to match than production, which is the wrong
            # direction for a control.
            "triggers": ["two-way diff", "both-directions diff",
                         "mirrored-set reconciliation", "set-reconciliation",
                         "positive-control the extraction",
                         "A-minus-B and B-minus-A"],
            "type": "procedure",
        },
        "no-triggers-skill": {"type": "procedure"},
    },
    "skills_pending_commit": {
        "uncommitted-skill": {
            "triggers": ["land a stranded PR", "stranded commit"],
        },
    },
}


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    p = tmp_path / "forged-skills.yaml"
    p.write_text(yaml.safe_dump(SYNTHETIC), encoding="utf-8")
    monkeypatch.setattr(_R, "FORGED_SKILLS_PATH", p)
    return p


def _names(entries):
    return [e["name"] for e in entries]


# ── the binding: positive controls ───────────────────────────────────────────

def test_positive_control_stranded_pr(registry):
    """The  known miss. Its title shares NO literal trigger phrase —
    it is PROBLEM-phrased where the trigger is INTENT-phrased — so this is the
    exact case an execute-preamble grep cannot reach."""
    got = _R.load_forged_skills(
        ["Investigate: g-326-686 closed completed but its commit is "
         "stranded on an unmerged branch"])
    assert "land-stranded-pr" in _names(got)


def test_positive_control_two_way_diff(registry):
    got = _R.load_forged_skills(
        ["reconcile two mirrored sets and positive-control the extraction "
         "in both directions"])
    assert "diff-mirrored-sets" in _names(got)


# ── the binding: negative control (an always-empty impl passes the above) ────

def test_negative_control_unrelated_query_is_silent(registry):
    """Without this, an always-MATCH implementation passes the positives."""
    got = _R.load_forged_skills(
        ["Recurring: generate a formatted progress report summarizing "
         "accomplishments and metrics"])
    assert "land-stranded-pr" not in _names(got)


def test_empty_categories_returns_empty(registry):
    assert _R.load_forged_skills([]) == []


# ── shape ────────────────────────────────────────────────────────────────────

def test_pending_commit_bucket_is_excluded(registry):
    """An uncommitted skill is not reachable fleet-wide. It shares a trigger
    with land-stranded-pr, so a loader reading both buckets returns it here."""
    got = _R.load_forged_skills(
        ["a stranded commit must reach the default branch, unmerged"])
    assert "uncommitted-skill" not in _names(got)
    assert "land-stranded-pr" in _names(got)


def test_skill_without_triggers_is_skipped(registry):
    got = _R.load_forged_skills(["no-triggers-skill"])
    assert "no-triggers-skill" not in _names(got)


def test_index_reads_the_nested_skills_key(registry):
    """Guards the two-top-level-key nesting: a top-level iteration finds
    `skills`/`skills_pending_commit`, no triggers, and returns []."""
    idx = _R._build_forged_skills_index()
    assert {e["name"] for e in idx} == {"land-stranded-pr", "diff-mirrored-sets"}


def test_entries_carry_name_and_triggers(registry):
    idx = _R._build_forged_skills_index()
    e = next(x for x in idx if x["name"] == "land-stranded-pr")
    assert e["triggers"] and isinstance(e["triggers"], list)
    assert "stranded" in e["content"]


# ── robustness ───────────────────────────────────────────────────────────────

def test_missing_store_yields_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(_R, "FORGED_SKILLS_PATH", tmp_path / "absent.yaml")
    assert _R.load_forged_skills(
        ["a stranded commit must reach the default branch"]) == []


def test_none_path_yields_empty(monkeypatch):
    monkeypatch.setattr(_R, "FORGED_SKILLS_PATH", None)
    assert _R.load_forged_skills(
        ["a stranded commit must reach the default branch"]) == []


def test_malformed_store_yields_empty(monkeypatch, tmp_path):
    p = tmp_path / "forged-skills.yaml"
    p.write_text("skills: not-a-dict\n", encoding="utf-8")
    monkeypatch.setattr(_R, "FORGED_SKILLS_PATH", p)
    assert _R.load_forged_skills(
        ["a stranded commit must reach the default branch"]) == []


def test_short_intent_phrase_does_not_fire(registry):
    """MEASURED while writing these tests, and worth pinning rather than
    hiding: the lane inherits `_TEXT_FALLBACK_MIN_OVERLAP` = 2 distinct
    length->=5 query tokens. The literal trigger phrase "land a stranded PR"
    carries exactly ONE ("stranded"), so quoting a trigger back at the lane
    does NOT fire it. That is the shared admission threshold doing its job
    (a 1-token rule matched ~half the RB corpus on stopword-heavy queries),
    not a defect here — but it means this lane is tuned for GOAL-LENGTH text,
    which is exactly the input it sees at goal-execution retrieval.
    If someone later lowers the threshold for this lane, they should have to
    delete this test and say why."""
    assert _R.load_forged_skills(["land a stranded PR"]) == []


# ── ranking + cap ────────────────────────────────────────────────────────────

def test_results_are_ranked_by_query_overlap(monkeypatch, tmp_path):
    """80 live skills == SUPPLEMENTARY_CAPS['deep'], so an unranked lane
    would cut arbitrarily. The stronger overlap must sort first."""
    p = tmp_path / "forged-skills.yaml"
    p.write_text(yaml.safe_dump({"skills": {
        "weak-match": {"triggers": ["stranded branch"]},
        "strong-match": {"triggers": ["stranded commit unmerged branch "
                                      "default reach"]},
    }}), encoding="utf-8")
    monkeypatch.setattr(_R, "FORGED_SKILLS_PATH", p)
    got = _names(_R.load_forged_skills(
        ["commit is stranded on an unmerged branch and must reach default"]))
    assert got[0] == "strong-match", got


def test_cap_is_enforced(monkeypatch, tmp_path):
    p = tmp_path / "forged-skills.yaml"
    p.write_text(yaml.safe_dump({"skills": {
        f"skill-{i:02d}": {"triggers": ["stranded commit unmerged branch"]}
        for i in range(_R.FORGED_SKILLS_CAP + 5)
    }}), encoding="utf-8")
    monkeypatch.setattr(_R, "FORGED_SKILLS_PATH", p)
    got = _R.load_forged_skills(
        ["commit is stranded on an unmerged branch"])
    assert len(got) == _R.FORGED_SKILLS_CAP


# ── live registry ────────────────────────────────────────────────────────────

@pytest.mark.skipif(_R.FORGED_SKILLS_PATH is None
                    or not _R.FORGED_SKILLS_PATH.exists(),
                    reason="no live forged-skills.yaml in this deployment")
def test_live_registry_is_not_empty():
    """Every hermetic test above passes against a synthetic registry while
    production returns an empty lane. This is the one that would notice."""
    assert _R._build_forged_skills_index(), (
        "live forged-skills.yaml produced an EMPTY index — check the "
        "`skills` nesting and the triggers field before trusting any "
        "empty retrieval result")
