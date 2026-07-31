"""Tests for human-blocked-defer-join.py ().

The sweep joins ARRIVING HUMAN SIGNALS against `human_blocked` defers — the one
defer class no re-probe sweep can cover, because what satisfies it is a message,
not a script exit code. Detective only; there is deliberately no `--apply`
(guard-1249), so these tests pin the two things that CAN still go wrong: the
signal CLASSIFICATION, and the refusal to render an unreadable source as a
clean zero.

Every fixture is a SHAPE MEASURED ON THE LIVE FLEET on 2026-07-31, not invented:

  g-326-69 / g-350-52 / g-350-68  three defers, one shared premise, all citing
                                  the fleet's ONE retired pq   -> pq_retired x3
                                                               -> cluster of 3
  g-115-2050 / g-115-3647         board post newer than the defer names the goal
                                  -> board_directive (heuristic ONLY)

Two pins below are the ones worth reading before touching the parser:

`test_trailing_period_does_not_break_the_lookup` is the regression this script
nearly SHIPPED. `.` is legal INSIDE a pq id, so the id pattern must allow it —
and on the first live run that let a sentence-final period ride along, three
defers reported `pq_missing` against ids that were real and one character wide.
That is a parser MANUFACTURING a confident negative claim: the emitted finding
would have asserted a human's pending question was never filed
(verify-before-assuming.md — a negative produced by a parser is still a
negative). It was caught by a plain grep of the pq files, which is the cheap
second signal the rule asks for.

`test_retired_is_not_answered` pins the distinction the FIRST version of this
sweep got silently wrong by omission. `retired` means the question was
WITHDRAWN — the clearing path is dead, not satisfied — so it demands the
OPPOSITE action from `answered`. Sitting in neither branch, the fleet's three
retired-pq defers produced no signal at all and fell straight through the sweep
written to catch exactly them.

Pattern: same importlib + sys.path shape as test_blocked_signal_resolution_check.py
(the script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "human-blocked-defer-join.py"


def _import():
    spec = importlib.util.spec_from_file_location("human_blocked_defer_join", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["human_blocked_defer_join"] = mod
    spec.loader.exec_module(mod)
    return mod


hbdj = _import()


def _goal(gid: str, reason: str, **kw) -> dict:
    g = {
        "id": gid,
        "status": "pending",
        "defer_reason": reason,
        "defer_reason_set_at": "2026-07-29T12:00:00",
        "_source": "world",
        "_aspiration_id": "asp-999",
        "title": "test goal",
        "intended_agent": "either",
    }
    g.update(kw)
    return g


def _run(monkeypatch, capsys, goals, pq=None, msgs=None,
         goal_err=None, pq_err=None, board_err=None, argv=("--output", "json")):
    """Drive main() end-to-end with the three I/O boundaries stubbed.

    Only the boundaries are faked — the premise grouping, the pq classification
    ladder and the verdict logic all execute for real.
    """
    monkeypatch.setattr(
        hbdj, "_read_goals",
        lambda source: ([g for g in goals if g["_source"] == source], goal_err))
    monkeypatch.setattr(hbdj, "_read_pending_questions", lambda: (pq or {}, pq_err))
    monkeypatch.setattr(hbdj, "_read_board", lambda ch, since: (msgs or [], board_err))
    monkeypatch.setattr(sys, "argv", ["human-blocked-defer-join.py", *argv])
    rc = hbdj.main()
    return rc, json.loads(capsys.readouterr().out)


def _sig(result, goal_id):
    for r in result["records"]:
        if r["goal_id"] == goal_id:
            return {s["signal"]: s for s in r["signals"]}
    return {}


# ── The parser pin: a sentence-final period is not part of the id ─────────

def test_trailing_period_does_not_break_the_lookup(monkeypatch, capsys):
    """THE regression. `.` is legal INSIDE a pq id, so it must be right-trimmed
    rather than excluded — otherwise a defer whose sentence ends in the id
    emits a CONFIDENT `pq_missing` against an id that exists."""
    goals = [_goal("g-350-68",
                   "human_blocked: wsl2_localhost_relay_down on the Studio host "
                   "(pq-fox-wsl-relay-restart).")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-fox-wsl-relay-restart": "retired"})
    sigs = _sig(res, "g-350-68")
    assert "pq_missing" not in sigs, (
        "the trailing period was swallowed into the id — this is the parser "
        "manufacturing a false negative claim about a human's filed question")
    assert sigs["pq_retired"]["pq"] == "pq-fox-wsl-relay-restart"


@pytest.mark.parametrize("suffix", [".", ",", ")", "):", "]", "};", "-", "_"])
def test_every_trailing_punctuation_form_trims(monkeypatch, capsys, suffix):
    goals = [_goal("g-1", f"human_blocked: relay_down see pq-real-question{suffix}")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real-question": "answered"})
    assert "pq_answered" in _sig(res, "g-1"), f"suffix {suffix!r} broke the lookup"


def test_dot_inside_an_id_is_preserved(monkeypatch, capsys):
    """The reason `.` is in the character class at all — trimming must be
    right-anchored, never a blanket exclusion."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-v1.2-migration and stop")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-v1.2-migration": "answered"})
    assert _sig(res, "g-1")["pq_answered"]["pq"] == "pq-v1.2-migration"


def test_genuinely_absent_id_still_reports_missing(monkeypatch, capsys):
    """The trim must not blunt the real signal it was masking."""
    goals = [_goal("g-1", "human_blocked: relay_down blocked on pq-never-filed.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-something-else": "answered"})
    sigs = _sig(res, "g-1")
    assert sigs["pq_missing"]["confidence"] == "none"
    assert sigs["pq_missing"]["pq"] == "pq-never-filed"


# ── `none` is a real rung: nothing arrived is not weak evidence ───────────

def test_missing_only_record_is_not_labelled_heuristic(monkeypatch, capsys):
    """Found by the mandated re-read of the phase pseudocode. Under the earlier
    deterministic/heuristic BINARY, a record whose only signal was `pq_missing`
    ranked `heuristic` — so the precheck renderer announced a board post that was
    never found. That is this sweep asserting evidence it never saw, i.e. the
    exact failure class it exists to catch, turned on itself."""
    goals = [_goal("g-1", "human_blocked: relay_down blocked on pq-never-filed.")]
    _, res = _run(monkeypatch, capsys, goals, pq={})
    assert res["records"][0]["best_confidence"] == "none"


def test_stronger_signal_wins_when_both_present(monkeypatch, capsys):
    """A broken citation alongside a real board post must not drag the rank down."""
    goals = [_goal("g-1", "human_blocked: relay_down blocked on pq-never-filed.")]
    msgs = [{"id": "m1", "timestamp": "2026-07-30T09:00:00", "text": "about g-1"}]
    _, res = _run(monkeypatch, capsys, goals, pq={}, msgs=msgs)
    assert res["records"][0]["best_confidence"] == "heuristic"


def test_confidence_rank_is_a_total_order_over_emitted_confidences():
    """Every confidence the script can emit must be rankable; an unranked value
    would silently sort as 0 and be rendered in the wrong bucket."""
    assert hbdj._CONF_RANK == {"deterministic": 2, "heuristic": 1, "none": 0}


def test_records_sort_deterministic_then_heuristic_then_none(monkeypatch, capsys):
    goals = [_goal("g-c", "human_blocked: p_one blocked on pq-never-filed."),
             _goal("g-b", "human_blocked: p_two needs a window"),
             _goal("g-a", "human_blocked: p_three per pq-real.")]
    msgs = [{"id": "m1", "timestamp": "2026-07-30T09:00:00", "text": "about g-b"}]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"}, msgs=msgs)
    assert [r["goal_id"] for r in res["records"]] == ["g-a", "g-b", "g-c"]
    assert [r["best_confidence"] for r in res["records"]] == [
        "deterministic", "heuristic", "none"]


# ── The classification pin: retired is the OPPOSITE of answered ───────────

def test_retired_is_not_answered(monkeypatch, capsys):
    """`retired` = the question was WITHDRAWN, so the clearing path is DEAD.
    Reading it as satisfied would authorise clearing a defer that can never be
    satisfied as written."""
    goals = [_goal("g-326-69", "human_blocked: relay_down per pq-fox-wsl-relay-restart.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-fox-wsl-relay-restart": "retired"})
    sigs = _sig(res, "g-326-69")
    assert "pq_retired" in sigs and "pq_answered" not in sigs
    assert sigs["pq_retired"]["confidence"] == "deterministic"
    assert "do NOT read this as granted" in sigs["pq_retired"]["detail"]


def test_retired_status_sets_are_disjoint():
    """A future editor folding `retired` into ANSWERED_STATUSES to 'simplify'
    would silently restore the exact defect this branch exists for."""
    assert not set(hbdj.ANSWERED_STATUSES) & set(hbdj.RETIRED_STATUSES)


@pytest.mark.parametrize("status", ["answered", "resolved"])
def test_answered_statuses_are_deterministic(monkeypatch, capsys, status):
    goals = [_goal("g-1", "human_blocked: relay_down waiting on pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": status})
    assert _sig(res, "g-1")["pq_answered"]["confidence"] == "deterministic"


@pytest.mark.parametrize("status", ["pending", "in-progress", ""])
def test_still_open_question_emits_no_signal(monkeypatch, capsys, status):
    """A live pq means the defer is CORRECTLY held — silence is the right output."""
    goals = [_goal("g-1", "human_blocked: relay_down waiting on pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": status})
    assert res["records"] == [] and res["verdict"] == "clean"


# ── The board signal is evidence, never a verdict ─────────────────────────

def test_board_signal_stays_heuristic(monkeypatch, capsys):
    goals = [_goal("g-115-2050", "human_blocked: requires a fleet-quiesced window")]
    msgs = [{"id": "m1", "author": "user", "channel": "decisions",
             "timestamp": "2026-07-30T09:00:00", "text": "go ahead on g-115-2050"}]
    _, res = _run(monkeypatch, capsys, goals, msgs=msgs)
    sigs = _sig(res, "g-115-2050")
    assert sigs["board_directive"]["confidence"] == "heuristic"
    assert _sig(res, "g-115-2050") and res["deterministic_count"] == 0
    assert res["records"][0]["best_confidence"] == "heuristic"


def test_board_post_predating_the_defer_is_excluded(monkeypatch, capsys):
    """A message written BEFORE the block cannot have granted it."""
    goals = [_goal("g-1", "human_blocked: quiesced_window needed",
                   defer_reason_set_at="2026-07-30T00:00:00")]
    msgs = [{"id": "m1", "timestamp": "2026-07-28T09:00:00", "text": "about g-1"}]
    _, res = _run(monkeypatch, capsys, goals, msgs=msgs)
    assert res["records"] == []


def test_deterministic_sorts_ahead_of_heuristic(monkeypatch, capsys):
    goals = [_goal("g-zzz", "human_blocked: quiesced_window needed"),
             _goal("g-aaa", "human_blocked: relay_down per pq-real.")]
    msgs = [{"id": "m1", "timestamp": "2026-07-30T09:00:00", "text": "about g-zzz"}]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"}, msgs=msgs)
    assert [r["goal_id"] for r in res["records"]] == ["g-aaa", "g-zzz"]


# ── guard-1249: the shared-premise cluster must be visible ────────────────

def test_shared_premise_cluster_is_surfaced(monkeypatch, capsys):
    """Three live defers named one Studio host. Batch-clearing that cluster on a
    single probe is precisely what guard-1249 forbids, so the count is reported."""
    goals = [_goal(g, "human_blocked: wsl2_localhost_relay_down per pq-fox.")
             for g in ("g-326-69", "g-350-52", "g-350-68")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-fox": "retired"})
    assert res["shared_premise_clusters"] == {"wsl2_localhost_relay_down": 3}
    assert all(r["shared_premise_count"] == 3 for r in res["records"])


def test_unclustered_premise_is_not_reported_as_a_cluster(monkeypatch, capsys):
    goals = [_goal("g-1", "human_blocked: relay_down per pq-fox.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-fox": "answered"})
    assert res["shared_premise_clusters"] == {}
    assert res["records"][0]["shared_premise_count"] == 1


# ── rb-245: a read failure must never render as a clean zero ──────────────

def test_unreadable_source_is_not_clean(monkeypatch, capsys):
    """The failure mode this sweep must not have. 'Nothing to surface' because
    nothing was READ would hide the class it exists to catch, forever."""
    _, res = _run(monkeypatch, capsys, [], goal_err="world read failed: 500")
    assert res["verdict"] == "unreadable"
    assert res["errors"], "the reason must travel with the verdict"


def test_readable_but_empty_is_clean(monkeypatch, capsys):
    _, res = _run(monkeypatch, capsys, [])
    assert res["verdict"] == "clean" and res["errors"] == []


def test_partial_read_failure_with_hits_still_reports_hits(monkeypatch, capsys):
    """Errors are carried even when the readable half produced records — the
    caller needs both, not a verdict that hides one."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"},
                  board_err="decisions: daemon down")
    assert res["verdict"] == "hits" and res["errors"] == ["decisions: daemon down"]


# ── Scope + posture ──────────────────────────────────────────────────────

def test_only_human_blocked_defers_are_examined(monkeypatch, capsys):
    """The agent-provisionable classes have their own re-probe sweeps (0.5b.4 /
    0.5b.9); double-covering them here would duplicate their findings."""
    goals = [_goal("g-1", "credential_blocked: needs a key, see pq-real."),
             _goal("g-2", "blocked on a partner, see pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"})
    assert res["human_blocked_defers"] == 0 and res["records"] == []


@pytest.mark.parametrize("status", ["completed", "skipped", "expired"])
def test_terminal_goals_are_ignored(monkeypatch, capsys, status):
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.", status=status)]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"})
    assert res["human_blocked_defers"] == 0


def test_never_mutates(monkeypatch, capsys):
    """Detective only (guard-1249). A keyword join proves a message MENTIONS a
    goal, never that it GRANTS the goal's specific blocking condition."""
    def _boom(*a, **k):
        raise AssertionError("the sweep attempted a daemon write")

    monkeypatch.setattr(hbdj._rt, "rt_call", _boom)
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    rc, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"})
    assert rc == 0 and res["mutates"] is False


def test_always_exits_zero_even_on_hits(monkeypatch, capsys):
    """A precheck detective must never block the loop."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    rc, _ = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"})
    assert rc == 0


def test_text_output_names_the_cluster_and_the_guard(monkeypatch, capsys):
    """The text path is what a reader actually sees in the precheck stream."""
    goals = [_goal(g, "human_blocked: wsl2_localhost_relay_down per pq-fox.")
             for g in ("g-1", "g-2")]
    monkeypatch.setattr(
        hbdj, "_read_goals",
        lambda source: ([g for g in goals if g["_source"] == source], None))
    monkeypatch.setattr(hbdj, "_read_pending_questions",
                        lambda: ({"pq-fox": "answered"}, None))
    monkeypatch.setattr(hbdj, "_read_board", lambda ch, since: ([], None))
    monkeypatch.setattr(sys, "argv", ["human-blocked-defer-join.py"])
    assert hbdj.main() == 0
    out = capsys.readouterr().out
    assert "guard-1249" in out and "wsl2_localhost_relay_down" in out
    assert "2 defers share premise" in out


def test_pq_cited_twice_is_reported_once(monkeypatch, capsys):
    goals = [_goal("g-1", "human_blocked: relay_down pq-real ... again pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"})
    assert len(res["records"][0]["signals"]) == 1
