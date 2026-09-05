""": the worker-set -> reducer-cleared defer path.

WHY THIS FILE EXISTS. A worker Body's only defer-writing site is worker-loop
Phase 4a's release path. Until 2026-09-04 it instructed a prose
`precondition_unmet:` defer and nothing else, so the defers it produced were
structurally unclearable: `precondition-defer-recheck.py` (precheck Phase
0.5b.3, the only sweep that clears this class) evaluates
`verification.preconditions` and reads NOTHING in `defer_reason`. Measured on
two boxes nine days apart — cc-04 2026-08-26: 104 eligible, 3 evaluated, 101
skipped_free_form, 0 cleared; cc-13 2026-09-04: 84 eligible, 3 evaluated, 81
skipped_free_form, 0 cleared. ~96% of the lane's own eligible population could
not be acted on at all.

The fix is on the WRITE side (the worker emits a structured predicate), so the
coverage that was missing is the write side's consequence:

  1. A worker-shaped defer CARRYING a structured predicate that passes is
     decided clearable (`would_clear`) on a dry run.
  2. ...and is actually cleared under --apply, via `_clear_defer(source, id)`.
  3. `after_time` — the type worker-loop now names for an elapsed-window gate,
     which was the goal's own motivating case (a running deploy) — clears once
     the window has elapsed and holds before it.
  4. REGRESSION PIN: the SAME defer WITHOUT `verification.preconditions` is
     counted skipped_free_form and never clears. This is the defect itself. If
     someone reverts the worker-loop instruction, this test still documents why
     the prose prefix alone cannot work.

Sibling `test_precondition_defer_recheck_failing_predicate.py` covers the
FAILING branch and never passes --apply (it asserts cleared == 0 throughout),
so no existing test drove a PASSING predicate through to the clear.

Hermetic: `_read_goals` is monkeypatched (no daemon, no live store),
`_clear_defer` is monkeypatched (it shells to aspirations.py against the REAL
world store), `--metrics-log ""` disables the metrics JSONL. No world/meta
writes.

Run: STORAGE_BACKEND=local py -3 -m pytest \
    core/scripts/tests/test_worker_set_defer_clears.py -v
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
        "precondition_defer_recheck_worker_set_module",
        SCRIPT_DIR / "precondition-defer-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def _worker_defer(preconditions, goal_id="g-test-306-434", hours_old=5.0):
    """A goal shaped exactly as worker-loop Phase 4a's release path leaves it:
    pending, precondition_unmet: prose defer, aged past the 2h gate. The
    `preconditions` list is the variable under test — [] reproduces the
    pre-fix worker output, a populated list reproduces the post-fix output."""
    set_at = (dt.datetime.now() - dt.timedelta(hours=hours_old)).strftime(
        "%Y-%m-%dT%H:%M:%S")
    return {
        "id": goal_id,
        "status": "pending",
        "defer_reason": "precondition_unmet: deploy running (worker release path)",
        "defer_reason_set_at": set_at,
        "verification": {"preconditions": preconditions},
    }


def _run_main(monkeypatch, capsys, goals, apply=False, clear_calls=None):
    def fake_read_goals(source):
        if source != "world":
            return []
        out = []
        for g in goals:
            g = dict(g)
            g["_source"] = "world"
            g["_aspiration_id"] = "asp-test"
            out.append(g)
        return out

    monkeypatch.setattr(M, "_read_goals", fake_read_goals)
    if clear_calls is not None:
        def fake_clear(source, goal_id):
            clear_calls.append((source, goal_id))
            return True
        # NEVER let the real one run: it shells to aspirations.py update-goal
        # against the LIVE world store.
        monkeypatch.setattr(M, "_clear_defer", fake_clear)

    argv = ["precondition-defer-recheck.py", "--max-age-hours", "2",
            "--metrics-log", ""]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    rc = M.main()
    return rc, json.loads(capsys.readouterr().out)


def _passing_file_check(tmp_path):
    marker = tmp_path / "worker-gate-cleared.marker"
    marker.write_text("ok", encoding="utf-8")
    return {
        "type": "file_check",
        "id": "pc-g306-434-gate-cleared",
        "path": str(tmp_path / "worker-gate-cleared.*"),
        "condition": "exists",
    }


def test_worker_defer_with_passing_predicate_is_clearable(monkeypatch, capsys, tmp_path):
    """(1) The post-fix worker output: a structured predicate that passes makes
    the defer clearable on a DRY RUN — would_clear is populated before the
    apply branch, so the decision is provable without touching any store."""
    rc, data = _run_main(monkeypatch, capsys,
                         [_worker_defer([_passing_file_check(tmp_path)])])
    assert rc == 0
    assert data["eligible"] == 1
    assert data["evaluated"] == 1
    assert data["skipped_free_form"] == 0
    assert data["would_clear"] == ["g-test-306-434"]
    assert data["cleared"] == 0, "dry run must not clear"


def test_worker_defer_clears_under_apply(monkeypatch, capsys, tmp_path):
    """(2) Under --apply the same defer is cleared through _clear_defer,
    called with the goal's own source and id."""
    calls = []
    rc, data = _run_main(monkeypatch, capsys,
                         [_worker_defer([_passing_file_check(tmp_path)])],
                         apply=True, clear_calls=calls)
    assert rc == 0
    assert data["cleared"] == 1
    assert calls == [("world", "g-test-306-434")]
    det = [d for d in data["details"] if d["goal_id"] == "g-test-306-434"]
    assert len(det) == 1 and det[0]["action"] == "cleared"


def test_after_time_window_holds_then_clears(monkeypatch, capsys):
    """(3) after_time is the type worker-loop now names for an elapsed-window
    gate. An unelapsed window HOLDS the defer; an elapsed one clears it."""
    now = dt.datetime.now()

    unelapsed = {"type": "after_time", "id": "pc-window-open",
                 "anchor": now.strftime("%Y-%m-%dT%H:%M:%S"),
                 "delay_seconds": 86400}
    rc, data = _run_main(monkeypatch, capsys, [_worker_defer([unelapsed])])
    assert rc == 0 and data["would_clear"] == [], "unelapsed window must hold"
    assert data["evaluated"] == 1, "an unelapsed window is EVALUATED, not skipped"

    elapsed = {"type": "after_time", "id": "pc-window-elapsed",
               "anchor": (now - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S"),
               "delay_seconds": 60}
    rc, data = _run_main(monkeypatch, capsys, [_worker_defer([elapsed])])
    assert rc == 0 and data["would_clear"] == ["g-test-306-434"]


def test_prose_only_defer_is_never_cleared(monkeypatch, capsys):
    """(4) THE DEFECT ITSELF. The pre-fix worker output — a correct
    precondition_unmet: prefix and NO structured precondition — is counted
    skipped_free_form and can never clear, however long it ages. The prefix
    governs SUPPRESSION; only verification.preconditions governs CLEARING.
    The explicit skip is the vacuous-truth guard working as designed: zero
    predicates must never read as 'all predicates pass'."""
    rc, data = _run_main(monkeypatch, capsys,
                         [_worker_defer([], hours_old=500.0)],
                         apply=True, clear_calls=[])
    assert rc == 0
    assert data["eligible"] == 1
    assert data["evaluated"] == 0
    assert data["skipped_free_form"] == 1
    assert data["cleared"] == 0 and data["would_clear"] == []
    det = [d for d in data["details"] if d["goal_id"] == "g-test-306-434"][0]
    assert det["action"] == "skipped"
    assert "no structured preconditions" in det["reason"]
