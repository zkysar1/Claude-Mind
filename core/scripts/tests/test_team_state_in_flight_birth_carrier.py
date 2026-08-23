"""Birth-carrier invariants on team-state-in-flight.sh ().

WHY THIS FILE IS STRUCTURAL RATHER THAN FUNCTIONAL, deliberately:

The write under test fires only when `-d "$(agent_dir "$AGENT")"` holds, and the
row write that immediately follows it goes through the daemon into the SHARED
team-state. `PROJECT_ROOT` is computed from the script's own location
(`_paths.sh:31`) and is not env-overridable, so exercising the positive path
requires a real agent dir under `agents/` -- at which point the sibling row write
manufactures a live shard for a fixture name. That is exactly the phantom-roster
hazard guard-2611 records happening to two earlier test files, which each created
a REAL `agent_status` row on every suite run while both owning suites stayed
green (a body-row write prints no stdout).

So the three regressions worth pinning are all SOURCE-SHAPE defects, and a
structural assertion is the stronger instrument for each:

  (a) ORDER -- carrier written AFTER the row reintroduces the crash window this
      change closes. A crash between the two writes must land on the carrier
      side, because body_row_reaper iterates ROWS: an orphan carrier is inert,
      an orphan row is a phantom that is unreapable at any age.
  (b) RESIDENCY -- the carrier write escaping the `_NO_ROW_REASON` gate makes it
      a per-principal writer with a predicate WEAKER than the row's, which is
      the asymmetry guard-2611 forbids ("a row can only exist where it could
      have been written").
  (c) PATH -- the carrier must resolve through `agent_state_dir` (the agent-wide
      `session/` dir). heartbeat-tick.sh gates its own carrier write on the
      per-SID `sessions/<SID>/` dir; that NARROWER predicate is the original
      defect, because a Body with no per-SID dir gets a row and can never get a
      carrier.

None of (a) or (b) is reachable by a functional test that does not itself create
the phantom it exists to prevent, so do not "upgrade" this file to an
integration test without first solving the isolation problem above.

Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_team_state_in_flight_birth_carrier.py -q
"""
import re
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = CORE_SCRIPTS / "team-state-in-flight.sh"
SRC = SCRIPT.read_text(encoding="utf-8")


def _line_of(pattern):
    """1-indexed line number of the first line matching `pattern`, or None."""
    for i, line in enumerate(SRC.splitlines(), start=1):
        if re.search(pattern, line):
            return i
    return None


def test_script_is_syntactically_valid():
    """Positive control for the whole file: every assertion below reads SOURCE
    TEXT, which stays true of a file bash can no longer parse. Without this,
    a syntax error would leave the rest of this suite passing."""
    r = subprocess.run([_bash(), "-n", SCRIPT.as_posix()],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def _bash():
    sys.path.insert(0, str(CORE_SCRIPTS))
    from _runtime_bash import bash_cmd  # noqa: E402
    return bash_cmd("--version")[0]


def test_birth_carrier_is_written_before_the_body_row():
    """(a) ORDER. Reversing these two writes is silent -- both still 'work' --
    and it re-opens the window that produces an unreapable row."""
    carrier = _line_of(r'^\s*_IF_CARRIER=')
    row = _line_of(r'in_flight_bodies\.\$\{MIND_SID\}')
    assert carrier is not None, "birth-carrier write not found (g-306-349)"
    assert row is not None, "body-row write not found"
    assert carrier < row, (
        f"birth carrier (line {carrier}) must be written BEFORE the body row "
        f"(line {row}) -- an orphan carrier is inert, an orphan row is a "
        f"permanently-unreapable phantom (g-306-349)")


def test_birth_carrier_sits_inside_the_residency_gate():
    """(b) RESIDENCY. The `no local agent dir` guard exits before either write;
    the carrier must be downstream of it, on the identical predicate as the
    row (guard-2611)."""
    gate = _line_of(r'_NO_ROW_REASON="no local agent dir')
    carrier = _line_of(r'^\s*_IF_CARRIER=')
    assert gate is not None, "residency gate not found -- guard-2611 invariant"
    assert carrier is not None
    assert gate < carrier, (
        f"birth carrier (line {carrier}) must sit AFTER the residency gate "
        f"(line {gate}) or it becomes a phantom-roster producer (guard-2611)")


def test_birth_carrier_resolves_through_agent_state_dir():
    """(c) PATH. Must use the agent-wide `session/` helper, never the per-SID
    `sessions/<SID>/` dir whose narrower predicate is the original defect."""
    state_line = [ln for ln in SRC.splitlines() if ln.strip().startswith("_IF_STATE_DIR=")]
    assert state_line, "birth-carrier state-dir assignment not found"
    assert 'agent_state_dir' in state_line[0], (
        "birth carrier must resolve via agent_state_dir (CLAUDE.md "
        "Agent-dir Resolution -- never hand-join PROJECT_ROOT/agent): "
        + state_line[0])
    carrier_line = [ln for ln in SRC.splitlines() if ln.strip().startswith("_IF_CARRIER=")]
    assert carrier_line, "birth-carrier assignment not found"
    assert '_IF_STATE_DIR' in carrier_line[0], carrier_line[0]
    assert 'SESSIONS_DIRNAME' not in carrier_line[0], (
        "carrier must NOT resolve under sessions/<SID>/ -- that narrower "
        "predicate is exactly what makes a row unreapable (g-306-349)")


def test_birth_carrier_creates_its_own_state_dir():
    """(d) THE GATE MUST NOT BE NARROWER THAN THE WRITE -- the defect this
    whole change fixes, reintroduced one level down and caught by fresh-eyes
    on the change itself (2026-08-22).

    The residency gate proves the AGENT dir exists; the carrier is written into
    `<agent>/session/`, a DIFFERENT directory. Measured that day: 2 of 11 agent
    dirs on the box had no `session/`. Without the mkdir the redirect fails, the
    `||` prints a WARN nobody reads, and the row is created with NO CARRIER --
    i.e. exactly the unreapable-row condition, silently, for the agents most
    likely to be partially initialized."""
    assert re.search(r'mkdir -p "\$_IF_STATE_DIR"', SRC), (
        "birth carrier must create its own state dir -- the residency gate "
        "covers the AGENT dir, not <agent>/session/ (g-306-349 fresh-eyes)")
    # And the write must still be guarded, so a failed mkdir degrades to
    # skip-with-a-warning rather than a failed redirect.
    assert re.search(r'if \[ -d "\$_IF_STATE_DIR" \] && \[ ! -f "\$_IF_CARRIER" \]', SRC), (
        "carrier write must be guarded on the state dir EXISTING as well as "
        "the carrier being absent")


def test_birth_carrier_does_not_suppress_its_own_diagnostic():
    """rb-400 / heartbeat-tick.sh L31-32: a silenced carrier failure recreates
    the invisible no-carrier row this change exists to prevent."""
    block = SRC.split("_IF_CARRIER=", 1)[1].split("FAIL-OPEN", 1)[0]
    assert "2>/dev/null" not in block, (
        "birth-carrier write must not suppress stderr (rb-400) -- a silent "
        "failure is indistinguishable from the defect being fixed")


def test_birth_carrier_only_creates_when_absent():
    """Refreshing a live carrier is heartbeat-tick.sh's job. Clobbering its
    timestamp here would put two writers on one liveness signal."""
    block = SRC.split("_IF_CARRIER=", 1)[1].split("FAIL-OPEN", 1)[0]
    assert re.search(r'! -f "\$_IF_CARRIER"', block), (
        "birth carrier must be created only when absent -- heartbeat-tick.sh "
        "owns refreshing it")
