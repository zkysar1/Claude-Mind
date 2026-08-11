"""Shared conflict-injection seam for tests that drive ``_rmw_with_conflict_retry``.

WHY THIS EXISTS. ``LocalBackend.conflict_error`` is the EMPTY TUPLE, so
``except ()`` catches nothing and ``_fileops._rmw_with_conflict_retry``
degenerates to a single transparent pass in which the modifier runs EXACTLY
ONCE. guard-955 pins ``STORAGE_BACKEND=local`` for every test run on an
own-cloud box. So the only thing standing between the whole
re-invocation defect class (g-306-163, g-306-164, g-306-166, guard-2544) and
total invisibility is that each test correctly injects a conflict. A test whose
injection silently stops working does NOT go red -- it goes GREEN, having
exercised the single-pass branch, which is the one branch the defect cannot
live in.

WHAT IS ACTUALLY HARD HERE, and it is not the fake backend. The fake is three
lines and legitimately differs per test (some need ``read_text``, some
``exists``, some only ``conflict_error``). The hard part is WHICH NAMESPACE to
patch. Three call sites independently discovered the same trap and each wrote
its own enumeration of it; this module is that enumeration, once. See
``patch_conflict_backend``.

WHAT THIS FIXTURE HOLDS CONSTANT -- read this before assuming it covers you
(guard-1778). It pins the SET of namespaces patched. A consumer that resolves
``get_backend`` from a namespace outside that set is silently NOT patched, and
no amount of green here reveals it. That is exactly the failure mode a shared
fixture invites: seven independent chances to lose coverage become one shared
chance to lose it for everybody at once. The mitigation is
``assert_reinvoked`` -- the centralized self-check that turns a missed
namespace into a RED instead of a pass on the single-pass branch. If you add a
consumer, pass its module in ``extra`` and keep the assertion; do not drop the
assertion to make a stubborn test pass.

Extracted by g-306-178 from the copies in test_experience_conflict_retry.py,
test_meta_write_class_conflict_retry.py, test_meta_yaml_conflict_retry.py and
test_retrieve_bump_conflict_retry.py.
"""
from __future__ import annotations

import importlib


def fileops_mod():
    """The CURRENT ``_fileops`` module object (sibling suites reload it).

    Several suites in this directory sandbox-reload ``_fileops`` via
    ``del sys.modules["_fileops"]`` + re-import. A module-level
    ``import _fileops`` binds the COLLECTION-time object, so patches applied to
    that stale reference are invisible to code that resolves the module at call
    time. Resolve dynamically at patch time instead.
    """
    return importlib.import_module("_fileops")


def retry_globals():
    """The namespace ``_rmw_with_conflict_retry`` actually reads at call time.

    ``mind_api/src/file_locks.py`` binds the FUNCTION OBJECT once at module
    level via ``from _fileops import _rmw_with_conflict_retry``. That function
    resolves ``get_backend`` and ``_conflict_backoff`` from its own
    ``__globals__`` -- the ``_fileops`` module dict it was DEFINED in -- which
    after a sibling reload is no longer the dict ``importlib.import_module``
    hands back. Patch the wrong one and ``conflict_cls`` falls back to the real
    backend's empty-tuple type, ``except conflict_cls`` matches nothing, and
    the injected conflict escapes.

    MEASURED (g-306-178, fresh interpreter): before any reload the two
    namespaces ARE the same dict, and after ``del sys.modules["_fileops"]`` +
    re-import they are NOT -- patching only ``fileops_mod()`` then leaves the
    retry reading ``conflict_error == ()``. This is why the affected tests
    passed in isolation and failed in the full suite before the helper existed,
    and it is why a mutation proof of this module run SOLO cannot go red on the
    namespace half: in one fresh process the two patches are the same patch.
    Reproduce the reload to prove this half (see
    ``test_conflict_fixture.py::test_namespace_patch_survives_a_sibling_reload``).

    Consumers whose import is FUNCTION-LEVEL (e.g. ``retrieve._locked_bump_jsonl``
    does ``from _fileops import _rmw_with_conflict_retry`` inside the function)
    resolve the current ``sys.modules`` entry at call time and do not need this
    namespace -- ``fileops_mod()`` suffices for them. Patching both is harmless.
    """
    from mind_api.src import file_locks

    return file_locks._rmw_with_conflict_retry.__globals__


def patch_conflict_backend(monkeypatch, backend, *extra, patch_backoff=True):
    """Patch ``get_backend`` in every namespace the retry path can resolve it from.

    ``backend`` is any object exposing ``conflict_error`` (a REAL exception
    class -- a bare ``Exception`` subclass; the empty tuple is what this whole
    module exists to defeat). ``extra`` names additional modules that bind
    ``get_backend`` in their own globals -- pass the module under test whenever
    it does ``from storage_backend import get_backend`` at module level, since
    patching ``storage_backend`` alone is invisible to such a binding.

    ``patch_backoff`` zeroes ``_conflict_backoff`` so a retry test does not pay
    real sleep. Set False only if the backoff itself is under test.

    Returns ``backend`` for convenient one-line fixture bodies.
    """
    get = lambda: backend  # noqa: E731 -- deliberate: monkeypatch wants a callable

    # 1. The retry primitive's own __globals__ -- where conflict_cls comes from.
    #    Reload-proof and exact; see retry_globals().
    monkeypatch.setitem(retry_globals(), "get_backend", get)
    # 2. The CURRENT _fileops module object -- the lock path + atomic write, and
    #    any consumer whose import is function-level.
    monkeypatch.setattr(fileops_mod(), "get_backend", get, raising=False)
    # 3. storage_backend itself -- consumers that import it at CALL time.
    import storage_backend

    monkeypatch.setattr(storage_backend, "get_backend", get, raising=False)
    # 4. Each consumer module's own global, bound at import via
    #    `from storage_backend import get_backend`.
    for mod in extra:
        monkeypatch.setattr(mod, "get_backend", get, raising=False)

    if patch_backoff:
        monkeypatch.setitem(retry_globals(), "_conflict_backoff", lambda attempt: 0)
        monkeypatch.setattr(
            fileops_mod(), "_conflict_backoff", lambda attempt: 0, raising=False
        )
    return backend


def assert_reinvoked(attempts, expected=2):
    """The self-check that makes this shared seam safer than N hand-rolled copies.

    ``attempts`` is the per-attempt record the cycle appends to (a list), or an
    int. A missed namespace, a fake whose ``conflict_error`` stopped being a
    real class, or a modifier that no longer raises all produce the SAME
    symptom: the wrapper never re-invoked, so the test silently exercised the
    single-pass branch. Assert on it explicitly rather than trusting a green.
    """
    n = attempts if isinstance(attempts, int) else len(attempts)
    assert n == expected, (
        f"the wrapper re-invoked {n} time(s), expected {expected} -- conflict "
        "injection failed. Either a namespace this fixture does not patch "
        "resolved get_backend (pass the consumer module in `extra`), the "
        "backend's conflict_error is not a real exception class, or the cycle "
        "no longer raises it. A single pass means the retry branch was never "
        "under test."
    )
