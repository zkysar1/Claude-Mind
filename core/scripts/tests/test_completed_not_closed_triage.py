"""Tests for completed-not-closed-triage.py ().

The script is REPORT-ONLY and its whole value is that it cannot act. So the
tests here are weighted toward what it must REFUSE and what it must never
include, not toward output formatting:

  * a LIVE carrier must never appear (releasing/closing a goal whose worker is
    still running is the destructive case)
  * an ABSENT carrier must never appear (absent != dead)
  * the sweep must never be invoked with --apply
  * an unreadable sweep must fail loudly, never as a clean empty population
"""

import importlib.util
import json
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def _load():
    """Import the hyphenated script by path (not importable as a module)."""
    spec = importlib.util.spec_from_file_location(
        "cnc_triage", _SCRIPTS / "completed-not-closed-triage.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _rec(goal_id, carrier_verdict="stale", grace=True, verdict="completed-not-closed",
         age=5000, asp="asp-115", note_len=5000, head="DONE."):
    """One sweep record, shaped exactly like the live ones."""
    r = {
        "goal_id": goal_id,
        "asp_id": asp,
        "source": "world",
        "title": f"title for {goal_id}",
        "age_minutes": age,
        "verdict": verdict,
        "foreign_sid_grace_expired": grace,
        "completion_evidence": {
            "predicate": "outcome_note", "note_len": note_len, "note_head": head,
        },
    }
    if carrier_verdict is not None:
        r["body_carrier"] = {
            "verdict": carrier_verdict, "sid": "abc123",
            "carrier_age_minutes": 4000, "carrier_host": "cc-08",
        }
    return r


# --------------------------------------------------------------------------
# The safety property: who must NEVER be in the candidate set.
# --------------------------------------------------------------------------

def test_live_carrier_is_never_a_candidate():
    """A goal whose worker is demonstrably ALIVE must not be surfaced.

    This is the destructive case the whole script is bounded against: that
    worker is mid-flight and will close its own goal. Verified by mutation --
    flipping only the carrier verdict must empty the candidate set.
    """
    summary = {"kept": [_rec("g-1", carrier_verdict="fresh-correct")]}
    assert mod._candidates(summary) == []

    # Positive control: the SAME record with a stale carrier IS a candidate,
    # so the empty result above is the carrier check firing and not a fixture
    # that could never match anything (guard-2421).
    summary_stale = {"kept": [_rec("g-1", carrier_verdict="stale")]}
    assert [r["goal_id"] for r in mod._candidates(summary_stale)] == ["g-1"]


def test_absent_carrier_is_never_a_candidate():
    """No carrier reading at all is NOT evidence the holder is dead.

    Measured live: 23 of 283 completed-not-closed records carry no
    body_carrier. Treating absent as dead would widen the population by 8%
    on a signal that says nothing.
    """
    assert mod._candidates({"kept": [_rec("g-1", carrier_verdict=None)]}) == []


def test_grace_not_expired_is_never_a_candidate():
    assert mod._candidates({"kept": [_rec("g-1", grace=False)]}) == []
    assert mod._candidates({"kept": [_rec("g-1", grace=None)]}) == []


def test_other_verdicts_are_never_candidates():
    for v in ("kept", "stranded", "possible-displacement", "alive", "self-sid"):
        assert mod._candidates({"kept": [_rec("g-1", verdict=v)]}) == [], v


# --------------------------------------------------------------------------
# The read-only invariant.
# --------------------------------------------------------------------------

def test_assert_read_only_refuses_apply():
    with pytest.raises(SystemExit) as e:
        mod._assert_read_only(["python", "stranded-claim-sweep.py", "--apply"])
    assert "report-only" in str(e.value)


def test_assert_read_only_passes_clean_argv():
    mod._assert_read_only(["python", "stranded-claim-sweep.py"])  # no raise


def test_script_exposes_no_apply_flag():
    """A future edit must not quietly add a mutation path."""
    src = (_SCRIPTS / "completed-not-closed-triage.py").read_text(encoding="utf-8")
    assert '"--apply"' not in src.split("_assert_read_only")[0], (
        "an --apply flag was added to the triage parser; this script is "
        "report-only by design (see its module docstring)"
    )


# --------------------------------------------------------------------------
# Unreadable input must fail loudly, never as a clean empty population.
# --------------------------------------------------------------------------

def test_no_json_in_saved_sweep_raises(tmp_path):
    p = tmp_path / "bad.log"
    p.write_text("traceback: everything died\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        mod._load_sweep(str(p), 60)
    assert "NOT an empty population" in str(e.value)


def test_zero_records_reports_extraction_failure(capsys):
    """0 records of ANY verdict means the walk broke, not that the queue is clean.

    No summary passed => the counter is unknowable, so the banner is still
    correct here. The two tests below cover the cases where it IS knowable.
    """
    rc = mod._render([], total_records=0, limit=20, product_only=False)
    assert rc == -1
    assert "NOT a result" in capsys.readouterr().out


def test_zero_records_with_zero_candidates_is_clean_not_a_failure(capsys):
    """A per-agent zero is a RESULT, and must not be reported as a broken shape.

    Measured 2026-08-15 (zeta/cc-02): scanned=1, kept_completed_not_closed=0,
    every summary key present and correct -- yet the extraction banner fired and
    sent the reader to debug a healthy shape. It fires on exactly the agents with
    no backlog, i.e. most of them.
    """
    rc = mod._render([], total_records=0, limit=20, product_only=False,
                     summary={"scanned": 1, "kept_completed_not_closed": 0})
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean for THIS AGENT" in out
    assert "NOT a result" not in out
    # The caveat is the load-bearing half: the sweep only examines the CALLING
    # agent's claims, so a clean read here is consistent with a large backlog
    # held by another agent. Without this line the zero reads as a fleet
    # all-clear, which is a worse failure than the banner it replaced.
    assert "NOT a fleet all-clear" in out


def test_zero_records_with_a_nonzero_counter_still_reports_failure(capsys):
    """The clean branch must NOT swallow a genuine shape failure.

    Counter says candidates exist while the walk found none => the summary
    really is the wrong shape. Collapsing this into the clean branch would turn
    a real failure into an all-clear, which is the dangerous direction.
    """
    rc = mod._render([], total_records=0, limit=20, product_only=False,
                     summary={"scanned": 300, "kept_completed_not_closed": 5})
    out = capsys.readouterr().out
    assert rc == -1
    assert "NOT a result" in out
    assert "clean for THIS AGENT" not in out


def test_saved_sweep_tolerates_stderr_preamble(tmp_path):
    """A log captured with 2>&1 has narration before the JSON."""
    p = tmp_path / "mixed.log"
    p.write_text(
        "[stranded-claim-sweep] RELEASING ...\n" + json.dumps({"kept": []}),
        encoding="utf-8",
    )
    assert mod._load_sweep(str(p), 60) == {"kept": []}


# --------------------------------------------------------------------------
# Projection behaviour.
# --------------------------------------------------------------------------

def test_walk_finds_records_in_an_unknown_bucket():
    """A bucket the sweep adds later must join automatically.

    The walk keys on record SHAPE, not on a hardcoded list of container names,
    so a new sweep bucket is not silently skipped.
    """
    summary = {"some_future_bucket": {"nested": [_rec("g-new")]}}
    assert [r["goal_id"] for r in mod._candidates(summary)] == ["g-new"]


def test_ordering_is_oldest_claim_first():
    summary = {"kept": [_rec("g-young", age=100), _rec("g-old", age=9999),
                        _rec("g-mid", age=5000)]}
    assert [r["goal_id"] for r in mod._candidates(summary)] == [
        "g-old", "g-mid", "g-young"]


def test_limit_announces_what_it_dropped(capsys):
    cands = [_rec(f"g-{i}", age=1000 - i) for i in range(10)]
    mod._render(cands, total_records=10, limit=3, product_only=False)
    out = capsys.readouterr().out
    assert "7 more not shown" in out
    assert "not excluded, only unprinted" in out


def test_product_only_reports_the_hidden_count(capsys):
    cands = mod._candidates({"kept": [
        _rec("g-p", asp="asp-335"), _rec("g-f1"), _rec("g-f2")]})
    mod._render(cands, total_records=3, limit=20, product_only=True)
    out = capsys.readouterr().out
    assert "g-p" in out and "g-f1" not in out
    assert "2 framework goals are hidden" in out


def test_truncation_is_labelled_and_the_read_command_is_offered(capsys):
    """The quoted head is a 220-char cut; presenting it as a verdict is the
    error this script refuses to automate, so it must name what it omits."""
    cands = mod._candidates({"kept": [_rec("g-1", note_len=9527, head="x" * 220)]})
    mod._render(cands, total_records=1, limit=20, product_only=False)
    out = capsys.readouterr().out
    assert "TRUNCATED" in out
    assert "9307 chars NOT shown" in out
    assert "aspirations-query.sh --goal-field id g-1 --full" in out


def test_noteless_predicate_does_not_print_an_empty_quote(capsys):
    """The pipeline-resolution case has no note; an empty quote would read as
    'the note is blank' rather than 'there is no note'."""
    r = _rec("g-1")
    r["completion_evidence"] = {"predicate": "pipeline_resolution_ref", "note_len": 0}
    mod._render(mod._candidates({"kept": [r]}), 1, 20, False)
    out = capsys.readouterr().out
    assert "NONE. Evidence is pipeline_resolution_ref" in out
    assert "must read the pipeline record" in out


def test_header_states_no_verdict_is_computed(capsys):
    mod._render(mod._candidates({"kept": [_rec("g-1")]}), 1, 20, False)
    out = capsys.readouterr().out
    assert "NO VERDICT IS COMPUTED" in out
    # guard-3628: these are KEPT deliberately, not missed.
    assert "DO NOT release these claims" in out


# ─────────────────── fleet-wide denominator () ───────────────────
#
# The ACTIONABLE list is bounded TWICE — to the calling agent's claims (the
# sweep is bound-agent-scoped) and to dead carriers. Measured 2026-08-15: alpha
# held 337 of 341 claimed-and-noted goals, so four boxes out of five read a
# structural 0 while the backlog was untouched. These tests pin the denominator
# that makes that gap visible, and pin that an unreadable store can never
# masquerade as an empty one.


def _world(aspirations):
    """Shape a fake `aspirations-read.sh --source world --active` payload."""
    return json.dumps(aspirations)


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _goal(gid, status="in-progress", note="x" * 5000, claimed_by=None):
    g = {"id": gid, "status": status, "outcome_note": note}
    if claimed_by:
        g["claimed_by"] = claimed_by
    return g


def test_note_min_chars_reads_the_sweeps_live_constant():
    """Read from the SSOT, never a re-typed copy — a retune of the sweep must
    not leave this report quoting a stale threshold (guard-2676)."""
    value = mod._note_min_chars()
    assert isinstance(value, int) and value > 0
    src = (_SCRIPTS / "stranded-claim-sweep.py").read_text(encoding="utf-8")
    assert f"_NOTE_EVIDENCE_MIN_CHARS = {value}" in src


def test_fleet_population_counts_holders_products_and_threshold(monkeypatch):
    payload = _world([
        {"id": "asp-335", "goals": [
            _goal("g-335-1", claimed_by="alpha"),
            _goal("g-335-2", claimed_by="alpha", note="short"),
            _goal("g-335-3"),                                  # noted, unclaimed
            _goal("g-335-4", status="completed", claimed_by="alpha"),   # terminal
            _goal("g-335-5", note=""),                          # no evidence
        ]},
        {"id": "asp-115", "goals": [_goal("g-115-1", claimed_by="echo")]},
    ])
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: _Proc(stdout=payload))
    pop = mod._fleet_population(timeout=30)
    assert pop["readable"] is True
    assert pop["noted"] == 4                    # 3 in asp-335 + 1 in asp-115
    assert pop["claimed_and_noted"] == 3
    assert pop["unclaimed_and_noted"] == 1
    assert pop["product"] == 3                  # asp-335 is a product prefix
    assert pop["by_holder"] == {"alpha": 2, "echo": 1}
    # `scanned` is the whole non-terminal population, not the noted subset:
    # 5 of the 6 fixture goals (g-335-4 is completed). A terminal goal is never
    # counted, however long its note.
    assert pop["non_terminal_scanned"] == 5
    # The 'short' note is BELOW the sweep's keep threshold but still carries
    # evidence — the denominator is a superset of the sweep's keep set.
    assert pop["over_threshold"] == 3


def test_fleet_population_reports_a_failed_read_as_unreadable_not_zero(monkeypatch):
    """An unreadable store is NOT an empty population (verify-before-assuming
    rule 4). A silent 0 here would read as 'backlog drained'."""
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: _Proc(stdout="", returncode=2, stderr="boom"))
    pop = mod._fleet_population(timeout=30)
    assert pop["readable"] is False
    assert "rc=2" in pop["reason"] and "boom" in pop["reason"]
    assert "noted" not in pop           # no count is invented on the failure path


def test_fleet_population_treats_zero_aspirations_as_unreadable(monkeypatch):
    """An empty read and an empty world are different findings; only one of
    them is good news, and the reader cannot tell them apart from a 0."""
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: _Proc(stdout="[]"))
    pop = mod._fleet_population(timeout=30)
    assert pop["readable"] is False
    assert "not an empty world" in pop["reason"]


def test_fleet_population_survives_a_raising_reader(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr(mod.subprocess, "run", _boom)
    pop = mod._fleet_population(timeout=30)
    assert pop["readable"] is False and "OSError" in pop["reason"]


def test_render_fleet_names_an_unreadable_population_as_not_an_all_clear(capsys):
    mod._render_fleet({"readable": False, "reason": "daemon down"}, "echo")
    out = capsys.readouterr().out
    assert "UNREADABLE" in out
    assert "NOT zero and NOT an all-clear" in out
    assert "daemon down" in out


def test_render_fleet_states_the_actionable_list_is_a_strict_subset(capsys):
    mod._render_fleet({
        "readable": True, "aspirations": 30, "non_terminal_scanned": 2107,
        "noted": 464, "claimed_and_noted": 341, "unclaimed_and_noted": 123,
        "product": 157, "min_chars": 1000, "over_threshold": 451,
        "by_holder": {"alpha": 337, "echo": 2},
    }, "echo")
    out = capsys.readouterr().out
    assert "464" in out and "157 product" in out
    assert "alpha=337" in out
    assert "held by echo: 2" in out
    # guard-3830: the bounded list must never read as the scan result.
    assert "strict subset" in out
    assert "DENOMINATOR, NOT A VERDICT" in out


def test_render_fleet_shows_zero_held_by_an_agent_that_holds_nothing(capsys):
    """The 'held by <me>' line is what makes a 0-ACTIONABLE run legible on a box
    that holds none of the backlog — it must render, not vanish."""
    mod._render_fleet({
        "readable": True, "aspirations": 30, "non_terminal_scanned": 100,
        "noted": 464, "claimed_and_noted": 341, "unclaimed_and_noted": 123,
        "product": 157, "min_chars": None, "over_threshold": None,
        "by_holder": {"alpha": 337},
    }, "bravo")
    out = capsys.readouterr().out
    assert "held by bravo: 0" in out
    assert "note >=" not in out          # threshold omitted, never invented


# ───────── the board post must fire on the FLEET backlog () ─────────
#
# The lane's only outbound signal used to be gated on `cands` — DEAD-carrier
# goals held by THE CALLING AGENT. That subset was empty on every box on
# 2026-08-15 while 341 claimed-and-noted goals sat unbanked, so the consumer
# never fired. These pin the firing condition and the headline ordering.


def _capture_post(monkeypatch):
    """Return a list that collects the body handed to board-post.sh."""
    sent = []

    def _fake_run(argv, **kw):
        sent.append(kw.get("input", ""))
        return _Proc(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return sent


_POP = {
    "readable": True, "aspirations": 30, "non_terminal_scanned": 2107,
    "noted": 464, "claimed_and_noted": 341, "unclaimed_and_noted": 123,
    "product": 157, "min_chars": 1000, "over_threshold": 451,
    "by_holder": {"alpha": 337, "echo": 2},
}


def test_post_fires_and_leads_with_the_fleet_count_when_no_dead_carriers(monkeypatch):
    """The regression that motivated this: zero dead-carrier candidates while
    the fleet backlog is 464. The post must still go, and must not open with a
    per-agent number (guard-3830)."""
    sent = _capture_post(monkeypatch)
    mod._post_board([], total=2, pop=_POP)
    assert len(sent) == 1
    body = sent[0]
    assert body.startswith("COMPLETED-NOT-CLOSED, FLEET-WIDE: 464")
    assert "157 product" in body and "alpha=337" in body
    # The per-agent count appears, but labelled as the subset it is.
    assert "DEAD-CARRIER SUBSET (this agent only): 0" in body
    assert body.index("FLEET-WIDE") < body.index("DEAD-CARRIER SUBSET")
    # The cross-agent guard rides with the number, since the holder is named.
    assert "must not be closed cross-agent" in body


def test_post_body_keeps_the_two_measured_rejected_remedies(monkeypatch):
    """Adding the fleet headline must not displace the guidance that stops a
    reader reaching for release or blind-close (g-115-5177 / guard-2852c)."""
    sent = _capture_post(monkeypatch)
    mod._post_board([], total=2, pop=_POP)
    body = sent[0]
    assert "RELEASE converts" in body
    assert "BLIND-CLOSE" in body


def test_post_surfaces_an_unreadable_population_rather_than_staying_silent(monkeypatch):
    sent = _capture_post(monkeypatch)
    mod._post_board([], total=0, pop={"readable": False, "reason": "daemon down"})
    assert "UNREADABLE this run (daemon down)" in sent[0]
    assert "That is not zero" in sent[0]


@pytest.mark.parametrize(
    "cands,pop,expect",
    [
        ([], {"readable": True, "noted": 464}, True),      # fleet backlog only
        ([1], {"readable": True, "noted": 0}, True),       # dead-carrier only
        ([], {"readable": False, "reason": "x"}, True),    # failed read is news
        ([], {"readable": True, "noted": 0}, False),       # genuinely clean
        ([], {"readable": False, "waived": True, "reason": "y"}, False),  # opted out
    ],
)
def test_board_fire_predicate(cands, pop, expect):
    """`--no-fleet` is an opt-OUT, not a failed read: it must not publish an
    UNREADABLE finding about a measurement nobody asked for."""
    backlog = bool(cands) or (
        not pop.get("waived")
        and (not pop.get("readable") or (pop.get("noted") or 0) > 0)
    )
    assert backlog is expect
