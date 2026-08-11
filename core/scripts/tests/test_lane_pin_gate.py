"""Tests for the lane-pin claim gate ().

A lane pin is a durable user-directed constraint fixing ONE agent's work
surface, recorded as a row in the ``## Standing Lane Pins`` table of the world's
capability-routing convention. The gate parses that table LIVE, so deleting a
row lifts the pin with no code change — the auto-lift case below is what proves
that property rather than merely asserting it in a docstring.

The registry fixture is SYNTHETIC and domain-free, but it reproduces the exact
STRUCTURE of the live pin it was tuned against: a lane column that mixes a
semicolon-separated ENUMERATION of short lane items with parentheticals, and an
out-of-lane column that appends COMMENTARY sentences about how the agent should
behave. Two of those tests exist because that structure produced measured false
refusals (see test_commentary_parenthetical_tokens_are_not_lane_evidence) —
they are the regression pins for the word caps, and a fixture that dropped the
commentary sentences would silently stop testing the thing that broke.
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
for _p in (_SCRIPTS, _SCRIPTS / "gates"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402

import lane_pin as lp  # noqa: E402


PIN_AGENT = "pinnedagent"
OTHER_AGENT = "unpinnedagent"

_IN_CELL = (
    "RUN WIDGET SESSIONS: agent-initiated headless sessions on staging "
    "(standing item, recurring 1h); in-session verification (clean boot, "
    "autonomous steps); trace capture to storage; RELAY/BOX-LOCAL fixes only "
    "(relay, portproxy, host plugin, host config, box infra); host-only "
    "provisioning; own agent hygiene (temp drain, own experience-store "
    "maintenance)."
)
# The second and third sentences are COMMENTARY, not enumeration. The
# parenthetical inside the second is the exact shape that leaked tokens before
# the parent-clause cap existed.
_OUT_CELL = (
    "ALL CODE work: gadget scripts, client scripts, workflows, analyzers, "
    "env-server, framework scripts, trace log analysis/measurement, web-API "
    "probes runnable from any box, doc trims. Pinnedagent FILES code defects "
    "with evidence (error text, session id, storage path) and never "
    "root-causes or fixes them inline. Selector score is NOT a justification "
    "for an out-of-lane claim."
)

_HEADER = (
    "| id | agent | in-lane | out-of-lane | pinned | provenance | expires |\n"
    "|----|-------|---------|-------------|--------|------------|---------|\n"
)


def _registry(rows: str = None) -> str:
    """A capability-routing document with a Standing Lane Pins section."""
    if rows is None:
        rows = (f"| pin-t01 | {PIN_AGENT} | {_IN_CELL} | {_OUT_CELL} | "
                f"2026-08-06 | user directive | user directive only "
                f"(revoke by deleting this row) |\n")
    return ("# Capability Routing\n\nPreamble prose.\n\n"
            "## Standing Lane Pins\n\n" + _HEADER + rows +
            "\n## Some Later Section\n\nUnrelated content.\n")


@pytest.fixture(autouse=True)
def _fixed_roster(monkeypatch):
    """Pin the roster so verdicts do not depend on the live fleet.

    `parse_pins` excludes agent names from lane evidence via `_roster()`, whose
    real implementation reads the live fleet. None of the fixture vocabulary
    collides with real agent names, so this changes no outcome — it makes that
    independence explicit instead of incidental.
    """
    monkeypatch.setattr(lp, "_roster", lambda: {"alpha", "bravo", PIN_AGENT})


def _goal(title, description="", category=""):
    return {"title": title, "description": description, "category": category}


def _evaluate(agent, goal, tmp_path, **kw):
    """Always route telemetry at a tmp meta dir — never the live gate log."""
    kw.setdefault("registry_text", _registry())
    return lp.evaluate(agent, goal, meta_dir=tmp_path, **kw)


# ── The four outcomes the goal's verification names ────────────────────────

def test_out_of_lane_claim_by_a_pinned_agent_is_refused(tmp_path):
    result = _evaluate(PIN_AGENT,
                       _goal("Fix the retry logic in the framework scripts"),
                       tmp_path)
    assert result["would_block"] is True
    assert result["verdict"] == "out-of-lane"
    assert result["pin_id"] == "pin-t01"
    assert "framework scripts" in result["evidence"]


def test_in_lane_claim_by_a_pinned_agent_is_allowed(tmp_path):
    result = _evaluate(PIN_AGENT,
                       _goal("Run widget sessions on staging and capture "
                             "traces to storage"),
                       tmp_path)
    assert result["would_block"] is False
    assert result["verdict"] == "in-lane"


def test_unpinned_agent_takes_the_no_pin_path(tmp_path):
    """An agent with no row is untouched — including on out-of-lane text.

    `fired` False is the load-bearing half: an unpinned claim is not a gate
    event, so it must not emit telemetry that would dilute the firing stats
    this gate's retirement evaluation reads.
    """
    result = _evaluate(OTHER_AGENT,
                       _goal("Fix the retry logic in the framework scripts"),
                       tmp_path)
    assert result["would_block"] is False
    assert result["fired"] is False
    assert result["verdict"] == "no-pin"
    assert result["pin_id"] is None


def test_deleting_the_row_lifts_the_pin_with_no_code_change(tmp_path):
    """Auto-lift: the SAME goal+agent that blocks above now passes."""
    blocked = _evaluate(PIN_AGENT,
                        _goal("Fix the retry logic in the framework scripts"),
                        tmp_path)
    assert blocked["would_block"] is True

    lifted = _evaluate(PIN_AGENT,
                       _goal("Fix the retry logic in the framework scripts"),
                       tmp_path, registry_text=_registry(rows=""))
    assert lifted["would_block"] is False
    assert lifted["verdict"] == "no-pin"


def test_override_allows_but_still_reports_out_of_lane(tmp_path):
    """The override must not launder the CLASSIFICATION.

    `verdict` stays "out-of-lane" and `evidence` survives so the ledger row
    records what was actually bypassed. A gate that downgraded its own verdict
    on override would make the ledger unreadable at audit time.
    """
    result = _evaluate(PIN_AGENT,
                       _goal("Fix the retry logic in the framework scripts"),
                       tmp_path,
                       override_lane_pin="host is the only box with the repro")
    assert result["would_block"] is False
    assert result["override"] == "host is the only box with the repro"
    assert result["verdict"] == "out-of-lane"
    assert "framework scripts" in result["evidence"]


# ── The measured false-refusal regression (why the word caps exist) ────────

def test_commentary_parenthetical_tokens_are_not_lane_evidence(tmp_path):
    """`(error text, session id, storage path)` must contribute NO tokens.

    Its parent clause — "Pinnedagent FILES code defects with evidence" — is
    prose, not a list item. Before the parent-clause cap, this parenthetical
    was lifted and harvested anyway, putting `error` and `session` into the
    out-of-lane token set; that wrongly refused two real goals whose only
    connection to the lane was the word "session".

    Asserted structurally rather than only behaviorally because widening either
    word cap re-admits the whole class SILENTLY — a behavioral test alone would
    keep passing for every goal that happens not to say "session".
    """
    pin = lp.parse_pins(_registry())[0]
    assert "error" not in pin["out_tokens"]
    assert "session" not in pin["out_tokens"]
    # The ENUMERATION in the same cell is still harvested — the cap separates
    # commentary from list items, it does not just discard the tail.
    assert {"workflows", "analyzers", "framework"} <= pin["out_tokens"]


def test_goal_matching_only_a_commentary_token_is_allowed(tmp_path):
    """Behavioral half of the regression above (real title, )."""
    result = _evaluate(PIN_AGENT,
                       _goal("Investigate: does making the runtime signal "
                             "arrive let a session start"),
                       tmp_path)
    assert result["would_block"] is False


def test_out_of_lane_token_in_the_description_alone_is_not_evidence(tmp_path):
    """A bare noun in a description is incidental vocabulary, not work type.

    Descriptions cite file paths procedurally. Measured over all 1,505 live
    pending goals: matching single tokens against the description refused a
    routine inbox sweep on the lone token `scripts`, harvested from a
    `bash world/scripts/...` command inside its own procedure.
    """
    result = _evaluate(PIN_AGENT,
                       _goal("Check agent email inbox for alerts",
                             description="Step 1 — run "
                                         "bash world/scripts/inbox-pull.sh "
                                         "check --json, then triage."),
                       tmp_path)
    assert result["would_block"] is False


def test_out_of_lane_phrase_in_the_description_IS_evidence(tmp_path):
    """The complement: a registry PHRASE is specific enough to match anywhere.

    Without this the description would be inert, and a goal could describe
    out-of-lane work in full while keeping a neutral title.
    """
    result = _evaluate(PIN_AGENT,
                       _goal("Investigate a reported defect",
                             description="The retry loop in the framework "
                                         "scripts drops the last attempt."),
                       tmp_path)
    assert result["would_block"] is True
    assert "framework scripts" in result["evidence"]


def test_category_participates_in_token_matching(tmp_path):
    """Category is part of the token surface — deliberately, and it dominates.

    Measured against the live registry over 1,505 pending goals: 658 of 690
    refusals (95%) came from the CATEGORY alone, 527 of those from a single
    category matching one token. That is not a defect — a category states the
    work type as directly as a title does — but it means this gate's real-world
    behavior is sensitive to the category taxonomy, so narrowing the surface to
    titles alone would collapse refusals to ~2% of the queue. Pinned here so
    that change has to be made on purpose.
    """
    result = _evaluate(PIN_AGENT,
                       _goal("Recurring: freshness drift sweep",
                             category="framework-architecture"),
                       tmp_path)
    assert result["would_block"] is True
    assert "framework" in result["evidence"]


def test_agent_names_are_never_lane_evidence(tmp_path):
    """The row names its own agent in prose; that must not match anything."""
    pin = lp.parse_pins(_registry())[0]
    assert PIN_AGENT not in pin["out_tokens"]
    assert PIN_AGENT not in pin["in_tokens"]


def test_tokens_present_in_both_columns_are_dropped_from_both(tmp_path):
    """A token on both sides discriminates nothing, so it is evidence for
    neither. `trace` appears in "trace capture to storage" (in-lane) and
    "trace log analysis/measurement" (out-of-lane)."""
    pin = lp.parse_pins(_registry())[0]
    assert "trace" not in pin["out_tokens"]
    assert "trace" not in pin["in_tokens"]


# ── Ambiguity and fail-open: the reason a false refusal cannot happen ──────

def test_matching_both_columns_allows_as_ambiguous(tmp_path):
    """The pin does not settle this goal, so it does not get to refuse it."""
    result = _evaluate(PIN_AGENT,
                       _goal("Run widget sessions, then fix the framework "
                             "scripts that parse the trace"),
                       tmp_path)
    assert result["would_block"] is False
    assert result["verdict"] == "ambiguous"


@pytest.mark.parametrize("registry_text", [
    None,                                  # no registry reachable at all
    "",                                    # empty document
    "# Capability Routing\n\nNo pins section here.\n",
    "## Standing Lane Pins\n\nProse but no table.\n",
    "## Standing Lane Pins\n\n" + _HEADER,  # header + separator, no rows
    "## Standing Lane Pins\n\n| pin-t01 | too | few |\n",  # malformed row
])
def test_unusable_registry_fails_open(tmp_path, registry_text):
    result = lp.evaluate(PIN_AGENT,
                         _goal("Fix the retry logic in the framework scripts"),
                         registry_text=registry_text, world_dir=None,
                         meta_dir=tmp_path)
    assert result["would_block"] is False
    assert result["verdict"] == "no-pin"


@pytest.mark.parametrize("agent,goal", [
    ("", _goal("Fix the retry logic in the framework scripts")),
    (None, _goal("Fix the retry logic in the framework scripts")),
    (PIN_AGENT, _goal("")),
    (PIN_AGENT, None),
    (PIN_AGENT, "not a dict"),
    (PIN_AGENT, {"title": None, "description": None}),
])
def test_unusable_input_fails_open(tmp_path, agent, goal):
    result = _evaluate(agent, goal, tmp_path)
    assert result["would_block"] is False


def test_an_exception_inside_the_gate_fails_open(tmp_path, monkeypatch):
    """A broken gate must never wedge the fleet's claims."""
    def _boom(_text):
        raise RuntimeError("registry parser exploded")
    monkeypatch.setattr(lp, "parse_pins", _boom)
    result = _evaluate(PIN_AGENT,
                       _goal("Fix the retry logic in the framework scripts"),
                       tmp_path)
    assert result["would_block"] is False
    assert result["verdict"] == "no-pin"


# ── The refusal message is the agent's only guidance ───────────────────────

def test_block_message_names_the_pin_registry_and_the_override(tmp_path):
    """Asserted on specifics, not on "some message exists" (guard-1082).

    This string is what a blocked agent sees INSTEAD of the goal it wanted, so
    it has to carry four things: which pin fired, what matched, where the row
    lives, and both ways out (claim in-lane work, or override with a written
    justification).
    """
    reason = _evaluate(PIN_AGENT,
                       _goal("Fix the retry logic in the framework scripts"),
                       tmp_path)["reason"]
    assert "pin-t01" in reason
    assert "framework scripts" in reason
    assert "conventions/capability-routing.md" in reason
    assert "Standing Lane Pins" in reason
    assert "--override-lane-pin" in reason
    # A pin is a USER directive: the selector's own ranking is not a way out,
    # and neither is re-probing whether the premise still holds.
    assert "score is NOT a justification" in reason


def test_registry_is_read_from_world_dir_when_no_text_is_supplied(tmp_path):
    """The production path: no registry_text, so the gate reads the file."""
    conv = tmp_path / "conventions"
    conv.mkdir()
    (conv / "capability-routing.md").write_text(_registry(), encoding="utf-8")
    result = lp.evaluate(PIN_AGENT,
                         _goal("Fix the retry logic in the framework scripts"),
                         world_dir=tmp_path, meta_dir=tmp_path)
    assert result["would_block"] is True
    assert result["pin_id"] == "pin-t01"
