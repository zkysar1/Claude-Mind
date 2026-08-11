"""Branch proof for the worker reducer-liveness poll ( mechanism 2).

Drives `decide()` directly — it is pure, so every branch is reachable without a
daemon, a second box, or a forked Body. The fail-safe invariant under test is
the one the design turns on: a worker NEVER continues on an unestablished
reducer, and NEVER promotes itself.

guard-1165: no module-level os.environ mutation and no sys.modules stubs.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from worker_reducer_liveness import (  # noqa: E402
    DEFAULT_ERROR_THRESHOLD,
    LIVE_MARKER,
    VERDICT_CONTINUE,
    VERDICT_WIND_DOWN,
    _parse_machine,
    decide,
    poll,
)


# ── rc=0 LIVE ────────────────────────────────────────────────────────────────

def test_live_same_machine_continues():
    r = decide(0, "cc-02", "cc-02", 0)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["consecutive_errors"] == 0


def test_first_live_poll_learns_the_machine():
    """No expected machine yet -> adopt the observed one, do not wind down."""
    r = decide(0, "cc-02", None, 0)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["expected_machine"] == "cc-02"


def test_live_on_a_different_machine_is_a_takeover():
    r = decide(0, "cc-05", "cc-02", 0)
    assert r["verdict"] == VERDICT_WIND_DOWN
    assert "takeover" in r["reason"]


def test_live_resets_the_transient_counter():
    """A run of blips that ends in a LIVE poll must not carry the count."""
    r = decide(0, "cc-02", "cc-02", DEFAULT_ERROR_THRESHOLD - 1)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["consecutive_errors"] == 0


def test_unknown_observed_machine_does_not_fabricate_a_takeover():
    """An unparseable summary line must not read as 'the reducer moved'.

    Still CONTINUE on the first occurrence and still not a takeover — but since
    F5 (g-306-131) it is no longer read as LIVE either. `consecutive_errors` is
    what separates the two readings, and it is the assertion this test was
    missing: before F5 this returned 0 (a reset, i.e. an assertion of life),
    so the two behaviours were indistinguishable through the other three keys.
    """
    r = decide(0, None, "cc-02", 0)
    assert r["verdict"] == VERDICT_CONTINUE
    assert "takeover" not in r["reason"]
    assert r["expected_machine"] == "cc-02"
    assert r["consecutive_errors"] == 1


# ── rc=4 NOT LIVE ────────────────────────────────────────────────────────────

def test_rc4_winds_down_immediately():
    """ABSENT / NOT-RUNNING / STALE / REFUSE all arrive as 4 and are decisive."""
    r = decide(4, None, "cc-02", 0)
    assert r["verdict"] == VERDICT_WIND_DOWN


def test_rc4_is_decisive_even_from_a_clean_counter():
    assert decide(4, None, None, 0)["verdict"] == VERDICT_WIND_DOWN


# ── transient rcs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rc", [1, 2, 3])
def test_single_transient_failure_does_not_wind_down(rc):
    """A daemon blip must not kill every worker in the fleet at once."""
    r = decide(rc, None, "cc-02", 0)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["consecutive_errors"] == 1


@pytest.mark.parametrize("rc", [1, 2, 3])
def test_transients_accumulate_to_the_threshold(rc):
    r = decide(rc, None, "cc-02", DEFAULT_ERROR_THRESHOLD - 1)
    assert r["verdict"] == VERDICT_WIND_DOWN
    assert r["consecutive_errors"] == DEFAULT_ERROR_THRESHOLD


def test_threshold_is_configurable():
    assert decide(1, None, "cc-02", 0, error_threshold=1)["verdict"] == VERDICT_WIND_DOWN


def test_transient_preserves_the_expected_machine():
    """A blip must not erase the learned reducer identity."""
    assert decide(1, None, "cc-02", 0)["expected_machine"] == "cc-02"


# ── the never-promote invariant ──────────────────────────────────────────────

def test_no_rc_ever_yields_a_promote_verdict():
    for rc in range(-1, 12):
        v = decide(rc, "cc-02", "cc-02", 0)["verdict"]
        assert v in (VERDICT_CONTINUE, VERDICT_WIND_DOWN), rc


def test_unrecognised_rc_resolves_to_wind_down():
    """Fail-safe: an unknown code from a refuse-rather-than-affirm script
    must never be read as permission to keep claiming work."""
    for rc in (7, 42, -1):
        assert decide(rc, None, "cc-02", 0)["verdict"] == VERDICT_WIND_DOWN


# ── summary-line parsing ─────────────────────────────────────────────────────

def test_parse_machine_from_the_real_live_line():
    line = ("[runner-claim] status: LIVE (backend=own-cloud) — 'zeta' is "
            "RUNNING on 'cc-02', heartbeat 272s old (threshold 3900s)")
    assert _parse_machine(line) == "cc-02"


def test_parse_machine_returns_none_on_a_non_live_line():
    line = ("[runner-claim] status: STALE (backend=own-cloud) — 'zeta' claim on "
            "'cc-02' is RUNNING but its heartbeat is 9000s old")
    assert _parse_machine(line) is None


def _emitter_branch(label):
    """The print statement for ONE status branch of runner-claim.sh: from its
    label to the sys.exit that terminates it.

    A fixed-width window is wrong here and quietly gives the wrong answer — the
    LIVE and STALE branches are adjacent in the source, so a 400-char slice
    taken from STALE spills into the LIVE statement below it and reports the
    marker as present in a branch that never prints it. Found by this test
    failing on its first run.
    """
    text = (SCRIPTS / "runner-claim.sh").read_text(encoding="utf-8")
    i = text.find(label)
    assert i >= 0, f"runner-claim.sh no longer has a {label!r} branch at all"
    j = text.find("sys.exit(", i)
    return text[i:j] if j > 0 else text[i:i + 400]


def test_the_live_marker_is_still_what_the_emitter_actually_prints():
    """F2 () — the contract test that did not exist.

    Takeover detection keys on a PROSE line from runner-claim.sh, and nothing
    joined the parser to the emitter. Both tests above pin HAND-COPIES, so a
    reformat upstream leaves them green while `_parse_machine` silently returns
    None and the takeover branch goes dead — guard-920: a test must replicate
    the literal production shape, not a believed one. This asserts against the
    emitter's SOURCE, and imports the marker from the module under test, so
    neither end can drift without failing here.

    It carries more weight than it looks: the claim payload exposes no
    runner_token (measured), so `machine_id` parsed out of this line is the ONLY
    takeover signal that exists.
    """
    live_stmt = _emitter_branch("status: LIVE")
    assert LIVE_MARKER in live_stmt, (
        f"runner-claim.sh's LIVE branch no longer prints {LIVE_MARKER!r}, so "
        "worker_reducer_liveness._parse_machine can no longer read the reducer's "
        "machine and takeover detection is dead. Re-derive the parse against the "
        "emitter's new format — do NOT just update this assertion.\n"
        f"LIVE statement now reads:\n{live_stmt[:200]}"
    )


def test_the_stale_branch_still_does_not_look_live():
    """The other half of the emitter contract: STALE prints 'is RUNNING but',
    which must keep NOT matching. If a reformat ever made it match, a stale
    (rc=4) claim would start yielding a parsed machine — and the two branches
    would become indistinguishable to this module."""
    assert LIVE_MARKER not in _emitter_branch("status: STALE")


# ── CLI seam ─────────────────────────────────────────────────────────────────

def _cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "worker_reducer_liveness.py"), "decide-only", *args],
        capture_output=True, text=True,
    )


def test_cli_exit_code_carries_the_verdict():
    """The worker loop branches on rc, so rc must track the verdict."""
    live = _cli("0", "cc-02", "cc-02", "0")
    assert live.returncode == 0
    assert json.loads(live.stdout)["verdict"] == VERDICT_CONTINUE

    dead = _cli("4", "", "cc-02", "0")
    assert dead.returncode == 1
    assert json.loads(dead.stdout)["verdict"] == VERDICT_WIND_DOWN


# ── F5: rc=0 WITHOUT the LIVE marker () ─────────────────────────────
#
# `runner-claim.sh status` exits 0 for exactly ONE designed reason — the LIVE
# branch — and that branch always prints "is RUNNING on '<machine>'"; every
# other status branch, including the no-python-launcher path, exits 4 (refuse
# rather than affirm). A zero carrying no marker therefore cannot come from the
# contract at all: it is the wrapper dying in a way that still yields 0. Before
# F5 it landed on the LIVE branch and returned continue / "reducer LIVE on
# unknown-machine" — a crash reading as life, inverting the never-promote
# invariant this module exists to enforce.

def test_rc0_without_the_live_marker_accumulates_instead_of_asserting_life():
    r = decide(0, None, None, 0)
    assert r["verdict"] == VERDICT_CONTINUE      # one crashed poll is not decisive
    assert r["consecutive_errors"] == 1          # ...but it is NOT a reset
    assert "LIVE marker" in r["reason"]


def test_rc0_without_the_live_marker_winds_down_at_the_threshold():
    """The branch that did not exist before F5: the unbounded continue is gone."""
    r = decide(0, None, "cc-02", DEFAULT_ERROR_THRESHOLD - 1)
    assert r["verdict"] == VERDICT_WIND_DOWN
    assert r["consecutive_errors"] == DEFAULT_ERROR_THRESHOLD
    assert r["expected_machine"] == "cc-02"
    # Wind-down is right, but nothing was OBSERVED, so nothing moved.
    assert "takeover" not in r["reason"]


def test_rc0_WITH_the_live_marker_is_still_live_and_still_resets():
    """The other half of the two-way proof (guard-1220): F5 must NARROW the
    LIVE branch, not break it. A marker-bearing zero still resets a pending
    escalation — otherwise the fix would wind down healthy workers, which is
    the disease being worse than the cure (guard-1562)."""
    r = decide(0, "cc-02", "cc-02", DEFAULT_ERROR_THRESHOLD - 1)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["consecutive_errors"] == 0
    assert "LIVE" in r["reason"]


def test_rc0_without_marker_respects_a_configured_threshold():
    assert decide(0, None, "cc-02", 0, error_threshold=1)["verdict"] == VERDICT_WIND_DOWN


# ── poll(): F1 + F3 () ──────────────────────────────────────────────

LIVE_LINE = ("[runner-claim] status: LIVE (backend=own-cloud) — 'alpha' is "
             "RUNNING on 'cc-04', heartbeat 10s old (threshold 3900s)")

FAKE_LIVE = "echo \"%s\"\nexit 0\n" % LIVE_LINE
FAKE_TRANSIENT = "echo 'daemon unreachable' >&2\nexit 1\n"


def _claim_stub(tmp_path, body):
    """A stand-in runner-claim.sh, so poll() never touches the real daemon.

    bash_cmd() invokes it as `bash <path> status --agent <agent>`, so no chmod
    is needed and the stub can ignore its arguments.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "runner-claim.sh").write_text(body, encoding="utf-8")
    return scripts


def _wedge_state_dir(tmp_path):
    """Make the state write fail: `sessions` becomes a FILE, so poll()'s
    mkdir(parents=True) of sessions/<sid>/ raises."""
    agent_dir = tmp_path / "alpha"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "sessions").write_text("not a directory", encoding="utf-8")
    return agent_dir


def test_poll_persists_the_counter_so_it_can_accumulate(tmp_path):
    """The mechanism F1 protects. Each real poll is a SEPARATE PROCESS, so the
    threshold can only ever accumulate THROUGH the state file — three calls,
    three fresh decide() invocations, escalating only because the file carried
    the count between them."""
    scripts = _claim_stub(tmp_path, FAKE_TRANSIENT)
    agent_dir = tmp_path / "alpha"

    first = poll("alpha", agent_dir, "SID1", scripts)
    assert first["verdict"] == VERDICT_CONTINUE and first["consecutive_errors"] == 1
    assert "state_write_error" not in first

    second = poll("alpha", agent_dir, "SID1", scripts)
    assert second["verdict"] == VERDICT_CONTINUE and second["consecutive_errors"] == 2

    third = poll("alpha", agent_dir, "SID1", scripts)
    assert third["verdict"] == VERDICT_WIND_DOWN
    assert third["consecutive_errors"] == DEFAULT_ERROR_THRESHOLD


def test_poll_winds_down_when_a_pending_escalation_cannot_be_persisted(tmp_path):
    """F1. The old comment here read 'never let a state-write failure decide the
    loop' and was exactly backwards: a swallowed write error does not merely
    lose telemetry, it freezes the counter and DISARMS the only mechanism that
    ever winds a worker down on transient faults — continue, forever."""
    scripts = _claim_stub(tmp_path, FAKE_TRANSIENT)
    agent_dir = _wedge_state_dir(tmp_path)

    r = poll("alpha", agent_dir, "SID1", scripts)

    assert r["rc"] == 1
    assert "state_write_error" in r            # the write really did fail
    assert r["verdict"] == VERDICT_WIND_DOWN   # ...and that is now decisive
    assert "disarmed fail-safe" in r["reason"]


def test_poll_does_NOT_wind_down_a_healthy_loop_when_the_write_fails(tmp_path):
    """The other half of F1, and the one that keeps the fix from being a new
    defect (guard-1562). On a verdict that already reset the counter, the
    unwritten file costs nothing — stopping a healthy loop on a plumbing fault
    is worse than the disease. Same wedged state dir as the test above, so the
    ONLY difference driving the opposite verdict is the poll's own outcome."""
    scripts = _claim_stub(tmp_path, FAKE_LIVE)
    agent_dir = _wedge_state_dir(tmp_path)

    r = poll("alpha", agent_dir, "SID1", scripts)

    assert r["rc"] == 0
    assert "state_write_error" in r            # the write failed here too
    assert r["verdict"] == VERDICT_CONTINUE    # ...and was correctly ignored
    assert r["consecutive_errors"] == 0


def test_poll_is_callable_as_a_library_without_mains_path_insert(tmp_path):
    """F3: the `_runtime_bash` import used to rely on a sys.path.insert that
    ONLY main() performed, so any direct library call raised ImportError before
    reaching the first line of real work.

    This REQUIRES a fresh interpreter. Line 19 of this very file already puts
    core/scripts on sys.path, so an in-process call would pass with the fix
    reverted — a vacuous test (guard-1220). The child therefore loads the module
    by file location (which does not touch sys.path), scrubs PYTHONPATH, and
    ASSERTS ITS OWN SEAM before proving anything: if `_runtime_bash` is already
    importable there, the test fails loudly as a broken precondition rather than
    passing for the wrong reason.
    """
    scripts = _claim_stub(tmp_path, FAKE_LIVE)
    agent_dir = tmp_path / "alpha"

    child = "\n".join([
        "import importlib.util, json, os, sys",
        "try:",
        "    import _runtime_bash",
        "    sys.exit(3)",          # precondition broken: the seam is not a seam
        "except ImportError:",
        "    pass",
        "spec = importlib.util.spec_from_file_location('wrl_probe', os.environ['WRL_MODULE'])",
        "m = importlib.util.module_from_spec(spec)",
        "spec.loader.exec_module(m)",
        "print(json.dumps(m.poll('alpha', os.environ['WRL_AGENT_DIR'], 'SID1',",
        "                        os.environ['WRL_SCRIPTS'])))",
    ])

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({
        "WRL_MODULE": str(SCRIPTS / "worker_reducer_liveness.py"),
        "WRL_AGENT_DIR": str(agent_dir),
        "WRL_SCRIPTS": str(scripts),
    })
    proc = subprocess.run([sys.executable, "-c", child],
                          capture_output=True, text=True, env=env)

    assert proc.returncode != 3, "precondition broken: _runtime_bash was already importable"
    assert proc.returncode == 0, proc.stderr
    r = json.loads(proc.stdout)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["expected_machine"] == "cc-04"
