"""test_goal_duplication_gate_title_exact.py — regression test for pending_queue Strategy 0.

Closes the exact-title hole measured 2026-08-28 on a small-model deployment
(coach, zc-03, 27B local model): a goal with a byte-identical title+description
to a PENDING sibling passed the gate, because Strategy 1 deliberately skips
generic bare origins (both goals carried `user_directive`) and Strategy 2's
prose branch demotes keyword-only overlap without a directory-qualified
file-path co-signal — and research prose carries no paths. Re-filing the same
goal minutes after filing it is precisely the small-model failure shape, so
exact-normalized-title equality now blocks unconditionally (Strategy 0):
no co-signal, no IDF, no demotion. Completed goals stay excluded (re-using a
completed goal's title is follow-up work), and --override-duplication remains
the escape hatch for a genuinely distinct same-titled filing.

Cases:
  T1 identical title (case/whitespace-folded) on a PENDING goal, generic
     shared origin, no file paths → BLOCK with match_strategy=title_exact
  T2 identical title on a COMPLETED goal only → PASS (completed excluded)
  T3 distinct titles, overlapping prose → PASS (Strategy 2 demotion unchanged)
  T4 identical title + --override-duplication → would_block False (escape hatch)

Isolation mirrors the gold-standard pending_queue test: MIND_WORLD and
MIND_AGENTS_ROOT redirect to a tmp dir so no live queue is read.

Run standalone: py -3 core/scripts/tests/test_goal_duplication_gate_title_exact.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml  # type: ignore
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"

TAG = "titleexact9812"

TITLE = f"Research the {TAG} widget service auth flow and endpoints"
DESC = ("Document the complete auth flow, all key endpoints, rate limits "
        "and throttle behavior, and response formats for the widget service.")


def _seed_world(tmp_world: Path, aspirations_records: list) -> None:
    asp_path = tmp_world / "aspirations.jsonl"
    asp_path.parent.mkdir(parents=True, exist_ok=True)
    # Distinct-topic fillers keep the candidate corpus realistic (n>=5).
    fillers = {
        "id": f"asp-{TAG}-fill",
        "title": "Filler aspiration",
        "status": "active",
        "goals": [
            {"id": f"g-{TAG}-fill-{i}", "title": t, "description": d,
             "status": "pending", "origin_signal": f"idea:{TAG}-fill-{i}",
             "participants": ["agent"]}
            for i, (t, d) in enumerate([
                ("Database migration for user preferences table",
                 "Alter the preferences schema and backfill rows."),
                ("Refactor authentication middleware session handling",
                 "Rework the session middleware token lifecycle."),
                ("Optimize rendering pipeline shader compilation",
                 "Cache compiled shaders across render passes."),
                ("Investigate memory leak in websocket connection pool",
                 "Trace the leaking connection pool allocations."),
            ], 1)
        ],
    }
    with open(asp_path, "w", encoding="utf-8") as f:
        for rec in list(aspirations_records) + [fillers]:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    ts_path = tmp_world / "team-state.yaml"
    with open(ts_path, "w", encoding="utf-8") as f:
        yaml.dump({
            "strategic_focus": {"primary": None, "rationale": None,
                                "set_by": None, "set_at": None,
                                "acknowledged_by": []},
            "active_blockers": [],
            "recent_completions": [],
            "agent_status": {"alpha": {"last_active": None, "in_flight": None}},
            "critical_blockers": [],
        }, f, default_flow_style=False, sort_keys=False)
    findings_path = tmp_world / "board" / "findings.jsonl"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    findings_path.write_text("", encoding="utf-8")


def _run_gate(goal: dict, tmp_world: Path, extra_args: list | None = None) -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(tmp_world)
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)] + (extra_args or []),
        input=json.dumps(goal),
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"goal-duplication-gate exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return json.loads(proc.stdout)


def _pending_queue(result: dict) -> dict | None:
    for c in result.get("checks", []):
        if c.get("name") == "pending_queue":
            return c
    return None


def _existing(status: str) -> dict:
    return {
        "id": f"asp-{TAG}-a",
        "title": "Host aspiration",
        "status": "active",
        "goals": [{
            "id": f"g-{TAG}-existing",
            "title": TITLE,
            "description": DESC,
            "status": status,
            "origin_signal": "user_directive",
            "participants": ["agent"],
        }],
    }


def _proposed() -> dict:
    return {
        # Case + whitespace variations prove normalization, not byte equality.
        "title": "  research the " + TAG + " WIDGET service auth flow and endpoints ",
        "description": DESC,
        "participants": ["agent"],
        "source": "world",
        "origin_signal": "user_directive",
    }


def main() -> int:
    failures: list[str] = []
    tmp_world = Path(tempfile.mkdtemp(prefix=f"titleexact-{TAG}-"))
    try:
        # ── T1: identical title on a PENDING goal → BLOCK (title_exact) ──
        _seed_world(tmp_world, [_existing("pending")])
        r1 = _run_gate(_proposed(), tmp_world)
        pq1 = _pending_queue(r1)
        if pq1 is None or pq1.get("passed") is not False:
            failures.append(f"T1: expected pending_queue block, got {pq1}")
        else:
            strategies = {m.get("match_strategy") for m in pq1.get("matches") or []}
            if "title_exact" not in strategies:
                failures.append(f"T1: expected title_exact strategy, got {strategies}")
        if not failures and r1.get("would_block") is not True:
            failures.append(f"T1: expected would_block=True, got {r1.get('would_block')}")

        # ── T2: identical title only on a COMPLETED goal → PASS ─────────
        _seed_world(tmp_world, [_existing("completed")])
        r2 = _run_gate(_proposed(), tmp_world)
        pq2 = _pending_queue(r2)
        if pq2 is None or pq2.get("passed") is not True:
            failures.append(
                f"T2: completed twin must not title-block, got {pq2 and pq2.get('reason')}")

        # ── T3: distinct titles, overlapping prose → PASS (demotion kept) ─
        _seed_world(tmp_world, [_existing("pending")])
        distinct = _proposed()
        distinct["title"] = f"Evaluate alternative {TAG} widget data sources"
        r3 = _run_gate(distinct, tmp_world)
        pq3 = _pending_queue(r3)
        if pq3 is None or pq3.get("passed") is not True:
            failures.append(
                f"T3: distinct title must not hard-block, got {pq3 and pq3.get('reason')}")

        # ── T4: identical title + override → would_block False ──────────
        _seed_world(tmp_world, [_existing("pending")])
        r4 = _run_gate(_proposed(), tmp_world,
                       extra_args=["--override-duplication",
                                   "test: genuinely distinct same-titled work"])
        if r4.get("would_block") is not False:
            failures.append(f"T4: override must clear would_block, got {r4.get('would_block')}")
    finally:
        shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print("FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS (4/4 cases)")
    return 0


def test_title_exact_gate():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
