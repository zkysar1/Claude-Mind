"""Tests for the requires_capability vocabulary gate ().

The defect this pins: KNOWN_CAPABILITIES called itself "the cross-file contract
between goal requires_capability values and the runner_capabilities config" and
had exactly three references fleet-wide — the definition, one subset assertion,
and a prose comment. Nothing validated against it, so an off-contract token was
a silent, permanent, fleet-wide fence (three measured live instances, one of them
revenue-bearing).

The fail-open cases below are not padding: a gate that cannot read its own
vocabulary must never refuse every capability-tagged filing on every box
(rb-1028), and that is the direction in which this gate would do real damage.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _runner_capabilities as rc  # noqa: E402
from gates.capability_vocab import evaluate  # noqa: E402


# --- the block direction ----------------------------------------------------

def test_unknown_token_is_refused():
    v = evaluate({"id": "g-1-1", "requires_capability": ["win-32-typo"]})
    assert v["would_block"] is True
    assert v["decision"] == "block"
    assert v["violations"] == ["unknown_capability_token"]


def test_refusal_message_names_the_offending_token_and_the_valid_set():
    v = evaluate({"id": "g-1-1", "requires_capability": ["vinheim-operator-api-key"]})
    msg = v["message"]
    assert "vinheim-operator-api-key" in msg
    # The author cannot fix the token without being told what IS valid.
    for tok in rc.KNOWN_CAPABILITIES:
        assert tok in msg


def test_free_text_value_is_refused():
    """The  shape: a prose sentence accepted silently as a token."""
    v = evaluate({"id": "g-115-9215",
                  "requires_capability": "live runner claim for agent dir foxtrot"})
    assert v["would_block"] is True


def test_a_bare_string_token_is_checked_not_ignored():
    """goal_required_capabilities accepts a bare str, so the gate must too —
    otherwise the single-token form (the most natural way to write it) is the
    one shape that slips through."""
    assert evaluate({"id": "g-1-1", "requires_capability": "not-a-real-cap"})["would_block"] is True


def test_mixed_valid_and_invalid_blocks_and_names_only_the_invalid():
    v = evaluate({"id": "g-1-1", "requires_capability": ["aws", "bogus-token"]})
    assert v["would_block"] is True
    assert "bogus-token" in v["message"]


# --- the pass / noop direction ---------------------------------------------

def test_known_token_passes():
    assert evaluate({"id": "g-1-1", "requires_capability": ["aws"]})["would_block"] is False


def test_absent_field_is_noop():
    v = evaluate({"id": "g-1-1"})
    assert v["decision"] == "noop"
    assert v["would_block"] is False


def test_null_field_is_noop():
    v = evaluate({"id": "g-1-1", "requires_capability": None})
    assert v["decision"] == "noop"


def test_never_auto_provided_token_is_NOT_refused():
    """studio-session is equally invisible when no box declares it, but it is a
    LEGITIMATE token that routes correctly the moment a Studio host declares it.
    Refusing it here would break real routing; the read-side block_detail is
    where that case is surfaced. Validating membership is necessary and NOT
    sufficient (zeta, 2026-09-06) — this pins that the gate does not overreach."""
    assert "studio-session" in rc.NEVER_AUTO_PROVIDED
    assert evaluate({"id": "g-1-1",
                     "requires_capability": ["studio-session"]})["would_block"] is False


# --- fail-open (the direction that would do fleet-wide damage) --------------

def test_unreadable_shape_fails_open():
    """goal_required_capabilities reads a non-str/list/tuple/set as 'no
    requirement', so refusing it here would disagree with the reader this gate
    exists to protect."""
    v = evaluate({"id": "g-1-1", "requires_capability": {"cap": "aws"}})
    assert v["would_block"] is False


def test_unresolvable_vocabulary_fails_open(monkeypatch):
    """If _runner_capabilities cannot be imported the gate must PASS, not block —
    otherwise one broken import refuses every capability-tagged filing fleet-wide."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "_runner_capabilities":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    v = evaluate({"id": "g-1-1", "requires_capability": ["definitely-not-a-cap"]})
    assert v["would_block"] is False
    assert v["decision"] == "pass"


def test_non_dict_goal_is_noop():
    assert evaluate("not-a-goal")["would_block"] is False


# --- the module-level helpers ----------------------------------------------

def test_win32_is_a_known_capability():
    """Goal check [0]: the originating token is now on the contract."""
    assert "win32" in rc.KNOWN_CAPABILITIES


def test_win32_is_probed_on_a_windows_box_not_hand_declared(monkeypatch):
    """The goal asks for win32 to be 'auto-provided by a platform probe rather
    than hand-declared per box'. This box is Linux, so the Windows branch is
    exercised by forcing the platform discriminators — stated plainly rather
    than claimed from a green run on the wrong OS (guard-4487)."""
    monkeypatch.setattr(os, "name", "nt", raising=False)
    assert "win32" in rc._probe_default_capabilities()


def test_win32_is_absent_on_a_posix_box():
    """Negative control for the probe — without it the test above would pass on
    a probe that unconditionally adds the token."""
    if os.name == "nt" or sys.platform.startswith("win"):
        pytest.skip("negative control is only meaningful off Windows")
    assert "win32" not in rc._probe_default_capabilities()


def test_unknown_capability_tokens_accepts_a_goal_dict_or_tokens():
    assert rc.unknown_capability_tokens({"requires_capability": ["aws"]}) == set()
    assert rc.unknown_capability_tokens(["aws", "nope"]) == {"nope"}
    assert rc.unknown_capability_tokens("nope") == {"nope"}
    assert rc.unknown_capability_tokens([]) == set()


# --- the block_detail wording (outcome [1]) ---------------------------------

def test_block_detail_says_NO_RUNNER_for_an_unknown_token():
    d = rc.capability_block_detail(["vinheim-operator-api-key"], {"aws", "git-push"})
    assert "NO RUNNER CAN EVER PROVIDE" in d
    assert "vinheim-operator-api-key" in d


def test_block_detail_flags_a_never_probed_token_distinctly():
    d = rc.capability_block_detail(["studio-session"], {"aws"})
    assert "NO RUNNER CAN EVER PROVIDE" not in d   # it IS providable — by declaration
    assert "never auto-probed" in d


def test_block_detail_for_an_ordinary_per_box_gap_stays_plain():
    """The one case where 'some other runner will take it' is TRUE keeps the
    original wording — the fix must not shout on the common, correct block."""
    d = rc.capability_block_detail(["gpu"], {"aws", "git-push"})
    assert "NO RUNNER" not in d
    assert "never auto-probed" not in d
    assert "gpu" in d and "aws" in d


def test_block_detail_reports_runner_caps_as_none_when_empty():
    assert "runner has: none" in rc.capability_block_detail(["gpu"], set())
