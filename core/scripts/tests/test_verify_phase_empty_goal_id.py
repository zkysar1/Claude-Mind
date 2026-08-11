"""Is `do_verify`'s required-arg guard what stops an unscoped clear? EXECUTED ().

`test_clear_in_flight_call_site_scoping.py::test_do_verify_still_refuses_an_empty_goal_id`
is a SOURCE pin: it asserts a condition string exists in `iteration-close.sh`.
Its message claims that without that guard "the verify-site CAS can now degrade
to an unconditional clear without any test noticing". Nothing ever ran
`--phase verify` with the guard removed to see whether that is true.

It is NOT true, and this file is the measurement. `do_verify` has TWO
independent barriers between an empty `$GOAL_ID` and the clear at the bottom of
the function, and the source pin can only see the first:

  1. the required-arg guard (the condition this file disarms), and
  2. `"${update_cmd[@]}"` -- the UNGUARDED call to `aspirations-update-goal.sh`,
     running under `set -euo pipefail` (iteration-close.sh:53). Measured against
     the real wrapper on 2026-08-04 (alpha, hostname cc-04, uname -r
     6.8.0-136-generic, Linux):

         bash core/scripts/aspirations-update-goal.sh --source world "" status completed
         -> rc=1, "Error: goal_id, field, and value are all required."

     so with the guard disarmed the script ABORTS there, one write short of the
     clear.

WHY THAT IS A STRONGER RESULT THAN do_recover's, not merely a similar one.
`test_recover_phase_execution.py` found the same two-barrier shape at the
recovery site, but there barrier 2 (a status probe against aspirations.jsonl)
can be held open by an INPUT -- a goal record carrying an empty id at status
`completed` -- so the recovery guard is genuinely load-bearing for a narrow,
reachable corpus. Here barrier 2 is a COLLABORATOR'S CONTRACT, not a property of
the input: no aspirations.jsonl, checkpoint, or flag combination makes
`aspirations-update-goal.sh` accept an empty goal id. `test_barrier_two_is_a
_collaborator_contract_not_an_input` holds it open the only way anything can --
by replacing that collaborator -- and that is what the guard's necessity would
depend on.

So the verify guard is redundant for CAS SAFETY and load-bearing for
DIAGNOSTICS: it turns an opaque mid-write `set -e` abort into `exit 2` plus a
usage line naming the missing flags. That is worth keeping and worth stating
accurately; it is not what the pin's message says.

THE FAITHFUL STUB IS THE LOAD-BEARING DESIGN CHOICE HERE. `STUB_UPDATE` refuses
an empty positional exactly as the real wrapper does, because a stub that
returned 0 unconditionally would assume barrier 2 away and manufacture the
opposite verdict -- the clear would fire and this file would "prove" the guard
necessary (guard-1660: a mutation proof establishes exactly the proposition it
mutated, so the collaborators around the mutation have to be the production
ones). The rc=1-on-empty contract above is the measurement that licenses it;
re-measure it before trusting this file if that wrapper's arg handling changes.

Hermetic by the sibling file's pattern: stage a COPY of the real
`iteration-close.sh` beside stub `_paths.sh`/`_platform.sh`, so the copy
resolves PROJECT_ROOT/WORLD_DIR/AGENT_DIR entirely inside tmp and every
collaborator it shells out to is a stub that records its argv. Real bytes of the
script under test, no daemon, no live world, no network.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_verify_phase_empty_goal_id.py -q
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

# The guard whose disarming is the mutation. Byte-identical to the condition in
# do_verify, and the SAME string test_do_verify_still_refuses_an_empty_goal_id
# pins -- so if that pin is reworded without this constant following, _stage
# fails loudly rather than silently mutating nothing.
#
# Disarmed by rewriting the condition to `false` rather than deleting the block:
# the nine-line if/fi carries the usage text, and deleting it would mutate the
# diagnostics as well as the guard. Replacing only the test leaves every other
# byte of do_verify intact, so what this file measures is the guard and nothing
# adjacent (guard-1856).
VERIFY_GUARD = ('if [[ -z "$GOAL_ID" || -z "$GOAL_STATUS" || -z "$SOURCE" '
                '|| -z "$OUTCOME" ]]; then')

STUB_PATHS = """
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"
WORLD_DIR="$PROJECT_ROOT/world"
META_DIR="$PROJECT_ROOT/meta"
AGENT_DIR="$PROJECT_ROOT/agents/alpha"
export PATH="$PROJECT_ROOT/shim:$PATH"
agent_dir() { printf '%s' "$PROJECT_ROOT/agents/$1"; }
"""

STUB_CLEAR = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$CLEAR_SINK"
exit 0
"""

# Faithful to the real wrapper's measured contract: an empty goal-id positional
# is refused with a nonzero exit BEFORE any daemon call. UPDATE_ACCEPTS_EMPTY
# holds that barrier open -- the only way anything can, since no input reaches
# it (see the module docstring).
STUB_UPDATE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$UPDATE_SINK"
if [ "${UPDATE_ACCEPTS_EMPTY:-0}" != "1" ]; then
    for a in "$@"; do
        if [ -z "$a" ]; then
            echo "Error: goal_id, field, and value are all required." >&2
            exit 1
        fi
    done
fi
exit 0
"""

STUB_OK = """#!/usr/bin/env bash
cat >/dev/null 2>&1 || true
exit 0
"""

PY_SHIM = """#!/usr/bin/env bash
exec "$PY_REAL" "$@"
"""

# Collaborators do_verify shells out to on the completed/world path. Each is a
# no-op that consumes stdin (execution-diary and board-post are fed by pipes).
PASSTHROUGH_STUBS = (
    "pending-deploys-gate.sh",
    "loop-state-save.sh",
    "aspirations-complete-by.sh",
    "execution-diary.sh",
    "board-post.sh",
    "close-defer-invalidation.py",
)


def _stage(tmp_path, disarm_guard=False):
    """Copy the REAL iteration-close.sh into a tmp project root beside stubs.

    disarm_guard rewrites do_verify's required-arg condition to `false`,
    reproducing a script whose guard is present-but-inert. It ships with the
    test as a permanent negative control so the discrimination outlives this
    commit.
    """
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)

    src = ITERATION_CLOSE.read_text(encoding="utf-8")
    if disarm_guard:
        # guard-2312: EXISTENCE IS NOT UNIQUENESS. Refuse loudly unless the
        # mutation target matches exactly one site -- a second match would mean
        # this file mutates something it never inspected.
        assert src.count(VERIFY_GUARD) == 1, (
            "expected exactly one do_verify required-arg guard to disarm; found "
            f"{src.count(VERIFY_GUARD)}. The guard was reworded -- update "
            "VERIFY_GUARD, do not weaken this assertion.")
        src = src.replace(VERIFY_GUARD, "if false; then")
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
    (core / "aspirations-update-goal.sh").write_text(STUB_UPDATE, encoding="utf-8")
    for name in PASSTHROUGH_STUBS:
        (core / name).write_text(STUB_OK, encoding="utf-8")
    for f in core.iterdir():
        f.chmod(0o755)

    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").write_text(PY_SHIM, encoding="utf-8")
    (shim / "python3").chmod(0o755)

    (tmp_path / "agents" / AGENT / "session").mkdir(parents=True)
    (tmp_path / "world").mkdir()
    (tmp_path / "core" / "logs").mkdir(parents=True)
    return core / "iteration-close.sh"


def _write_aspirations(tmp_path, goal_id, status, recurring=False):
    """One world aspiration carrying goal_id at the given status."""
    row = {"id": "asp-777",
           "goals": [{"id": goal_id, "status": status, "recurring": recurring}]}
    (tmp_path / "world" / "aspirations.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")


def _run_verify(tmp_path, script, args, accepts_empty=False):
    clear_sink = tmp_path / "clear-argv.txt"
    update_sink = tmp_path / "update-argv.txt"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "MIND_AGENT": AGENT,
        "PY_REAL": sys.executable,
        "CLEAR_SINK": str(clear_sink),
        "UPDATE_SINK": str(update_sink),
    }
    if accepts_empty:
        env["UPDATE_ACCEPTS_EMPTY"] = "1"
    # guard-580/581: never a bare "bash" argv[0], never str(WindowsPath).
    proc = subprocess.run([BASH, Path(script).as_posix(), "--phase", "verify"] + args,
                          capture_output=True, text=True, env=env, timeout=120)
    # The stub writes one argv entry per line, so the text ends in a trailing
    # newline and split() yields one spurious "" at the END. Drop exactly that
    # one -- NEVER filter all empties. An empty argv entry is the value this
    # suite exists to observe (`--if-goal ""` is the degraded-CAS signature),
    # and a `[a for a in argv if a]` convenience filter silently discards it,
    # turning the unscoped-clear assertion into an IndexError at best and a
    # false pass at worst.
    raw = clear_sink.read_text(encoding="utf-8") if clear_sink.exists() else ""
    clear_argv = raw.split("\n")[:-1] if raw.endswith("\n") else (
        raw.split("\n") if raw else [])
    update_raw = (update_sink.read_text(encoding="utf-8")
                  if update_sink.exists() else "")
    return proc, clear_argv, update_raw


# ── 1. positive control: the harness reaches the clear ──────────────────────

def test_verify_clears_in_flight_scoped_to_the_closing_goal(tmp_path):
    """An ordinary close, executed end to end: the clear fires carrying the
    goal id the close is for.

    This is the positive control for everything below. Both negatives in this
    file assert that a clear did NOT happen, and a negative passes for free if
    the harness never reaches the decision point -- so without this test they
    would prove nothing (guard-1829).
    """
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, clear_argv, _ = _run_verify(
        tmp_path, script,
        ["--goal", GID, "--status", "completed", "--source", "world",
         "--outcome", "routine"])

    assert clear_argv, (
        "the verify phase never invoked team-state-clear-in-flight.sh; "
        f"stderr was: {proc.stderr}")
    assert "--if-goal" in clear_argv, (
        f"the clear ran UNSCOPED -- argv was {clear_argv}")
    assert clear_argv[clear_argv.index("--if-goal") + 1] == GID, (
        f"--if-goal carried the wrong id; argv was {clear_argv}")
    assert clear_argv[clear_argv.index("--agent") + 1] == AGENT, (
        f"--agent carried the wrong agent; argv was {clear_argv}")


# ── 2. the guard refuses, and says which flag is missing ────────────────────

def test_verify_refuses_an_absent_goal_and_names_the_missing_flag(tmp_path):
    """With the guard in place, `--phase verify` without `--goal` exits 2 and
    names the flag. No clear, no state write.

    This is the behaviour the source pin means to protect. Asserting the exit
    code AND the message is deliberate: the diagnostics are what the guard
    actually contributes (see test 3), so a change that kept the refusal but
    dropped the usage line would be a real regression this catches.
    """
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, clear_argv, update_raw = _run_verify(
        tmp_path, script,
        ["--status", "completed", "--source", "world", "--outcome", "routine"])

    assert proc.returncode == 2, (
        f"expected the required-arg guard's exit 2; got {proc.returncode}. "
        f"stderr: {proc.stderr}")
    assert "--goal" in proc.stderr, (
        f"the refusal did not name the missing flag: {proc.stderr!r}")
    assert clear_argv == [], (
        f"do_verify issued a clear despite refusing the close: {clear_argv}")
    assert update_raw == "", (
        f"do_verify wrote goal state despite refusing the close: {update_raw!r}")


# ── 3. THE MEASUREMENT: disarming the guard does NOT reach the clear ────────

def test_disarming_the_guard_still_does_not_reach_the_clear(tmp_path):
    """Disarm the required-arg guard, omit `--goal`, and the clear STILL does
    not fire -- because `aspirations-update-goal.sh` refuses the empty id and
    `set -e` aborts the phase before the clear is reached.

    This is the answer to g-306-180 scope (b), and it FALSIFIES the claim in
    test_do_verify_still_refuses_an_empty_goal_id's assertion message that
    removing the guard lets the verify CAS "degrade to an unconditional clear".
    It cannot: barrier 2 stops it. The guard's real contribution is the exit-2
    diagnostic asserted in test 2, not CAS safety.

    The assertion on the update sink is what distinguishes "barrier 2 caught it"
    from "the script died somewhere earlier and this negative is vacuous" -- the
    empty argv entry proves control reached the update call carrying the empty
    id, which is one line short of the clear.
    """
    script = _stage(tmp_path, disarm_guard=True)
    _write_aspirations(tmp_path, GID, "completed")

    proc, clear_argv, update_raw = _run_verify(
        tmp_path, script,
        ["--status", "completed", "--source", "world", "--outcome", "routine"])

    assert clear_argv == [], (
        "with the guard disarmed the clear fired anyway -- barrier 2 no longer "
        "holds, so the verify guard IS load-bearing for CAS safety and this "
        f"file's verdict is stale. argv was {clear_argv}")
    assert update_raw.split("\n")[:5] == ["--source", "world", "", "status",
                                          "completed"], (
        "control did not reach the status update carrying an empty goal id, so "
        "the negative above is vacuous -- something aborted earlier. update "
        f"argv was {update_raw!r}")
    assert proc.returncode != 0, (
        "the phase exited 0 with the guard disarmed and no --goal; the abort "
        f"this test attributes to `set -e` did not happen. stderr: {proc.stderr}")


# ── 4. discriminator: what the guard's necessity would actually depend on ───

def test_barrier_two_is_a_collaborator_contract_not_an_input(tmp_path):
    """Hold barrier 2 open and the clear fires with an empty `--if-goal`.

    This is the discriminator for test 3 (guard-1829): it proves test 3 observes
    barrier 2 rather than an early crash, because the ONLY difference between
    the two runs is whether the update collaborator accepts an empty positional.

    It also names the finding precisely. At the RECOVERY site an input can hold
    barrier 2 open -- a goal record with an empty id at status `completed`, which
    a malformed aspirations.jsonl really can carry, so that guard is load-bearing
    (test_recover_phase_execution.py::test_removing_the_guard_lets_the_empty_id
    _clear_fire). Here nothing in any input reaches barrier 2: it is
    `aspirations-update-goal.sh`'s own refusal. Making the verify guard
    load-bearing would take a CHANGE TO THAT WRAPPER, not a corpus. If this test
    ever has to stop stubbing the refusal away to see a clear, that change has
    happened and the verify guard has become load-bearing.
    """
    script = _stage(tmp_path, disarm_guard=True)
    _write_aspirations(tmp_path, GID, "completed")

    proc, clear_argv, _ = _run_verify(
        tmp_path, script,
        ["--status", "completed", "--source", "world", "--outcome", "routine"],
        accepts_empty=True)

    assert clear_argv, (
        "with the guard disarmed AND barrier 2 held open the clear still did "
        "not fire -- this file's account of do_verify's control flow is wrong, "
        f"and test 3's negative proves less than it claims. stderr: {proc.stderr}")
    assert clear_argv[clear_argv.index("--if-goal") + 1] == "", (
        f"expected the unguarded path to pass an empty --if-goal; got {clear_argv}")
