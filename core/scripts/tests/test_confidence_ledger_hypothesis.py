"""Tests for the hypothesis-resolution calibration surface ().

The ledger's first two surfaces judge reasoning-bank entries and guardrails, and
those stores barely declare confidence, so every one of their 29 rows was null
on the x-axis (rb-12446). The tree index does declare it. This surface links a
hypothesis to the tree node whose claim it tests (`tests_node`) and writes one
row when the resolution lands.

WHAT IS PINNED HERE, and why each is a real regression rather than a restatement:

  1. The verdict MAPPING. CONFIRMED maps only through the declared stance, so a
     confirmed CHALLENGE scores the node refuted (the polarity inversion
     g-115-9063 found on the adjudication surface is one missing branch away).
     A bare CORRECTED never maps to refuted (guard-2728), and outcomes match
     exactly (guard-654: 'CORRECTED' contains 'correct').
  2. The row itself: a NON-NULL declared_confidence read from the tree INDEX for
     a node whose index value is known, and `node_in_index` keeping the two
     causes of a null apart.
  3. The response parse: `record` only, never the whole body (guard-4578).
  4. WIRING (guard-1943: a passing unit test proves the function, never the
     wiring). Both wrappers call the capture after the daemon's 200 and after
     printing the record, and review-hypotheses runs its E10 recalibration AFTER
     the resolution move, so the row never reads a confidence the verdict already
     lowered.
"""
import importlib.util
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_REPO = _SCRIPTS.parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "_confidence_ledger_hyp_for_test", _SCRIPTS / "_confidence_ledger.py")
L = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(L)

MOVE_WRAPPER = (_SCRIPTS / "pipeline-move.sh").read_text(encoding="utf-8")
ADD_WRAPPER = (_SCRIPTS / "pipeline-add.sh").read_text(encoding="utf-8")
REVIEW_SKILL = (_REPO / ".claude" / "skills" / "review-hypotheses"
                / "SKILL.md").read_text(encoding="utf-8")

# A minimal index in the real layout: node keys directly under `nodes:`, fields
# one level deeper. `domain_confidence` precedes `confidence` on purpose: it must
# never be read as the declared value.
_INDEX = """\
last_updated: '2026-09-29'
nodes:
  known-node:
    children: []
    domain_confidence: 0.9
    confidence: 0.7
    file: world/knowledge/tree/x/known-node.md
  bare-node:
    file: world/knowledge/tree/x/bare-node.md
    parent: known-node
"""


def _rec(**kw):
    rec = {"id": "2026-09-29_calibration-probe", "stage": "resolved",
           "outcome": "CONFIRMED",
           "tests_node": {"key": "known-node", "stance": "supports"}}
    rec.update(kw)
    return rec


def _world(tmp_path):
    tree = tmp_path / "knowledge" / "tree"
    tree.mkdir(parents=True)
    (tree / "_tree.yaml").write_text(_INDEX, encoding="utf-8")
    return tmp_path


def _rows(world):
    led = world / L.LEDGER_NAME
    if not led.exists():
        return []
    return [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def _capture(world, rec):
    L.capture_resolution_response(json.dumps({"ok": True, "record": rec}),
                                  world_dir=world)


# --------------------------------------------------------------------------
# 1. the verdict map
# --------------------------------------------------------------------------

def test_confirmed_supports_scores_the_node_survived():
    """POSITIVE CONTROL for the map (guard-2421): without it, a function that
    always returned None would satisfy every exclusion assertion below."""
    assert L.hypothesis_truth_event(_rec()) == (
        "known-node", "survived",
        {"outcome": "CONFIRMED", "stance": "supports",
         "verdict_basis": "confirmed_supports"})


def test_confirmed_challenge_scores_the_node_refuted():
    key, verdict, extra = L.hypothesis_truth_event(
        _rec(tests_node={"key": "known-node", "stance": "challenges"}))
    assert (key, verdict) == ("known-node", "refuted")
    assert extra["verdict_basis"] == "confirmed_challenges"


def test_bare_corrected_is_never_mapped_to_refuted():
    for stance in ("supports", "challenges"):
        _, verdict, extra = L.hypothesis_truth_event(_rec(
            outcome="CORRECTED",
            tests_node={"key": "known-node", "stance": stance}))
        assert verdict == "unknown"
        assert extra["verdict_basis"] == "corrected_unjudged"


def test_explicit_node_verdict_wins_on_either_outcome():
    for outcome in ("CONFIRMED", "CORRECTED"):
        _, verdict, extra = L.hypothesis_truth_event(
            _rec(outcome=outcome, node_verdict="revised"))
        assert verdict == "revised"
        assert extra["verdict_basis"] == "node_verdict"


def test_invalid_node_verdict_is_unknown_not_guessed():
    _, verdict, extra = L.hypothesis_truth_event(_rec(node_verdict="survived!"))
    assert verdict == "unknown"
    assert extra["verdict_basis"] == "node_verdict_invalid"


def test_confirmed_without_a_valid_stance_is_unknown():
    for link in ({"key": "known-node"}, {"key": "known-node", "stance": "agrees"}):
        _, verdict, extra = L.hypothesis_truth_event(_rec(tests_node=link))
        assert verdict == "unknown"
        assert extra == {"outcome": "CONFIRMED", "stance": None,
                         "verdict_basis": "stance_missing"}


def test_outcome_is_matched_exactly():
    assert L.hypothesis_truth_event(_rec(outcome="confirmed")) is None
    assert L.hypothesis_truth_event(_rec(outcome="CONFIRMED ")) is None


def test_outcomes_that_settle_nothing_write_no_event():
    for outcome in ("EXPIRED", "UNRESOLVABLE", None):
        assert L.hypothesis_truth_event(_rec(outcome=outcome)) is None


def test_unusable_link_is_no_event():
    for link in ("known-node", {"stance": "supports"}, {"key": "  "}, None, []):
        assert L.hypothesis_truth_event(_rec(tests_node=link)) is None


def test_every_mapped_verdict_is_in_the_ledger_vocabulary():
    produced = set()
    for outcome in ("CONFIRMED", "CORRECTED"):
        for stance in ("supports", "challenges", None):
            for nv in (None, "survived", "refuted", "revised", "unknown", "bogus"):
                link = {"key": "known-node"}
                if stance:
                    link["stance"] = stance
                rec = _rec(outcome=outcome, tests_node=link)
                if nv is not None:
                    rec["node_verdict"] = nv
                produced.add(L.hypothesis_truth_event(rec)[1])
    assert produced <= set(L.VERDICTS)
    assert {"survived", "refuted", "revised", "unknown"} <= produced


# --------------------------------------------------------------------------
# 2. the row
# --------------------------------------------------------------------------

def test_capture_writes_a_non_null_row_for_a_node_with_a_known_index_value(tmp_path):
    """POSITIVE CONTROL for the row: the index says 0.7, so the row must too."""
    world = _world(tmp_path)
    _capture(world, _rec())
    rows = _rows(world)
    assert len(rows) == 1
    row = rows[0]
    assert row["entry_id"] == "known-node"
    assert row["store"] == "tree"
    assert row["declared_confidence"] == 0.7
    assert row["verdict"] == "survived"
    assert row["evidence_ref"] == "2026-09-29_calibration-probe"
    assert row["source"] == "hypothesis-resolution"
    assert row["extra"]["node_in_index"] is True
    assert "judge_model" in row and "harness" in row


def test_a_null_confidence_keeps_its_cause(tmp_path):
    world = _world(tmp_path)
    _capture(world, _rec(id="2026-09-29_bare",
                         tests_node={"key": "bare-node", "stance": "supports"}))
    _capture(world, _rec(id="2026-09-29_missing",
                         tests_node={"key": "no-such-node", "stance": "supports"}))
    bare, missing = _rows(world)
    assert (bare["declared_confidence"], bare["extra"]["node_in_index"]) == (None, True)
    assert (missing["declared_confidence"], missing["extra"]["node_in_index"]) == (None, False)


def test_an_unreadable_index_is_recorded_as_unknown_presence(tmp_path):
    _capture(tmp_path, _rec())
    (row,) = _rows(tmp_path)
    assert row["declared_confidence"] is None
    assert row["extra"]["node_in_index"] is None


def test_a_body_without_record_writes_nothing(tmp_path):
    """guard-4578: the top level of a response is never read as the record,
    even when it is shaped exactly like one."""
    world = _world(tmp_path)
    L.capture_resolution_response(json.dumps(_rec()), world_dir=world)
    L.capture_resolution_response(
        json.dumps({"error": "validation_failed", "detail": "tests_node"}),
        world_dir=world)
    assert _rows(world) == []


def test_only_a_resolved_record_is_captured(tmp_path):
    world = _world(tmp_path)
    for stage in ("discovered", "active", "archived"):
        _capture(world, _rec(stage=stage))
    assert _rows(world) == []


def test_an_unparseable_body_never_raises(tmp_path):
    L.capture_resolution_response("not json {", world_dir=tmp_path)
    L.capture_resolution_response("", world_dir=tmp_path)
    assert _rows(tmp_path) == []


def test_an_unusable_link_on_a_scored_outcome_warns(tmp_path, capsys):
    world = _world(tmp_path)
    _capture(world, _rec(tests_node="known-node"))
    assert _rows(world) == []
    assert "unusable tests_node" in capsys.readouterr().err


def test_index_lookup_matches_node_keys_only(tmp_path):
    world = _world(tmp_path)
    idx = world / "knowledge" / "tree" / "_tree.yaml"
    assert L._tree_index_lookup(idx, "known-node") == (True, 0.7)
    assert L._tree_index_lookup(idx, "bare-node") == (True, None)
    for not_a_node in ("nodes", "last_updated", "parent", "confidence", "file"):
        assert L._tree_index_lookup(idx, not_a_node) == (False, None)
    assert L.resolve_declared_confidence("known-node", "tree", world_dir=world) == 0.7


# --------------------------------------------------------------------------
# 3. wiring
# --------------------------------------------------------------------------

def _success_branches(text):
    """The text of each branch that prints the record and exits 0."""
    marker = "print(json.dumps(rec, indent=2, ensure_ascii=False))"
    parts = text.split(marker)[1:]
    return [p[:p.index("exit 0")] for p in parts]


def test_both_wrappers_capture_in_every_success_branch_after_the_print():
    for text in (MOVE_WRAPPER, ADD_WRAPPER):
        branches = _success_branches(text)
        assert len(branches) == 2  # the direct call and the autospawn retry
        for branch in branches:
            assert "_calibration_capture" in branch
        assert "from _confidence_ledger import capture_resolution_response" in text


def test_the_capture_cannot_change_stdout_or_the_exit_code():
    for text in (MOVE_WRAPPER, ADD_WRAPPER):
        body = text[text.index("_calibration_capture() {"):]
        body = body[:body.index("\n}\n")]
        assert '" >&2 || true' in body
        assert "or resp" not in body


def test_the_move_capture_fires_only_on_a_move_into_resolved():
    body = MOVE_WRAPPER[MOVE_WRAPPER.index("_calibration_capture() {"):]
    assert body.index('[ "$STAGE" = "resolved" ] || return 0') < body.index("printf")


def test_e10_recalibration_runs_after_the_resolution_move():
    move = REVIEW_SKILL.index("pipeline-move.sh <id> resolved")
    recal = REVIEW_SKILL.index('"value": "surprise-recalibration"')
    assert REVIEW_SKILL.count('"value": "surprise-recalibration"') == 1
    assert recal > move
