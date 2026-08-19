""" — every handler that routes `source` into _resolve_paths must
reject an invalid value BEFORE doing so.

THE DEFECT. `_resolve_paths(ctx, source)` branches `if source == "agent": ...
else: <world>` and has NO error arm. So any value that is not exactly the string
"agent" — a typo (`agnet`), a case variant (`Agent`), a shell-mangled empty
string — resolves to the WORLD queue. On `complete_by` that completed a goal in
the wrong queue; on `retire` that archived an aspiration in the wrong queue.
Both are writes, and both reported success.

WHY THIS TEST IS STRUCTURAL RATHER THAN PER-HANDLER. The obvious fix is to copy
the sibling guard into the two handlers that lacked it, and that is what landed.
But that fix has no memory: a fifteenth handler added next month inherits
nothing, and the defect reopens silently — which was the argument for putting the
rejection inside `_resolve_paths` instead. That alternative was rejected on blast
radius (guard-1562): the helper has 17 dynamic call sites, returns a tuple rather
than a Response, and giving it an error arm changes the contract for 13 handlers
that already reject before ever reaching it.

This test buys the "no future handler can drift" property without that contract
change. It enumerates the call sites from the AST rather than from a hardcoded
list, so a NEW handler joins the population automatically and fails here on the
day it is written — it cannot be forgotten, because nobody has to remember to add
it.

WHAT IT DELIBERATELY DOES NOT CHECK. It asserts the guard is PRESENT, not that it
is reached before `_resolve_paths` on every path, and not that it returns 400.
Those are behavioural properties covered by the per-handler runtime tests
(test_runtime_aspirations_*). A presence check is the right shape here precisely
because the failure mode being guarded is an omission, not a mistake.
"""
import ast
import pathlib

import pytest

ENDPOINT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "endpoints" / "aspirations_write.py"
)

# The resolver whose missing error arm makes an unvalidated source dangerous.
_ROUTES_SOURCE = "_resolve_paths(ctx, source)"
_GUARD = "invalid_source"


def _handlers_routing_source():
    """Every function whose body routes a *variable* source into _resolve_paths.

    Derived from the AST, not hardcoded: a handler added later is picked up with
    no edit here. Call sites passing a literal (`_resolve_paths(ctx, "world")`)
    are excluded — a literal cannot be invalid.
    """
    src = ENDPOINT.read_text(encoding="utf-8")
    lines = src.split("\n")
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if _ROUTES_SOURCE in body:
            out.append((node.name, body))
    return out


def test_population_is_non_empty():
    """Anti-vacuity: if the AST walk or the call-site string ever stops matching,
    every assertion below would pass over an EMPTY population and this file would
    report green while checking nothing."""
    handlers = _handlers_routing_source()
    assert len(handlers) >= 10, (
        f"expected the aspirations write surface to route source into "
        f"{_ROUTES_SOURCE} in many handlers, found {len(handlers)} — the call-site "
        f"pattern probably changed, and this suite is now vacuous"
    )


@pytest.mark.parametrize("name", [n for n, _ in _handlers_routing_source()])
def test_handler_rejects_invalid_source(name):
    """Each handler routing a variable source must carry the rejection.

    g-115-5306 measured `complete_by` and `retire` without it. A new handler that
    forgets it fails here on the day it is added.
    """
    body = dict(_handlers_routing_source())[name]
    assert _GUARD in body, (
        f"{name}() routes `source` into _resolve_paths but never rejects an "
        f"invalid value. _resolve_paths has no error arm — anything that is not "
        f"exactly 'agent' silently resolves to the WORLD queue and the write "
        f"reports success. Add:\n"
        f"    if source not in (\"world\", \"agent\"):\n"
        f"        return Response.error(400, \"invalid_source\", "
        f"f\"source must be 'world' or 'agent', got '{{source}}'\")"
    )


def test_resolve_paths_still_has_no_error_arm():
    """Pins the PREMISE, so this suite cannot silently outlive its own reason.

    If a future change gives _resolve_paths its own rejection, the per-handler
    guards become belt-and-braces rather than load-bearing, and this file should
    be revisited rather than left asserting a rationale that no longer holds.
    Failing here is a prompt to re-read, not a defect.
    """
    src = ENDPOINT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.split("\n")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_resolve_paths"
    )
    body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
    assert _GUARD not in body and "raise" not in body, (
        "_resolve_paths now appears to validate or raise. The per-handler guards "
        "this suite enforces were justified by it NOT doing so (see module "
        "docstring); re-read that argument before deleting or keeping them."
    )
