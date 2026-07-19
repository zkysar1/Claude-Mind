"""4: regression test for the failing-predicate emission branch of
precondition-defer-recheck.py — the branch that crashed in production.

Incident (2026-07-16, cc-03 echo precheck): the Phase 0.5b.3 sweep died with
`AttributeError: 'PredicateResult' object has no attribute 'predicate_type'`
at the failing_predicates emission (precondition-defer-recheck.py:282 read
`r.predicate_type`; the dataclass field is `.type`, predicate.py:56). The
crash violated the sweep contract (fail-open, exit 0) and froze every
precondition_unmet defer fleet-wide behind the 120h timeout. Root cause fixed
inline as g-115-2393 (one-line rename); THIS file adds the coverage that was
missing: test_precondition_defer_recheck_tolerant_parse.py covers parse
tolerance only, so 13/13 targeted tests passed while production crashed —
no test ever drove a FAILING structured predicate through the emission.

Coverage contract (sq-019 integration-path lesson from g-335-77):
  1. An aged precondition_unmet defer whose structured predicate FAILS runs
     end-to-end through main() -> evaluate_all() -> real PredicateResult
     objects -> the failing_predicates emission, and main() returns 0 with
     parseable JSON (no traceback). This pins the PredicateResult attribute
     interface the sweep reads (.type / .predicate_id / .reason): renaming
     any of them crashes THIS test the way production crashed.
  2. details[].failing_predicates[0] carries the type/id/reason keys
     downstream consumers parse.
  3. Direct dataclass pin: the three attributes the emission reads exist on
     PredicateResult.

Hermetic: _read_goals is monkeypatched (no daemon, no live store),
--metrics-log "" disables the metrics JSONL. No world/meta writes.

Run: STORAGE_BACKEND=local py -3 -m pytest \
    core/scripts/tests/test_precondition_defer_recheck_failing_predicate.py -v
"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import precondition-defer-recheck.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "precondition_defer_recheck_failing_pred_module",
        SCRIPT_DIR / "precondition-defer-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _aged_goal(goal_id="g-test-2384", hours_old=5.0):
    """A goal shaped exactly like the sweep's eligibility filters require:
    precondition_unmet defer, pending, no deferred_until, aged past the gate,
    ONE structured predicate that deterministically FAILS (file_check on a
    path that cannot exist)."""
    set_at = (dt.datetime.now() - dt.timedelta(hours=hours_old)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    return {
        "id": goal_id,
        "status": "pending",
        "defer_reason": "precondition_unmet: regression fixture (g-115-2384)",
        "defer_reason_set_at": set_at,
        "verification": {
            "preconditions": [
                {
                    "type": "file_check",
                    "id": "pc-g115-2384-absent-marker",
                    "path": "/nonexistent-g115-2384-regression/absent-*.marker",
                    "condition": "exists",
                },
            ],
        },
    }


def _run_main(monkeypatch, capsys, goals_world):
    """Drive main() in-process with _read_goals monkeypatched. Returns
    (exit_code, parsed_json)."""
    def fake_read_goals(source):
        if source == "world":
            out = []
            for g in goals_world:
                g = dict(g)
                g["_source"] = "world"
                g["_aspiration_id"] = "asp-test"
                out.append(g)
            return out
        return []

    monkeypatch.setattr(M, "_read_goals", fake_read_goals)
    monkeypatch.setattr(sys, "argv", [
        "precondition-defer-recheck.py",
        "--max-age-hours", "2",
        "--metrics-log", "",   # disable metrics JSONL — hermetic
    ])
    rc = M.main()
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_failing_predicate_emission_exits_zero_with_json(monkeypatch, capsys):
    """The production crash branch: eligible goal + failing structured
    predicate must flow through the emission and exit 0 with parseable JSON.
    Before g-115-2393 this raised AttributeError at the emission dict."""
    rc, data = _run_main(monkeypatch, capsys, [_aged_goal()])
    assert rc == 0
    assert data["scanned"] == 1
    assert data["eligible"] == 1
    assert data["evaluated"] == 1
    assert data["cleared"] == 0
    assert data["would_clear"] == []


def test_failing_predicates_detail_carries_type_id_reason(monkeypatch, capsys):
    """details[].failing_predicates[0] must carry the type/id/reason keys —
    the exact fields read from PredicateResult (.type/.predicate_id/.reason).
    An interface rename on the dataclass crashes _run_main before this
    assertion, reproducing the incident signature."""
    rc, data = _run_main(monkeypatch, capsys, [_aged_goal()])
    det = [d for d in data["details"] if d["goal_id"] == "g-test-2384"]
    assert len(det) == 1, f"expected one detail row, got: {data['details']}"
    det = det[0]
    assert det["action"] == "skipped"
    assert det["reason"] == "1/1 structured predicates still failing"
    fps = det["failing_predicates"]
    assert len(fps) == 1
    fp = fps[0]
    assert set(fp.keys()) == {"type", "id", "reason"}
    assert fp["type"] == "file_check"
    assert fp["id"] == "pc-g115-2384-absent-marker"
    assert "found 0" in fp["reason"]


def test_predicate_result_interface_pin():
    """Direct pin of the PredicateResult attributes the sweep emission reads.
    If predicate.py reshapes the dataclass (e.g. type -> predicate_type),
    this fails with a message naming the sweep dependency."""
    from predicate import PredicateResult
    r = PredicateResult(passed=False, type="file_check",
                        predicate_id="pc-pin", reason="pin")
    for attr in ("type", "predicate_id", "reason"):
        assert hasattr(r, attr), (
            f"PredicateResult.{attr} missing — precondition-defer-recheck.py's "
            f"failing_predicates emission reads it (g-115-2384 incident class)")
    assert r.type == "file_check"
    assert r.predicate_id == "pc-pin"
    assert r.reason == "pin"
