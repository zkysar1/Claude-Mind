""": iteration-close --phase verify refuses a candidate's close before its gates run.

The intake tier's transition table (world/conventions/goal-intake-management.md section 2)
has a candidate pass through pending first, and the daemon refuses candidate -> completed
with candidate_transition_forbidden. Until this goal that refusal came only at the status
write, after every gate in do_verify had run. Measured 2026-10-02 on cc-14: a self-filed
candidate's close spent 16 minutes in the domain-suite gate and was then refused, and the
RECOVERY block's Retry line repeated the command that had just been refused.

HOW THESE TESTS RUN THE REAL CODE. As in test_iteration_close_forward_precondition.py, the
behavioural tests extract the real functions from iteration-close.sh at test time and source
them. The check reads the live status through aspirations-query.sh, so SCRIPT_DIR points at
a stand-in that prints a fixed reply in the query's default projection and records how it was
called; the recovery printer's _probe_goal_status is stubbed. No store is read and nothing is
written. The last test pins the wiring the extraction cannot see: do_verify calls the check
before its first gate and before the status write.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import BASH  # noqa: E402
from test_iteration_close_forward_precondition import _extract  # noqa: E402
from test_post_status_stamps_are_non_fatal import STATUS_WRITE, _do_verify_body  # noqa: E402

CHECK = "_refuse_candidate_close"
RECOVERY = "_print_recovery_instructions"
QUERY_ARGV = "--goal-field id g-999-1"


def _row(status, source="world", goal_id="g-999-1", **extra):
    """One row in the query's default projection (its six keys), plus any extra keys."""
    row = {"goal_id": goal_id, "asp_id": "asp-999", "source": source, "title": "t",
           "status": status, "category": "framework"}
    row.update(extra)
    return row


def _fake_query(tmp_path: Path, reply: str, rc: int) -> Path:
    """A stand-in aspirations-query.sh that records its argv, prints `reply`, exits `rc`."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "aspirations-query.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" > "{(tmp_path / "argv").as_posix()}"\n'
        "cat <<'REPLY'\n"
        f"{reply}\n"
        "REPLY\n"
        f"exit {rc}\n",
        encoding="utf-8",
    )
    return scripts


def _run(tmp_path: Path, reply, goal_status: str = "completed", rc: int = 0):
    """Source the real check and the real recovery printer, with the query faked.

    `reply` is the stand-in query's stdout: a list of rows, sent as JSON, or a raw string.
    Prints REFUSE_RC=<rc> on stdout; on a refusal, prints what the EXIT trap would print for
    it (rc 2 in the verify phase) on stderr, as the real script does. The trap's own probe
    reads "candidate", which is what it would read after a refusal from this check.
    """
    text = reply if isinstance(reply, str) else json.dumps(reply)
    scripts = _fake_query(tmp_path, text, rc)
    harness = f"""
set -uo pipefail
SCRIPT_DIR="{scripts.as_posix()}"
GOAL_ID="g-999-1"; GOAL_STATUS="{goal_status}"; SOURCE="world"; OUTCOME="deep"
SUMMARY=""; OVERRIDE_UNCOMMITTED=""; OVERRIDE_MISSING_ARTIFACT=""; OVERRIDE_DOMAIN_SUITE=""
OVERRIDE_CLOSURE_EVIDENCE=""; OUTCOME_NOTE_FILE=""; _CURRENT_PHASE="verify"
_probe_goal_status() {{ printf '%s' "candidate"; }}
{_extract(CHECK)}
{_extract(RECOVERY)}
rc=0; {CHECK} || rc=$?
echo "REFUSE_RC=$rc"
[[ $rc -ne 0 ]] && {RECOVERY} 2
exit 0
"""
    return subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60)


def _argv(tmp_path: Path):
    """What the stand-in query was called with, or None when it was never called."""
    f = tmp_path / "argv"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def _recovery_only(live: str, goal_status: str = "completed") -> str:
    """What the trap prints for a refusal from some LATER step (a gate or the write)."""
    harness = f"""
set -uo pipefail
GOAL_ID="g-999-1"; GOAL_STATUS="{goal_status}"; SOURCE="world"; OUTCOME="deep"
SUMMARY=""; OVERRIDE_UNCOMMITTED=""; OVERRIDE_MISSING_ARTIFACT=""; OVERRIDE_DOMAIN_SUITE=""
OVERRIDE_CLOSURE_EVIDENCE=""; OUTCOME_NOTE_FILE=""; _CURRENT_PHASE="verify"
_probe_goal_status() {{ printf '%s' "{live}"; }}
{_extract(RECOVERY)}
{RECOVERY} 1
"""
    p = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60)
    return p.stderr


def _line(stderr: str, label: str) -> str:
    return next((ln.strip() for ln in stderr.splitlines() if ln.strip().startswith(label)), "")


def test_a_candidate_close_is_refused_before_any_gate_with_the_promote_first(tmp_path):
    p = _run(tmp_path, [_row("candidate")])
    assert "REFUSE_RC=1" in p.stdout, p.stdout + p.stderr
    assert "g-999-1 is a candidate" in p.stderr and "no gate ran" in p.stderr, p.stderr
    # The status came from the single-goal query. A check that went back to
    # _probe_goal_status, a whole-aspiration read on every completed close, never calls it.
    assert _argv(tmp_path) == QUERY_ARGV, _argv(tmp_path)
    promote, retry = _line(p.stderr, "Promote first:"), _line(p.stderr, "Then retry:")
    assert promote.endswith("aspirations-update-goal.sh --source world g-999-1 status pending"), promote
    assert "--phase verify --goal g-999-1 --status completed" in retry, retry
    lines = [ln.strip() for ln in p.stderr.splitlines()]
    assert lines.index(promote) < lines.index(retry), "the promote must come before the retry"
    # The bare retry would be refused again, and the pending write is not a revert here.
    assert not _line(p.stderr, "Retry:") and "Revert (mark pending)" not in p.stderr, p.stderr


@pytest.mark.parametrize("live", ["pending", "in-progress"])
def test_a_goal_past_the_candidate_tier_goes_on_to_its_gates(tmp_path, live):
    """POSITIVE CONTROL: the check passes every goal that may be completed, silently, and
    the pass comes from a real read."""
    p = _run(tmp_path, [_row(live)])
    assert "REFUSE_RC=0" in p.stdout and p.stderr == "", p.stdout + p.stderr
    assert _argv(tmp_path) == QUERY_ARGV, _argv(tmp_path)


@pytest.mark.parametrize("reply,rc", [
    ("", 1),            # the query failed, e.g. the daemon is down
    ("", 0),            # a goal the live store no longer holds: measured as 0 bytes
    ("not json", 0),
    ("[]", 0),
    (json.dumps([{"goal_id": "g-999-1", "source": "world"}]), 0),   # a row with no status
])
def test_an_unreadable_status_fails_open_to_the_writes_own_refusal(tmp_path, reply, rc):
    p = _run(tmp_path, reply, rc=rc)
    assert "REFUSE_RC=0" in p.stdout and p.stderr == "", p.stdout + p.stderr


@pytest.mark.parametrize("target", ["skipped", "blocked"])
def test_only_a_completed_target_is_refused(tmp_path, target):
    """section 2 forbids candidate -> completed and -> in-progress, and verify's usage names
    only completed, blocked and skipped as targets. A skip or a block of a candidate is the
    daemon's to judge, and those closes do not pay for the read."""
    p = _run(tmp_path, [_row("candidate")], goal_status=target)
    assert "REFUSE_RC=0" in p.stdout and p.stderr == "", p.stdout + p.stderr
    assert _argv(tmp_path) is None, "a close this check cannot refuse must not read"


@pytest.mark.parametrize("rows", [
    [_row("candidate", source="agent")],                      # the other store's row
    [_row("candidate", read_from="peer-mirror-unverified")],  # a peer-mirror row
    [_row("candidate", goal_id="g-999-10")],                  # another goal's row
    [_row("candidate", source="agent"), _row("pending")],     # the caller's store says pending
])
def test_only_a_clean_row_from_the_callers_store_refuses(tmp_path, rows):
    p = _run(tmp_path, rows)
    assert "REFUSE_RC=0" in p.stdout and p.stderr == "", p.stdout + p.stderr


def test_the_callers_row_is_found_among_others(tmp_path):
    """POSITIVE CONTROL for the test above: the same filters still find a clean row."""
    p = _run(tmp_path, [_row("pending", source="agent"), _row("candidate")])
    assert "REFUSE_RC=1" in p.stdout, p.stdout + p.stderr


def test_a_later_refusal_on_a_candidate_also_names_the_promote():
    """The daemon's own refusal (the check could not read the status, or an older caller)
    reaches the same trap branch, so it gets the same remedy."""
    stderr = _recovery_only("candidate")
    assert "status=candidate" in stderr and _line(stderr, "Promote first:"), stderr
    assert not _line(stderr, "Retry:"), stderr


def test_the_recovery_of_a_pending_goal_is_unchanged():
    """POSITIVE CONTROL for the two absence checks above: off the candidate tier the trap
    still prints the plain retry and the conditional revert."""
    stderr = _recovery_only("pending")
    assert _line(stderr, "Retry:") and "Revert (mark pending)" in stderr, stderr
    assert not _line(stderr, "Promote first:"), stderr


def test_do_verify_calls_the_check_before_its_first_gate_and_the_status_write():
    body = _do_verify_body()
    call = body.find(f"{CHECK} || exit 2")
    assert call != -1, f"do_verify no longer calls {CHECK}"
    for later in ("pending-deploys-gate.sh", "domain-suite-gate.py", STATUS_WRITE):
        at = body.find(later)
        assert at != -1, f"do_verify no longer contains {later!r}; re-pin this test"
        assert call < at, f"{CHECK} must run before {later!r}, or the gates run for nothing"
