"""Regression pins for core/scripts/run-full-suite.sh ().

Two independent defects, both of which made a run in which ZERO framework tests
executed look like a passing run:

1. ORDERING. The wrapper called `python3 run-full-suite.py` BEFORE sourcing
   _paths.sh, and _paths.sh is what puts core/scripts/.python-shim on PATH -- on
   Windows that shim IS `python3` (synthesized from `py`/`python` when
   `python3 -c pass` fails). Under any invocation the PreToolUse bash-agent-inject
   hook does not reach (nohup'd / backgrounded), the call died rc=127 and the
   whole framework half was skipped.

2. MISLEADING LOG. The exit code was already honest (127 propagates), but the
   framework error is line 1 of ~15 while the invisible-suite and domain halves
   still run below it and print green. A reader who reads the tail sees a green
   run. rb-5650 / looks-like-coverage-delivers-none.

The ordering test is STATIC because the failure is Windows-only (on Linux
`python3` is a real binary, so a behavioural test cannot reproduce it here) --
exactly the platform trap that let three defects in g-115-3820 survive a
Linux-only re-test. A static pin catches the regression on every box.

The banner tests are hermetic: the wrapper is copied to a tmp dir so SCRIPT_DIR
resolves there, with a stub run-full-suite.py, a stub _paths.sh, and no
tests/run-invisible-suites.sh / WORLD_PATH -- so no real suite runs.

The stub _paths.sh is load-bearing, not scaffolding. The wrapper hardcodes
`python3`, and the real _paths.sh is what makes that resolve on Windows Git Bash.
An earlier revision of this file omitted it, which left the child depending on
AMBIENT python3: correct on Linux, but on Windows every case returns 127, so the
two rc=127 tests would pass for the WRONG reason and the other four would fail.
Caught by fresh-eyes review of this file's own first commit -- the same
platform-asymmetry trap the ordering test above exists to defend against.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# parents: [0]=tests [1]=core/scripts [2]=core [3]=repo root
REPO = Path(__file__).resolve().parents[3]
WRAPPER = REPO / "core" / "scripts" / "run-full-suite.sh"
SHIM_DIR = REPO / "core" / "scripts" / ".python-shim"
assert WRAPPER.is_file(), f"wrapper not found at {WRAPPER} -- REPO resolved to {REPO}"

sys.path.insert(0, str(REPO / "core" / "scripts"))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])


def _lines():
    return WRAPPER.read_text(encoding="utf-8").splitlines()


def _index_of(pred, what):
    for i, line in enumerate(_lines()):
        if pred(line):
            return i
    raise AssertionError(f"could not locate {what} in {WRAPPER}")


# ---------------------------------------------------------------- ordering ---

def test_paths_sourced_before_framework_invocation():
    """_paths.sh must be sourced BEFORE the python3 call it makes resolvable."""
    src = _index_of(
        lambda l: l.strip().startswith("source ") and "_paths.sh" in l,
        "the _paths.sh source",
    )
    call = _index_of(
        lambda l: l.startswith("python3 ") and "run-full-suite.py" in l,
        "the framework python3 invocation",
    )
    assert src < call, (
        f"_paths.sh sourced at line {src + 1} but the framework suite is invoked at "
        f"line {call + 1}. python3 is unresolvable on Windows until _paths.sh puts "
        f".python-shim on PATH, so this ordering skips the ENTIRE framework half "
        f"with rc=127 while the halves below still print green (g-115-3917)."
    )


def test_paths_sourced_after_cygpath_probe():
    """...and AFTER `command -v cygpath`, or the shim can shadow real cygpath.

    The shim dir is gitignored and per-box; on cc-02 it contains a `cygpath`
    passthrough. Sourcing _paths.sh first prepends that dir to PATH, so
    `command -v cygpath` could resolve to the passthrough and defeat the
    g-115-892 POSIX->Windows conversion. Placement is between the two.
    """
    probe = _index_of(
        lambda l: "command -v cygpath" in l, "the cygpath availability probe"
    )
    src = _index_of(
        lambda l: l.strip().startswith("source ") and "_paths.sh" in l,
        "the _paths.sh source",
    )
    assert probe < src, (
        f"cygpath probed at line {probe + 1} but _paths.sh sourced at line "
        f"{src + 1}. Sourcing first can put a .python-shim/cygpath passthrough "
        f"ahead of the real MSYS2 cygpath (g-115-3917)."
    )


def test_domain_block_does_not_re_source_paths():
    """The domain hook must reuse the hoisted source, not re-source it.

    Not cosmetic: two sources meant two chances to disagree about WORLD_PATH.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    assert text.count('source "$SCRIPT_DIR/_paths.sh"') == 1, (
        "run-full-suite.sh should source _paths.sh exactly once (hoisted above the "
        "framework call); the domain block reuses WORLD_PATH from it."
    )


# ------------------------------------------------------------ tail banner ---

def _run_wrapper(tmp_path, stub_exit_code):
    """Run the wrapper in a hermetic tmp dir with a stubbed framework suite."""
    shutil.copy(WRAPPER, tmp_path / "run-full-suite.sh")
    (tmp_path / "run-full-suite.py").write_text(
        f"import sys; sys.exit({stub_exit_code})\n", encoding="utf-8"
    )
    # Stub _paths.sh so `python3` resolves in the child on EVERY platform. The
    # wrapper hardcodes python3; the real _paths.sh is what makes it resolvable on
    # Windows Git Bash. Omitting this leaves the child on AMBIENT python3, which is
    # fine on Linux and returns 127 for every case on Windows. Mirrors the
    # established pattern in test_verified_wm_set.py. Sets ONLY PATH, so WORLD_PATH
    # stays unset and the domain block is still skipped.
    (tmp_path / "_paths.sh").write_text(
        'command -v python3 >/dev/null 2>&1 || export PATH="%s:$PATH"\n'
        % SHIM_DIR.as_posix(),
        encoding="utf-8",
    )
    # No tests/run-invisible-suites.sh and WORLD_PATH scrubbed -> both trailing
    # halves are skipped, so this is fast and hermetic.
    env = dict(os.environ)
    for k in ("WORLD_PATH", "WORLD_DIR", "MIND_WORLD"):
        env.pop(k, None)
    env["STORAGE_BACKEND"] = "local"  # guard-955
    return subprocess.run(
        [BASH, str(tmp_path / "run-full-suite.sh")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )


def test_banner_names_did_not_run_on_127(tmp_path):
    """rc=127 means the suite never executed -- a setup failure, not a red."""
    r = _run_wrapper(tmp_path, 127)
    assert r.returncode == 127, f"exit contract changed: {r.returncode}"
    assert "FRAMEWORK HALF DID NOT RUN" in r.stdout, (
        f"no did-not-run banner for rc=127. stdout={r.stdout!r}"
    )
    assert "ZERO framework tests executed" in r.stdout
    # The distinction is the point: 127 must NOT read as a test failure.
    assert "DID NOT PASS" not in r.stdout


@pytest.mark.parametrize("rc", [1, 2, 3])
def test_banner_names_did_not_pass_on_other_failures(tmp_path, rc):
    """Non-127 failures keep their exit code AND get a tail banner."""
    r = _run_wrapper(tmp_path, rc)
    assert r.returncode == rc, (
        f"exit contract changed for rc={rc}: got {r.returncode}. "
        f"2 means INVALID/contended and must never be downgraded."
    )
    assert "FRAMEWORK HALF DID NOT PASS" in r.stdout, (
        f"no did-not-pass banner for rc={rc}. stdout={r.stdout!r}"
    )
    assert f"rc={rc}" in r.stdout


def test_no_banner_on_clean_run(tmp_path):
    """A clean framework half must stay silent -- a banner on green would train
    the reader to ignore it, which is the failure this banner exists to fix."""
    r = _run_wrapper(tmp_path, 0)
    assert r.returncode == 0, f"clean run should exit 0, got {r.returncode}"
    assert "FRAMEWORK HALF" not in r.stdout, (
        f"banner fired on a clean run. stdout={r.stdout!r}"
    )


def test_banner_is_last_thing_printed(tmp_path):
    """The banner must be at the TAIL -- that is the whole fix.

    The original error is line 1 of ~15; readers (and `| tail -40`) see the end.
    """
    r = _run_wrapper(tmp_path, 127)
    tail = [l for l in r.stdout.splitlines() if l.strip()][-4:]
    assert any("FRAMEWORK HALF DID NOT RUN" in l for l in tail), (
        f"banner is not in the last 4 non-empty stdout lines: {tail!r}"
    )
