"""Tests for defer-drift-check.py (; apply path ).

The guard flags goals whose deferred_until has gone PAST while a structured-
defer marker persists (deferred_readiness selector pollution — canonical
g-304-11). These tests pin the eligibility ladder, the pure helpers, and the
per-member open-audit dedup + --apply filing path (the old any-open-audit
suppression was the 55-day latch behind g-115-5132).

Pattern: same importlib + sys.path shape as test_unblock_parent_status_sweep.py
(the script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "defer-drift-check.py"

# Fixed reference time so hours_past is deterministic across machines.
NOW = dt.datetime(2026, 6, 12, 7, 0, 0)


def _import():
    spec = importlib.util.spec_from_file_location("defer_drift_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["defer_drift_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _goal(**kw):
    """Canonical drifted (-shaped) goal; override fields via kwargs."""
    g = {
        "id": "g-304-11",
        "status": "pending",
        "defer_reason": "precondition_unmet: 30-day window completes ~2026-07-11",
        "deferred_until": "2026-05-26T00:00:00",   # 17d+ in the past vs NOW
        "_source": "world",
        "_aspiration_id": "asp-304",
        "title": "Build skill-usage-report.py dashboard (Layer 5)",
        "verification": {"preconditions": None},
    }
    g.update(kw)
    return g


# ── _parse_iso (guard-420 datetime tolerance) ──────────────────────────────

def test_parse_iso_valid():
    mod = _import()
    assert mod._parse_iso("2026-05-26T00:00:00") == dt.datetime(2026, 5, 26, 0, 0, 0)


def test_parse_iso_strips_trailing_z():
    mod = _import()
    assert mod._parse_iso("2026-05-26T00:00:00Z") == dt.datetime(2026, 5, 26, 0, 0, 0)


def test_parse_iso_empty_and_none_return_none():
    mod = _import()
    assert mod._parse_iso("") is None
    assert mod._parse_iso(None) is None


def test_parse_iso_malformed_returns_none_not_raises():
    mod = _import()
    assert mod._parse_iso("not-a-date") is None
    assert mod._parse_iso("2026-13-99") is None


# ── _defer_prefix (the three STRUCTURED_DEFER_PREFIXES) ─────────────────────

def test_defer_prefix_matches_all_three_structured():
    mod = _import()
    assert mod._defer_prefix("precondition_unmet: x") == "precondition_unmet:"
    assert mod._defer_prefix("blocked_on_dependency: g-2 incomplete") == "blocked_on_dependency"
    assert mod._defer_prefix("Circuit breaker: 3 consecutive failures") == "Circuit breaker:"


def test_defer_prefix_rejects_free_form_narrative():
    mod = _import()
    assert mod._defer_prefix("waiting on the user to decide") is None
    assert mod._defer_prefix("") is None
    assert mod._defer_prefix("blocked on user-initiated session") is None


# ── _classify_drift (the eligibility ladder) ───────────────────────────────

def test_canonical_g_304_11_shape_is_drift():
    """The exact incident shape: precondition_unmet + past deferred_until +
    pending + prose preconditions → flagged, prose status, large hours_past."""
    mod = _import()
    entry = mod._classify_drift(_goal(), NOW)
    assert entry is not None
    assert entry["goal_id"] == "g-304-11"
    assert entry["defer_prefix"] == "precondition_unmet:"
    assert entry["precondition_status"] == "prose"
    assert entry["hours_past"] > 400  # 2026-05-26 -> 2026-06-12 07:00 ≈ 415h


def test_future_deferred_until_not_drift():
    """Gate still in the future = working as intended (deferred_readiness
    correctly filters). This is the post-fix state of g-304-11."""
    mod = _import()
    assert mod._classify_drift(_goal(deferred_until="2026-07-11T00:00:00"), NOW) is None


def test_terminal_status_not_drift():
    mod = _import()
    for st in ("completed", "archived", "skipped", "expired", "resolved"):
        assert mod._classify_drift(_goal(status=st), NOW) is None


def test_free_form_defer_not_drift():
    """A non-structured (prose-only, no prefix) defer is not this guard's
    concern — it never trips deferred_readiness in the structured way."""
    mod = _import()
    assert mod._classify_drift(_goal(defer_reason="waiting on user input"), NOW) is None


def test_no_deferred_until_not_drift():
    """No time gate → precondition-defer-recheck's domain, not ours."""
    mod = _import()
    assert mod._classify_drift(_goal(deferred_until=None), NOW) is None


def test_unparseable_deferred_until_not_drift():
    mod = _import()
    assert mod._classify_drift(_goal(deferred_until="soon"), NOW) is None


def test_min_hours_past_suppresses_just_elapsed_gate():
    """A gate only 1h past should be suppressed at min_hours_past=2 (a later
    sweep would legitimately re-evaluate it), but flagged at the default 0."""
    mod = _import()
    one_hour_ago = (NOW - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    g = _goal(deferred_until=one_hour_ago)
    assert mod._classify_drift(g, NOW, min_hours_past=2.0) is None
    assert mod._classify_drift(g, NOW, min_hours_past=0.0) is not None


def test_blocked_on_dependency_prefix_is_drift():
    mod = _import()
    entry = mod._classify_drift(
        _goal(defer_reason="blocked_on_dependency: g-304-19 not complete"), NOW)
    assert entry is not None
    assert entry["defer_prefix"] == "blocked_on_dependency"


def test_circuit_breaker_prefix_is_drift():
    mod = _import()
    entry = mod._classify_drift(
        _goal(defer_reason="Circuit breaker: 3 consecutive failures"), NOW)
    assert entry is not None
    assert entry["defer_prefix"] == "Circuit breaker:"


# ── _precondition_status ───────────────────────────────────────────────────

def test_precondition_status_prose_when_no_structured_pcs():
    mod = _import()
    assert mod._precondition_status(_goal()) == "prose"
    # empty list and missing verification both read as prose
    assert mod._precondition_status(_goal(verification={"preconditions": []})) == "prose"
    assert mod._precondition_status(_goal(verification=None)) == "prose"


def test_precondition_status_uncheckable_when_evaluator_absent(monkeypatch):
    """A goal with structured preconditions but no evaluate_all available
    must report 'uncheckable', never crash."""
    mod = _import()
    monkeypatch.setattr(mod, "evaluate_all", None)
    g = _goal(verification={"preconditions": [{"type": "file_exists_after", "path": "/x"}]})
    assert mod._precondition_status(g) == "uncheckable"


# ── on-schedule-expiry discrimination () ─────────────────────────

def test_on_schedule_expiry_classified_and_suppressed():
    """A defer whose prose maturity date == deferred_until expired on schedule —
    classified on_schedule_expiry (NOT drift) so main() keeps it OUT of
    drifted[] (drift_count excludes it, precheck files no Investigate).
    Canonical g-115-1541 FP (asp-304 Layer-5: prose '~2026-06-18' ==
    deferred_until 2026-06-18, flagged at only fractions of an hour past)."""
    mod = _import()
    g = _goal(
        defer_reason="precondition_unmet: 7-day cross-agent window completes ~2026-06-12",
        deferred_until="2026-06-12T00:00:00",  # 7h before NOW; same day as prose
    )
    entry = mod._classify_drift(g, NOW)
    assert entry is not None
    assert entry["classification"] == "on_schedule_expiry"
    assert entry["prose_date"] == "2026-06-12"
    assert entry["hours_past"] == 7.0


def test_genuine_drift_keeps_drift_classification():
    """The canonical  shape (prose ~46d AFTER a stale deferred_until)
    stays classification=drift and carries no prose_date field — the
    on-schedule fix must NOT swallow real drift."""
    mod = _import()
    entry = mod._classify_drift(_goal(), NOW)  # prose 2026-07-11 vs du 2026-05-26
    assert entry is not None
    assert entry["classification"] == "drift"
    assert "prose_date" not in entry


def test_on_schedule_window_boundary():
    """<= window_days (default 1) is on-schedule; > window_days is drift."""
    mod = _import()
    # prose exactly 1 day after deferred_until -> on-schedule (<= 1)
    g1 = _goal(defer_reason="precondition_unmet: completes ~2026-06-11",
               deferred_until="2026-06-10T00:00:00")
    assert mod._classify_drift(g1, NOW)["classification"] == "on_schedule_expiry"
    # prose 2 days after deferred_until -> drift (> 1)
    g2 = _goal(defer_reason="precondition_unmet: completes ~2026-06-12",
               deferred_until="2026-06-10T00:00:00")
    assert mod._classify_drift(g2, NOW)["classification"] == "drift"


def test_no_prose_date_is_drift_not_on_schedule():
    """A structured defer with NO calendar date in the prose cannot be
    on-schedule (no maturity date to compare) — stays drift."""
    mod = _import()
    g = _goal(defer_reason="precondition_unmet: 30-day telemetry window not yet met",
              deferred_until="2026-05-26T00:00:00")
    entry = mod._classify_drift(g, NOW)
    assert entry is not None
    assert entry["classification"] == "drift"


# ── _extract_prose_dates ───────────────────────────────────────────────────

def test_extract_prose_dates_pulls_iso_tokens():
    mod = _import()
    dates = mod._extract_prose_dates("window completes ~2026-07-11 (started 2026-06-11)")
    assert dt.date(2026, 7, 11) in dates
    assert dt.date(2026, 6, 11) in dates


def test_extract_prose_dates_skips_malformed_and_empty():
    mod = _import()
    assert mod._extract_prose_dates("month 2026-13-45 is not a real date") == []
    assert mod._extract_prose_dates("") == []
    assert mod._extract_prose_dates(None) == []


# ── _is_on_schedule_expiry ─────────────────────────────────────────────────

def test_is_on_schedule_expiry_true_when_prose_near_deferred_until():
    mod = _import()
    du = dt.datetime(2026, 6, 12, 13, 0, 0)
    ok, pdate = mod._is_on_schedule_expiry("completes ~2026-06-12", du)
    assert ok is True
    assert pdate == dt.date(2026, 6, 12)


def test_is_on_schedule_expiry_false_when_prose_far_from_deferred_until():
    mod = _import()
    du = dt.datetime(2026, 5, 26, 0, 0, 0)
    ok, pdate = mod._is_on_schedule_expiry("completes ~2026-07-11", du)
    assert ok is False
    assert pdate is None


def test_is_on_schedule_expiry_false_when_no_prose_date_or_no_du():
    mod = _import()
    assert mod._is_on_schedule_expiry("no date here", dt.datetime(2026, 6, 12)) == (False, None)
    assert mod._is_on_schedule_expiry("completes ~2026-06-12", None) == (False, None)


# ── main() per-member dedup + --apply () ─────────────────────────
#
# The old LLM-side dedup (any open class-keyed audit suppresses every filing)
# was the 55-day latch behind . The new rule, in-script: an open
# audit covers a drifted id IFF it NAMES it (title/description, word-
# boundaried); uncovered ids get ONE fresh filing per run. main() is tested
# with _read_goals and _rt.aspirations_add_goal monkeypatched (no daemon),
# --metrics-log "" disables the metrics append (no I/O).

def _audit(goal_id, title=None, description=None, status="pending"):
    mod = _import()
    g = {"id": goal_id, "status": status,
         "origin_signal": mod.AUDIT_ORIGIN_SIGNAL}
    if title is not None:
        g["title"] = title
    if description is not None:
        g["description"] = description
    return g


def _drifted_b():
    """A second canonical drifted goal (sibling of _goal's )."""
    return _goal(id="g-304-15", deferred_until="2026-06-11T00:00:00")


def _patch_main(monkeypatch, mod, world_goals):
    # mod is the TEST-LOCAL import: each _import() re-execs a fresh module
    # object, so the patch must land on the SAME object whose main() runs.
    monkeypatch.setattr(mod, "_read_goals",
                        lambda source: world_goals if source == "world" else [])


def _patch_add(monkeypatch, mod, calls):
    def fake_add(asp_id, record, source="world", overrides=None):
        calls.append({"asp_id": asp_id, "record": record, "source": source,
                      "overrides": overrides})
        return {"goal_id": "g-115-audit-new"}
    monkeypatch.setattr(mod._rt, "aspirations_add_goal", fake_add)


def _run_main(monkeypatch, mod, argv, capsys):
    """main() here takes NO argv (argparse reads sys.argv) — patch it.
    The reason-less sibling's main() takes argv directly; this shape
    difference is the script's, not a test defect."""
    monkeypatch.setattr(sys, "argv", ["defer-drift-check.py"] + argv)
    rc = mod.main()
    return rc, json.loads(capsys.readouterr().out)


def test_main_dry_run_reports_uncovered_but_files_nothing(monkeypatch, capsys):
    mod = _import()
    _patch_main(monkeypatch, mod, [_goal(), _drifted_b()])
    calls = []
    _patch_add(monkeypatch, mod, calls)
    rc, res = _run_main(monkeypatch, mod, ["--metrics-log", ""], capsys)
    assert rc == 0
    assert res["drift_count"] == 2
    assert res["actions_taken"] == "dry-run"
    assert res["uncovered_ids"] == ["g-304-11", "g-304-15"]
    assert res["investigate_filed"] is None
    assert calls == []  # dry-run never files


def test_main_apply_outcome2_open_audit_names_A_new_drift_B(monkeypatch, capsys):
    """THE outcome-2 scenario: an open re-gate audit names goal A; the NEW
    drift is goal B. B is uncovered and gets filed; A is not re-filed, and
    the filed audit names B and not A."""
    mod = _import()
    goals = [
        _goal(),                          #  = A (covered by the audit)
        _drifted_b(),                     #  = B (new drift, uncovered)
        _audit("g-115-5132",
               title="Investigate: re-gate 1 drifted defer(s) g-304-11",
               description="Drifted goals (most-overdue first):\n"
                           "  - g-304-11 [asp-304] (world)"),
    ]
    _patch_main(monkeypatch, mod, goals)
    calls = []
    _patch_add(monkeypatch, mod, calls)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["uncovered_ids"] == ["g-304-15"]
    assert res["open_audit_goal_ids"] == ["g-115-5132"]
    assert res["investigate_filed"] == "g-115-audit-new"
    assert len(calls) == 1  # exactly ONE new audit
    rec = calls[0]["record"]
    assert "g-304-15" in rec["title"]
    assert "g-304-15" in rec["description"]
    # A (covered by the older audit) is NOT re-named in the fresh filing:
    assert "g-304-11" not in rec["title"]
    assert "g-304-11" not in rec["description"]


def test_main_apply_stale_audit_names_none_does_not_suppress(monkeypatch, capsys):
    """An open audit naming NONE of the current drifted ids is stale — it
    must NOT suppress a fresh filing (the class-keyed latch, g-115-5132)."""
    mod = _import()
    goals = [
        _goal(),
        _drifted_b(),
        _audit("g-115-5132",
               title="Investigate: re-gate 1 drifted defer(s) g-999-999",
               description="Drifted goals (most-overdue first):\n"
                           "  - g-999-999 [asp-999] (world)"),
    ]
    _patch_main(monkeypatch, mod, goals)
    calls = []
    _patch_add(monkeypatch, mod, calls)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["uncovered_ids"] == ["g-304-11", "g-304-15"]
    assert res["investigate_filed"] == "g-115-audit-new"
    assert len(calls) == 1
    rec = calls[0]["record"]
    assert "g-304-11" in rec["description"]
    assert "g-304-15" in rec["description"]


def test_main_apply_all_covered_files_nothing(monkeypatch, capsys):
    """Every drifted id named by an open audit -> no filing (the idempotent
    skip the old dedup existed for is preserved)."""
    mod = _import()
    goals = [
        _goal(),
        _drifted_b(),
        _audit("g-115-audit",
               title="Investigate: re-gate 2 drifted defer(s) g-304-11 g-304-15"),
    ]
    _patch_main(monkeypatch, mod, goals)
    calls = []
    _patch_add(monkeypatch, mod, calls)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["actions_taken"] == "apply"
    assert res["uncovered_ids"] == []
    assert res["investigate_filed"] is None
    assert calls == []


def test_main_apply_unreadable_audit_surface_fail_closed(monkeypatch, capsys):
    """An open audit whose title AND description are empty has an unreadable
    naming surface -> treated as covering (suppress the filing). A
    cross-box duplicate never self-heals, so skip-on-uncertainty stays
    correct (guard-487)."""
    mod = _import()
    goals = [_goal(), _audit("g-115-audit")]  # no title, no description
    _patch_main(monkeypatch, mod, goals)
    calls = []
    _patch_add(monkeypatch, mod, calls)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["actions_taken"] == "apply"
    assert res["open_audit_exists"] is True
    assert res["uncovered_ids"] == []
    assert res["investigate_filed"] is None
    assert calls == []


def test_main_apply_terminal_audit_does_not_cover(monkeypatch, capsys):
    """A COMPLETED audit's naming surface is not live coverage — the drift
    may have recurred with (the same) goals; file again."""
    mod = _import()
    goals = [
        _goal(),
        _audit("g-115-audit", status="completed",
               title="Investigate: re-gate 1 drifted defer(s) g-304-11"),
    ]
    _patch_main(monkeypatch, mod, goals)
    calls = []
    _patch_add(monkeypatch, mod, calls)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["uncovered_ids"] == ["g-304-11"]
    assert res["investigate_filed"] == "g-115-audit-new"
    assert len(calls) == 1


def test_main_apply_dup_retry_with_override(monkeypatch, capsys):
    """ (mirrors 's sibling retry): a
    goal_duplication_blocked refusal on the first attempt triggers ONE
    justified X-Mind-Override-Duplication retry (the dup-gate can only
    match COMPLETED prior recurring audits here — the per-member dedup
    already proved no OPEN audit covers the filed ids)."""
    mod = _import()
    _patch_main(monkeypatch, mod, [_goal()])
    calls = []

    def fake_add(asp_id, record, source="world", overrides=None):
        calls.append({"overrides": overrides})
        if overrides is None:
            raise mod._rt.RtError(
                "blocked", status=409,
                body='{"error": "goal_duplication_blocked", '
                     '"gate": "goal-duplication-gate"}')
        return {"goal_id": "g-115-audit-override"}

    monkeypatch.setattr(mod._rt, "aspirations_add_goal", fake_add)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["investigate_filed"] == "g-115-audit-override"
    assert len(calls) == 2
    assert calls[0]["overrides"] is None
    assert "Duplication" in (calls[1]["overrides"] or {})


def test_main_apply_non_dup_error_surfaces_without_retry(monkeypatch, capsys):
    mod = _import()
    _patch_main(monkeypatch, mod, [_goal()])
    calls = []

    def fake_add(asp_id, record, source="world", overrides=None):
        calls.append({"overrides": overrides})
        raise mod._rt.RtError("daemon 500", status=500, body="internal error")

    monkeypatch.setattr(mod._rt, "aspirations_add_goal", fake_add)
    rc, res = _run_main(monkeypatch, mod, ["--apply", "--metrics-log", ""], capsys)
    assert rc == 0
    assert res["investigate_filed"] is None
    assert "investigate_error" in res
    assert len(calls) == 1  # no override retry on a non-duplication error
