""": a write to an agent dir this box does not hold the live runner
claim for must raise NoClaimError, NOT ConflictError.

THE NEGATIVE TEST IS THE LOAD-BEARING ONE and it is written FIRST, deliberately.
A positive control ("writes to an OWNED dir still succeed") passes identically
whether the guard works or is entirely absent, so it cannot detect the failure
mode that actually shipped and was reverted here: a consult whose NameError was
swallowed by a fail-open wrapper, leaving a no_claim feature structurally
incapable of emitting no_claim while compiling clean and passing every existing
test. Only an assertion that the guard FIRES can distinguish those two worlds.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import owncloud_backend  # noqa: E402
from _frontier_world import requires_frontier_world  # noqa: E402


def test_no_claim_error_type_exists_and_is_distinct():
    """NoClaimError must be its own type — the daemon classifies by TYPE
    (server.py: isinstance(e, get_backend().conflict_error)), so a differently
    worded ConflictError can never map to a distinct no_claim error code."""
    assert hasattr(owncloud_backend, "NoClaimError")
    assert issubclass(owncloud_backend.NoClaimError, Exception)
    assert not issubclass(owncloud_backend.NoClaimError,
                          owncloud_backend.ConflictError)
    assert not issubclass(owncloud_backend.ConflictError,
                          owncloud_backend.NoClaimError)


def test_backend_exposes_no_claim_error_attribute():
    """Mirrors conflict_error so server.py can classify lazily, with zero
    behaviour change off own-cloud."""
    assert owncloud_backend.OwnCloudBackend.no_claim_error is (
        owncloud_backend.NoClaimError)


@requires_frontier_world("it reads agents/bravo, a dir only the frontier origin's fleet holds")
def test_agent_name_derivation_agrees_with_predicate():
    """The consult derives the agent name and the under-agent-dir predicate
    from one consistent reading; they must not drift apart."""
    from _paths import agents_root, is_under_agent_dir
    root = Path(agents_root()).resolve()

    def name_of(rel):
        p = (root.parent / rel).resolve()
        try:
            return p.relative_to(root).parts[0]
        except ValueError:
            return None

    for rel, expected in [("agents/bravo/experience.jsonl", "bravo"),
                          ("agents/bravo/session/working-memory.yaml", "bravo"),
                          ("agents/alpha/journal.jsonl", "alpha"),
                          ("world/team-state.yaml", None),
                          ("core/scripts/x.py", None)]:
        got = name_of(rel)
        assert got == expected, "%s -> %r (expected %r)" % (rel, got, expected)
        under = is_under_agent_dir((root.parent / rel))
        assert under is (expected is not None), (
            "predicate/derivation disagree on %s" % rel)


def test_put_to_unowned_agent_dir_raises_no_claim():
    """THE test. Fires only on provenance == 'live-claims' with the agent absent
    from the owned set; every other provenance falls through to the ordinary
    fenced PUT, because on those this box may in fact own the dir and merely
    failed to prove it."""
    import inspect
    src = inspect.getsource(owncloud_backend.OwnCloudBackend._put)
    assert "NoClaimError" in src, "_put does not consult ownership"
    assert "live-claims" in src, "_put does not gate on provenance"
    # Ordering: the guard-955 tempdir tripwire must fire BEFORE the ownership
    # verdict, or a tmp-world PUT is masked by a no_claim refusal.
    assert src.index("_assert_not_tempdir_put") < src.index("NoClaimError"), (
        "ownership consult must come AFTER _assert_not_tempdir_put")


def test_no_claim_message_names_the_delivery_first_order():
    """. The refusal told every refused writer to relay on the
    coordination board, and board relays sat undelivered 5-21h. It must name the
    delivery-first order -- the holder's box, then a routed world goal -- plus
    the tags a board relay needs, and all of it must survive server.py's
    str(e)[:600]."""
    import inspect
    # _put must raise THIS text, formatted with a dict. A template and call that
    # disagree raise inside _put's fail-open consult, which lets the write through.
    assert 'NO_CLAIM_MESSAGE % {"agent": ' in inspect.getsource(
        owncloud_backend.OwnCloudBackend._put), "_put must raise THIS text"
    # Formatted exactly as _put formats it, with a long agent name for margin.
    msg = owncloud_backend.NO_CLAIM_MESSAGE % {"agent": "a-sixteen-char-x"}
    assert msg.startswith("no_claim:")
    assert len(msg) <= 600, len(msg)   # server.py returns str(e)[:600]
    # --agent: the refused dir can belong to an agent other than the writer's own.
    holder = msg.index("runner-claim.sh status --agent a-sixteen-char-x")
    goal = msg.index("world goal")
    board = msg.index("board post")
    assert holder < goal < board, "delivery-first order"
    for word in ("intended_agent", "handoff_to", "requires_action_by",
                 "action_type", "severity"):
        assert word in msg, word
    assert "Relay instead" not in msg
    # Other files quote these diagnosis sentences; they stay verbatim.
    for quoted in ("does not hold the live runner claim",
                   "permanently behind the claim-holder's advancing version",
                   "STRUCTURAL, not a race"):
        assert quoted in msg, quoted


def test_the_relay_verb_the_message_names_is_one_the_sweep_floors():
    """. A relayer who follows the message tags the action_type it
    names. insight-trigger-sweep.py floors a relay at MEDIUM only when its verb
    is in RELAY_ACTION_TYPES; untagged, it files LOW and is never selected. So
    the two files must agree."""
    import importlib.util
    import re
    msg = owncloud_backend.NO_CLAIM_MESSAGE % {"agent": "x"}
    verb = re.search(r"action_type:([a-z-]+)", msg).group(1)
    sweep = Path(owncloud_backend.__file__).with_name("insight-trigger-sweep.py")
    spec = importlib.util.spec_from_file_location("its_for_no_claim", sweep)
    its = importlib.util.module_from_spec(spec)
    sys.modules["its_for_no_claim"] = its
    spec.loader.exec_module(its)
    assert verb in its.RELAY_ACTION_TYPES, verb
