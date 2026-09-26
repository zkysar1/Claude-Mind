""": recurring-close.sh must READ what its interval tuners write.

THE DEFECT. cargo-cult-detector's auto-contract lowered a directive-set cadence
three times, and each time an agent restored it by hand, having caught it only
by reading one line of a long close's stdout. The detector already printed a
line on every move (`[cargo-cult-contract] auto-contracted ...`), but nothing
consumed that line, so a tuner could reverse a human decision in silence: the
writer-without-reader shape, with a detector as the unread writer.

THE FIX (the READER half; the PREVENTION half, the interval_pinned_by pin, lives
in cargo-cult-detector.py). finalize_counters' heredoc captures interval_hours
BEFORE the tuner step and re-reads the PERSISTED value after it. On a move it
renders a review: the pin question, guard-3060's second question on a
contraction, and the exact restore command. bash prints it ONCE, before every
terminal form, and folds a pointer to it into each NEXT ACTION line.

WHAT IS PINNED HERE:
  A. DETECTION -- the REAL heredoc, run on a tmp world, writes the review when
     auto-contract moves the interval, and writes NOTHING when the tuner does not
     run, or runs and declines (the benign case, guard-6447). Detection keys on
     the store, so a declined tuner must stay silent.
  B. DELIVERY -- the REAL terminal region prints the review once, ahead of the
     banner. Each NEXT ACTION line (reducer clean, repair step 3, worker) carries
     the pointer and is byte-identical to its no-review form once the pointer is
     removed. With no review, the region's output is exactly the banner plus the
     unchanged block.
  C. EVERY TERMINAL FORM -- the landing exit prints the review before it exits
     (rb-11640). A run killed before any terminal form prints it from the EXIT
     trap, and a normal exit never prints it twice.
  D. THE REDUCER'S REMINDER -- iteration-close-reminder.py, whose system-reminder
     outranks this stdout, leads with the review when fed the real bytes above,
     and is unchanged when no review printed.

Every block is EXTRACTED from recurring-close.sh and EXECUTED, never restated,
the same pattern as test_recurring_close_failure_imperative.py. A test that
restated a block would pass against its own copy while production drifted.

Run: STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_recurring_close_interval_review.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")

SRC = RECURRING_CLOSE_SH.read_text(encoding="utf-8")

GATE = " (settle the INTERVAL MOVED review above first)"
REVIEW_ANCHOR = "# INTERVAL-MOVE REVIEW (g-115-6612)"
LANDING_ANCHOR = "# STATE-MISMATCH LANDING (g-357-51)"
BLOCK_START = "# The proceed text is COMPUTED ONCE"
END = "A Bash echo or text summary as the terminal action kills the loop"
BANNER = "[recurring-close] ═══ ITERATION COMPLETE ═══"

# A stand-in review for the delivery tests: the region prints whatever
# INTERVAL_REVIEW holds, so its wording is the heredoc's concern (section A).
FIXTURE_REVIEW = (
    "[recurring-close] ⚠ INTERVAL MOVED — REVIEW-FIXTURE line 1\n"
    "[recurring-close]   REVIEW-FIXTURE line 2"
)


# ─── extraction ─────────────────────────────────────────────────────────────


def _between(start: str, end: str, *, include_end_line: bool) -> str:
    assert SRC.count(start) == 1, f"anchor {start!r} must match exactly once"
    i = SRC.find(start)
    j = SRC.find(end, i)
    assert j > i, f"end anchor {end!r} not found after {start!r}"
    return SRC[i:SRC.find("\n", j) + 1] if include_end_line else SRC[i:j]


def _slice(first: str, last: str, label: str) -> str:
    """The real text from line `first` through line `last`, both exact lines."""
    lines = SRC.split("\n")
    starts = [i for i, l in enumerate(lines) if l == first]
    ends = [i for i, l in enumerate(lines) if l == last]
    assert len(starts) == 1, f"{label}: opening anchor matched {len(starts)} lines"
    assert len(ends) == 1, f"{label}: closing anchor matched {len(ends)} lines"
    assert starts[0] < ends[0], f"{label}: anchors out of order"
    return "\n".join(lines[starts[0]:ends[0] + 1])


def _finalize_heredoc() -> str:
    i = SRC.index("finalize_counters() {")
    h = SRC.index("python3 - <<'PYEOF'", i)
    body_start = SRC.index("\n", h) + 1
    body_end = SRC.index("\nPYEOF\n", body_start)
    return SRC[body_start:body_end + 1]


REGION = _between(REVIEW_ANCHOR, END, include_end_line=True)
REVIEW_PRINT = _between(REVIEW_ANCHOR, LANDING_ANCHOR, include_end_line=False)
BLOCK = _between(BLOCK_START, END, include_end_line=True)
HEREDOC = _finalize_heredoc()


def _assign(name: str, value: str) -> str:
    """bash ANSI-C assignment that reproduces `value` byte-for-byte."""
    esc = value.replace("\\", "\\\\").replace("'", r"\'").replace("\n", "\\n")
    return f"{name}=$'{esc}'\n"


# ─── A. DETECTION: the real heredoc on a tmp world ──────────────────────────


def _make_world(tmp: Path, *, interval_hours: float, consecutive_deep: int,
                calibration_exempt: bool | None = None) -> Path:
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": "g-100-01",
        "title": "Recurring deep-prone goal",
        "description": "interval review fixture",
        "status": "pending",
        "priority": "MEDIUM",
        "recurring": True,
        "interval_hours": interval_hours,
        "consecutive_routine": 0,
        "consecutive_deep": consecutive_deep,
        "achievedCount": consecutive_deep + 1,
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "participants": ["agent"],
    }
    if calibration_exempt is not None:
        goal["calibration_exempt"] = calibration_exempt
    asp = {"id": "asp-100", "title": "Test asp", "motivation": "Test",
           "scope": "project", "priority": "MEDIUM", "status": "active",
           "created": "2026-09-25T00:00:00", "goals": [goal]}
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _interval(world: Path):
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            for g in json.loads(line).get("goals", []):
                if g.get("id") == "g-100-01":
                    return g.get("interval_hours")
    raise AssertionError("fixture goal vanished from the tmp store")


def _run_heredoc(tmp: Path, world: Path, *, outcome: str):
    review_file = tmp / "review.txt"
    review_file.write_text("", encoding="utf-8")
    meta = tmp / "meta"
    meta.mkdir()
    agent = tmp / "agent"
    (agent / "session").mkdir(parents=True)
    env = dict(os.environ)
    for key in ("BODY_ROLE", "MIND_SID"):
        env.pop(key, None)
    env.update({
        "GID": "g-100-01", "SF": str(world / "aspirations.jsonl"),
        "OUTCOME": outcome, "OUTCOME_ORIGIN": "genuine", "SRC_FLAG": "world",
        "SD": str(CORE_SCRIPTS), "NOW": "2026-09-25T00:00:00",
        "INTERVAL_REVIEW_FILE": str(review_file),
        "MIND_WORLD": str(world), "MIND_META": str(meta),
        "MIND_AGENT": "alpha", "MIND_AGENT_DIR": str(agent),
        # guard-3375 / guard-862: a worker box injects the LIVE Body WM path into
        # every process; point it at a throwaway file so nothing here can reach it.
        "BODY_WM_PATH": str(tmp / "wm.yaml"),
        "STORAGE_BACKEND": "local",  # guard-955
    })
    r = subprocess.run([sys.executable, "-"], input=HEREDOC, capture_output=True,
                       text=True, env=env, cwd=str(PROJECT_ROOT), timeout=120)
    return r, review_file.read_text(encoding="utf-8")


def test_contraction_writes_the_review(tmp_path):
    """consecutive_deep 2 -> 3 fires auto-contract (4.0 / 1.5 = 2.67): the review
    names the move, asks both questions and carries the exact restore command."""
    world = _make_world(tmp_path, interval_hours=4.0, consecutive_deep=2)
    r, review = _run_heredoc(tmp_path, world, outcome="deep")
    assert r.returncode == 0, r.stderr
    # Positive control: the tuner really moved the PERSISTED value.
    assert _interval(world) == 2.67, (_interval(world), r.stdout, r.stderr)
    assert "auto-contracted" in r.stdout, r.stdout  # the detector's own line still prints
    assert ("INTERVAL MOVED — g-100-01 interval_hours 4.0h -> 2.67h across this "
            "close's auto-contract step (consecutive_deep=3, "
            "original_interval_hours=4.0)") in review, review
    assert "Q1: was 4.0h a HUMAN decision" in review, review
    assert "interval_pinned_by" in review, review
    assert "Q2 (guard-3060)" in review, review
    assert ("Restore: bash core/scripts/aspirations-update-goal.sh --source world "
            "g-100-01 interval_hours 4.0") in review, review
    lines = [l for l in review.split("\n") if l]
    assert lines and all(l.startswith("[recurring-close] ") for l in lines), review
    # Handed to bash through the file, not printed inline a second time.
    assert "INTERVAL MOVED" not in r.stdout, r.stdout


def test_no_tuner_run_writes_nothing(tmp_path):
    """consecutive_deep 0 -> 1 is below the threshold: no tuner, no review."""
    world = _make_world(tmp_path, interval_hours=4.0, consecutive_deep=0)
    r, review = _run_heredoc(tmp_path, world, outcome="deep")
    assert r.returncode == 0, r.stderr
    assert _interval(world) == 4.0
    assert review == "", review
    assert "INTERVAL MOVED" not in r.stdout + r.stderr


def test_a_tuner_that_declines_writes_nothing(tmp_path):
    """The benign case (guard-6447): auto-contract RUNS on a calibration_exempt
    goal and declines. The value did not move, so there is nothing to review.
    This is the row that separates keying on the store from keying on the
    tuner having run."""
    world = _make_world(tmp_path, interval_hours=4.0, consecutive_deep=2,
                        calibration_exempt=True)
    r, review = _run_heredoc(tmp_path, world, outcome="deep")
    assert r.returncode == 0, r.stderr
    assert "calibration_exempt" in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert _interval(world) == 4.0
    assert review == "", review


def test_both_interval_writing_tuners_are_marked():
    """Structural, and deliberately so: the auto-EXTEND branch is unreachable
    end-to-end under the shipped config (batch_audit_dedupe_hours > 0 routes a
    routine streak to --audit-all, which files an Idea and writes no interval).
    If someone disables batching, that branch writes interval_hours again, and
    the review must already cover it."""
    extend = 'tuner = ("auto-extend", f"consecutive_routine={new_val}")'
    contract = 'tuner = ("auto-contract", f"consecutive_deep={new_deep}")'
    assert HEREDOC.count(extend) == 1 and HEREDOC.count(contract) == 1
    legacy = HEREDOC.index("# Legacy per-goal path")
    assert legacy < HEREDOC.index(extend) < HEREDOC.index("cargo-cult-detector failed:")
    assert (HEREDOC.index("if outcome == \"deep\" and new_deep >= contract_threshold:")
            < HEREDOC.index(contract) < HEREDOC.index("--contract-mode\"],"))


# ─── B. DELIVERY: the real terminal region ──────────────────────────────────


def _run_region(tmp: Path, *, review: str = "", body_role: str | None = None,
                max_rc: int = 0, outcome: str = "deep", deadman_disabled: bool = False,
                failed: bool = False, landing: bool = False, block: str = REGION):
    stub_dir = tmp / "scripts"
    stub_dir.mkdir(parents=True, exist_ok=True)
    # Stubs, so a test never runs the real landing (it can move agent-state) and
    # the worker terminal stays observable without the real emitter's deps.
    (stub_dir / "deadman-directive.sh").write_text('echo "STUB-WORKER-TERMINAL"\n')
    if landing:
        (stub_dir / "state-mismatch-landing.sh").write_text('echo "STUB-LANDING"\nexit 0\n')
    core_root = tmp / "core"
    (core_root / "logs").mkdir(parents=True, exist_ok=True)
    agent_dir = tmp / "agent"
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    if deadman_disabled:
        (agent_dir / "session" / "deadman-disabled").write_text("")
    role_line = ("unset BODY_ROLE\n" if body_role is None
                 else f"export BODY_ROLE={body_role!r}\n")
    script = (
        "set -uo pipefail\n"  # production's own options: an unbound name must fail here
        + role_line
        + f"SCRIPT_DIR={stub_dir.as_posix()!r}\n"
        + f"CORE_ROOT={core_root.as_posix()!r}\n"
        + f"AGENT_DIR={agent_dir.as_posix()!r}\n"
        + f"MAX_RC={max_rc}\nOUTCOME={outcome!r}\nGOAL_ID='g-100-01'\n"
        + (f"FAILED_PHASES='verify '\nPHASE_RESULTS='verify=fail(1) '\n"
           if failed else "FAILED_PHASES=''\nPHASE_RESULTS=''\n")
        + _assign("FAILED_RETRY_CMDS",
                  "bash core/scripts/iteration-close.sh --phase verify --goal g-100-01\n"
                  if failed else "")
        + _assign("INTERVAL_REVIEW", review)
        + "INTERVAL_REVIEW_PRINTED=0\n"
        + block
    )
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       cwd=str(tmp), timeout=30)
    # Only the landing exits inside the extracted text (`exit $MAX_RC`); every
    # other path ends on the kill-the-loop echo.
    want_rc = max_rc if landing else 0
    assert r.returncode == want_rc, f"rc={r.returncode}; stderr: {r.stderr}"
    return r.stdout


def _line(out: str, needle: str) -> str:
    hits = [l for l in out.split("\n") if needle in l]
    assert len(hits) == 1, f"expected one line with {needle!r}, got {hits!r}"
    return hits[0]


def _assert_printed_once_before_banner(out: str):
    assert out.count("REVIEW-FIXTURE line 1") == 1, out
    assert out.count("REVIEW-FIXTURE line 2") == 1, out
    assert out.index("REVIEW-FIXTURE line 2") < out.index(BANNER), out


@pytest.mark.parametrize("outcome", ["deep", "routine"])
@pytest.mark.parametrize("deadman_disabled", [False, True])
def test_reducer_clean_line_carries_the_pointer(tmp_path, outcome, deadman_disabled):
    kw = dict(outcome=outcome, deadman_disabled=deadman_disabled)
    plain = _line(_run_region(tmp_path / "a", **kw), "NEXT ACTION REQUIRED")
    out = _run_region(tmp_path / "b", review=FIXTURE_REVIEW, **kw)
    gated = _line(out, "NEXT ACTION REQUIRED")
    _assert_printed_once_before_banner(out)
    assert "NEXT ACTION REQUIRED" + GATE + ": " in gated, gated
    assert gated.replace(GATE, "", 1) == plain, (gated, plain)
    assert out.index(BANNER) < out.index(gated), out


def test_repair_step_3_carries_the_pointer(tmp_path):
    plain = _line(_run_region(tmp_path / "a", max_rc=1, failed=True),
                  "3. ONLY once the retry succeeds")
    out = _run_region(tmp_path / "b", review=FIXTURE_REVIEW, max_rc=1, failed=True)
    gated = _line(out, "3. ONLY once the retry succeeds")
    _assert_printed_once_before_banner(out)
    assert "re-enter the loop" + GATE + ": OUTCOME=" in gated, gated
    assert gated.replace(GATE, "", 1) == plain, (gated, plain)
    assert "REPAIR FIRST" in out  # the repair ordering itself is untouched


def test_worker_line_carries_the_pointer(tmp_path):
    plain = _line(_run_region(tmp_path / "a", body_role="worker"), "(worker Body)")
    out = _run_region(tmp_path / "b", review=FIXTURE_REVIEW, body_role="worker")
    gated = _line(out, "(worker Body)")
    _assert_printed_once_before_banner(out)
    assert "NEXT ACTION REQUIRED" + GATE + ": " in gated, gated
    assert gated.replace(GATE, "", 1) == plain, (gated, plain)
    assert out.index(gated) < out.index("STUB-WORKER-TERMINAL"), out


@pytest.mark.parametrize("body_role,max_rc,failed", [
    (None, 0, False), (None, 1, True), ("worker", 0, False)])
def test_no_review_adds_nothing(tmp_path, body_role, max_rc, failed):
    """Negative control: the region with no review prints exactly the banner
    plus what the unchanged NEXT-ACTION block prints on its own."""
    kw = dict(body_role=body_role, max_rc=max_rc, failed=failed)
    out = _run_region(tmp_path / "a", **kw)
    alone = _run_region(tmp_path / "b", block=BLOCK, **kw)
    assert out == "\n" + BANNER + "\n" + alone, (out, alone)
    assert "INTERVAL MOVED" not in out and GATE not in out


def test_landing_exit_prints_the_review_before_it_exits(tmp_path):
    """rb-11640: the landing is a terminal form too. It exits before the banner,
    so a review printed after the landing check would never reach it."""
    out = _run_region(tmp_path, review=FIXTURE_REVIEW, landing=True)
    assert out.count("REVIEW-FIXTURE line 1") == 1, out
    assert out.index("REVIEW-FIXTURE line 2") < out.index("STUB-LANDING"), out
    assert BANNER not in out, out


# ─── C. the EXIT trap: a run that never reached a terminal form ─────────────


@pytest.mark.parametrize("kill_at", ["before-finalize", "after-finalize", None])
def test_trap_prints_an_unprinted_review_exactly_once(tmp_path, kill_at):
    flags = _slice("COUNTERS_OWED=0", "PY_RC=0", "flag block")
    trap_block = _slice("_recurring_close_on_exit() {", "trap 'exit 130' INT", "trap block")
    guards = _slice("finalize_counters() {", "    COUNTERS_FINALIZED=1", "guard block")
    script = f"""
set -uo pipefail
FAILED_PHASE=""
{flags}

{trap_block}

{guards}
    INTERVAL_REVIEW="[recurring-close] REVIEW-FIXTURE"
}}

COUNTERS_OWED=1
{'kill -TERM $$' if kill_at == 'before-finalize' else ''}
finalize_counters
{'kill -TERM $$' if kill_at == 'after-finalize' else ''}
{REVIEW_PRINT}
exit 0
"""
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
    if kill_at is None:
        # A normal exit: the terminal region printed it; the trap must not repeat it.
        assert r.returncode == 0, r.stderr
        assert r.stdout.count("REVIEW-FIXTURE") == 1, r.stdout
        assert "REVIEW-FIXTURE" not in r.stderr, r.stderr
    else:
        assert r.returncode == 143, (r.returncode, r.stderr)
        assert r.stderr.count("REVIEW-FIXTURE") == 1, r.stderr
        assert "REVIEW-FIXTURE" not in r.stdout, r.stdout


# ─── D. the REDUCER'S REMINDER, fed the producer's real bytes ───────────────
#
# On the reducer path iteration-close-reminder.py injects a system-reminder that
# outranks this stdout and says "VERY NEXT tool call MUST be ...". It prefixes
# that reminder when the review printed. Fed here the bytes the heredoc and the
# region ACTUALLY produce (rb-11490: audit a two-harness contract from what the
# consumer reads, with the producer's real bytes), never a restated headline.

sys.path.insert(0, str(SCRIPT_DIR))
from test_iteration_close_reminder import (  # noqa: E402
    AGENT, COMMAND_RECURRING, SID, _additional_context, _build_fake_project,
    _invoke_hook, _make_payload)


@pytest.mark.parametrize("deadman_disabled", [False, True])
def test_the_reducer_reminder_leads_with_the_real_review(tmp_path, deadman_disabled):
    world = _make_world(tmp_path, interval_hours=4.0, consecutive_deep=2)
    r, review = _run_heredoc(tmp_path, world, outcome="deep")
    assert r.returncode == 0 and "INTERVAL MOVED" in review, (r.stderr, review)
    proj = _build_fake_project(tmp_path / "hook", AGENT, SID)
    if deadman_disabled:
        (proj / "agents" / AGENT / "session" / "deadman-disabled").write_text("")
    ctx = {}
    for label, rev in (("plain", ""), ("review", review)):
        out = _run_region(tmp_path / label, review=rev, deadman_disabled=deadman_disabled)
        payload = _make_payload(
            COMMAND_RECURRING, {"stdout": out, "stderr": "", "interrupted": False})
        rc, stdout, stderr = _invoke_hook(payload, proj)
        assert rc == 0, stderr
        ctx[label] = _additional_context(stdout)
    plain, reviewed = ctx["plain"], ctx["review"]
    assert plain and "INTERVAL MOVED" not in plain, plain  # the reminder did fire
    assert reviewed.startswith("<system-reminder>\nFIRST settle the INTERVAL MOVED review"), reviewed
    prefix = reviewed[len("<system-reminder>\n"):reviewed.index("THEN:\n") + len("THEN:\n")]
    assert reviewed.replace(prefix, "", 1) == plain, (reviewed, plain)
