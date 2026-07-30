"""test_capability_gate_conditional_cure.py — regression tests for .

USER_ONLY_PRECONDITION_CURES mapped a precondition substring to a bare action
string, and a resolvable cure ALONE forces the refusal (`cure_block`). Every
cure was therefore UNCONDITIONAL — including `active_sessions=0 ->
roblox-studio.sh start-session`, whose own precondition capability-routing.md
scopes to "anytime roblox-studio.sh status returns plugin_connected: true".

When that precondition was unmet the gate refused the defer anyway and filed an
Unblock instructing an action that could not succeed. Measured cost: 6 impossible
Unblock goals across asp-307/318/335/326 — g-307-48, g-307-51, g-318-43, g-318-68
SKIPPED by four different agents, g-335-246 BLOCKED 2.5 days, g-326-70 filed the
day this was fixed. The defect was never "the gate ignores the condition"; it was
that the registry COULD NOT EXPRESS one.

WHAT IS PINNED HERE

  1. RECALL FIRST (guard-958). Tightening a keyword-matching safety gate loses
     recall SILENTLY, so the un-disproved cure firing is the first assertion in
     this file, not an afterthought. If only the exemption cases were pinned, a
     future edit could disable the cure registry outright and stay green.

  2. All three disproof spellings, because the marker is copied out of tool
     output whose separator varies: JSON (`plugin_connected: false`), kv
     (`plugin_connected=false`), prose (`the plugin is not connected`). A
     literal-substring implementation passes the first and fails the rest.

  3. SEMANTIC EQUIVALENCE to an already-sanctioned shape. A disproved cure must
     behave byte-identically to a precondition registered with cure `None`
     (e.g. `player_keypress_required`), which has been the accepted user-only
     exemption since g-115-372. This is the assertion that says the change
     routes into an EXISTING bucket rather than inventing a new escape hatch —
     including for the mixed case where an agent-provisionable verb sits in the
     same text.

  4. Disproved stays DISTINGUISHABLE from no-cure-registered in the payload.
     Both collapse to cure_action=None at the block sites, so without
     `cure_disproved_by` a non-firing is unattributable after the fact.

EVIDENCE-SHAPED, NOT PROBE-SHAPED: the predicate reads what the caller already
measured rather than shelling out to re-measure. That also repairs an inversion
worth stating — before this, QUOTING your diagnostic output made refusal more
likely, because the quoted marker was what the substring scan matched on. A
well-evidenced defer must not be punished for its evidence.

Written pytest-collectable ON PURPOSE. The sibling coverage for this registry
(test_capability_gate_user_only_precondition.py) is a main()-style file that
pytest collects ZERO tests from — verified during this goal — so it never runs
in the mandated `pytest core/scripts/tests` sweep (the g-115-2349
invisible-suite class, which left 9 silent reds).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.capability import (  # noqa: E402
    USER_ONLY_PRECONDITION_CURES,
    _resolve_cure_action,
    evaluate,
)

# The precondition whose cure is conditional, with no disproof present.
_LIVE = "Deferred: active_sessions=0 so verification cannot run."

# A precondition registered with cure None — the already-sanctioned exemption
# shape () that a disproved cure must become indistinguishable from.
_NONE_CURE = "Deferred: player_keypress_required — needs a human at the keyboard."


def _eval(text: str) -> dict:
    return evaluate(text, intended_participants="user", caller_context="defer")


# ------------------------------------------------------------- recall control

def test_registered_cure_still_blocks_when_not_disproved():
    """guard-958 recall control — FIRST, because this is what a tightening loses.

    With no disproof marker present the registry must behave exactly as it did
    before the conditional form existed. A green suite that only pinned the
    exemptions would happily accept a registry that never fires at all.
    """
    d = _eval(_LIVE)
    assert d["would_block"] is True, (
        f"an un-disproved registered cure must still force the refusal; got "
        f"would_block={d['would_block']!r} cure_action={d['cure_action']!r}"
    )
    assert d["cure_action"] == USER_ONLY_PRECONDITION_CURES[
        "active_sessions=0"]["action"], (
        f"cure_action must still resolve to the registered action; got "
        f"{d['cure_action']!r}"
    )
    assert d["cure_disproved_by"] is None, (
        f"nothing in this text disproves the cure; got "
        f"{d['cure_disproved_by']!r}"
    )


# ------------------------------------------------------- disproof recognition

@pytest.mark.parametrize("label,text", [
    ("json",  "Deferred: active_sessions=0. Probed roblox-studio.sh status -> "
              "plugin_connected: false, so start-session cannot work."),
    ("kv",    "Deferred: insufficient_session_data. status shows "
              "plugin_connected=false."),
    ("prose", "Deferred: active_sessions=0 and the plugin is not connected."),
])
def test_disproved_cure_does_not_force_refusal(label, text):
    """Each separator spelling the marker actually appears in must be caught.

    A literal-substring implementation passes `json` and fails the other two —
    which is why all three are parametrized rather than represented by one.
    """
    d = _eval(text)
    assert d["would_block"] is False, (
        f"[{label}] caller already disproved the cure in its own text; the gate "
        f"must not refuse and must not file an Unblock instructing an action "
        f"that cannot succeed. got would_block={d['would_block']!r} "
        f"cure_action={d['cure_action']!r}"
    )
    assert d["cure_action"] is None, (
        f"[{label}] a disproved cure must not resolve; got {d['cure_action']!r}"
    )
    assert d["cure_disproved_by"], (
        f"[{label}] the disproof must be recorded — otherwise this non-firing "
        f"is indistinguishable from 'no cure registered'. got "
        f"{d['cure_disproved_by']!r}"
    )


def test_disproof_is_distinguishable_from_no_cure_registered():
    """Both collapse to cure_action=None; only the payload separates them."""
    disproved = _eval(
        "Deferred: active_sessions=0. status: plugin_connected: false.")
    no_cure = _eval(_NONE_CURE)

    assert disproved["cure_action"] is None and no_cure["cure_action"] is None
    assert disproved["cure_disproved_by"] is not None, "disproof unrecorded"
    assert no_cure["cure_disproved_by"] is None, (
        f"a precondition with NO registered cure was never disproved — "
        f"reporting one would be a false attribution; got "
        f"{no_cure['cure_disproved_by']!r}"
    )


# --------------------------------------------------------- semantic-equivalence

@pytest.mark.parametrize("suffix", [
    "",                      # bare precondition
    " Also push the fix.",   # + an agent-provisionable verb in the same text
])
def test_disproved_cure_matches_sanctioned_none_cure_semantics(suffix):
    """A disproved cure must land in the bucket  already sanctioned.

    The mixed case is the one that matters: with an agent-provisionable verb in
    the same text, the disproved-cure path and the None-cure path must agree. If
    they diverge, the change invented a NEW escape hatch rather than reusing the
    accepted user-only exemption — and that is a decision needing its own goal,
    not a side effect of this one.
    """
    disproved = _eval(
        "Deferred: active_sessions=0, plugin_connected: false." + suffix)
    none_cure = _eval(_NONE_CURE + suffix)

    assert disproved["would_block"] == none_cure["would_block"], (
        f"disproved-cure and None-cure must agree on would_block "
        f"(suffix={suffix!r}): {disproved['would_block']!r} vs "
        f"{none_cure['would_block']!r}"
    )
    assert disproved["would_block"] is False, (
        "both shapes are user-only exemptions — neither may force a refusal"
    )


# ----------------------------------------------------------- resolver contract

def test_resolver_returns_pair_and_still_accepts_bare_string_cures():
    """The registry must keep accepting an UNCONDITIONAL bare-string cure.

    Five of the seven entries are None and two are dicts today, but the bare
    string is the form every entry had before this change and the form a future
    genuinely-unconditional cure should still be able to use. Pinned against a
    synthetic registry rather than a live key so it cannot rot when the domain
    entries change.
    """
    import gates.capability as cap

    saved = cap.USER_ONLY_PRECONDITION_CURES
    try:
        cap.USER_ONLY_PRECONDITION_CURES = {"synthetic_precon": "do the thing"}
        action, disproof = cap._resolve_cure_action(
            ["synthetic_precon"], "any text at all")
        assert action == "do the thing", (
            f"bare-string cure must still resolve; got {action!r}")
        assert disproof is None, (
            f"an unconditional cure can never be disproved; got {disproof!r}")
    finally:
        cap.USER_ONLY_PRECONDITION_CURES = saved

    # Unmatched precondition → no cure, no disproof.
    assert _resolve_cure_action([], "text") == (None, None)
    assert _resolve_cure_action(["not_a_registered_precon"], "text") == (
        None, None)


def test_disproof_patterns_accept_plain_strings_not_only_compiled():
    """A plain-string disproof entry must work, not raise inside the gate.

    Found by the fresh-eyes pass on this same change. Everything adjacent in the
    registry is a plain string — USER_ONLY_PRECONDITION_SUBSTRINGS, and the
    sibling "action" key — so `"disproved_by": ["plugin_connected"]` is the
    natural way to write a new entry, not an exotic one. Compiled-only raised
    AttributeError INSIDE evaluate(), which the daemon calls on every narrative
    defer: the failure would surface as a 500 on a write rather than a fail-open
    verdict. That is strictly worse than the false refusal this registry exists
    to prevent — a gate must not be the thing that breaks the write.
    """
    import gates.capability as cap

    assert cap._cure_disproof_hit(
        "plugin_connected: false", ["plugin_connected"]) == "plugin_connected"
    # Compiled entries keep working, and a mixed list resolves either kind.
    assert cap._cure_disproof_hit(
        "plugin_connected: false", cap._CURE_DISPROOF_NO_PLUGIN)
    assert cap._cure_disproof_hit(
        "the plugin is not connected",
        ["nomatch"] + cap._CURE_DISPROOF_NO_PLUGIN)
    # And a non-match still returns None rather than a truthy artifact.
    assert cap._cure_disproof_hit("all good here", ["plugin_connected"]) is None


def test_disproved_cure_does_not_end_the_scan():
    """A disproved cure must not hide a LATER precondition's viable cure.

    The pre-conditional loop returned the first USABLE cure and skipped past
    unusable entries. Returning early on a disproof breaks that: a text matching
    two preconditions where only the first is disproved would be exempted, when
    the second's cure would have worked — a silent recall loss (guard-958), and
    the failure direction that never announces itself.

    Latent against the live registry (both conditional entries share one disproof
    set), so it is pinned against a synthetic registry — otherwise the property
    is untested until the entry that breaks it already shipped.
    """
    import re as _re

    import gates.capability as cap

    saved = cap.USER_ONLY_PRECONDITION_CURES
    try:
        cap.USER_ONLY_PRECONDITION_CURES = {
            "precon_a": {
                "action": "cure A",
                "disproved_by": [_re.compile(r"A-is-out", _re.IGNORECASE)],
            },
            "precon_b": "cure B",
        }
        action, disproof = cap._resolve_cure_action(
            ["precon_a", "precon_b"], "A-is-out, but nothing rules B out.")
        assert action == "cure B", (
            f"scan must continue past a disproved cure to a viable one; got "
            f"action={action!r} disproof={disproof!r}"
        )
        assert disproof is None, (
            f"a usable cure was found, so the run is a REFUSAL — reporting a "
            f"disproof alongside it would misattribute why; got {disproof!r}"
        )

        # And when every candidate is disproved, the first disproof is reported.
        cap.USER_ONLY_PRECONDITION_CURES = {
            "precon_a": {
                "action": "cure A",
                "disproved_by": [_re.compile(r"A-is-out", _re.IGNORECASE)],
            },
            "precon_b": {
                "action": "cure B",
                "disproved_by": [_re.compile(r"B-is-out", _re.IGNORECASE)],
            },
        }
        action, disproof = cap._resolve_cure_action(
            ["precon_a", "precon_b"], "A-is-out and B-is-out.")
        assert action is None, f"no cure survives; got {action!r}"
        assert disproof == "A-is-out", (
            f"the FIRST disproof is the attributable one; got {disproof!r}")
    finally:
        cap.USER_ONLY_PRECONDITION_CURES = saved
