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


# --- recurring goals default to "either" () ----------------------
#
# NOTE ON _OFFLOAD: every one of these files a RECURRING goal, and the
# pre-existing operator-offload gate refuses ANY recurring filing that does not
# carry a justification (it fires on `recurring: true` / `interval_hours` and is
# entirely orthogonal to routing — it asks whether a cron should do the work,
# not who owns it). Without the header these tests 400 before Phase D's result
# is ever persisted, so the header is a precondition of the fixture, not part of
# what is under test.
#
# A recurring goal is a CADENCE obligation, not owned work. Routing one to a
# single agent makes that agent's queue depth the goal's cadence, and a busy
# owner then starves it with no escape:  stopped for 144h and
#  for 9h, both at 1097/1180 on bravo's ranking with
# recurring_urgency already at the urgency_max clamp, every peer excluded by
# block_reason=routed_to_agent. Both had to be widened by hand.


_OFFLOAD = {"X-Mind-Override-Offload":
            "routing-behaviour fixture; the cadence work is genuinely LLM work"}


def test_recurring_goal_defaults_to_either(running_daemon):
    """The headline behaviour, carrying its own control.

    Both goals use the SAME "Investigate:" title shape that
    test_capability_route_stamps_intended_agent pins to zeta. The only
    difference is `recurring`, so the control is what proves recurring-ness —
    not some incidental property of the title — produced "either".
    """
    project_root, port = running_daemon
    for title, recurring in (("Investigate: cadence sweep A", True),
                             ("Investigate: cadence sweep B", False)):
        goal = {"title": title, "status": "pending",
                "origin_signal": "user_directive"}
        if recurring:
            goal.update({"recurring": True, "interval_hours": 24})
        code, body = _add_goal(port, goal, headers=_OFFLOAD)
        assert code == 200, (title, body)
    assert _read_persisted_goal(
        project_root, "Investigate: cadence sweep A")["intended_agent"] == "either"
    assert _read_persisted_goal(
        project_root, "Investigate: cadence sweep B")["intended_agent"] == "zeta"


def test_recurring_with_requires_capability_keeps_the_classifier(running_daemon):
    """A DECLARED owner-constraint opts out of the widening.

    requires_capability is the only field in the goal schema that expresses
    where a goal can run (measured 2026-09-03; there is no requires_box /
    affinity / runs_on field), so it is also the goal's stated "box affinity".
    """
    project_root, port = running_daemon
    goal = {"title": "Investigate: pinned to a capable box", "status": "pending",
            "origin_signal": "user_directive", "recurring": True,
            "interval_hours": 24, "requires_capability": ["aws"]}
    code, _ = _add_goal(port, goal, headers=_OFFLOAD)
    assert code == 200
    persisted = _read_persisted_goal(
        project_root, "Investigate: pinned to a capable box")
    assert persisted["intended_agent"] == "zeta"


def test_recurring_explicit_intended_agent_still_wins(running_daemon):
    """Caller-explicit routing outranks the default — the gate never overrides."""
    project_root, port = running_daemon
    goal = {"title": "Investigate: explicitly owned cadence", "status": "pending",
            "origin_signal": "user_directive", "recurring": True,
            "interval_hours": 24, "intended_agent": "alpha"}
    code, _ = _add_goal(port, goal, headers=_OFFLOAD)
    assert code == 200
    assert _read_persisted_goal(
        project_root,
        "Investigate: explicitly owned cadence")["intended_agent"] == "alpha"


def test_recurring_route_to_header_still_wins(running_daemon):
    """The per-call header is the caller SAYING where this goes."""
    project_root, port = running_daemon
    goal = {"title": "Investigate: header-routed cadence", "status": "pending",
            "origin_signal": "user_directive", "recurring": True,
            "interval_hours": 24}
    code, _ = _add_goal(port, goal,
                        headers={**_OFFLOAD, "X-Mind-Route-To": "bravo"})
    assert code == 200
    assert _read_persisted_goal(
        project_root,
        "Investigate: header-routed cadence")["intended_agent"] == "bravo"


def test_recurring_handoff_to_does_not_pin_the_cadence(running_daemon):
    """Deliberate ordering, pinned so it is not "tidied" back later.

    handoff_to is a hand-off of THIS firing, not the appointment of a permanent
    cadence owner — honouring it as routing re-creates the starvation one filing
    later. A caller that genuinely wants a permanent owner sets intended_agent,
    which never reaches this branch (test above).
    """
    project_root, port = running_daemon
    goal = {"title": "Investigate: handed-off cadence", "status": "pending",
            "origin_signal": "user_directive", "recurring": True,
            "interval_hours": 24, "handoff_to": "bravo"}
    code, _ = _add_goal(port, goal, headers=_OFFLOAD)
    assert code == 200
    assert _read_persisted_goal(
        project_root, "Investigate: handed-off cadence")["intended_agent"] == "either"


def test_recurring_default_is_read_back_by_the_filer(running_daemon):
    """guard-2980's second half: omission does NOT mean unrouted, so the value
    has to travel BACK to whoever filed it.

    Both channels are asserted because they reach different readers: the
    response's `goal` is what aspirations-add-goal.sh prints to stdout, and
    `warnings` is what it re-emits to stderr. A filer reading either one sees
    the routing that actually landed rather than assuming its omission held.
    """
    project_root, port = running_daemon
    goal = {"title": "Investigate: read-back cadence", "status": "pending",
            "origin_signal": "user_directive", "recurring": True,
            "interval_hours": 24}
    code, body = _add_goal(port, goal, headers=_OFFLOAD)
    assert code == 200
    resp = json.loads(body)
    assert resp["goal"]["intended_agent"] == "either"
    warned = [w for w in (resp.get("warnings") or [])
              if "recurring-route-default" in w]
    assert warned, "the filer must be TOLD the field was set: %r" % resp.get("warnings")
    assert "either" in warned[0] and "intended_agent" in warned[0]


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
    """A goal that triggers no advisories should NOT have a warnings field.

    Every field below is load-bearing and annotated with the advisory it
    silences — the fixture has to dodge ALL of them or it stops testing the
    clean-response contract and starts testing whichever advisory it happens to
    trip. Adding an advisory to _run_add_goal_pipeline without extending this
    fixture breaks this test, which is guard-1038 exactly: a new gate's own unit
    tests passing says nothing about pre-existing RUNTIME tests that POST the
    now-warned shape.
    """
    _, port = running_daemon
    goal = {
        "title": "Clean goal",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,  # long enough → no description-length warning
        # no user participant → no user_leg_scope warning
        # outcomes present → no verification-outcomes-absent warning ()
        "verification": {"outcomes": ["the clean-response contract holds"],
                         "checks": [], "preconditions": []},
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
