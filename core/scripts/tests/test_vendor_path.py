"""Unit tests for _vendor_path ().

_vendor_path sits on the daemon QUERY hot path — three importers
(_embedding_retrieval, _embedding_model, embedding-index-build) pull it in at
startup — and its whole contract is NEVER RAISE. Its correctness is mostly
about the ABSENT case, which is the state of every box that has not
provisioned a vendored encoder stack.

Deliberately numpy-free at module level. The box this module exists FOR is the
one without the vendored stack, so a module-level importorskip("numpy") would
skip exactly the coverage that box needs. Only the one test that reaches
_embedding_retrieval takes that dependency, and it takes it locally.
"""
import errno
import sys

import pytest

import _vendor_path as vp


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Restore sys.path, and PIN MIND_VENDOR_DIR rather than inherit it.

    The pin is not ceremony (guard-2334): every redirect below goes through
    MIND_VENDOR_DIR, so a box that exports it would silently override each
    test's tmp dir with its own — the tests would pass while measuring the real
    vendor stack, which is the exact failure this goal exists to prevent.
    Restoring sys.path matters because these tests mutate a process global that
    the rest of a chunked suite run inherits (guard-2287).
    """
    monkeypatch.delenv("MIND_VENDOR_DIR", raising=False)
    before = list(sys.path)
    yield before
    sys.path[:] = before


# --- case 1: vendor dir present -------------------------------------------

def test_present_dir_returns_true_and_lands_on_path(tmp_path, monkeypatch):
    d = tmp_path / "py"
    d.mkdir()
    monkeypatch.setenv("MIND_VENDOR_DIR", str(d))
    assert vp.ensure_vendor_path() is True
    assert str(d) in sys.path


# --- case 2: vendor dir absent (the unprovisioned-box guarantee) -----------

def test_absent_dir_returns_false_and_leaves_path_untouched(tmp_path, monkeypatch, _isolate):
    missing = tmp_path / "not-provisioned"
    monkeypatch.setenv("MIND_VENDOR_DIR", str(missing))
    assert vp.ensure_vendor_path() is False
    # sys.path is byte-identical to before the call — not merely "missing dir
    # absent from it". This is the import-time behavior too: line 66 calls this
    # same function, so an unprovisioned box's import is a pure no-op.
    #
    # Asserted through the function rather than importlib.reload(vp): a reload
    # would recompute the module-level VENDOR_DIR from the tmp env and leave it
    # pointed at a deleted tmp_path for every later test in the process
    # (guard-2287 pollution). The function IS what import runs.
    assert sys.path == _isolate


def test_absent_dir_still_degrades_cosine_scores_to_empty(tmp_path, monkeypatch):
    """The graceful-degradation guarantee callers actually depend on: with no
    vendored stack, the retrieval hot path returns {} instead of raising."""
    pytest.importorskip("numpy")  # local, not module-level — see module docstring
    import _embedding_retrieval as er

    monkeypatch.setenv("MIND_VENDOR_DIR", str(tmp_path / "not-provisioned"))
    assert vp.ensure_vendor_path() is False
    er.clear_caches()
    assert er.cosine_scores("anything", index_dir=tmp_path / "no-index") == {}


# --- case 3: APPEND, never PREPEND (the load-bearing ordering property) ----

def test_appends_never_prepends(tmp_path, monkeypatch, _isolate):
    """The vendor dir is a FALLBACK provider and must never shadow a real
    system/venv install. Measured on cc-04 2026-08-31: that box has BOTH a
    system numpy (/usr/lib/python3/dist-packages) and a populated vendor dir,
    and numpy resolves to the system copy precisely because this appends —
    so the property is live and load-bearing, not hypothetical.
    """
    d = tmp_path / "py"
    d.mkdir()
    monkeypatch.setenv("MIND_VENDOR_DIR", str(d))
    assert vp.ensure_vendor_path() is True
    # Last entry, and every pre-existing entry still ahead of it in order.
    # Under a prepend this fails on both clauses.
    assert sys.path[-1] == str(d)
    assert sys.path[:-1] == _isolate


# --- case 4: idempotent ----------------------------------------------------

def test_idempotent_no_duplicate_entry(tmp_path, monkeypatch):
    d = tmp_path / "py"
    d.mkdir()
    monkeypatch.setenv("MIND_VENDOR_DIR", str(d))
    assert vp.ensure_vendor_path() is True
    assert vp.ensure_vendor_path() is True
    assert sys.path.count(str(d)) == 1


# --- case 5: unreadable dir -> False, never raises -------------------------

def test_oserror_is_swallowed_not_raised(monkeypatch):
    """The `except OSError` branch.

    Injected rather than provoked with chmod: the suite runs as uid 0 on the
    fleet boxes, and root bypasses DAC — measured 2026-08-31, a chmod-000
    parent returned is_dir() -> True with no error, so a permission-based test
    would pass for the wrong reason on exactly the boxes that run it.
    EACCES genuinely raises out of is_dir() here (pathlib._IGNORED_ERRNOS is
    ENOENT/ENOTDIR/EBADF/ELOOP — EACCES is not among them), so the branch is
    reachable in production; only the local reproduction needs the stand-in.
    """
    class _Unreadable:
        def is_dir(self):
            raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(vp, "_resolve_vendor_dir", lambda: _Unreadable())
    assert vp.ensure_vendor_path() is False  # returns, does not propagate


# --- back-compat: VENDOR_DIR remains a redirectable default ---------------

def test_constant_still_honored_when_env_unset(tmp_path, monkeypatch):
    """The goal requires VENDOR_DIR stay a computed default for back-compat.
    Pinning it keeps guard-577's remedy (patch the constant) available to any
    future test, alongside the call-time env seam."""
    d = tmp_path / "py"
    d.mkdir()
    # env deliberately left unset by the autouse fixture
    monkeypatch.setattr(vp, "VENDOR_DIR", d)
    assert vp._resolve_vendor_dir() == d
    assert vp.ensure_vendor_path() is True
    assert sys.path[-1] == str(d)
