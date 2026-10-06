""": dropped-field-audit part (3) must match by SCOPE, not by name.

THE DEFECT. The audit's consumer test matched variable NAMES file-wide:
`rebound_from` (container key -> variable names) and `reads_by_var` (key ->
variable names it is read off) were both built with a bare `ast.walk` and
intersected on the bare name. In close-review-queue.py, main() binds
`req = select_requests(...)` (the SOURCE) and reads `req["rows"]`, while
_print_list() binds `req = result.get("requests") or {}` (the RECEIVER of a
rebuild that deliberately omits `rows`). On the name alone the intersection is
{req}, so a standing false LIVE (close-review-queue.py:873) read the audit's
whole product -- a ZERO -- as a known non-zero, and every deferrable precheck
lane re-fired it (bravo 05:11, foxtrot 05:12, 2026-10-04).

THE FIX PINS THREE BEHAVIORS:
  1. a read is evidence about a receiver only when it is dataflow-reachable
     from that receiver's BINDING -- inside the binding's function or a
     function nested in it (closures inherit); a same-named variable in a
     sibling function is a different receiver. The collision source below is
     the goal's own shape: two functions each bind `req`, only one from
     `result.get` of the container key, and the key read in the OTHER
     function must not be reported live.
  2. the canonical classify_stranded -> _file_investigate `draft` case stays
     LIVE: the receiver `pr` is bound and read in the SAME function
     (_file_investigate), so the scope rule keeps it (the defect is a record
     that TRAVELS from the rebuild to its consumer, not a record handled in
     one place).
  3. over core/scripts the audit reports live=0, projections_dropped
     unchanged at 1, and no other file's finding gained or lost (the
     g-115-6323 zero survives the scope revision, as the docstring's
     censuses require).

RUN: py -3 -m pytest core/scripts/tests/test_dropped_field_audit.py -q
"""
import ast
import importlib.util
import pathlib
import textwrap

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "dfa", SCRIPTS / "dropped-field-audit.py")
dfa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dfa)


# --------------------------------------------------------------------------
# The collision source (outcome 1 + outcome 4). Mirrors close-review-queue.py
# exactly: main binds req from the SOURCE call and reads req["rows"]; a
# SIBLING function binds req from result.get("requests") (the receiver of a
# rebuild that deliberately omits rows, already merged elsewhere).
# --------------------------------------------------------------------------

COLLISION_SRC = textwrap.dedent('''
    def select(pool):
        rows = [{"id": x} for x in pool]
        return {"rows": rows, "eligible_total": len(rows), "same_mind": [],
                "no_closer": [], "skipped": {}}


    def producer(rec):
        return {
            "rows": rec.get("rows"),
            "eligible_total": rec.get("eligible_total"),
            "same_mind": rec.get("same_mind"),
            "no_closer": rec.get("no_closer"),
            "skipped": rec.get("skipped"),
        }


    def summarize(result):
        req = select(result.get("pool") or [])
        asked = [r["id"] for r in req["rows"]]
        # rows were already merged into result["candidates"] above: the summary
        # deliberately omits them.
        result["requests"] = {
            "eligible_total": req["eligible_total"],
            "same_mind": req["same_mind"],
            "no_closer": req["no_closer"],
            "skipped": req["skipped"],
        }
        return asked


    def show(result):
        req = result.get("requests") or {}
        print(req.get("eligible_total", 0))
        print(req.get("no_closer"))
''')


def _prefixed_name_match(text):
    """Reference part (3) as it existed BEFORE : name-keyed,
    file-wide. Kept here ONLY for discrimination -- to prove the collision
    source is sensitive to the fix (rb-5828: a test whose known-defect input
    cannot go red proves nothing). Parts (1)/(2) reuse the audit's own
    helpers; only the match is the old shape. Do not extend it: it is a
    mirror of the defect, not the audit."""
    tree = ast.parse(text)
    all_dicts = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)]
    container_key = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                        and isinstance(t.slice.value, str):
                    container_key[id(n.value)] = t.slice.value
    rebound_from = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        t = n.targets[0]
        if not isinstance(t, ast.Name):
            continue
        for sub in ast.walk(n.value):
            key = None
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr == "get" and sub.args \
                    and isinstance(sub.args[0], ast.Constant) \
                    and isinstance(sub.args[0].value, str):
                key = sub.args[0].value
            elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                    and isinstance(sub.slice.value, str):
                key = sub.slice.value
            if key:
                rebound_from.setdefault(key, set()).add(t.id)
    reads_by_var = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and n.args \
                and isinstance(n.func.value, ast.Name) \
                and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            reads_by_var.setdefault(n.args[0].value, set()).add(n.func.value.id)
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str):
            reads_by_var.setdefault(n.slice.value, set()).add(n.value.id)
    live = []
    for d in all_dicts:
        keys = [dfa._str_key(k) for k in d.keys]
        if any(k is None for k in keys) or not keys:
            continue
        srcs = [dfa._src_of_value(v) for v in d.values]
        named = [s for s in srcs if s]
        if len(named) < dfa.MIN_GET_VALUES:
            continue
        src = max(set(named), key=named.count)
        if named.count(src) < dfa.MIN_GET_VALUES:
            continue
        rebuilt = set(keys)
        producer_keys = set()
        for o in all_dicts:
            if o is d:
                continue
            okeys = {dfa._str_key(k) for k in o.keys}
            okeys.discard(None)
            shared = okeys & rebuilt
            if len(shared) >= dfa.SHAPE_OVERLAP and len(shared) >= dfa.MIN_SHAPE_RATIO * len(rebuilt):
                producer_keys |= okeys
        missing = producer_keys - rebuilt
        if not missing:
            continue
        ckey = container_key.get(id(d))
        if not ckey:
            continue
        receivers = rebound_from.get(ckey, set())
        if not receivers:
            continue
        live += sorted(k for k in missing if (reads_by_var.get(k, set()) & receivers))
    return live


def test_name_collision_is_not_live():
    """Outcome 1: two functions each bind `req`, only one from result.get of
    the container key; the audit reports no live finding for the key read in
    the other function."""
    findings, rebuilds, projections, ok = dfa.audit_source(
        COLLISION_SRC, "collision.py")
    assert ok
    # Honest denominator (rb-245): the probe MUST have seen the rebuild --
    # otherwise "no live finding" is a zero with no denominator, and a
    # parser regression would read clean.
    assert rebuilds >= 1, (
        "the collision source's rebuild was not even detected -- the audit's "
        "parts (1)/(2) regressed, so this test's green proves nothing")
    coll = [f for f in findings
            if "rows" in f["dropped_but_consumed"]]
    assert not coll, (
        "a read of `rows` off main's own `req` (the source) was attributed to "
        "the sibling function's receiver `req` -- part (3) is matching by NAME "
        "file-wide again (g-115-11975): " + repr(coll))


def test_name_collision_matched_pair():
    """Outcome 4 (matched pair): the SAME source is live under the pre-fix
    name-keyed match and clean under the scoped audit. Without the red half,
    the green half above could be a parser that finds nothing (rb-5828)."""
    pre = _prefixed_name_match(COLLISION_SRC)
    assert pre == ["rows"], (
        "the reference pre-fix matcher no longer reproduces the false LIVE on "
        "the collision source -- either the source drifted or the reference "
        "copy drifted from the pre-fix algorithm; the pair below would then be "
        "testing the wrong thing: " + repr(pre))
    findings, _rb, _pj, ok = dfa.audit_source(COLLISION_SRC, "collision.py")
    assert ok
    post = [k for f in findings for k in f["dropped_but_consumed"]]
    assert "rows" not in post, (
        "the scoped audit still reports the collision key live: " + repr(post))


# --------------------------------------------------------------------------
# The canonical case (outcome 2). Mirrors the real dataflow in
# completed-not-committed-sweep: classify_stranded stores the rebuild under
# entry["pull_request"]; _file_investigate binds pr = entry.get("pull_request")
# or {} and reads pr.get("draft") -- in the SAME function, the way the real
# code does (line 1977 / 1989). The scope rule must keep it LIVE.
# --------------------------------------------------------------------------

CANONICAL_SRC = textwrap.dedent('''
    def probe(sha):
        # the producer: the probe record the rebuild is a projection of
        # (real shape: probe_sha_pull_request resolves number/state/url/
        # title/draft/merge_commit_sha; the rebuild below omits draft).
        return {
            "number": 425,
            "state": "open",
            "url": "https://x/425",
            "title": "self-serve api-key revoke",
            "draft": True,
            "merge_commit_sha": None,
        }


    def classify_stranded(goal, pr):
        entry = {}
        entry["goal_id"] = goal.get("goal_id")
        if pr and pr.get("number"):
            entry["pull_request"] = {
                "number": pr.get("number"),
                "state": pr.get("state"),
                "url": pr.get("url"),
                "title": (pr.get("title") or "")[:80],
            }
        return entry


    def _file_investigate(entry):
        pr = entry.get("pull_request") or {}
        is_draft = pr.get("draft") is True
        return is_draft
''')


def test_canonical_stranded_draft_case_stays_live():
    """Outcome 2: classify_stranded -> _file_investigate draft case pinned as
    live. The receiver is bound and read in one function while the rebuild
    travels to it from another -- scoping must NOT kill that."""
    findings, rebuilds, _pj, ok = dfa.audit_source(
        CANONICAL_SRC, "canonical.py")
    assert ok
    assert rebuilds >= 1, "the canonical rebuild was not detected (parts (1)/(2) regressed)"
    hits = [f for f in findings if "draft" in f["dropped_but_consumed"]]
    assert hits, (
        "the canonical g-115-6323/g-115-6295 shape is no longer live -- the "
        "scope revision over-corrected and the audit's one true positive is "
        "gone. The defect class (a record that TRAVELS to its consumer and "
        "arrives missing a field) must stay detectable: " + repr(findings))
    assert all(f["function"] == "classify_stranded" for f in hits), hits


# --------------------------------------------------------------------------
# The tree (outcome 3).
# --------------------------------------------------------------------------

def _audit_tree():
    root = SCRIPTS
    files = sorted(root.glob("*.py")) + sorted(root.glob("gates/*.py"))
    findings, rebuilds, projections, parsed = [], 0, 0, 0
    unparseable = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fi, rb, pj, ok = dfa.audit_source(text, f.name)
        if not ok:
            unparseable.append(f.name)
            continue
        parsed += 1
        findings += fi
        rebuilds += rb
        projections += pj
    return findings, rebuilds, projections, parsed, unparseable


def test_tree_reports_zero_live_and_unchanged_projection():
    """Outcome 3: the audit over core/scripts reports live=0,
    projections_dropped unchanged at 1, and no other file's finding gained or
    lost (baseline 2026-10-04: live=1 [close-review-queue:873 false positive],
    projections=1 [completed-not-committed-sweep merge_commit_sha],
    rebuilds=330)."""
    findings, rebuilds, projections, parsed, unparseable = _audit_tree()
    assert not unparseable, unparseable
    assert parsed >= 100, (
        f"the denominator collapsed ({parsed} files) -- the probe is blind, "
        "not the tree clean (rb-245)")
    assert not findings, (
        "the audit reports live instance(s) over core/scripts -- the "
        "g-115-6323 zero no longer holds: "
        + "\n".join(f"  {f['file']}:{f['line']} [{f['function']}] "
                    f"dropped_and_consumed={f['dropped_but_consumed']}"
                    for f in findings))
    assert projections == 1, (
        f"projections_dropped moved from 1 to {projections} -- the "
        "completed-not-committed-sweep merge_commit_sha projection is the only "
        "deliberate one in the tree (outcome 3: unchanged)")
    assert rebuilds >= 300, (
        f"rebuilds_found collapsed to {rebuilds} (baseline 330) -- parts "
        "(1)/(2) regressed")
    # no other file's finding gained or lost: the baseline's ONLY finding was
    # the close-review-queue:873 false positive; live=0 with the projection
    # intact is the exact "lost the false one, kept nothing else" shape.
    assert all(f["file"] != "close-review-queue.py" for f in findings)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
