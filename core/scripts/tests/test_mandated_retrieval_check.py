"""Tests for core/scripts/mandated-retrieval-check.py ().

Two things are pinned here, and the second is the one that matters most.

1. The `is not False` discriminator. The real retrieve.py path OMITS
   `retrieval_performed` rather than setting it True, so `bool(...)` reads every
   genuine retrieval as a miss and the check becomes 100% false-firing. That is
   the g-115-3113 regression class, which has already made a sibling gate inert
   once. `test_bool_discriminator_would_be_wrong` is a MUTATION PROOF: it asserts
   the specific falsy value the real path produces (absent key) resolves the
   opposite way from what bool() would give, so swapping the operator back turns
   this file red rather than leaving it quietly green.

2. The narration/mandate split (guard-1430). The gate's whole justification is
   that it fires on self-addressed mandates and NOT on the 19-of-21 corpus
   majority that merely discusses retrieval. `test_narration_does_not_fire` uses
   real description text drawn from that measured corpus, including the line
   from g-115-3282 itself -- the goal that proposed the rejected predicate and
   would have been its own first false positive.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
_SCRIPT = CORE_SCRIPTS / "mandated-retrieval-check.py"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


mod = _load("mandated_retrieval_check", "mandated-retrieval-check.py")


# The real mandate, verbatim from 's description (measured 2026-08-10).
MANDATE = (
    "STEP 0 (MANDATORY - run BEFORE any probe below): bash core/scripts/"
    "retrieve.sh --category vinheim-runtime --goal g-335-09. Read the returned "
    "reasoning-bank entries before step 1."
)

# Narration drawn from the same corpus. Each contains the literal; none is a
# mandate addressed to its own executor.
NARRATION = {
    #  -- the goal that proposed the substring predicate.
    "g-115-3282": (
        "PROPOSED SCOPE - deliberately NARROW: IF the executing goal's own "
        "description contains the literal 'retrieve.sh --category', THEN require "
        "retrieval_performed is not False for that goal_id at close."
    ),
    #  -- quotes ANOTHER goal's invocation as measurement evidence.
    "g-115-3286": (
        "MEASURED. One call: retrieve.sh --category \"<free text>\" --depth "
        "shallow --goal g-115-4633. What it RETURNED included pattern_signatures "
        "count 3."
    ),
    #  -- a falsifiable prediction quoting invocations.
    "g-115-3494": (
        "CHEAP FALSIFIABLE PREDICTION: retrieve.sh --category \"user-experience\" "
        "--depth medium should return NONE of rb-5219 / guard-1516."
    ),
    #  -- cites the rule that prescribes the shape.
    "g-115-3770": (
        "code-review-protocol.md step 4 mandates retrieve.sh --category "
        "'<one-line fix description>' before any framework fix."
    ),
}


# --------------------------------------------------------------------------
# mandates_self_retrieval
# --------------------------------------------------------------------------

def test_self_addressed_mandate_is_detected():
    assert mod.mandates_self_retrieval(MANDATE, "g-335-09") is True


def test_equals_spelling_is_detected():
    desc = "Step 0: retrieve.sh --category foo --goal=g-335-09"
    assert mod.mandates_self_retrieval(desc, "g-335-09") is True


@pytest.mark.parametrize("goal_id,desc", sorted(NARRATION.items()))
def test_narration_does_not_fire(goal_id, desc):
    """The 19-of-21 corpus majority must not fire (guard-1430)."""
    assert mod.mandates_self_retrieval(desc, goal_id) is False


def test_quoting_another_goals_invocation_does_not_fire():
    """The self-reference IS the discriminator -- another goal's id is narration."""
    desc = "prior run did: retrieve.sh --category x --goal g-999-99"
    assert mod.mandates_self_retrieval(desc, "g-335-09") is False


def test_template_without_goal_flag_does_not_fire():
    """'s shape: a template invocation, deliberately out of scope."""
    desc = 'bash core/scripts/retrieve.sh --category "<subject>" --depth shallow'
    assert mod.mandates_self_retrieval(desc, "g-115-23") is False


def test_empty_inputs_do_not_fire():
    assert mod.mandates_self_retrieval("", "g-335-09") is False
    assert mod.mandates_self_retrieval(MANDATE, "") is False
    assert mod.mandates_self_retrieval(None, "g-335-09") is False


# --------------------------------------------------------------------------
# retrieval_performed_for -- the discriminator
# --------------------------------------------------------------------------

def test_absent_key_is_performed():
    """The REAL retrieve.py path omits the key. This must read as performed."""
    session = {"goal_id": "g-335-09", "tree_nodes_loaded": ["a"]}
    assert mod.retrieval_performed_for(session, "g-335-09") is True


def test_explicit_false_is_not_performed():
    """The no-retrieval stub written by iteration-close.sh."""
    session = {"goal_id": "g-335-09", "retrieval_performed": False}
    assert mod.retrieval_performed_for(session, "g-335-09") is False


def test_bool_discriminator_would_be_wrong():
    """MUTATION PROOF for the  regression class.

    The real path's manifest has NO `retrieval_performed` key, so
    `bool(session.get("retrieval_performed"))` is False -- the opposite of the
    correct answer. If someone swaps `is not False` for a truthiness test, the
    assertion below flips and this file goes red.
    """
    real = {"goal_id": "g-335-09", "tree_nodes_loaded": ["a"]}
    assert bool(real.get("retrieval_performed")) is False      # what bool() says
    assert mod.retrieval_performed_for(real, "g-335-09") is True  # the truth


def test_stale_manifest_from_another_goal_is_not_performed():
    """Single-slot file: a prior goal's REAL manifest must not count as ours."""
    session = {"goal_id": "g-115-9999", "tree_nodes_loaded": ["a"]}
    assert mod.retrieval_performed_for(session, "g-335-09") is False


def test_missing_or_malformed_session_is_not_performed():
    assert mod.retrieval_performed_for(None, "g-335-09") is False
    assert mod.retrieval_performed_for([], "g-335-09") is False


# --------------------------------------------------------------------------
# evaluate -- the composed decision
# --------------------------------------------------------------------------

def test_mandate_plus_stub_fires():
    fired, reason = mod.evaluate(
        MANDATE, {"goal_id": "g-335-09", "retrieval_performed": False}, "g-335-09")
    assert fired is True
    assert "no retrieval was recorded" in reason


def test_mandate_plus_real_retrieval_does_not_fire():
    fired, _ = mod.evaluate(
        MANDATE, {"goal_id": "g-335-09", "tree_nodes_loaded": ["a"]}, "g-335-09")
    assert fired is False


def test_no_mandate_plus_stub_does_not_fire():
    """A goal with no self-declared step is unaffected -- no new noise."""
    fired, reason = mod.evaluate(
        NARRATION["g-115-3282"],
        {"goal_id": "g-115-3282", "retrieval_performed": False}, "g-115-3282")
    assert fired is False
    assert "no self-addressed retrieval mandate" in reason


# --------------------------------------------------------------------------
# CLI -- advisory posture
# --------------------------------------------------------------------------

def _run_cli(tmp_path, description, session, goal_id, output="json"):
    sess = tmp_path / "retrieval-session.json"
    sess.write_text(json.dumps(session), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--goal-id", goal_id,
         "--session-file", str(sess), "--output", output],
        input=description, capture_output=True, text=True)


def test_cli_fires_and_still_exits_zero(tmp_path):
    """ADVISORY: a fire must never fail the close (guard-1562)."""
    r = _run_cli(tmp_path, MANDATE,
                 {"goal_id": "g-335-09", "retrieval_performed": False}, "g-335-09")
    assert r.returncode == 0
    assert json.loads(r.stdout)["fired"] is True


def test_cli_text_mode_writes_banner_to_stderr(tmp_path):
    r = _run_cli(tmp_path, MANDATE,
                 {"goal_id": "g-335-09", "retrieval_performed": False},
                 "g-335-09", output="text")
    assert r.returncode == 0
    assert "[mandated-retrieval] ADVISORY" in r.stderr
    assert "g-335-09" in r.stderr


def test_cli_quiet_when_not_fired(tmp_path):
    r = _run_cli(tmp_path, NARRATION["g-115-3282"],
                 {"goal_id": "g-115-3282", "retrieval_performed": False},
                 "g-115-3282", output="text")
    assert r.returncode == 0
    assert r.stderr.strip() == ""


# --------------------------------------------------------------------------
# description_from_query -- the iteration-close.sh wire-up shape
# --------------------------------------------------------------------------

def test_query_extraction_picks_the_right_row():
    raw = json.dumps([{"id": "g-000-01", "description": "other"},
                      {"id": "g-335-09", "description": MANDATE}])
    assert mod.description_from_query(raw, "g-335-09") == MANDATE


def test_query_extraction_is_tolerant_of_junk():
    """A broken query must yield "" (silent), never a fire."""
    for raw in ("", "not json", "null", "{}", "[]", '[{"id":"g-1"}]'):
        assert mod.description_from_query(raw, "g-335-09") == ""


def test_broken_query_cannot_manufacture_a_fire(tmp_path):
    """End-to-end: empty query output + stub manifest => silent, exit 0."""
    sess = tmp_path / "retrieval-session.json"
    sess.write_text(json.dumps({"goal_id": "g-335-09",
                                "retrieval_performed": False}), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--goal-id", "g-335-09",
         "--session-file", str(sess), "--query-json", "--output", "json"],
        input="", capture_output=True, text=True)
    assert r.returncode == 0
    assert json.loads(r.stdout)["fired"] is False


def test_cli_query_json_mode_fires_on_real_shape(tmp_path):
    sess = tmp_path / "retrieval-session.json"
    sess.write_text(json.dumps({"goal_id": "g-335-09",
                                "retrieval_performed": False}), encoding="utf-8")
    raw = json.dumps([{"id": "g-335-09", "description": MANDATE}])
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--goal-id", "g-335-09",
         "--session-file", str(sess), "--query-json", "--output", "json"],
        input=raw, capture_output=True, text=True)
    assert r.returncode == 0
    assert json.loads(r.stdout)["fired"] is True


def test_cli_missing_session_file_exits_zero(tmp_path):
    """Fail-open: an unreadable manifest must not block a close."""
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--goal-id", "g-335-09",
         "--session-file", str(tmp_path / "nope.json"), "--output", "json"],
        input=MANDATE, capture_output=True, text=True)
    assert r.returncode == 0
    # No manifest at all => nothing recorded for this goal => the mandate fires.
    assert json.loads(r.stdout)["fired"] is True


# --------------------------------------------------------------------------
# PRODUCTION-SHAPE PINS (fresh-eyes F-1, )
#
# Every test above passes --query-json. The iteration-close.sh call site did
# NOT, for the whole life of the first commit -- so the entire suite above was
# green against a path production never took (guard-920). Worse, the two shapes
# AGREED on the first case anyone tried by hand, because a 1-row query blob
# happens to contain the same tokens as the description it wraps. These two
# tests exist so that divergence cannot recur silently.
# --------------------------------------------------------------------------

ITERATION_CLOSE = CORE_SCRIPTS / "iteration-close.sh"


def test_call_site_passes_query_json():
    """The production invocation must pass --query-json.

    A grep, deliberately: the semantic test below proves WHY the flag matters,
    but only this one fails when someone edits the call site and drops it.
    """
    text = ITERATION_CLOSE.read_text(encoding="utf-8")
    assert "mandated-retrieval-check.py" in text, "call site vanished from iteration-close.sh"
    # Locate the invocation and assert the flag rides on it.
    idx = text.index("mandated-retrieval-check.py")
    window = text[idx:idx + 400]
    assert "--query-json" in window, (
        "call site invokes mandated-retrieval-check.py WITHOUT --query-json; "
        "the checker will match the raw query JSON blob (title, outcome_note, "
        "everything) instead of the extracted description -- the exact narration "
        "false positive this gate exists to prevent (F-1)")


def test_raw_blob_shape_false_positives_on_title_only_match(tmp_path):
    """The discriminating case that proves --query-json is load-bearing.

    A goal whose DESCRIPTION carries no mandate, but whose TITLE contains the
    tokens. Fed as raw text (the pre-fix production shape) the checker fires --
    a false positive. Fed with --query-json it extracts the description and
    stays correctly silent.
    """
    blob = json.dumps([{
        "id": "g-999-01",
        "title": "Investigate why retrieve.sh --category x --goal g-999-01 was never run",
        "description": "No mandate here. This goal only discusses the topic.",
    }])
    sess = tmp_path / "retrieval-session.json"
    sess.write_text(json.dumps({"goal_id": "g-999-01",
                                "retrieval_performed": False}), encoding="utf-8")

    def run(*extra):
        return subprocess.run(
            [sys.executable, str(_SCRIPT), "--goal-id", "g-999-01",
             "--session-file", str(sess), "--output", "json", *extra],
            input=blob, capture_output=True, text=True)

    wrong = run()                  # pre-fix production shape
    right = run("--query-json")    # correct shape
    assert wrong.returncode == 0 and right.returncode == 0
    assert json.loads(wrong.stdout)["fired"] is True, (
        "control broken: the raw-blob shape should false-positive here")
    assert json.loads(right.stdout)["fired"] is False, (
        "--query-json must extract the description and suppress the title match")
