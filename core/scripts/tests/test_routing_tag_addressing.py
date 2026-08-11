""": the shared board-routing-tag addressing rule, and the consumer
that files goals from it.

`insight-trigger-sweep.py` REFUSES a bare `requires_action_by:<agent>` when the
name is in the collision set (declared by more than one deployment's roster) and
tells the poster to write `<agent>@<env-id>` instead — pinned by
test_insight_trigger_sweep_addressing.py. The NON-sweep consumers then tested
that tag with exact string equality, so the qualified form the sweep recommends
compared unequal and was dropped in SILENCE.

That is a pincer, and it is why this could not be fixed by qualifying the posts:
bare fails LOUDLY upstream, qualified fails SILENTLY downstream, so mass-
qualifying would have converted a visible failure into an invisible one while
looking exactly like a fix.

The rule (core/config/conventions/cross-deployment-channel.md "Addressing an
agent"), now implemented once in peer_surface.routing_tag_targets_agent:

  agent-part != me                 -> not mine
  no @ qualifier (bare)            -> mine, unchanged behavior
  @qualifier == my ENVIRONMENT_ID  -> mine
  @qualifier == another env-id     -> a PEER deployment's same-named agent
  ENVIRONMENT_ID unresolvable      -> fail OPEN (see the divergence pin below)

Per guard-2860 the carve-out pins ("qualified @self is admitted") are the LEAST
valuable here — they cannot fail in the dangerous direction. The load-bearing
pins are the EXCLUSIONS: a peer's same-named agent, and a prefix-sibling name.
Both are what a `split("@")[0]` shortcut would silently admit.

Run: python3 -m pytest core/scripts/tests/test_routing_tag_addressing.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import peer_surface as ps  # noqa: E402

SELF_ENV = "test-self-env"
PEER_ENV = "test-peer-env"


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the shared predicate ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tag", [
    "zeta",
    "requires_action_by:zeta",
])
def test_bare_forms_target_local_agent(tag):
    """The installed base. Measured 2026-08-06 over 9110 board records: 353 of
    360 routing tags are bare, so any regression here is the widest blast
    radius in this change."""
    assert ps.routing_tag_targets_agent(tag, "zeta", SELF_ENV) is True


@pytest.mark.parametrize("tag", [
    "zeta@" + SELF_ENV,
    "requires_action_by:zeta@" + SELF_ENV,
])
def test_self_env_qualified_targets_local_agent(tag):
    """THE DEFECT: the form the convention recommends, previously unequal to a
    bare name under exact string comparison and therefore silently dropped."""
    assert ps.routing_tag_targets_agent(tag, "zeta", SELF_ENV) is True


@pytest.mark.parametrize("tag", [
    "zeta@" + PEER_ENV,
    "requires_action_by:zeta@" + PEER_ENV,
])
def test_peer_env_qualified_is_not_local_agent(tag):
    """LOAD-BEARING (guard-2860). Same agent NAME, different deployment. A
    `split("@")[0] == me` shortcut passes every other pin in this file and
    fails this one — which is the entire reason the predicate compares the
    parsed (agent, env) pair component-wise rather than matching a pattern."""
    assert ps.routing_tag_targets_agent(tag, "zeta", SELF_ENV) is False


@pytest.mark.parametrize("tag", [
    "zetax@" + SELF_ENV,
    "requires_action_by:zetax",
    "requires_action_by:alpha@" + SELF_ENV,
])
def test_other_agents_are_not_local_agent(tag):
    """LOAD-BEARING. `zetax` merely shares a prefix with `zeta`; a startswith
    or prefix test admits it."""
    assert ps.routing_tag_targets_agent(tag, "zeta", SELF_ENV) is False


def test_live_corpus_shape_still_refused():
    """All 7 qualified tags on the live board (2026-08-06) read
    `omni@zds-mind`. This pins that the change is behaviour-PRESERVING on the
    real corpus: the newly-admitted set on today's data is empty."""
    assert ps.routing_tag_targets_agent(
        "requires_action_by:omni@zds-mind", "zeta", "ayoai-mind") is False


@pytest.mark.parametrize("tag", [
    "requires_action_by:zeta@" + SELF_ENV,
    "requires_action_by:zeta@" + PEER_ENV,
])
def test_unresolvable_self_env_fails_open(tag):
    """DELIBERATE DIVERGENCE from insight-trigger-sweep.py, which REFUSES an
    explicit @env target when ENVIRONMENT_ID is unresolvable (guard-1562 — name
    what each direction newly admits or refuses).

    The sweep FILES A GOAL, so a wrong route there is a durable cross-deployment
    mutation and refusing is recoverable because the refusal names the post.
    These consumers print an advisory / decide whether to surface work to their
    OWN agent: a false positive is one dismissible line, a false negative is the
    silent lane-skip of guard-1310. Do not "align" the two postures — this pin
    exists so a future consistency pass has to read that reasoning first."""
    assert ps.routing_tag_targets_agent(tag, "zeta", None) is True


@pytest.mark.parametrize("empty", ["", None])
def test_falsy_self_env_fails_open_not_just_none(empty):
    """The fail-open branch tests FALSY, not `is None`.

    Found by the fresh-eyes pass on this file's own first cut, which used
    `if self_env is None`. Both live callers pass _paths.ENVIRONMENT_ID, whose
    trailing `or None` normalizes falsy to None -- so the gap was LATENT and
    `is None` was correct for them. But a caller writing the natural
    `os.environ.get("ENVIRONMENT_ID", "")` hands us "" and, under `is None`,
    every qualified tag compares unequal and is dropped: fail-CLOSED, which is
    this function's documented failure direction inverted, and silent."""
    assert ps.routing_tag_targets_agent(
        "requires_action_by:zeta@" + SELF_ENV, "zeta", empty) is True


def test_malformed_tags_do_not_raise():
    """Tags come from free-form board JSON -- a non-string or truncated tag must
    return a verdict, never propagate an exception into the caller's loop."""
    for bad in [None, 123, "", "@", "zeta@", "@" + SELF_ENV, "requires_action_by:"]:
        assert ps.routing_tag_targets_agent(bad, "zeta", SELF_ENV) in (True, False)
    # `zeta@` (trailing @, empty qualifier) reads as BARE: split_author maps an
    # empty env to None, so it takes the unchanged-behavior branch.
    assert ps.routing_tag_targets_agent("zeta@", "zeta", SELF_ENV) is True


def test_parse_splits_on_first_at_only():
    """Every registry env-id contains a hyphen, so `@` is the separator and a
    hyphen-joined form could not be split back unambiguously."""
    assert ps.parse_routing_tag("requires_action_by:zeta@a-b") == ("zeta", "a-b")
    assert ps.parse_routing_tag("zeta") == ("zeta", None)


# ── consumer wiring: insight-trigger-gate ────────────────────────────────────
# A green predicate certifies the FUNCTION, never the WIRING (guard-1943), so
# these drive the gate's real _collect_triggers. This is the highest-severity
# consumer in the family: it is what FILES the Investigate goal on an
# `invalidates` finding, so a dropped trigger means a partner's "your assumption
# is dead" never reaches the agent at all.

def _finding(msg_id, requires_by):
    return {
        "id": msg_id, "author": "alpha",
        "tags": ["insight_trigger", "severity:invalidates",
                 "requires_action_by:" + requires_by, "affects:g-115-9999"],
        "text": "the assumption behind g-115-9999 is dead",
    }


@pytest.fixture
def gate(monkeypatch):
    mod = _load("itg_routing_under_test", "insight-trigger-gate.py")
    # Isolate from this box's real insight-actions.jsonl.
    monkeypatch.setattr(mod, "_already_processed", lambda _id: False)
    monkeypatch.setattr(mod, "ENVIRONMENT_ID", SELF_ENV)
    return mod


@pytest.mark.parametrize("requires_by,acts", [
    ("zeta",               True),   # bare -> unchanged
    ("zeta@" + SELF_ENV,   True),   # THE FIX
    ("zeta@" + PEER_ENV,   False),  # LOAD-BEARING: peer deployment's zeta
    ("zetax@" + SELF_ENV,  False),  # LOAD-BEARING: prefix sibling
    ("alpha",              False),  # unrelated agent
])
def test_gate_routes_both_addressing_forms(gate, requires_by, acts):
    got = gate._collect_triggers([_finding("msg-" + requires_by, requires_by)],
                                 "zeta")
    assert (len(got) == 1) is acts


def test_gate_still_skips_findings_with_no_routing_tag(gate):
    """UNTOUCHED CLAUSE (guard-1807). The absent-tag skip is a real policy —
    with no `requires_action_by`, both agents see it and neither is required to
    act. The excluded class this change admits failed the EQUALITY clause, not
    this one, so deleting a clause was never the right edit."""
    rec = {"id": "msg-none", "author": "alpha",
           "tags": ["insight_trigger", "severity:invalidates",
                    "affects:g-115-9999"],
           "text": "no routing tag"}
    assert gate._collect_triggers([rec], "zeta") == []
