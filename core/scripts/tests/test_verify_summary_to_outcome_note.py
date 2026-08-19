"""Does `--summary` reach the GOAL RECORD, and does prose survive the shell?

EXECUTED (g-115-5157 producer + g-115-4208 input path). Two goals, one function,
deliberately one file: g-115-5157's outcome 3 requires the g-115-4208
shell-evaluation hazard to be "resolved or explicitly sequenced before this
lands", because wiring `--summary` into `outcome_note` while the prose can still
be silently holed by the shell would make a transient corruption PERMANENT in
the queryable record. Landing them together is the sanctioned order.

WHAT WAS BROKEN, measured 2026-08-08 (alpha, hostname cc-07, uname -r
6.8.0-136-generic, Linux, own-cloud):

  - `do_verify` wrote `--summary` to the execution diary (per-agent, per-box) and
    a board post (chronological, ages out of every `--since` window) and NEVER to
    the goal record -- the durable, shared, queryable artifact. 185 of 644 goals
    completed 08-01..08-06 (29%) carried an `outcome_note`, because writing one
    was an act of REMEMBERING rather than a byproduct of the closure path.

  - `--summary` took an INLINE string only. Every real caller passes a
    multi-paragraph verify narrative as a double-quoted shell argument, so any
    backtick, `$(...)` or bare `$` in the prose is expanded BEFORE the script
    runs. The write then succeeds at rc=0 with a hole in prose nobody re-reads.
    `test_summary_file_survives_shell_metacharacters_verbatim` and its
    `..._inline_control` are the two halves of that measurement: same bytes, two
    input paths, opposite fidelity.

THE INLINE CONTROL IS THE LOAD-BEARING TEST IN THIS FILE, not the round-trip.
A round-trip alone shows `--summary-file` works; it cannot show the flag was
NEEDED. The control runs the identical content through a real `/bin/sh` command
line -- the production caller shape -- and asserts the prose arrives CORRUPTED.
Without it, someone could delete `--summary-file` and every other test here
would stay green (guard-1829: a negative that never reaches the hazard proves
nothing).

WHY `--summary-file` AND NOT "quote it better": the failure moment is composing
a long argument, which is exactly when an author is thinking about content
rather than quoting. A guardrail for this already existed and did not prevent
three subsequent recurrences, which is the rb-840 signal -- the answer is
WIRING, not a fourth encoding.

SCOPE THIS FILE DOES NOT COVER, named rather than left to be discovered
(guard-1462 -- the fixture seam is a silent scope declaration):
  - `do_state_update`'s metric-gate fallback (the CONSUMER half). It lives in a
    different phase with a much larger collaborator surface; it is covered by a
    source pin here and by a live end-to-end measurement recorded in the goal's
    outcome_note, NOT by execution in this file.
  - The daemon's actual persistence of `outcome_note`. The update collaborator
    is stubbed, so what is proven is the ARGV `do_verify` emits, not that the
    daemon stores it. That boundary is deliberate and is where the sibling
    file's hermeticity ends too.

Hermetic by `test_verify_phase_empty_goal_id.py`'s pattern: stage a COPY of the
real `iteration-close.sh` beside stub `_paths.sh`/`_platform.sh`, so the copy
resolves PROJECT_ROOT/WORLD_DIR/AGENT_DIR entirely inside tmp and every
collaborator it shells out to is a stub recording its argv. Real bytes of the
script under test, no daemon, no live world, no network.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
guard-955: no live store is reachable from here, but run with the pin anyway.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_verify_summary_to_outcome_note.py -q
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

# Prose containing every expansion class the inline path mangles. Written to
# disk by Python (never echoed through a shell) so the FIXTURE itself cannot
# corrupt the bytes it is measuring.
HAZARD_PROSE = (
    "Measured 3 distinct findings.\n"
    "The fix was `git merge-base --is-ancestor $SHA HEAD`.\n"
    "Ratio $(passed/total) held at 100%; cost $5 per run.\n"
)

STUB_PATHS = """
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"
WORLD_DIR="$PROJECT_ROOT/world"
META_DIR="$PROJECT_ROOT/meta"
AGENT_DIR="$PROJECT_ROOT/agents/alpha"
export PATH="$PROJECT_ROOT/shim:$PATH"
agent_dir() { printf '%s' "$PROJECT_ROOT/agents/$1"; }
"""

# Records argv one entry per line. NUL-free content only, which HAZARD_PROSE is.
STUB_UPDATE = """#!/usr/bin/env bash
{ for a in "$@"; do printf '%s\\n' "---ARG---"; printf '%s\\n' "$a"; done; } >> "$UPDATE_SINK"
exit 0
"""

# The record read behind _probe_goal_outcome_note. EXISTING_NOTE controls whether
# the goal already carries a note, which is the never-clobber branch selector.
STUB_QUERY = """#!/usr/bin/env bash
printf '[{"id":"%s","goal_id":"%s","outcome_note":%s}]\\n' \\
    "$QGID" "$QGID" "$(printf '%s' "${EXISTING_NOTE:-}" | "$PY_REAL" -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))')"
exit 0
"""

STUB_OK = """#!/usr/bin/env bash
cat >/dev/null 2>&1 || true
exit 0
"""

PY_SHIM = """#!/usr/bin/env bash
exec "$PY_REAL" "$@"
"""

PASSTHROUGH_STUBS = (
    "pending-deploys-gate.sh",
    "loop-state-save.sh",
    "aspirations-complete-by.sh",
    "execution-diary.sh",
    "board-post.sh",
    "close-defer-invalidation.py",
    "team-state-clear-in-flight.sh",
)


def _stage(tmp_path):
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)

    (core / "iteration-close.sh").write_text(
        ITERATION_CLOSE.read_text(encoding="utf-8"), encoding="utf-8")
    (core / "_paths.sh").write_text(STUB_PATHS, encoding="utf-8")
    (core / "_platform.sh").write_text("", encoding="utf-8")
    # Real bytes: self-contained arg normalizer that runs before the strict
    # parse loop. Stubbing it would replace production arg handling with a guess.
    (core / "_goal-arg-normalize.sh").write_text(
        (SCRIPTS / "_goal-arg-normalize.sh").read_text(encoding="utf-8"),
        encoding="utf-8")
    # Real bytes, same reason as the normalizer above:  moved the
    # never-clobber probe+write OUT of iteration-close.sh and into this shared
    # helper so a worker Body can call the same component (guard-2676). It IS
    # the production behaviour these tests assert on — stubbing it would replace
    # the thing under test with a guess and every assertion below would pass
    # against the stub.
    (core / "closure-evidence-write.sh").write_text(
        (SCRIPTS / "closure-evidence-write.sh").read_text(encoding="utf-8"),
        encoding="utf-8")
    (core / "aspirations-update-goal.sh").write_text(STUB_UPDATE, encoding="utf-8")
    (core / "aspirations-query.sh").write_text(STUB_QUERY, encoding="utf-8")
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

    row = {"id": "asp-777",
           "goals": [{"id": GID, "status": "completed", "recurring": False}]}
    (tmp_path / "world" / "aspirations.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")
    return core / "iteration-close.sh"


def _env(tmp_path, existing_note=""):
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "MIND_AGENT": AGENT,
        "PY_REAL": sys.executable,
        "UPDATE_SINK": str(tmp_path / "update-argv.txt"),
        "QGID": GID,
        "EXISTING_NOTE": existing_note,
    }


def _run(tmp_path, script, args, existing_note=""):
    proc = subprocess.run(
        [BASH, Path(script).as_posix(), "--phase", "verify"] + args,
        capture_output=True, text=True, env=_env(tmp_path, existing_note),
        timeout=120)
    return proc, _writes(tmp_path)


def _writes(tmp_path):
    """Every aspirations-update-goal.sh invocation, as a list of argv lists.

    Split on the ---ARG--- sentinel rather than newlines: a multi-paragraph
    summary contains newlines, and a line-based parse would shred one argv entry
    into many and silently destroy the very fidelity this file measures.
    """
    sink = tmp_path / "update-argv.txt"
    if not sink.exists():
        return []
    raw = sink.read_text(encoding="utf-8")
    args = [a[:-1] if a.endswith("\n") else a
            for a in raw.split("---ARG---\n")[1:]]
    calls, cur = [], []
    for a in args:
        if a == "--source" and cur:
            calls.append(cur)
            cur = []
        cur.append(a)
    if cur:
        calls.append(cur)
    return calls


def _note_writes(calls):
    return [c for c in calls if "outcome_note" in c]


# ── 1. positive control: the harness reaches the record write ───────────────

def test_summary_lands_on_the_goal_record(tmp_path):
    """ outcome 1. A verify close passing --summary results in a
    non-empty outcome_note on the goal record.

    Positive control for every negative below: they all assert a write did NOT
    happen, and each would pass for free if the harness never reached the
    decision point (guard-1829).
    """
    script = _stage(tmp_path)
    proc, calls = _run(tmp_path, script,
                       ["--goal", GID, "--status", "completed", "--source",
                        "world", "--outcome", "routine",
                        "--summary", "verified: 3 findings, all reproduced"])

    assert proc.returncode == 0, f"close failed: {proc.stderr}"
    notes = _note_writes(calls)
    assert len(notes) == 1, (
        "expected exactly one outcome_note write; got "
        f"{len(notes)}. all calls: {calls}. stderr: {proc.stderr}")
    argv = notes[0]
    assert argv[argv.index("outcome_note") + 1] == \
        "verified: 3 findings, all reproduced", (
        f"outcome_note carried the wrong value: {argv}")
    assert GID in argv, f"the write targeted the wrong goal: {argv}"


# ── 2. THE LOAD-BEARING NEGATIVE (guard-1423) ───────────────────────────────

def test_no_summary_still_closes_and_writes_no_note(tmp_path):
    """ outcome 2. A verify close passing NO --summary succeeds and
    does not block.

    This is the guard-1423 negative the goal names explicitly: arming a
    consumer must never make the un-armed path fail. A caller with nothing to
    say must still be able to close.
    """
    script = _stage(tmp_path)
    proc, calls = _run(tmp_path, script,
                       ["--goal", GID, "--status", "completed", "--source",
                        "world", "--outcome", "routine"])

    assert proc.returncode == 0, (
        "a close without --summary was refused -- the new write is not "
        f"guarded on non-empty SUMMARY. stderr: {proc.stderr}")
    assert _note_writes(calls) == [], (
        f"an outcome_note was written with no --summary supplied: {calls}")
    # Discriminator: the close really ran rather than exiting early, so the
    # negative above is not vacuous.
    assert any("status" in c for c in calls), (
        f"the close never reached its status write; negative is vacuous: {calls}")


# ── 3. never clobber ────────────────────────────────────────────────────────

def test_existing_note_is_never_overwritten(tmp_path):
    """An agent-authored note wins over the verify summary, and the skip is
    announced.

    The 29% who write a note write it BEFORE calling verify, and theirs is the
    richer artifact. aspirations-update-goal.sh has no append mode, so the write
    is an overwrite -- clobbering here would be a worse defect than the one
    being fixed.
    """
    script = _stage(tmp_path)
    proc, calls = _run(tmp_path, script,
                       ["--goal", GID, "--status", "completed", "--source",
                        "world", "--outcome", "routine",
                        "--summary", "short verify line"],
                       existing_note="a long hand-authored note with evidence")

    assert proc.returncode == 0, f"close failed: {proc.stderr}"
    assert _note_writes(calls) == [], (
        f"verify overwrote an existing outcome_note: {calls}")
    assert "already present" in proc.stderr, (
        "the skip was silent -- a reader cannot tell the summary was dropped "
        f"from the record. stderr: {proc.stderr!r}")


def test_the_clobber_guard_is_reachable_both_ways(tmp_path):
    """Discriminator for test 3 (guard-1220): the SAME invocation writes when no
    note exists and declines when one does.

    Without this pairing, test 3 passes for any reason that suppresses the write
    -- including the feature never working at all.
    """
    script = _stage(tmp_path)
    args = ["--goal", GID, "--status", "completed", "--source", "world",
            "--outcome", "routine", "--summary", "identical summary text"]

    _, absent = _run(tmp_path, script, args, existing_note="")
    (tmp_path / "update-argv.txt").unlink()
    _, present = _run(tmp_path, script, args, existing_note="pre-existing")

    assert len(_note_writes(absent)) == 1, (
        f"no write on the empty-note path: {absent}")
    assert _note_writes(present) == [], (
        f"wrote anyway on the populated-note path: {present}")


# ── 4. : the shell-evaluation hazard, both halves ─────────────────

def test_summary_file_survives_shell_metacharacters_verbatim(tmp_path):
    """. Backticks, $(...) and a bare $ round-trip byte-identical.

    Compared against the file's own bytes, not against a re-typed literal, so
    the assertion cannot drift from the fixture.
    """
    script = _stage(tmp_path)
    sf = tmp_path / "summary.txt"
    sf.write_text(HAZARD_PROSE, encoding="utf-8")

    proc, calls = _run(tmp_path, script,
                       ["--goal", GID, "--status", "completed", "--source",
                        "world", "--outcome", "routine",
                        "--summary-file", str(sf)])

    assert proc.returncode == 0, f"close failed: {proc.stderr}"
    notes = _note_writes(calls)
    assert len(notes) == 1, f"expected one outcome_note write: {calls}"
    argv = notes[0]
    got = argv[argv.index("outcome_note") + 1]
    # $(<file) strips trailing newlines; nothing else may differ.
    assert got == HAZARD_PROSE.rstrip("\n"), (
        "prose was altered on the --summary-file path.\n"
        f"  expected: {HAZARD_PROSE.rstrip(chr(10))!r}\n"
        f"  got:      {got!r}")
    for token in ("`git merge-base", "$SHA", "$(passed/total)", "$5"):
        assert token in got, f"{token!r} did not survive: {got!r}"


def test_inline_summary_is_mangled_by_the_shell_inline_control(tmp_path):
    """THE CONTROL THAT MAKES THE FLAG NECESSARY, not merely correct.

    Identical bytes, but composed as a double-quoted argument on a real /bin/sh
    command line -- the production caller shape. The shell expands them before
    iteration-close.sh ever runs, so the prose arrives holed at rc=0.

    If this test ever FAILS (inline survives intact), the hazard is gone and
    --summary-file's justification needs re-deriving rather than assuming.
    shell=True is deliberate here and is the whole point; it is the only way to
    reproduce the production composition path, which subprocess's list argv --
    correctly -- never exercises.
    """
    script = _stage(tmp_path)
    env = _env(tmp_path)

    cmd = (f'{BASH} {Path(script).as_posix()} --phase verify --goal {GID} '
           f'--status completed --source world --outcome routine '
           f'--summary "{HAZARD_PROSE}"')
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          env=env, timeout=120)

    calls = _writes(tmp_path)
    notes = _note_writes(calls)
    assert notes, (
        "the inline run wrote no outcome_note at all, so this control observes "
        f"nothing. rc={proc.returncode} stderr: {proc.stderr!r}")
    got = notes[0][notes[0].index("outcome_note") + 1]
    assert got != HAZARD_PROSE.rstrip("\n"), (
        "inline --summary round-tripped INTACT through a real shell. The "
        "expansion hazard g-115-4208 documents is not reproducing here, so "
        "--summary-file's justification must be re-measured, not assumed. "
        f"got: {got!r}")
    assert "$(passed/total)" not in got or "`git merge-base" not in got, (
        f"expected at least one expansion class to be consumed; got {got!r}")


def test_summary_and_summary_file_are_mutually_exclusive(tmp_path):
    """. Passing both is refused with a clear message.

    Refused rather than precedence-ordered: either choice silently discards
    prose the caller believed it had supplied.
    """
    script = _stage(tmp_path)
    sf = tmp_path / "summary.txt"
    sf.write_text("from file", encoding="utf-8")

    proc, calls = _run(tmp_path, script,
                       ["--goal", GID, "--status", "completed", "--source",
                        "world", "--outcome", "routine",
                        "--summary", "inline", "--summary-file", str(sf)])

    assert proc.returncode == 2, (
        f"expected exit 2 for both flags; got {proc.returncode}: {proc.stderr}")
    assert "mutually exclusive" in proc.stderr, (
        f"the refusal did not explain itself: {proc.stderr!r}")
    assert calls == [], (
        f"state was written despite refusing the call: {calls}")


def test_summary_file_missing_and_empty_are_refused(tmp_path):
    """A path that does not exist, and one that is empty, both refuse loudly.

    An empty file is refused rather than treated as "no summary": the caller
    passed the flag, so silence would be a lost narrative, not an omission.
    """
    script = _stage(tmp_path)
    base = ["--goal", GID, "--status", "completed", "--source", "world",
            "--outcome", "routine"]

    proc, _ = _run(tmp_path, script,
                   base + ["--summary-file", str(tmp_path / "nope.txt")])
    assert proc.returncode == 2 and "not found" in proc.stderr, (
        f"missing file not refused: rc={proc.returncode} {proc.stderr!r}")

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    proc, _ = _run(tmp_path, script, base + ["--summary-file", str(empty)])
    assert proc.returncode == 2 and "is empty" in proc.stderr, (
        f"empty file not refused: rc={proc.returncode} {proc.stderr!r}")


# ── 5. source pin for the CONSUMER half (see module docstring scope note) ───

def test_metric_gate_falls_back_to_the_record(tmp_path):
    """SOURCE PIN, not an execution test -- and labelled so it is never mistaken
    for one.

    do_state_update's metric gate was piped `${SUMMARY:-}`, which is empty on
    every normal closure because state-update is a separate invocation and no
    call site passes --summary to it. Fixing the producer alone would leave that
    gate exactly as starved, so the fallback is what makes this goal deliver.

    Pinned at source because the phase's collaborator surface is far larger than
    this harness stages; the live end-to-end measurement is recorded in the
    goal's outcome_note instead.
    """
    src = ITERATION_CLOSE.read_text(encoding="utf-8")
    assert "_metric_input" in src, (
        "the metric gate no longer routes through _metric_input -- the "
        "outcome_note fallback was removed or renamed")
    assert '_metric_input="$(_probe_goal_outcome_note)"' in src, (
        "the metric gate's empty-SUMMARY fallback no longer reads the goal "
        "record; the gate is starved again on the loop path")
    # The fallback is worthless if the producer stops writing the field it reads.
    #
    # FOLLOW THE WRITE, DO NOT WEAKEN THE PIN (). The literal
    # `outcome_note "$SUMMARY"` used to sit in iteration-close.sh; it now lives
    # in closure-evidence-write.sh, because a WORKER Body skips do_verify
    # entirely and had no producer at all, so the write had to become a shared
    # component both orchestrators call (guard-2676). That is a refactor, not a
    # regression -- but deleting this assertion would retire the only thing
    # tying producer to consumer, so it is re-pointed at BOTH halves instead:
    # iteration-close must still route to the helper, and the helper must still
    # perform the write. Either half alone can be true while the field goes
    # unpopulated.
    assert "closure-evidence-write.sh" in src, (
        "do_verify no longer routes to closure-evidence-write.sh, so nothing on "
        "the reducer close path writes SUMMARY to outcome_note and the consumer "
        "fallback above reads a field nothing populates")
    helper_src = (SCRIPTS / "closure-evidence-write.sh").read_text(encoding="utf-8")
    assert 'outcome_note "$SUMMARY"' in helper_src, (
        "closure-evidence-write.sh no longer writes SUMMARY to outcome_note -- "
        "the shared producer stopped populating the field the metric gate reads")
