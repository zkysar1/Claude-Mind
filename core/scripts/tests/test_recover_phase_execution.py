"""`iteration-close.sh --phase recover` is EXECUTED here, not read ().

`test_clear_in_flight_call_site_scoping.py` pins the recovery site by reading
`do_recover`'s source: the `--if-goal "$_gid"` argument is present, the empty-id
guard is present, and the guard is ordered above the clear. Those are SOURCE
pins. They assert that lines exist in a file. Nothing in the tree ever ran the
phase and watched what it did — measured at filing time, `--phase recover`
occurred 5 times across `core/scripts/` and `.claude/` and all 5 were comments
or docstrings, zero were invocations.

That gap matters because `do_recover`'s correctness is a CHAIN, and a source pin
can only see its ends: checkpoint present and parseable -> `_gid`/`_src`
non-empty -> early return skipped -> status probe says `completed` ->
`team-state-clear-in-flight.sh` invoked with `--if-goal $_gid`. g-306-161 proved
the middle of exactly this chain can silently drop the flag while both ends
still read correct.

Beware the look-alike: `test_recovery_clears_team_state_in_flight.py` reads by
name like this coverage and collects ZERO tests (main()-style, no top-level
`def test_`), and per exp-g-306-161 it drives `session-manifest-clear.sh`, a
different caller. It answers "is recovery covered?" yes by its filename and no
by its contents -- the guard-1760 shape.

WHY THE MUTATION TEST IS NOT OPTIONAL. The second assertion is a NEGATIVE --
"the clear was not invoked". A negative passes for free if the script died
before ever reaching the decision, so on its own it cannot distinguish "the
guard worked" from "the harness never got there". `test_the_guard_is_what_stops
_it` deletes ONLY the guard line from the staged copy and requires the clear to
fire; that is what makes the negative mean what it says (guard-1829/guard-1856:
a mutation proof establishes exactly the proposition it mutated, so the mutation
must be the guard itself and nothing else).

Hermetic by the sibling file's pattern: stage a COPY of the real
`iteration-close.sh` beside stub `_paths.sh`/`_platform.sh`, so the copy
resolves PROJECT_ROOT/WORLD_DIR/AGENT_DIR entirely inside tmp and every
collaborator it shells out to is a stub that records its argv. Real bytes of the
script under test, no daemon, no live world, no network.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_recover_phase_execution.py -q
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: explicit bash binary)

SCRIPTS = Path(__file__).resolve().parents[1]
ITERATION_CLOSE = SCRIPTS / "iteration-close.sh"

AGENT = "alpha"
GID = "g-777-1"

# The guard whose removal is the mutation. Byte-identical to the line in
# do_recover; _stage asserts it matched exactly once, so a refactor that
# reworded it fails loudly here instead of silently disarming the control.
GID_GUARD = '[[ -z "$_gid" || -z "$_src" ]] && return 0'

# Stub _paths.sh. iteration-close.sh sets SCRIPT_DIR from its own location and
# sources this, so everything below resolves inside tmp. `python3` is put on
# PATH as a shim that execs the interpreter running pytest -- the real
# _paths.sh does the same thing via core/scripts/.python-shim, and without it
# the script's inline `python3 -c` blocks hit the Microsoft Store stub on
# Windows (python-invocation.md).
STUB_PATHS = """
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"
WORLD_DIR="$PROJECT_ROOT/world"
META_DIR="$PROJECT_ROOT/meta"
export PATH="$PROJECT_ROOT/shim:$PATH"
agent_dir() { printf '%s' "$PROJECT_ROOT/agents/$1"; }
"""

# Records argv one-per-line, then succeeds. The sink path travels by env so no
# test data is interpolated into shell source (guard-165).
STUB_CLEAR = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$CLEAR_SINK"
exit 0
"""

STUB_LOOP_SAVE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$LOOP_SINK"
exit 0
"""

PY_SHIM = """#!/usr/bin/env bash
exec "$PY_REAL" "$@"
"""


def _stage(tmp_path, drop_gid_guard=False):
    """Copy the REAL iteration-close.sh into a tmp project root beside stubs.

    drop_gid_guard removes ONLY the empty-`_gid` early return, reproducing the
    pre-guard script exactly. It ships with the test as a permanent negative
    control so the discrimination outlives this commit.
    """
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)

    src = ITERATION_CLOSE.read_text(encoding="utf-8")
    if drop_gid_guard:
        kept = [ln for ln in src.splitlines(True) if ln.strip() != GID_GUARD]
        assert len(kept) == len(src.splitlines(True)) - 1, (
            "expected exactly one empty-_gid guard line to remove; found "
            f"{len(src.splitlines(True)) - len(kept)}. The guard was reworded "
            "-- update GID_GUARD, do not weaken this assertion.")
        src = "".join(kept)
    (core / "iteration-close.sh").write_text(src, encoding="utf-8")

    (core / "_paths.sh").write_text(STUB_PATHS, encoding="utf-8")
    (core / "_platform.sh").write_text("", encoding="utf-8")
    # Real bytes, not a stub: this helper rewrites --goal-id/--goal=<id> into
    # the canonical form before the strict parse loop, and it is self-contained
    # (sources nothing). Stubbing it would replace production arg handling with
    # a guess in the one test that claims to execute the production path.
    (core / "_goal-arg-normalize.sh").write_text(
        (SCRIPTS / "_goal-arg-normalize.sh").read_text(encoding="utf-8"),
        encoding="utf-8")
    (core / "team-state-clear-in-flight.sh").write_text(STUB_CLEAR, encoding="utf-8")
    (core / "loop-state-save.sh").write_text(STUB_LOOP_SAVE, encoding="utf-8")

    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text(PY_SHIM, encoding="utf-8")
    (shim / "python3").chmod(0o755)

    (tmp_path / "agents" / AGENT / "session").mkdir(parents=True)
    (tmp_path / "world").mkdir()
    return core / "iteration-close.sh"


def _write_checkpoint(tmp_path, **fields):
    cp = tmp_path / "agents" / AGENT / "session" / "iteration-checkpoint.json"
    cp.write_text(json.dumps(fields), encoding="utf-8")


def _write_aspirations(tmp_path, goal_id, status):
    """One world aspiration carrying goal_id at the given status."""
    row = {"id": "asp-777", "goals": [{"id": goal_id, "status": status}]}
    (tmp_path / "world" / "aspirations.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")


def _run_recover(tmp_path, script):
    clear_sink = tmp_path / "clear-argv.txt"
    loop_sink = tmp_path / "loop-argv.txt"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "MIND_AGENT": AGENT,
        "PY_REAL": sys.executable,
        "CLEAR_SINK": str(clear_sink),
        "LOOP_SINK": str(loop_sink),
    }
    # guard-580/581: never a bare "bash" argv[0], never str(WindowsPath).
    proc = subprocess.run([BASH, Path(script).as_posix(), "--phase", "recover"],
                          capture_output=True, text=True, env=env, timeout=120)
    # The stub writes one argv entry per line, so the text ends in a trailing
    # newline and split() yields one spurious "" at the END. Drop exactly that
    # one -- NEVER filter all empties. An empty argv entry is the value this
    # suite exists to observe (`--if-goal ""` is the degraded-CAS signature),
    # and a `[a for a in argv if a]` convenience filter silently discards it,
    # turning the unscoped-clear assertion into an IndexError at best and a
    # false pass at worst. Measured while writing this file.
    raw = clear_sink.read_text(encoding="utf-8") if clear_sink.exists() else ""
    clear_argv = raw.split("\n")[:-1] if raw.endswith("\n") else (
        raw.split("\n") if raw else [])
    loop_argv = (loop_sink.read_text(encoding="utf-8") if loop_sink.exists() else "")
    return proc, clear_argv, loop_argv


# ── 1. the clear IS invoked, scoped to the checkpoint's goal ─────────────────

def test_recover_clears_in_flight_scoped_to_the_checkpoint_goal(tmp_path):
    """The whole chain, executed: checkpoint parsed -> _gid/_src non-empty ->
    status probe reads `completed` -> clear invoked carrying that exact id."""
    script = _stage(tmp_path)
    _write_checkpoint(tmp_path, intent_state="complete", goal_id=GID,
                      source="world", intent_outcome="deep")
    _write_aspirations(tmp_path, GID, "completed")

    proc, clear_argv, loop_argv = _run_recover(tmp_path, script)

    assert proc.returncode == 0, proc.stderr
    assert clear_argv, (
        "the recovery phase never invoked team-state-clear-in-flight.sh; "
        f"stderr was: {proc.stderr}")
    assert "--if-goal" in clear_argv, (
        f"the clear ran UNSCOPED -- argv was {clear_argv}")
    assert clear_argv[clear_argv.index("--if-goal") + 1] == GID, (
        f"--if-goal carried the wrong id; argv was {clear_argv}")
    assert clear_argv[clear_argv.index("--agent") + 1] == AGENT, (
        f"--agent carried the wrong agent; argv was {clear_argv}")
    # The forward-recovery branch also finishes the transition. Asserting this
    # distinguishes "took the completed branch" from "took the rollback branch
    # and happened not to clear".
    assert "intent_state=committed" in loop_argv, (
        f"forward-recovery did not commit the intent; loop argv was {loop_argv!r}")


# ── 2. the clear is NOT invoked on an empty checkpoint goal id ───────────────

def test_recover_does_not_clear_when_the_checkpoint_goal_id_is_empty(tmp_path):
    """An empty `_gid` is dropped by the wrapper's `-n` check, so a clear issued
    with one silently degrades to UNCONDITIONAL -- it would blank whatever row
    is standing, including a live claim from a newer iteration. do_recover must
    return before reaching the clear.

    This assertion is a negative and is only meaningful alongside test 1 (same
    harness, clear observed) and test 3 (the guard is what stops it).
    """
    script = _stage(tmp_path)
    _write_checkpoint(tmp_path, intent_state="complete", goal_id="",
                      source="world", intent_outcome="deep")
    _write_aspirations(tmp_path, GID, "completed")

    proc, clear_argv, loop_argv = _run_recover(tmp_path, script)

    assert proc.returncode == 0, proc.stderr
    assert clear_argv == [], (
        "do_recover issued an UNSCOPED clear from an empty checkpoint goal id "
        f"-- argv was {clear_argv}")
    assert loop_argv == "", (
        "do_recover fell through to a branch that mutates loop state despite an "
        f"empty goal id; loop argv was {loop_argv!r}")


# ── 3. negative control: the guard is what stops it ─────────────────────────

def test_removing_the_guard_lets_the_empty_id_clear_fire(tmp_path):
    """Delete ONLY the empty-`_gid` early return and the clear fires with an
    empty `--if-goal`. This is what proves test 2 observes the guard rather than
    an early crash, and it is why test 2 is not vacuous.

    THE GOAL RECORD HERE CARRIES AN EMPTY id, AND THAT IS THE POINT -- measured
    while writing this suite, not assumed. do_recover has TWO independent
    barriers against an unscoped clear, and the source pin in
    test_clear_in_flight_call_site_scoping.py can only see the first:

      1. the `_gid` guard (the line this test removes), and
      2. the status probe, which looks the id up in aspirations.jsonl -- an
         empty id matches no goal, so `_status` comes back empty, the
         `== completed` branch is not taken, and control reaches the ROLLBACK
         branch, which issues no clear at all.

    So with an ordinary corpus, deleting the guard changes NOTHING observable:
    the first attempt at this control removed the guard, kept a normal
    aspirations.jsonl, and saw `split-brain detected for  (intent=complete,
    status=unknown)` -- barrier 2 caught it. A control that cannot fail proves
    nothing (guard-1856: a mutation establishes exactly the proposition it
    mutated), so barrier 2 has to be held OPEN for barrier 1 to be under test.

    A goal record with an empty id and status `completed` is what holds it
    open, and it is precisely the corpus the guard exists for: without the
    guard, that record makes the probe answer `completed`, the clear fires with
    `--if-goal ""`, the wrapper's `-n` check drops the empty value, and the CAS
    degrades to an unconditional clear that blanks whatever row is standing --
    the g-306-170 class. The guard is load-bearing for a NARROWER input than
    "any empty checkpoint goal id", and naming that input is what this control
    contributes over the source pin.
    """
    script = _stage(tmp_path, drop_gid_guard=True)
    _write_checkpoint(tmp_path, intent_state="complete", goal_id="",
                      source="world", intent_outcome="deep")
    _write_aspirations(tmp_path, "", "completed")

    proc, clear_argv, _ = _run_recover(tmp_path, script)

    assert proc.returncode == 0, proc.stderr
    assert clear_argv, (
        "with the guard removed the clear STILL did not fire -- test 2 is "
        "passing for some reason other than the guard, and this suite does not "
        f"prove what it claims. stderr: {proc.stderr}")
    assert clear_argv[clear_argv.index("--if-goal") + 1] == "", (
        f"expected the unguarded path to pass an empty --if-goal; got {clear_argv}")
