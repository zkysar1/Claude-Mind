"""A swallowed backend failure must be RECORDED, and must still not raise — .

THE DEFECT. Eleven sites across ``core/scripts`` and ``mind_api/src`` wrap a
best-effort ``ensure_local``/``refresh`` in a bare ``except Exception: pass``.
The swallow is CORRECT and must stay: each call precedes an
``exists()``/``is_file()``/read gate, and every one of those gates guards a
WRITE, so crashing is worse than answering conservatively.

What was wrong is that the failure was recorded NOWHERE. Each of those idioms
was written to fix a specific own-cloud bug — an S3-only ``world/config``
overlay read as absent, a synced team-state re-created and clobbered, a
tree-node body read as empty. When the backend breaks, the ``except`` fires,
the site degrades to exactly the local-only answer the idiom was written to
prevent, and the restored bug is byte-indistinguishable from healthy operation.

TWO-WAY BY CONSTRUCTION (guard-1220). Proving "a broken backend now emits" is
half a test — it would also pass against a helper that emits unconditionally,
which would bury the signal under noise on every healthy run. So the healthy
path is pinned too: a working backend must stay SILENT.

WHAT THIS FIXTURE SEAM EXCLUDES (guard-1462). Only ONE of the eleven sites
(``tree_match.parse_front_matter``) is driven end-to-end against a broken
backend. It was chosen because it takes a path directly and needs no world
resolution. The other ten are covered by the AST invariant below, which proves
the reporting SHAPE is present at each site — NOT that each site's runtime
behaviour is correct. A site could carry the right shape and still, say, report
the wrong path. That gap is real and is not claimed to be covered here.

The AST invariant is itself proved discriminating before it is trusted: a
detector that silently stopped matching would report "0 bare sites" forever,
which is the same false all-clear this whole change exists to remove.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# core/scripts on sys.path so `import storage_backend` resolves (mirrors the
# import shape production uses).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage_backend  # noqa: E402
from storage_backend import (  # noqa: E402
    LocalBackend,
    note_swallowed_backend_error,
    reset_backend_for_tests,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BANNER = "[storage-backend] WARNING"


class _BrokenBackend:
    """Every materialize raises — models a broken or unreachable backend."""

    def ensure_local(self, p):
        raise OSError("simulated backend failure")

    def refresh(self, p):
        raise OSError("simulated backend failure")


@pytest.fixture(autouse=True)
def _isolate():
    """Reset the backend singleton AND the per-process dedup set around each test.

    Without the dedup reset, the second test to report an (op, exception-class)
    pair would be suppressed by the first and read as "emitted nothing".
    """
    reset_backend_for_tests()
    yield
    reset_backend_for_tests()


# --------------------------------------------------------------------------
# The helper itself
# --------------------------------------------------------------------------

def test_emits_naming_the_exception_class_the_message_and_the_path(capsys):
    note_swallowed_backend_error("ensure_local", "/w/config/tree.yaml", OSError("boom"))
    err = capsys.readouterr().err
    assert BANNER in err, f"nothing was emitted at all: {err!r}"
    assert "OSError" in err, f"the exception CLASS is the diagnosis and is missing: {err!r}"
    assert "boom" in err, f"the exception message is missing: {err!r}"
    assert "/w/config/tree.yaml" in err, (
        f"this is a path-resolution failure class, so the path IS the diagnosis: {err!r}")
    assert "ensure_local" in err, f"the operation is missing: {err!r}"


def test_never_raises_on_a_hostile_exception_or_path():
    """The contract that makes this safe to call from inside an ``except``.

    A diagnostic that raises would convert every fail-open site into a
    fail-closed one — strictly worse than the silence it replaces. The calls
    below ARE the assertion: any propagation fails the test.
    """
    class _ExplodingStr(Exception):
        def __str__(self):
            raise RuntimeError("__str__ exploded")

    class _ExplodingPath:
        def __str__(self):
            raise RuntimeError("__str__ exploded")

        __repr__ = __str__

    note_swallowed_backend_error("refresh", "/p", _ExplodingStr())
    note_swallowed_backend_error("refresh", _ExplodingPath(), OSError("x"))
    note_swallowed_backend_error("refresh", None, OSError("x"))


def test_dedup_is_per_op_and_exception_class_not_per_call(capsys):
    """``parse_front_matter`` is the per-node reader behind the concept index,
    so an unconditional line would emit one per tree node (~1246 here) and bury
    its own signal."""
    note_swallowed_backend_error("ensure_local", "/first", OSError("a"))
    note_swallowed_backend_error("ensure_local", "/second", OSError("b"))
    err = capsys.readouterr().err
    assert err.count(BANNER) == 1, f"repeat was not suppressed: {err!r}"
    assert "/first" in err, "the FIRST occurrence must name a concrete path"
    assert "/second" not in err

    # A different exception class is a different failure mode — not a repeat.
    note_swallowed_backend_error("ensure_local", "/third", ValueError("c"))
    err2 = capsys.readouterr().err
    assert err2.count(BANNER) == 1, (
        f"a DIFFERENT exception class was wrongly deduped away: {err2!r}")
    assert "ValueError" in err2


# --------------------------------------------------------------------------
# A real production call site, driven end-to-end
# --------------------------------------------------------------------------

def test_real_call_site_reports_and_still_does_not_raise(capsys, tmp_path):
    """The pin. Before this change the site swallowed silently.

    The helper existing says nothing about the sites being WIRED to it — that
    is the g-115-3731 / g-306-233 shape (a correct component with no caller).
    So this drives the real function, not the helper.
    """
    import tree_match

    storage_backend._ACTIVE_BACKEND = _BrokenBackend()
    result = tree_match.parse_front_matter(tmp_path / "no-such-node.md")

    assert result == {}, (
        "THE SWALLOW MUST STAY: the site has to degrade to its conservative "
        "answer, never raise — this predicate gates a write")
    err = capsys.readouterr().err
    assert BANNER in err, (
        "a real call site swallowed a backend failure with NO diagnostic "
        "anywhere — that is the g-306-218 defect, unfixed")
    assert "OSError" in err, f"the diagnostic does not name the exception: {err!r}"


def test_real_call_site_stays_silent_when_the_backend_is_healthy(capsys, tmp_path):
    """Two-way half. Without this, a helper that emitted unconditionally would
    pass every other test in this file while making the diagnostic worthless."""
    import tree_match

    storage_backend._ACTIVE_BACKEND = LocalBackend()
    node = tmp_path / "node.md"
    node.write_text("---\ntitle: healthy\n---\nbody\n", encoding="utf-8")

    result = tree_match.parse_front_matter(node)

    assert result.get("title") == "healthy", f"the healthy path broke: {result!r}"
    err = capsys.readouterr().err
    assert BANNER not in err, (
        f"a healthy backend emitted a swallow warning — noise on every normal "
        f"run is how a real diagnostic gets ignored: {err!r}")


# --------------------------------------------------------------------------
# The tree-wide invariant, and proof that its detector discriminates
# --------------------------------------------------------------------------

_OPS = {"ensure_local", "refresh"}


def _scan(source: str, label: str):
    """Return (bare, reporting) site labels for one module's source.

    A "site" is a ``try`` whose LAST body statement is a bare
    ``<x>.ensure_local(...)`` / ``<x>.refresh(...)`` call. It is *bare* when a
    handler body is nothing but ``pass``/``continue``, and *reporting* when the
    handler mentions ``note_swallowed_backend_error``.
    """
    bare, reporting = [], []
    tree = ast.parse(source)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Try) or not n.body:
            continue
        last = n.body[-1]
        if not (isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)):
            continue
        fn = last.value.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in _OPS):
            continue
        for h in n.handlers:
            where = f"{label}:{last.lineno} {fn.attr}"
            if all(isinstance(s, (ast.Pass, ast.Continue)) for s in h.body):
                bare.append(where)
            elif "note_swallowed_backend_error" in ast.dump(
                    ast.Module(body=h.body, type_ignores=[])):
                reporting.append(where)
    return bare, reporting


def test_the_detector_discriminates_before_it_is_trusted():
    """guard-1220 applied to the detector. A scanner that quietly stopped
    matching would report 0 bare sites forever — a permanent false all-clear,
    the exact failure mode this change removes."""
    bare_src = (
        "def f(p):\n"
        "    try:\n"
        "        from storage_backend import get_backend\n"
        "        get_backend().ensure_local(p)\n"
        "    except Exception:\n"
        "        pass  # a trailing comment must not hide this site\n"
    )
    reporting_src = (
        "def f(p):\n"
        "    try:\n"
        "        from storage_backend import get_backend\n"
        "        get_backend().ensure_local(p)\n"
        "    except Exception as e:\n"
        "        try:\n"
        "            from storage_backend import note_swallowed_backend_error\n"
        "            note_swallowed_backend_error('ensure_local', p, e)\n"
        "        except Exception:\n"
        "            pass\n"
    )
    bare, reporting = _scan(bare_src, "<bare>")
    assert len(bare) == 1 and not reporting, (bare, reporting)

    bare2, reporting2 = _scan(reporting_src, "<reporting>")
    assert not bare2 and len(reporting2) == 1, (bare2, reporting2)


def test_no_bare_swallow_backend_site_remains_in_the_tree():
    """The scalable half: covers all eleven sites and any added later.

    A trailing comment on the ``pass`` is deliberately in the detector's
    fixture above — a line-based predicate misses exactly that shape, which is
    how ``retrieve.py:188`` escaped the first enumeration of this defect and
    turned a reported count of 10 into 11.
    """
    all_bare, all_reporting = [], []
    for root in ("core/scripts", "mind_api/src"):
        for p in sorted((PROJECT_ROOT / root).rglob("*.py")):
            rel = p.relative_to(PROJECT_ROOT).as_posix()
            if "/tests/" in rel or "__pycache__" in rel:
                continue
            try:
                source = p.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                b, r = _scan(source, rel)
            except SyntaxError:
                continue
            all_bare += b
            all_reporting += r

    assert not all_bare, (
        "these sites swallow a backend failure with no diagnostic — the failure "
        f"is recorded nowhere (g-306-218):\n  " + "\n  ".join(all_bare))
    # Anti-vacuity: a detector that matched nothing would also produce an empty
    # `all_bare`. The known population is 11; a floor of 8 tolerates legitimate
    # refactors while still failing if the wiring is removed wholesale.
    assert len(all_reporting) >= 8, (
        f"only {len(all_reporting)} reporting site(s) found — the detector is "
        "probably no longer matching, so the assertion above proves nothing")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
