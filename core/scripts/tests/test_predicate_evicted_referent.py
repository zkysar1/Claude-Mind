"""goal_completed_after / vcs_commits_since against an EVICTED referent ().

THE DEFECT THIS PINS. A terminal goal is EVICTED from its aspiration's `goals[]`
after `aspirations_eviction.age_days` and survives only as a bare id in
`archived_census.evicted_ids`. `predicate.py` resolved referents with a
hand-rolled live+archive+bound-agent scan that never read that census, so a
`goal_completed_after` precondition whose prerequisite HAD COMPLETED returned
`passed=False, reason="goal X not found in live or archive"` — freezing its
dependent goal forever while reporting an ordinary not-yet. Four live HIGH goals
were frozen that way (g-372-08 x2, g-326-882, g-358-40). guard-6762 / guard-5278
/ guard-4748 are the rules; this file is the mechanical guard.

THREE DISPOSITIONS ARE PINNED SEPARATELY, and the separation is the point — a
test that only pinned "evicted passes" would be satisfied by a change that made
EVERYTHING pass:
  evicted+completed -> passes (no timestamp exists; decided on disposition)
  evicted+skipped   -> fails, still EVALUABLE (a decided no, not a wait)
  unknown           -> UNEVALUABLE (goal-selector's PERMANENT class)
plus the observer-independence case: a referent in a NON-bound agent's queue
must resolve, because the old scan appended only `MIND_AGENT`'s queue and so
answered differently depending on who was running (g-353-108 defect 2).

Sibling: g-115-9191 asks for the same pin on dependency-cycle-check.py, whose
dangling_edges scan is the other consumer of this one blind spot.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import predicate  # noqa: E402


CUTOFF = "iso:2026-09-01T00:00:00"


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp world + agents root wired into predicate's resolution path.

    Returns a callable: build(live_rows, archive_rows=(), agent_queues={}).
    `agent_queues` maps agent-name -> aspiration rows, so a test can place a
    referent in an agent that is NOT the bound one.
    """
    import aspirations as asp_mod

    def build(live_rows, archive_rows=(), agent_queues=None):
        live = _write_jsonl(tmp_path / "world" / "aspirations.jsonl", live_rows)
        arch = _write_jsonl(tmp_path / "world" / "aspirations-archive.jsonl",
                            list(archive_rows))
        agents = tmp_path / "agents"
        for name, rows in (agent_queues or {}).items():
            _write_jsonl(agents / name / "aspirations.jsonl", rows)
        agents.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(asp_mod, "LIVE_PATH", live, raising=False)
        monkeypatch.setattr(asp_mod, "ARCHIVE_PATH", arch, raising=False)
        monkeypatch.setattr(predicate, "_agents_root", lambda: agents)
        # The bound agent is deliberately one that owns NO queue here, so any
        # hit in `other-agent` below can only come from the all-queues scan.
        monkeypatch.setenv("MIND_AGENT", "bound-agent")
    return build


def _asp_with_census(asp_id, evicted, goals=()):
    return {"id": asp_id, "title": asp_id, "goals": list(goals),
            "archived_census": {"evicted_ids": evicted}}


def _eval_completed_after(goal_id):
    return predicate.evaluate({"id": "pc", "type": "goal_completed_after",
                               "goal_id": goal_id, "after_ref": CUTOFF})


# --------------------------------------------------------------------------
# disposition resolution
# --------------------------------------------------------------------------

def test_evicted_completed_resolves_from_census(world):
    world([_asp_with_census("asp-900", {"completed": ["g-900-01"]})])
    rec, disp, status = predicate._resolve_goal_referent("g-900-01")
    assert rec is None
    assert (disp, status) == ("evicted", "completed")


def test_live_record_still_wins_over_census(world):
    """Positive control: an ordinary live record must resolve as `live`. If this
    ever reports `evicted`, the census pass is running ahead of the record scan
    and every timestamp comparison below it has been silently skipped."""
    world([{"id": "asp-900", "goals": [{"id": "g-900-02", "status": "completed",
                                        "completed_date": "2026-09-10"}]}])
    rec, disp, status = predicate._resolve_goal_referent("g-900-02")
    assert rec is not None and disp == "live" and status == "completed"


def test_referent_in_another_agents_queue_resolves(world):
    """ defect 2 — observer independence. MIND_AGENT is `bound-agent`,
    which owns no queue; the referent lives in `other-agent`. Pre-fix the scan
    appended only the bound agent's file, so this returned None on every box but
    one and the SAME predicate gave different answers to different sessions."""
    world([], agent_queues={"other-agent": [
        {"id": "asp-901", "goals": [{"id": "g-901-01", "status": "completed",
                                     "completed_date": "2026-09-10"}]}]})
    rec, disp, _ = predicate._resolve_goal_referent("g-901-01")
    assert rec is not None and disp == "agent"


def test_unknown_referent_is_unknown_not_evicted(world):
    world([_asp_with_census("asp-900", {"completed": ["g-900-01"]})])
    assert predicate._resolve_goal_referent("g-900-99") == (None, "unknown", None)


# --------------------------------------------------------------------------
# goal_completed_after verdicts
# --------------------------------------------------------------------------

def test_evicted_completed_precondition_passes(world):
    """THE CORE CASE. Pre-fix: passed=False, "not found in live or archive",
    forever. The census carries no instant, so the cutoff comparison is NOT
    made — the reason string must say so rather than imply a measurement."""
    world([_asp_with_census("asp-900", {"completed": ["g-900-01"]})])
    r = _eval_completed_after("g-900-01")
    assert r.passed is True, r.reason
    assert r.evaluable is True
    assert "evicted-completed" in r.reason
    assert "NOT made" in r.reason
    assert r.observed_value["completed_at"] is None


@pytest.mark.parametrize("status", ["skipped", "expired", "superseded"])
def test_evicted_but_not_completed_fails_and_stays_evaluable(world, status):
    """A goal that reached a terminal state WITHOUT completing is a decided NO.
    It must not pass (it never completed) and must not go unevaluable (the
    question was answered) — the branch that distinguishes those is what keeps
    `evicted` from becoming a blanket pass."""
    world([_asp_with_census("asp-900", {status: ["g-900-03"]})])
    r = _eval_completed_after("g-900-03")
    assert r.passed is False
    assert r.evaluable is True
    assert status in r.reason


def test_unknown_referent_is_unevaluable(world):
    """ item 3. No world change can ever resolve this, so re-probing it
    every 2h forever is the defect; `evaluable=False` puts it in goal-selector's
    existing PERMANENT class ("fix the predicate")."""
    world([_asp_with_census("asp-900", {"completed": ["g-900-01"]})])
    r = _eval_completed_after("g-900-99")
    assert r.passed is False
    assert r.evaluable is False, r.reason
    assert "no record in any queue" in r.reason
    # gates/check_schema.py substring-matches this reason to decide whether a
    # check is SCHEMA-invalid at filing time. "unresolvable" is one of its
    # SCHEMA_REASONS, so using that word here would make the filing gate REFUSE
    # a precondition that forward-references a goal not yet filed — the
    # load-bearing `ok` case of test_check_schema_gate. Pin the exclusion: the
    # selection-time verdict (evaluable=False) and the filing-time verdict
    # (well-formed) must be able to disagree.
    assert "unresolvable" not in r.reason


def test_live_completed_still_compares_timestamps(world):
    """Regression guard in the other direction: for a referent that DOES have a
    record, the cutoff comparison must still run and still be able to FAIL."""
    world([{"id": "asp-900", "goals": [
        {"id": "g-900-04", "status": "completed", "completed_date": "2026-08-01"},
        {"id": "g-900-05", "status": "completed", "completed_date": "2026-09-10"}]}])
    assert _eval_completed_after("g-900-04").passed is False   # before cutoff
    assert _eval_completed_after("g-900-05").passed is True    # after cutoff
