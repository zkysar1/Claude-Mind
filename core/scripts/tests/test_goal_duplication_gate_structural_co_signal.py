"""test_goal_duplication_gate_structural_co_signal.py — regression test for
the goal-duplication-gate structural-co-signal invariant (commits 9db5384 +
6e1563e, 2026-05-16).

Verifies that `_check_recent_completions` requires a STRUCTURAL co-signal
for any hard block:

    has_specific = bool(hit_paths) or any(
        re.search(r"[-_0-9]", k) for k in hit_kws)

A purely-plain-words strong overlap (e.g. "cache" + "streaming" both 1.0 idf,
weighted >= 1.5) is demoted to an advisory; it never hard-blocks. A
file-path hit OR a hit keyword carrying a hyphen/underscore/digit
(structured identifier — rb-335) is required for the block to fire.

Cases covered:
  G1 plain-words overlap → DEMOTE (rc.passed=True, strong_keyword_only
     advisory present)
  G2 file-path overlap → BLOCK (rc.passed=False, matches non-empty)
  G3 structured-identifier hit_kw overlap → BLOCK (rc.passed=False,
     matches non-empty)

Companion to test_goal_duplication_gate_partner_in_flight.py. Same
backup/restore pattern around team-state.yaml + findings.jsonl. Filed by
g-115-838 (verify-learning Section CO smoke check; sq-018 lens, 2026-05-16
bravo encode-session Lane 5).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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

from _paths import WORLD_DIR  # type: ignore

TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"
FINDINGS_PATH = WORLD_DIR / "board" / "findings.jsonl"
GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"

PID = os.getpid()
TEAM_STATE_BACKUP = TEAM_STATE_PATH.with_suffix(f".yaml.scs-test-backup.{PID}")
FINDINGS_BACKUP = FINDINGS_PATH.with_suffix(f".jsonl.scs-test-backup.{PID}")


def _now_iso(offset_hours: float = 0) -> str:
    return (datetime.now() + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


_FILLERS = [
    "Updated documentation index sections and renumbered nested anchors.",
    "Adjusted thread pool sizing within the orchestrator container layer.",
    "Reviewed boundary diagrams across the deployment service registry.",
    "Pruned outdated entries from the historical reference catalog index.",
    "Tightened logging verbosity flags around the watcher polling loop.",
    "Renamed stale references inside the inventory manifest collection.",
    "Reorganized chapter outlines under the curriculum reference set.",
]


def _seed_state(target_entry):
    """Write team-state.yaml with one TARGET recent_completion entry plus
    filler entries that share no vocabulary with the target. Filler raises
    IDF for the target's terms (g-248-12 rare-identifier path).

    target_entry: dict with goal_id, completed_by, completed_at, key_finding.
    """
    recent_completions = [target_entry]
    for i, kf in enumerate(_FILLERS):
        recent_completions.append({
            "goal_id": f"g-scs-noise-{i:02d}",
            "completed_by": "bravo",
            "completed_at": _now_iso(-6 - i),
            "key_finding": kf,
        })
    agent_status = {
        "alpha": {
            "last_active": _now_iso(0),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
            "in_flight": None,
        },
        "bravo": {
            "last_active": _now_iso(-0.1),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
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
    TEAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TEAM_STATE_PATH, "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)

    # Empty findings.jsonl so insight_triggers stays clean.
    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FINDINGS_PATH, "w", encoding="utf-8") as f:
        f.write("")


def _run_gate(goal: dict, agent: str = "alpha") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(WORLD_DIR)
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


def main() -> int:
    if TEAM_STATE_PATH.exists():
        TEAM_STATE_BACKUP.write_bytes(TEAM_STATE_PATH.read_bytes())
    if FINDINGS_PATH.exists():
        FINDINGS_BACKUP.write_bytes(FINDINGS_PATH.read_bytes())

    failures = []

    try:
        # ── G1: plain-words overlap → DEMOTE (no hard block) ─────────────
        # Recent completion's key_finding shares plain English words with
        # the proposed goal. No file paths, no -_0-9 in shared keywords.
        # Expected: strong=True, has_specific=False -> advisory only.
        _seed_state({
            "goal_id": "g-scs-filler-G1",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Refactored cache and streaming buffers in monitoring "
                "throughout the metrics stack."
            ),
        })
        case_g1 = {
            "title": "Optimize cache and streaming behavior",
            "description": (
                "Tune the cache buffers and streaming concurrency "
                "settings in the metrics pipeline."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg1 = _run_gate(case_g1)
        rc1 = _find_check(rg1, "recent_completions")
        if rc1 is None:
            failures.append("G1: recent_completions check missing from result")
        elif rc1.get("passed") is not True:
            failures.append(
                f"G1: recent_completions should have passed (plain-words demote). "
                f"reason={rc1.get('reason')} matches={rc1.get('matches')}"
            )
        else:
            advisories = rc1.get("advisories") or []
            strong_only = [a for a in advisories if a.get("strong_keyword_only")]
            if not strong_only:
                failures.append(
                    "G1: expected strong_keyword_only advisory (demoted overlap), "
                    f"got advisories={advisories}"
                )

        # ── G2: file-path overlap → BLOCK ────────────────────────────────
        # Proposed goal mentions a file the recent completion also touched
        # (key_finding contains the file path). has_specific = bool(hit_paths)
        # = True -> hard block.
        _seed_state({
            "goal_id": "g-scs-filler-G2",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Fixed cache concurrency bug in core/scripts/buffer-handler.py "
                "after the recent refactor."
            ),
        })
        case_g2 = {
            "title": "Optimize buffer-handler.py concurrency",
            "description": (
                "Refactor cache and concurrency layers in "
                "core/scripts/buffer-handler.py."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg2 = _run_gate(case_g2)
        rc2 = _find_check(rg2, "recent_completions")
        if rc2 is None:
            failures.append("G2: recent_completions check missing from result")
        elif rc2.get("passed") is not False:
            failures.append(
                f"G2: recent_completions should have failed (file-path block). "
                f"reason={rc2.get('reason')} matches={rc2.get('matches')}"
            )
        else:
            matches = rc2.get("matches") or []
            if not matches:
                failures.append("G2: expected matches non-empty on file-path block")
            elif not any(m.get("file_path_hits") for m in matches):
                failures.append(
                    f"G2: expected file_path_hits in matches, got "
                    f"{[m.get('file_path_hits') for m in matches]}"
                )

        # ── G3: structured-identifier hit_kw → BLOCK ─────────────────────
        # No file paths, but a keyword carrying hyphens+digits (a goal-id)
        # is shared. has_specific = re.search(r"[-_0-9]", k) -> True ->
        # hard block. This is the rare-identifier path (rb-335).
        _seed_state({
            "goal_id": "g-scs-filler-G3",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Closed g-115-100 by adjusting recurring cadence in the "
                "scheduler module."
            ),
        })
        case_g3 = {
            "title": "Recurring cadence work referencing g-115-100 patterns",
            "description": (
                "Apply cadence adjustments referenced in the g-115-100 "
                "outcome record."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg3 = _run_gate(case_g3)
        rc3 = _find_check(rg3, "recent_completions")
        if rc3 is None:
            failures.append("G3: recent_completions check missing from result")
        elif rc3.get("passed") is not False:
            failures.append(
                f"G3: recent_completions should have failed (structured-id block). "
                f"reason={rc3.get('reason')} matches={rc3.get('matches')}"
            )
        else:
            matches = rc3.get("matches") or []
            if not matches:
                failures.append("G3: expected matches non-empty on structured-id block")
            else:
                has_structured_kw = False
                for m in matches:
                    for k in (m.get("keyword_hits") or []):
                        if any(ch in k for ch in "-_0123456789"):
                            has_structured_kw = True
                            break
                    if has_structured_kw:
                        break
                if not has_structured_kw:
                    failures.append(
                        f"G3: expected at least one keyword_hit with -_0-9, got "
                        f"{[m.get('keyword_hits') for m in matches]}"
                    )

    finally:
        if TEAM_STATE_BACKUP.exists():
            TEAM_STATE_PATH.write_bytes(TEAM_STATE_BACKUP.read_bytes())
            TEAM_STATE_BACKUP.unlink()
        if FINDINGS_BACKUP.exists():
            FINDINGS_PATH.write_bytes(FINDINGS_BACKUP.read_bytes())
            FINDINGS_BACKUP.unlink()

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (3/3 cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
