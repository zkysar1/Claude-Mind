"""tree_growth_log SSOT tests ().

The defect this guards is NOT "the append is wrong" — it is "the append is
absent." `tree_growth_log` sat frozen at 8 rows for 3.7 months because no
script ever wrote a DECOMPOSE row; the instruction lived only as prose in
`.claude/skills/tree/SKILL.md`. So the behavioural tests below are necessary
but NOT sufficient, and the wiring tests at the bottom are the ones that
actually cover the failure mode: they assert the call SITES exist on BOTH
write paths. A green behavioural suite over a module nobody calls is exactly
the shape that let the sibling l1-pick-log go silent for ~6 weeks
(g-115-1943) — the module worked fine; the daemon just never invoked it.
"""
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))

from _growth_log import (  # noqa: E402
    decompose_rows, prune_rows, reparent_row, append_rows,
    record_batch, record_reparent,
)

DAY = "2026-07-29"


def _decompose_ops(parent="n", children=("c1", "c2")):
    ops = [{"op": "set", "key": parent, "field": "node_type",
            "value": "interior"}]
    ops += [{"op": "add-child", "key": parent, "child": {"key": c}}
            for c in children]
    ops.append({"op": "propagate", "key": parent})
    return ops


# ---------------------------------------------------------------- DECOMPOSE

def test_decompose_recognized_from_batch_signature():
    rows = decompose_rows(_decompose_ops(), DAY)
    assert len(rows) == 1
    r = rows[0]
    assert r["op"] == "DECOMPOSE"
    assert r["node"] == "n"
    assert r["children"] == ["c1", "c2"]
    assert r["date"] == DAY


def test_decompose_row_shape_matches_the_eight_historical_rows():
    """The 2026-04-04 rows carry exactly these five keys. A reviewer reads old
    and new rows in one list, so the shape must not fork."""
    assert set(decompose_rows(_decompose_ops(), DAY)[0]) == {
        "op", "node", "children", "date", "reason"}


def test_decompose_reason_names_the_writer():
    """Script rows must stay distinguishable from the 8 hand-written ones —
    the 2026-04-04 window is deliberately NOT backfilled (item 4), so a reader
    has to be able to tell which rows came from where."""
    assert "batch" in decompose_rows(_decompose_ops(), DAY)[0]["reason"]


def test_ordinary_add_child_logs_nothing():
    """ item 3, pinned: ordinary child-add does NOT belong in this
    log. Logging every add would bury the structural signal the consumer
    (/fresh-eyes-tree Phase 2.3) reads this log FOR."""
    assert decompose_rows(
        [{"op": "add-child", "key": "n", "child": {"key": "c"}}], DAY) == []


def test_interior_flip_without_children_logs_nothing():
    """Half the signature is not the signature. A bare node_type set is a
    metadata correction, not a decompose."""
    assert decompose_rows(
        [{"op": "set", "key": "n", "field": "node_type",
          "value": "interior"}], DAY) == []


def test_add_child_to_a_different_parent_is_not_part_of_the_decompose():
    """A batch may flip one node and add children elsewhere. Only children of
    the flipped parent belong to its DECOMPOSE row."""
    ops = _decompose_ops("n", ("c1",))
    ops.append({"op": "add-child", "key": "other", "child": {"key": "x"}})
    rows = decompose_rows(ops, DAY)
    assert len(rows) == 1 and rows[0]["children"] == ["c1"]


def test_two_decomposes_in_one_batch_yield_two_rows():
    rows = decompose_rows(
        _decompose_ops("a", ("a1",)) + _decompose_ops("b", ("b1", "b2")), DAY)
    assert [r["node"] for r in rows] == ["a", "b"]


# -------------------------------------------------------------------- PRUNE

def test_prune_row_per_removed_child():
    rows = prune_rows([
        {"op": "remove-child", "key": "p", "child_key": "c1"},
        {"op": "remove-child", "key": "p", "child_key": "c2"},
    ], DAY)
    assert [r["op"] for r in rows] == ["PRUNE", "PRUNE"]
    assert [r["node"] for r in rows] == ["c1", "c2"]


def test_prune_skips_ops_with_no_child_key():
    assert prune_rows([{"op": "remove-child", "key": "p"}], DAY) == []


# ----------------------------------------------------------------- REPARENT

def test_reparent_row():
    r = reparent_row("n", "np", DAY)[0]
    assert r["op"] == "REPARENT" and r["node"] == "n" and "np" in r["reason"]


def test_reparent_requires_both_endpoints():
    assert reparent_row("n", "", DAY) == []
    assert reparent_row("", "np", DAY) == []


# ------------------------------------------------------------------- append

def test_append_preserves_existing_rows():
    tree = {"tree_growth_log": [{"op": "DECOMPOSE", "node": "old",
                                 "date": "2026-04-04"}]}
    append_rows(tree, decompose_rows(_decompose_ops(), DAY))
    assert len(tree["tree_growth_log"]) == 2
    assert tree["tree_growth_log"][0]["node"] == "old"


def test_append_seeds_a_missing_key():
    tree = {}
    assert append_rows(tree, decompose_rows(_decompose_ops(), DAY)) == 1
    assert len(tree["tree_growth_log"]) == 1


def test_append_of_nothing_does_not_create_the_key():
    tree = {}
    assert append_rows(tree, []) == 0
    assert "tree_growth_log" not in tree


# ---------------------------------------------------------------- fail-open

def test_record_batch_fails_open_on_garbage():
    """Contract: a logging bug must never fail the tree write that already
    succeeded. Returns 0, raises nothing."""
    assert record_batch({}, "not-a-list", DAY) == 0
    assert record_batch({}, [None], DAY) == 0


def test_record_reparent_fails_open_on_garbage():
    assert record_reparent(None, "n", "np", DAY) == 0


def test_record_batch_emits_decompose_and_prune_together():
    tree = {}
    ops = _decompose_ops("n", ("c1",)) + [
        {"op": "remove-child", "key": "p", "child_key": "gone"}]
    assert record_batch(tree, ops, DAY) == 2
    assert {r["op"] for r in tree["tree_growth_log"]} == {"DECOMPOSE", "PRUNE"}


# ------------------------------------------------------- WIRING (the real guard)
# These are the tests that cover the actual defect. The module above can be
# perfect and the log still frozen if nothing calls it — and it must be called
# on BOTH write paths, because the daemon is the LIVE one
# (.claude/rules/no-python-cli-fallback.md) while the CLI is what the
# byte-compat suite exercises.

CLI = REPO / "core" / "scripts" / "tree.py"
DAEMON = REPO / "mind_api" / "src" / "world" / "tree_write.py"


def _call_sites(path, name):
    """Count real call sites — `name(` — excluding the import alias line."""
    return len([
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\b%s\s*\(" % re.escape(name), ln)
        and "import" not in ln and not ln.strip().startswith("#")
    ])


def test_cli_calls_the_ssot_on_every_structural_path():
    # cmd_batch + cmd_remove_child
    assert _call_sites(CLI, "_growth_record_batch") >= 2
    # cmd_reparent
    assert _call_sites(CLI, "_growth_record_reparent") >= 1


def test_daemon_calls_the_ssot_on_every_structural_path():
    """The half that was missing for the sibling log (). The daemon
    is the live write path; a CLI-only append is a silent no-op in production."""
    assert _call_sites(DAEMON, "_growth_record_batch") >= 2
    assert _call_sites(DAEMON, "_growth_record_reparent") >= 1


def test_both_paths_import_the_same_module():
    """Not two copies of the logic — ONE module. A forked implementation is
    how the two paths drift apart again."""
    for p in (CLI, DAEMON):
        assert "from _growth_log import" in p.read_text(encoding="utf-8")


TREE_SKILL = REPO / ".claude" / "skills" / "tree" / "SKILL.md"


def test_producer_batch_shape_still_carries_the_signature():
    """The PRODUCER half — found by fresh-eyes review of this very change.

    Everything else here tests that `_growth_log` recognizes the decompose
    signature. Nothing tested that `/tree decompose` still EMITS it, and that
    op is a live deletion risk: batch add-child ALREADY auto-flips a leaf
    parent to interior (tree.py, g-115-1437), so the explicit
    `set node_type interior` in the decompose batch changes no state and reads
    exactly like dead code a tidy-up should drop. Drop it and DECOMPOSE rows
    stop, silently, with every test above still green — the original bug,
    re-created one level up.
    """
    src = TREE_SKILL.read_text(encoding="utf-8")
    i = src.find("# 5-8. Atomically convert parent")
    assert i != -1, "decompose batch block not found in tree/SKILL.md"
    block = src[i:i + 1200]
    assert '"field": "node_type", "value": "interior"' in block, (
        "the decompose batch no longer emits an explicit set-interior op — "
        "_growth_log.decompose_rows cannot recognize a decompose without it, "
        "so DECOMPOSE rows have silently stopped (g-115-3210)")
    assert '"op": "add-child"' in block, (
        "the decompose batch no longer emits add-child ops on the flipped key")


def test_auto_flip_still_exists_so_the_redundancy_warning_stays_true():
    """Guards the OTHER direction: the SKILL.md warning claims the set op is
    redundant-but-load-bearing. If the auto-flip were ever removed, the op
    would become genuinely load-bearing for tree state too and the warning's
    reasoning would be stale — a comment that argues from a fact should fail
    when the fact changes."""
    src = (REPO / "core" / "scripts" / "tree.py").read_text(encoding="utf-8")
    assert 'parent["node_type"] = "interior"' in src, (
        "batch add-child no longer auto-flips leaf->interior; revisit the "
        "'looks redundant' warning in tree/SKILL.md (g-115-1437 / g-115-3210)")


def test_growth_log_call_precedes_serialization_in_both_paths():
    """The row mutates `tree`, so it must land BEFORE the write. If it moves
    after, every row is silently dropped and every test above still passes —
    which is precisely the invisible-failure shape this file exists for.

    SCOPED TO THE INNERMOST ENCLOSING BLOCK, not a line window (g-115-8344).
    The prior form asked "is there A writer within N code lines after the
    call", which cannot tell THIS call's writer from a LATER one's:
    tree_write.py carries 3 growth call sites and 12 `_write_tree_locked`
    occurrences, so a call relocated PAST its own serialization point still
    found a writer inside the window and passed (reproduced bravo 2026-08-30,
    alpha 2026-09-05). guard-2340: never associate structure with a call site
    by line proximity. guard-2257: an assertion that cannot go RED against
    the regression it names is worse than absent.

    WHY THE BLOCK AND NOT THE FUNCTION — measured, after an enclosing-function
    version of this test FAILED to catch the relocation. The daemon's `write()`
    spans lines 1348-2270 and holds many branches, each with its own writer, so
    "a writer later in this function" is satisfied by a DIFFERENT branch's
    writer and the mutant stayed green. Each growth call in fact sits in its own
    `If.body` branch holding exactly one writer (1656->1705, 1886->1889,
    2074->2077), which is the true unit of the contract.

    The CLI needs the enclosing rung as well, and that is not a loosening: it
    uses a CALLBACK shape — `_do_remove(data)` mutates and the caller one scope
    out runs `locked_modify_yaml(_tree_path(), _do_remove)` — so the writer
    CANNOT be inside the mutator. All three tree.py sites are exactly that
    (2772->2779, 3159->3163, 3566->3571), and the invariant still holds.

    Using ast bounds also retires the code-line-budget proxy, so the comment
    fragility the sibling fix worked around cannot recur.
    """
    def _enclosing_blocks(module):
        """(lo, hi, label) for every statement-list block, plus function
        bodies, so a call can be scoped to the tightest one containing it."""
        out = []

        def walk(node):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(node, field, None)
                if isinstance(stmts, list) and stmts:
                    lo = min(s.lineno for s in stmts)
                    hi = max(getattr(s, "end_lineno", s.lineno) for s in stmts)
                    out.append((lo, hi, "%s.%s" % (type(node).__name__, field)))
                    for s in stmts:
                        walk(s)
            for handler in getattr(node, "handlers", []) or []:
                walk(handler)

        walk(module)
        return out

    for path, writer in ((CLI, "locked_modify_yaml"),
                         (DAEMON, "_write_tree_locked")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        blocks = _enclosing_blocks(ast.parse(text, filename=str(path)))

        found_any = False
        for i, ln in enumerate(lines):
            if not re.search(r"\b_growth_record_(batch|reparent)\s*\(", ln) \
                    or "import" in ln:
                continue
            call_line = i + 1
            found_any = True

            # Tightest enclosing block first, then widening. The call is
            # satisfied by the FIRST enclosing scope that contains a writer
            # after it — tight enough that a sibling branch's writer cannot
            # count, wide enough for the CLI's callback shape.
            chain = sorted((b for b in blocks if b[0] <= call_line <= b[1]),
                           key=lambda b: b[1] - b[0])
            assert chain, (
                "%s:%d — growth-log call is not inside any block; the "
                "enclosing-scope contract cannot be evaluated"
                % (path.name, call_line))

            # WIDEN AT MOST ONE RUNG, AND ONLY OUT OF A FUNCTION BODY.
            # Unlimited widening defeats the whole fix, measured 2026-09-05:
            # for a call relocated past its writer the tightest block
            # (If.body 1635-1714) correctly holds NO later writer — the RED
            # signal — but the next rungs out (With.body / Try.body /
            # FunctionDef.body, all ~866 lines) each contain five writers
            # belonging to OTHER branches, so any walk up the chain finds one
            # and the mutant stays green. The only legitimate reason to leave
            # the tightest block is the CLI's callback shape, where the writer
            # lives in the CALLER of the mutator function; that is exactly one
            # step, out of a FunctionDef body, so the escape is scoped to it.
            def _writers_in(hi_line):
                return [
                    k + 1 for k in range(call_line, hi_line)
                    if writer in lines[k]
                    and not lines[k].strip().startswith("#")
                ]

            where = chain[0]
            writer_lines = _writers_in(where[1])
            if not writer_lines:
                # The escape target must itself be a FUNCTION body — the
                # caller of the mutator. Widening to Module.body would admit
                # every writer in the file and silently restore the defect
                # (measured 2026-09-05: the daemon chain is If.body ->
                # With.body -> Try.body -> FunctionDef.body -> Module.body,
                # and only Module.body is reachable past the function, so an
                # unrestricted "one rung out" is really "the whole file").
                fn_idx = next(
                    (j for j, b in enumerate(chain)
                     if b[2] == "FunctionDef.body"), None)
                if fn_idx is not None:
                    outer = next(
                        (b for b in chain[fn_idx + 1:]
                         if b[2] == "FunctionDef.body"), None)
                    if outer is not None:
                        writer_lines = _writers_in(outer[1])
                        if writer_lines:
                            where = outer
            assert writer_lines, (
                "%s:%d — growth-log call is not followed by a %s inside its "
                "enclosing block %s (lines %d-%d), nor in the caller function "
                "body if this is a callback mutator; it may have drifted past "
                "serialization"
                % (path.name, call_line, writer, where[2], where[0], where[1]))

        assert found_any, (
            "%s — no _growth_record_* call site found at all; the pin has "
            "lost its subject (guard-1653: a test that asserts nothing "
            "passes)" % path.name)
