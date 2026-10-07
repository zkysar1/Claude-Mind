"""test_goal_selector_review_hold.py --  (close-review plan step 4b).

Pins the close-review hold in goal-selector.py: while close-review check A is on, a
goal carrying a review request that no verdict answers yet is not a candidate, and
collect_blocked reports it as `awaiting_review`.

WHY THE HOLD EXISTS. When check A refuses a close it stamps review_requested
(g-375-147), and the loop then releases the goal (Phase 5.3), which clears only the
claim. Without a hold the goal is pending again at once, it is offered again, and
each re-pick is refused the same way until three in a row trip the circuit breaker
(Phase 5.5), which defers the goal and notifies the user. A review request is
already a hold in the completed-not-closed drain (g-375-143); this is the same rule
on the selection side.

WHAT IS PINNED:
  1. `_awaiting_review` decisions over the real queue and gate modules, with verdict
     files in a tmp dir (CLOSE_REVIEW_LEDGER_DIR): no verdict holds; an answering
     verdict lifts the hold, a REJECT included; a verdict from before the request
     does not; an unreadable verdict file counts as no verdict.
  2. Dormant with check A off, and the request test short-circuits before the
     queue loads at all.
  3. A queue that cannot load fails OPEN, says so once, and caches the answer.
  4. collect_candidates drops a held goal; collect_blocked names it with a dict
     blocker_ref, so quiescence can still fire on a queue that only waits for reviews.
  5. THE SYMMETRY INVARIANT (g-115-3150): the goal is in exactly one list.
  6. With check A off, carrying a request changes no ranking and no block row.
  7. No circularity (rb-9816): the review queue still offers the goal the selector
     holds, to a reviewer other than its closer.
  8. A replayed refused loop close: the real gate's refusal, the loop's release, and
     then no candidate until a verdict answers the request.

Harness mirrors test_goal_selector_fresh_session_marker.py: pin MIND_AGENT around
import, per-test AGENT_NAME pin, neutralize the orthogonal capability filter. No
subprocess, no store, no daemon.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

ASKED = "2026-10-06T09:00:00"


def _goal(gid, requested=None, **kw):
    """Minimal pending, agent-eligible, unclaimed, non-recurring goal."""
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if requested:
        g["review_requested"] = requested
    g.update(kw)
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _verdict(tmp_path, gid, verdict, reviewed_at):
    """One verdict as the producer writes it: a list trail whose last entry wins."""
    d = tmp_path / "audit-reports" / "close-reviews"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.json" % gid)).write_text(json.dumps(
        [{"verdict": verdict, "reviewer": "peer-mind", "reviewed_at": reviewed_at}]),
        encoding="utf-8")


def _queue():
    """close-review-queue.py the way the selector loads it: sys.modules first, else by
    path under the same name, so the test and the hold share one module."""
    crq = sys.modules.get("close_review_queue")
    if crq is None:
        spec = importlib.util.spec_from_file_location(
            "close_review_queue", CORE_SCRIPTS / "close-review-queue.py")
        crq = importlib.util.module_from_spec(spec)
        sys.modules["close_review_queue"] = crq
        spec.loader.exec_module(crq)
    return crq


def _pin(monkeypatch, tmp_path, check_a):
    """Agent identity pinned, capability filter neutral, verdicts read from tmp, check A
    set as asked, and the hold's per-process cache cleared so it re-reads that flag.

    OFF is forced, never left to the shipped config: the env can only switch check A
    on, so an unset env reads aspirations.yaml, and the dormancy tests would then pass
    only while the config ships check A off, and go red the day it is switched on
    (guard-6333). The gate's own flag read is wrapped, so `_review_hold` still runs its
    real check against it."""
    monkeypatch.setattr(gs, "AGENT_NAME", "alpha")
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    monkeypatch.setattr(gs, "_REVIEW_HOLD", None)
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    if check_a:
        monkeypatch.setenv("CLOSE_REVIEW_GATE_ENABLED", "1")
    else:
        monkeypatch.delenv("CLOSE_REVIEW_GATE_ENABLED", raising=False)
        gate = _queue()._gate()
        flags = gate._flags
        monkeypatch.setattr(gate, "_flags", lambda: {**flags(), "enabled": False})


# -- 1. the decision, over the real queue and gate ----------------------------

@pytest.mark.parametrize("verdict,held", [
    (None, True),
    (("APPROVE", "2026-10-06T10:00:00"), False),
    (("REJECT", "2026-10-06T10:00:00"), False),
    (("APPROVE", "2026-10-06T08:00:00"), True),
    (("REJECT", "2026-10-06T08:00:00"), True),
], ids=["no-verdict", "APPROVE-answers", "REJECT-answers", "APPROVE-before-the-ask",
        "REJECT-before-the-ask"])
def test_a_request_is_held_until_a_verdict_answers_it(monkeypatch, tmp_path, verdict, held):
    """Any answering verdict lifts the hold: a REJECT returns the goal for rework, which
    is selection's job. A verdict from before the request answers nothing."""
    _pin(monkeypatch, tmp_path, check_a=True)
    if verdict:
        _verdict(tmp_path, "g-held", *verdict)
    assert gs._awaiting_review("g-held", _goal("g-held", ASKED)) is held


def test_an_unreadable_verdict_file_counts_as_no_verdict(monkeypatch, tmp_path):
    """As in the drain: a verdict that cannot be read cannot answer the request."""
    _pin(monkeypatch, tmp_path, check_a=True)
    d = tmp_path / "audit-reports" / "close-reviews"
    d.mkdir(parents=True)
    (d / "g-held.json").write_text("{not json", encoding="utf-8")
    assert gs._awaiting_review("g-held", _goal("g-held", ASKED)) is True


# -- 2. dormant with check A off, and the short-circuit -------------------------

def test_check_A_off_holds_nothing(monkeypatch, tmp_path):
    """With check A off the hold is dormant, whatever the shipped config says."""
    _pin(monkeypatch, tmp_path, check_a=False)
    assert gs._awaiting_review("g-held", _goal("g-held", ASKED)) is False


def test_a_goal_with_no_request_never_loads_the_queue(monkeypatch, tmp_path):
    """The request test MUST come first: if the order ever inverts, every goal in the
    queue pays the load, and this raises instead."""
    _pin(monkeypatch, tmp_path, check_a=True)

    def _boom():
        raise AssertionError("the review hold loaded for a goal with no request")
    monkeypatch.setattr(gs, "_review_hold", _boom)
    assert gs._awaiting_review("g-plain", _goal("g-plain")) is False


# -- 3. a queue that cannot load fails open, loudly, once -----------------------

def test_a_queue_that_cannot_load_holds_nothing_and_says_so(monkeypatch, tmp_path, capsys):
    """A filter that cannot read its input must not hide work. The answer is cached, so
    the fault is printed once per run, not once per goal."""
    _pin(monkeypatch, tmp_path, check_a=True)

    class _Broken:
        @staticmethod
        def _gate():
            raise RuntimeError("queue half-loaded")

    monkeypatch.setitem(sys.modules, "close_review_queue", _Broken())
    assert gs._awaiting_review("g-held", _goal("g-held", ASKED)) is False
    assert gs._awaiting_review("g-held", _goal("g-held", ASKED)) is False
    err = capsys.readouterr().err
    assert err.count("could not load") == 1, err
    assert gs._REVIEW_HOLD is False


# -- 4. the two lists -----------------------------------------------------------

def test_candidates_drop_a_held_goal_and_keep_the_rest(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path, check_a=True)
    goals = [_goal("g-held", ASKED), _goal("g-plain")]
    ids = {c["goal"]["id"] for c in gs.collect_candidates(_asps(goals), source="world")}
    assert "g-held" not in ids     # before : PRESENT, and re-picked at once
    assert "g-plain" in ids


def test_blocked_names_a_held_goal_with_a_blocker_ref(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path, check_a=True)
    rows = {e["goal_id"]: e for e in gs.collect_blocked(_asps([_goal("g-held", ASKED)]))}
    row = rows["g-held"]
    assert row["block_reason"] == "awaiting_review"
    assert ASKED in row["block_detail"]
    ref = row["blocker_ref"]
    assert isinstance(ref, dict) and ref["type"] == "resource", ref
    assert ref["external_id"].startswith("awaiting-review:"), ref


def test_an_answered_request_leaves_the_goal_a_candidate(monkeypatch, tmp_path):
    """The positive control for the two tests above: the same goal, once a verdict
    answers its request, is offered again and is not reported blocked."""
    _pin(monkeypatch, tmp_path, check_a=True)
    _verdict(tmp_path, "g-held", "REJECT", "2026-10-06T10:00:00")
    goals = [_goal("g-held", ASKED)]
    ids = {c["goal"]["id"] for c in gs.collect_candidates(_asps(goals), source="world")}
    blocked = {e["goal_id"] for e in gs.collect_blocked(_asps(goals))}
    assert "g-held" in ids and "g-held" not in blocked


# -- 5. THE SYMMETRY INVARIANT () --------------------------------------

@pytest.mark.parametrize("check_a,verdict", [
    (True, None),
    (True, ("APPROVE", "2026-10-06T10:00:00")),
    (True, ("APPROVE", "2026-10-06T08:00:00")),
    (False, None),
], ids=["held", "answered", "stale-verdict", "check-A-off"])
def test_a_requested_goal_is_in_exactly_one_list(monkeypatch, tmp_path, check_a, verdict):
    """Never in NEITHER. A hold wired into only one site drops the goal from both."""
    _pin(monkeypatch, tmp_path, check_a=check_a)
    if verdict:
        _verdict(tmp_path, "g-held", *verdict)
    goals = [_goal("g-held", ASKED), _goal("g-plain")]
    cand = {c["goal"]["id"] for c in gs.collect_candidates(_asps(goals), source="world")}
    blocked = {e["goal_id"] for e in gs.collect_blocked(_asps(goals))}
    assert ("g-held" in cand) != ("g-held" in blocked), (
        "requested goal is in %s -- SYMMETRY broken"
        % ("BOTH lists" if "g-held" in cand else "NEITHER list"))


# -- 6. with check A off, a request changes nothing -----------------------------

def test_check_A_off_ranking_and_blocks_identical_with_or_without_requests(monkeypatch, tmp_path):
    """The anti-regression half: this is what fails if the hold ever runs with check A
    off, where it must land dormant."""
    _pin(monkeypatch, tmp_path, check_a=False)
    plain = [_goal("g-a"), _goal("g-b"), _goal("g-c")]
    asked = [_goal("g-a", ASKED), _goal("g-b"), _goal("g-c", ASKED)]
    rank = lambda goals: [c["goal"]["id"]                                      # noqa: E731
                          for c in gs.collect_candidates(_asps(goals), source="world")]
    rows = lambda goals: [(e["goal_id"], e.get("block_reason"))                # noqa: E731
                          for e in gs.collect_blocked(_asps(goals))]
    assert rank(plain) == rank(asked)
    assert rows(plain) == rows(asked)
    assert not any(r == "awaiting_review" for _, r in rows(asked))


# -- 7. no circularity: the queue still offers what the selector holds ----------

def test_the_review_queue_offers_the_goal_the_selector_holds(monkeypatch, tmp_path):
    """rb-9816: a hold that also hid the goal from its reviewers would wait forever. The
    goal is shaped as a refused loop close leaves it: pending, its claim released,
    executed_by kept, the outcome note written, the request stamped, no verdict."""
    _pin(monkeypatch, tmp_path, check_a=True)
    goal = _goal("g-held", ASKED, executed_by="alpha", outcome_note="OUTCOME 1: MET - x.")
    assert gs._awaiting_review("g-held", goal) is True
    crq = gs._review_hold()
    gate = crq._gate()
    reviewed = {"g-held"} if crq.answers(
        gate.read_verdict(gate.verdict_path("g-held")), ASKED) else set()
    out = crq.select_requests([goal], reviewed=reviewed, reviewer="bravo")
    assert [r["goal_id"] for r in out["rows"]] == ["g-held"], out
    # The same goal is not offered to its own closer, the queue's independence rule.
    own = crq.select_requests([goal], reviewed=reviewed, reviewer="alpha")
    assert own["rows"] == [] and own["same_mind"] == ["g-held"], own


# -- 8. replay: a refused loop close is not picked again until a verdict answers ----

def test_a_replayed_refused_loop_close_is_held_until_a_verdict_answers(
        monkeypatch, tmp_path, capsys):
    """Outcome 3, over the real gate and the real hold. Replay one refused loop close:
    close-review-gate.py refuses a tier-2 close and writes the request and the note (its
    two store writes spied onto /bin/true), the loop's Phase 5.3 release clears only the
    claim, and the selector then offers nothing. Phase 5.3 counts a refusal toward the
    circuit breaker only when the same goal fails again, so a goal that cannot be picked
    cannot push consecutive_goal_failures past 1. A REJECT answering the request offers
    the goal again, for rework."""
    _pin(monkeypatch, tmp_path, check_a=True)
    monkeypatch.setenv("MIND_AGENT", "alpha")
    record = _goal("g-held", status="in-progress", priority="HIGH", goal_id="g-held",
                   claimed_by="alpha", executed_by="alpha")
    spec = importlib.util.spec_from_file_location(
        "_crg_replay", CORE_SCRIPTS / "close-review-gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    def _spy(script, *args):
        name = Path(script).name
        if name == "aspirations-update-goal.sh":       # the request stamp
            record[args[3]] = args[4]
        elif name == "closure-evidence-write.sh":      # the outcome note
            record["outcome_note"] = args[args.index("--summary") + 1]
        return ["/bin/true"]

    monkeypatch.setattr(gate, "bash_cmd", _spy)
    monkeypatch.setattr(gate, "load_goal", lambda gid, src: dict(record))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"OUTCOME 1: MET - shipped.")))
    assert gate.main(["--goal", "g-held", "--source", "world", "--summary-stdin"]) == 1
    capsys.readouterr()
    asked = record.get("review_requested")
    assert asked and record.get("outcome_note") == "OUTCOME 1: MET - shipped.", record

    # Phase 5.3: the release clears the claim and nothing else.
    record["status"] = "pending"
    record.pop("claimed_by")
    queue = _asps([record])
    assert gs.collect_candidates(queue, source="world") == []
    rows = [e for e in gs.collect_blocked(queue) if e["goal_id"] == "g-held"]
    assert [r["block_reason"] for r in rows] == ["awaiting_review"], rows

    later = (datetime.fromisoformat(asked) + timedelta(minutes=1)).isoformat(timespec="seconds")
    _verdict(tmp_path, "g-held", "REJECT", later)
    assert [c["goal"]["id"] for c in gs.collect_candidates(queue, source="world")] == ["g-held"]
