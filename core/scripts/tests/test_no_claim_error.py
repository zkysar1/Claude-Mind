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
