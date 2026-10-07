""" — the archive-sweep wrappers must REFUSE an argument, never sweep on one.

WHY THIS FILE EXISTS
`pipeline-archive.sh`, `experience-archive.sh` and `aspirations-archive.sh` (plus
`agent-aspirations-archive.sh`, which forwards to the last) each fire ONE
unconditional sweep against the live store. None has a preview mode and none
rejected an argument: the first copied its argument list into an array that nothing
read, the second never parsed anything, the third discarded every unrecognised
token with a bare shift. So `--help` — the standard safe probe — EXECUTED the
sweep, and so did a mistyped flag. The pipeline sweep's prune half permanently
deletes tombstones, which made that a delete behind a request for usage text.

WHY THIS TEST IS HERMETIC, AND IS NOT A ROW IN test_unknown_flag_refusal.py
That file's `_run()` executes the REAL wrapper as a subprocess with only
STORAGE_BACKEND pinned: there is no daemon isolation. A row there is safe only
because its reverted path is harmless — a deliberately nonexistent record id, or
a trailing `--dry-run` (its docstring, point 3). These wrappers have neither: no
id to make bogus and no preview flag. A row for them would run a LIVE sweep on the
first red run, on any TDD red run, and on any future regression of the guard — a
regression test that must corrupt the store in order to detect corruption.

So every case here runs a COPY of the wrapper inside a throwaway project root
whose `_runtime.sh` is a stub: `rt_call` appends its argv to a log file and
returns a canned body, and nothing else exists. A wrapper resolves its project root
from its own path, so the copy sources the stub and never the real runtime. A
reverted guard fails these tests by the log being non-empty, and harms nothing.

THE POSITIVE CONTROL
`calls == []` is only evidence when the stub CAN record a call. The bare-call
test is that control: same sandbox, no argument, and it must log exactly one POST
and print the documented output. Delete it and every "sweeps nothing" assertion
below turns vacuous.

WHY rc == 2 AND NOT `rc != 0`
`_argv_strict.sh` states it outright: the daemon transport path also exits
non-zero, so an `rc != 0` assertion stays green with the guard reverted. Every
refusal below pins rc == 2 exactly.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

# conftest.py already puts core/scripts/ on sys.path for collected tests; this
# insert matches test_unknown_flag_refusal.py so the file also imports when run
# directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

WRAPPERS = [
    "pipeline-archive.sh",
    "experience-archive.sh",
    "aspirations-archive.sh",
]
# Copied into the sandbox as well, but not parametrised on its own: it is a
# one-line forwarder, covered transitively (test_agent_wrapper_is_fixed_transitively).
FORWARDER = "agent-aspirations-archive.sh"

# What a bare call must do, per wrapper: (POST path, trailing rt_call args as the
# stub logs them, expected stdout, a fragment the stub-fed run must put on stderr).
BARE = {
    "pipeline-archive.sh": (
        "/v1/pipeline/archive-sweep",
        "",
        "7\n",
        "[pipeline-archive] pruned_count=2 stamped_count=1",
    ),
    "experience-archive.sh": (
        "/v1/experience/archive-sweep",
        "",
        "4\n",
        None,
    ),
    "aspirations-archive.sh": (
        "/v1/aspirations/archive-sweep",
        " --query source=world",
        "5\n",
        "WARNING: stub-warning",
    ),
}

# Stand-in for core/scripts/_runtime.sh. It defines exactly what the three wrappers
# call and nothing else, so a wrapper that reaches for anything more fails loudly
# here instead of silently touching a real daemon.
_STUB_RUNTIME = """#!/usr/bin/env bash
rt_call() {
    printf '%s\\n' "$*" >> "$ARCHIVE_STUB_MARKER"
    case "$2" in
        /v1/pipeline/archive-sweep)
            printf '%s' '{"archived_count":7,"pruned_count":2,"stamped_count":1}';;
        /v1/experience/archive-sweep)
            printf '%s' '{"archived":4}';;
        /v1/aspirations/archive-sweep)
            printf '%s' '{"archived_count":5,"warnings":["stub-warning"]}';;
        *)
            echo "stub rt_call: unexpected path $2" >&2
            return 2;;
    esac
}
rt_python_launcher() { printf '%s' "$ARCHIVE_STUB_PYTHON"; }
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "stub rt_no_daemon_error: $1" >&2; exit 1; }
"""


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway project root: wrapper copies + the real _argv_strict.sh + the stub."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in (*WRAPPERS, FORWARDER, "_argv_strict.sh"):
        shutil.copy(SCRIPTS / name, scripts / name)
    # newline="\n": a CRLF stub would break `set -e` bash on a Windows checkout.
    with open(scripts / "_runtime.sh", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_STUB_RUNTIME)
    return scripts, tmp_path / "rt_call.log"


def _run(sandbox, wrapper, *argv):
    scripts, marker = sandbox
    env = dict(os.environ)
    # Belt to the stub's braces: nothing here can reach a backend, but guard-955 /
    # rb-2983 say a test subprocess must never inherit an own-cloud backend.
    env["STORAGE_BACKEND"] = "local"
    env["ARCHIVE_STUB_MARKER"] = marker.as_posix()
    env["ARCHIVE_STUB_PYTHON"] = Path(sys.executable).as_posix()
    r = subprocess.run(
        # bash_cmd(), never a hand-built argv: guards 580 and 581 (see
        # test_unknown_flag_refusal.py::_run for the reasoning).
        bash_cmd(scripts / wrapper, *argv),
        capture_output=True,
        text=True,
        input="",
        env=env,
        cwd=str(scripts.parent.parent),
        timeout=60,
    )
    calls = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    return r, calls


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_bare_call_still_sweeps_and_keeps_its_output_contract(sandbox, wrapper):
    """The POSITIVE CONTROL for every `calls == []` below, and the unchanged contract.

    Callers parse stdout as a bare integer, so nothing else may ride on it.
    """
    path, query, stdout, stderr_fragment = BARE[wrapper]
    r, calls = _run(sandbox, wrapper)
    assert r.returncode == 0, f"{wrapper} rc={r.returncode}\nstderr={r.stderr!r}"
    assert calls == [f"POST {path}{query}"], (
        f"{wrapper} must issue exactly one sweep request; the stub logged {calls!r}"
    )
    assert r.stdout == stdout, f"stdout must stay a bare integer, got {r.stdout!r}"
    if stderr_fragment:
        assert stderr_fragment in r.stderr, r.stderr


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_help_exits_0_and_sweeps_nothing(sandbox, wrapper, flag):
    r, calls = _run(sandbox, wrapper, flag)
    assert r.returncode == 0, f"{wrapper} {flag} rc={r.returncode}\nstderr={r.stderr!r}"
    assert calls == [], f"{wrapper} {flag} ISSUED A SWEEP: {calls!r}"
    assert f"Usage: {wrapper}" in r.stdout, r.stdout
    # The one fact a reader of --help must walk away with: the bare form mutates.
    assert "MUTATES" in r.stdout, r.stdout


@pytest.mark.parametrize("bad", ["--bogus-flag", "--hlep", "-help", "-x"])
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_unknown_flag_is_refused_with_rc_2_and_sweeps_nothing(sandbox, wrapper, bad):
    r, calls = _run(sandbox, wrapper, bad)
    assert r.returncode == 2, (
        f"{wrapper} {bad} returned {r.returncode}, expected exactly 2.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert calls == [], f"{wrapper} {bad} ISSUED A SWEEP: {calls!r}"
    assert "unknown option" in r.stderr and bad in r.stderr, r.stderr
    assert r.stdout == "", "a refusal must put nothing on stdout"


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_stray_positional_is_refused_with_rc_2_and_sweeps_nothing(sandbox, wrapper):
    r, calls = _run(sandbox, wrapper, "stray")
    assert r.returncode == 2, (
        f"{wrapper} returned {r.returncode}, expected exactly 2.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert calls == [], f"{wrapper} stray positional ISSUED A SWEEP: {calls!r}"
    assert "unexpected extra argument 'stray'" in r.stderr, r.stderr


def test_aspirations_source_flag_is_still_honoured(sandbox):
    r, calls = _run(sandbox, "aspirations-archive.sh", "--source", "agent")
    assert r.returncode == 0, r.stderr
    assert calls == ["POST /v1/aspirations/archive-sweep --query source=agent"]


@pytest.mark.parametrize(
    "argv,rc",
    [
        (["--source", "agent", "--help"], 0),
        (["--source", "agent", "--bogus-flag"], 2),
        (["--source", "agent", "stray"], 2),
    ],
    ids=["help-after-source", "unknown-after-source", "positional-after-source"],
)
def test_aspirations_guard_still_fires_after_an_accepted_flag(sandbox, argv, rc):
    r, calls = _run(sandbox, "aspirations-archive.sh", *argv)
    assert r.returncode == rc, f"rc={r.returncode}\nstderr={r.stderr!r}"
    assert calls == [], f"ISSUED A SWEEP: {calls!r}"


def test_agent_wrapper_is_fixed_transitively(sandbox):
    """The forwarder has no parser of its own: it inherits the guard or has none."""
    r, calls = _run(sandbox, FORWARDER)
    assert r.returncode == 0, r.stderr
    assert calls == ["POST /v1/aspirations/archive-sweep --query source=agent"], calls
    for argv, rc in ((["--help"], 0), (["--bogus-flag"], 2), (["stray"], 2)):
        _, marker = sandbox
        # Fresh log each pass: the bare call above recorded one POST, and a refusal
        # that records nothing leaves no file for the next pass to remove.
        marker.unlink(missing_ok=True)
        r, calls = _run(sandbox, FORWARDER, *argv)
        assert r.returncode == rc, f"{argv}: rc={r.returncode}\nstderr={r.stderr!r}"
        assert calls == [], f"{FORWARDER} {argv} ISSUED A SWEEP: {calls!r}"


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_guard_is_parsed_before_the_runtime_is_sourced(wrapper):
    """No daemon client exists yet when the refusal fires, so no request can be sent.

    Stronger than the stub's empty log: a guard placed AFTER the runtime source
    but before the first call would also leave the log empty, yet could be masked
    by a runtime failure — the ordering is what makes the refusal unconditional.
    """
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    # find(), not index(): a wrapper with no guard at all must fail with this
    # message (offset -1), not with a bare ValueError that names no wrapper.
    guard = text.find('source "$CORE_ROOT/scripts/_argv_strict.sh"')
    runtime = text.find('source "$CORE_ROOT/scripts/_runtime.sh"')
    first_call = text.find("rt_call POST")
    assert 0 <= guard < runtime < first_call, (
        f"{wrapper}: the strict-argv guard must be sourced and run BEFORE "
        f"_runtime.sh is sourced and before the first rt_call "
        f"(offsets guard={guard} runtime={runtime} call={first_call}; -1 = absent)"
    )


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_dead_argv_copy_is_not_declared(wrapper):
    """The write-only array that let every argument fall through is gone.

    Pins the DECLARATION, not the word: "dead" is a property of a reader's absence,
    not of a name — another wrapper's identically named array is live.
    """
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    assert not re.search(r"^\s*declare\s+-a\s+PASSTHROUGH\w*", text, re.MULTILINE), (
        f"{wrapper} declares a PASSTHROUGH array again; it has no reader here"
    )
