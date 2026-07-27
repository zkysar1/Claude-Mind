"""test_goal_duplication_gate_cluster_idf.py — regression test for .

The token-shape strong path (`has_specific`) previously treated ANY hit
keyword carrying [-_0-9] as a strong-block co-signal, ignoring per-term IDF.
A CLUSTER-COMMON structured identifier (e.g. a goal-id referenced across many
recent completions — low per-term IDF) therefore false-strong-blocked legit
follow-up goals (canonical: 4 FPs filed 2026-06-01 + the g-115-1374 filing,
2026-06-09, which needed --override-duplication).

Fix (g-115-1325): the structured-token branch ALSO requires per-term IDF >=
idf_floor, where idf_floor = log(n/(1+STRUCT_IDF_DF_CEIL)) is derived from the
LIVE corpus size (idf(k) >= floor  <=>  df(k) <= STRUCT_IDF_DF_CEIL=1). A
structured identifier is a discriminating co-signal only if it is RARE
(df<=1, unique to the one compared goal), not cluster-common vocab.

Cases:
  C1 cluster-common structured id (df>=2) is the ONLY co-signal -> DEMOTE
     (rc.passed=True, strong_keyword_only advisory). [the FP, now fixed]
  C2 rare structured id (df==1) -> BLOCK (rc.passed=False, matches non-empty).
     [confirms the fix did NOT over-tighten — the legit duplicate signal and
      gate test CASE G3 still fire]

Test isolation strategy (g-115-1375, 2026-06-09): redirect MIND_WORLD to a
tmp directory and seed team-state.yaml THERE, so the live shared
world/team-state.yaml is NEVER read or written. This removes the seed-clobber
race (rb-1547) the prior backup/seed/restore-the-live-file harness was prone
to — the canonical diagnostic was this very test's C1 FP naming a NON-seeded
real-world goal_id (g-248-07) instead of the seeded g-clusterid-77 because a
partner write clobbered the seed mid-test. The gate reads no META and resolves
team-state from world_dir (← MIND_WORLD), so MIND_WORLD-only isolation is
sufficient (mirrors the gold-standard test_goal_duplication_gate_pending_queue.py).

Pytest-collectable via the thin `test_*` wrapper at the bottom; also runnable
standalone via `py -3 core/scripts/tests/test_goal_duplication_gate_cluster_idf.py`.
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

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"


def _now_iso(offset_hours: float = 0) -> str:
    return (datetime.now() + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


# Vocabulary-disjoint filler entries — raise corpus n without sharing terms
# with the proposed goal, so n>2 (floor active) and target terms stay rare.
_FILLERS = [
    "Updated documentation index sections and renumbered nested anchors.",
    "Adjusted thread pool sizing within the orchestrator container layer.",
    "Reviewed boundary diagrams across the deployment service registry.",
    "Pruned outdated entries from the historical reference catalog index.",
    "Tightened logging verbosity flags around the watcher polling loop.",
    "Renamed stale references inside the inventory manifest collection.",
    "Reorganized chapter outlines under the curriculum reference set.",
]


def _seed_state(tmp_world: Path, extra_entries):
    """Write tmp_world/team-state.yaml with `extra_entries` (the TARGET + any
    cluster repeats) plus disjoint fillers. Fillers raise n so the IDF floor is
    active and the target's UNIQUE terms keep high IDF. Also writes an empty
    tmp_world/board/findings.jsonl and empty tmp_world/aspirations.jsonl. NEVER
    touches the live world (rb-1547 seed-clobber fix)."""
    recent_completions = list(extra_entries)
    for i, kf in enumerate(_FILLERS):
        recent_completions.append({
            "goal_id": f"g-cidf-noise-{i:02d}",
            "completed_by": "bravo",
            "completed_at": _now_iso(-6 - i),
            "key_finding": kf,
        })
    base_status = {
        "last_active": _now_iso(0), "current_focus": "",
        "session_goals_completed": 0, "live_phase": "between-phases",
        "in_flight": None,
    }
    team_state = {
        "strategic_focus": {"primary": None, "rationale": None, "set_by": None,
                            "set_at": None, "acknowledged_by": []},
        "active_blockers": [],
        "recent_completions": recent_completions,
        "agent_status": {"alpha": dict(base_status), "bravo": dict(base_status)},
        "critical_blockers": [],
    }
    ts_path = tmp_world / "team-state.yaml"
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ts_path, "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)
    findings_path = tmp_world / "board" / "findings.jsonl"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(findings_path, "w", encoding="utf-8") as f:
        f.write("")
    with open(tmp_world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write("")


def _run_gate(goal: dict, tmp_world: Path, agent: str = "alpha") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(tmp_world)
    # Hermetic agent-queue scan (): keep live agent queues out
    # of the wrapper's pending_queue check (rb-3784 corpus coupling).
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(goal),
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"goal-duplication-gate exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:200]}")
    return json.loads(proc.stdout)


def _find_check(result, name):
    for c in result.get("checks", []):
        if c.get("name") == name:
            return c
    return None


def main() -> int:
    failures = []

    tmp_world = Path(tempfile.mkdtemp(prefix="cidf-test-"))

    try:
        # ── C1: cluster-common structured id (df>=2) only co-signal → DEMOTE ──
        # "g-clusterid-77" appears in the TARGET plus two sibling completions
        # (df=3). The proposed goal shares it + two plain words unique to the
        # target ("telemetry","buffers" → high IDF → weighted>=1.5, strong).
        # The ONLY structured co-signal is the cluster-common id. Pre-fix:
        # has_specific=True (digit) → BLOCK (FP). Post-fix: its low per-term
        # IDF (< floor) excludes it → has_specific=False → DEMOTE.
        _seed_state(tmp_world, [
            {"goal_id": "g-cidf-target-C1", "completed_by": "bravo",
             "completed_at": _now_iso(-2),
             "key_finding": "Refactored g-clusterid-77 telemetry buffers in the metrics layer."},
            {"goal_id": "g-cidf-sib1-C1", "completed_by": "bravo",
             "completed_at": _now_iso(-3),
             "key_finding": "Adjusted g-clusterid-77 scheduling in the dispatch coordinator."},
            {"goal_id": "g-cidf-sib2-C1", "completed_by": "bravo",
             "completed_at": _now_iso(-4),
             "key_finding": "Validated g-clusterid-77 boundary inside the routing harness."},
        ])
        case_c1 = {
            "title": "Optimize g-clusterid-77 telemetry buffers",
            "description": "Tune the g-clusterid-77 telemetry and buffers pathway.",
            "participants": ["agent"], "source": "world",
        }
        r1 = _run_gate(case_c1, tmp_world)
        rc1 = _find_check(r1, "recent_completions")
        if rc1 is None:
            failures.append("C1: recent_completions check missing")
        elif rc1.get("passed") is not True:
            failures.append(
                "C1: cluster-common structured id should DEMOTE (not block). "
                f"reason={rc1.get('reason')} matches={rc1.get('matches')}")
        else:
            adv = rc1.get("advisories") or []
            if not any(a.get("strong_keyword_only") for a in adv):
                failures.append(
                    f"C1: expected strong_keyword_only advisory (demoted), got {adv}")

        # ── C2: rare structured id (df==1) → BLOCK (discriminator preserved) ──
        # "g-uniqueid-91" appears in ONE completion only (df=1 → idf==floor →
        # passes). The fix must NOT over-tighten: a genuinely-rare identifier
        # is still a hard-block co-signal.
        _seed_state(tmp_world, [
            {"goal_id": "g-cidf-target-C2", "completed_by": "bravo",
             "completed_at": _now_iso(-2),
             "key_finding": "Closed g-uniqueid-91 by adjusting recurring cadence in the scheduler."},
        ])
        case_c2 = {
            "title": "Recurring cadence work referencing g-uniqueid-91 patterns",
            "description": "Apply cadence adjustments referenced in the g-uniqueid-91 outcome record.",
            "participants": ["agent"], "source": "world",
        }
        r2 = _run_gate(case_c2, tmp_world)
        rc2 = _find_check(r2, "recent_completions")
        if rc2 is None:
            failures.append("C2: recent_completions check missing")
        elif rc2.get("passed") is not False:
            failures.append(
                "C2: rare (df==1) structured id should still BLOCK. "
                f"reason={rc2.get('reason')} matches={rc2.get('matches')}")
        else:
            matches = rc2.get("matches") or []
            if not matches:
                failures.append("C2: expected matches non-empty on rare-id block")
            elif not any("g-uniqueid-91" in (m.get("keyword_hits") or [])
                         for m in matches):
                failures.append(
                    f"C2: expected g-uniqueid-91 in keyword_hits, got "
                    f"{[m.get('keyword_hits') for m in matches]}")
    finally:
        shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (2/2 cases)")
    return 0


def test_cluster_idf_gate():
    """Pytest entry point () — runs the 2-case suite in an isolated
    tmp world and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
