"""test_goal_selector_verdict_banners.py — .

Pins `write_scorer_verdict_banners`: the SECOND, additive write that records both
banner emitters' returns onto `agents/<agent>/session/scorer-verdict.json`, so the
banners survive loss of stderr.

WHY A SIDECAR AT ALL. Both emitters report ONLY to stderr. g-115-4286 measured
that the original premise — "a loop caller discards stderr" — is FALSE: all 9
loop-path callers invoke `goal-selector.sh` bare, and the 10 discarding callers
are verify-learning / checklist TEST contexts. So the real failure mode is ad-hoc
invocation carrying a hand-typed redirect, which NO call-site edit can reach. A
durable sidecar is the only remedy that survives it, which is what makes the
feature worth having.

WHY A SECOND WRITER RATHER THAN A REORDER. `write_scorer_verdict` runs BEFORE both
emitters deliberately ("so it can never disturb the pinned
emit_directive_honor_banner call site", g-115-2807), and the banners do not exist
yet at that point. Reordering would move the pinned call site; appending does not.

THE TWO EMITTERS RETURN DIFFERENT SHAPES, preserved rather than normalised:
`emit_directive_honor_banner` returns structured records
({directive_id, goal_id, rank}) and PRINTS its prose without returning it, while
`emit_strategic_focus_banner` returns its full banner TEXT. Reshaping the former
is not available — `test_goal_selector_directive_honor_banner.py` asserts on
`warns[0]["directive_id"]/["goal_id"]/["rank"]` and on `== []`, so changing that
return would break the very pin this placement protects.

guard-1220: the two BOUNDARY tests below read their expectation from the EMITTERS
at runtime and never from a hardcoded string. That rule's 2026-07-31 discriminator
is satisfied — the emitters are a different component from the writer under test,
and this change does not touch them, so a regression in the writer cannot shrink
the expectation along with the result.

guard-2545 is the invariant most at risk from this change and is pinned last; read
that test's docstring for the honest limit on what a static check proves.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Distinct aliases so this file's monkeypatching cannot leak into the other
# goal-selector test modules through a shared module object.
gs = _load("goal_selector_vb", "goal-selector.py")
svg = _load("scorer_verdict_gate_vb", "scorer-verdict-gate.py")


@pytest.fixture(autouse=True)
def _resident(monkeypatch):
    """Force the cross-agent residency guard TRUE.

    Load-bearing against a VACUOUS PASS, not a convenience. `write_scorer_verdict_banners`
    returns early when the running agent is not resident on this box (g-115-5850),
    and every "no-op" assertion below would then pass for the wrong reason on any
    box where MIND_AGENT is not resident. Pinning it makes the positive tests the
    control: if the guard ever short-circuits, they fail loudly instead.
    """
    monkeypatch.setattr(gs, "_agent_is_resident", lambda: True)
    gs._STRATEGIC_FOCUS = None
    gs._TEAM_STATE_CACHE = None
    yield
    gs._STRATEGIC_FOCUS = None
    gs._TEAM_STATE_CACHE = None


def _seed_verdict(tmp_path):
    """Write a real primary verdict through the real writer, then return its path."""
    gs.write_scorer_verdict(
        [{"goal_id": "g-1", "score": 9.9}, {"goal_id": "g-2", "score": 4.4}], tmp_path)
    target = tmp_path / "session" / "scorer-verdict.json"
    assert target.exists(), "primary writer did not produce a sidecar to append to"
    return target


def _banners(directive=None, strategic=None, errors=None):
    return {
        "directive_honor": directive if directive is not None else [],
        "strategic_focus": strategic if strategic is not None else [],
        "errors": errors if errors is not None else [],
    }


# ── additive contract ────────────────────────────────────────────────────

def test_banners_appended_without_disturbing_the_gate_keys(tmp_path):
    """The append must be purely ADDITIVE: every key the claim gate reads survives
    byte-identically. Compared against the pre-append snapshot rather than a
    restated literal, so a schema change to the primary writer cannot make this
    assertion silently stop covering the real keys."""
    target = _seed_verdict(tmp_path)
    before = json.loads(target.read_text(encoding="utf-8"))

    gs.write_scorer_verdict_banners(_banners(strategic=["⚠ STRATEGIC-FOCUS: ..."]), tmp_path)

    after = json.loads(target.read_text(encoding="utf-8"))
    assert "banners" in after
    for key, value in before.items():
        assert after[key] == value, f"append disturbed pre-existing key {key!r}"
    assert set(after) == set(before) | {"banners"}


def test_banners_key_written_even_when_every_list_is_empty(tmp_path):
    """THE LOAD-BEARING CASE. An ABSENT `banners` key means the sidecar predates
    this feature or the second write failed; a PRESENT key holding empty lists
    means the emitters ran and had nothing to say. Collapsing those two states
    would leave the sidecar unable to answer the question it was added to answer,
    so "no banners fired" must still produce the key."""
    target = _seed_verdict(tmp_path)
    gs.write_scorer_verdict_banners(_banners(), tmp_path)

    data = json.loads(target.read_text(encoding="utf-8"))
    assert "banners" in data
    assert data["banners"]["directive_honor"] == []
    assert data["banners"]["strategic_focus"] == []


def test_emitter_exception_is_recorded_not_silently_empty(tmp_path):
    """An emitter that RAISED reports only to stderr — the very channel this
    sidecar backstops — so without `errors` an exception would be
    indistinguishable from "ran and had nothing to say"."""
    target = _seed_verdict(tmp_path)
    gs.write_scorer_verdict_banners(
        _banners(errors=["strategic_focus: ValueError: boom"]), tmp_path)

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["banners"]["errors"] == ["strategic_focus: ValueError: boom"]
    assert data["banners"]["strategic_focus"] == []


# ── boundary: the writer persists exactly what the emitters return (guard-1220) ──

def test_directive_honor_records_persisted_match_the_emitter_return(tmp_path):
    """BOUNDARY TEST. The expectation is READ FROM THE EMITTER at runtime, never
    restated — a test that supplied its own expected dict would be self-consistent
    within the writer and could not fail on a producer/consumer mismatch, which is
    the whole bug class (guard-1220)."""
    board = tmp_path / "coordination.jsonl"
    board.write_text(json.dumps({
        "id": "msg-dir-1", "author": "alpha", "type": "directive",
        "channel": "coordination",
        "tags": ["directive", "target:g-315-390", "zeta", "user-directive"],
        "text": "USER DIRECTIVE: zeta -- prioritize g-315-390.",
    }) + "\n", encoding="utf-8")

    scored = [{"goal_id": "g-315-390", "score": 10.0}, {"goal_id": "g-999-1", "score": 9.0}]
    expected = gs.emit_directive_honor_banner(scored, "zeta", board_path=board)
    assert expected, "fixture did not make the emitter fire — the test would be vacuous"

    target = _seed_verdict(tmp_path)
    gs.write_scorer_verdict_banners(_banners(directive=expected), tmp_path)

    stored = json.loads(target.read_text(encoding="utf-8"))["banners"]["directive_honor"]
    assert stored == expected
    # The structured record IS the actionable content the prose renders.
    assert stored[0]["directive_id"] == "msg-dir-1"
    assert stored[0]["goal_id"] == "g-315-390"


def test_strategic_focus_text_persisted_matches_the_emitter_return(tmp_path, monkeypatch):
    """BOUNDARY TEST, the text-shaped half. Same guard-1220 discipline: the banner
    string is obtained from the emitter and round-tripped, never retyped — retyping
    it would also silently pin the emitter's wording, which is not this test's
    subject."""
    monkeypatch.setattr(
        gs, "_load_team_state_cached",
        lambda: {"strategic_focus": {"primary": "Product completeness: asp-999 lane."}})
    gs._STRATEGIC_FOCUS = None

    scored = [
        {"goal_id": "g-sweep-1", "score": 9.0, "recurring": True,
         "aspiration_id": "asp-001", "title": "Recurring infra sweep"},
        {"goal_id": "g-lane-1", "score": 7.0, "recurring": False,
         "aspiration_id": "asp-999", "title": "Lane product goal"},
    ]
    expected = gs.emit_strategic_focus_banner(scored, "alpha")
    assert expected, "fixture did not make the emitter fire — the test would be vacuous"

    target = _seed_verdict(tmp_path)
    gs.write_scorer_verdict_banners(_banners(strategic=expected), tmp_path)

    stored = json.loads(target.read_text(encoding="utf-8"))["banners"]["strategic_focus"]
    assert stored == expected
    assert "STRATEGIC-FOCUS" in stored[0]


# ── consumer tolerance ───────────────────────────────────────────────────

def test_gate_still_evaluates_a_verdict_carrying_the_banners_key(tmp_path):
    """The claim chokepoint reads only top_goal_id + ts. Verified against the REAL
    gate rather than by asserting the key is 'additive' in the abstract: the gate
    must still refuse a non-top claim and allow the top one with `banners` present."""
    target = _seed_verdict(tmp_path)
    gs.write_scorer_verdict_banners(_banners(strategic=["⚠ STRATEGIC-FOCUS: ..."]), tmp_path)

    verdict = json.loads(target.read_text(encoding="utf-8"))
    assert "banners" in verdict  # positive control: the key really is present
    assert svg.evaluate(verdict, "g-2", "", datetime.now())[0] == 2
    assert svg.evaluate(verdict, "g-1", "", datetime.now())[0] == 0


# ── refusal / fail-open branches ─────────────────────────────────────────

def test_missing_sidecar_is_a_noop_and_creates_nothing(tmp_path):
    """When the primary writer no-op'd (no candidates) there is nothing to append
    to. The append must NOT fabricate a sidecar lacking top_goal_id — that file is
    the claim gate's input."""
    (tmp_path / "session").mkdir()
    gs.write_scorer_verdict_banners(_banners(strategic=["x"]), tmp_path)
    assert not (tmp_path / "session" / "scorer-verdict.json").exists()


def test_non_dict_sidecar_is_left_untouched(tmp_path):
    """A sidecar whose shape we do not recognise is never overwritten."""
    session = tmp_path / "session"
    session.mkdir()
    target = session / "scorer-verdict.json"
    target.write_text('["not", "a", "dict"]', encoding="utf-8")

    gs.write_scorer_verdict_banners(_banners(strategic=["x"]), tmp_path)
    assert json.loads(target.read_text(encoding="utf-8")) == ["not", "a", "dict"]


def test_unparseable_sidecar_fails_open_without_raising(tmp_path):
    """FAIL-OPEN is the safety property: a banner-write failure must never block
    selection output."""
    session = tmp_path / "session"
    session.mkdir()
    (session / "scorer-verdict.json").write_text("{not json", encoding="utf-8")
    gs.write_scorer_verdict_banners(_banners(strategic=["x"]), tmp_path)  # must not raise


def test_none_agent_dir_and_empty_mapping_are_noops():
    """Both guards return before any filesystem touch and never raise."""
    gs.write_scorer_verdict_banners(_banners(strategic=["x"]), None)
    gs.write_scorer_verdict_banners({}, Path("/nonexistent"))


# ── guard-2545 invariant ─────────────────────────────────────────────────

def test_cmd_blocked_contains_no_sidecar_write(tmp_path):
    """guard-2545: `goal-selector.sh blocked` is a PURE READ — it mutates neither
    the drain-lane state nor the scorer-verdict sidecar, MEASURED byte- and
    mtime-identical, and a prescribed cross-check ritual depends on that. Adding a
    second sidecar writer is exactly the change that could falsify it, so the
    invariant is pinned here rather than left to the guardrail's prose.

    HONEST LIMIT: this is a STATIC check on cmd_blocked's own source. It catches
    the realistic regression — someone wiring either writer into the blocked path —
    but it cannot see a write reached through an indirect callee, and it is not a
    substitute for guard-2545's live before/after stat.
    """
    src = inspect.getsource(gs.cmd_blocked)
    assert "write_scorer_verdict" not in src
    assert "write_scorer_verdict_banners" not in src


def test_banner_writer_is_called_from_cmd_select(tmp_path):
    """The complement of the test above: the writer must actually be WIRED, and
    wired AFTER both emitters. A writer nobody calls is indistinguishable from one
    that always succeeds — the exact defect class this file exists to pin."""
    src = inspect.getsource(gs.cmd_select)
    assert "write_scorer_verdict_banners(banners, AGENT_DIR)" in src
    assert src.index("emit_directive_honor_banner(scored") < src.index(
        "write_scorer_verdict_banners(banners")
    assert src.index("emit_strategic_focus_banner(scored") < src.index(
        "write_scorer_verdict_banners(banners")
