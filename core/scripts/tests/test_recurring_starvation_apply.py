"""test_recurring_starvation_apply.py —  integration-path pins.

Sibling to test_recurring_starvation_check.py, which has 22 tests that ALL
monkeypatch `_read_active` and therefore exercise `scan()` in isolation. None
of them reach `main()`, so the apply half — the dedup check and the
`--max-file` cap — was exercised only by hand during g-115-3921 and carried no
regression guard.

This file pins the two behaviours the filing goal names, both of which fail
SILENTLY (a broken dedup looks identical to a working one until the queue
fills with duplicates; a broken cap looks identical until the first run that
files 22 HIGH-priority goals at once):

  1. DEDUP on the exact origin_signal `unblock:recurring-starved-<goal-id>`,
     scanning BOTH queues.
  2. The `--max-file` cap, including its subtlety: the loop breaks on
     `len(filed)`, which counts SUCCESSFUL files only — a dedup or a filing
     failure must NOT consume the run's budget.

Only the daemon boundary is stubbed (`_rt.aspirations_add_goal`), so the real
`_existing_origin_signals()`, the real `_file_unblock()` payload construction,
and the real loop arithmetic in `main()` all run.

Two guardrails shaped the design and are load-bearing here:
  * guard-1482 — a harness that ALWAYS passes a flag leaves that flag's
    DEFAULT untested. `test_default_max_file_is_one` omits `--max-file`
    entirely and pins where the default lands.
  * guard-1639 — never assert inside a loop over a possibly-empty collection.
    Assertions below are exact-count, and the one place a captured list is
    iterated asserts non-empty first.

Pattern: importlib-load the hyphenated module (precedent:
test_recurring_starvation_check.py, check-settings-deny-baseline.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_apply",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)

SIG = "unblock:recurring-starved-{}".format

# Captured at import, BEFORE any fixture replaces it, so the agent-queue test
# below can opt back into the real implementation.
_real_existing = rsc._existing_origin_signals


def _row(goal_id: str = "g-999-01", ratio: float = 5.0, **over) -> dict:
    """One starved row in the exact shape scan() emits (script L390-403)."""
    r = {
        "goal_id": goal_id,
        "aspiration_id": "asp-999",
        "source": "world",
        "title": "Recurring: synthetic sweep",
        "age_hours": 120.0,
        "anchor_field": "lastAchievedAt",
        "interval_hours": 6,
        "basis_hours": 24.0,
        "basis_reason": "interval",
        "ratio": ratio,
        "declared_ratio": 20.0,
        "intended_agent": "zeta",
    }
    r.update(over)
    return r


def _rows(n: int) -> list:
    """n starved rows, worst-first (scan() sorts by -ratio before returning)."""
    return [_row(goal_id=f"g-999-{i:02d}", ratio=float(50 - i)) for i in range(n)]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No live queues, no daemon. Every test opts into what it needs."""
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set())


def _stub_filer(monkeypatch, fail_ids=()):
    """Capture aspirations_add_goal calls. Returns the capture list.

    Only the daemon call is replaced — _file_unblock's real payload
    construction runs, so a regression in the origin_signal it stamps shows up
    in `captured`.
    """
    captured = []

    def _fake(asp_id, payload, source=None):
        captured.append({"aspiration_id": asp_id, "payload": payload,
                         "source": source})
        if payload.get("origin_signal", "").rsplit("-", 1)[-1] in fail_ids:
            raise RuntimeError("simulated daemon failure")
        return {"id": f"g-115-{900 + len(captured)}"}

    monkeypatch.setattr(rsc._rt, "aspirations_add_goal", _fake)
    return captured


def _run(monkeypatch, capsys, rows, argv_extra):
    """Invoke the REAL main() over `rows`; return its parsed JSON verdict."""
    monkeypatch.setattr(rsc, "scan", lambda mult, breaks=None: (rows, {}))
    monkeypatch.setattr(
        sys, "argv",
        ["recurring-starvation-check.py", "--output", "json"] + argv_extra)
    rc = rsc.main()
    assert rc == 0, "main() must always exit 0 (fail-open sweep)"
    return json.loads(capsys.readouterr().out)


# ── The --max-file cap ────────────────────────────────────────────────────

def test_cap_files_exactly_one_of_many(monkeypatch, capsys):
    """22 starved, --max-file 1 -> exactly 1 filed.

    The regression this pins is the one that cannot be noticed by reading the
    code path that broke: if the cap stops working, the NEXT scheduled run
    files one HIGH-priority Unblock per starved goal in a single pass.
    """
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, _rows(22), ["--apply", "--max-file", "1"])

    assert len(out["filed"]) == 1
    assert len(captured) == 1, "cap must bound daemon writes, not just the report"
    # Worst-first: scan() sorts by -ratio, so the cap must keep the FIRST row.
    assert out["filed"][0]["goal_id"] == "g-999-00"


def test_cap_of_three_files_three(monkeypatch, capsys):
    """The cap is a real bound, not a hardcoded 1."""
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, _rows(22), ["--apply", "--max-file", "3"])

    assert len(out["filed"]) == 3
    assert len(captured) == 3


def test_default_max_file_is_one(monkeypatch, capsys):
    """guard-1482: --max-file omitted entirely, so the DEFAULT is what runs.

    Every other test here passes the flag explicitly, which would leave
    `default=1` untested. Production (precheck Phase 0.5c.1) passes it, but a
    default that drifted to 0 or None would be invisible to a harness that
    always overrides it. DO NOT delete this test to deduplicate it against
    test_cap_files_exactly_one_of_many — the point is the omitted flag.
    """
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, _rows(22), ["--apply"])

    assert len(out["filed"]) == 1
    assert len(captured) == 1


def test_max_file_zero_files_nothing(monkeypatch, capsys):
    """--max-file 0 is the report-only shape used for the full summary."""
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, _rows(22), ["--apply", "--max-file", "0"])

    assert out["filed"] == []
    assert captured == [], "no daemon write may happen at cap 0"


def test_no_apply_files_nothing(monkeypatch, capsys):
    """Report-only is the default posture; --apply is the opt-in."""
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, _rows(22), [])

    assert out["filed"] == []
    assert captured == []


# ── Dedup on the exact origin_signal ──────────────────────────────────────

def test_dedup_skips_already_filed_signal(monkeypatch, capsys):
    """An existing Unblock for this goal suppresses a second one."""
    monkeypatch.setattr(rsc, "_existing_origin_signals",
                        lambda: {SIG("g-999-00")})
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, [_row(goal_id="g-999-00")],
               ["--apply", "--max-file", "1"])

    assert out["filed"] == []
    assert out["deduped"] == 1
    assert captured == [], "dedup must happen BEFORE the daemon write"


def test_dedup_absent_signal_does_file(monkeypatch, capsys):
    """Negative control for the test above — proves it discriminates.

    Identical input, empty `existing`: the same row DOES file. Without this,
    a dedup that rejected everything unconditionally would pass the skip test.
    """
    monkeypatch.setattr(rsc, "_existing_origin_signals", lambda: set())
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, [_row(goal_id="g-999-00")],
               ["--apply", "--max-file", "1"])

    assert len(out["filed"]) == 1
    assert out["deduped"] == 0
    assert len(captured) == 1


def test_dedup_consults_the_agent_queue_not_just_world(monkeypatch, capsys):
    """The REAL _existing_origin_signals() must scan BOTH queues.

    `_sources()` yields "agent" only when MIND_AGENT is set, and the sibling
    test file's fixture deletes that env var — so the agent-queue half of the
    dedup read has never executed under test. This pin puts the marker
    ONLY in the agent queue: if _sources() ever regresses to world-only, or
    _existing_origin_signals() stops iterating sources, the marker goes unseen
    and a duplicate Unblock is filed.
    """
    monkeypatch.setenv("MIND_AGENT", "zeta")
    monkeypatch.setattr(
        rsc, "_read_active",
        lambda source: ([{"id": "asp-001", "goals": [
            {"id": "g-001-99", "origin_signal": SIG("g-999-00")}]}]
            if source == "agent" else []))
    # Opt back into the REAL implementation over the fixture's empty-set stub.
    monkeypatch.setattr(rsc, "_existing_origin_signals", _real_existing)
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, [_row(goal_id="g-999-00")],
               ["--apply", "--max-file", "1"])

    assert out["deduped"] == 1, "agent-queue signal must suppress the file"
    assert captured == []


def test_dedup_ignores_unrelated_signals(monkeypatch, capsys):
    """A different goal's starvation Unblock must not suppress this one."""
    monkeypatch.setattr(rsc, "_existing_origin_signals",
                        lambda: {SIG("g-999-77"), "investigate:something-else"})
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, [_row(goal_id="g-999-00")],
               ["--apply", "--max-file", "1"])

    assert len(out["filed"]) == 1
    assert out["deduped"] == 0
    assert len(captured) == 1


def test_filed_payload_carries_the_exact_dedup_signal(monkeypatch, capsys):
    """The signal WRITTEN must equal the signal the next run READS.

    Dedup is a join between _file_unblock's payload and
    _existing_origin_signals' scan. If the two formats ever drift apart, both
    halves keep working in isolation and the sweep re-files forever.
    """
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, [_row(goal_id="g-999-00")],
               ["--apply", "--max-file", "1"])

    assert len(out["filed"]) == 1
    # guard-1639: assert non-empty before reading into the collection.
    assert captured, "filer was never called — the assertion below would be vacuous"
    assert captured[0]["payload"]["origin_signal"] == SIG("g-999-00")
    assert captured[0]["source"] == "world"
    assert captured[0]["aspiration_id"] == "asp-999"


# ── The cap counts SUCCESSFUL files, not attempts ─────────────────────────

def test_dedup_does_not_consume_the_cap(monkeypatch, capsys):
    """A deduped row must not spend the run's budget.

    main() breaks on `len(filed)`, and a dedup takes `continue` without
    appending — so with the worst row already filed, the cap-1 budget must
    still reach the SECOND row. A regression to counting attempts would file
    nothing here and the sweep would stall permanently behind its own
    highest-ratio goal.
    """
    monkeypatch.setattr(rsc, "_existing_origin_signals",
                        lambda: {SIG("g-999-00")})
    captured = _stub_filer(monkeypatch)
    out = _run(monkeypatch, capsys, _rows(3), ["--apply", "--max-file", "1"])

    assert out["deduped"] == 1
    assert len(out["filed"]) == 1
    assert out["filed"][0]["goal_id"] == "g-999-01", "must advance past the dedup"


def test_file_failure_does_not_consume_the_cap(monkeypatch, capsys):
    """A failed daemon write must not spend the budget either.

    _file_unblock swallows the exception and returns None, so main() counts it
    in `file_failures` and moves on with `len(filed)` unchanged.
    """
    captured = _stub_filer(monkeypatch, fail_ids={"00"})
    out = _run(monkeypatch, capsys, _rows(3), ["--apply", "--max-file", "1"])

    assert out["file_failures"] == 1
    assert len(out["filed"]) == 1
    assert out["filed"][0]["goal_id"] == "g-999-01"
    assert len(captured) == 2, "first attempt failed, second succeeded"
