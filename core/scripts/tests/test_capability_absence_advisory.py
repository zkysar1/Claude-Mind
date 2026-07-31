"""Tests for the capability-absence write-time advisory ().

Covers the detector, the advisory composer, and the aspirations.py call-site
field predicate. Every branch of `advise()` is exercised per guard-502
(block / pass / noop / early-return all need coverage).

The false-positive test is not decoration: capability-gate's own
`trigger_matched` values in meta/gate-firings.jsonl are single generic tokens
(`against` 233, `clean` 268, `verify` 236, `commit` 215), which is the
over-matching this module's phrase-anchored patterns exist to avoid. If a
future edit loosens a pattern into a bare token, test_benign_prose_does_not_fire
is what goes red.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

cap = importlib.import_module("_capability_absence_patterns")


# --------------------------------------------------------------------------
# detect()
# --------------------------------------------------------------------------

def test_detects_routing_away_phrasings():
    for text in [
        "There is no agent-provisionable path for this.",
        "Only a human can approve the billing change.",
        "This requires a real user session.",
        "human-only action, cannot be automated by the loop",
        "needs human approval before it can proceed",
    ]:
        assert cap.detect(text), "expected a match for: %s" % text


def test_detects_credential_absence():
    for text in [
        "It needs credentials I do not hold.",
        "I don't have the permission to read that policy.",
        "no access to the bucket lifecycle configuration",
    ]:
        assert cap.detect(text), "expected a match for: %s" % text


def test_detects_build_side_twin():
    # verify-before-assuming.md names "needs to be built" as the same class as
    # "doesn't exist" — the symmetric failure is building a duplicate.
    for text in [
        "A helper needs to be built for this.",
        "There is no support for that shape yet.",
        "The endpoint does not exist yet.",
        "It doesn't exist yet on this box.",
    ]:
        assert cap.detect(text), "expected a match for: %s" % text


def test_benign_prose_does_not_fire():
    # The exact generic tokens capability-gate over-matches on. None may fire.
    for text in [
        "Ran the suite against main and it was clean.",
        "verify the commit landed, then push",
        "Cleaned up the npc behavior fixture and re-ran it.",
        "The human-readable report is in temp/.",
        "I built the helper and committed it.",
        "This exists and is covered by tests.",
    ]:
        assert cap.detect(text) == [], "false positive on: %s" % text


def test_non_string_and_empty_input_are_safe():
    for bad in [None, "", 0, [], {}, 3.5]:
        assert cap.detect(bad) == []


# --------------------------------------------------------------------------
# advise()
# --------------------------------------------------------------------------

def test_advise_returns_none_when_nothing_matches():
    assert cap.advise("ran the tests against main, all clean") is None
    assert cap.advise(None) is None
    assert cap.advise("") is None


def test_advise_banner_carries_all_three_prompts():
    banner = cap.advise("no agent-provisionable path exists", field="defer_reason",
                        goal_id="g-999-01")
    assert banner is not None
    # 1. recency — the goal names this as the cheapest disproof, and it is the
    #    piece nothing else in the framework asks for.
    assert "RECENCY" in banner
    assert "execution-diary" in banner
    # 2. retrieval, WITH the flag that is load-bearing ().
    #    Bind the assertion to the COMMAND line, not to the banner as a whole:
    #    the banner also explains the flag in a following parenthetical, so a
    #    substring test over the whole string still passes when the flag is
    #    dropped from the command itself. Mutation-proven — removing it from the
    #    command left a bare `in banner` assertion green.
    cmd_lines = [ln for ln in banner.splitlines() if "retrieve.sh" in ln]
    assert len(cmd_lines) == 1, "expected exactly one retrieve.sh command line"
    assert "--include-framework" in cmd_lines[0], \
        "the retrieve command itself must carry --include-framework (g-115-3777)"
    assert "--depth" in cmd_lines[0]
    # 3. standing grants — a static capability catalog cannot see a permission
    #    that changed at a point in time
    assert "capability-routing.md" in banner
    # and it must say plainly that nothing is blocked
    assert "NOT blocked" in banner


def test_advise_names_the_field_and_goal():
    banner = cap.advise("only a human can do this", field="outcome_note",
                        goal_id="g-115-1")
    assert "outcome_note" in banner
    assert "g-115-1" in banner


def test_advise_without_context_still_composes():
    banner = cap.advise("cannot generate that artifact")
    assert banner is not None
    assert "RECENCY" in banner


def test_advise_never_raises_on_hostile_input():
    class Exploding(object):
        def __getattr__(self, name):
            raise RuntimeError("boom")
    for bad in [Exploding(), object(), 12, b"bytes"]:
        assert cap.advise(bad) is None


# --------------------------------------------------------------------------
# call-site field predicate (aspirations.py cmd_update_goal)
# --------------------------------------------------------------------------

ADVISORY_FIELDS = ("defer_reason", "description", "outcome_note")


def test_call_site_fields_are_the_durable_prose_fields():
    src = open(os.path.join(os.path.dirname(__file__), "..", "aspirations.py"),
               encoding="utf-8").read()
    assert "_capability_absence_patterns import advise" in src, \
        "call site missing from aspirations.py cmd_update_goal"
    for f in ADVISORY_FIELDS:
        assert '"%s"' % f in src


def test_call_site_is_wrapped_so_it_cannot_break_a_write():
    src = open(os.path.join(os.path.dirname(__file__), "..", "aspirations.py"),
               encoding="utf-8").read()
    idx = src.find("_capability_absence_patterns import advise")
    assert idx > 0
    window = src[max(0, idx - 400):idx + 400]
    assert "try:" in window and "except Exception:" in window, \
        "advisory call site must be exception-wrapped — it must never break a durable write"


def test_daemon_side_carries_the_same_advisory():
    """guard-742 parity: the advisory must exist on BOTH the CLI and daemon sides.

    This is the load-bearing test of the pair. aspirations-update-goal.sh is
    DAEMON-ONLY, so the daemon is the path every agent write actually takes; the
    CLI entry serves only the rb-428 sweeps. A CLI-only advisory would be inert
    on the live path — the exact failure mode g-115-3181 exists to fix, since
    exhaustive-search-gate (5 firings, all noop) and verify-before-assuming-gate
    (0 firings) are inert for precisely that reason.
    """
    daemon = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                          "mind_api", "src", "endpoints", "aspirations_write.py")
    src = open(daemon, encoding="utf-8").read()
    assert "_capability_absence_patterns import advise" in src, \
        "daemon update_goal is missing the capability-absence advisory (guard-742 parity)"
    # It must append to `warnings`, not print: daemon stderr goes to the daemon
    # log, while warnings[] is re-emitted to the caller's stderr by the wrapper.
    idx = src.find("_capability_absence_patterns import advise")
    # Window spans BOTH directions: the field predicate guards the try-block, so
    # it sits ABOVE the import, while warnings.append sits below. A forward-only
    # slice reports a missing field predicate that is right there — which is how
    # this test first went red.
    window = src[max(0, idx - 400):idx + 500]
    assert "warnings.append" in window, \
        "daemon advisory must append to warnings[] — printing would be unreachable"
    for f in ADVISORY_FIELDS:
        assert '"%s"' % f in window, \
            "daemon field predicate must match the CLI side for %s" % f
