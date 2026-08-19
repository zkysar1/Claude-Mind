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


# ── suspected_routing_tags: the write-side advisory () ──────────────
#
# MEASURED POPULATION (2026-08-11, four agents over ten days): six board posts
# carried a tag that LOOKS like an address and routes to NOBODY --
# `relay-to-omni` x3, `forward-to:omni@zds-mind` x2, `forward-to:omni` x1. Two
# of them were time-critical user relays to a peer deployment. Board tags are
# free-form, so the poster got no feedback of any kind.
#
# Every tag list below is VERBATIM from a real board post; none is synthetic.

def test_stranded_hyphen_form_is_flagged():
    """`relay-to-omni` parses to agent `relay-to-omni` and contains NO colon,
    so the colon rule alone cannot see it. 3 of the 6 measured posts."""
    got = ps.suspected_routing_tags(
        ['relay-to-omni', 'cross-deployment', 'user-directive', 'time-critical'])
    assert got == [('relay-to-omni', 'omni')]


def test_stranded_prefix_form_is_flagged():
    """A colon SURVIVING parse_routing_tag is proof of an unrecognised prefix."""
    assert ps.suspected_routing_tags(['forward-to:omni@zds-mind']) == [
        ('forward-to:omni@zds-mind', 'omni')]
    assert ps.suspected_routing_tags(['forward-to:omni']) == [
        ('forward-to:omni', 'omni')]


def test_a_rescuing_tag_on_the_same_post_suppresses_the_warning():
    """THE false-positive guard, and the reason this is not a per-tag check.

    VERBATIM from msg-20260814-163801-bravo-5214 — a live post answering a
    peer's URGENT outage question. It carries the broken `to:omni@zds-mind`
    AND a bare `omni`. Consumers test tags with any(), so it routed and was
    delivered; warning here would raise a false alarm on a working message.
    """
    tags = ['omni', 'zds-mind', 'lodestar-outage', 'aws-account', 'urgent',
            'to:omni@zds-mind']
    assert ps.routing_tag_targets_agent('omni', 'omni', 'ayoai-mind') is True
    assert ps.suspected_routing_tags(tags) == []


def test_same_broken_tag_alone_is_flagged():
    """Discriminator for the test above: it is the RESCUING tag that suppresses,
    not the broken tag being tolerated."""
    assert ps.suspected_routing_tags(['to:omni@zds-mind', 'urgent']) == [
        ('to:omni@zds-mind', 'omni')]


def test_rescue_suppression_is_case_insensitive_on_both_sides():
    """The FLAG path case-normalises its half and the SILENCE path did not.

    Found by the Step-0.35 adversarial-review protocol during g-115-6347's own
    reflection, asking whether the release predicate is a superset of the
    acquire predicate. It was not: the verb is matched `.strip().lower()`, so
    `FOR:zeta` is recognised as addressing, but the rescue check compared the
    suspect value against `routed` verbatim — so `for:Zeta` + a bare `zeta`
    that DOES route still warned. guard-1942: both sides of a join on an
    identity key must draw from the same namespace.

    Latent, not live (0 occurrences across 9,917 posts — agent names are
    lowercase kebab-case by convention), which is exactly why it needs a test
    rather than a measurement: nothing in the corpus would ever fail.
    """
    # Mixed case on the SUSPECT side.
    assert ps.suspected_routing_tags(['for:Zeta', 'zeta']) == []
    # Mixed case on the ROUTED side.
    assert ps.suspected_routing_tags(['for:zeta', 'Zeta']) == []
    # Mixed case on BOTH, differing from each other.
    assert ps.suspected_routing_tags(['FOR:Zeta', 'zEtA']) == []

    # POSITIVE CONTROL — the suppression must still be an IDENTITY match, not a
    # blanket silence. A rescuing tag naming a DIFFERENT agent rescues nothing,
    # and the returned `who` keeps the tag's original case because callers print
    # it verbatim.
    assert ps.suspected_routing_tags(['for:Zeta', 'alpha']) == [
        ('for:Zeta', 'Zeta')]
    assert ps.suspected_routing_tags(['for:zeta', 'alpha']) == [
        ('for:zeta', 'zeta')]


def test_provenance_prefix_is_not_an_addressing_attempt():
    """VERBATIM from msg-20260813-125258-omni-5857. `from:` marks who SENT a
    peer post; it is emitted on every omni outbound. Warning on it would be a
    guaranteed false positive on all peer traffic, which is how an advisory
    earns being ignored."""
    assert ps.suspected_routing_tags(
        ['brand-and-domains', 'freshness', 'from:omni@zds-mind']) == []


def test_correct_forms_and_ordinary_tags_are_silent():
    assert ps.suspected_routing_tags(
        ['requires_action_by:omni@zds-mind', 'relay', 'time-critical']) == []
    assert ps.suspected_routing_tags(['requires_action_by:zeta']) == []
    assert ps.suspected_routing_tags(['claim', 'g-335-1233', 'echo']) == []
    assert ps.suspected_routing_tags([]) == []
    assert ps.suspected_routing_tags(None) == []


def test_no_roster_is_consulted():
    """Deliberate: a peer agent this world does not enumerate is the case MOST
    likely to be mis-addressed, so a roster would weaken the check exactly
    where it matters. An unknown name must still flag."""
    assert ps.suspected_routing_tags(['forward-to:nobody-has-ever-heard-of-me']) == [
        ('forward-to:nobody-has-ever-heard-of-me', 'nobody-has-ever-heard-of-me')]


# ── the prefix, not the value, is the discriminator () ────────────
#
# MEASURED 2026-08-16 (zeta, hostname cc-02, uname -r 6.8.0-137-generic) over
# the full live corpus: 9,898 posts on findings+coordination across 720h. The
# prior predicate warned on ANY unrecognised colon prefix and fired on 1,860
# posts / 3,710 tag instances, of which 3,575 were clear false positives —
# 2.8% precision. After: 70 posts / 104 instances, ALL genuine. Recall of the
# 104 real mis-addresses is unchanged; only the noise is gone.
#
# Every tag list below is VERBATIM from a real board post; none is synthetic.

def test_structured_metadata_is_not_an_addressing_attempt():
    """VERBATIM from msg-20260815-175520-foxtrot-5395, a CORRECTLY-addressed
    post (it carries `requires_action_by:alpha`). The old predicate warned on it
    three times and proposed `requires_action_by:g-326-292` — routing a post to
    a GOAL ID, advice that converts a working `affects:` tag into a dead one
    (guard-3982). `severity:` and `affects:` are consumed vocabulary:
    insight-trigger-sweep.py matches `^affects:(g-\\d+-\\d+)$` and
    aspirations-select Phase 2.07 parses both by name.
    """
    tags = ['severity:enables', 'affects:g-326-292', 'affects:g-326-205',
            'requires_action_by:alpha']
    assert ps.suspected_routing_tags(tags) == []


def test_metadata_prefixes_stay_silent_without_any_rescuing_tag():
    """Discriminator for the test above: these are silent because the PREFIX is
    not an address verb, NOT because another tag rescued the post. Drop the
    `requires_action_by:alpha` and they must still be silent — otherwise the
    fix would only be masking via the suppression clause."""
    assert ps.suspected_routing_tags(
        ['severity:enables', 'affects:g-326-292', 'affects:g-326-205']) == []
    # The long tail, all observed live: 44 distinct metadata prefixes appeared
    # in the corpus. An allowlist of the top four still left 222 false firings,
    # which is why the closed ADDRESS-VERB set is enumerated instead.
    for tag in ('target:g-326-174', 'action_type:investigate',
                'probe_dimension:platform', 'finding_id:zeta-fec-x',
                'lane:either', 'target_status:pending', 'finding_kind:bug',
                'severity:constrains', 'box:cc-02'):
        assert ps.suspected_routing_tags([tag]) == [], tag


def test_for_prefix_is_the_largest_real_mis_address():
    """`for:<agent>` routes to nobody and is the single most common genuine
    mis-address in the corpus — 71 instances across five agents, MORE than every
    previously-known form combined (`to:` 13, `forward-to:` 13, `relay-to-` 5,
    `fyi:` 1). It had never been reported because it sat under 3,575 false
    firings, so this test also pins WHY the noise mattered: an advisory that
    noisy conceals its own true positives.

    This is the case that rules out a closed-verb set copied from
    `_HYPHEN_ROUTING_RE` (which has no `for`) — that fix would have silenced the
    biggest real defect while looking like a cleanup.
    """
    assert ps.suspected_routing_tags(['for:bravo']) == [('for:bravo', 'bravo')]
    assert ps.suspected_routing_tags(['for:omni@zds-mind']) == [
        ('for:omni@zds-mind', 'omni')]
    assert ps.suspected_routing_tags(['notify:zeta']) == [('notify:zeta', 'zeta')]


def test_broadcast_and_mixed_case_addresses_still_flag():
    """VERBATIM live tags. `all@<env>` is a real broadcast address form, and one
    poster wrote `Omni` capitalised — both are addressing attempts that route to
    nobody, so neither may be lost to the prefix gate."""
    assert ps.suspected_routing_tags(['forward-to:all@zds-mind']) == [
        ('forward-to:all@zds-mind', 'all')]
    assert ps.suspected_routing_tags(['forward-to:Omni@zds-mind']) == [
        ('forward-to:Omni@zds-mind', 'Omni')]


def test_routed_flags_but_attributive_agent_nouns_stay_silent():
    """The recall boundary of the prefix gate, drawn from live evidence.

    VERBATIM live tags. Of the 3,606 instances the narrowing drops, 27 carry a
    value naming a live agent and 21 of those sit on a post with no properly
    routing tag — so the question "did I silence a real mis-address?" has a
    concrete answer rather than an intuition.

    `routed:` FLAGS. Both live instances are `insight_trigger` posts carrying no
    `requires_action_by:`, so aspirations-select Phase 2.07 SKIPped them and the
    triggers reached nobody. It is also the past participle of `route`, already
    in the set.

    Everything else in that population STAYS SILENT, and this half is the one
    that matters: `agent:`, `lane:`, `credit:` and `cross-agent:` are NOUNS used
    attributively — a finding ABOUT zeta, a lane emitted mechanically by
    blocked-signal-resolution-check, a credit line. Admitting them because their
    values happen to look like agent names would re-open the metadata set this
    design closes, and would re-introduce the value-identity predicate that
    `test_no_roster_is_consulted` deliberately rejects.
    """
    assert ps.suspected_routing_tags(['routed:alpha']) == [('routed:alpha', 'alpha')]
    assert ps.suspected_routing_tags(['routed:bravo']) == [('routed:bravo', 'bravo')]

    for attributive in ('agent:zeta', 'agent:alpha', 'lane:alpha',
                        'lane:foxtrot', 'credit:foxtrot', 'cross-agent:echo'):
        assert ps.suspected_routing_tags([attributive]) == [], attributive

    # The whole live tag list of msg-20260812-175336-zeta-5507, a mechanically
    # emitted status post: silent as a unit, not merely tag-by-tag.
    assert ps.suspected_routing_tags(
        ['blocked-signal-routed', 'g-335-1158', 'lane:alpha']) == []
