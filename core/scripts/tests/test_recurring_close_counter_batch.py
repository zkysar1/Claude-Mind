""": a recurring close writes its counter fields through ONE goal-store rewrite.

The counter block of `recurring-close.sh` wrote consecutive_routine,
consecutive_deep, last_outcome_origin, substantive_runs, substantive_hits,
last_substantive_at and pull_signal as separate `aspirations.py update-goal`
calls, and on a whole-object-PUT store each call is a full rewrite of
aspirations.jsonl (g-115-11231: eight to one goal in 37 s). It now lands them in
one daemon write and fails the close, naming the fields, on any it cannot confirm.

THE COUNT IS TAKEN FROM THE CHANGELOG, NOT FROM A HOOK ON THE DAEMON. Both writers
-- the CLI the old block used and the daemon route the new one uses -- append one
row per store rewrite to `<world>/changelog.jsonl`, so the same assertion measures
either implementation. That is what lets the mutation proof (restore the per-field
calls) turn THIS test red rather than a different one.

The block under test is the REAL heredoc, extracted from recurring-close.sh and
executed against a tmp world served by the shared in-process daemon fixture; a
test that restated it would keep passing after production drifted.

Run: STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_recurring_close_counter_batch.py -v
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO_ROOT = CORE_SCRIPTS.parent.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")
from _daemon_fixture import DaemonFixture  # noqa: E402

SRC = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
GID = "g-100-01"
ALL_SEVEN = ["consecutive_routine", "consecutive_deep", "last_outcome_origin",
             "substantive_runs", "substantive_hits", "last_substantive_at", "pull_signal"]


def _heredoc() -> str:
    i = SRC.index("finalize_counters() {")
    h = SRC.index("python3 - <<'PYEOF'", i)
    start = SRC.index("\n", h) + 1
    return SRC[start:SRC.index("\nPYEOF\n", start) + 1]


HEREDOC = _heredoc()


def _make_world(tmp: Path) -> Path:
    """A recurring goal whose GENUINE deep close writes all seven fields:
    deep+genuine advances consecutive_deep and substantive_hits, and the goal
    carries a pull_signal for the close to clear. consecutive_deep 0 -> 1 stays
    under the auto-contract threshold, so no tuner touches the store."""
    world = tmp / "world"
    world.mkdir()
    goal = {"id": GID, "title": "Recurring goal", "description": "fixture", "status": "pending",
            "priority": "MEDIUM", "recurring": True, "interval_hours": 4.0,
            "consecutive_routine": 0, "consecutive_deep": 0, "substantive_runs": 4,
            "substantive_hits": 1, "pull_signal": {"reason": "pulled"}, "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "participants": ["agent"]}
    asp = {"id": "asp-100", "title": "Test", "motivation": "t", "scope": "project",
           "priority": "MEDIUM", "status": "active", "created": "2026-09-25T00:00:00",
           "goals": [goal]}
    (world / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _goal(world: Path) -> dict:
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        for g in json.loads(line).get("goals", []):
            if g["id"] == GID:
                return g
    raise AssertionError("fixture goal vanished from the tmp store")


def _store_rewrites(world: Path) -> list:
    """Changelog rows for aspirations.jsonl: one per store rewrite, by either writer."""
    log = world / "changelog.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if r.get("file") == "aspirations.jsonl"]


def _run(tmp: Path, world: Path, *, daemon: bool = True, outcome: str = "deep"):
    """Run the real heredoc. daemon=False points the child at an EMPTY runtime dir,
    so it can reach neither the fixture nor this box's live daemon."""
    failure_file = tmp / "counter-failure.txt"
    failure_file.write_text("", encoding="utf-8")
    meta, agent = tmp / "meta", tmp / "agent"
    (agent / "session").mkdir(parents=True)
    meta.mkdir()

    def go():
        env = dict(os.environ)
        for key in ("BODY_ROLE", "MIND_SID", "RT_PORT_FILE"):
            env.pop(key, None)
        if not daemon:
            (tmp / "no-daemon").mkdir()
            env["RT_DIR"] = str(tmp / "no-daemon")
        env.update({
            "GID": GID, "SF": str(world / "aspirations.jsonl"), "OUTCOME": outcome,
            "OUTCOME_ORIGIN": "genuine", "SRC_FLAG": "world", "SD": str(CORE_SCRIPTS),
            "NOW": "2026-10-03T12:00:00", "INTERVAL_REVIEW_FILE": str(tmp / "review.txt"),
            "COUNTER_FAILURE_FILE": str(failure_file),
            "MIND_WORLD": str(world), "MIND_META": str(meta),
            "MIND_AGENT": "alpha", "MIND_AGENT_DIR": str(agent),
            "BODY_WM_PATH": str(tmp / "wm.yaml"),  # guard-3375: nothing may reach a live WM
            "STORAGE_BACKEND": "local",  # guard-955
        })
        return subprocess.run([sys.executable, "-"], input=HEREDOC, capture_output=True,
                              text=True, env=env, cwd=str(REPO_ROOT), timeout=120)

    if daemon:
        with DaemonFixture(world):
            proc = go()
    else:
        proc = go()
    return proc, failure_file.read_text(encoding="utf-8")


def test_a_close_is_one_store_rewrite(tmp_path):
    world = _make_world(tmp_path)
    assert _store_rewrites(world) == []  # nothing before the close
    proc, failure = _run(tmp_path, world)
    assert proc.returncode == 0, proc.stderr
    rewrites = _store_rewrites(world)
    assert len(rewrites) == 1, (
        f"a recurring close rewrote the goal store {len(rewrites)} times; the counter "
        "block must land in ONE rewrite (the per-field path made up to eight): "
        f"{[r['summary'] for r in rewrites]}")
    assert rewrites[0]["summary"].startswith(f"update-goal-fields {GID} consecutive_routine,")
    assert failure == "", "a clean close must leave the failure file empty"


def test_every_field_is_persisted_as_the_close_computed_it(tmp_path):
    world = _make_world(tmp_path)
    proc, _ = _run(tmp_path, world)
    assert proc.returncode == 0, proc.stderr
    g = _goal(world)
    assert (g["consecutive_routine"], g["consecutive_deep"], g["substantive_runs"],
            g["substantive_hits"]) == (0, 1, 5, 2)
    assert g["last_outcome_origin"] == "genuine" and g["last_substantive_at"] == "2026-10-03T12:00:00"
    assert "pull_signal" in g and g["pull_signal"] is None  # null, never key removal
    assert f"{GID}: outcome_origin=genuine consecutive_deep=0→1" in proc.stderr
    assert "pull_signal CLEARED" in proc.stderr


def test_one_unconfirmed_field_fails_the_close_naming_exactly_it(tmp_path, monkeypatch):
    """Failure injection. The authoritative store carries a stale substantive_runs;
    the close must fail and name THAT field -- not all seven, and not none."""
    from mind_api.src.endpoints import aspirations_write as aw
    world = _make_world(tmp_path)

    def stale(live_path, asp_id, goal_id):
        g = copy.deepcopy(_goal(world))
        g["substantive_runs"] = 4  # the pre-close value
        return "found", g

    monkeypatch.setattr(aw, "_authoritative_goal_lookup", stale)
    proc, failure = _run(tmp_path, world)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "COUNTER WRITE FAILED" in proc.stderr
    named = proc.stderr.split("unwritten fields: ", 1)[1].split(" — ", 1)[0]
    assert named == "substantive_runs", named
    names, *retry = failure.splitlines()
    assert names == "substantive_runs"
    assert retry == ["bash core/scripts/aspirations-update-goal.sh --source world "
                     f"{GID} substantive_runs 5"], retry
    assert "pull_signal CLEARED" not in proc.stderr, "a failed close must not narrate success"
    assert "consecutive_deep=0→1" not in proc.stderr, "nor claim the transition it did not confirm"


def test_the_confirming_read_back_does_not_fail_the_close(tmp_path, monkeypatch):
    """Positive control for the failure test: the SAME hook, returning the goal as
    written, lets the close through, so that failure came from the stale field."""
    from mind_api.src.endpoints import aspirations_write as aw
    world = _make_world(tmp_path)
    monkeypatch.setattr(aw, "_authoritative_goal_lookup",
                        lambda *a: ("found", copy.deepcopy(_goal(world))))
    proc, _ = _run(tmp_path, world)
    assert proc.returncode == 0, proc.stderr


def test_an_unreachable_daemon_names_every_field(tmp_path):
    world = _make_world(tmp_path)
    before = (world / "aspirations.jsonl").read_bytes()
    proc, failure = _run(tmp_path, world, daemon=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    named = proc.stderr.split("unwritten fields: ", 1)[1].split(" — ", 1)[0]
    assert named.split(", ") == ALL_SEVEN
    assert failure.splitlines()[0] == ",".join(ALL_SEVEN)
    assert (world / "aspirations.jsonl").read_bytes() == before, "no fallback write may land"


def _failure_file_block() -> str:
    lines = SRC.split("\n")
    starts = [i for i, l in enumerate(lines) if l == 'if [[ -n "$_cff" ]]; then']
    assert len(starts) == 1, f"failure-file handler anchor matched {len(starts)} lines, want 1"
    end = next(i for i in range(starts[0] + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[starts[0]:end + 1])


def _run_failure_file_block(tmp_path: Path, content: str):
    cff = tmp_path / "cff.txt"
    cff.write_text(content, encoding="utf-8")
    script = f"""
set -uo pipefail
FAILED_PHASES="verify "; PHASE_RESULTS="verify=fail(3) "; FAILED_RETRY_CMDS="RETRY-VERIFY"$'\\n'
PY_RC=1
_cff={str(cff)!r}
{_failure_file_block()}
printf 'FAILED_PHASES=[%s]\\nPHASE_RESULTS=[%s]\\nRETRY=[%s]\\nFILE_LEFT=%s\\n' \\
    "$FAILED_PHASES" "$PHASE_RESULTS" "$FAILED_RETRY_CMDS" "$([ -e "$_cff" ] && echo yes || echo no)"
"""
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)


def test_the_banner_inputs_name_the_unwritten_fields(tmp_path):
    """The PHASE FAILURE banner prints FAILED_PHASES, PHASE_RESULTS and
    FAILED_RETRY_CMDS; a counter failure must reach all three, beside the phases
    that already failed rather than replacing them."""
    proc = _run_failure_file_block(
        tmp_path, "substantive_runs,pull_signal\n"
                  "bash core/scripts/aspirations-update-goal.sh --source world g-1-1 substantive_runs 5\n"
                  "bash core/scripts/aspirations-update-goal.sh --source world g-1-1 pull_signal null\n")
    assert proc.returncode == 0, proc.stderr
    assert "FAILED_PHASES=[verify counters[substantive_runs,pull_signal] ]" in proc.stdout
    assert "PHASE_RESULTS=[verify=fail(3) counters=fail(1) ]" in proc.stdout
    assert "RETRY=[RETRY-VERIFY\n" in proc.stdout
    assert "substantive_runs 5\nbash core/scripts/aspirations-update-goal.sh --source world g-1-1 pull_signal null" in proc.stdout
    assert "FILE_LEFT=no" in proc.stdout


def test_an_empty_failure_file_adds_nothing(tmp_path):
    proc = _run_failure_file_block(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert "FAILED_PHASES=[verify ]" in proc.stdout and "PHASE_RESULTS=[verify=fail(3) ]" in proc.stdout
    assert "FILE_LEFT=no" in proc.stdout
