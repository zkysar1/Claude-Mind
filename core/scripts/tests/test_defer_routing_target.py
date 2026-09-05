"""Tests for gates/defer_routing_target.py ().

Each test pins one of the five MEASURED design constraints in the module
docstring, plus the three measured instances the goal names. The constraints are
not stylistic — every one of them is a shape that would have shipped a check
that looks correct in review and fires on the wrong population.
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from gates.defer_routing_target import (  # noqa: E402
    evaluate,
    extract_agent_targets,
    extract_grant_citations,
    extract_owner_phrases,
)

ROSTER = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "omni", "zeta"]

# A minimal registry carrying BOTH tables the module reads. The pin columns are
# ABRIDGED FROM THE LIVE pin-001 and must stay faithful to its shape: an in-lane
# list that includes "promotion", and an out-of-lane list that includes "server"
# — that exact overlap is what makes the  case `ambiguous` rather than
# out-of-lane, and a fixture that drops one side silently tests a different pin
# than the one in production.
REGISTRY = """
## Standing User Grants

| id | scope | granted | source | quote | expires |
|----|-------|---------|--------|-------|---------|
| grant-001 | commit, push (all repos) | 2026-04-20 | user email | "go ahead" | never |
| grant-007 | product-repo PR merge — agent may merge product-estate PRs | 2026-07-18 | user | "q" | never |

## Standing Lane Pins

| id | agent | in lane | out of lane | set | source | authority | review |
|----|-------|---------|-------------|-----|--------|-----------|--------|
| pin-001 | foxtrot | RUN WORLDS: agent-initiated no-player sessions on dev+ppe+prod; in-session verification; session-log capture; Studio-host provisioning; promotion per grant-008; own agent hygiene | ALL CODE work: server Lua, client Lua, workflows, analyzers, env-server, framework scripts, EFS log analysis, web-API probes | 2026-08-06 | user directive | user directive only | 2026-11-04 |

## Something Else
"""


@pytest.fixture()
def world(tmp_path):
    conv = tmp_path / "conventions"
    conv.mkdir(parents=True)
    (conv / "capability-routing.md").write_text(REGISTRY, encoding="utf-8")
    return tmp_path


def _ev(text, world=None, roster=ROSTER):
    return evaluate("g-000-1", text, world_dir=world, roster=roster)


# ---------------------------------------------------------------- constraint 1
def test_a_STRUCTURED_defer_is_checked_not_skipped(world):
    """Constraint 1: trigger on the FIELD, never on is_narrative_defer.

    Measured 2026-09-04: 131 of 131 live non-terminal defers are STRUCTURED and
    0 are narrative. A check gated on narrative-ness fires on zero of the real
    population while looking correct in review (guard-1802).
    """
    text = ("precondition_unmet: routed to foxtrot — rewrite the analyzer "
            "workflow scripts and the env-server Java.")
    assert text.startswith("precondition_unmet:")   # it IS a structured defer
    r = _ev(text, world)
    assert r["refuse"] is True
    assert "pin-001" in r["reason"]


# ---------------------------------------------------------------- constraint 2
def test_a_bare_mention_of_an_agent_is_NOT_a_routing_claim():
    """Constraint 2: WIDE flags 91 live defers, ROLE-AWARE flags 8."""
    assert extract_agent_targets("bravo already measured this on cc-05", ROSTER) == []
    assert extract_agent_targets("see the note zeta left in progress_note", ROSTER) == []
    hits = extract_agent_targets("this is routed to foxtrot for the session", ROSTER)
    assert [h["agent"] for h in hits] == ["foxtrot"]


def test_possessive_own_is_excluded_but_lane_is_not():
    """"<agent>'s own X" describes that agent's surface, not a routing-away.

    The `own` marker must be excluded STRUCTURALLY (inside the pattern), not by
    inspecting the text after the match. Found by mutation test 2026-09-04: as a
    tail-slice check it was unreachable for the phrase it was written for, and
    the only shape that reached it was a genuine routing claim it suppressed.
    The two `lane owner` assertions below are what pin that — drop them and the
    dead-and-wrong check passes again.
    """
    assert extract_agent_targets("foxtrot's own agent hygiene", ROSTER) == []
    assert extract_agent_targets("foxtrot's own lane", ROSTER) == []
    assert extract_agent_targets("foxtrot's own queue", ROSTER) == []
    hits = extract_agent_targets("that sits in foxtrot's lane", ROSTER)
    assert [h["agent"] for h in hits] == ["foxtrot"]
    # A word merely STARTING with "own" after the noun is not an exclusion.
    owner = extract_agent_targets("foxtrot's lane owner signed off", ROSTER)
    assert [h["agent"] for h in owner] == ["foxtrot"]


def test_clause_scoping_does_not_refuse_a_correct_defer(world):
    """The measured  false positive.

    Its defer routes only "a DEV session" (in-lane) while naming env-server,
    Java and Lua as GOAL CONTEXT. Evaluating the whole text refused it; scoping
    to the routing clause must not.
    """
    text = ("precondition_unmet: outcome 2 is MET (counter identified in the "
            "env-server relationships view, sourced from IntentEngineVerticle "
            "Java, target-linked by construction). What remains is outcome 1's "
            "DECISION half: branch A needs a DEV session showing the toAyoKey "
            "edge, foxtrot's lane per pin-001; branch B needs a reasoning-bank "
            "amendment which is reducer-only.")
    r = _ev(text, world)
    assert r["refuse"] is False, r["reason"]


# ---------------------------------------------------------------- constraint 3
def test_unknown_grant_is_refused(world):
    r = _ev("precondition_unmet: covered by grant-004, awaiting that window.", world)
    assert r["refuse"] is True
    assert "grant-004" in r["reason"]


def test_known_grant_beside_blocking_language_is_NOT_refused(world):
    """Constraint 3, the measured ceiling.

    A "grant cited beside blocking language contradicts the grant" rule was
    tested against all 9 live grant-citing defers and would have refused 9 of 9
    CORRECT defers — most cite a grant precisely to say it does NOT apply
    (g-335-1438: "grant-007 does not cover it"). Existence is the honest ceiling.
    """
    text = ("precondition_unmet: charter gate, not a capability gap. Standing "
            "merge grants authorize the WORK, never the prod blast radius, so "
            "grant-007 does not cover it; awaiting the promotion window.")
    r = _ev(text, world)
    assert r["refuse"] is False, r["reason"]
    assert [c["grant"] for c in r["grant_citations"]] == ["grant-007"]
    assert r["grant_citations"][0]["known"] is True


def test_grant_extraction_dedupes_and_lowercases():
    assert extract_grant_citations("GRANT-010 and grant-010 and grant-014") == \
        ["grant-010", "grant-014"]


# ---------------------------------------------------------------- constraint 4
def test_ambiguous_pin_verdict_advises_and_never_refuses(world):
    """Constraint 4 + outcome 5: the historical  case is CAUGHT.

    "promotion" is in pin-001's in-lane column (its own promotion item) while "server"
    is out-of-lane, so lane_pin returns `ambiguous` and — by its deliberate
    claim-time posture — allows. At DEFER time that ambiguity is worth a word,
    so it becomes an ADVISORY. It is NOT upgraded to a refusal: widening
    lane_pin would risk false refusals on the hotter claim path.
    """
    text = ("precondition_unmet: the dev->main promotion of "
            "Ayoai-Environment-Server is routed to foxtrot under grant-007.")
    r = evaluate("g-369-112", text, world_dir=world, roster=ROSTER)
    assert r["refuse"] is False
    assert any("ambiguous" in a or "BOTH in-lane and out-of-lane" in a
               for a in r["advisories"]), r["advisories"]
    assert r["agent_targets"][0]["verdict"] == "ambiguous"


def test_in_lane_routing_is_silent(world):
    r = _ev("precondition_unmet: routed to foxtrot to run a no-player session "
            "on dev with in-session verification.", world)
    assert r["refuse"] is False
    assert r["advisories"] == []


# ---------------------------------------------------------------- outcome 3
def test_owner_phrase_advises_never_refuses(world):
    """Outcome 3, and the two resource-ownership instances (/).

    Refusing on prose is the guard-1470 false-positive shape.
    """
    for text in (
        "precondition_unmet: the digest cannot send until an upstream owner "
        "restores handle reservations in product-accounts.",
        "precondition_unmet: cross-deployment, section 8 assigns the vin-key row "
        "to Omni in zds-accounts, not self-service.",
    ):
        r = _ev(text, world)
        assert r["refuse"] is False
        assert r["advisories"], text
        assert "NOT refused" in r["advisories"][-1]


def test_owner_phrase_is_discharged_by_a_probe_citation(world):
    text = ("precondition_unmet: an upstream owner holds this row. RE-PROBED "
            "2026-09-01: describe-table returns AccessDenied for the fleet "
            "principal, so the boundary is measured.")
    r = _ev(text, world)
    assert r["advisories"] == []


def test_owner_phrase_extraction_is_narrow():
    """Deliberately narrow: an advisory that fires on every 'owner' mention
    becomes noise and gets trained away. Live population measured 1 of 131."""
    assert extract_owner_phrases("owner directive 2026-08-06 says so") == []
    assert extract_owner_phrases("the owner emailed the reply") == []
    assert extract_owner_phrases("blocked until an upstream owner acts")


# ---------------------------------------------------------------- constraint 5
def test_empty_roster_skips_the_agent_lane_rather_than_guessing(world):
    """Constraint 5: an unresolvable roster skips the lane; it never widens it."""
    r = evaluate("g-000-1", "precondition_unmet: routed to foxtrot — env-server Java.",
                 world_dir=world, roster=[])
    assert r["refuse"] is False
    assert r["agent_targets"] == []
    assert "roster" in (r["skip_reason"] or "")


def test_roster_defaults_to_the_fleet_ssot_not_a_glob(monkeypatch):
    """No caller supplies a roster, so no caller can supply a wrong one.

    Measured 2026-09-04: globbing team-state/agents/*.yaml returns 15 names on
    this box, 10 of them test residue, against the SSOT's 5. The roster is the
    allowlist the refusal path keys on.
    """
    import gates.defer_routing_target as mod
    assert mod._default_roster.__doc__, "the default must be an explicit helper"

    calls = {}

    def fake():
        calls["hit"] = True
        return ["foxtrot"]

    monkeypatch.setattr(mod, "_default_roster", fake)
    mod.evaluate("g-000-1", "precondition_unmet: routed to foxtrot for a session.",
                 world_dir=None)
    assert calls.get("hit"), "evaluate() must resolve the roster when none is passed"


def test_default_roster_is_failopen(monkeypatch):
    import gates.defer_routing_target as mod
    monkeypatch.setitem(__import__("sys").modules, "_agents", None)
    assert mod._default_roster() == []


# ---------------------------------------------------------------- fail-open
def test_unreadable_world_never_refuses_a_grant(tmp_path):
    """guard-142: fail OPEN on your own dependency. An unreadable registry must
    not turn every grant citation into a refusal."""
    r = _ev("precondition_unmet: covered by grant-004.", tmp_path / "nope")
    assert r["refuse"] is False
    assert r["grant_citations"][0]["known"] is None


def test_empty_and_none_defer_text_are_noops(world):
    for val in ("", "   ", None):
        r = evaluate("g-000-1", val, world_dir=world, roster=ROSTER)
        assert r["refuse"] is False
        assert r["checked"] is False


def test_shape_is_always_identical(world):
    keys = {"refuse", "reason", "advisories", "agent_targets", "grant_citations",
            "owner_phrases", "checked", "skip_reason"}
    for text in ("", "precondition_unmet: nothing here",
                 "precondition_unmet: routed to foxtrot — env-server Java.",
                 "precondition_unmet: grant-099 authorises it."):
        assert set(_ev(text, world).keys()) == keys


def test_refusal_names_the_existing_override_and_invents_no_second_one(world):
    """Outcome 4: reuse --force-defer; do not introduce a second override."""
    r = _ev("precondition_unmet: routed to foxtrot — env-server Java analyzers.", world)
    assert r["refuse"] is True
    assert "--force-defer" in r["reason"]
    assert "--override-routing-target" not in r["reason"]
    assert "--override-agent-match" not in r["reason"]
