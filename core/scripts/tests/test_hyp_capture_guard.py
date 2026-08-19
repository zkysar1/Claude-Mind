""" — the no-double-resolution guard (outcome 2's test).

Every case here pins a decision that could plausibly have gone the other way, and
several pin a FAIL-SAFE DIRECTION rather than a happy path: an unreadable slot, an
unreadable pipeline, and a malformed entry all have a "safe" answer and an
"authoritative-looking wrong" answer, and the wrong one is silent in each case.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import hyp_capture_guard as g  # noqa: E402

HID = "2026-08-10_retry-backoff"


def _e(hid=HID, goal="g-306-200", **kw):
    d = {"hypothesis_id": hid, "goal_id": goal, "evidence_summary": "x",
         "confirms_or_contradicts": "confirms"}
    d.update(kw)
    return d


# --- the guard proper ------------------------------------------------------

def test_detects_worker_evidence_and_names_its_goals():
    v = g.guard_verdict([_e(goal="g-1"), _e(hid="other", goal="g-2"), _e(goal="g-3")], HID)
    assert v["has_evidence"] is True
    assert v["count"] == 2
    assert v["goal_ids"] == ["g-1", "g-3"], (
        "goal_ids must carry ONLY the matching entries' goals -- the reducer reads "
        "those outcome_notes alongside the evidence")


def test_no_evidence_is_a_clean_negative_not_an_error():
    v = g.guard_verdict([_e(hid="unrelated")], HID)
    assert v["has_evidence"] is False and v["count"] == 0 and v["entries"] == []


def test_match_is_exact_never_prefix_or_substring():
    """Pipeline ids are YYYY-MM-DD_slug, so a prefix match would let a shorter id
    harvest a longer one's evidence and attribute it to the wrong hypothesis --
    silently, and in the direction that makes a resolution look BETTER informed
    than it is."""
    entries = [_e(hid="2026-08-10_retry"), _e(hid="2026-08-10_retry-backoff-v2")]
    assert g.guard_verdict(entries, HID)["count"] == 0


# --- fail-safe directions --------------------------------------------------

def test_unreadable_slot_reports_no_evidence_rather_than_claiming_some():
    """None / a dict / a string are all 'I could not read the slot'. The safe answer
    is 'no evidence' (reducer proceeds normally). Claiming evidence exists would
    make the reducer wait for or hunt something that is not there."""
    for raw in (None, {}, "not-a-list", 42, [None, "x", 7]):
        v = g.guard_verdict(raw, HID)
        assert v["has_evidence"] is False, f"{raw!r} must not report evidence"
        assert v["count"] == 0


def test_empty_hypothesis_id_matches_nothing():
    """Guards the join from the other side: an empty query id must not collect every
    entry whose hypothesis_id is also empty/absent."""
    assert g.guard_verdict([_e(), {"goal_id": "g-x"}], "")["count"] == 0


def test_entry_without_goal_id_still_counts_as_evidence():
    """goal_id is required by the writing phase, but a missing one must not erase
    the EVIDENCE -- the point of the guard is that the reducer knows evidence
    exists, and attribution is secondary."""
    v = g.guard_verdict([_e(goal=None)], HID)
    assert v["has_evidence"] is True and v["count"] == 1 and v["goal_ids"] == []


# --- linkage check ---------------------------------------------------------

def test_unresolvable_ids_flags_only_ids_absent_from_the_pipeline():
    ids = g.unresolvable_ids([_e(hid="a"), _e(hid="b"), _e(hid="a")], ["a", "c"])
    assert ids == ["b"], "must report each unmatched id ONCE, in slot order"


def test_empty_known_id_set_flags_nothing():
    """rb-245 class, and the single most important case in this file: a zero-length
    corpus is the signature of a FAILED pipeline read, not of a pipeline with no
    hypotheses. Flagging everything then produces an authoritative-looking report
    that every entry is bogus -- one unreadable file becoming a fleet-wide false
    alarm."""
    assert g.unresolvable_ids([_e(hid="a"), _e(hid="b")], []) == []
    assert g.unresolvable_ids([_e(hid="a")], None) == []


# --- CLI contract ----------------------------------------------------------

def test_cli_emits_json_and_always_exits_zero(capsys):
    """Exit 0 even WITH evidence is deliberate: a non-zero exit invites a caller to
    read 'evidence exists' as 'do not resolve', which converts an under-informed
    resolution into a hypothesis nobody may ever close. Evidence can be weighed; a
    stuck hypothesis cannot."""
    rc = g.main(["--hypothesis-id", HID,
                 "--slot-json", json.dumps([_e()]),
                 "--known-ids-json", json.dumps(["some-other-id"])])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["has_evidence"] is True
    assert out["unresolvable_ids"] == [HID]


def test_cli_survives_malformed_slot_json():
    """A caller pasting shell-mangled output must not crash the reducer's
    pre-resolution check."""
    assert g.main(["--hypothesis-id", HID, "--slot-json", "{not json"]) == 0


# --- empty vs unreadable ---------------------------------------------------
# Every test above this line passes against the version of this guard that was
# INERT at its only call site. That is the point of the block below: the defect
# was never in the function, so no test of the function could find it.

def test_slot_read_separates_no_evidence_from_could_not_look():
    """guard-2352. `has_evidence: false` answers two different questions and the
    caller cannot tell which it got. That ambiguity is exactly what hid the
    two-Bash-call defect: an empty string produced a confident clean answer."""
    assert g.parse_slot(json.dumps([_e()]))[1] == "ok"
    assert g.parse_slot("[]")[1] == "empty"
    assert g.parse_slot("null")[1] == "empty", (
        "wm-read.sh renders an absent slot as literal `null` -- a SUCCESSFUL "
        "read of a slot holding nothing, not a failed read")
    for bad in ("", "   ", None, "{not json", '{"a": 1}'):
        assert g.parse_slot(bad)[1] == "unreadable", f"{bad!r} must not read as empty"


def test_cli_surfaces_slot_read_on_every_verdict():
    """The distinction is worthless if it stops at the function boundary -- the
    reducer reads the CLI's JSON, not parse_slot's return value."""
    g.main(["--hypothesis-id", HID, "--slot-json", ""])
    import io, contextlib  # noqa: E401
    for payload, expected in (("", "unreadable"), ("[]", "empty"),
                              (json.dumps([_e()]), "ok")):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = g.main(["--hypothesis-id", HID, "--slot-json", payload])
        assert rc == 0
        assert json.loads(buf.getvalue())["slot_read"] == expected


# --- THE CALL SITE ---------------------------------------------------------
# The regression this file previously could not catch. On 2026-08-11 the guard
# was measured inert in production shape: the SKILL.md read the slot in one
# `Bash:` directive and consumed `"$slot"` in the next, shell state does not
# survive between Bash tool calls (guard-128/guard-492), and the guard reported
# has_evidence:false against a live populated slot. Same slot, same hypothesis:
# two-call shape -> false; one-call shape -> true.

PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SKILL = PROJECT_ROOT / ".claude" / "skills" / "review-hypotheses" / "SKILL.md"
WRAPPER = CORE_SCRIPTS / "hyp-capture-guard.sh"


def _bash_directives(text) -> list:
    """The EXECUTABLE `Bash:` lines only -- never the surrounding prose.

    guard-1099. An unanchored whole-file grep counts a file's own cautionary text
    as live code, and this SKILL.md deliberately QUOTES the broken two-call shape
    in the warning that exists to prevent it. The first version of the test below
    was whole-file and failed on that warning -- the check firing on the fix's own
    documentation. (`/verify-learning`'s glob-routing check was re-anchored for
    exactly this reason, and .claude/rules/gradle-tests-pattern.md documents the
    same collision from the gate side.)

    Anchoring here fails SAFE in the other direction too: it is what lets the
    routing test below assert the call site EXISTS, which a substring-in-file
    check cannot do -- prose naming the wrapper would satisfy that trivially.
    """
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("Bash:")]


def test_call_site_never_carries_a_shell_variable_between_bash_calls():
    """The literal defect signature. A shell variable consumed by a later
    `Bash:` directive is always empty; here that silently disabled the guard."""
    offenders = [ln for ln in _bash_directives(SKILL.read_text(encoding="utf-8"))
                 if '--slot-json "$' in ln or "slot=$(" in ln]
    assert not offenders, (
        "a Bash: directive consumes a shell variable that does not survive the "
        f"tool-call boundary -- the guard would read an empty string: {offenders}")


def test_call_site_routes_through_the_wrapper_and_still_exists():
    """guard-350, and the structural fix: the wrapper reads the slot in the same
    shell, so there is no cross-call value left to lose.

    Asserts the call site EXISTS before asserting its shape -- a guard deleted
    from the skill and a guard invoked correctly are both 'no bad call site',
    and this file's whole reason for existing is that an absent check and a
    passing check had looked identical."""
    calls = [ln for ln in _bash_directives(SKILL.read_text(encoding="utf-8"))
             if "hyp-capture-guard.sh" in ln or "hyp_capture_guard.py" in ln]
    assert calls, "no Bash: directive in review-hypotheses/SKILL.md invokes the guard at all"
    bad = [ln for ln in calls if "hyp_capture_guard.py" in ln]
    assert not bad, (
        "SKILL.md must invoke the wrapper; a direct .py call site is what forced "
        f"the caller to assemble --slot-json by hand: {bad}")


def test_wrapper_reads_the_slot_itself():
    assert WRAPPER.exists(), "the call site names a wrapper that does not exist"
    assert "wm-read.sh" in WRAPPER.read_text(encoding="utf-8")


def test_wrapper_does_not_launder_a_failed_read_into_an_empty_one():
    """guard-332. `2>/dev/null || echo '{}'` would restore the exact ambiguity
    this fix removes -- and it is the idiom the nearest sibling wrapper
    (belief-contradiction-check.sh:47) actually uses, so the pull toward it is
    real rather than hypothetical."""
    body = WRAPPER.read_text(encoding="utf-8")
    offenders = [ln.strip() for ln in body.splitlines()
                 if "wm-read.sh" in ln and not ln.strip().startswith("#")
                 and ("2>/dev/null" in ln or "|| echo" in ln)]
    assert not offenders, f"failed read laundered into an empty slot: {offenders}"
