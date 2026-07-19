"""Daemon update_goal defer-cascade tests (PR 7e/3).

Covers the in-lock mutation cascades that follow a defer_reason write:
  - last_modified stamped on every write
  - defer_reason set: defer_reason_set_at + auto-deferred_until +
    blocker_ref persistence
  - defer_reason clear: defer_reason_set_at clear + blocker_ref drop
  - blocker_ref requirement gate on narrative defers (header-driven)

Tests that need to bypass the upstream capability gate (which would fire
on common defer phrasing — "human", "deploy", etc. — based on the forged-
skill registry) monkeypatch _capability_eval to a fixed-pass. This keeps
cascade tests sensitive to cascade bugs only, not to forged-skill churn.

Structured-prefix bypass (gate doesn't fire) is covered in
test_runtime_aspirations_gates.py via the capability-gate parametrize.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest


@pytest.fixture
def aspirations_write_module():
    from mind_api.src.endpoints import aspirations_write
    return aspirations_write


@pytest.fixture
def cap_gate_passes(aspirations_write_module, monkeypatch):
    """Force the capability gate to a clean pass so cascade tests aren't
    coupled to forged-skill keyword drift."""
    monkeypatch.setattr(
        aspirations_write_module, "_capability_eval",
        lambda *a, **kw: {"would_block": False, "reason": "monkeypatched pass"},
    )


def _post(port, path, query, body, *, agent="alpha", headers=None):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}" if qs else f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _seed_goal(port, **fields):
    """Add a base goal to asp-001 for the test. Returns the persisted goal."""
    goal = {
        "title": "Seed goal for cascade test",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,
    }
    goal.update(fields)
    # mc-066 (0): the Phase E.5 operator-offload gate 400s any
    # recurring-shaped seed (recurring=True OR interval_hours present) that
    # lacks an offload_decision. Inject the fixture decision unless the test
    # supplies its own — the gate-shape pin test seeds via raw _post
    # precisely to exercise the 400.
    if (goal.get("recurring") is True
            or goal.get("interval_hours") is not None) \
            and "offload_decision" not in goal:
        goal["offload_decision"] = "stays-mind: test fixture"
    body = json.dumps(goal).encode("utf-8")
    code, resp_body = _post(port, "/v1/aspirations/add-goal",
                            {"asp_id": "asp-001", "source": "world"}, body)
    assert code == 200, resp_body
    return json.loads(resp_body)["goal"]


def _update(port, goal_id, field, value, **kwargs):
    return _post(port, "/v1/aspirations/update-goal",
                 {"id": goal_id, "field": field, "source": "world"},
                 json.dumps(value).encode("utf-8"), **kwargs)


def _read_goal(project_root, goal_id):
    live = project_root / "world" / "aspirations.jsonl"
    for line in live.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


# ---------------------------------------------------------------------------
# last_modified — stamped on every successful write
# ---------------------------------------------------------------------------

def test_last_modified_stamped_on_every_update(running_daemon):
    project_root, port = running_daemon
    g = _seed_goal(port)
    code, _ = _update(port, g["id"], "title", "Edited title")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert "last_modified" in persisted
    # ISO 8601 second-precision
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                    persisted["last_modified"])


# ---------------------------------------------------------------------------
# defer_reason SET path — blocker_ref required (or structured-prefix bypass)
# ---------------------------------------------------------------------------

def test_narrative_defer_without_blocker_ref_blocked(running_daemon, cap_gate_passes):
    """Narrative defer + no blocker_ref header + no override → 400."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(port, g["id"], "defer_reason",
                         "blocked on user feedback")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "blocker_ref_required"
    assert err["gate"] == "blocker-ref-gate"


def test_narrative_defer_with_valid_blocker_ref_passes(running_daemon, cap_gate_passes):
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "user_action", "external_id": "rfc-42-ack"})
    code, body = _update(
        port, g["id"], "defer_reason",
        "awaiting human ack on rfc",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200, f"unexpected 400: {body}"
    persisted = _read_goal(project_root, g["id"])
    # Defer was written, set_at was stamped
    assert persisted["defer_reason"] == "awaiting human ack on rfc"
    assert "defer_reason_set_at" in persisted
    # blocker_ref persisted with derived expires_at
    assert persisted["blocker_ref"]["type"] == "user_action"
    assert persisted["blocker_ref"]["external_id"] == "rfc-42-ack"
    assert "expires_at" in persisted["blocker_ref"]


def test_narrative_defer_with_invalid_blocker_ref_blocked(running_daemon, cap_gate_passes):
    """X-Mind-Blocker-Ref with bad JSON / bad type → 400 blocker_ref_invalid."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(
        port, g["id"], "defer_reason", "blocked on something",
        headers={"X-Mind-Blocker-Ref": '{"type":"made-up","external_id":"x"}'},
    )
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "blocker_ref_invalid"
    assert "type must be one of" in err["reason"]


def test_force_unstructured_defer_override(running_daemon, cap_gate_passes):
    """X-Mind-Force-Unstructured-Defer passes the gate AND writes the
    audit ledger to world/blocker-gate-overrides.jsonl."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    code, _ = _update(
        port, g["id"], "defer_reason",
        "blocked on something the framework can't reference",
        headers={"X-Mind-Force-Unstructured-Defer":
                 "genuinely no structured signal available"},
    )
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["defer_reason"].startswith("blocked on something")
    # blocker_ref must NOT be set when the override path was used
    assert "blocker_ref" not in persisted or persisted.get("blocker_ref") is None
    # Audit ledger written
    ledger = project_root / "world" / "blocker-gate-overrides.jsonl"
    assert ledger.exists()
    rec = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert rec["goal_id"] == g["id"]
    assert "genuinely no structured signal available" in rec["justification"]


def test_structured_defer_skips_blocker_ref_requirement(running_daemon):
    """Circuit breaker: prefix → no gate, no blocker_ref needed."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    code, _ = _update(port, g["id"], "defer_reason",
                      "Circuit breaker: 3 consecutive failures")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["defer_reason"] == "Circuit breaker: 3 consecutive failures"


# ---------------------------------------------------------------------------
# deferred_until — auto-extraction from narrative
# ---------------------------------------------------------------------------

def test_deferred_until_auto_extracted_from_iso_date(running_daemon, cap_gate_passes):
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "external-service", "external_id": "probe-x"})
    code, _ = _update(
        port, g["id"], "defer_reason",
        "Not before 2099-07-14",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["deferred_until"] == "2099-07-14T00:00:00"


def test_deferred_until_caller_supplied_wins(running_daemon, cap_gate_passes):
    """If goal already has deferred_until, narrative extraction must not
    overwrite it (caller-supplied value wins)."""
    project_root, port = running_daemon
    g = _seed_goal(port, deferred_until="2099-01-01T00:00:00")
    ref = json.dumps({"type": "external-service", "external_id": "probe-y"})
    code, _ = _update(
        port, g["id"], "defer_reason",
        "Not before 2099-07-14",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["deferred_until"] == "2099-01-01T00:00:00"


def test_deferred_until_unmatched_text_leaves_field_unset(running_daemon, cap_gate_passes):
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "user_action", "external_id": "z"})
    code, _ = _update(
        port, g["id"], "defer_reason",
        "blocked on something with no date in it",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    # No date in text → no auto-pairing
    assert persisted.get("deferred_until") in (None, "")


# ---------------------------------------------------------------------------
# defer_reason CLEAR path — defer_reason_set_at clear + blocker_ref drop
# ---------------------------------------------------------------------------

def test_defer_reason_clear_drops_blocker_ref(running_daemon, cap_gate_passes):
    """Setting defer_reason=null must drop blocker_ref and clear set_at."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    # First, set a defer with a blocker_ref
    ref = json.dumps({"type": "user_action", "external_id": "x"})
    code, _ = _update(port, g["id"], "defer_reason", "blocked on foo",
                      headers={"X-Mind-Blocker-Ref": ref})
    assert code == 200
    # Now clear it
    code, _ = _update(port, g["id"], "defer_reason", None)
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted.get("defer_reason") in (None, "")
    assert persisted["defer_reason_set_at"] is None
    assert "blocker_ref" not in persisted or persisted.get("blocker_ref") is None


def test_defer_reason_clear_with_empty_string_drops_blocker_ref(running_daemon, cap_gate_passes):
    """Empty string == None for the clear path (legacy CLI semantics)."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "user_action", "external_id": "x"})
    _update(port, g["id"], "defer_reason", "blocked on foo",
            headers={"X-Mind-Blocker-Ref": ref})
    code, _ = _update(port, g["id"], "defer_reason", "")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted.get("defer_reason") in (None, "")
    assert persisted["defer_reason_set_at"] is None
    assert "blocker_ref" not in persisted or persisted.get("blocker_ref") is None


# ---------------------------------------------------------------------------
# Dotted field — 400 invalid_field (PR 7g)
# ---------------------------------------------------------------------------

def test_dotted_field_returns_400(running_daemon):
    """Dotted field name is rejected pre-lock with a canonical error.
    Prevents literal "verification.outcomes" string key corruption."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(port, g["id"], "verification.outcomes", ["ok"])
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "dotted_field_rejected"
    assert "dotted field name" in err["detail"]


def test_dotted_field_does_not_mutate_goal(running_daemon):
    """The 400 must fire BEFORE any write — no literal-string key persists."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    _update(port, g["id"], "verification.outcomes", ["ok"])
    persisted = _read_goal(project_root, g["id"])
    assert "verification.outcomes" not in persisted


# ---------------------------------------------------------------------------
# recurring=false cascade — drops interval_hours + lastAchievedAt (PR 7g)
# ---------------------------------------------------------------------------

def test_recurring_false_drops_interval_hours_and_last_achieved(running_daemon):
    """recurring=false must pop the recurring-shape fields at the data
    primitive so goal-selector's `hours_since(lastAchievedAt) < interval_hours`
    doesn't keep the dead goal alive between archive sweeps."""
    project_root, port = running_daemon
    g = _seed_goal(port, recurring=True, interval_hours=24,
                   lastAchievedAt="2026-05-12T08:00:00")
    code, body = _update(port, g["id"], "recurring", False)
    assert code == 200, body
    persisted = _read_goal(project_root, g["id"])
    assert persisted["recurring"] is False
    assert "interval_hours" not in persisted
    assert "lastAchievedAt" not in persisted


def test_recurring_true_preserves_interval_hours(running_daemon):
    """recurring=true must NOT drop interval_hours — cascade fires only on
    falsy."""
    project_root, port = running_daemon
    g = _seed_goal(port, recurring=False, interval_hours=24)
    code, _ = _update(port, g["id"], "recurring", True)
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["recurring"] is True
    assert persisted["interval_hours"] == 24


def test_recurring_false_preserves_history_fields(running_daemon):
    """achievedCount / currentStreak / longestStreak are factual record —
    preserved on the cascade. Only the future-scheduling fields drop."""
    project_root, port = running_daemon
    g = _seed_goal(port, recurring=True, interval_hours=24,
                   lastAchievedAt="2026-05-12T08:00:00",
                   achievedCount=5, currentStreak=2, longestStreak=4)
    code, _ = _update(port, g["id"], "recurring", False)
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted.get("achievedCount") == 5
    assert persisted.get("currentStreak") == 2
    assert persisted.get("longestStreak") == 4


# ---------------------------------------------------------------------------
# blocked_by → blocked_since cascade (PR 7g)
# ---------------------------------------------------------------------------

def test_blocked_by_set_stamps_blocked_since(running_daemon):
    """Non-empty blocked_by stamps blocked_since when previously unset."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    assert g.get("blocked_since") is None
    code, _ = _update(port, g["id"], "blocked_by", ["g-001-99"])
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["blocked_by"] == ["g-001-99"]
    assert "blocked_since" in persisted
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                    persisted["blocked_since"])


def test_blocked_by_set_preserves_existing_blocked_since(running_daemon):
    """If blocked_since is already set, the cascade must NOT overwrite it
    (the 'how long has this been blocked?' signal would lose history)."""
    project_root, port = running_daemon
    g = _seed_goal(port, blocked_since="2026-01-01T00:00:00")
    code, _ = _update(port, g["id"], "blocked_by", ["g-001-99"])
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["blocked_since"] == "2026-01-01T00:00:00"


def test_blocked_by_clear_nulls_blocked_since(running_daemon):
    """Empty blocked_by nulls blocked_since (no longer blocked → no since)."""
    project_root, port = running_daemon
    g = _seed_goal(port, blocked_by=["g-001-99"],
                   blocked_since="2026-05-10T00:00:00")
    code, _ = _update(port, g["id"], "blocked_by", [])
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["blocked_by"] == []
    assert persisted["blocked_since"] is None


# ---------------------------------------------------------------------------
# participants → user_leg_scope advisory (PR 7h)
# ---------------------------------------------------------------------------

def test_participants_with_user_no_scope_warns(running_daemon):
    """participants includes 'user' but goal has no user_leg_scope → warning."""
    _, port = running_daemon
    g = _seed_goal(port)
    assert g.get("user_leg_scope") in (None, "")
    code, body = _update(port, g["id"], "participants", ["agent", "user"])
    assert code == 200
    resp = json.loads(body)
    assert "warnings" in resp
    assert any("user_leg_scope" in w for w in resp["warnings"])
    # Goal still got updated — advisory is warn-only
    assert resp["goal"]["participants"] == ["agent", "user"]


def test_participants_with_user_and_scope_no_warn(running_daemon):
    """participants includes 'user' AND goal has user_leg_scope → no warning."""
    _, port = running_daemon
    g = _seed_goal(port, user_leg_scope="commit")
    code, body = _update(port, g["id"], "participants", ["agent", "user"])
    assert code == 200
    resp = json.loads(body)
    assert "warnings" not in resp or not resp["warnings"]


def test_participants_without_user_no_warn(running_daemon):
    """participants without 'user' → no advisory regardless of scope."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(port, g["id"], "participants", ["agent"])
    assert code == 200
    resp = json.loads(body)
    assert "warnings" not in resp or not resp["warnings"]


def test_other_field_writes_omit_warnings_key(running_daemon):
    """Non-participants field writes don't emit warnings key — the
    response stays compact for the common case."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(port, g["id"], "priority", "HIGH")
    assert code == 200
    resp = json.loads(body)
    assert "warnings" not in resp


# ===========================================================================
# PR 7i: status guards (3)
# ===========================================================================

def test_status_superseded_blocked_pre_lock(running_daemon):
    """status=superseded must go through aspirations-complete-intent.sh,
    not direct update_goal. Pre-lock guard returns 400."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(port, g["id"], "status", "superseded")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "invalid_status_transition"
    assert "superseded" in err["detail"]
    assert "intent_satisfaction" in err["detail"]


def test_status_completed_on_recurring_blocked(running_daemon):
    """Recurring goals must never reach status=completed. In-lock guard."""
    _, port = running_daemon
    g = _seed_goal(port, recurring=True, interval_hours=24)
    code, body = _update(port, g["id"], "status", "completed")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "invalid_status_transition"
    assert "recurring" in err["detail"]
    assert "complete-by" in err["detail"]


def test_status_completed_on_non_recurring_passes(running_daemon):
    """Sanity: non-recurring goal CAN go to completed (guard scopes correctly)."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    code, _ = _update(port, g["id"], "status", "completed")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["status"] == "completed"


def test_status_blocked_requires_evidence(running_daemon):
    """status=blocked with no blocker_ref / no blocked_by / no header → 400."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(port, g["id"], "status", "blocked")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "blocker_ref_required_for_blocked_status"


def test_status_blocked_accepts_x_ayoai_blocker_ref_header(running_daemon):
    """X-Mind-Blocker-Ref header satisfies the evidence requirement and
    the validated ref persists on the goal."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "infrastructure", "external_id": "k8s-namespace-x"})
    code, _ = _update(
        port, g["id"], "status", "blocked",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["status"] == "blocked"
    assert persisted["blocker_ref"]["type"] == "infrastructure"
    assert persisted["blocker_ref"]["external_id"] == "k8s-namespace-x"


def test_status_blocked_accepts_existing_blocker_ref(running_daemon):
    """If blocker_ref is already populated (from a prior defer write), the
    transition into blocked passes without a header."""
    project_root, port = running_daemon
    g = _seed_goal(port, blocker_ref={"type": "user_action",
                                       "external_id": "rfc-42"})
    code, _ = _update(port, g["id"], "status", "blocked")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["status"] == "blocked"
    # Existing ref preserved
    assert persisted["blocker_ref"]["external_id"] == "rfc-42"


def test_status_blocked_accepts_non_empty_blocked_by(running_daemon):
    """Non-empty blocked_by is sufficient evidence — goal-chain deps."""
    project_root, port = running_daemon
    g = _seed_goal(port, blocked_by=["g-001-99"])
    code, _ = _update(port, g["id"], "status", "blocked")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["status"] == "blocked"


def test_status_blocked_invalid_ref_header(running_daemon):
    """X-Mind-Blocker-Ref with bad JSON or bad type → 400 blocker_ref_invalid."""
    _, port = running_daemon
    g = _seed_goal(port)
    code, body = _update(
        port, g["id"], "status", "blocked",
        headers={"X-Mind-Blocker-Ref": '{"type":"made-up","external_id":"x"}'},
    )
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "blocker_ref_invalid"


def test_status_blocked_idempotent_re_write(running_daemon):
    """status=blocked → status=blocked (already blocked) is NOT a transition,
    so the requirement check doesn't fire."""
    project_root, port = running_daemon
    g = _seed_goal(port, blocked_by=["g-001-99"])
    # First write: transitions to blocked, satisfies via blocked_by
    code, _ = _update(port, g["id"], "status", "blocked")
    assert code == 200
    # Second write: already blocked → no requirement check; passes without
    # needing fresh evidence
    code, _ = _update(port, g["id"], "status", "blocked")
    assert code == 200


# ===========================================================================
# PR 7i: status cascades (6)
# ===========================================================================

def test_status_in_progress_bumps_selection_count(running_daemon):
    """Transition into in-progress bumps asp.selection_count + last_selected."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    # Read pre-update asp state
    live = project_root / "world" / "aspirations.jsonl"
    asp_before = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
    sc_before = int(asp_before.get("selection_count", 0) or 0)

    code, _ = _update(port, g["id"], "status", "in-progress")
    assert code == 200
    asp_after = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
    assert asp_after["selection_count"] == sc_before + 1
    assert "last_selected" in asp_after


def test_status_in_progress_idempotent_on_redundant_write(running_daemon):
    """Resume/retry writes of the same in-progress goal must NOT inflate
    selection_count (old_status != "in-progress" guard)."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    _update(port, g["id"], "status", "in-progress")
    live = project_root / "world" / "aspirations.jsonl"
    sc_after_first = json.loads(
        live.read_text(encoding="utf-8").splitlines()[0])["selection_count"]

    # Redundant write — same status
    _update(port, g["id"], "status", "in-progress")
    sc_after_second = json.loads(
        live.read_text(encoding="utf-8").splitlines()[0])["selection_count"]
    assert sc_after_first == sc_after_second


def test_status_terminal_stamps_completed_at(running_daemon):
    """Terminal status stamps completed_at when previously None."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    assert g.get("completed_at") in (None, "")
    code, _ = _update(port, g["id"], "status", "completed")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["completed_at"] is not None
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                    persisted["completed_at"])


def test_status_terminal_preserves_existing_completed_at(running_daemon):
    """If completed_at is already set (back-stamp from external backfill),
    the cascade must not overwrite it."""
    project_root, port = running_daemon
    g = _seed_goal(port, completed_at="2026-01-01T00:00:00")
    code, _ = _update(port, g["id"], "status", "completed")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert persisted["completed_at"] == "2026-01-01T00:00:00"


def test_status_blocked_auto_stamps_blocked_since(running_daemon):
    """Transition into blocked stamps blocked_since when previously unset."""
    project_root, port = running_daemon
    g = _seed_goal(port, blocked_by=["g-001-99"])
    assert g.get("blocked_since") in (None, "") or g.get("blocked_since")
    # Clear blocked_since to test stamping
    pre_state = _read_goal(project_root, g["id"])
    # blocked_by was set during seed → blocked_since was stamped already
    # by the blocked_by cascade. For this test we want a clean transition,
    # so seed without blocked_by and use the header path instead.
    g2 = _seed_goal(port)  # fresh goal, no blocker_since
    ref = json.dumps({"type": "infrastructure", "external_id": "infra-y"})
    code, _ = _update(port, g2["id"], "status", "blocked",
                       headers={"X-Mind-Blocker-Ref": ref})
    assert code == 200
    persisted = _read_goal(project_root, g2["id"])
    assert "blocked_since" in persisted
    assert persisted["blocked_since"] is not None


def test_status_terminal_clears_stale_blockers(running_daemon):
    """When a goal goes terminal, OTHER goals listing it in blocked_by
    must drop the reference."""
    project_root, port = running_daemon
    blocker = _seed_goal(port, title="Blocker")
    dependent = _seed_goal(port, title="Dependent",
                           blocked_by=[blocker["id"]])

    # Complete the blocker
    code, _ = _update(port, blocker["id"], "status", "completed")
    assert code == 200
    # Dependent's blocked_by should now be empty, blocked_since nulled
    dep_after = _read_goal(project_root, dependent["id"])
    assert dep_after["blocked_by"] == []
    assert dep_after["blocked_since"] is None


def test_status_terminal_clears_claim(running_daemon):
    """When a goal goes terminal, claimed_by + claimed_at are popped."""
    project_root, port = running_daemon
    g = _seed_goal(port,
                   claimed_by="alpha",
                   claimed_at="2026-05-12T10:00:00")
    code, _ = _update(port, g["id"], "status", "completed")
    assert code == 200
    persisted = _read_goal(project_root, g["id"])
    assert "claimed_by" not in persisted
    assert "claimed_at" not in persisted


def test_recompute_progress_fires_on_every_write(running_daemon):
    """recompute_progress runs on every successful update_goal write,
    not just status changes. After a completed write the asp's progress
    counts a non-zero completed_goals."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    # Initial state — no completed goals
    live = project_root / "world" / "aspirations.jsonl"
    asp = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
    assert asp["progress"]["completed_goals"] == 0

    # Update a non-status field — recompute still fires (no-op recompute,
    # progress unchanged but the write path executed)
    code, _ = _update(port, g["id"], "priority", "HIGH")
    assert code == 200
    asp = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
    assert "progress" in asp
    assert asp["progress"]["completed_goals"] == 0
    assert asp["progress"]["total_goals"] == 1

    # Complete the goal — progress shifts
    _update(port, g["id"], "status", "completed")
    asp = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
    assert asp["progress"]["completed_goals"] == 1


# ===========================================================================
# PR 7i: E9 skip observation (post-lock subprocess)
# ===========================================================================

def test_e9_fires_on_skipped_status(running_daemon):
    """status=skipped triggers E9 — sensory_buffer append in working memory."""
    project_root, port = running_daemon
    g = _seed_goal(
        port,
        title="A clearly substantive goal title",
        description=("This goal has a long enough description (40+ chars) "
                     "to clear the trivial-skip filter."),
    )
    wm_path = project_root / "agents" / "alpha" / "session" / "working-memory.yaml"
    pre_text = wm_path.read_text(encoding="utf-8")

    code, _ = _update(port, g["id"], "status", "skipped")
    assert code == 200
    post_text = wm_path.read_text(encoding="utf-8")
    # E9 appended a sensory_buffer observation (working memory grew)
    assert len(post_text) > len(pre_text), (
        "E9 observation should have appended to working-memory.yaml. "
        f"Before: {len(pre_text)} chars; after: {len(post_text)} chars."
    )


def test_e9_fires_on_expired_status(running_daemon):
    """status=expired ALSO triggers E9 (same encoding lane as skipped)."""
    project_root, port = running_daemon
    g = _seed_goal(
        port,
        title="Another substantive goal for the expired path test",
        description=("Has enough description characters to clear the "
                     "trivial-skip filter at 40+ chars."),
    )
    wm_path = project_root / "agents" / "alpha" / "session" / "working-memory.yaml"
    pre_text = wm_path.read_text(encoding="utf-8")

    code, _ = _update(port, g["id"], "status", "expired")
    assert code == 200
    post_text = wm_path.read_text(encoding="utf-8")
    assert len(post_text) > len(pre_text)


def test_e9_skips_trivial_goals(running_daemon):
    """Goals with short title (<30) AND short description (<40) are too
    mechanical for tree encoding — E9 skips them entirely."""
    project_root, port = running_daemon
    g = _seed_goal(port, title="Tiny", description="x")
    wm_path = project_root / "agents" / "alpha" / "session" / "working-memory.yaml"
    pre_text = wm_path.read_text(encoding="utf-8")

    code, _ = _update(port, g["id"], "status", "skipped")
    assert code == 200
    post_text = wm_path.read_text(encoding="utf-8")
    # No E9 append for trivial goal
    assert post_text == pre_text


def test_e9_does_not_fire_on_completed_status(running_daemon):
    """Only skipped/expired transitions trigger E9 — completed transitions
    do not (they have their own encoding lane via state-update Phase 8)."""
    project_root, port = running_daemon
    g = _seed_goal(
        port,
        title="A substantive title that clears the 30-char trivial cutoff",
        description=("Description long enough to clear the 40-char cutoff "
                     "for the trivial filter."),
    )
    wm_path = project_root / "agents" / "alpha" / "session" / "working-memory.yaml"
    pre_text = wm_path.read_text(encoding="utf-8")

    code, _ = _update(port, g["id"], "status", "completed")
    assert code == 200
    post_text = wm_path.read_text(encoding="utf-8")
    assert post_text == pre_text


# ---------------------------------------------------------------------------
# PR 7j — daemon-side Layer-D auto-Unblock filing
# ---------------------------------------------------------------------------
#
# Replaces the legacy wrapper fallback. When the daemon's capability gate
# refuses a defer_reason that names an agent-provisionable capability AND
# the gate suggests an Unblock, the daemon now files the Unblock atomically
# (under the live aspirations.jsonl lock) and surfaces filed_unblock_id in
# the 400 response body. Wrapper no longer falls back to legacy CLI.


def _layer_d_gate_stub(suggested: bool = True):
    """Build a fake cap_result that simulates a would_block + suggest result.

    Lets the tests below exercise the filing path without depending on the
    forged-skill registry (which can drift, and isn't the unit under test).
    """
    base = {
        "would_block": True,
        "matches": [{
            "skill": "/notify-user",
            "matched_keyword": "user-feedback",
            "row": "notify the user about <event>",
        }],
        "match_count": 1,
    }
    if suggested:
        base["unblock_suggested"] = True
        base["unblock_title"] = "Unblock: notify for {goal_id}"
        base["unblock_description"] = (
            "Defer-gate refused defer_reason — capability-routing matched "
            "an agent-provisionable action.")
    else:
        base["unblock_suggested"] = False
    return base


def test_layer_d_files_unblock_inline(running_daemon, aspirations_write_module,
                                      monkeypatch):
    """Capability gate blocks + suggests an Unblock → daemon files it under
    the live aspirations.jsonl lock and returns 400 with filed_unblock_id.

    No wrapper fallback runs. The original defer_reason is NEVER written
    (original goal stays pending).
    """
    project_root, port = running_daemon
    g = _seed_goal(port)

    def _stub_cap(value, **kw):
        gid = kw.get("for_goal_id") or "<unknown>"
        result = _layer_d_gate_stub(suggested=True)
        result["unblock_title"] = f"Unblock: notify for {gid}"
        return result

    monkeypatch.setattr(aspirations_write_module, "_capability_eval", _stub_cap)

    code, body = _update(port, g["id"], "defer_reason",
                         "blocked on user feedback")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "capability_blocked"
    assert err["filed_unblock_id"] is not None
    assert err["filed_unblock_id"].startswith("g-")
    assert "Filed Unblock" in err["unblock_filing_status"]
    assert err["unblock_routing_strategy"] in (
        "asp-001-current-source", "original-parent-asp", "first-active-asp",
    )

    # Original goal stayed pending (defer was refused).
    persisted_original = _read_goal(project_root, g["id"])
    assert persisted_original.get("status") == "pending"
    assert persisted_original.get("defer_reason") in (None, "")

    # Filed Unblock landed on disk with the canonical origin_signal.
    filed_goal = _read_goal(project_root, err["filed_unblock_id"])
    assert filed_goal is not None
    assert filed_goal["origin_signal"] == f"unblock:{g['id']}"
    assert filed_goal["status"] == "pending"
    assert filed_goal["title"].startswith("Unblock:")
    assert "defer-gate-routed" in filed_goal.get("tags", [])


def test_layer_d_dedup_skips_when_existing_unblock(running_daemon,
                                                   aspirations_write_module,
                                                   monkeypatch):
    """Second defer attempt against the same goal must NOT file a duplicate.
    Dedup matches via origin_signal=unblock:<gid> — strategy (a)."""
    project_root, port = running_daemon
    g = _seed_goal(port)

    def _stub_cap(value, **kw):
        gid = kw.get("for_goal_id") or "<unknown>"
        result = _layer_d_gate_stub(suggested=True)
        result["unblock_title"] = f"Unblock: notify for {gid}"
        return result

    monkeypatch.setattr(aspirations_write_module, "_capability_eval", _stub_cap)

    # First attempt — Unblock filed.
    code1, body1 = _update(port, g["id"], "defer_reason",
                           "blocked on user feedback")
    assert code1 == 400
    first_id = json.loads(body1)["filed_unblock_id"]
    assert first_id is not None

    # Second attempt — same goal, same gate block. Dedup must skip.
    code2, body2 = _update(port, g["id"], "defer_reason",
                           "blocked on user feedback again")
    assert code2 == 400
    err = json.loads(body2)
    assert err["filed_unblock_id"] is None
    assert "idempotent skip" in err["unblock_filing_status"]


def test_layer_d_no_suggestion_no_unblock_filed(running_daemon,
                                                aspirations_write_module,
                                                monkeypatch):
    """Capability gate blocks but emits no unblock_suggested → daemon does
    NOT file. Response carries filed_unblock_id=None (the cap_block_for_
    layer_d branch is bypassed entirely)."""
    project_root, port = running_daemon
    g = _seed_goal(port)

    def _stub_cap(value, **kw):
        return _layer_d_gate_stub(suggested=False)

    monkeypatch.setattr(aspirations_write_module, "_capability_eval", _stub_cap)

    code, body = _update(port, g["id"], "defer_reason",
                         "blocked on something the gate matches "
                         "but does not suggest unblock for")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "capability_blocked"
    # When unblock_suggested=False, daemon never even attempts filing —
    # the response body carries no filed_unblock_id key.
    assert "filed_unblock_id" not in err


def test_layer_d_emits_gate_firing_telemetry(running_daemon,
                                             aspirations_write_module,
                                             monkeypatch, tmp_path):
    """Filed Unblock writes a `capability-gate-layer-d` record into the
    calling agent's gate-firings.jsonl. Tests the new _gate_log meta_dir
    override path."""
    project_root, port = running_daemon
    #  made _gate_log.log() a silent no-op under PYTEST_CURRENT_TEST
    # unless GATE_LOG_ALLOW_PYTEST is set (synthetic-firing pollution guard).
    # This test POSITIVELY asserts on the firing record and its destination is
    # already the hermetic fixture repo (ctx.paths.meta), so opt in — the
    # in-process daemon shares this env (0; twin of the opt-in
    #  itself added to test_layer_d_telemetry.py).
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    g = _seed_goal(port)

    def _stub_cap(value, **kw):
        gid = kw.get("for_goal_id") or "<unknown>"
        result = _layer_d_gate_stub(suggested=True)
        result["unblock_title"] = f"Unblock: notify for {gid}"
        return result

    monkeypatch.setattr(aspirations_write_module, "_capability_eval", _stub_cap)

    firings_path = project_root / "meta" / "gate-firings.jsonl"
    pre_existing = firings_path.exists()
    pre_size = firings_path.stat().st_size if pre_existing else 0

    code, body = _update(port, g["id"], "defer_reason",
                         "blocked on user feedback")
    assert code == 400
    filed_id = json.loads(body)["filed_unblock_id"]
    assert filed_id is not None

    assert firings_path.exists()
    # Byte-slice off pre-existing records: pre_size is stat().st_size (raw
    # bytes) and on Windows this file is CRLF-terminated. read_text's
    # universal-newline translation collapses CRLF->LF, desyncing the offset by
    # one byte per pre-existing line and dropping the new record's leading '{'
    # (json.loads then fails "Extra data" on the headless line). Slice the raw
    # bytes, then decode — matches the read_bytes() approach the tree
    # byte-compat tests already use. 9.
    new_bytes = firings_path.read_bytes()
    if pre_size:
        new_bytes = new_bytes[pre_size:]
    new_records = [json.loads(line) for line in new_bytes.decode("utf-8").splitlines()
                   if line.strip()]
    matching = [r for r in new_records
                if r.get("gate_id") == "capability-gate-layer-d"]
    assert len(matching) >= 1
    rec = matching[-1]
    assert rec["decision"] == "block"
    assert rec["extra"]["filed_unblock_id"] == filed_id
    assert rec["extra"]["original_goal_id"] == g["id"]
    assert rec["extra"]["source"] == "daemon"


# ---------------------------------------------------------------------------
# PR 7j housekeeping — defer-date audit log on daemon path
# ---------------------------------------------------------------------------


def test_defer_date_extraction_writes_audit_log(running_daemon,
                                                cap_gate_passes):
    """Narrative→deferred_until auto-pair must append one record to
    world/defer-date-extractions.jsonl. Convention requires it — see
    core/config/conventions/goal-schemas.md "Auto-pairing"."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "external-service", "external_id": "probe-99"})

    audit_path = project_root / "world" / "defer-date-extractions.jsonl"
    pre_size = audit_path.stat().st_size if audit_path.exists() else 0

    code, _ = _update(
        port, g["id"], "defer_reason",
        "Not before 2099-08-12",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200

    assert audit_path.exists()
    new_text = audit_path.read_text(encoding="utf-8")
    if pre_size:
        new_text = new_text[pre_size:]
    records = [json.loads(line) for line in new_text.splitlines()
               if line.strip()]
    matching = [r for r in records if r.get("goal_id") == g["id"]]
    assert len(matching) == 1
    rec = matching[0]
    assert rec["extracted_deferred_until"] == "2099-08-12T00:00:00"
    assert "Not before 2099-08-12" in rec["defer_reason"]


def test_defer_date_no_extraction_no_audit_log_entry(running_daemon,
                                                     cap_gate_passes):
    """When narrative has no date, no record is appended."""
    project_root, port = running_daemon
    g = _seed_goal(port)
    ref = json.dumps({"type": "user_action", "external_id": "no-date"})

    audit_path = project_root / "world" / "defer-date-extractions.jsonl"
    pre_size = audit_path.stat().st_size if audit_path.exists() else 0

    code, _ = _update(
        port, g["id"], "defer_reason",
        "blocked on something with no date",
        headers={"X-Mind-Blocker-Ref": ref},
    )
    assert code == 200
    post_size = audit_path.stat().st_size if audit_path.exists() else 0
    assert post_size == pre_size  # No append


def test_recurring_seed_without_offload_decision_blocked(running_daemon):
    """mc-066 gate-shape pin (0 companion): a recurring goal filed
    WITHOUT an offload_decision must 400 with operator_offload_blocked.

    Seeds via raw _post — deliberately bypassing _seed_goal's fixture
    injection — so this test breaks loudly if the Phase E.5 gate is ever
    removed/renamed (the _seed_goal injection would then be dead weight) or
    if its error contract changes shape.
    """
    project_root, port = running_daemon
    goal = {
        "title": "Recurring seed missing offload decision",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,
        "recurring": True,
        "interval_hours": 24,
    }
    code, resp_body = _post(port, "/v1/aspirations/add-goal",
                            {"asp_id": "asp-001", "source": "world"},
                            json.dumps(goal).encode("utf-8"))
    assert code == 400, resp_body
    err = json.loads(resp_body)
    assert err["error"] == "operator_offload_blocked"
    assert err["gate"] == "operator-offload-gate"
    assert err["gate_output"]["would_block"] is True
    # ...and the SAME goal WITH a decision passes the gate:
    goal["offload_decision"] = "stays-mind: test fixture"
    code, resp_body = _post(port, "/v1/aspirations/add-goal",
                            {"asp_id": "asp-001", "source": "world"},
                            json.dumps(goal).encode("utf-8"))
    assert code == 200, resp_body
