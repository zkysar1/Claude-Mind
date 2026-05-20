"""Daemon endpoint -> gate wiring tests (PR 7b).

These verify the daemon CALLS each gate with the right inputs and HONORS its
verdict. They do NOT re-test the gate logic — that lives in core/tests/gates/.

We monkeypatch the gate references bound at module load in
mind_api.src.endpoints.aspirations_write so we can deterministically force
block/pass/override outcomes regardless of repo state or filesystem fixtures
(which can't easily simulate dirty git, partner in_flight claims, or
agent-provisionable capability matches).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(port: int, path: str, query: dict, body: bytes,
          *, agent: str = "alpha", headers: dict | None = None):
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


@pytest.fixture
def aspirations_write_module():
    """Direct handle to the daemon's aspirations_write module so we can
    monkeypatch the gate references bound at import time."""
    from mind_api.src.endpoints import aspirations_write
    return aspirations_write


def _add_goal(port, goal, **kwargs):
    return _post(port, "/v1/aspirations/add-goal",
                 {"asp_id": "asp-001", "source": "world"},
                 json.dumps(goal).encode("utf-8"), **kwargs)


def _update_goal(port, goal_id, field, value, **kwargs):
    return _post(port, "/v1/aspirations/update-goal",
                 {"id": goal_id, "field": field, "source": "world"},
                 json.dumps(value).encode("utf-8"), **kwargs)


# ---------------------------------------------------------------------------
# origin-signal gate
# ---------------------------------------------------------------------------

def test_origin_signal_blocks_when_agent_lacks_signal(running_daemon):
    """Agent-sourced goal without origin_signal must be blocked. Uses the
    REAL gate (no monkeypatch) so we verify import + invocation, not just
    the daemon's plumbing."""
    _, port = running_daemon
    goal = {"title": "Missing signal", "status": "pending"}
    code, body = _add_goal(port, goal, agent="alpha")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "origin_signal_blocked"
    assert err["gate"] == "origin-signal-gate"
    assert err["gate_output"]["would_block"] is True


def test_origin_signal_pass_with_valid_signal(running_daemon):
    """user_directive is a canonical valid signal — must pass."""
    _, port = running_daemon
    goal = {"title": "With signal", "status": "pending",
            "origin_signal": "user_directive"}
    code, body = _add_goal(port, goal, agent="alpha")
    assert code == 200
    assert json.loads(body)["ok"] is True


def test_origin_signal_override_header(running_daemon):
    """X-Mind-Override-Signal bypasses the block and writes to the audit
    log (audit-log assertion is out of scope — the gate's own tests cover
    it). Verified here: 200 response despite missing signal."""
    _, port = running_daemon
    goal = {"title": "Override path", "status": "pending"}
    code, body = _add_goal(
        port, goal,
        headers={"X-Mind-Override-Signal": "explicit justification"},
    )
    assert code == 200
    assert json.loads(body)["ok"] is True


def test_origin_signal_auto_derive_unblock_prefix(running_daemon):
    """Title 'Unblock: X' triggers Layer-D auto-derive. Goal must land with
    its origin_signal patched to 'unblock:<slug>' (gate-computed)."""
    project_root, port = running_daemon
    goal = {"title": "Unblock: efs reconnect", "status": "pending"}
    code, body = _add_goal(port, goal, agent="alpha")
    assert code == 200

    # Verify the persisted record was patched
    live = project_root / "world" / "aspirations.jsonl"
    items = [json.loads(l) for l in live.read_text(encoding="utf-8").splitlines() if l.strip()]
    new_goal = next(g for asp in items if asp["id"] == "asp-001"
                    for g in asp["goals"] if g["title"] == "Unblock: efs reconnect")
    sig = new_goal.get("origin_signal", "")
    assert sig.startswith("unblock:"), f"expected auto-derived unblock signal, got {sig!r}"


# ---------------------------------------------------------------------------
# goal-duplication gate
# ---------------------------------------------------------------------------

def test_goal_duplication_blocks_when_gate_says_block(running_daemon,
                                                     aspirations_write_module,
                                                     monkeypatch):
    """Force the goal-duplication gate to return would_block=True and verify
    the daemon honors it. Doesn't test gate's own decision logic — that's
    in core/tests/gates/test_goal_duplication_gate.py."""
    _, port = running_daemon

    fake = {"would_block": True,
            "blocking_checks": [{"check": "test", "reason": "synthetic"}],
            "passing_checks": []}
    monkeypatch.setattr(aspirations_write_module,
                        "_goal_duplication_eval",
                        lambda *a, **kw: fake)

    goal = {"title": "Should be blocked", "status": "pending",
            "origin_signal": "user_directive"}
    code, body = _add_goal(port, goal, agent="alpha")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "goal_duplication_blocked"
    assert err["gate"] == "goal-duplication-gate"
    assert err["gate_output"] == fake


def test_goal_duplication_override_header_forwarded(running_daemon,
                                                   aspirations_write_module,
                                                   monkeypatch):
    """X-Mind-Override-Duplication value must be forwarded to the gate as
    override_duplication kwarg. We capture the call and assert."""
    _, port = running_daemon
    captured = {}

    def _capture(goal, **kw):
        captured.update(kw)
        return {"would_block": False, "blocking_checks": [], "passing_checks": []}

    monkeypatch.setattr(aspirations_write_module,
                        "_goal_duplication_eval",
                        _capture)

    goal = {"title": "Override goal", "status": "pending",
            "origin_signal": "user_directive"}
    code, _ = _add_goal(
        port, goal,
        headers={"X-Mind-Override-Duplication": "intentional overlap"},
    )
    assert code == 200
    assert captured.get("override_duplication") == "intentional overlap"


# ---------------------------------------------------------------------------
# uncommitted-work gate (update-goal status -> completed)
# ---------------------------------------------------------------------------

def _seed_goal(port):
    """Seed a goal we can update in update_goal tests."""
    goal = {"id": "g-001-50", "title": "Seed",
            "status": "pending", "origin_signal": "user_directive"}
    code, _ = _add_goal(port, goal, agent="alpha")
    assert code == 200


def test_uncommitted_work_only_fires_on_completed(running_daemon,
                                                  aspirations_write_module,
                                                  monkeypatch):
    """Setting status to in-progress (not completed) must NOT call the
    uncommitted-work gate."""
    _, port = running_daemon
    _seed_goal(port)

    called = {"n": 0}

    def _track(*a, **kw):
        called["n"] += 1
        return {"would_block": False, "dirty_framework_files": []}

    monkeypatch.setattr(aspirations_write_module,
                        "_uncommitted_work_eval", _track)

    code, _ = _update_goal(port, "g-001-50", "status", "in-progress")
    assert code == 200
    assert called["n"] == 0, "uncommitted gate must not fire for non-completed transitions"


def test_uncommitted_work_blocks_completed_when_dirty(running_daemon,
                                                     aspirations_write_module,
                                                     monkeypatch):
    """Force the gate to report dirty files; daemon must return 400 with
    gate_output."""
    _, port = running_daemon
    _seed_goal(port)

    fake = {"would_block": True,
            "dirty_framework_files": ["core/scripts/foo.py"],
            "repo_path": "/x", "goal_id": "g-001-50",
            "override_applied": None}
    monkeypatch.setattr(aspirations_write_module,
                        "_uncommitted_work_eval",
                        lambda **kw: fake)

    code, body = _update_goal(port, "g-001-50", "status", "completed")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "uncommitted_work_blocked"
    assert err["gate"] == "uncommitted-work-gate"
    assert err["gate_output"] == fake


def test_uncommitted_work_override_header_forwarded(running_daemon,
                                                   aspirations_write_module,
                                                   monkeypatch):
    """X-Mind-Override-Uncommitted must reach the gate as override kwarg."""
    _, port = running_daemon
    _seed_goal(port)
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return {"would_block": False, "dirty_framework_files": []}

    monkeypatch.setattr(aspirations_write_module,
                        "_uncommitted_work_eval", _capture)

    code, _ = _update_goal(
        port, "g-001-50", "status", "completed",
        headers={"X-Mind-Override-Uncommitted": "partner mid-flight"},
    )
    assert code == 200
    assert captured.get("override") == "partner mid-flight"


# ---------------------------------------------------------------------------
# completion-artifact gate (update-goal status -> completed)
# ---------------------------------------------------------------------------

def test_completion_artifact_only_fires_on_completed(running_daemon,
                                                    aspirations_write_module,
                                                    monkeypatch):
    """Setting status to in-progress must NOT call the artifact gate."""
    _, port = running_daemon
    _seed_goal(port)
    called = {"n": 0}

    def _track(**kw):
        called["n"] += 1
        return {"would_block": False, "missing_artifacts": [],
                "near_misses": {}, "checked_paths": 0,
                "goal_id": kw.get("goal_id"), "override_applied": None,
                "skipped_reason": None}

    monkeypatch.setattr(aspirations_write_module,
                        "_completion_artifact_eval", _track)

    code, _ = _update_goal(port, "g-001-50", "status", "in-progress")
    assert code == 200
    assert called["n"] == 0, "artifact gate must not fire for non-completed transitions"


def test_completion_artifact_blocks_when_missing(running_daemon,
                                                 aspirations_write_module,
                                                 monkeypatch):
    """Force the artifact gate to report would_block; daemon must return 400."""
    _, port = running_daemon
    _seed_goal(port)
    # Force uncommitted-work gate to pass — it runs before us.
    monkeypatch.setattr(aspirations_write_module,
                        "_uncommitted_work_eval",
                        lambda **kw: {"would_block": False,
                                       "dirty_framework_files": []})
    fake = {"would_block": True,
            "missing_artifacts": ["core/scripts/missing.sh"],
            "near_misses": {}, "checked_paths": 1,
            "goal_id": "g-001-50", "override_applied": None,
            "skipped_reason": None}
    monkeypatch.setattr(aspirations_write_module,
                        "_completion_artifact_eval",
                        lambda **kw: fake)

    code, body = _update_goal(port, "g-001-50", "status", "completed")
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "missing_artifact_blocked"
    assert err["gate"] == "completion-artifact-gate"
    assert err["gate_output"] == fake


def test_completion_artifact_override_header_forwarded(running_daemon,
                                                      aspirations_write_module,
                                                      monkeypatch):
    """X-Mind-Override-Missing-Artifact reaches the gate as override kwarg."""
    _, port = running_daemon
    _seed_goal(port)
    monkeypatch.setattr(aspirations_write_module,
                        "_uncommitted_work_eval",
                        lambda **kw: {"would_block": False,
                                       "dirty_framework_files": []})
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return {"would_block": False, "missing_artifacts": [],
                "near_misses": {}, "checked_paths": 0,
                "goal_id": kw.get("goal_id"),
                "override_applied": kw.get("override"),
                "skipped_reason": None}

    monkeypatch.setattr(aspirations_write_module,
                        "_completion_artifact_eval", _capture)

    code, _ = _update_goal(
        port, "g-001-50", "status", "completed",
        headers={"X-Mind-Override-Missing-Artifact": "path was renamed"},
    )
    assert code == 200
    assert captured.get("override") == "path was renamed"


# ---------------------------------------------------------------------------
# capability gate (update-goal defer_reason -> non-empty)
# ---------------------------------------------------------------------------

def test_capability_only_fires_on_nonempty_defer_reason(running_daemon,
                                                       aspirations_write_module,
                                                       monkeypatch):
    """defer_reason updates with empty/null value must NOT call the
    capability gate (clearing a defer is always allowed)."""
    _, port = running_daemon
    _seed_goal(port)
    called = {"n": 0}

    def _track(*a, **kw):
        called["n"] += 1
        return {"would_block": False, "reason": "noop"}

    monkeypatch.setattr(aspirations_write_module,
                        "_capability_eval", _track)

    # Set to empty string and to null — both should bypass the gate
    for value in ("", None):
        code, _ = _update_goal(port, "g-001-50", "defer_reason", value)
        assert code == 200, f"clearing defer_reason ({value!r}) should pass"
    assert called["n"] == 0


def test_capability_blocks_when_gate_says_block(running_daemon,
                                                aspirations_write_module,
                                                monkeypatch):
    """Force capability gate to block. Daemon must return 400 with the
    full gate_output (including unblock_suggested if present)."""
    _, port = running_daemon
    _seed_goal(port)

    fake = {
        "would_block": True,
        "matched_capability": {"keyword": "deploy", "skill": "deploy-runtime"},
        "reason": "deploy is agent-provisionable",
        "unblock_suggested": True,
        "unblock_title": "Unblock: deploy for g-001-50",
    }
    monkeypatch.setattr(aspirations_write_module,
                        "_capability_eval", lambda *a, **kw: fake)

    code, body = _update_goal(
        port, "g-001-50", "defer_reason",
        "blocked on user-initiated deploy",
    )
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "capability_blocked"
    assert err["gate"] == "capability-gate"
    assert err["gate_output"] == fake


def test_capability_force_defer_header_forwarded(running_daemon,
                                                 aspirations_write_module,
                                                 monkeypatch):
    """X-Mind-Force-Defer reaches the gate as override_agent_match.

    PR 7e/3 added a separate blocker_ref requirement gate that fires
    after the capability gate. This test focuses on the capability
    gate's header-forwarding contract, so we also pass
    X-Mind-Force-Unstructured-Defer to bypass the downstream gate.
    Without it the request would 400 on blocker_ref_required and the
    capability-gate assertion below would never be reached."""
    _, port = running_daemon
    _seed_goal(port)
    captured = {}

    def _capture(failure_reason, **kw):
        captured["failure_reason"] = failure_reason
        captured.update(kw)
        return {"would_block": False, "reason": "override applied"}

    monkeypatch.setattr(aspirations_write_module,
                        "_capability_eval", _capture)

    code, _ = _update_goal(
        port, "g-001-50", "defer_reason",
        "blocked on user-initiated deploy",
        headers={
            "X-Mind-Force-Defer": "genuine human approval required",
            "X-Mind-Force-Unstructured-Defer": "test fixture - no real ref",
        },
    )
    assert code == 200
    assert captured.get("override_agent_match") == "genuine human approval required"
    assert captured.get("for_goal_id") == "g-001-50"
    assert captured.get("suggest_unblock") is True
    assert captured["failure_reason"] == "blocked on user-initiated deploy"


# ---------------------------------------------------------------------------
# Structured-prefix bypass (PR 7e/1) — capability gate must NOT fire on
# machine-written internal markers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("structured_defer", [
    "Circuit breaker: 3 consecutive failures",
    "circuit breaker: lowercase variant",
    "Circuit Breaker: titlecase drift",
    "precondition_unmet: g-001-01 status != pending",
    "blocked_on_dependency g-001-02",
])
def test_capability_skips_structured_defer_prefixes(running_daemon,
                                                   aspirations_write_module,
                                                   monkeypatch,
                                                   structured_defer):
    """Structured-prefix defers must bypass the capability gate. Without
    this, keyword scans collide with forged skill names (rb-246 pattern)
    and the framework's own protective mechanisms get blocked."""
    _, port = running_daemon
    _seed_goal(port)
    called = {"n": 0}

    def _track(*a, **kw):
        called["n"] += 1
        return {"would_block": True, "reason": "should not be invoked"}

    monkeypatch.setattr(aspirations_write_module,
                        "_capability_eval", _track)

    code, _ = _update_goal(port, "g-001-50", "defer_reason", structured_defer)
    assert code == 200, (
        f"structured defer {structured_defer!r} must pass without gate firing"
    )
    assert called["n"] == 0, (
        f"capability gate must NOT be invoked for structured prefix; "
        f"got {called['n']} invocation(s)"
    )
