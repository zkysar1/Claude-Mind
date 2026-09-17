"""Branch proof for the stop-complete (quiesce) predicate ( outcome 3).

Drives `decide` / `decide_agent` / `live_claude_pids` directly — they are pure,
so every branch is reachable without a fleet, a daemon, or a scheduled window.

Two invariants are under test and only one of them is obvious:

  * the DECLARED case — `verify` must refuse while a stop turn is still writing,
    which on the file signals is a STALE `agent-mode` beside a fresh
    `agent-state` (the D1-written / D7-unwritten window);
  * the case the prose predicate does NOT survive — a WORKER box. Its
    `agents/*/session/` is gitignored and therefore box-local, so `agent-state`
    reads IDLE and `agent-mode` reads newer-than-state on a box that is
    mid-unit. Measured on cc-09 2026-09-17 during this very goal. With the
    reducer stopped (claim rc=4) both file signals and the claim signal say
    "quiesced" and ONLY the live-process signal refuses.

Plus the fail-closed direction: every unreadable or unrecognised signal must
return UNKNOWN (rc=2), never QUIESCED — and a non-vacuity proof that the
all-positive fixture really is the one the quiesced branch needs, so an
always-refuse implementation cannot pass this file.

guard-1165: no module-level os.environ mutation and no sys.modules stubs.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from stop_complete import (  # noqa: E402
    QUIESCED,
    UNKNOWN,
    WRITING,
    decide,
    decide_agent,
    live_claude_pids,
)

# A fully-positive row: IDLE, agent-mode written AFTER agent-state, no live
# claim, no foreign claude process. The ONE shape that authorizes the window.
QUIET = {
    "agent": "alpha",
    "agent_state": "IDLE",
    "state_mtime": 1000.0,
    "mode_mtime": 2000.0,
    "claim_rc": 4,
    "claude_pids": [],
}


def row(**over):
    r = dict(QUIET)
    r.update(over)
    return r


def test_the_all_positive_row_is_quiesced_and_rc_zero():
    """Non-vacuity: without this the refuse-everything implementation passes."""
    assert decide_agent(QUIET)["verdict"] == QUIESCED
    out = decide([QUIET])
    assert out["verdict"] == QUIESCED
    assert out["rc"] == 0


# ---------------------------------------------------------------------------
# The declared case: a stop caught between D1 and D7.
# ---------------------------------------------------------------------------

def test_agent_mode_older_than_agent_state_is_a_stop_in_flight():
    v = decide_agent(row(state_mtime=2000.0, mode_mtime=1000.0))
    assert v["verdict"] == WRITING
    assert "STALE" in v["reason"]


def test_agent_mode_absent_beside_a_state_file_is_a_stop_that_never_reached_d7():
    v = decide_agent(row(mode_mtime=None))
    assert v["verdict"] == WRITING
    assert "D7" in v["reason"]


def test_equal_mtimes_are_quiesced_not_a_stop_in_flight():
    # D1 and D7 inside one filesystem-timestamp granule is a COMPLETED stop.
    assert decide_agent(row(state_mtime=2000.0, mode_mtime=2000.0))["verdict"] == QUIESCED


# ---------------------------------------------------------------------------
# The worker-box trap — the reason the process signal is load-bearing.
# ---------------------------------------------------------------------------

def test_worker_box_reads_quiesced_on_every_file_signal_while_it_is_writing():
    """All three non-process signals are positive; the live process refuses."""
    file_and_claim_only = row(claude_pids=[])
    assert decide_agent(file_and_claim_only)["verdict"] == QUIESCED, (
        "fixture guard: the worker box's file+claim signals really do read clean"
    )
    v = decide_agent(row(claude_pids=[7855]))
    assert v["verdict"] == WRITING
    assert "7855" in v["reason"]


def test_a_live_claim_is_decisive_even_when_the_box_is_quiet():
    v = decide_agent(row(claim_rc=0))
    assert v["verdict"] == WRITING
    assert "LIVE" in v["reason"]


def test_agent_state_running_is_decisive():
    assert decide_agent(row(agent_state="RUNNING"))["verdict"] == WRITING


# ---------------------------------------------------------------------------
# Fail-closed: rc=4 is necessary, never sufficient; nothing else is evidence.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rc", [1, 2, 3, 124, 127])
def test_a_claim_rc_that_is_neither_live_nor_absent_is_unknown_never_quiesced(rc):
    v = decide_agent(row(claim_rc=rc))
    assert v["verdict"] == UNKNOWN
    assert decide([row(claim_rc=rc)])["rc"] == 2


@pytest.mark.parametrize("field", ["claim_rc", "claude_pids"])
def test_an_unreadable_signal_is_unknown_never_quiesced(field):
    assert decide_agent(row(**{field: None}))["verdict"] == UNKNOWN


def test_an_unreadable_agent_state_is_unknown():
    assert decide_agent(row(agent_state=None, state_mtime=None))["verdict"] == UNKNOWN


def test_an_unrecognised_agent_state_is_unknown_not_quiesced():
    assert decide_agent(row(agent_state="UNINITIALIZED"))["verdict"] == UNKNOWN


def test_no_agents_measured_is_unknown_not_quiesced():
    out = decide([])
    assert out["verdict"] == UNKNOWN
    assert out["rc"] == 2


# ---------------------------------------------------------------------------
# Box-level composition.
# ---------------------------------------------------------------------------

def test_one_writing_agent_refuses_the_whole_box():
    out = decide([QUIET, row(agent="bravo", claim_rc=0)])
    assert out["verdict"] == WRITING
    assert out["rc"] == 1
    assert "bravo" in out["reason"]


def test_writing_outranks_unknown_so_the_operator_hears_the_decisive_one():
    out = decide([row(agent="a", claim_rc=2), row(agent="b", agent_state="RUNNING")])
    assert out["verdict"] == WRITING
    assert "b" in out["reason"] and "a" not in out["reason"].split(":")[0]


# ---------------------------------------------------------------------------
# live_claude_pids — self-exclusion by ancestry.
# ---------------------------------------------------------------------------

# Shape measured on cc-09 2026-09-17: the CLI (7855) -> bash (552938, the
# checker) -> bash -> the CLI's bundled search binary, which keeps comm
# `claude.exe` while carrying someone else's argv. A pid-equality self-exclusion
# counts its own tree; an args match (`pgrep -f claude`) counts its own grep.
SELF_TREE = [
    (7854, 1, "node"),
    (7855, 7854, "claude"),
    (552938, 7855, "bash"),
    (552941, 552938, "bash"),
    (552944, 552941, "claude.exe"),
]


def test_the_checkers_own_claude_tree_is_excluded_root_and_descendants():
    assert live_claude_pids(SELF_TREE, self_pid=552938) == []


def test_a_foreign_claude_is_counted_while_self_is_excluded():
    table = SELF_TREE + [(9001, 1, "claude")]
    assert live_claude_pids(table, self_pid=552938) == [9001]


def test_run_from_outside_any_claude_process_nothing_is_excluded():
    """The driver's own shape: `lxc exec`/`ssh` has no claude ancestry."""
    assert live_claude_pids(SELF_TREE, self_pid=1) == [7855, 552944]


def test_a_pid_whose_parent_is_gone_does_not_hang_the_ancestry_walk():
    table = [(4242, 999999, "claude"), (4243, 4242, "bash")]
    assert live_claude_pids(table, self_pid=4243) == []
    assert live_claude_pids(table, self_pid=7) == [4242]


def test_a_parent_cycle_terminates():
    table = [(10, 11, "bash"), (11, 10, "bash"), (12, 1, "claude")]
    assert live_claude_pids(table, self_pid=10) == [12]


# ---------------------------------------------------------------------------
# The WRAPPER, end to end. The pure module above cannot catch a broken TSV
# round-trip, a heredoc quoting fault, a bad sys.path, or an rc that never
# reaches the caller — and `run-scoped-suite.sh --since` reported
# stop-complete-check.sh as UNMAPPED (referenced by no test), which is the gap
# these two close. guard-580: resolve the interpreter, never a bare "bash".
# ---------------------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402

from _runtime_bash import BASH  # noqa: E402

WRAPPER = SCRIPTS / "stop-complete-check.sh"


def _run(*args):
    return subprocess.run(
        [BASH, WRAPPER.as_posix(), *args],
        cwd=str(SCRIPTS.parents[1]), capture_output=True, text=True, timeout=300,
    )


def test_wrapper_never_authorizes_the_window_for_an_agent_it_cannot_read():
    """The fail-closed property, and the only one stable on every box.

    An agent with no session files here has an unreadable `agent-state`, so the
    verdict must not be QUIESCED and rc must not be 0 — whatever this box's
    claim daemon and process table happen to say.
    """
    p = _run("--agent", "no-such-agent-for-tests")
    assert p.returncode != 0, p.stdout + p.stderr
    assert "stop-complete:" in p.stdout
    assert "QUIESCED" not in p.stdout.split("\n")[0]
    assert "-> rc=" in p.stdout


def test_wrapper_json_mode_emits_the_decide_payload_and_rc_agrees_with_it():
    p = _run("--agent", "no-such-agent-for-tests", "--json")
    payload = json.loads(p.stdout)
    assert payload["verdict"] in (QUIESCED, WRITING, UNKNOWN)
    assert payload["rc"] == p.returncode, "the wrapper must pass decide()'s rc through"
    assert [a["agent"] for a in payload["agents"]] == ["no-such-agent-for-tests"]
