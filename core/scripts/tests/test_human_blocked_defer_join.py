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
         goal_err=None, pq_err=None, board_err=None, argv=("--output", "json"),
         real_pq=False):
    """Drive main() end-to-end with the three I/O boundaries stubbed.

    Only the boundaries are faked — the premise grouping, the pq classification
    ladder and the verdict logic all execute for real. `real_pq=True` leaves the
    pending-question reader unstubbed, for the tests that wire it to a fake store.
    """
    monkeypatch.setattr(
        hbdj, "_read_goals",
        lambda source: ([g for g in goals if g["_source"] == source], goal_err))
    if not real_pq:
        monkeypatch.setattr(hbdj, "_read_pending_questions",
                            lambda: (pq or {}, pq_err))
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


def test_kebab_case_premise_clusters(monkeypatch, capsys):
    """The separator the fleet actually writes. MEASURED 2026-09-13 (zeta, cc-02):
    across 19 signalled live defers the snake_case-only pattern matched ZERO, so
    `shared_premise_clusters` was permanently `{}` — indistinguishable from the
    healthy answer "no shared premises exist", which is why it survived. Four
    defers were declaring `quiet-window` membership of g-326-565 in plain text
    and the join could not see one of them. CLAUDE.md's naming rule is
    kebab-case ("no underscores"), so the writers were following the house style
    and the regex was not; the sibling test above pins the snake form, and only
    this one pins the form anybody uses."""
    goals = [_goal(g, "human_blocked: quiet-window member of g-326-565 per pq-fox.")
             for g in ("g-306-128", "g-306-294", "g-326-565", "g-372-08")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-fox": "retired"})
    assert res["shared_premise_clusters"] == {"quiet-window": 4}
    assert all(r["premise_resource"] == "quiet-window" for r in res["records"])


def test_prose_defer_is_not_mined_for_a_premise(monkeypatch, capsys):
    """The other direction, which is what makes accepting `-` safe (guard-1636 —
    a widening must be measured BOTH ways). A prose defer's first two words are
    separated by a SPACE, which is in neither character class, so no premise is
    extracted and no false cluster forms — even when a LATER word is hyphenated."""
    goals = [_goal("g-1", "human_blocked: the last agent-side path closed per pq-fox.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-fox": "answered"})
    assert res["records"][0]["premise_resource"] is None
    assert res["shared_premise_clusters"] == {}


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


def test_partial_read_failure_with_hits_reports_partial_and_keeps_the_hits(
        monkeypatch, capsys):
    """Errors are carried even when the readable half produced records — the
    caller needs both, not a verdict that hides one. The verdict is `partial`
    rather than `hits` (g-115-4265): a renderer that branches on the verdict and
    only reads `errors` under `unreadable` never sees a swallowed source."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"},
                  board_err="decisions: daemon down")
    assert res["verdict"] == "partial" and res["errors"] == ["decisions: daemon down"]
    assert [r["goal_id"] for r in res["records"]] == ["g-1"]


# ── Scope + posture ──────────────────────────────────────────────────────

def test_live_human_blocked_predicate_is_the_population_filter():
    """The 72h user digest imports this function as its second leg (),
    so it must stay exactly this lane's filter: live status AND the prefix."""
    mod = _import()
    pred = mod.is_live_human_blocked
    for status in ("pending", "in-progress", "blocked"):
        assert pred({"status": status, "defer_reason": "human_blocked: owner"})
    assert not pred({"status": "completed", "defer_reason": "human_blocked: owner"})
    assert not pred({"status": "pending",
                     "defer_reason": "precondition_unmet: human_blocked later"})
    assert not pred({"status": "pending", "defer_reason": None})
    assert not pred({"status": "pending"})


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


# ── : an incomplete pending-question map must not read as a broken
# citation. The swallowed per-agent error (F-001) and the verdict that hid
# `errors` on the non-`unreadable` branches (F-002) manufactured `pq_missing`
# against ids that were real, filed by an agent whose file this box could not
# read. `pq_missing` is a negative claim, so it needs a COMPLETE map.

def test_incomplete_map_emits_pq_unverifiable_not_pq_missing(monkeypatch, capsys):
    """VERIFY (b). The cited id is absent from what WAS read, but the read was
    known-incomplete, so the sweep cannot say the citation is broken."""
    goals = [_goal("g-1", "human_blocked: relay_down blocked on pq-unreadable-owner.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-other": "pending"},
                  pq_err="pending-questions map INCOMPLETE — bravo store: read failed")
    sigs = _sig(res, "g-1")
    assert "pq_missing" not in sigs, "an incomplete map manufactured a broken-citation claim"
    assert sigs["pq_unverifiable"]["confidence"] == "none"
    assert sigs["pq_unverifiable"]["pq"] == "pq-unreadable-owner"
    assert "NOT a broken citation" in sigs["pq_unverifiable"]["detail"]
    assert res["records"][0]["best_confidence"] == "none"


def test_known_id_survives_an_incomplete_map(monkeypatch, capsys):
    """A positive found in a readable file stays valid however incomplete the rest is."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"},
                  pq_err="pending-questions map INCOMPLETE — bravo store: read failed")
    assert _sig(res, "g-1")["pq_answered"]["confidence"] == "deterministic"
    assert res["deterministic_count"] == 1


def test_complete_map_still_reports_missing(monkeypatch, capsys):
    """The fail-safe must not blunt the real signal: pq_err None means COMPLETE."""
    goals = [_goal("g-1", "human_blocked: relay_down blocked on pq-never-filed.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-other": "pending"})
    assert "pq_missing" in _sig(res, "g-1") and "pq_unverifiable" not in _sig(res, "g-1")


def test_errors_without_hits_is_partial_not_clean(monkeypatch, capsys):
    """VERIFY (c), the clean branch. Defers exist, none carried a signal, and one
    source failed: that is not `clean`, which would assert the whole population
    was examined."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "pending"},
                  board_err="decisions: daemon down")
    assert res["records"] == []
    assert res["verdict"] == "partial" and res["errors"] == ["decisions: daemon down"]


def test_errors_travel_directly_after_the_verdict(monkeypatch, capsys):
    """A buried instrument is not an instrument (guard-5004): `errors` sat at the
    far tail of a ~33 KB payload, past every `head` a lane runner prints."""
    goals = [_goal("g-1", "human_blocked: relay_down per pq-real.")]
    _, res = _run(monkeypatch, capsys, goals, pq={"pq-real": "answered"})
    assert list(res)[:2] == ["verdict", "errors"]


# The store-of-record reader. A fake backend stands in for the store; the tmp
# agents root is the box's local mirror.

class _FakeStore:
    """`objects` maps an absolute path to bytes, or to an exception to raise."""

    def __init__(self, objects, listing, list_exc=None):
        self.objects, self.listing, self.list_exc = objects, listing, list_exc

    def read_authoritative_bytes(self, path):
        assert Path(path).is_absolute(), (
            "a relative path silently falls back to the local mirror (g-115-4256)")
        v = self.objects.get(str(path))
        if v is None:
            raise FileNotFoundError(str(path))
        if isinstance(v, Exception):
            raise v
        return v

    def list_dir(self, path):
        assert Path(path).is_absolute()
        if self.list_exc is not None:
            raise self.list_exc
        return list(self.listing)


def _pq_a(*rows):
    """Shape A: {"questions": [...]}"""
    return ("questions:\n" + "".join(
        f"- id: {i}\n  status: {s}\n" for i, s in rows)).encode()


def _pq_env(monkeypatch, tmp_path, store=None, local=None, listing=None,
            list_exc=None, backend_exc=None):
    """Point the REAL reader at a tmp agents root plus a fake store of record."""
    for name, raw in (local or {}).items():
        d = tmp_path / name / "session"
        d.mkdir(parents=True)
        (d / "pending-questions.yaml").write_bytes(raw)
    objects = {str(tmp_path / n / "session" / "pending-questions.yaml"): v
               for n, v in (store or {}).items()}
    fake = _FakeStore(objects, sorted(store or {}) if listing is None else listing,
                      list_exc)

    def _get_backend():
        if backend_exc is not None:
            raise backend_exc
        return fake

    monkeypatch.setattr(hbdj, "agents_root", lambda: tmp_path)
    monkeypatch.setattr("storage_backend.get_backend", _get_backend)


def test_corrupt_agent_file_propagates_an_error(monkeypatch, tmp_path):
    """VERIFY (a), F-001. The old reader hit `except Exception: continue` and
    returned a clean map minus that agent's questions."""
    _pq_env(monkeypatch, tmp_path, store={
        "alpha": _pq_a(("pq-a-1", "pending")),
        "bravo": b"questions:\n- id: pq-b-1\n  note: [unclosed\n  status: pending\n"})
    pq, err = hbdj._read_pending_questions()
    assert err is not None and "bravo" in err and "alpha" not in err
    assert pq == {"pq-a-1": "pending"}, "the readable agent's rows must survive"


def test_error_text_carries_no_file_content(monkeypatch, tmp_path):
    """A pending-questions file holds questions addressed to the owner. The error
    names the agent, the exception type and the parser position, never a snippet."""
    _pq_env(monkeypatch, tmp_path, store={
        "bravo": b"questions:\n- id: pq-b-1\n  note: [CANARY-OWNER-TEXT\n"})
    _, err = hbdj._read_pending_questions()
    assert err is not None and "bravo" in err
    assert "Error" in err and "line " in err, "type and parser position must be named"
    assert "CANARY" not in err


def test_truncated_file_parsing_as_a_scalar_is_a_failure(monkeypatch, tmp_path):
    """A write cut short mid-key parses as a bare string, which the old flatten
    read as "no questions" — a clean-looking empty agent."""
    _pq_env(monkeypatch, tmp_path, store={"alpha": b"questi"})
    _, err = hbdj._read_pending_questions()
    assert err is not None and "alpha" in err and "unrecognized top-level shape" in err


def test_a_constructor_value_error_is_a_failure_not_a_crash(monkeypatch, tmp_path):
    """`safe_load` raises ValueError, not YAMLError, for a value such as an
    impossible date. The old blanket `except Exception` absorbed it; narrowing the
    catch to YAML errors alone would turn one odd value into a dead precheck lane."""
    _pq_env(monkeypatch, tmp_path, store={
        "alpha": b"questions:\n- id: pq-1\n  created: 2026-02-30\n  status: pending\n"})
    pq, err = hbdj._read_pending_questions()
    assert pq == {} and err is not None and "alpha" in err and "ValueError" in err


def test_corrupt_local_mirror_is_moot_beside_a_good_store_copy(monkeypatch, tmp_path):
    """The store is the record: a half-synced mirror beside a readable store copy
    must not turn every cited id of that agent `pq_unverifiable`."""
    _pq_env(monkeypatch, tmp_path,
            store={"alpha": _pq_a(("pq-a-1", "answered"))},
            local={"alpha": b"questions: [unclosed\n"})
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-a-1": "answered"}


def test_corrupt_local_mirror_is_a_failure_when_it_is_the_only_copy(
        monkeypatch, tmp_path):
    _pq_env(monkeypatch, tmp_path, store={}, listing=[],
            local={"alpha": b"questions: [unclosed\n"})
    pq, err = hbdj._read_pending_questions()
    assert pq == {} and err is not None and "alpha local:" in err


def test_many_failures_are_capped_in_the_error_text(monkeypatch, tmp_path):
    """A down store fails every agent at once; the text stays one readable line."""
    _pq_env(monkeypatch, tmp_path,
            store={f"agent{i}": b"questions: [unclosed\n" for i in range(7)})
    _, err = hbdj._read_pending_questions()
    assert err is not None and err.count(" store: ") == 5 and err.endswith("(+2 more)")


def test_store_row_beats_stale_local_mirror(monkeypatch, tmp_path):
    """The stale-peer-mirror cause: a mirror that never saw the answer."""
    _pq_env(monkeypatch, tmp_path,
            store={"alpha": _pq_a(("pq-x", "answered"))},
            local={"alpha": _pq_a(("pq-x", "pending"))})
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-x": "answered"}


def test_local_only_row_is_known_not_missing(monkeypatch, tmp_path):
    """The owner's just-filed question may not have reached the store yet."""
    _pq_env(monkeypatch, tmp_path,
            store={"alpha": _pq_a(("pq-old", "pending"))},
            local={"alpha": _pq_a(("pq-old", "pending"), ("pq-new", "pending"))})
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-old": "pending", "pq-new": "pending"}


def test_agent_absent_from_the_local_tree_is_still_enumerated(monkeypatch, tmp_path):
    """A cold box never materialises peers' dirs; the store listing is the roster."""
    _pq_env(monkeypatch, tmp_path, store={"alpha": _pq_a(("pq-a-1", "answered"))})
    assert not (tmp_path / "alpha").exists()
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-a-1": "answered"}


def test_store_failure_is_named_and_local_rows_still_count(monkeypatch, tmp_path):
    """No SILENT local fallback: the degrade is an error, so a not-found id turns
    `pq_unverifiable`; the mirror's rows are still honoured as positives."""
    _pq_env(monkeypatch, tmp_path,
            store={"alpha": ConnectionError("store down")},
            local={"alpha": _pq_a(("pq-real", "answered"))})
    pq, err = hbdj._read_pending_questions()
    assert pq == {"pq-real": "answered"}
    assert err is not None and "alpha" in err and "ConnectionError" in err


def test_store_absent_object_is_not_an_error(monkeypatch, tmp_path):
    """An agent that never filed a question has no file anywhere."""
    _pq_env(monkeypatch, tmp_path,
            store={"alpha": _pq_a(("pq-a-1", "pending"))},
            listing=["alpha", "bravo"])
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-a-1": "pending"}


def test_non_agent_listing_entries_fail_open(monkeypatch, tmp_path):
    """The store listing holds files and fixtures beside the agent dirs; reading
    `<file>/session/...` must not read as a failure."""
    _pq_env(monkeypatch, tmp_path,
            store={"alpha": _pq_a(("pq-a-1", "pending")),
                   "skill-invocations.jsonl": NotADirectoryError("not a dir")},
            listing=["alpha", "skill-invocations.jsonl", "tricks-dryrun"])
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-a-1": "pending"}


def test_listing_failure_is_an_error_not_a_smaller_roster(monkeypatch, tmp_path):
    """guard-2549: a set-valued read needs its own enumeration-completeness signal."""
    _pq_env(monkeypatch, tmp_path,
            local={"alpha": _pq_a(("pq-a-1", "pending"))},
            list_exc=ConnectionError("listing down"))
    pq, err = hbdj._read_pending_questions()
    assert pq == {"pq-a-1": "pending"}
    assert err is not None and "store listing failed" in err


def test_unavailable_backend_is_an_error_not_a_local_only_box(monkeypatch, tmp_path):
    _pq_env(monkeypatch, tmp_path, local={"alpha": _pq_a(("pq-a-1", "pending"))},
            backend_exc=RuntimeError("no backend"))
    pq, err = hbdj._read_pending_questions()
    assert pq == {"pq-a-1": "pending"}
    assert err is not None and "storage backend unavailable" in err


def test_unresolvable_agents_root_is_an_error_not_a_crash(monkeypatch):
    """The lane always exits 0 (a precheck detective must never block the loop)."""
    def _boom():
        raise RuntimeError("no agents root")

    monkeypatch.setattr(hbdj, "agents_root", _boom)
    pq, err = hbdj._read_pending_questions()
    assert pq == {} and err is not None and "agents_root unavailable" in err


def test_no_file_anywhere_is_an_error(monkeypatch, tmp_path):
    """The old guard, kept: an empty map from a fleet with no files is a blind read."""
    _pq_env(monkeypatch, tmp_path, listing=[])
    pq, err = hbdj._read_pending_questions()
    assert pq == {} and err is not None and "no pending-questions.yaml found" in err


@pytest.mark.parametrize("doc", [
    # shape A: {"questions": [...]}
    b"questions:\n- id: pq-1\n  status: answered\n- id: pq-2\n  status: pending\n",
    # shape B: a list of {"questions": [...]} documents
    b"- questions:\n  - id: pq-1\n    status: answered\n"
    b"- questions:\n  - id: pq-2\n    status: pending\n",
    # shape C: a bare list of entries, one non-entry item ignored
    b"- id: pq-1\n  status: answered\n- id: pq-2\n  status: pending\n- just a string\n",
])
def test_every_container_shape_flattens_alike(monkeypatch, tmp_path, doc):
    """Lock-step with pending-questions-sweep.py::_load_questions. Shape B was
    invisible to the old reader: its items carry `questions`, not `id`."""
    _pq_env(monkeypatch, tmp_path, store={"alpha": doc})
    pq, err = hbdj._read_pending_questions()
    assert err is None and pq == {"pq-1": "answered", "pq-2": "pending"}


def test_end_to_end_a_corrupt_peer_file_yields_partial_and_unverifiable(
        monkeypatch, capsys, tmp_path):
    """The whole chain through main() with only the store faked: the id in the
    readable file classifies normally, the id that lives in the unreadable file is
    UNVERIFIABLE (never `pq_missing`), `errors` names the agent, the verdict is
    `partial`."""
    _pq_env(monkeypatch, tmp_path, store={
        "alpha": _pq_a(("pq-readable", "answered")),
        "bravo": b"questions: [unclosed\n"})
    goals = [_goal("g-1", "human_blocked: relay_down per pq-readable."),
             _goal("g-2", "human_blocked: relay_down per pq-in-bravo-file.")]
    _, res = _run(monkeypatch, capsys, goals, real_pq=True)
    assert res["verdict"] == "partial"
    assert len(res["errors"]) == 1 and "bravo" in res["errors"][0]
    assert "pq_answered" in _sig(res, "g-1")
    sigs2 = _sig(res, "g-2")
    assert "pq_unverifiable" in sigs2 and "pq_missing" not in sigs2


def test_end_to_end_a_complete_read_is_unchanged(monkeypatch, capsys, tmp_path):
    """No regression on the healthy path: no errors, `hits`, a real `pq_missing`."""
    _pq_env(monkeypatch, tmp_path, store={"alpha": _pq_a(("pq-readable", "answered"))})
    goals = [_goal("g-1", "human_blocked: relay_down per pq-readable."),
             _goal("g-2", "human_blocked: relay_down per pq-never-filed.")]
    _, res = _run(monkeypatch, capsys, goals, real_pq=True)
    assert res["verdict"] == "hits" and res["errors"] == []
    assert "pq_missing" in _sig(res, "g-2")
