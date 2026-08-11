"""Tests for the shared conflict-injection seam (_conflict_fixture.py, ).

This file is the shared fixture's OWN mutation proof. The seam's whole purpose
is that a degraded injection fails RED instead of passing on the single-pass
branch, so a green here that was never capable of going red would defeat the
extraction entirely -- the #21 shape in the test-coverage-illusions node,
reached through the fixture rather than the code under test.

Each positive test below is paired with a control that removes exactly one
thing and asserts the failure. Note in particular
``test_namespace_patch_survives_a_sibling_reload``: the two namespaces
``fileops_mod()`` and ``retry_globals()`` are THE SAME DICT in a fresh
interpreter, so a control for that half run without reproducing the reload
cannot fail no matter how broken the patching is.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = CORE_SCRIPTS.parents[1]
for _p in (str(CORE_SCRIPTS), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _conflict_fixture as CF  # noqa: E402


class _Conflict(Exception):
    """Stands in for owncloud_backend.ConflictError (a bare Exception subclass)."""


class _Backend:
    conflict_error = _Conflict


def _retry_fn():
    """The function object the production path actually calls -- the one bound at
    module level in file_locks, whose __globals__ retry_globals() targets."""
    from mind_api.src import file_locks

    return file_locks._rmw_with_conflict_retry


def _cycle_raising_once(attempts):
    """A cycle that loses the fence on attempt 1 and commits on attempt 2."""

    def _cycle():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise _Conflict("If-Match fence lost to a concurrent writer")
        return "committed"

    return _cycle


# --- the seam works ---------------------------------------------------------


def test_patch_conflict_backend_makes_the_real_wrapper_reinvoke(monkeypatch):
    """The REAL _rmw_with_conflict_retry re-enters the cycle after an injected
    conflict. Drives the production function, not a hand-written double loop
    (guard-1829)."""
    CF.patch_conflict_backend(monkeypatch, _Backend())
    attempts = []

    result = _retry_fn()(Path("unused.yaml"), _cycle_raising_once(attempts))

    assert result == "committed"
    CF.assert_reinvoked(attempts)


def test_without_the_patch_the_conflict_escapes(monkeypatch):
    """CONTROL for the test above. With no injection the backend is the real
    LocalBackend whose conflict_error is the empty tuple, so `except ()` matches
    nothing and the raised _Conflict propagates instead of being retried.

    This is the single-pass branch. It is what every degraded fixture silently
    exercises, and asserting it here is what proves the positive test above is
    measuring the retry rather than an unconditional pass."""
    attempts = []

    with pytest.raises(_Conflict):
        _retry_fn()(Path("unused.yaml"), _cycle_raising_once(attempts))

    assert len(attempts) == 1, "expected exactly one pass with no injection"


# --- the namespace half, which needs the reload to be provable --------------


def test_retry_globals_targets_the_namespace_the_wrapper_actually_reads(monkeypatch):
    """``retry_globals()`` must name the dict ``_rmw_with_conflict_retry`` reads
    for ``get_backend``, whatever the process's reload history.

    Proven BEHAVIOURALLY -- patch only that namespace and confirm the real
    wrapper picks the injection up -- rather than by comparing dict identities.
    An earlier version asserted ``fileops_mod().__dict__ is retry_globals()``
    under the name "...before any reload". That is true in a fresh interpreter
    and FALSE once any sibling suite has already reloaded ``_fileops`` earlier
    in the same process: the name stated a precondition that nothing enforced,
    so the test passed alone and failed in a full run (measured g-306-178,
    alongside test_atomic_write_fallback.py). Order-dependence in a test that
    exists to document an order-dependent hazard is its own small irony; the
    fix is to assert the invariant that holds in BOTH states.

    The vacuity warning that assertion used to carry still stands and is why
    the next test reloads deliberately: in one fresh process the two namespaces
    ARE the same dict, so a control for the namespace split that does not
    reproduce a reload cannot fail.
    """
    monkeypatch.setitem(CF.retry_globals(), "get_backend", lambda: _Backend())
    attempts = []

    _retry_fn()(Path("unused.yaml"), _cycle_raising_once(attempts))

    CF.assert_reinvoked(attempts)


def test_namespace_patch_survives_a_sibling_reload(monkeypatch):
    """MUTATION PROOF for the hard half of this seam.

    Reproduces what sibling suites do (`del sys.modules["_fileops"]` +
    re-import), after which the module dict importlib returns is no longer the
    dict the retry function reads. Asserts BOTH directions: patching only the
    import_module namespace leaves the retry blind (the defect), while
    patch_conflict_backend covers it (the fix)."""
    original = sys.modules["_fileops"]
    try:
        del sys.modules["_fileops"]
        reloaded = importlib.import_module("_fileops")

        # The divergence itself -- without this the rest of the test is vacuous.
        assert reloaded.__dict__ is not CF.retry_globals(), (
            "expected the reload to split the namespaces; if this fails the "
            "control below cannot discriminate and this test proves nothing"
        )

        # THE DEFECT: patch only the import_module namespace, as three call
        # sites did before they each discovered retry_globals independently.
        monkeypatch.setattr(reloaded, "get_backend", lambda: _Backend())
        blind = CF.retry_globals()["get_backend"]().conflict_error
        assert blind is not _Conflict, (
            "the naive patch reached the retry's namespace -- the reload did "
            "not produce the split this test exists to reproduce"
        )

        # THE FIX: the shared seam patches the namespace the retry reads.
        CF.patch_conflict_backend(monkeypatch, _Backend())
        assert CF.retry_globals()["get_backend"]().conflict_error is _Conflict

        attempts = []
        _retry_fn()(Path("unused.yaml"), _cycle_raising_once(attempts))
        CF.assert_reinvoked(attempts)
    finally:
        sys.modules["_fileops"] = original


# --- the self-check itself ---------------------------------------------------


def test_assert_reinvoked_fails_on_a_single_pass():
    """The self-check must go RED on the exact symptom every degraded fixture
    produces. Without this, assert_reinvoked could be a no-op and nothing here
    would notice."""
    with pytest.raises(AssertionError, match="conflict injection failed"):
        CF.assert_reinvoked([1])


def test_assert_reinvoked_accepts_an_int_count():
    CF.assert_reinvoked(2)
    with pytest.raises(AssertionError):
        CF.assert_reinvoked(1)
