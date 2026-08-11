"""test_per_goal_experience_check.py —  regression test.

Pins the PER-GOAL Phase 4.25 experience check after its extraction out of
recurring-close.sh into the shared `core/scripts/per-goal-experience-check.py`,
and pins the new wiring of that helper into the NON-recurring close path
(iteration-close.sh do_state_update).

THE DEFECT
----------
Both close paths leave the experience WRITE to the LLM (experience-add.sh).
What differed was ENFORCEMENT GRANULARITY:

  * recurring-close.sh ran a PER-GOAL check keyed on the specific goal_id and
    set force_experience_archival on a miss, which aspirations-precheck Phase
    0-pre2 then consumes to force a retro-compose.
  * iteration-close.sh (non-recurring) ran only experience-staleness-check.sh,
    which is STORE-level: newest-entry-of-any-kind vs a 12h threshold, with no
    goal_id join at all.

So the per-goal remedy existed and was wired to the path that needed it least.
Measured fleet-wide (echo, cc-03, 2026-08-02, joined against experience.jsonl +
experience-archive.jsonl + experience/*.md across 5 agents): non-recurring
completed goals with ANY experience record ran 16-32%; recurring goals — the one
lane where the check was wired — ran 95%.

WHAT THESE TESTS PIN
--------------------
1. Helper behavior: the 30-min window, the goal_id/source_goal DUAL match, the
   exact 4-key payload Phase 0-pre2 consumes, and always-exit-0 fail-open.
2. guard-2015: recurring-close.sh keeps NO fork of the extracted logic.
3. The non-recurring wiring exists in do_state_update, is gated on
   deep + not-recurring, and carries VISIBLE degradation (not a bare `|| true`).

Cross-refs:
  - g-115-4661 (this fix), g-115-4660 (zeta's measurement), g-115-547 (origin canary)
  - g-115-2511 / guard-697 / guard-713 (the goal_id vs source_goal seam)
  - guard-2015 (extract-and-delete-the-origin)
  - msg-20260801-171952-zeta-5643 (insight trigger: no bare `|| true` on this file)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
HELPER = CORE_SCRIPTS / "per-goal-experience-check.py"
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"
ITERATION_CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"

TRIGGER = "test-trigger"


# ───────────────────────────── fixtures ─────────────────────────────

def _sandbox_agent(entries):
    """Build a temp AGENT_DIR with an experience.jsonl holding `entries`.

    Returns (tmpdir, agent_dir). Caller removes tmpdir.
    `entries is None` means: do not create experience.jsonl at all.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pgec-"))
    agent_dir = tmp / "agents" / "testagent"
    (agent_dir / "session").mkdir(parents=True)
    if entries is not None:
        (agent_dir / "experience.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )
    return tmp, agent_dir


def _run(agent_dir: Path, goal_id: str, *extra):
    env = os.environ.copy()
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env["STORAGE_BACKEND"] = "local"          # guard-955
    return subprocess.run(
        [sys.executable, str(HELPER), "--goal-id", goal_id, "--trigger", TRIGGER, *extra],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _sentinel(agent_dir: Path):
    """force_experience_archival lives under data['slots'] (not TOP_LEVEL_KEYS)."""
    wm_path = agent_dir / "session" / "working-memory.yaml"
    if not wm_path.exists():
        return None
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
    return (wm.get("slots") or {}).get("force_experience_archival")


def _iso(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


# ───────────────────────── helper behavior ──────────────────────────

def test_recent_record_keyed_on_goal_id_suppresses_sentinel():
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(2)},
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "record exists — sentinel must NOT fire"
        assert "no sentinel needed" in r.stderr
    finally:
        _rm(tmp)


def test_recent_record_keyed_on_legacy_source_goal_also_suppresses():
    """: entries carrying only source_goal must still count.

    Dropping this fallback makes the sentinel FALSE-fire on closes whose record
    exists — the exact defect guard-697 / guard-713 describe from the write side.
    """
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "source_goal": "g-1", "goal_id": None, "created": _iso(2)},
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "source_goal match must suppress the sentinel"
    finally:
        _rm(tmp)


def test_record_outside_window_does_not_suppress():
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-1", "created": _iso(45)},   # > 30min
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, "stale record must not count as coverage"
    finally:
        _rm(tmp)


def test_record_for_a_different_goal_does_not_suppress():
    """The whole point of per-goal granularity: a fresh store is not coverage."""
    tmp, agent_dir = _sandbox_agent([
        {"id": "exp-1", "goal_id": "g-OTHER", "created": _iso(1)},
    ])
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        s = _sentinel(agent_dir)
        assert s is not None, "another goal's record must not cover this goal"
        assert s["goal_id"] == "g-1"
    finally:
        _rm(tmp)


def test_missing_store_fires_sentinel():
    tmp, agent_dir = _sandbox_agent(None)
    try:
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is not None, "no store at all == no record"
    finally:
        _rm(tmp)


def test_payload_shape_is_exactly_what_phase_0_pre2_consumes():
    tmp, agent_dir = _sandbox_agent([])
    try:
        r = _run(agent_dir, "g-1", "--original-outcome", "routine")
        assert r.returncode == 0, r.stderr
        s = _sentinel(agent_dir)
        assert s is not None
        assert set(s) == {"triggered_at", "trigger", "goal_id", "original_outcome"}, (
            f"payload shape drifted — Phase 0-pre2 consumes these 4 keys: {s}"
        )
        assert s["goal_id"] == "g-1"
        assert s["trigger"] == TRIGGER
        assert s["original_outcome"] == "routine"
        datetime.fromisoformat(s["triggered_at"])           # parseable
    finally:
        _rm(tmp)


def test_malformed_line_is_skipped_not_fatal():
    tmp, agent_dir = _sandbox_agent(None)
    try:
        (agent_dir / "experience.jsonl").write_text(
            "{not json\n" + json.dumps({"goal_id": "g-1", "created": _iso(1)}) + "\n",
            encoding="utf-8",
        )
        r = _run(agent_dir, "g-1")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None, "valid line after a malformed one must still match"
    finally:
        _rm(tmp)


def test_dry_run_reports_without_writing():
    tmp, agent_dir = _sandbox_agent([])
    try:
        r = _run(agent_dir, "g-1", "--dry-run")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["goal_id"] == "g-1"
        assert _sentinel(agent_dir) is None, "--dry-run must not write the sentinel"
    finally:
        _rm(tmp)


def test_empty_goal_id_is_a_noop_not_a_crash():
    """Fail-open: a check failure must never block a close."""
    tmp, agent_dir = _sandbox_agent([])
    try:
        r = _run(agent_dir, "")
        assert r.returncode == 0, r.stderr
        assert _sentinel(agent_dir) is None
    finally:
        _rm(tmp)


# ──────────────────── extraction + wiring invariants ────────────────────

def test_recurring_close_keeps_no_fork_of_the_extracted_logic():
    """guard-2015: the origin must not keep a copy, or it rots silently.

    Asserted on the DISTINCTIVE lines of the extracted block, not on the word
    `force_experience_archival` — recurring-close.sh legitimately still mentions
    the sentinel in its comments.
    """
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    for forked in (
        'e.get("goal_id"), e.get("source_goal")',
        '"trigger": "recurring-close-postflip-deep-no-recent-entry"',
        '"set", "force_experience_archival"',
    ):
        assert forked not in src, (
            f"recurring-close.sh still carries the extracted logic: {forked!r} "
            "(guard-2015 — delete the origin copy in the SAME change)"
        )
    assert "per-goal-experience-check.py" in src, (
        "recurring-close.sh must invoke the shared helper"
    )
    # The trigger label the helper now receives must still be the original one,
    # so consumers keyed on it are unaffected by the extraction.
    assert "recurring-close-postflip-deep-no-recent-entry" in src


def test_nonrecurring_close_path_invokes_the_helper_gated_and_loudly():
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    marker = "Phase 4.25 PER-GOAL experience check for NON-recurring deep closes"
    assert marker in src, "the non-recurring wiring is missing from iteration-close.sh"
    idx = src.index(marker)
    end = src.index("End Phase 4.25 per-goal experience check", idx)
    block = src[idx:end]

    # Assert on EXECUTABLE lines only. The block's own comments quote the very
    # strings under test (`|| true`), so a whole-block substring check reports a
    # prose mention as live code — the guard-2401 false-positive class, caught by
    # this test failing on its own first run.
    code = "\n".join(
        ln for ln in block.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    )

    assert "per-goal-experience-check.py" in code
    assert "_winpath" in code, "python3 <file-arg> in this file must route through _winpath"
    assert '"$OUTCOME" == "deep"' in code, "must fire only on deep closes"
    assert '"$_su_is_recurring" != "true"' in code, (
        "must NOT fire for recurring goals — recurring-close.sh already runs the check"
    )
    # The insight trigger this goal was filed under: degradation must be visible.
    assert "|| echo" in code and "WARN" in code, (
        "a bare `|| true` here makes the check undetectable in exactly the "
        "scenario it exists for (msg-20260801-171952-zeta-5643)"
    )
    assert "|| true" not in code, f"bare `|| true` in executable lines:\n{code}"


def test_store_level_check_is_untouched():
    """The store-level canary stays as the long-horizon backstop."""
    src = ITERATION_CLOSE_SH.read_text(encoding="utf-8")
    assert 'bash "$SCRIPT_DIR/experience-staleness-check.sh"' in src


def _rm(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)
