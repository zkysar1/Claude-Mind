"""Tests for adjudication-lane.py entry-verdict polarity ().

`challenge_survived` is about the CHALLENGE; the calibration ledger's `verdict`
is about the ENTRY, and confidence-calibration-ledger.md defines it as "what
later happened to ITS claim" for "the entry judged". The two are INVERSES, and
the lane passed the raw boolean straight through -- writing entry-survived on
every row where the challenge was upheld, i.e. exactly the rows where the entry
was wrong.

The decisive real case: guard-5973 was RETIRED and superseded by guard-6019
after its challenge was upheld. A retired entry did not survive under EITHER
reading of the ambiguity, so `challenge_survived=True -> "survived"` is
falsifiable without resolving it. That case is pinned below by name.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "adjudication_lane",
    Path(__file__).resolve().parents[1] / "adjudication-lane.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

# The closed set the ledger accepts. Imported rather than restated so this test
# fails loudly if the vocabularies ever diverge.
_LEDGER_SPEC = importlib.util.spec_from_file_location(
    "_confidence_ledger_for_test",
    Path(__file__).resolve().parents[1] / "_confidence_ledger.py",
)
_ledger = importlib.util.module_from_spec(_LEDGER_SPEC)
_LEDGER_SPEC.loader.exec_module(_ledger)


def test_challenge_upheld_never_scores_the_entry_as_survived():
    """The regression itself. Every upheld challenge means the entry was wrong."""
    for change in ("amended", "retired", "superseded", "none", None, "bogus"):
        assert mod.entry_verdict(True, change) != "survived", change


def test_guard_5973_retired_entry_is_refuted_not_survived():
    """The decisive real row: challenge upheld, entry retired + superseded.

    Pre-fix this produced "survived" and was one of 4 rows in a ledger whose
    verdicts were 100% survived -- a store from which a calibration curve could
    only ever report perfect calibration.
    """
    assert mod.entry_verdict(True, "retired") == "refuted"
    assert mod.entry_verdict(True, "superseded") == "refuted"


def test_amendment_is_revised_not_refuted():
    """guard-5965: amended_fields stamped in response to the stance."""
    assert mod.entry_verdict(True, "amended") == "revised"


def test_entry_upheld_when_challenge_did_not_survive():
    """The one branch where the entry genuinely survived. POSITIVE CONTROL:
    without this, a function hardcoded to never return "survived" would pass
    every other assertion in this file (guard-2421)."""
    assert mod.entry_verdict(False, None) == "survived"
    assert mod.entry_verdict(False, "amended") == "survived"


def test_acknowledged_but_not_applied_is_unknown_not_a_resolution():
    """The third state. An ack is NOT a resolution: msg-20260904-204611-zeta-5272
    records one that landed while amended_fields stayed {} for twelve hours."""
    assert mod.entry_verdict(True, None) == "unknown"
    assert mod.entry_verdict(True, "none") == "unknown"


def test_unrecognised_artifact_change_degrades_to_unknown_not_a_guess():
    assert mod.entry_verdict(True, "sort-of-fixed") == "unknown"


def test_every_verdict_emitted_is_in_the_ledgers_closed_set():
    """Vocabulary parity: a verdict outside VERDICTS is silently rewritten to
    "unknown" by the ledger writer, so a drift here would be invisible there."""
    emitted = {
        mod.entry_verdict(s, c)
        for s in (True, False)
        for c in ("amended", "retired", "superseded", "none", None, "bogus")
    }
    assert emitted <= set(_ledger.VERDICTS), emitted - set(_ledger.VERDICTS)
    # And it must actually exercise more than one verdict, or the assertion above
    # is satisfied vacuously by a constant function.
    assert len(emitted) >= 3, emitted


# ---------------------------------------------------------------------------
# AUTOMATED RESOLUTION SWEEP (). Every case below is a LIVE row from
# world/telemetry/adjudication-lane-ledger.jsonl as measured 2026-09-06, not a
# synthetic fixture — the sweep's whole claim is that it reproduces the manual
# pass, so the regression pins must be the rows the manual pass ruled on.
# ---------------------------------------------------------------------------


def test_challenge_time_prefers_board_msg_over_ledger_at():
    """The board id is stamped at POST time by a different writer than the row."""
    at, src = mod.challenge_time(
        {"board_msg": "msg-20260905-111443-alpha-5040", "at": "2026-09-05T11:19:05"}
    )
    assert at == "2026-09-05T11:14:43"
    assert src == "board_msg"


def test_challenge_time_falls_back_to_at_when_unparseable():
    at, src = mod.challenge_time({"board_msg": "not-a-msg-id", "at": "2026-09-05T11:19:05"})
    assert at == "2026-09-05T11:19:05"
    assert src.startswith("ledger_at")
    at, src = mod.challenge_time({"at": "2026-09-05T11:19:05"})
    assert at == "2026-09-05T11:19:05"


def test_sweep_scores_amendment_that_postdates_and_cites():
    """rb-10262: amended 00:40:03 after a 00:08:30 challenge, and cites it."""
    entry = {
        "id": "rb-10262",
        "status": "active",
        "amended_fields": {"content": "2026-09-06T00:40:03"},
        "content": "AMENDED 2026-09-06 (zeta, on alpha's adjudication msg-20260906-000830-alpha-5326)",
    }
    change, reason = mod.sweep_decision(entry, "2026-09-06T00:08:30", "msg-20260906-000830-alpha-5326")
    assert change == "amended"
    assert mod.entry_verdict(True, change) == "revised"


def test_sweep_refuses_amendment_predating_its_challenge():
    """rb-10222: amended 10:25:46, challenged 10:30:32. Scoring it flatters the lane."""
    entry = {
        "id": "rb-10222",
        "status": "active",
        "amended_fields": {"content": "2026-09-05T10:25:46"},
        "content": "msg-20260905-103032-alpha-5042",  # cites, and STILL must be refused
    }
    change, reason = mod.sweep_decision(entry, "2026-09-05T10:30:32", "msg-20260905-103032-alpha-5042")
    assert change is None
    assert reason.startswith("predates-challenge")


def test_sweep_does_not_score_the_163s_gap_as_predating():
    """guard-6043 regression: keyed on ledger `at` this read as predating and the
    hand-scored `revised` was contradicted. Keyed on the board post it is genuine."""
    entry = {
        "id": "guard-6043",
        "status": "active",
        "amended_fields": {"rule": "2026-09-05T11:16:22"},
        "rule": "per adjudication msg-20260905-111443-alpha-5040",
    }
    review = {"board_msg": "msg-20260905-111443-alpha-5040", "at": "2026-09-05T11:19:05"}
    chal_at, _ = mod.challenge_time(review)
    change, _ = mod.sweep_decision(entry, chal_at, review["board_msg"])
    assert change == "amended"


def test_sweep_requires_citation_for_causation():
    """rb-10106: amended after its challenge but cites nothing. Ordering != causation."""
    entry = {
        "id": "rb-10106",
        "status": "active",
        "amended_fields": {"content": "2026-09-04T02:00:00"},
        "content": "no reference to the adjudication at all",
    }
    change, reason = mod.sweep_decision(entry, "2026-09-04T01:14:20", "msg-20260904-011420-alpha-5417")
    assert change is None
    assert reason.startswith("no-citation")


def test_sweep_never_auto_scores_no_amendment_as_unknown():
    """`unknown` is acknowledged-but-not-applied; asserting it needs ack evidence."""
    change, reason = mod.sweep_decision(
        {"id": "guard-6089", "status": "active", "amended_fields": {}},
        "2026-09-06T00:15:10", "msg-20260906-001510-alpha-5333",
    )
    assert change is None
    assert reason.startswith("no-amendment")
    assert "unknown" in reason


def test_sweep_never_keys_on_amended_at():
    """Measured: amended_at is ABSENT from reasoning_bank rows entirely, so an
    amended_at key reproduces the undercount the sweep exists to remove."""
    entry = {"id": "rb-x", "status": "active", "amended_fields": {}, "amended_at": "2026-09-06T01:00:00"}
    change, _ = mod.sweep_decision(entry, "2026-09-06T00:00:00", "msg-20260906-000000-a-1")
    assert change is None, "amended_at must never license a score"


def test_sweep_terminal_status_is_refuted_without_ordering():
    for status in ("retired", "superseded"):
        change, _ = mod.sweep_decision({"id": "g", "status": status, "amended_fields": {}}, None, None)
        assert change == status
        assert mod.entry_verdict(True, change) == "refuted"


def test_sweep_decision_is_total_on_junk_amended_fields():
    change, reason = mod.sweep_decision(
        {"id": "g", "status": "active", "amended_fields": {"rule": 7}}, "2026-09-06T00:00:00", None
    )
    assert change is None
    assert reason.startswith("unorderable")


def test_citation_search_is_body_scoped_not_whole_record():
    """A msg id sitting in METADATA is not a citation. Whole-record matching
    biased toward SCORING, which manufactures the number the ledger measures."""
    entry = {
        "id": "rb-x", "status": "active",
        "amended_fields": {"content": "2026-09-06T02:00:00"},
        "content": "an amendment that references nothing",
        "source": "msg-20260906-000000-alpha-1",      # metadata, not prose
        "tags": ["msg-20260906-000000-alpha-1"],
    }
    change, reason = mod.sweep_decision(entry, "2026-09-06T00:00:00", "msg-20260906-000000-alpha-1")
    assert change is None
    assert reason.startswith("no-citation")


def test_citation_search_finds_the_id_in_prose():
    entry = {
        "id": "rb-x", "status": "active",
        "amended_fields": {"content": "2026-09-06T02:00:00"},
        "content": "AMENDED per adjudication msg-20260906-000000-alpha-1",
    }
    change, _ = mod.sweep_decision(entry, "2026-09-06T00:00:00", "msg-20260906-000000-alpha-1")
    assert change == "amended"


def test_body_text_falls_back_to_all_strings_when_no_named_field():
    """A store with an unknown body key degrades to the old behaviour rather
    than silently never matching."""
    assert "hello" in mod._entry_body_text({"some_new_body_key": "hello"})
    assert mod._entry_body_text({"n": 1}) == ""
