"""test_reason_less_blocked_check.py — regression tests for .

Asserts reason-less-blocked-check.py correctly:
  1. FLAGS a status=blocked goal with an empty Blocker Reference Schema
     (blocker_ref None AND blocked_by [] AND defer_reason None) — the violation
     that stranded g-115-2198-b / g-115-2200 for ~2 days (surfaced by g-115-2591).
  2. does NOT flag a properly-blocked goal — one where ANY of blocker_ref,
     blocked_by, or defer_reason is present (the VERIFY clause: "a properly-
     blocked goal (valid blocker_ref) is NOT flagged").
  3. does NOT flag a non-blocked goal (pending/completed), even with empty
     blocker fields.
  4. detects an already-open audit Investigate (dedup) via the SAME read, and
     ignores a resolved/terminal one.
  5. --apply files ONE reconcile Investigate when reason-less goals exist AND no
     open audit exists; SKIPS filing when an open audit already exists (dedup)
     or when nothing is reason-less; and dry-run never files.

Pattern mirrors test_handoff_aging_check.py: importlib load + monkeypatch on
_read_goals so the suite never hits the daemon, and a fake _rt.aspirations_add_goal
so the apply path never writes a real goal.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_module():
    """Load reason-less-blocked-check.py via importlib (hyphen-free attr name)."""
    spec = importlib.util.spec_from_file_location(
        "reason_less_blocked_check_mod",
        CORE_SCRIPTS / "reason-less-blocked-check.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for reason-less-blocked-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _import_module()


def _blocked(**kw):
    """A status=blocked goal with all blocker fields empty unless overridden."""
    g = {
        "id": "g-test-01",
        "status": "blocked",
        "blocker_ref": None,
        "blocked_by": [],
        "defer_reason": None,
        "title": "test blocked goal",
        "_source": "world",
        "_aspiration_id": "asp-test",
    }
    g.update(kw)
    return g


# ── Pure classifier: the VERIFY clause ──────────────────────────────────────

def test_flags_reason_less_blocked():
    assert mod._is_reason_less_blocked(_blocked()) is True


def test_not_flagged_when_blocker_ref_present():
    # "a properly-blocked goal (valid blocker_ref) is NOT flagged"
    assert mod._is_reason_less_blocked(_blocked(blocker_ref="pq-s3-iam-grant")) is False


def test_not_flagged_when_blocked_by_present():
    assert mod._is_reason_less_blocked(_blocked(blocked_by=["g-115-2068"])) is False


def test_not_flagged_when_defer_reason_present():
    assert mod._is_reason_less_blocked(
        _blocked(defer_reason="precondition_unmet: waiting on DEV env")) is False


def test_not_flagged_when_not_blocked():
    for st in ("pending", "in-progress", "completed", "skipped", "expired"):
        assert mod._is_reason_less_blocked(_blocked(status=st)) is False, st


def test_empty_list_and_missing_fields_are_reason_less():
    # blocked_by absent entirely (not just []), blocker_ref/defer_reason absent
    g = {"id": "g-x", "status": "blocked", "title": "t"}
    assert mod._is_reason_less_blocked(g) is True


def test_non_dict_is_not_flagged():
    assert mod._is_reason_less_blocked(None) is False
    assert mod._is_reason_less_blocked("blocked") is False


# ── Dedup: _find_open_audit uses the same read ──────────────────────────────

def test_find_open_audit_detects_open():
    goals = [
        _blocked(),
        {"id": "g-115-audit", "status": "pending",
         "origin_signal": mod.AUDIT_ORIGIN_SIGNAL},
    ]
    assert mod._find_open_audit(goals) == "g-115-audit"


def test_find_open_audit_ignores_resolved():
    goals = [
        {"id": "g-115-audit", "status": "completed",
         "origin_signal": mod.AUDIT_ORIGIN_SIGNAL},
        {"id": "g-115-audit2", "status": "skipped",
         "origin_signal": mod.AUDIT_ORIGIN_SIGNAL},
    ]
    assert mod._find_open_audit(goals) is None


def test_find_open_audit_none_when_absent():
    assert mod._find_open_audit([_blocked(), {"id": "g", "status": "pending"}]) is None


# ── Investigate record shape ────────────────────────────────────────────────

def test_build_investigate_shape():
    entries = [
        {"goal_id": "g-350-04", "aspiration_id": "asp-350",
         "intended_agent": None, "title": "Feature 3 (Tools)"},
        {"goal_id": "g-350-10", "aspiration_id": "asp-350",
         "intended_agent": "either", "title": "Feature 1 completion"},
    ]
    rec = mod._build_investigate(entries)
    assert rec["origin_signal"] == mod.AUDIT_ORIGIN_SIGNAL
    assert rec["participants"] == ["agent"]
    assert rec["category"] == "framework-maintenance"
    assert "2 reason-less-blocked" in rec["title"]
    # every affected goal id is named in the description (routing surface)
    assert "g-350-04" in rec["description"]
    assert "g-350-10" in rec["description"]


# ── main() apply / dry-run / dedup integration (no daemon) ──────────────────

def _patch_reads(monkeypatch, world_goals, agent_goals=None):
    agent_goals = agent_goals or []

    def fake_read(source):
        return world_goals if source == "world" else agent_goals

    monkeypatch.setattr(mod, "_read_goals", fake_read)


def _patch_add_goal(monkeypatch, calls, returns=None):
    returns = returns or {"goal_id": "g-115-audit-new"}

    def fake_add(asp_id, record, source="world", overrides=None):
        calls.append({"asp_id": asp_id, "record": record, "source": source,
                      "overrides": overrides})
        return returns

    monkeypatch.setattr(mod._rt, "aspirations_add_goal", fake_add)


def _run_main(monkeypatch, argv, capsys):
    rc = mod.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_main_dry_run_reports_but_files_nothing(monkeypatch, capsys):
    _patch_reads(monkeypatch, [_blocked(id="g-350-04")])
    calls = []
    _patch_add_goal(monkeypatch, calls)
    rc, res = _run_main(monkeypatch, [], capsys)  # no --apply
    assert rc == 0
    assert res["reason_less_count"] == 1
    assert res["actions_taken"] == "dry-run"
    assert res["investigate_filed"] is None
    assert calls == []  # dry-run never files


def test_main_apply_files_one_when_no_open_audit(monkeypatch, capsys):
    _patch_reads(monkeypatch, [_blocked(id="g-350-04"), _blocked(id="g-350-10")])
    calls = []
    _patch_add_goal(monkeypatch, calls)
    rc, res = _run_main(monkeypatch, ["--apply"], capsys)
    assert rc == 0
    assert res["reason_less_count"] == 2
    assert res["investigate_filed"] == "g-115-audit-new"
    assert len(calls) == 1  # exactly ONE Investigate
    assert calls[0]["record"]["origin_signal"] == mod.AUDIT_ORIGIN_SIGNAL


def test_main_apply_skips_when_open_audit_exists(monkeypatch, capsys):
    goals = [
        _blocked(id="g-350-04"),
        {"id": "g-115-audit", "status": "pending",
         "origin_signal": mod.AUDIT_ORIGIN_SIGNAL},
    ]
    _patch_reads(monkeypatch, goals)
    calls = []
    _patch_add_goal(monkeypatch, calls)
    rc, res = _run_main(monkeypatch, ["--apply"], capsys)
    assert rc == 0
    assert res["reason_less_count"] == 1
    assert res["open_audit_exists"] is True
    assert res["investigate_filed"] is None
    assert calls == []  # dedup: no second audit filed


def test_main_apply_files_nothing_when_clean(monkeypatch, capsys):
    _patch_reads(monkeypatch, [_blocked(id="g-ok", blocker_ref="pq-x")])
    calls = []
    _patch_add_goal(monkeypatch, calls)
    rc, res = _run_main(monkeypatch, ["--apply"], capsys)
    assert rc == 0
    assert res["reason_less_count"] == 0
    assert res["investigate_filed"] is None
    assert calls == []


def test_main_apply_surfaces_filing_failure(monkeypatch, capsys):
    _patch_reads(monkeypatch, [_blocked(id="g-350-04")])
    calls = []
    _patch_add_goal(monkeypatch, calls,
                    returns=None)  # _rt returning non-dict -> <unknown-id> path

    # Simulate a hard filing failure: aspirations_add_goal raises RtError.
    def fake_add_raises(asp_id, record, source="world"):
        raise mod._rt.RtError("daemon 500")

    monkeypatch.setattr(mod._rt, "aspirations_add_goal", fake_add_raises)
    rc, res = _run_main(monkeypatch, ["--apply"], capsys)
    assert rc == 0
    assert res["investigate_filed"] is None
    assert "investigate_error" in res  # failure surfaced, not swallowed


def test_main_apply_retries_with_override_on_duplication_block(monkeypatch, capsys):
    # : _file_investigate must retry ONCE with a justified
    # X-Mind-Override-Duplication when the daemon blocks on
    # goal_duplication_blocked (the dup-gate matches COMPLETED prior recurring
    # audits — a structural FP the sweep's own open-audit dedup already rules
    # out). Without the retry the reason-less safety mechanism can never
    # escalate a straggler once >=1 reconcile audit has completed.
    _patch_reads(monkeypatch, [_blocked(id="g-350-04")])
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
    rc, res = _run_main(monkeypatch, ["--apply"], capsys)
    assert rc == 0
    assert res["reason_less_count"] == 1
    # Filed on the SECOND (override) attempt.
    assert res["investigate_filed"] == "g-115-audit-override"
    assert res.get("investigate_error") is None
    # Exactly two attempts: first without override (blocked), retry WITH override.
    assert len(calls) == 2
    assert calls[0]["overrides"] is None
    assert "Duplication" in (calls[1]["overrides"] or {})


def test_main_apply_no_retry_on_non_duplication_error(monkeypatch, capsys):
    # : a NON-duplication RtError must NOT trigger the override retry
    # — it surfaces as investigate_error. The retry is scoped strictly to the
    # goal_duplication_blocked false positive, never a blind override on an
    # unrelated failure.
    _patch_reads(monkeypatch, [_blocked(id="g-350-04")])
    calls = []

    def fake_add(asp_id, record, source="world", overrides=None):
        calls.append({"overrides": overrides})
        raise mod._rt.RtError("daemon 500", status=500, body="internal error")

    monkeypatch.setattr(mod._rt, "aspirations_add_goal", fake_add)
    rc, res = _run_main(monkeypatch, ["--apply"], capsys)
    assert rc == 0
    assert res["investigate_filed"] is None
    assert "investigate_error" in res
    # Only ONE attempt — no override retry on a non-duplication error.
    assert len(calls) == 1
    assert calls[0]["overrides"] is None
