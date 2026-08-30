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
    TOKEN_FP_MARKER,
    TOKEN_FP_UNKNOWN,
    VERDICT_CONTINUE,
    VERDICT_WIND_DOWN,
    _parse_machine,
    _parse_token_fp,
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


# ── the token-fingerprint axis () ───────────────────────────────────
#
# `machine_id` structurally cannot see a SAME-BOX reducer restart: the claim row
# stays LIVE on the machine this Body expects while a NEW runner holds it, so
# every poll returned CONTINUE and the Body kept producing work nobody would
# merge. That was this module's documented MEASURED LIMIT.
#
# The closing signal is a NON-REVERSIBLE digest of runner_token, never the token
# itself. The raw value is the ConditionExpression bearer credential for
# `heartbeat` and `release_runner`, so publishing it to close a liveness gap
# would hand every reader the ability to forge a heartbeat (defeating
# reclaim_if_stale) or release a live claim — i.e. would defeat the mechanism
# this poll exists to protect. See owncloud_backend.runner_token_fingerprint.

FP_A = "1f4c0a9b2e6d8035"
FP_B = "9a3e7c15d0b84621"

LIVE_LINE_FP = LIVE_LINE + ", token-fp " + FP_A


def test_parse_token_fp_from_the_real_live_line():
    assert _parse_token_fp(LIVE_LINE_FP) == FP_A


def test_parse_token_fp_is_absent_on_a_pre_upgrade_live_line():
    """A daemon predating the field emits the old line verbatim. Absent, not
    empty-string and not a crash — the mixed-version fleet's whole upgrade path
    runs through this returning None."""
    assert _parse_token_fp(LIVE_LINE) is None


def test_parse_token_fp_maps_the_literal_unknown_to_none():
    """The emitter prints `unknown` when the daemon supplied no fingerprint. If
    that survived as a STRING it would compare EQUAL across two genuinely
    different reducers and read as 'no takeover' — the one direction this axis
    must never fail in."""
    assert _parse_token_fp(LIVE_LINE + ", token-fp " + TOKEN_FP_UNKNOWN) is None


def test_parse_token_fp_ignores_a_marker_on_a_LATER_line():
    """Scoping proof, and the case a naive whole-string find gets wrong.

    poll() feeds stdout+stderr CONCATENATED, so anything the wrapper logs can
    sit beside the summary line. A mis-scoped read is worse than no read: it
    would manufacture a spurious 'the fp changed' and wind down a healthy
    worker."""
    noisy = LIVE_LINE + "\n[runner-claim] debug: token-fp " + FP_B
    assert _parse_token_fp(noisy) is None


def test_parse_token_fp_ignores_a_marker_on_an_EARLIER_line():
    noisy = "[runner-claim] warn: cached token-fp " + FP_B + "\n" + LIVE_LINE_FP
    assert _parse_token_fp(noisy) == FP_A


# A TRUNCATED LIVE line: cut between the opening and closing quote of the
# machine id, with a later line supplying a stray quote. Shared by the parity
# test below so BOTH parsers are pinned to the SAME fixture — the point of the
# pin is that a future reader cannot harden one parser and leave its sibling
# behind, which is exactly what happened here (rb-1915, guard-1924).
TRUNCATED_LIVE = (
    "[runner-claim] status: LIVE (backend=own-cloud) — 'zeta' " + LIVE_MARKER
    + "'cc-0\n[warn] peer 'cc-99' busy"
)


def test_neither_parser_reads_across_a_truncated_live_line():
    """ fresh-eyes: `_parse_machine` was NOT line-scoped and this fixture
    made it return `'cc-0\\n[warn] peer '` — a machine id that differs from
    expected_machine, so decide() wound down a HEALTHY reducer. That is the
    fail-UNSAFE direction, and truncated output (ssh cut, bounded read, OOM kill
    mid-write) is likeliest exactly when a worker is polled during trouble.
    `_parse_token_fp` was already scoped and returned None on the same bytes."""
    # Positive control FIRST — without it a fixture that merely lacked the
    # marker would satisfy the assertions below for an uninteresting reason.
    intact = TRUNCATED_LIVE.split("\n", 1)[0] + "2', heartbeat 1s old"
    assert _parse_machine(intact) == "cc-02", "fixture is not a parseable LIVE line"

    assert _parse_machine(TRUNCATED_LIVE) is None
    assert _parse_token_fp(TRUNCATED_LIVE) is None

    # The consequence, not just the parse: an unreadable machine must be
    # non-discriminating, never a takeover.
    assert decide(0, _parse_machine(TRUNCATED_LIVE), "cc-02", 0)["verdict"] != (
        VERDICT_WIND_DOWN
    )


def test_same_machine_changed_fp_is_a_restart_takeover():
    """THE new capability. Machine is IDENTICAL in both operands — this verdict
    is unreachable by the machine axis at any threshold, which is exactly why
    the gap existed."""
    r = decide(0, "cc-02", "cc-02", 0, observed_token_fp=FP_B, expected_token_fp=FP_A)
    assert r["verdict"] == VERDICT_WIND_DOWN
    assert "restart" in r["reason"]
    assert FP_A in r["reason"] and FP_B in r["reason"]


def test_same_machine_same_fp_still_continues():
    """The other half of the two-way proof (guard-1220): the new branch must
    NARROW the LIVE path, not break it. Without this, a fix that wound down on
    every poll would pass the test above."""
    r = decide(0, "cc-02", "cc-02", 0, observed_token_fp=FP_A, expected_token_fp=FP_A)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["expected_token_fp"] == FP_A


def test_first_live_poll_learns_the_fp():
    r = decide(0, "cc-02", None, 0, observed_token_fp=FP_A)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["expected_token_fp"] == FP_A


def test_an_absent_observed_fp_is_non_discriminating_not_a_change():
    """FAIL-SAFE ASYMMETRY, and it points the OTHER way from this module's usual
    invariant on purpose. An absent fp is a fact about the plumbing's VERSION,
    not a signal about the reducer. Reading absence as change would wind down
    every worker in a mixed-version fleet at once — the same fleet-wide kill the
    transient threshold exists to prevent."""
    r = decide(0, "cc-02", "cc-02", 0, observed_token_fp=None, expected_token_fp=FP_A)
    assert r["verdict"] == VERDICT_CONTINUE
    # ...and the learned fp SURVIVES, so a fleet that upgrades mid-session does
    # not silently disarm the axis it had already armed.
    assert r["expected_token_fp"] == FP_A


def test_an_absent_expected_fp_cannot_wind_down():
    """The first poll after an upgrade has an observation and no baseline. One
    known operand is not a comparison."""
    r = decide(0, "cc-02", "cc-02", 0, observed_token_fp=FP_B, expected_token_fp=None)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["expected_token_fp"] == FP_B


def test_machine_takeover_still_wins_the_message_when_both_axes_move():
    """A cross-box takeover changes BOTH signals. Either ordering yields
    wind-down, so this pins only the more useful diagnostic — the one naming the
    two boxes."""
    r = decide(0, "cc-05", "cc-02", 0, observed_token_fp=FP_B, expected_token_fp=FP_A)
    assert r["verdict"] == VERDICT_WIND_DOWN
    assert "takeover" in r["reason"]


def test_legacy_positional_callers_keep_todays_behaviour_exactly():
    """The compatibility contract that lets this ship into a running fleet: a
    caller that never learned the new params gets the machine-only decision, and
    the new key is present-but-None rather than absent (a KeyError in poll())."""
    r = decide(0, "cc-02", "cc-02", 0)
    assert r["verdict"] == VERDICT_CONTINUE
    assert r["expected_token_fp"] is None


@pytest.mark.parametrize("rc", [1, 2, 3, 4, 7])
def test_every_non_live_branch_preserves_the_learned_fp(rc):
    """A blip or a not-live poll must not erase the reducer identity — same
    reasoning as test_transient_preserves_the_expected_machine, and it has to
    hold on EVERY return path or a single transient rc silently disarms the
    axis."""
    assert decide(rc, None, "cc-02", 0, expected_token_fp=FP_A)["expected_token_fp"] == FP_A


def test_the_token_fp_marker_is_still_what_the_emitter_actually_prints():
    """The emitter join, same shape and same reason as the LIVE_MARKER contract
    test above (guard-920). runner-claim.sh is bash-with-embedded-python and
    cannot import this constant, so asserting against its SOURCE is the only
    real link. Without this, a reformat upstream leaves every hand-copied
    parsing test green while `_parse_token_fp` silently returns None and the
    same-box-restart branch goes dead — the exact failure mode F2 found on the
    machine axis, on the axis added to close F2's own measured limit."""
    live_stmt = _emitter_branch("status: LIVE")
    assert TOKEN_FP_MARKER in live_stmt, (
        f"runner-claim.sh's LIVE branch no longer prints {TOKEN_FP_MARKER!r}, so "
        "worker_reducer_liveness._parse_token_fp can no longer read the claim's "
        "token fingerprint and SAME-BOX reducer-restart detection is dead "
        "(machine_id cannot see it — that is the whole reason this axis exists). "
        "Re-derive the parse against the emitter's new format — do NOT just "
        f"update this assertion.\nLIVE statement now reads:\n{live_stmt[:300]}"
    )


def test_the_emitter_never_prints_the_raw_token():
    """Security contract, asserted at the surface a reader actually sees.

    `runner_token` authorises `heartbeat` and `release_runner` via
    ConditionExpression, so a wrapper that echoed it would put a bearer
    credential into every worker's captured stdout, its state file, and any log
    that quotes a poll. The endpoint deliberately has no raw-token field to
    print (RunnerClaim carries only the digest); this pins the wrapper end so a
    future 'just add the token, it's easier to debug' edit fails here."""
    text = (SCRIPTS / "runner-claim.sh").read_text(encoding="utf-8")
    assert "runner_token_fp" in text, "the wrapper no longer reads the digest at all"
    bare = [ln for ln in text.splitlines()
            if "runner_token" in ln and "runner_token_fp" not in ln]
    assert bare == [], (
        "runner-claim.sh references the RAW runner_token: " + "; ".join(bare))


def test_poll_persists_the_fp_and_winds_down_on_a_same_box_restart(tmp_path):
    """End-to-end through the state file, on the shape that motivated the goal.

    Both polls report the SAME machine, so the machine axis cannot fire; only
    the fp moves. Two separate poll() calls because — exactly as with the
    transient counter — each real poll is its own PROCESS, so the baseline can
    only reach the second decision THROUGH the persisted state."""
    scripts = _claim_stub(tmp_path, "echo \"%s\"\nexit 0\n" % (LIVE_LINE + ", token-fp " + FP_A))
    agent_dir = tmp_path / "alpha"

    first = poll("alpha", agent_dir, "SID1", scripts)
    assert first["verdict"] == VERDICT_CONTINUE
    assert first["expected_token_fp"] == FP_A
    state = json.loads((agent_dir / "sessions" / "SID1" /
                        "reducer-liveness-state.json").read_text(encoding="utf-8"))
    assert state["expected_token_fp"] == FP_A

    # Same box, re-minted token: a new runner stale-broke in.
    _claim_stub(tmp_path, "echo \"%s\"\nexit 0\n" % (LIVE_LINE + ", token-fp " + FP_B))
    second = poll("alpha", agent_dir, "SID1", scripts)
    assert second["expected_machine"] == "cc-04"      # machine never moved
    assert second["verdict"] == VERDICT_WIND_DOWN
    assert "restart" in second["reason"]


def test_a_same_box_restart_ADOPTS_the_new_fp_so_the_wind_down_is_one_shot():
    """The fp axis fires ONCE. It must not re-assert the stale baseline.

    Persisting `expected_token_fp` on this branch made the verdict permanent for
    the life of the session dir — every later poll re-compared the same stale
    baseline against the same live fp and wound down again, with no path back.
    Measured 2026-08-30 on zc-03: a reducer relaunch re-minted the token and all
    7 worker Bodies latched, 41 state files none of which had learned the live
    fp. The wind-down's purpose is discharged by its first firing (the Body
    closes its unit and stages its WM); a Body that has already wound down has
    nothing left to orphan.
    """
    r = decide(0, "cc-02", "cc-02", 0, observed_token_fp=FP_B, expected_token_fp=FP_A)
    assert r["verdict"] == VERDICT_WIND_DOWN          # it still FIRES...
    assert r["expected_token_fp"] == FP_B             # ...and it adopts

    # The consequence, which is the whole point: feed the persisted state back in
    # (what the next poll does) and the Body rejoins under the new runner.
    again = decide(0, "cc-02", r["expected_machine"], r["consecutive_errors"],
                   observed_token_fp=FP_B, expected_token_fp=r["expected_token_fp"])
    assert again["verdict"] == VERDICT_CONTINUE


def test_a_cross_box_takeover_stays_LATCHED_unlike_the_same_box_restart():
    """The other half of the asymmetry, and the control that keeps the adoption
    above from being read as 'expected values are always adopted'.

    Cross-box, the premise the fp branch cannot claim IS true: a reducer on
    another box may never see this Body's locally-staged WM, so the Body must
    stay down until an operator relaunches it. Re-asserting `expected_machine`
    is what makes that stick, and it must keep sticking.
    """
    r = decide(0, "cc-05", "cc-02", 0, observed_token_fp=FP_B, expected_token_fp=FP_A)
    assert r["verdict"] == VERDICT_WIND_DOWN
    assert r["expected_machine"] == "cc-02"           # NOT adopted

    again = decide(0, "cc-05", r["expected_machine"], r["consecutive_errors"],
                   observed_token_fp=FP_B, expected_token_fp=r["expected_token_fp"])
    assert again["verdict"] == VERDICT_WIND_DOWN      # still down, by design


def test_poll_rejoins_under_the_new_runner_on_the_poll_after_a_restart(tmp_path):
    """The fleet-recovery proof, end to end through the state file.

    Three separate poll() calls, because the only channel between them is the
    persisted baseline — which is exactly what was frozen. Poll 3 is the one
    that could never happen before this fix: on zc-03 it was poll 3, 4, 5 ...
    each re-reading fp A against a live fp B, for five hours.
    """
    agent_dir = tmp_path / "alpha"
    live_with = lambda fp: "echo \"%s\"\nexit 0\n" % (LIVE_LINE + ", token-fp " + fp)

    scripts = _claim_stub(tmp_path, live_with(FP_A))
    assert poll("alpha", agent_dir, "SID1", scripts)["verdict"] == VERDICT_CONTINUE

    # The reducer is relaunched on the SAME box: token re-minted, machine identical.
    _claim_stub(tmp_path, live_with(FP_B))
    second = poll("alpha", agent_dir, "SID1", scripts)
    assert second["verdict"] == VERDICT_WIND_DOWN
    assert "restart" in second["reason"]

    state = json.loads((agent_dir / "sessions" / "SID1" /
                        "reducer-liveness-state.json").read_text(encoding="utf-8"))
    assert state["expected_token_fp"] == FP_B, "the baseline never advanced — latched"

    third = poll("alpha", agent_dir, "SID1", scripts)
    assert third["verdict"] == VERDICT_CONTINUE, (
        "a worker that wound down on a same-box restart could never rejoin")


def test_poll_against_a_pre_upgrade_emitter_continues_and_learns_nothing(tmp_path):
    """The mixed-version fleet, end to end: an old wrapper emits no fp clause,
    so two polls in a row stay CONTINUE with a null baseline. A regression that
    treated None as a change would wind down every worker whose box had not been
    upgraded yet."""
    scripts = _claim_stub(tmp_path, FAKE_LIVE)
    agent_dir = tmp_path / "alpha"

    for _ in range(2):
        r = poll("alpha", agent_dir, "SID1", scripts)
        assert r["verdict"] == VERDICT_CONTINUE
        assert r["expected_token_fp"] is None


def test_cli_seam_drives_the_fp_axis():
    """The worker loop branches on rc, and the two new argv slots are how a
    shell-side probe reaches this branch at all."""
    restart = _cli("0", "cc-02", "cc-02", "0", str(DEFAULT_ERROR_THRESHOLD), FP_B, FP_A)
    assert restart.returncode == 1
    assert json.loads(restart.stdout)["verdict"] == VERDICT_WIND_DOWN

    steady = _cli("0", "cc-02", "cc-02", "0", str(DEFAULT_ERROR_THRESHOLD), FP_A, FP_A)
    assert steady.returncode == 0
    assert json.loads(steady.stdout)["verdict"] == VERDICT_CONTINUE

    # Trailing args omitted -> today's behaviour, unchanged.
    legacy = _cli("0", "cc-02", "cc-02", "0")
    assert legacy.returncode == 0
    assert json.loads(legacy.stdout)["expected_token_fp"] is None
