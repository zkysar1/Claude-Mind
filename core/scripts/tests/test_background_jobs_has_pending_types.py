"""has-pending `--types` allowlist (stop-hook Gate 2.6, 2026-09-25).

Gate 2.6 ALLOWs a turn-end when this body owns a live, monitored background
job. An ALLOW exits the hook with NO payload, so it is only safe for a job
whose exit will RE-INVOKE the model -- the registered external-wait sleep,
launched run_in_background from the model's own turn. Measured 2026-09-25 on a
reducer: a detached `processor` row (live PID, monitor goal, own sid) satisfied
the gate at every turn-end for ~8h; nothing ever woke the loop.

The fix is an OPT-IN type allowlist with the same three-way shape as
--body-sid (g-306-135):

  absent  -> every type counts (recovery-gate's agent-wide Cond 4 unchanged)
  ""      -> nothing counts: exit 1, the BLOCK proceeds (rb-605 direction)
  "a,b"   -> a job counts only when its `type` is one of them

A job with a missing or unlisted type never matches. The error direction is
therefore always BLOCK-proceeds, never a silent ALLOW.
"""
import argparse
import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "bgjobs_types", str(SCRIPTS / "background-jobs.py")
)
bgjobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bgjobs)

SID = "sid-runner-1111"
OTHER = "sid-worker-2222"


@pytest.fixture
def jobs_path(tmp_path, monkeypatch):
    p = tmp_path / "session" / "background-jobs.yaml"
    monkeypatch.setattr(bgjobs, "JOBS_PATH", p)
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: True)
    return p


def _seed(jobs):
    """Write rows straight to the store -- the shape the hook reads."""
    bgjobs.write_data({"jobs": jobs})


def _row(jtype, owner=SID, job_id=None, **extra):
    row = {"job_id": job_id or f"job-{jtype or 'untyped'}", "pid": 4242,
           "owner_sid": owner, "monitor_goal_id": "g-mon",
           "completion_check": "", "started_at": "2026-09-25T00:00:00"}
    if jtype is not None:
        row["type"] = jtype
    row.update(extra)
    return row


def _pending(**kw):
    """True when the gate would ALLOW the turn-end (exit 0)."""
    with pytest.raises(SystemExit) as exc:
        bgjobs.cmd_has_pending(argparse.Namespace(**kw))
    return exc.value.code == 0


# ------------------------------------------------------------ the three shapes

def test_no_types_flag_counts_every_type(jobs_path):
    """Byte-identical legacy behaviour: recovery-gate passes no --types."""
    _seed([_row("processor")])
    assert _pending() is True
    assert _pending(body_sid=SID) is True


def test_allowlisted_type_counts(jobs_path):
    _seed([_row("external-wait-sleep")])
    assert _pending(body_sid=SID, types="external-wait-sleep") is True


def test_unlisted_type_does_not_count(jobs_path):
    """The 2026-09-25 row: live, monitored, own sid, wrong type -> BLOCK."""
    _seed([_row("processor")])
    assert _pending(body_sid=SID, types="external-wait-sleep") is False


def test_missing_type_never_matches(jobs_path):
    _seed([_row(None)])
    assert _pending(body_sid=SID, types="external-wait-sleep") is False
    # ...but still counts when no allowlist is asked for.
    assert _pending(body_sid=SID) is True


def test_empty_types_value_means_nothing_counts(jobs_path):
    """A caller that asked to filter and named nothing gets exit 1."""
    _seed([_row("external-wait-sleep")])
    assert _pending(body_sid=SID, types="") is False
    assert _pending(body_sid=SID, types=" , ") is False


def test_csv_is_tolerant_of_spaces_and_extra_entries(jobs_path):
    _seed([_row("external-wait-sleep")])
    assert _pending(body_sid=SID, types=" session-run , external-wait-sleep ") is True


def test_type_filter_composes_with_body_filter(jobs_path):
    """Right type, wrong owner still does not count; both must hold."""
    _seed([_row("external-wait-sleep", owner=OTHER)])
    assert _pending(body_sid=SID, types="external-wait-sleep") is False
    _seed([_row("external-wait-sleep", owner=OTHER),
           _row("external-wait-sleep", owner=SID, job_id="mine")])
    assert _pending(body_sid=SID, types="external-wait-sleep") is True


def test_dead_pid_still_does_not_count_even_when_type_matches(jobs_path, monkeypatch):
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: False)
    _seed([_row("external-wait-sleep")])
    assert _pending(body_sid=SID, types="external-wait-sleep") is False


# ------------------------------------------------------------ call-site pins

def _code_lines(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_stop_hook_gate_2_6_passes_the_wait_allowlist():
    """The hook is the ONLY caller that may pass --types, and it must."""
    src = _code_lines(SCRIPTS / "stop-hook.sh")
    hits = [ln for ln in src if "background-jobs.sh" in ln and "has-pending" in ln]
    assert len(hits) == 1, f"expected one Gate 2.6 call site, found {hits}"
    assert "--types external-wait-sleep" in hits[0], hits[0]
    assert '--body-sid "$HOOK_SID"' in hits[0], hits[0]


def test_recovery_gate_stays_agent_wide_and_untyped():
    """Cond 4 asks 'is this mind busy?' -- a processor row must still answer."""
    src = _code_lines(SCRIPTS / "recovery-gate.sh")
    hits = [ln for ln in src if "background-jobs.sh" in ln and "has-pending" in ln]
    assert hits, "recovery-gate.sh lost its has-pending call"
    for ln in hits:
        assert "--types" not in ln, ln
        assert "--body-sid" not in ln, ln


def test_parser_exposes_types_with_none_default():
    parser = bgjobs.build_parser()
    ns = parser.parse_args(["has-pending"])
    assert ns.types is None
    ns = parser.parse_args(["has-pending", "--types", "external-wait-sleep"])
    assert ns.types == "external-wait-sleep"
    # The registered wait's type string is spelled the same at its register
    # site, so the allowlist and the producer cannot drift apart silently.
    sleeper = (SCRIPTS / "interruptible-sleep.sh").read_text(encoding="utf-8")
    assert re.search(r'_QS_JOB_TYPE="external-wait-sleep"', sleeper), (
        "interruptible-sleep.sh no longer registers type external-wait-sleep")
