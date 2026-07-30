"""test_seed_transform_comment_span_parity.py — PARITY guard across ALL seed
transform handlers (g-115-3564, guard-1640).

WHY A PARITY TEST AND NOT ANOTHER PER-HANDLER TEST. `_seed_transforms.py` has
THREE handlers sharing one shape — `apply_inline_edit`, `apply_global_regex`,
`apply_word_list_strip` — each doing `_check_context(...)` followed by a
substitution. Every one MUST route its `context == "comment"` branch through
`_split_comment_span`, because `_is_comment_line` returns True for a CODE line
carrying a trailing `#`, so a whole-line substitution on a DELETING rule strips
tokens out of executable code (the 2026-07-27 corruption: `import boto3  # noqa`
-> `import   # noqa`, a SyntaxError, published to the public seed repo).

The class HAS ALREADY RECURRED ONCE, which is the whole argument for this file.
g-115-3445 / g-115-3503 fixed `apply_word_list_strip` alone. guard-1640's
trigger_condition named all three families, and the other two kept the defect
and shipped 47 corrupted sites downstream before g-115-3374 caught it (fix:
commit 071d9a327). Nothing structural stopped that, and nothing stops a fourth
handler — or a refactor of an existing one — from dropping the guard again.

EXISTING COVERAGE IS PER-HANDLER, NOT PARITY:
`test_seed_word_list_strip_code_span.py` covers one handler and
`test_seed_manifest_goalid_strip.py` covers another. Neither asserts that EVERY
handler is guarded, which is precisely the property that failed. A new handler
added tomorrow is silently uncovered by both.

WHY COUNT-PARITY AND NOT `grep -c _split_comment_span >= 1`:
a whole-file "at least one" assertion PASSES in exactly the two-of-three state
that shipped the damage. It is satisfiable by the handlers that are already
correct, so it can never report the one that is not. Count-parity fails by
construction when a handler gains a `_check_context` call without the matching
`_split_comment_span` — which is the defect shape, stated directly.

Run: py -3 -m pytest core/scripts/tests/test_seed_transform_comment_span_parity.py
"""
from __future__ import annotations

import ast
import pathlib

import pytest

MODULE = pathlib.Path(__file__).resolve().parents[1] / "_seed_transforms.py"

# The handler family this parity contract governs. A new `apply_*` handler that
# does comment-context substitution belongs here; see test_handler_family_is_complete.
GUARDED_HANDLERS = ("apply_inline_edit", "apply_global_regex", "apply_word_list_strip")


def _tree():
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def _calls_in(fn_node) -> list:
    """Every called-function NAME inside a function body (nested calls included)."""
    out = []
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.append(n.func.id)
    return out


def _handler_nodes() -> dict:
    return {
        n.name: n
        for n in ast.walk(_tree())
        if isinstance(n, ast.FunctionDef) and n.name in GUARDED_HANDLERS
    }


def test_module_is_parseable():
    """Precondition — a syntax error must fail loudly here, not silently skip below."""
    assert _tree() is not None


def test_all_guarded_handlers_exist():
    """A renamed handler must break this file rather than quietly leave the family."""
    found = _handler_nodes()
    missing = [h for h in GUARDED_HANDLERS if h not in found]
    assert not missing, (
        f"seed transform handler(s) {missing} not found in {MODULE.name}. If a handler was "
        "renamed, update GUARDED_HANDLERS; if it was deleted, remove it. Do NOT delete this "
        "assertion — it is what stops the family from silently shrinking (g-115-3564)."
    )


@pytest.mark.parametrize("handler", GUARDED_HANDLERS)
def test_handler_routes_comment_context_through_split(handler):
    """PER-HANDLER: any handler that gates on _check_context must also split the span.

    This is the assertion that fails by construction when a fourth handler is added
    without the guard, or when a refactor drops the split from an existing one.
    """
    node = _handler_nodes().get(handler)
    assert node is not None, f"{handler} missing — see test_all_guarded_handlers_exist"
    calls = _calls_in(node)
    assert "_check_context" in calls, (
        f"{handler} no longer calls _check_context — it has stopped gating on comment "
        "context entirely, which is a bigger change than this test's scope. Verify "
        "deliberately before updating (g-115-3564)."
    )
    assert "_split_comment_span" in calls, (
        f"{handler} calls _check_context but NOT _split_comment_span. _is_comment_line "
        "returns True for a CODE line with a trailing '#', so a whole-line substitution "
        "on a DELETING rule strips tokens out of executable code. That exact shape "
        "published `import boto3  # noqa` -> `import   # noqa` (a SyntaxError) to the "
        "public seed repo, and then RECURRED across the other two handlers for 47 sites "
        "(g-115-3374, guard-1640)."
    )


def test_check_context_and_split_call_site_counts_are_equal():
    """MODULE-WIDE COUNT PARITY — the property a per-handler test cannot express.

    Deliberately NOT `count >= 1`: that form passes in exactly the two-of-three state
    that shipped the damage, because the correct handlers satisfy it on the broken
    one's behalf.
    """
    tree = _tree()
    names = [
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    checks = names.count("_check_context")
    splits = names.count("_split_comment_span")
    assert checks == splits, (
        f"comment-span guard parity broken: {checks} _check_context call site(s) vs "
        f"{splits} _split_comment_span call site(s) in {MODULE.name}. Every handler that "
        "gates on comment context must confine its substitution to the comment tail. An "
        "unmatched _check_context is a handler that will corrupt code on a deleting rule "
        "(g-115-3564, guard-1640)."
    )
    assert checks >= len(GUARDED_HANDLERS), (
        f"only {checks} _check_context call sites for {len(GUARDED_HANDLERS)} guarded "
        "handlers — a handler stopped gating on context, which this parity count would "
        "otherwise report as healthy (both sides dropping together still reads equal)."
    )


def test_handler_family_is_complete():
    """Catch a NEW apply_* handler that does comment-context work outside the family.

    The parity count above is module-wide so it already sees a new handler's call
    sites; this names the handler explicitly so the failure says WHICH one to add.
    """
    stray = []
    for n in ast.walk(_tree()):
        if not isinstance(n, ast.FunctionDef):
            continue
        if not n.name.startswith("apply_") or n.name in GUARDED_HANDLERS:
            continue
        if "_check_context" in _calls_in(n):
            stray.append(n.name)
    assert not stray, (
        f"apply_* handler(s) {stray} gate on _check_context but are not in "
        "GUARDED_HANDLERS, so the per-handler parity assertions above never run for "
        "them. Add them to GUARDED_HANDLERS (g-115-3564)."
    )
