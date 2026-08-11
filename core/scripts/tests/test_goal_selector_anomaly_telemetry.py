""" — all_blocked telemetry integrity: test isolation + else-branch logging.

TWO defects, and the goal is emphatic that the ORDER matters, because a backoff
tuned before both land would be tuned against a metric that does not exist in
either direction.

STEP 1 — TEST ISOLATION. `_log_transient_allblocked_recovery` built its
destination inline as `Path(WORLD_DIR) / "goal-selector-anomalies.jsonl"`. The
sibling suite test_goal_selector_allblocked_reread.py patches EIGHT module
attributes (read_jsonl, read_wm, score_goal, AGENT_DIR, ...) but could not patch
that path, because there was nothing to patch. So every suite run appended
fabricated anomalies to real deployment evidence. Measured on cc-02 2026-08-01:
1014 records, ALL 1014 fixture output, spanning 2026-05-31 to that morning. Zero
real anomalies — the evidence file for a live investigation was 100% noise.

The fix is the CLASS fix the goal asks for, not the instance fix: the output now
goes through `_anomalies_path()` (patchable exactly like the inputs), plus
`_anomalies_write_refused()`, which refuses under PYTEST_CURRENT_TEST unless a
destination is named. Same chokepoint shape as g-115-3329. Patching WORLD_DIR in
the one offending test would have fixed only the tests that exist today.

STEP 2 (quarantine) is a data operation, not code — see the goal record.

STEP 3 — ELSE-BRANCH TELEMETRY. The recovery logger had a SINGLE call site,
inside the SUCCESS branch. When the retry ALSO returned zero, nothing was
emitted: no event, no counter. The failure mode under investigation therefore
had a count of zero BY CONSTRUCTION rather than by measurement — structurally
unobservable. It now emits a sibling `transient_all_blocked_retry_also_empty`.

NOTE ON THE GOAL'S OWN DISCRIMINATOR. The goal says to identify fixture records
by `world_aspirations == 1 and world_goals == 1`. Those field names are not in
the schema — the real keys are `first_world_aspirations` / `retry_world_*`
(verified with jsonl-field-probe: field_present=false). Applied literally, that
test classifies every record as real and leaves the pollution in place. The
rb-245 rule is why this was checked instead of trusted. `test_fixture_record_
discriminator_uses_real_field_names` below pins the correct predicate.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# ------------------------------------------------------------------ the seam

def test_anomalies_path_defaults_to_world_dir():
    os.environ.pop("GOAL_SELECTOR_ANOMALIES_PATH", None)
    p = gs._anomalies_path()
    assert p.name == "goal-selector-anomalies.jsonl"
    assert str(gs.WORLD_DIR) in str(p)


def test_anomalies_path_honors_override(tmp_path, monkeypatch):
    target = tmp_path / "anom.jsonl"
    monkeypatch.setenv("GOAL_SELECTOR_ANOMALIES_PATH", str(target))
    assert gs._anomalies_path() == target


def test_write_refused_under_pytest_without_override(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.delenv("GOAL_SELECTOR_ANOMALIES_PATH", raising=False)
    assert gs._anomalies_write_refused() is True


def test_write_allowed_under_pytest_with_override(tmp_path, monkeypatch):
    """Naming a destination is the opt-in: a test that WANTS the record gets it."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setenv("GOAL_SELECTOR_ANOMALIES_PATH", str(tmp_path / "a.jsonl"))
    assert gs._anomalies_write_refused() is False


def test_write_allowed_in_production(monkeypatch):
    """The real deployment path must be entirely unaffected by the chokepoint."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GOAL_SELECTOR_ANOMALIES_PATH", raising=False)
    assert gs._anomalies_write_refused() is False


# ------------------------------------------- emitter: payload, not exit status

def test_emitter_writes_recovery_record_to_override(tmp_path, monkeypatch):
    """Positive control asserting the PAYLOAD (guard-1627): reading only 'a file
    appeared' would be satisfied by an emitter failing for the wrong reason."""
    target = tmp_path / "anom.jsonl"
    monkeypatch.setenv("GOAL_SELECTOR_ANOMALIES_PATH", str(target))
    first = [{"id": "a1", "status": "active", "goals": [{"id": "g1"}, {"id": "g2"}]}]
    retry = [{"id": "a1", "status": "active", "goals": [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}]}]

    gs._log_transient_allblocked_recovery(first, retry, 3)

    rec = json.loads(target.read_text(encoding="utf-8").strip())
    assert rec["event"] == "transient_all_blocked_recovered"
    assert rec["retry_candidates"] == 3
    assert rec["first_world_goals"] == 2 and rec["retry_world_goals"] == 3
    assert rec["world_content_changed_between_reads"] is True


def test_emitter_writes_sibling_retry_also_empty_record(tmp_path, monkeypatch):
    """STEP 3: the failure branch is now observable."""
    target = tmp_path / "anom.jsonl"
    monkeypatch.setenv("GOAL_SELECTOR_ANOMALIES_PATH", str(target))
    world = [{"id": "a1", "status": "active", "goals": [{"id": "g1"}]}]

    gs._log_transient_allblocked_recovery(
        world, world, 0, event="transient_all_blocked_retry_also_empty")

    rec = json.loads(target.read_text(encoding="utf-8").strip())
    assert rec["event"] == "transient_all_blocked_retry_also_empty"
    assert rec["retry_candidates"] == 0
    assert rec["world_content_changed_between_reads"] is False


def test_emitter_suppressed_under_pytest_writes_nothing(tmp_path, monkeypatch):
    """The isolation guarantee itself. Points WORLD_DIR at tmp_path so a
    regression would be caught HERE rather than in the real world file."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.delenv("GOAL_SELECTOR_ANOMALIES_PATH", raising=False)
    monkeypatch.setattr(gs, "WORLD_DIR", str(tmp_path))
    world = [{"id": "a1", "status": "active", "goals": [{"id": "g1"}]}]

    gs._log_transient_allblocked_recovery(world, world, 1)

    assert not (tmp_path / "goal-selector-anomalies.jsonl").exists(), \
        "fixture output reached the deployment path — the isolation fix regressed"


# --------------------------------------------------------------- call wiring

def _drive(monkeypatch, world_reads):
    """Minimal cmd_select driver — mirrors test_goal_selector_allblocked_reread."""
    world_q = list(world_reads)

    def _rj(path):
        if path == gs.WORLD_ASP_PATH:
            return world_q.pop(0) if world_q else []
        return []

    monkeypatch.setattr(gs, "read_jsonl", _rj)
    monkeypatch.setattr(gs, "read_wm", lambda: {"slots": {}})
    monkeypatch.setattr(gs, "load_recent_class_completions", lambda window_size=20: [])
    monkeypatch.setattr(gs, "load_exploration_params", lambda: (0.0, 0.0))
    monkeypatch.setattr(gs, "score_goal", lambda c, wm, resolved, sc, **kw: {
        "goal_id": c["goal"]["id"], "aspiration_id": c["aspiration"]["id"],
        "title": c["goal"].get("title", ""), "score": 1.0,
        "recurring": False, "breakdown": {}, "raw": {}})
    monkeypatch.setattr(gs, "apply_substantive_demotion", lambda scored, cfg: None)
    monkeypatch.setattr(gs, "_record_strategy_application", lambda *a, **k: None)
    monkeypatch.setattr(gs, "AGENT_DIR", None)

    seen = []
    real = gs._log_transient_allblocked_recovery

    def _spy(first, retry, count, event="transient_all_blocked_recovered"):
        seen.append(event)
        return real(first, retry, count, event=event)

    monkeypatch.setattr(gs, "_log_transient_allblocked_recovery", _spy)
    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_select(argparse.Namespace())
    return buf.getvalue(), seen


def _blocked_world():
    return [{"id": "asp-t", "status": "active", "goals": [{
        "id": "g-t-09", "title": "blocked", "status": "pending",
        "participants": ["agent"], "deferred_until": "2099-01-01T00:00:00"}]}]


def _pending_world():
    return [{"id": "asp-t", "status": "active", "goals": [{
        "id": "g-t-01", "title": "pending", "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM"}]}]


def test_else_branch_emits_retry_also_empty(monkeypatch):
    """THE STEP 3 WIRING. Before this, the genuine-all-blocked path emitted
    nothing, so its rate was unmeasurable and any backoff tuned against it would
    have been tuned against a number that could not move."""
    out, events = _drive(monkeypatch, [_blocked_world(), _blocked_world()])
    assert json.loads(out).get("all_blocked") is True
    assert events == ["transient_all_blocked_retry_also_empty"], events


def test_success_branch_still_emits_recovery(monkeypatch):
    """The pre-existing success event must not be disturbed by the new sibling."""
    out, events = _drive(monkeypatch, [_blocked_world(), _pending_world()])
    assert isinstance(json.loads(out), list)
    assert events == ["transient_all_blocked_recovered"], events


def test_no_event_when_first_pass_succeeds(monkeypatch):
    """No retry, so neither event fires — keeps the two events meaningful as rates."""
    out, events = _drive(monkeypatch, [_pending_world()])
    assert isinstance(json.loads(out), list)
    assert events == [], events


# ------------------------------------------------------- the discriminator

def test_fixture_record_discriminator_uses_real_field_names():
    """The goal's stated discriminator (`world_aspirations`/`world_goals`) is not
    in this schema; the emitter writes `first_world_*` / `retry_world_*`. Pinned
    because applying the stated version literally classifies EVERY record as real
    and silently leaves the pollution in place — a false negative in the
    expensive direction (rb-245)."""
    emitted = {
        "ts": "2026-08-01T10:06:18",
        "event": "transient_all_blocked_recovered",
        "retry_candidates": 1,
        "first_world_aspirations": 1, "retry_world_aspirations": 1,
        "first_world_goals": 1, "retry_world_goals": 1,
        "world_content_changed_between_reads": False,
    }
    assert "world_aspirations" not in emitted
    assert "world_goals" not in emitted

    def is_fixture(rec):
        return (rec.get("first_world_aspirations") == 1
                and rec.get("first_world_goals") == 1
                and rec.get("retry_world_aspirations") == 1
                and rec.get("retry_world_goals") == 1)

    assert is_fixture(emitted) is True
    real = dict(emitted, first_world_aspirations=22, first_world_goals=1366,
                retry_world_aspirations=22, retry_world_goals=1366)
    assert is_fixture(real) is False
