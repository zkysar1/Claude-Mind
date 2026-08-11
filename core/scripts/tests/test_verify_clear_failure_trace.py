"""A FAILING in_flight clear leaves a durable, cross-box-readable trace ().

`iteration-close.sh` do_verify ends its close by clearing the agent's team-state
`in_flight` row. That call was fail-open with a stderr-only WARN:

    bash .../team-state-clear-in-flight.sh --agent "$AGENT" --if-goal "$GOAL_ID" \\
        || echo "[iteration-close] WARN: ..." >&2

Two measured facts make that WARN worthless as evidence:

  1. `team-state-clear-in-flight.sh` prints NOTHING on its failure paths (rc=2 ->
     bare `exit 1`; any other rc -> bare `exit $rc`). So the rc is the only
     reliable signal, and an EMPTY capture is the NORMAL failure shape.
  2. guard-772: a fail-open WARN written only to stderr is invisible when the
     command runs inside a backgrounded Bash subprocess, which iteration-close
     does whenever it exceeds the 2-minute tool timeout. The harness bg task file
     does not capture a nested process's stderr — so the WARN was unreadable even
     on the CLOSING agent's own box, not merely from other boxes.

g-306-219 left a stale `in_flight` row it could not explain precisely because of
this: the 03:12:17 clear either never ran or ran and failed into a WARN nobody
could read. The fix appends the failure to the execution diary, which is
`sync_tier: continuity` and therefore readable by a partner.

WHAT THIS FILE PROVES, AND WHY IT IS EXECUTED RATHER THAN READ. The goal's
verification demands the trace be "proven to fire by inducing a real clear
failure, not by reading the code path" (guard-1943: a green suite certifies the
function, never the wiring). So every test here RUNS `--phase verify` against a
staged copy of the real script whose clear stub genuinely exits nonzero.

The guard-772 read-back is covered in BOTH directions on purpose. A diagnostic
added to end a silent failure becomes the next silent layer unless its own
failure modes are loud (guard-1977) — so `test_readback_warns_when_the_append_
silently_drops` proves the read-back fires when the append is lost, and
`test_readback_is_silent_when_the_append_lands` is its discriminator: without it
the first would also pass if the warning fired unconditionally.

Hermetic by the sibling files' pattern (`test_verify_phase_empty_goal_id.py`,
`test_recover_phase_execution.py`): stage a COPY of the real `iteration-close.sh`
beside stub `_paths.sh`/`_platform.sh`, so the copy resolves PROJECT_ROOT /
WORLD_DIR / AGENT_DIR entirely inside tmp and every collaborator is a stub that
records what it was handed. Real bytes of the script under test, no daemon, no
live world, no network.

guard-1165: no module-level os.environ mutation, no sys.modules stubs.
Run: STORAGE_BACKEND=local python -m pytest \
     core/scripts/tests/test_verify_clear_failure_trace.py -q
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

# The marker the durable trace carries. Production greps for this same string in
# its read-back, so a rename here without a rename there silently disarms the
# verification — pinned by test_the_readback_greps_the_string_the_append_writes.
TRACE_MARKER = "team-state-clear-in-flight FAILED"

# First line of the diary-append block, and the last. The mutation control
# deletes exactly this range to reproduce the pre- script (WARN only,
# no durable trace). Anchored on the assignment prefix because the closing line
# is byte-identical to do_verify's OTHER diary append ~100 lines above — an
# anchor on the closing line alone would mutate the wrong block.
APPEND_BLOCK_START = 'AG="$AGENT" GID="$GOAL_ID" RC="$clear_rc" ERR="$clear_out"'
APPEND_BLOCK_END = "' | bash \"$SCRIPT_DIR/execution-diary.sh\" append || true"

STUB_PATHS = """
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"
WORLD_DIR="$PROJECT_ROOT/world"
META_DIR="$PROJECT_ROOT/meta"
AGENT_DIR="$PROJECT_ROOT/agents/alpha"
export PATH="$PROJECT_ROOT/shim:$PATH"
agent_dir() { printf '%s' "$PROJECT_ROOT/agents/$1"; }
"""

# The clear stub. CLEAR_RC and CLEAR_STDOUT/CLEAR_STDERR travel by env so no test
# data is interpolated into shell source (guard-165). Default rc=0 keeps the
# success path available as the positive control.
STUB_CLEAR = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$CLEAR_SINK"
[ -n "${CLEAR_STDOUT:-}" ] && printf '%s\\n' "$CLEAR_STDOUT"
[ -n "${CLEAR_STDERR:-}" ] && printf '%s\\n' "$CLEAR_STDERR" >&2
exit "${CLEAR_RC:-0}"
"""

# The diary stub serves BOTH subcommands the fix uses, because the read-back is
# part of the behaviour under test: `append` consumes stdin into the sink,
# `read` replays the sink. DIARY_APPEND_RC=1 models an append that FAILS — which
# production swallows with `|| true`, so nothing lands and only the read-back can
# notice. That is the guard-772 case.
STUB_DIARY = """#!/usr/bin/env bash
case "${1:-}" in
    append)
        if [ "${DIARY_APPEND_RC:-0}" != "0" ]; then
            cat >/dev/null 2>&1 || true
            exit "$DIARY_APPEND_RC"
        fi
        cat >> "$DIARY_SINK"
        printf '\\n' >> "$DIARY_SINK"
        exit 0
        ;;
    read)
        cat "$DIARY_SINK" 2>/dev/null || true
        exit 0
        ;;
esac
cat >/dev/null 2>&1 || true
exit 0
"""

STUB_UPDATE = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$UPDATE_SINK"
exit 0
"""

STUB_OK = """#!/usr/bin/env bash
cat >/dev/null 2>&1 || true
exit 0
"""

PY_SHIM = """#!/usr/bin/env bash
exec "$PY_REAL" "$@"
"""

# Collaborators do_verify shells out to on the completed/world path. execution-
# diary.sh is deliberately NOT in this list — it gets the recording stub above.
PASSTHROUGH_STUBS = (
    "pending-deploys-gate.sh",
    "loop-state-save.sh",
    "aspirations-complete-by.sh",
    "board-post.sh",
    "close-defer-invalidation.py",
)


def _strip_append_block(src):
    """Reproduce the pre- script: WARN kept, durable trace removed.

    guard-2312 (existence is not uniqueness): refuse loudly unless the anchor
    matches exactly one site, so a refactor that reworded it fails here instead
    of silently disarming the control.
    """
    lines = src.splitlines(True)
    starts = [i for i, ln in enumerate(lines)
              if ln.strip().startswith(APPEND_BLOCK_START)]
    assert len(starts) == 1, (
        "expected exactly one diary-append block to remove; found "
        f"{len(starts)}. The block was reworded — update APPEND_BLOCK_START, "
        "do not weaken this assertion.")
    start = starts[0]
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].rstrip("\n") == APPEND_BLOCK_END)
    return "".join(lines[:start] + lines[end + 1:])


def _stage(tmp_path, strip_append=False):
    """Copy the REAL iteration-close.sh into a tmp project root beside stubs."""
    core = tmp_path / "core" / "scripts"
    core.mkdir(parents=True)

    src = ITERATION_CLOSE.read_text(encoding="utf-8")
    if strip_append:
        src = _strip_append_block(src)
    (core / "iteration-close.sh").write_text(src, encoding="utf-8")

    (core / "_paths.sh").write_text(STUB_PATHS, encoding="utf-8")
    (core / "_platform.sh").write_text("", encoding="utf-8")
    # Real bytes, not a stub: self-contained arg normalizer. Stubbing it would
    # replace production arg handling with a guess in a test that claims to
    # execute the production path.
    (core / "_goal-arg-normalize.sh").write_text(
        (SCRIPTS / "_goal-arg-normalize.sh").read_text(encoding="utf-8"),
        encoding="utf-8")
    (core / "team-state-clear-in-flight.sh").write_text(STUB_CLEAR, encoding="utf-8")
    (core / "execution-diary.sh").write_text(STUB_DIARY, encoding="utf-8")
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


def _write_aspirations(tmp_path, goal_id, status):
    row = {"id": "asp-777",
           "goals": [{"id": goal_id, "status": status, "recurring": False}]}
    (tmp_path / "world" / "aspirations.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")


def _run_verify(tmp_path, script, clear_rc=0, clear_stdout="",
                clear_stderr="", diary_append_rc=0):
    diary_sink = tmp_path / "diary.jsonl"
    diary_sink.write_text("", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "MIND_AGENT": AGENT,
        "PY_REAL": sys.executable,
        "CLEAR_SINK": str(tmp_path / "clear-argv.txt"),
        "UPDATE_SINK": str(tmp_path / "update-argv.txt"),
        "DIARY_SINK": str(diary_sink),
        "CLEAR_RC": str(clear_rc),
        "CLEAR_STDOUT": clear_stdout,
        "CLEAR_STDERR": clear_stderr,
        "DIARY_APPEND_RC": str(diary_append_rc),
    }
    # guard-580/581: never a bare "bash" argv[0], never str(WindowsPath).
    proc = subprocess.run(
        [BASH, Path(script).as_posix(), "--phase", "verify",
         "--goal", GID, "--status", "completed", "--source", "world",
         "--outcome", "routine"],
        capture_output=True, text=True, env=env, timeout=120)
    diary = diary_sink.read_text(encoding="utf-8")
    return proc, diary


def _trace_entries(diary):
    """Parse the diary sink into the records carrying the failure marker."""
    out = []
    for line in diary.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if TRACE_MARKER in rec.get("content", ""):
            out.append(rec)
    return out


# ── 1. positive control: an ORDINARY close is unchanged ─────────────────────

def test_a_successful_clear_writes_no_trace_and_still_prints_its_line(tmp_path):
    """Outcome 3: instrumenting this call must not change the success path.

    The clear's success line previously flowed straight to the terminal. Capturing
    its output to read the rc would have swallowed it, so the fix re-emits it —
    this pins that, and pins that no failure entry is written when nothing failed.
    """
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, diary = _run_verify(tmp_path, script, clear_rc=0,
                              clear_stdout="in_flight cleared for alpha")

    assert proc.returncode == 0, proc.stderr
    assert "in_flight cleared for alpha" in proc.stdout, (
        "capturing the clear's output silenced its success line; it no longer "
        f"reaches stdout. stdout was: {proc.stdout!r}")
    assert _trace_entries(diary) == [], (
        f"a failure trace was written for a SUCCESSFUL clear; diary was {diary!r}")
    assert "WARN: team-state-clear-in-flight failed" not in proc.stderr


# ── 2. a REAL clear failure leaves a durable trace ──────────────────────────

def test_a_failing_clear_writes_a_durable_trace_naming_agent_goal_and_rc(tmp_path):
    """The goal's outcome 1, executed: the clear genuinely exits nonzero and the
    diary receives a record a partner can read."""
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, diary = _run_verify(tmp_path, script, clear_rc=2,
                              clear_stderr="daemon refused")

    traces = _trace_entries(diary)
    assert len(traces) == 1, (
        f"expected exactly one failure trace, got {len(traces)}; diary was "
        f"{diary!r}; stderr was {proc.stderr!r}")
    rec = traces[0]
    assert rec["entry_type"] == "failure", (
        f"the trace is not typed as a failure: {rec!r}")
    assert rec["goal_id"] == GID, f"the trace names the wrong goal: {rec!r}"
    assert AGENT in rec["content"], f"the trace does not name the agent: {rec!r}"
    assert "rc=2" in rec["content"], (
        f"the trace lost the exit code — the only reliable failure signal "
        f"this call has: {rec!r}")
    assert "daemon refused" in rec["content"], (
        f"the trace dropped the clear's own stderr: {rec!r}")


def test_the_close_still_succeeds_when_the_clear_fails(tmp_path):
    """Outcome 3, the other half: fail-open is preserved. A team-state blip must
    not abort a close, and the original WARN must still be on stderr."""
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, _ = _run_verify(tmp_path, script, clear_rc=2)

    assert proc.returncode == 0, (
        "a failing in_flight clear now ABORTS the close — the fail-safe "
        f"direction was reversed. stderr: {proc.stderr}")
    assert "WARN: team-state-clear-in-flight failed" in proc.stderr, (
        f"the original WARN was dropped; stderr was {proc.stderr!r}")


def test_the_trace_fires_on_the_silent_failure_shape(tmp_path):
    """The MEASURED real shape: rc=2 -> bare `exit 1`, printing nothing at all.

    An empty capture is what production will almost always see, so the trace has
    to be driven by the rc and must say explicitly that there was no output —
    otherwise a reader cannot tell a silent failure from a lost message.
    """
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, diary = _run_verify(tmp_path, script, clear_rc=1,
                              clear_stdout="", clear_stderr="")

    traces = _trace_entries(diary)
    assert len(traces) == 1, (
        "no trace was written for a clear that failed SILENTLY — the most "
        f"common production shape. diary was {diary!r}, stderr {proc.stderr!r}")
    assert "rc=1" in traces[0]["content"]
    assert "(none" in traces[0]["content"], (
        "the trace does not distinguish 'the clear printed nothing' from "
        f"'the output was lost': {traces[0]!r}")


# ── 3. the read-back, proven in BOTH directions (guard-772 / guard-1977) ────

def test_readback_warns_when_the_append_silently_drops(tmp_path):
    """The fix's own failure mode must be loud.

    The append is `|| true`, so a failed append is swallowed and nothing lands —
    reproducing, one layer down, the exact invisibility this goal removes. The
    read-back exists to catch that, and this proves it does.
    """
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, diary = _run_verify(tmp_path, script, clear_rc=2, diary_append_rc=1)

    assert _trace_entries(diary) == [], (
        "the harness did not actually drop the append, so this test is not "
        f"exercising the case it claims: diary was {diary!r}")
    assert "did NOT land in the execution diary" in proc.stderr, (
        "the durable trace was lost and NOTHING said so — the fix became the "
        f"next silent layer (guard-1977). stderr was: {proc.stderr!r}")
    assert proc.returncode == 0, (
        "the read-back aborted the close; it must warn, never block")


def test_readback_is_silent_when_the_append_lands(tmp_path):
    """Discriminator for the test above (guard-1829).

    Without this, that test would pass just as well if the 'did NOT land'
    warning fired unconditionally — which would make it pure noise and train
    readers to ignore it.
    """
    script = _stage(tmp_path)
    _write_aspirations(tmp_path, GID, "completed")

    proc, diary = _run_verify(tmp_path, script, clear_rc=2, diary_append_rc=0)

    assert len(_trace_entries(diary)) == 1, "the append did not land"
    assert "did NOT land in the execution diary" not in proc.stderr, (
        "the read-back cried wolf on a trace that DID land; the warning is "
        f"unconditional and therefore meaningless. stderr was: {proc.stderr!r}")


def test_the_readback_greps_the_string_the_append_writes():
    """The append and the read-back are coupled by a bare string. A rename on
    either side is silent: the append would land and the read-back would warn
    that it had not, forever. Cheap source pin for a coupling no execution can
    see, because both halves are inside the same branch."""
    src = ITERATION_CLOSE.read_text(encoding="utf-8")
    assert src.count(TRACE_MARKER) == 2, (
        f"expected the marker {TRACE_MARKER!r} exactly twice in "
        "iteration-close.sh (written by the append, matched by the read-back); "
        f"found {src.count(TRACE_MARKER)}. One side was renamed.")


# ── 4. permanent negative control ───────────────────────────────────────────

def test_the_append_block_is_what_writes_the_trace(tmp_path):
    """guard-1829/guard-1856: a mutation proof establishes exactly the
    proposition it mutated, so the mutation must be the fix itself.

    With ONLY the diary-append block removed, the script is the pre-g-306-233
    one: still fail-open, still WARNing to stderr, and leaving no durable
    evidence. If this ever stops reproducing, every test above has stopped
    discriminating and is passing on something other than the fix.
    """
    script = _stage(tmp_path, strip_append=True)
    _write_aspirations(tmp_path, GID, "completed")

    proc, diary = _run_verify(tmp_path, script, clear_rc=2)

    assert proc.returncode == 0, proc.stderr
    assert "WARN: team-state-clear-in-flight failed" in proc.stderr, (
        "the control removed more than the append block — the WARN went too, "
        "so it no longer reproduces the pre-fix script")
    assert _trace_entries(diary) == [], (
        "the pre-fix script wrote a durable trace, which it cannot do; the "
        f"mutation is not removing what this file thinks it is. diary: {diary!r}")
