"""Daemon endpoint -> PR 7c pipeline wiring tests.

Covers the 4 gates + 2 advisories + 2 mutators added in PR 7c:
  Advisories:  user_leg_scope, description_length
  Mutators:    category-suggest, work_class, capability-route
  Blockers:    scaffolded-exploration

Plus origin-signal Layer-D auto-derive ordering: capability-route runs AFTER
origin-signal so it can see the patched origin_signal field.

Existing PR 7b tests cover origin-signal, goal-duplication, uncommitted-work,
and capability gates — those aren't repeated here.
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
    from mind_api.src.endpoints import aspirations_write
    return aspirations_write


def _add_goal(port, goal, **kwargs):
    return _post(port, "/v1/aspirations/add-goal",
                 {"asp_id": "asp-001", "source": "world"},
                 json.dumps(goal).encode("utf-8"), **kwargs)


def _read_persisted_goal(project_root, goal_title):
    """Read the live aspirations.jsonl and return the first goal with
    matching title, or None."""
    live = project_root / "world" / "aspirations.jsonl"
    items = [json.loads(l) for l in live.read_text(encoding="utf-8").splitlines() if l.strip()]
    for asp in items:
        for g in asp.get("goals", []):
            if g.get("title") == goal_title:
                return g
    return None


# ---------------------------------------------------------------------------
# Advisories — surface in `warnings` array on success
# ---------------------------------------------------------------------------

def test_user_leg_scope_warning_surfaced(running_daemon):
    """user participant without user_leg_scope → warning in response.warnings."""
    _, port = running_daemon
    goal = {
        "title": "Ask user something",
        "status": "pending",
        "origin_signal": "user_directive",
        "participants": ["agent", "user"],
        # user_leg_scope intentionally missing
    }
    code, body = _add_goal(port, goal)
    assert code == 200
    resp = json.loads(body)
    assert "warnings" in resp
    assert any("user_leg_scope" in w for w in resp["warnings"])


def test_description_length_warning_surfaced(running_daemon):
    """Short description on non-recurring goal → warning surfaced."""
    _, port = running_daemon
    goal = {
        "title": "Short", "status": "pending",
        "origin_signal": "user_directive",
        "description": "tiny",  # < 80 chars
    }
    code, body = _add_goal(port, goal)
    assert code == 200
    resp = json.loads(body)
    assert any("description short" in w for w in resp.get("warnings", []))


def test_long_description_no_warning(running_daemon):
    """Goal with sufficient description → no description-length warning."""
    _, port = running_daemon
    goal = {
        "title": "Long enough",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,
    }
    code, body = _add_goal(port, goal)
    assert code == 200
    resp = json.loads(body)
    desc_warnings = [w for w in resp.get("warnings", [])
                     if "description short" in w]
    assert desc_warnings == []


def test_recurring_goal_skips_description_warning(running_daemon):
    """Recurring goals are exempt — title-as-spec pattern."""
    _, port = running_daemon
    goal = {
        "title": "Recurring check", "status": "pending",
        "origin_signal": "recurring_cadence:health",
        "description": "x",  # short, but recurring
        "recurring": True,
        "interval_hours": 24,
        # Recurring goals must carry offload_decision (operator-offload-gate,
        # 1be14521f) — orthogonal to this test's description-warning assertion.
        "offload_decision": "stays on LLM loop — health check needs contextual judgment, not an operator cron",
    }
    code, body = _add_goal(port, goal)
    assert code == 200
    resp = json.loads(body)
    desc_warnings = [w for w in resp.get("warnings", [])
                     if "description short" in w]
    assert desc_warnings == []


# ---------------------------------------------------------------------------
# Mutators — observable via persisted goal record
# ---------------------------------------------------------------------------

def test_category_suggest_assigns_when_absent(running_daemon):
    """Goal without category → category-suggest mutator picks from tree
    OR falls back to 'uncategorized'."""
    project_root, port = running_daemon
    goal = {
        "title": "alpha-test-node related work",  # matches fixture tree node
        "status": "pending",
        "origin_signal": "user_directive",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "alpha-test-node related work")
    # category should be set (either to a tree-node key match or "uncategorized")
    assert persisted is not None
    assert persisted.get("category"), "category mutator should always set a value"


def test_category_explicit_not_overridden(running_daemon):
    """Caller-supplied category wins; mutator skips."""
    project_root, port = running_daemon
    goal = {
        "title": "Explicit cat goal",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "framework-loop",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "Explicit cat goal")
    assert persisted["category"] == "framework-loop"


def test_work_class_resolved_from_category(running_daemon):
    """work_class mutator runs the resolver on the (possibly just-assigned)
    category. work_class.resolve falls back to 'unclassified' for unmapped
    categories — that's the expected default."""
    project_root, port = running_daemon
    goal = {
        "title": "Resolve work class",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "unmapped-test-category",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "Resolve work class")
    assert "work_class" in persisted
    assert persisted["work_class"] == "unclassified"  # fail-open default


def test_work_class_explicit_not_overridden(running_daemon):
    project_root, port = running_daemon
    goal = {
        "title": "Explicit work class",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "anything",
        "work_class": "exploration",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "Explicit work class")
    assert persisted["work_class"] == "exploration"


def test_capability_route_stamps_intended_agent(running_daemon):
    """Title 'Investigate: ...' → capability-route stamps intended_agent=zeta."""
    project_root, port = running_daemon
    goal = {
        "title": "Investigate: weird behavior",
        "status": "pending",
        "origin_signal": "user_directive",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "Investigate: weird behavior")
    assert persisted.get("intended_agent") == "zeta"


def test_capability_route_explicit_not_overridden(running_daemon):
    """Caller-set intended_agent wins; capability-route mutator skips."""
    project_root, port = running_daemon
    goal = {
        "title": "Investigate: foo",  # would normally → zeta
        "status": "pending",
        "origin_signal": "user_directive",
        "intended_agent": "alpha",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "Investigate: foo")
    assert persisted["intended_agent"] == "alpha"


def test_route_to_header_forwarded(running_daemon, aspirations_write_module,
                                   monkeypatch):
    """X-Mind-Route-To header reaches capability-route as route_to kwarg."""
    _, port = running_daemon
    captured = {}

    def _capture(title, **kw):
        captured["title"] = title
        captured.update(kw)
        return {"intended_agent": "bravo", "confidence": 1.0, "rationale": "test"}

    monkeypatch.setattr(aspirations_write_module,
                        "_cap_route_eval", _capture)

    goal = {"title": "Investigate: foo", "status": "pending",
            "origin_signal": "user_directive"}
    code, _ = _add_goal(port, goal,
                       headers={"X-Mind-Route-To": "bravo"})
    assert code == 200
    assert captured.get("route_to") == "bravo"


# ---------------------------------------------------------------------------
# scaffolded-exploration blocker
# ---------------------------------------------------------------------------

def test_scaffolded_exploration_blocks_apply_in_product_category(running_daemon):
    """Apply: title + npc-* category + no discovered_by → 400."""
    _, port = running_daemon
    goal = {
        "title": "Apply: NPC behavior fix",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "npc-cognition",
        # no discovered_by
    }
    code, body = _add_goal(port, goal)
    assert code == 400
    err = json.loads(body)
    assert err["error"] == "scaffolded_exploration_blocked"
    assert err["gate"] == "scaffolded-exploration-gate"
    assert err["gate_output"]["matched_category_prefix"] == "npc-"


def test_scaffolded_exploration_passes_with_discovered_by(running_daemon):
    """Apply: + product category + discovered_by → pass."""
    _, port = running_daemon
    goal = {
        "title": "Apply: NPC behavior fix",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "npc-cognition",
        "discovered_by": "g-115-001",
    }
    code, _ = _add_goal(port, goal)
    assert code == 200


def test_scaffolded_exploration_override_header_passes(running_daemon):
    _, port = running_daemon
    goal = {
        "title": "Apply: NPC behavior fix",
        "status": "pending",
        "origin_signal": "user_directive",
        "category": "npc-cognition",
    }
    code, _ = _add_goal(
        port, goal,
        headers={"X-Mind-Override-No-Investigate": "emergency hotfix"},
    )
    assert code == 200


# ---------------------------------------------------------------------------
# Pipeline ordering — capability-route sees the auto-derived origin_signal
# ---------------------------------------------------------------------------

def test_origin_signal_auto_derive_visible_to_later_gates(running_daemon):
    """Goal with title 'Unblock: X' — origin_signal gate patches the field
    to 'unblock:<slug>'. Verify the persisted record reflects the patch.

    Also implicitly tests that pipeline order is correct: the goal must
    survive all subsequent blockers (goal-duplication,
    scaffolded-exploration) and land in the file with the patched value.
    """
    project_root, port = running_daemon
    goal = {"title": "Unblock: efs reconnect issue",
            "status": "pending"}  # no origin_signal — should auto-derive
    code, _ = _add_goal(port, goal)
    assert code == 200
    persisted = _read_persisted_goal(project_root, "Unblock: efs reconnect issue")
    assert persisted is not None
    sig = persisted.get("origin_signal", "")
    assert sig.startswith("unblock:")


# ---------------------------------------------------------------------------
# Warnings field absent on no-warning paths (clean response)
# ---------------------------------------------------------------------------

def test_warnings_field_absent_when_no_advisories(running_daemon):
    """A goal that triggers no advisories should NOT have a warnings field."""
    _, port = running_daemon
    goal = {
        "title": "Clean goal",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,  # long enough → no description-length warning
        # no user participant → no user_leg_scope warning
    }
    code, body = _add_goal(port, goal)
    assert code == 200
    resp = json.loads(body)
    assert "warnings" not in resp, \
        f"expected no warnings field; got {resp.get('warnings')}"


# ---------------------------------------------------------------------------
# 200 response includes full persisted goal (PR 7d — wrapper migration support)
# ---------------------------------------------------------------------------

def test_add_goal_response_includes_full_goal(running_daemon):
    """The 200 response must include the full goal record under `goal` so
    wrappers can print it to stdout (matches legacy CLI's json.dumps(goal))."""
    _, port = running_daemon
    goal = {
        "title": "Investigate: full goal in response",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,
    }
    code, body = _add_goal(port, goal)
    assert code == 200
    resp = json.loads(body)
    assert "goal" in resp, "200 response missing 'goal' key"
    persisted = resp["goal"]
    # Identifying fields must round-trip
    assert persisted["title"] == "Investigate: full goal in response"
    assert persisted["status"] == "pending"
    assert persisted["id"] == resp["goal_id"]
    # Daemon-side mutations must appear (capability-route stamps zeta on
    # 'Investigate:' titles; category-suggest assigns a category)
    assert persisted.get("intended_agent") == "zeta"
    assert persisted.get("category"), "category mutator should have set a value"
    assert persisted.get("work_class"), "work_class resolver should have set a value"
