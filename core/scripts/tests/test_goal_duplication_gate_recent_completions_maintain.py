"""test_goal_duplication_gate_recent_completions_maintain.py — 6.

Regression test for the Completed-Maintain skip in _check_recent_completions.

Completes the carve-out parity: the status=completed Maintain skip was rolled
out check-by-check (git_log g-115-1813, partner_in_flight g-115-2477,
insight_triggers g-115-2685) but _check_recent_completions was left out — so a
completed-Maintain filing whose vocabulary overlapped a PARTNER's recent
completion was still false-blocked (needing --override-duplication), defeating
the carve-out. recent_completions is a purely fuzzy IDF keyword/path check, so a
FULL skip is correct (pending_queue is the only check that RESTRICTS instead,
because it is the exact-duplicate-record check).

Full-gate hermetic cases (tmp MIND_WORLD, no live state touched — g-115-1376):
  A. pending goal + rare-keyword overlap        → would_block=True  (real signal preserved)
  B. completed-Maintain goal + same overlap     → would_block=False (the fix; blocked pre-fix)
  C. completed NON-Maintain goal + same overlap → would_block=True  (skip is Maintain-narrow)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML required", file=sys.stderr)
    sys.exit(2)

RARE = "rare_identifier_foo9 unique_marker_baz7"


def _now(off_h: float = 0) -> str:
    return (datetime.now() + timedelta(hours=off_h)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed(w: Path):
    (w / "board").mkdir(parents=True, exist_ok=True)
    (w / "board" / "findings.jsonl").write_text("", encoding="utf-8")  # no insight_triggers
    ts = {
        "strategic_focus": {"primary": None, "rationale": None, "set_by": None,
                            "set_at": None, "acknowledged_by": []},
        "active_blockers": [], "critical_blockers": [], "agent_status": {},
        # rare identifiers in the target entry + diverse fillers so IDF gives the
        # rare terms real weight (N=1 corpus makes every term weight 0).
        "recent_completions": [
            {"goal_id": "g-rcm-prior", "completed_by": "bravo", "completed_at": _now(-2),
             "key_finding": f"{RARE} phase ordering fixed."},
            {"goal_id": "g-rcm-f1", "completed_by": "bravo", "completed_at": _now(-3),
             "key_finding": "Database connection pool tuning query latency improved."},
            {"goal_id": "g-rcm-f2", "completed_by": "bravo", "completed_at": _now(-4),
             "key_finding": "API rate limiting retry policy exponential backoff."},
            {"goal_id": "g-rcm-f3", "completed_by": "bravo", "completed_at": _now(-5),
             "key_finding": "Frontend rendering performance audit virtual scrolling."},
            {"goal_id": "g-rcm-f4", "completed_by": "bravo", "completed_at": _now(-6),
             "key_finding": "Memory leak triage websocket subscription cleanup."},
        ],
    }
    (w / "team-state.yaml").write_text(yaml.dump(ts, sort_keys=False), encoding="utf-8")


def _run_gate(goal: dict, w: Path) -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(w)
    env["MIND_AGENTS_ROOT"] = str(w / "agents")  # hermetic agent-queue scan (1)
    env["STORAGE_BACKEND"] = "local"
    p = subprocess.run([sys.executable, str(GATE_PY)], input=json.dumps(goal),
                       capture_output=True, text=True, env=env, timeout=30)
    if p.returncode not in (0, 1):
        raise RuntimeError(f"gate exit {p.returncode}: {(p.stderr or p.stdout)[:300]}")
    return json.loads(p.stdout)


def _run():
    failures = []
    w = Path(tempfile.mkdtemp(prefix="rcm2686-"))
    try:
        _seed(w)

        a = _run_gate({"title": "Refactor phase ordering", "status": "pending",
                       "description": f"Standalone refactor. {RARE} cleanup.",
                       "participants": ["agent"], "source": "world",
                       "origin_signal": "parent_aspiration:asp-115"}, w)
        if a.get("would_block") is not True:
            failures.append(f"A pending+overlap should block, got would_block={a.get('would_block')}")

        b = _run_gate({"title": "Maintain: adjusted phase ordering inline", "status": "completed",
                       "description": f"Recorded inline fix. {RARE} cleanup.",
                       "participants": ["agent"], "source": "world",
                       "origin_signal": "maintain:rcm-2686"}, w)
        if b.get("would_block") is not False:
            failures.append(f"B completed-Maintain+overlap should SKIP, got would_block={b.get('would_block')} ({b.get('reason')})")

        c = _run_gate({"title": "Refactor phase ordering (standalone)", "status": "completed",
                       "description": f"Non-maintain completed goal. {RARE} cleanup.",
                       "participants": ["agent"], "source": "world",
                       "origin_signal": "parent_aspiration:asp-115"}, w)
        if c.get("would_block") is not True:
            failures.append(f"C completed-non-Maintain+overlap should still block, got would_block={c.get('would_block')}")

        return failures
    finally:
        shutil.rmtree(w, ignore_errors=True)


def test_recent_completions_completed_maintain_skip():
    failures = _run()
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    fails = _run()
    if fails:
        print(f"TEST FAIL ({len(fails)}):", file=sys.stderr)
        for f in fails:
            print("  " + f, file=sys.stderr)
        sys.exit(1)
    print("TEST PASS: 3 cases (pending-blocks, completed-Maintain-skips, completed-non-Maintain-blocks)")
    sys.exit(0)
