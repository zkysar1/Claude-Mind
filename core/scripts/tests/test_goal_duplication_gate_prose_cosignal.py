"""test_goal_duplication_gate_prose_cosignal.py — regression test for .

Verifies the PROSE-source structural-co-signal tightening in
`_check_pending_queue`: a prose-sourced proposal (no verification block) that
shares a structured keyword-identifier with a pending goal but NO file-path is
DEMOTED to advisory (not a hard block), while a shared FILE-PATH still blocks,
and a VERIFICATION-sourced proposal sharing the same structured identifier still
blocks (that path is unchanged — g-248-12).

Canonical FP (2026-07-08, alpha): a board-write latency-canary follow-up goal
(source=prose, verification=null) was hard-blocked by pending_queue
structural_overlap against its PARENT g-328-30 on 15 shared incident keywords
(board_write / append_jsonl_record / g-328-30 / 16ms — file_path_hits EMPTY).
The follow-up RECAPS its parent's incident to explain itself; it is a distinct
deliverable, not a duplicate. A [_0-9]-keyword co-signal alone must not
hard-block a prose proposal — only a file-path (work-target) should. Same FP
class named by g-115-1821 itself (blocked on "git_log_48h" framework vocab) and
g-001-302 / g-115-1819 / g-115-1820.

Cases (all assert on the pending_queue check; the other five checks are seeded
clean so only pending_queue can fire):
  C1 prose + shared structured keyword-identifier, NO file-path -> DEMOTE
     (pending_queue passed=True, strong_keyword_only advisory present) [THE FIX]
  C2 prose + shared FILE-PATH -> BLOCK (passed=False, file_path_hits present)
     [regression guard — file-path work-target still blocks]
  C3 verification-source + shared structured identifier, NO file-path -> BLOCK
     (passed=False) [regression guard — verification path unchanged, g-248-12]

Isolation (rb-1547): MIND_WORLD -> a tmp dir with a seeded aspirations.jsonl +
empty team-state/findings. Never reads or writes the live shared world.
Pytest-collectable via the thin test_* wrapper; also runnable standalone via
`py -3 core/scripts/tests/test_goal_duplication_gate_prose_cosignal.py`.
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

GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"

# Structured identifier (carries digits -> passes the [_0-9] co-signal) unique
# to the synthetic parent (df=1 across the candidate corpus -> passes the IDF
# floor). So the ONLY thing gating a hard block for a prose proposal is the
# file-path rule under test.
IDENT = "boardwrite_probe42"
SHARED_PATH = "core/scripts/gd1821-probe-shared.py"

# Filler pending goals sharing NO vocabulary with the parent, so IDF over the
# candidate corpus lifts the parent's rare terms above the weight threshold
# (mirrors _FILLERS in test_goal_duplication_gate_structural_co_signal.py).
_FILLERS = [
    "Renumber nested documentation anchors in the reference index catalog.",
    "Adjust orchestrator container thread pool sizing under the watcher layer.",
    "Prune historical entries from the deployment registry manifest collection.",
    "Reorganize curriculum chapter outlines within the reference set index.",
    "Tighten polling verbosity flags around the inventory watcher loop.",
    "Review boundary diagrams across the service registry deployment map.",
]


def _seed_pending(tmp_world: Path):
    """Seed aspirations.jsonl with ONE pending parent (prose carrying IDENT +
    SHARED_PATH + board-durability incident vocabulary) plus vocabulary-disjoint
    filler pending goals for IDF. Empty team-state + findings keep the other
    five checks clean so ONLY pending_queue can fire."""
    parent_desc = (
        f"Board durability incident: the daemon ACK'd 200 but persisted nowhere. "
        f"Root cause in {SHARED_PATH} — the {IDENT} write-behind buffer was lost "
        f"at restart. Durable append semantics under investigation."
    )
    goals = [{
        "id": "g-gd1821-parent", "status": "pending", "participants": ["agent"],
        "title": "Investigate: board write silently dropped incident",
        "description": parent_desc,
        "origin_signal": "investigate:board-drop-probe",
    }]
    for i, kf in enumerate(_FILLERS):
        goals.append({
            "id": f"g-gd1821-noise-{i:02d}", "status": "pending",
            "participants": ["agent"], "title": f"Idea: filler {i}",
            "description": kf, "origin_signal": f"idea:gd1821-noise-{i:02d}",
        })
    asp = {"id": "asp-gd1821", "status": "active", "goals": goals}

    tmp_world.mkdir(parents=True, exist_ok=True)
    with open(tmp_world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp) + "\n")
    with open(tmp_world / "team-state.yaml", "w", encoding="utf-8") as f:
        f.write("recent_completions: []\nagent_status: {}\n")
    findings = tmp_world / "board" / "findings.jsonl"
    findings.parent.mkdir(parents=True, exist_ok=True)
    findings.write_text("", encoding="utf-8")


def _run_gate(goal: dict, tmp_world: Path, agent: str = "alpha") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(tmp_world)
    # Hermetic agent-queue scan (): keep live agent queues out
    # of the wrapper's pending_queue check (rb-3784 corpus coupling).
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)], input=json.dumps(goal),
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"gate rc={proc.returncode}: {proc.stderr[:400]}")
    return json.loads(proc.stdout)


def _pendq(result: dict) -> dict:
    for c in result.get("checks", []):
        if c.get("name") == "pending_queue":
            return c
    return {}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gd1821-prose-cosignal-"))
    world = tmp / "world"
    failures: list[str] = []
    try:
        _seed_pending(world)

        # C1 — THE FIX: prose proposal sharing IDENT + incident vocab, NO
        # file-path. Pre-fix this hard-blocked (strong + [_0-9] co-signal);
        # post-fix it is demoted to a strong_keyword_only advisory.
        c1 = {
            "id": "g-gd1821-c1",
            "title": "Idea: latency canary for the board write path",
            "description": (
                f"Follow-up hardening. The {IDENT} buffer durability incident "
                f"showed a fast ACK at restart. Add a runtime latency canary "
                f"that warns on suspiciously fast board persistence."),
            "participants": ["agent"], "origin_signal": "idea:latency-canary-probe",
        }
        r1 = _pendq(_run_gate(c1, world))
        if not r1.get("passed", None):
            failures.append(
                f"C1: prose keyword-identifier-only overlap MUST demote (passed=True), "
                f"got passed={r1.get('passed')} reason={r1.get('reason')}")
        else:
            advs = r1.get("advisories") or []
            if not any(a.get("strong_keyword_only") for a in advs):
                failures.append(
                    "C1: expected a strong_keyword_only advisory proving the overlap "
                    f"was strong-but-demoted (else the case is trivial), got {advs}")

        # C2 — regression guard: prose proposal sharing SHARED_PATH (a work-
        # target file). File-path co-signal must STILL block.
        c2 = {
            "id": "g-gd1821-c2",
            "title": "Fix: rework the board write retry path",
            "description": (
                f"Rework {SHARED_PATH} to add retry/backoff on the durable "
                f"append. Board durability incident follow-up."),
            "participants": ["agent"], "origin_signal": "idea:board-retry-probe",
        }
        r2 = _pendq(_run_gate(c2, world))
        if r2.get("passed", True):
            failures.append(
                f"C2: prose FILE-PATH overlap MUST still block (passed=False), "
                f"got passed={r2.get('passed')} reason={r2.get('reason')}")
        elif not any(m.get("file_path_hits") for m in (r2.get("matches") or [])):
            failures.append(
                f"C2: expected file_path_hits in the blocking match, got "
                f"{[m.get('file_path_hits') for m in (r2.get('matches') or [])]}")

        # C3 — regression guard: verification-sourced proposal naming IDENT, NO
        # file-path. Verification path is unchanged -> still blocks ().
        c3 = {
            "id": "g-gd1821-c3",
            "title": "Investigate the board write durability path",
            "description": "short",
            "verification": {"outcomes": [
                f"The {IDENT} buffer durability incident restart behavior "
                f"root-caused with durable append semantics confirmed"]},
            "participants": ["agent"], "origin_signal": "investigate:verif-probe",
        }
        r3 = _pendq(_run_gate(c3, world))
        if r3.get("passed", True):
            failures.append(
                f"C3: verification-sourced structured-identifier overlap MUST still "
                f"block (passed=False), got passed={r3.get('passed')} "
                f"reason={r3.get('reason')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: prose structural co-signal — C1 demote (fix), C2 file-path block, "
          "C3 verification block (g-115-1821)")
    return 0


def test_goal_duplication_gate_prose_cosignal():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
