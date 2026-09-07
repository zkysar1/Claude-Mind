"""progress.total_goals must be recomputed on goal APPEND ().

THE DEFECT. `_recompute_progress` fired on goal COMPLETE (update_goal) but not
on goal APPEND, so `progress.total_goals` went stale for any aspiration that
accumulated appends without a close. The discriminator is time-since-last-close,
not a property of the aspiration (bravo, 2026-08-23): asp-360 read 0 of 11
completed — never closed a goal, so nothing ever reset it — and carried the
largest deficit in the fleet.

WHY TWO TESTS. There are TWO unrecomputed append paths, and a pin covering only
the obvious one passes while the defect persists:
  - add_goal(ctx)                       — the main POST /v1/aspirations/add-goal
  - _file_routing_audit_investigate(..) — auto-files an Investigate goal
The second fires from an AUDIT into aspirations that may never close a goal,
i.e. exactly the shape that maximises drift.

WHY THE SECOND PIN IS STRUCTURAL. The audit path has no direct endpoint — it
fires only under an internal routing condition — so it is pinned by resolving
the function's AST boundaries and asserting a `_recompute_progress` call inside
its body. That is the same method that located the defect. It is deliberately
NOT a grep: a bare-identifier regex cannot distinguish `x.add(` from `add(`, and
that exact false positive (the unrelated `/v1/aspirations/add` endpoint) had to
be cleared by hand during diagnosis.
"""
from __future__ import annotations

import ast
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT_SRC = (pathlib.Path(__file__).resolve().parents[1]
                / "src" / "endpoints" / "aspirations_write.py")

STORE_REL = ("world", "aspirations.jsonl")


def _post(port, path, query, body=None, *, agent="alpha"):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    data = body if isinstance(body, bytes) else (body.encode("utf-8") if body else None)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _load_records(path: pathlib.Path):
    """Read the fixture store created by the running_daemon tmp tree.

    This is the daemon test fixture's own temporary tree, never the live
    governed store; sibling suites (test_runtime_aspirations_write.py,
    test_runtime_aspirations_add.py) read it the same way.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _asp(records, asp_id):
    return next(a for a in records if a["id"] == asp_id)


def _nonrecurring(asp):
    return sum(1 for g in asp.get("goals", []) if not g.get("recurring"))


def _goal(**kw):
    base = {"title": "Test goal", "status": "pending",
            "origin_signal": "user_directive"}
    base.update(kw)
    return base


def test_add_goal_recomputes_progress_total_on_append(running_daemon):
    """POST add-goal must leave progress.total_goals matching the live count.

    The pre-append value is asserted FIRST. Without that control this test
    could pass against a build where progress happened to be correct for an
    unrelated reason, which is the failure mode that let the original defect
    hide behind 18 expected mismatches in the other direction.
    """
    project_root, port = running_daemon
    live = project_root.joinpath(*STORE_REL)

    before = _asp(_load_records(live), "asp-001")
    total_before = before["progress"]["total_goals"]
    nonrec_before = _nonrecurring(before)
    # Control: the fixture starts reconciled, so a later mismatch is caused by
    # the append under test and not inherited.
    assert total_before == nonrec_before, (
        f"fixture not reconciled before append: progress={total_before} "
        f"live_non_recurring={nonrec_before}")

    status, body = _post(port, "/v1/aspirations/add-goal",
                         {"asp_id": "asp-001", "source": "world"},
                         json.dumps(_goal(title="Appended goal")).encode("utf-8"))
    assert status == 200, f"add-goal failed: {status} {body}"

    after = _asp(_load_records(live), "asp-001")
    total_after = after["progress"]["total_goals"]
    nonrec_after = _nonrecurring(after)

    assert nonrec_after == nonrec_before + 1, "the append itself did not land"
    # THE PIN: pre-fix this read total_before (stale) instead of +1.
    assert total_after == total_before + 1, (
        f"progress.total_goals went stale on APPEND (g-115-6476): "
        f"{total_before} -> {total_after}, live non-recurring {nonrec_after}")
    assert total_after == nonrec_after


def _fn_calls_recompute(tree: ast.AST, fn_name: str) -> bool:
    """True iff module-level `fn_name` calls _recompute_progress in its body."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_recompute_progress"):
                    return True
            return False
    raise AssertionError(f"{fn_name} not found at module level in {ENDPOINT_SRC}")


def test_both_append_paths_recompute_progress():
    """Both append paths must recompute — patching only add_goal is not a fix.

    update_goal is asserted too as a positive control: it recomputed BEFORE this
    fix, so if it ever reads False the AST resolution itself is broken and the
    two assertions above it would be meaningless rather than reassuring.
    """
    tree = ast.parse(ENDPOINT_SRC.read_text(encoding="utf-8"))
    assert _fn_calls_recompute(tree, "update_goal"), (
        "positive control failed: update_goal has always recomputed, so a False "
        "here means this test's AST resolution is broken, not that the code is")
    assert _fn_calls_recompute(tree, "add_goal"), (
        "add_goal does not recompute progress on append (g-115-6476)")
    assert _fn_calls_recompute(tree, "_file_routing_audit_investigate"), (
        "the routing-audit append path does not recompute progress "
        "(g-115-6476) — it files into aspirations that may never close a goal, "
        "the shape that maximises drift")
