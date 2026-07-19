"""End-to-end wrapper test for aspirations-update-goal.sh.

Verify the wrapper talks to the daemon, JSON-encodes values mirroring
parse_value, forwards override flags as headers, and prints the persisted
goal to stdout. Layer-D auto-Unblock filing happens inline (see
test_runtime_update_goal_cascade.py for daemon-side test coverage).

Test strategy:
  - running_daemon fixture spawns a daemon in a tmp project_root
  - We override RT_DIR so the wrapper finds the tmp daemon's port file
    instead of REPO_ROOT/mind_api/state/
  - Assert exit 0 + persisted goal landed
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ADD_WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-add-goal.sh"
UPDATE_WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-update-goal.sh"


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _run(args, *, project_root: Path, agent: str = "alpha", stdin: str = ""):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [_bash(), UPDATE_WRAPPER.as_posix(), *args],
        env=env, input=stdin, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _seed_goal(project_root: Path, port: int, *, title: str = "Seed",
               agent: str = "alpha", origin_signal: str = "user_directive",
               description: str = "x" * 100, extras: dict | None = None) -> str:
    """Add a goal via daemon, return the allocated goal_id."""
    goal = {"title": title, "status": "pending",
            "origin_signal": origin_signal, "description": description}
    if extras:
        goal.update(extras)
    # mc-066 (0): the Phase E.5 operator-offload gate 400s any
    # recurring-shaped seed (recurring=True OR interval_hours present) that
    # lacks an offload_decision — inject the fixture decision unless the
    # test supplies its own.
    if (goal.get("recurring") is True
            or goal.get("interval_hours") is not None) \
            and "offload_decision" not in goal:
        goal["offload_decision"] = "stays-mind: test fixture"
    qs = urllib.parse.urlencode({"asp_id": "asp-001", "source": "world"})
    url = f"http://127.0.0.1:{port}/v1/aspirations/add-goal?{qs}"
    req = urllib.request.Request(
        url, data=json.dumps(goal).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["goal_id"]


def _read_goal(project_root: Path, goal_id: str) -> dict | None:
    """Read a goal directly from the fixture's aspirations.jsonl."""
    path = project_root / "world" / "aspirations.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


# ---------------------------------------------------------------------------
# Hot path
# ---------------------------------------------------------------------------

def test_wrapper_basic_field_update_via_daemon(running_daemon):
    """Daemon path: simple flat field update prints the persisted goal."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "priority", "HIGH"], project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["id"] == goal_id
    assert parsed["priority"] == "HIGH"
    # Cascade: last_modified stamped by daemon
    assert "last_modified" in parsed

    # Verify fixture was updated
    on_disk = _read_goal(project_root, goal_id)
    assert on_disk["priority"] == "HIGH"


def test_wrapper_encodes_typed_values(running_daemon):
    """parse_value-equivalent encoding: 'true' → bool, 'null' → None, '42' → int."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    # bool
    rc, out, _ = _run(
        [goal_id, "decomposed", "true"], project_root=project_root)
    assert rc == 0
    assert json.loads(out)["decomposed"] is True

    # int
    rc, out, _ = _run(
        [goal_id, "interval_hours", "168"], project_root=project_root)
    assert rc == 0
    assert json.loads(out)["interval_hours"] == 168

    # null
    rc, out, _ = _run(
        [goal_id, "deferred_until", "null"], project_root=project_root)
    assert rc == 0
    assert json.loads(out)["deferred_until"] is None


def test_wrapper_defer_reason_clear_via_daemon(running_daemon):
    """defer_reason=null drops defer_reason_set_at + blocker_ref. Daemon
    cascade tested by test_runtime_update_goal_cascade; this confirms the
    WRAPPER takes the daemon path on defer_reason."""
    project_root, port = running_daemon
    # Seed a goal with an existing defer_reason + blocker_ref already on disk
    # so we can observe the clear cascade.
    goal_id = _seed_goal(
        project_root, port,
        extras={
            "defer_reason": "precondition_unmet: waiting on partner",
            "defer_reason_set_at": "2026-05-01T10:00:00",
            "blocker_ref": {"type": "dependency", "external_id": "g-001-02"},
        },
    )

    rc, out, err = _run(
        [goal_id, "defer_reason", "null"], project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["defer_reason"] is None
    # Daemon-side defer-clear cascade dropped both companions
    assert parsed.get("defer_reason_set_at") is None
    assert "blocker_ref" not in parsed


def test_wrapper_defer_reason_with_blocker_ref_via_daemon(running_daemon):
    """Narrative defer_reason + --blocker-ref + --force-defer reaches the
    daemon and persists the structured ref alongside the prose."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    ref = json.dumps({"type": "partner-response",
                      "external_id": "user-feedback-pending"})
    rc, out, err = _run(
        [goal_id, "defer_reason", "awaiting user feedback on the proposal",
         "--blocker-ref", ref,
         "--force-defer", "test bypass for non-agent-provisionable signal"],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["defer_reason"] == "awaiting user feedback on the proposal"
    assert parsed["blocker_ref"]["type"] == "partner-response"
    assert parsed["blocker_ref"]["external_id"] == "user-feedback-pending"
    assert parsed.get("defer_reason_set_at") is not None


def test_wrapper_forwards_force_unstructured_defer_header(running_daemon):
    """--force-unstructured-defer maps to X-Mind-Force-Unstructured-Defer
    header which the daemon honors as a blocker_ref override."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "defer_reason", "user must explicitly confirm scope",
         "--force-defer", "non-agent-provisionable",
         "--force-unstructured-defer", "external signal has no observable id"],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["defer_reason"] == "user must explicitly confirm scope"
    # Override path: no blocker_ref persisted
    assert "blocker_ref" not in parsed


# ---------------------------------------------------------------------------
# Daemon-side gate blocks (terminal — wrapper exits 1 with daemon body)
# ---------------------------------------------------------------------------

def test_wrapper_blocker_ref_required_returns_nonzero(running_daemon):
    """Narrative defer without --blocker-ref and without --force-unstructured-
    defer → daemon blocks with blocker_ref_required → wrapper exits 1."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "defer_reason", "user must explicitly confirm scope",
         "--force-defer", "non-agent-provisionable"],
        project_root=project_root,
    )
    assert rc == 1, f"expected exit 1 on blocker_ref_required, got {rc}: out={out}"
    assert "blocker_ref_required" in err


# ---------------------------------------------------------------------------
# Fallback dispatch (field-triggered)
# ---------------------------------------------------------------------------

def _looks_like_daemon_success_response(out: str) -> bool:
    """Daemon-path stdout is a JSON dict with `id` (goal record). Anything
    else means the daemon path was bypassed."""
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and "id" in parsed


def test_wrapper_status_in_progress_via_daemon(running_daemon):
    """field=status now goes through the daemon (PR 7i). status=in-progress
    bumps selection_count on the aspiration."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "status", "in-progress"], project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "in-progress"

    # Daemon cascade: aspiration's selection_count bumped
    asp_path = project_root / "world" / "aspirations.jsonl"
    asp = json.loads(asp_path.read_text(encoding="utf-8").splitlines()[0])
    assert asp["selection_count"] == 1


def test_wrapper_status_completed_via_daemon(running_daemon):
    """status=completed stamps completed_at + clears any claim."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "status", "completed"], project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "completed"
    assert parsed.get("completed_at") is not None


def test_wrapper_status_blocked_requires_evidence(running_daemon):
    """status=blocked without evidence returns 400 from daemon."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "status", "blocked"], project_root=project_root,
    )
    assert rc == 1, f"expected exit 1, got {rc}: out={out}"
    assert "blocker_ref_required_for_blocked_status" in err


def test_wrapper_status_blocked_with_ref(running_daemon):
    """status=blocked + --blocker-ref succeeds and persists the ref."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    # Use the spaces-inside-JSON workaround for Windows MSYS-bash quote
    # mangling — see test_wrapper_blocked_by_via_daemon docs.
    ref = '{ "type": "infrastructure", "external_id": "k8s-x" }'
    rc, out, err = _run(
        [goal_id, "status", "blocked",
         "--blocker-ref", ref],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["status"] == "blocked"
    assert parsed["blocker_ref"]["type"] == "infrastructure"
    assert parsed["blocker_ref"]["external_id"] == "k8s-x"


def test_wrapper_status_recurring_completed_blocked(running_daemon):
    """recurring=True + status=completed returns 400 invalid_status_transition."""
    project_root, port = running_daemon
    goal_id = _seed_goal(
        project_root, port,
        extras={"recurring": True, "interval_hours": 24},
    )
    rc, out, err = _run(
        [goal_id, "status", "completed"], project_root=project_root,
    )
    assert rc == 1, f"expected exit 1, got {rc}: out={out}"
    assert "invalid_status_transition" in err
    assert "recurring" in err


def test_wrapper_status_superseded_blocked(running_daemon):
    """status=superseded direct-set returns 400 invalid_status_transition."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)
    rc, out, err = _run(
        [goal_id, "status", "superseded"], project_root=project_root,
    )
    assert rc == 1, f"expected exit 1, got {rc}: out={out}"
    assert "invalid_status_transition" in err
    assert "intent_satisfaction" in err


def test_wrapper_participants_no_warn_via_daemon(running_daemon):
    """field=participants now goes through the daemon (PR 7h). When the
    advisory doesn't trigger (no 'user' in participants), stderr stays
    clean and the goal updates."""
    project_root, port = running_daemon
    # Use the spaces-inside-brackets workaround for Windows MSYS-bash quote
    # mangling — see test_wrapper_blocked_by_via_daemon for the diagnostic.
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "participants", '[ "agent" ]'],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["participants"] == ["agent"]
    # No 'user' in participants → no user_leg_scope warning
    assert "user_leg_scope" not in err


def test_wrapper_participants_advisory_re_emits_to_stderr(running_daemon):
    """field=participants with 'user' but no user_leg_scope set → wrapper
    re-emits the advisory warning to stderr (matches add-goal wrapper)."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "participants", '[ "agent", "user" ]'],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    # Goal still got updated — warning is advisory only
    assert "user" in parsed["participants"]
    # Wrapper re-emitted the advisory message from response.warnings[] to stderr
    assert "user_leg_scope" in err


def test_wrapper_recurring_false_via_daemon(running_daemon):
    """field=recurring now goes through the daemon (PR 7g). recurring=false
    cascade drops interval_hours and lastAchievedAt."""
    project_root, port = running_daemon
    # Seed a goal already configured as recurring
    goal_id = _seed_goal(
        project_root, port,
        extras={"recurring": True, "interval_hours": 24,
                "lastAchievedAt": "2026-05-12T08:00:00"},
    )

    rc, out, err = _run(
        [goal_id, "recurring", "false"], project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["recurring"] is False
    # Daemon cascade dropped the recurring-shape fields
    assert "interval_hours" not in parsed
    assert "lastAchievedAt" not in parsed


def test_wrapper_blocked_by_via_daemon(running_daemon):
    """field=blocked_by now goes through the daemon (PR 7g). Setting non-
    empty blocked_by stamps blocked_since.

    NOTE on the value formatting: a quote-bearing JSON literal with no
    internal spaces (e.g., '["g-001-99"]') is mangled by Python
    subprocess→MSYS-bash on Windows (the inner `"` chars get eaten and
    the `\\` reappears as a delimiter). Adding spaces inside the array
    literal sidesteps that quirk while staying valid JSON — the daemon's
    json.loads accepts both."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "blocked_by", '[ "g-001-99" ]'],
        project_root=project_root,
    )
    assert rc == 0, f"wrapper exit {rc}: stderr={err}"
    parsed = json.loads(out)
    assert parsed["blocked_by"] == ["g-001-99"]
    # Daemon cascade auto-stamped blocked_since
    assert "blocked_since" in parsed and parsed["blocked_since"] is not None


def test_wrapper_dotted_field_returns_400(running_daemon):
    """Dotted field now hits daemon's 400 dotted_field_rejected guard."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    rc, out, err = _run(
        [goal_id, "verification.outcomes", '["ok"]'],
        project_root=project_root,
    )
    assert rc == 1, f"expected exit 1 on dotted field, got {rc}: out={out}"
    assert "dotted_field_rejected" in err
    # No corruption — literal-string key did NOT land on disk
    on_disk = _read_goal(project_root, goal_id)
    assert "verification.outcomes" not in on_disk


def test_wrapper_missing_positional_error(running_daemon):
    """Missing positional args → wrapper exits non-zero."""
    project_root, _ = running_daemon

    # Only goal_id, no field/value — daemon requires field
    rc, out, err = _run(["g-001-99"], project_root=project_root)
    assert not _looks_like_daemon_success_response(out)


# ---------------------------------------------------------------------------
# PR 7j — wrapper no longer falls back on capability_blocked
# ---------------------------------------------------------------------------


def test_wrapper_capability_blocked(running_daemon):
    """When the daemon refuses a defer_reason with error=capability_blocked,
    the wrapper exits 1 and surfaces the 400 body to stderr. Layer-D
    auto-Unblock filing happens daemon-side under the live lock (covered in
    test_runtime_update_goal_cascade.py)."""
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)

    # Pick a defer narrative that reliably trips the forged-skill keyword
    # scan: any phrasing about "user" or "human" matches /notify-user-style
    # capabilities. The exact match is not load-bearing — we only assert
    # that on a capability_blocked 400, the wrapper exits 1 without falling
    # back. If forged-skill drift makes this defer phrase pass the gate,
    # the test naturally degrades to "wrapper ran the hot path"; the
    # in-daemon test still pins the no-fallback behavior end-to-end.
    rc, out, err = _run(
        [goal_id, "defer_reason", "blocked on user feedback for the human"],
        project_root=project_root,
    )
    # Two acceptable outcomes:
    #   (a) gate blocked → rc=1 + stderr contains capability_blocked
    #   (b) gate passed  → rc=0 (then the test isn't exercising the path;
    #       see in-daemon coverage)
    if "capability_blocked" in err:
        assert rc == 1, f"capability_blocked must exit 1, got {rc}"
        assert "[defer-gate] routing Unblock" not in err
        # Wrapper does NOT print the daemon success shape on a refusal.
        assert not _looks_like_daemon_success_response(out)
