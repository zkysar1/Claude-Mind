"""test_routing_audit_age_uncomputable.py —  (from ).

POSITIVE CONTROL for one branch, and only that branch.

routing-audit-target-status-sweep.py's age gate used to read

    if age_h is None or age_h < args.max_age_hours:
        ... "reason": f"age {age_h} below threshold {args.max_age_hours}h"

which rendered the literal string "age None below threshold 0.0h" when the goal
carried no parseable timestamp — a sentence that is not well-formed, let alone
true. A reader scanning `reason` sees a threshold word and moves on, while the
goal is structurally incapable of ever aging into eligibility because it has no
timestamp at all. Split into its own verdict; behavior deliberately unchanged
(guard-420: skipping on a null is correct — only the NAME was wrong). guard-2024.

WHY THIS FILE EXISTS AT ALL. The live queue yields ZERO uncomputable rows today
(created_at was backfilled in g-115-4084), so a live run cannot distinguish "the
branch works" from "the branch is unreachable" — which is the exact flattering
zero the parent goal was about. Its sibling fix in unblock-parent-status-sweep.py
is pinned inside that script's existing integration file; this sweep had no
integration harness, only unit tests over helpers, so the branch would otherwise
have shipped unproven.

Harness mirrors test_unblock_parent_status_sweep_integration.py. Note
DaemonFixture takes the WORLD dir, not the tmp root — passing tmp yields
scanned=0 with no error, which looks exactly like a filtered-out fixture.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
SWEEP = CORE_SCRIPTS / "routing-audit-target-status-sweep.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _daemon_fixture import DaemonFixture  # noqa: E402


def _make_world(tmp: Path, *, audit_extra: dict | None = None):
    world = tmp / "world"
    world.mkdir()

    target = {
        "id": "g-700-69", "title": "Apply: do thing", "description": "Target goal",
        "status": "completed", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
        "outcome_note": "done at test time",
    }
    audit = {
        "id": "g-700-90",
        "title": "Investigate: routing-mismatch g-700-69 — recommended agent differs",
        "description": "Routing audit goal filed by post-decompose-routing-audit",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "routing-mismatch:g-700-69",
        "discovered_by": "post-decompose-routing-audit",
        "participants": ["agent"],
        "created_at": "2026-05-01T00:00:00",
    }
    if audit_extra:
        audit.update(audit_extra)

    asp = {"id": "asp-700", "title": "Routing audit sweep test",
           "motivation": "Pin the age_uncomputable branch", "scope": "project",
           "priority": "MEDIUM", "status": "active",
           "created": "2026-05-01T00:00:00", "goals": [target, audit]}
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _run(world: Path, agent_dir: Path):
    env = os.environ.copy()
    env.update({"MIND_WORLD": str(world), "MIND_AGENT": "alpha",
                "MIND_AGENT_DIR": str(agent_dir)})
    proc = subprocess.run(
        [sys.executable, str(SWEEP), "--output", "json", "--metrics-log", ""],
        capture_output=True, text=True, timeout=20, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _row(result, goal_id):
    return next((d for d in result.get("details", [])
                 if d.get("goal_id") == goal_id), None)


def test_untimestamped_goal_reports_age_uncomputable_not_below_threshold():
    """No parseable timestamp -> its own verdict, its own null age, no threshold claim."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # created_at=None is falsy and neither defer_reason_set_at nor started
        # exist, so ref_ts resolves to None and _age_hours returns None.
        world, agent_dir = _make_world(tmp, audit_extra={"created_at": None})
        with DaemonFixture(world):
            rc, out, err = _run(world, agent_dir)
        assert rc == 0, f"sweep rc={rc}; stderr={err!r}"
        result = json.loads(out)

        row = _row(result, "g-700-90")
        assert row is not None, (
            f"the untimestamped goal must appear in details; "
            f"scanned={result.get('scanned')} details={result.get('details')}")
        assert str(row.get("reason", "")).startswith("age_uncomputable"), (
            f"an undefined age needs its OWN verdict; got {row.get('reason')!r}")
        assert "below threshold" not in str(row.get("reason", "")), (
            f"reason must not claim a comparison it never made; got {row.get('reason')!r}")
        assert row.get("age_hours") is None, (
            f"age_hours must stay null, not be coerced; got {row.get('age_hours')!r}")
        assert row.get("action") == "skipped"
        assert result.get("eligible") == 0, "an uncomputable age must not become eligible"


def test_timestamped_goal_still_takes_the_threshold_path():
    """Negative control: a REAL age must not leak into the uncomputable bucket.

    Without this, a fix that routed every goal to age_uncomputable would pass the
    test above — the assertion that the split is a split, not a replacement.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world, agent_dir = _make_world(tmp)  # aged created_at, default max-age 0.0
        with DaemonFixture(world):
            rc, out, err = _run(world, agent_dir)
        assert rc == 0, f"sweep rc={rc}; stderr={err!r}"
        result = json.loads(out)

        row = _row(result, "g-700-90")
        reason = str(row.get("reason", "")) if row else ""
        assert not reason.startswith("age_uncomputable"), (
            f"a goal WITH a parseable created_at must never be labelled "
            f"age_uncomputable; got {reason!r}")
        # It cleared the 0.0h threshold, so it reached the real work.
        assert result.get("eligible", 0) >= 1, (
            f"an aged goal must pass the age gate; result={result}")
