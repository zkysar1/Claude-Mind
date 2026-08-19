"""Pins : learning-routing-repair must REFUSE to raw-write class-(b) stores.

`repair_file` writes by read → mutate → `.tmp` → `replace`. That is safe for a
merge-registered store (write-class (a)): a concurrent write that loses the race
is reconciled by the store's handler on the next sync. It is UNSAFE for a store
with no handler (write-class (b)), where the last writer wins outright and there
is no reconciler below the write to restore the loser.

MEASURED INCIDENT. The day this tool's experience-store lookup was repaired
(g-115-5646) its experience branch went live for the first time, and the next
automatic run — fired by `tree.py::_post_remove_sweep_dangling` after a node
removal — nulled 772 experience-side fields across 12 files and 6 agents. 651 of
those were VALID: 460 tree refs resolve by LEAF name and 191 hypothesis refs
resolve in `pipeline-archive.jsonl`, neither of which this tool's resolver
consults. Before that fix, 8 runs had each nulled ~315 `pipeline.experience_ref`
and the count never moved — because `pipeline.jsonl` IS registered and its
handler put them back every time. A destructive loop ran for days and was
invisible *because the self-heal worked*. Experience stores have no such handler.

WHY THIS SUITE IS MEANINGFUL ON A LOCAL BACKEND, when
`governed-store-write-classes.md` warns that local-backend green proves nothing.
That warning is about fixes whose behavior DIFFERS by backend — a `locked_rmw`
fix is exercised entirely differently against LocalBackend than against
OwnCloudBackend, so a green local run says nothing about production. This gate
is not such a fix: `merge_handler_for` is a pure function of the PATH and opens
no backend at all. `test_gate_verdict_is_backend_independent` pins that property
directly rather than asserting it in prose, so the exemption is measured and
would fail loudly if someone rewired the gate through a backend probe.

Each test is written to fail on a specific mutation:
  - delete the gate                → refusal + bytes-unchanged tests fail
  - gate everything                → class-(a) positive control fails
  - reimplement as a basename grep → shard path-pattern test fails
  - flip the except to fail-open   → fail-closed test fails
  - drop `refused` from the return → signature test fails
"""
import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

REPAIR_PY = SCRIPTS / "learning-routing-repair.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


repair = _load(REPAIR_PY, "_lr_repair_wcg")


# A record whose tree ref is dangling by construction, plus the finding shape
# `repair_file` consumes (record_id / field / ref).
REC = {"id": "exp-probe-1", "tree_nodes_related": ["gone/node"], "hypothesis_id": "h-gone"}
FINDINGS = [
    {"record_id": "exp-probe-1", "field": "tree_nodes_related", "ref": "gone/node"},
    {"record_id": "exp-probe-1", "field": "hypothesis_id", "ref": "h-gone"},
]


def _seed(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The classifier itself
# --------------------------------------------------------------------------

def test_experience_store_classifies_as_unprotected():
    """The store the incident destroyed must classify as write-class (b).

    Basename-keyed, so a tmp path is a faithful probe — no live agent dir needed.
    """
    assert repair.is_merge_protected(Path("/x/agents/alpha/experience.jsonl")) is False
    assert repair.is_merge_protected(Path("/x/agents/alpha/experience-archive.jsonl")) is False


def test_world_stores_classify_as_protected():
    """Positive control. If this fails, the gate refuses EVERYTHING and the tool
    is inert — a silent no-op is the other way to break this fix."""
    for name in ("reasoning-bank.jsonl", "guardrails.jsonl",
                 "pipeline.jsonl", "pattern-signatures.jsonl"):
        assert repair.is_merge_protected(Path("/x/world") / name) is True, name


def test_gate_resolves_through_merge_handler_for_not_basename():
    """A per-agent team-state shard is protected ONLY by a path-pattern branch
    that runs before the basename dict. It has no `_HANDLERS` entry, so any
    reimplementation of this gate as a basename grep reports it unprotected.

    `merge_handler_for`'s own docstring states the rule this pins: 'always
    resolve through this function, never through a grep of the dict.'
    """
    shard = Path("/x/world/team-state/agents/alpha.yaml")
    assert repair.is_merge_protected(shard) is True
    # ...and the same basename OUTSIDE the shard directory is not covered by
    # that branch, which is what makes the test discriminating rather than a
    # tautology about the filename.
    assert repair.is_merge_protected(Path("/x/world/alpha.yaml")) is False


def test_gate_fails_closed_when_classifier_raises(monkeypatch):
    """An unreadable classifier must refuse the write, never re-enable it.

    Simulated by making the import itself fail, which is the real failure mode
    (a moved/renamed/broken coordination_merge), not a contrived exception.
    """
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "coordination_merge":
            raise ImportError("simulated: classifier unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "coordination_merge", raising=False)
    monkeypatch.setattr(builtins, "__import__", _boom)
    assert repair.is_merge_protected(Path("/x/world/reasoning-bank.jsonl")) is False


def test_gate_verdict_is_backend_independent(monkeypatch):
    """The `governed-store-write-classes.md` local-green caveat does not apply
    to this gate, and that is measured here rather than asserted in prose.

    The verdict must be identical under every STORAGE_BACKEND value, including
    an unset one and a nonsense one — which holds only while the gate consults
    no backend.
    """
    probes = [Path("/x/agents/alpha/experience.jsonl"),
              Path("/x/world/reasoning-bank.jsonl")]
    baseline = [repair.is_merge_protected(p) for p in probes]
    for backend in ("local", "own-cloud", "definitely-not-a-backend"):
        monkeypatch.setenv("STORAGE_BACKEND", backend)
        assert [repair.is_merge_protected(p) for p in probes] == baseline, backend
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    assert [repair.is_merge_protected(p) for p in probes] == baseline


# --------------------------------------------------------------------------
# The write path
# --------------------------------------------------------------------------

def test_class_b_file_is_left_byte_identical(tmp_path):
    """THE load-bearing assertion. Delete the gate and this fails: the file is
    rewritten and both fields come back null.

    Byte-equality (not just 'the ref survived') is deliberate — it also proves
    the tool never rewrites the file at all, which is the property that makes a
    concurrent experience write impossible to drop: there is no read-modify-write
    window to lose, by construction rather than by timing.
    """
    p = _seed(tmp_path / "agents" / "alpha" / "experience.jsonl", [REC])
    before = p.read_bytes()

    applied, refused = repair.repair_file(p, FINDINGS)

    assert applied == []
    assert len(refused) == len(FINDINGS)
    assert p.read_bytes() == before
    # No stray .tmp left behind either — a refusal must not half-write.
    assert not (tmp_path / "agents" / "alpha" / "experience.jsonl.tmp").exists()
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["tree_nodes_related"] == ["gone/node"]
    assert rec["hypothesis_id"] == "h-gone"


def test_class_a_file_is_still_repaired(tmp_path):
    """Positive control for the write path. Gate everything and this fails."""
    p = _seed(tmp_path / "world" / "reasoning-bank.jsonl", [dict(REC, id="rb-probe-1")])
    findings = [dict(f, record_id="rb-probe-1") for f in FINDINGS]

    applied, refused = repair.repair_file(p, findings)

    assert refused == []
    assert len(applied) == len(findings)
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["tree_nodes_related"] is None
    assert rec["hypothesis_id"] is None
    # old_value is preserved for the journal — the undo path depends on it.
    assert {a["field"]: a["old_value"] for a in applied} == {
        "tree_nodes_related": ["gone/node"], "hypothesis_id": "h-gone"}


def test_refusal_precedes_the_existence_check(tmp_path):
    """A class-(b) path that does not exist must still report REFUSED, not a
    silent empty result. Ordering matters: 'nothing to do' and 'declined to act'
    are different answers, and collapsing them is how a refusal disappears from
    a caller's view."""
    missing = tmp_path / "agents" / "ghost" / "experience.jsonl"
    applied, refused = repair.repair_file(missing, FINDINGS)
    assert applied == []
    assert len(refused) == len(FINDINGS)


def test_repair_file_returns_applied_and_refused(tmp_path):
    """Signature pin. Dropping `refused` from the return silently discards every
    refusal before `main()` can report it."""
    p = _seed(tmp_path / "world" / "guardrails.jsonl", [dict(REC, id="guard-probe-1")])
    result = repair.repair_file(p, [])
    assert isinstance(result, tuple) and len(result) == 2
    assert all(isinstance(half, list) for half in result)


# --------------------------------------------------------------------------
# Wiring — AST, never a text scan (guard-1099: a source-text grep counts the
# prose that DOCUMENTS the defect and goes red on a healthy file)
# --------------------------------------------------------------------------

def _functions_calling(name):
    tree = ast.parse(REPAIR_PY.read_text(encoding="utf-8"))
    out = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == name):
                out.add(fn.name)
    return out


def test_gate_is_wired_into_both_the_write_and_the_listing():
    """`repair_file` is the write chokepoint; `main` prints the pre-flight
    listing. A dry run that lists a ref it will silently decline to touch is the
    same misleading-report defect the gate exists to end, so both must consult
    the classifier."""
    callers = _functions_calling("is_merge_protected")
    assert "repair_file" in callers
    assert "main" in callers


def test_module_docstring_does_not_promise_a_permanent_zero():
    """The header used to claim the audit reaches 0 dangling refs and that
    'dangling == 0' becomes permanent. With the gate that is false, and a
    docstring asserting a property the code no longer provides is exactly the
    inverted-claim defect this goal was filed to correct — the next reader
    implements the wrong cure from it."""
    doc = ast.get_docstring(ast.parse(REPAIR_PY.read_text(encoding="utf-8"))) or ""
    assert "becomes permanent" not in doc
    # ...and it must positively warn against "fixing" the residual count, since
    # driving it to zero is precisely what destroyed 651 valid refs.
    assert "REFUSED" in doc or "write-class (b)" in doc


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
