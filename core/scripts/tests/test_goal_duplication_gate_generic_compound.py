"""test_goal_duplication_gate_generic_compound.py — regression test for
g-248-117 (fixes the g-248-115 uncovered path in the goal-duplication gate's
`has_specific` structural-co-signal predicate).

g-248-115 found that a generic word-HYPHEN-word compound (own-cloud,
end-to-end, env-server, phantom-world, one-button) tripped the has_specific
co-signal in `_check_recent_completions` because the old shape test was
`re.search(r"[-_0-9]", k)` — the bare HYPHEN matched. A locally-rare compound
(clears idf_floor) therefore turned a KEYWORD-ONLY overlap (zero shared
file-path) into a HARD duplicate BLOCK, false-blocking new-capability goals.

g-248-117 replaces that shape test with `_is_structural_identifier(token)`:
a token is a real work-target IDENTIFIER only if it carries an UNDERSCORE or a
DIGIT (goal-id, hash, snake_case symbol) OR ends in a file-extension suffix. A
pure hyphen-joined compound has none of these, so it no longer qualifies.

Contract asserted here (recent_completions path — the g-248-115 incident path):

  CASE A — 'own-cloud' + 'end-to-end' keyword-only overlap, non-recurring →
     DEMOTE. strong=True but has_specific=False (both are generic compounds,
     not identifiers) → a strong_keyword_only advisory, NEVER a hard block
     (would_block=False). This is the direct g-248-117 regression: pre-fix this
     hard-blocked.
  CASE B — 'phantom-world' + 'one-button' keyword-only overlap, non-recurring →
     DEMOTE. Proves the exclusion is not own-cloud-specific: any pure
     hyphen-compound pair demotes.
  CASE C — 'loop_state' + 'session_signals' keyword-only overlap, non-recurring
     → BLOCK (control / no-over-correction guard). Genuine underscore
     identifiers STILL trip has_specific → a real keyword-only structural
     duplicate hard-blocks exactly as before. Proves g-248-117 excludes ONLY
     generic compounds, not real identifiers.

Scope: this file exercises `_check_recent_completions` only (the g-248-115
incident site). The parallel `_check_pending_queue` predicate got the SAME
`_is_structural_identifier` refinement but already excluded bare hyphens (it
was `[_0-9]`, never `[-_0-9]`), so generic-compound behavior there is
unchanged — covered by test_goal_duplication_gate_pending_queue.py. The digit
and file-path identifier paths are covered by G3/G9 in the sibling
test_goal_duplication_gate_structural_co_signal.py.

Test isolation (mirrors the gold-standard test_goal_duplication_gate_pending_queue.py
and the structural-co-signal sibling): redirect MIND_WORLD + MIND_AGENTS_ROOT
to a tmp dir and seed team-state.yaml THERE, so the live shared world is never
read or written (rb-1547 seed-clobber fix, g-115-2465 hermetic agent-queue scan).

Pytest-collectable via the thin `test_*` wrapper at the bottom; also runnable
standalone via `py -3 core/scripts/tests/test_goal_duplication_gate_generic_compound.py`.
"""

from __future__ import annotations

import json
import os
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


# Filler completions that share NO vocabulary with any case's tokens, so each
# case's shared terms have df=1 → high IDF → clear idf_floor (
# rare-identifier path; matches the structural-co-signal sibling's fillers).
_FILLERS = [
    "Updated documentation index sections and renumbered nested anchors.",
    "Adjusted thread pool sizing within the orchestrator container layer.",
    "Reviewed boundary diagrams across the deployment service registry.",
    "Pruned outdated entries from the historical reference catalog index.",
    "Tightened logging verbosity flags around the watcher polling loop.",
    "Renamed stale references inside the inventory manifest collection.",
    "Reorganized chapter outlines under the curriculum reference set.",
]


def _seed_state(tmp_world: Path, target_entry):
    """Write tmp_world/team-state.yaml with one TARGET recent_completion plus
    vocabulary-disjoint fillers (raise IDF for the target's terms), an empty
    board/findings.jsonl (insight_triggers clean), and an empty aspirations.jsonl
    (pending_queue scan finds nothing — these cases assert on recent_completions
    only, and no completion is recurring so recurring_vacuum never fires). NEVER
    touches the live world (rb-1547).
    """
    recent_completions = [target_entry]
    for i, kf in enumerate(_FILLERS):
        recent_completions.append({
            "goal_id": f"g-gc-noise-{i:02d}",
            "completed_by": "bravo",
            "completed_at": _now_iso(-6 - i),
            "key_finding": kf,
        })
    agent_status = {
        "alpha": {
            "last_active": _now_iso(0), "current_focus": "",
            "session_goals_completed": 0, "live_phase": "between-phases",
            "in_flight": None,
        },
        "bravo": {
            "last_active": _now_iso(-0.1), "current_focus": "",
            "session_goals_completed": 0, "live_phase": "between-phases",
            "in_flight": None,
        },
    }
    team_state = {
        "strategic_focus": {
            "primary": None, "rationale": None,
            "set_by": None, "set_at": None, "acknowledged_by": [],
        },
        "active_blockers": [],
        "recent_completions": recent_completions,
        "agent_status": agent_status,
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
    # Hermetic agent-queue scan (): keep live agent queues out of the
    # wrapper's pending_queue check (rb-3784 corpus coupling).
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(goal),
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"goal-duplication-gate exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:200]}"
        )
    return json.loads(proc.stdout)


def _find_check(result, name):
    for c in result.get("checks", []):
        if c.get("name") == name:
            return c
    return None


def _assert_demote(failures, label, rc, tokens):
    """rc (recent_completions check) must PASS (would_block=False) with a
    strong_keyword_only advisory — a generic-compound keyword-only overlap."""
    if rc is None:
        failures.append(f"{label}: recent_completions check missing from result")
        return
    if rc.get("passed") is not True:
        failures.append(
            f"{label}: recent_completions should DEMOTE (generic-compound "
            f"keyword-only overlap {tokens} must NOT hard-block after g-248-117 "
            f"— hyphen compounds are not structural identifiers). "
            f"reason={rc.get('reason')} matches={rc.get('matches')}"
        )
        return
    advisories = rc.get("advisories") or []
    if not any(a.get("strong_keyword_only") for a in advisories):
        failures.append(
            f"{label}: expected a strong_keyword_only advisory (demoted "
            f"generic-compound overlap), got advisories={advisories}"
        )


def _assert_block(failures, label, rc, tokens):
    """rc (recent_completions check) must FAIL (would_block=True) — a genuine
    structured-identifier keyword-only overlap still hard-blocks."""
    if rc is None:
        failures.append(f"{label}: recent_completions check missing from result")
        return
    if rc.get("passed") is not False:
        failures.append(
            f"{label}: recent_completions should BLOCK (genuine structured "
            f"identifiers {tokens} still trip has_specific — g-248-117 excludes "
            f"ONLY generic compounds, not real ids). "
            f"reason={rc.get('reason')} matches={rc.get('matches')}"
        )


def main() -> int:
    failures = []
    tmp_world = Path(tempfile.mkdtemp(prefix="gc-test-"))
    try:
        # ── CASE A: own-cloud + end-to-end generic compounds → DEMOTE ──────
        # The direct  regression: a keyword-only overlap on two
        # locally-rare hyphen compounds pushed N>=2 + weighted>=1.5 +
        # has_specific=True (old [-_0-9]) → HARD block. After  the
        # compounds are not identifiers → has_specific=False → demote.
        _seed_state(tmp_world, {
            "goal_id": "g-gc-caseA",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Migration moved the store to own-cloud with end-to-end "
                "verification across the sync path."
            ),
        })
        case_a = {
            "title": "Investigate: own-cloud end-to-end sync gap",
            "description": (
                "Add an own-cloud end-to-end sync probe in a new module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        _assert_demote(failures, "CASE A",
                       _find_check(_run_gate(case_a, tmp_world), "recent_completions"),
                       "own-cloud/end-to-end")

        # ── CASE B: phantom-world + one-button generic compounds → DEMOTE ──
        # Generalization: the exclusion is not own-cloud-specific. Any pure
        # hyphen-compound pair demotes.
        _seed_state(tmp_world, {
            "goal_id": "g-gc-caseB",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Rollout enabled the phantom-world one-button flow for the "
                "preview cohort."
            ),
        })
        case_b = {
            "title": "Investigate: phantom-world one-button flow gap",
            "description": (
                "Add a phantom-world one-button flow probe in a new module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        _assert_demote(failures, "CASE B",
                       _find_check(_run_gate(case_b, tmp_world), "recent_completions"),
                       "phantom-world/one-button")

        # ── CASE C: loop_state + session_signals genuine ids → BLOCK ───────
        # No-over-correction control: genuine underscore identifiers STILL trip
        # has_specific (underscore, not hyphen). A real keyword-only structural
        # duplicate hard-blocks exactly as before .
        _seed_state(tmp_world, {
            "goal_id": "g-gc-caseC",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Sweep confirmed loop_state and session_signals restoration "
                "held across the cycle."
            ),
        })
        case_c = {
            "title": "Investigate: loop_state session_signals restoration gap",
            "description": (
                "Add a loop_state session_signals restoration probe in a new "
                "module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        _assert_block(failures, "CASE C",
                      _find_check(_run_gate(case_c, tmp_world), "recent_completions"),
                      "loop_state/session_signals")

    finally:
        import shutil
        shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — g-248-117 generic-compound demote + genuine-id block control")
    return 0


def test_generic_compound_demote_gate():
    """Pytest entry point () — runs the CASE A/B/C suite in an isolated
    tmp world and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
