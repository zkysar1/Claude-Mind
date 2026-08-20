""" — wm-append.sh must accept EXACTLY ONE positional, or refuse loudly.

THE DEFECT: the wrapper's arg loop was a bare catch-all —

    SLOT=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            *) SLOT="$1"; shift;;
        esac
    done

— which ASSIGNS on every token, so the LAST one wins and any token at all
becomes the slot name. Two shapes fall out of that, and they need different
defences:

  * `wm-append.sh spark_capture --json`      -> slot=--json.
  * `wm-append.sh spark_capture exp_capture` -> slot=exp_capture.

Only the FIRST is reachable by a downstream check. g-115-6726 added
`unknown_lane_refusal`, which refuses a leading-dash root always and an
unregistered root at lane-creation — so the flag shape is caught, though the
wrapper discards the daemon's stderr and the operator sees rc=1 with empty
output. The SECOND shape names two REGISTERED lanes, so no downstream check has
any basis to object: the entry lands in the wrong lane at HTTP 200, silently.
That is the half a leading-dash-only fix misses, and it is the mechanism that
actually bit (measured twice in one session, alpha worker Body, hostname cc-07,
uname -r 6.8.0-137-generic, 2026-08-19).

WHY THIS FILE IS SEPARATE FROM test_wm_append_unknown_slot.py: that file pins
the REFUSAL PREDICATE (`wm.unknown_lane_refusal`) and the two append paths that
consume it. Not one of its cases invokes `bash core/scripts/wm-append.sh`, so
the arg loop between the operator and those paths was untested by construction.
This file tests the WRAPPER, in its literal production call shape (guard-920).

HERMETIC BY CONSTRUCTION — no daemon, no network, no working-memory write. Each
case runs a COPY of the production wrapper (copied at test time, so it can never
drift from the real file) inside a tmp project root whose `_runtime.sh` is a
stub that RECORDS the query string instead of sending it. That records the one
fact worth pinning: which slot would have reached the transport, and whether the
transport was reached at all.

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_wm_append_arg_parser.py -q
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_ROOT = SCRIPT_DIR.parent.parent
PROJECT_ROOT = CORE_ROOT.parent
WRAPPER = CORE_ROOT / "scripts" / "wm-append.sh"

# The stub stands in for core/scripts/_runtime.sh. `rt_call` writes the --query
# argument to $RT_STUB_LOG and succeeds; the file's EXISTENCE is therefore the
# "did the wrapper reach the transport at all" signal, and its CONTENT is the
# slot that would have been sent.
_RUNTIME_STUB = """\
rt_url_encode() { printf '%s' "$1"; }
rt_call() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --query) printf '%s' "$2" > "$RT_STUB_LOG"; shift 2;;
            *) shift;;
        esac
    done
    printf '%s' '{"ok": true}'
    return 0
}
rt_try_autospawn() { return 1; }
rt_no_daemon_error() { echo "stub: rt_no_daemon_error reached" >&2; exit 1; }
"""


def _sandbox(tmp_path: Path, mutate: bool = False) -> Path:
    """A tmp project root holding a copy of the wrapper plus the stub runtime.

    `mutate=True` restores the pre-fix bare catch-all in the copy. That is the
    positive control: without it, assertions that merely observe "the refused
    shapes never reached the transport" would pass just as happily against a
    harness that never ran the wrapper at all (guard-2421).
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WRAPPER, scripts / "wm-append.sh")
    (scripts / "_runtime.sh").write_text(_RUNTIME_STUB, encoding="utf-8")

    if mutate:
        src = (scripts / "wm-append.sh").read_text(encoding="utf-8")
        start = src.index("SLOT=\"\"\n_seen=0")
        end = src.index("if [ -z \"$SLOT\" ]; then", start)
        assert start < end, "could not locate the arg loop to mutate"
        src = src[:start] + (
            'SLOT=""\n'
            'while [[ $# -gt 0 ]]; do\n'
            '    case "$1" in\n'
            '        *) SLOT="$1"; shift;;\n'
            '    esac\n'
            'done\n\n'
        ) + src[end:]
        (scripts / "wm-append.sh").write_text(src, encoding="utf-8")

    return scripts


def _run(scripts: Path, args, stdin: str = '{"observation": "x"}'):
    log = scripts.parent.parent / "rt-stub.log"
    if log.exists():
        log.unlink()
    # BASH, not a bare "bash": the bare name resolves to WSL bash on win32 and
    # hangs past the timeout (guard-580). `.as_posix()` for the script path
    # because bash silently strips the backslashes of a str(WindowsPath)
    # (guard-581) — which would look exactly like "the script does not exist".
    proc = subprocess.run(
        [BASH, (scripts / "wm-append.sh").as_posix(), *args],
        input=stdin, text=True, capture_output=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(scripts.parent.parent),
             "RT_STUB_LOG": str(log)},
    )
    sent = log.read_text(encoding="utf-8") if log.exists() else None
    return proc, sent


# --- (a) a leading-dash token in the slot position ---------------------------

def test_leading_dash_alone_is_refused_and_names_the_token(tmp_path):
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["--json"])
    assert proc.returncode != 0, "a flag in the slot position must exit non-zero"
    assert "--json" in proc.stderr, (
        f"the refusal must NAME the offending token; got: {proc.stderr!r}")
    assert sent is None, "a refused shape must never reach the transport"


def test_trailing_flag_after_a_valid_slot_is_refused(tmp_path):
    """The shape that actually bit: the slot is right there, and loses anyway."""
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["spark_capture", "--json"])
    assert proc.returncode != 0
    assert "--json" in proc.stderr, (
        f"the refusal must name the FLAG, not the slot it displaced: {proc.stderr!r}")
    assert sent is None, (
        f"the wrapper still sent something to the transport: {sent!r}")


@pytest.mark.parametrize("flag", ["--json", "--stdin-json", "--load-bearing", "-v"])
def test_every_flag_shape_is_refused(tmp_path, flag):
    """`--stdin-json` is not hypothetical: it exists as a `slots.*` child in two
    live WM files on cc-07 (value None), minted by exactly this defect before
    the daemon-side refusal landed. A single-flag test would not have covered it.
    """
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["spark_capture", flag])
    assert proc.returncode != 0, f"{flag} was accepted as a slot name"
    assert flag in proc.stderr
    assert sent is None


# --- (b) a second positional -------------------------------------------------

def test_second_positional_is_refused_not_silently_preferred(tmp_path):
    """BOTH names are registered append targets, so nothing downstream can catch
    this — the refusal has to happen here or not at all."""
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["spark_capture", "exp_capture"])
    assert proc.returncode != 0, (
        "a second positional was accepted — the append would land in the wrong lane")
    assert "exp_capture" in proc.stderr and "spark_capture" in proc.stderr, (
        f"the refusal must name BOTH the offending token and the slot it would "
        f"have displaced: {proc.stderr!r}")
    assert sent is None


def test_second_positional_refused_in_either_order(tmp_path):
    """Order-independence matters: the pre-fix loop was last-wins, so a reader
    could conclude the defect is 'the trailing token' rather than 'more than
    one token'. Both orders must refuse."""
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["exp_capture", "spark_capture"])
    assert proc.returncode != 0
    assert sent is None


def test_third_positional_is_refused(tmp_path):
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["spark_capture", "exp_capture", "hyp_capture"])
    assert proc.returncode != 0
    assert sent is None


# --- (c) a valid append is unchanged -----------------------------------------

def test_single_valid_positional_reaches_the_transport_unchanged(tmp_path):
    """The positive control. Without it, a parser that refused EVERYTHING would
    pass every assertion above while breaking working memory entirely."""
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["spark_capture"])
    assert proc.returncode == 0, (
        f"a valid single-positional append was refused: {proc.stderr!r}")
    assert sent == "slot=spark_capture", (
        f"the wrapper sent {sent!r} instead of the requested slot")


def test_dotted_slot_path_still_works(tmp_path):
    """Dotted paths are a real call shape (`loop_state.signals.x`) and contain no
    dash, so the new refusal must not disturb them."""
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, ["loop_state.signals.goals_since_last_tree_update"])
    assert proc.returncode == 0, proc.stderr
    assert sent == "slot=loop_state.signals.goals_since_last_tree_update"


def test_no_arguments_still_reports_the_missing_slot(tmp_path):
    """Pre-existing behaviour; pinned so the rewrite cannot drop it."""
    scripts = _sandbox(tmp_path)
    proc, sent = _run(scripts, [])
    assert proc.returncode != 0
    assert "slot name required" in proc.stderr
    assert sent is None


# --- positive control: the harness detects the pre-fix defect -----------------

def test_mutation_control_prefix_loop_sends_the_wrong_slot(tmp_path):
    """Restore the bare catch-all in the copy and confirm the defect reappears.

    This is what separates "the assertions above pass" from "the assertions
    above would catch a regression". Under the pre-fix loop both refused shapes
    reach the transport carrying the WRONG slot, at rc=0.
    """
    scripts = _sandbox(tmp_path, mutate=True)

    proc, sent = _run(scripts, ["spark_capture", "--json"])
    assert proc.returncode == 0, "pre-fix loop should have accepted the flag shape"
    assert sent == "slot=--json", (
        f"mutation did not reproduce the flag defect (sent {sent!r}) — the "
        f"control is not exercising the code the tests above protect")

    proc, sent = _run(scripts, ["spark_capture", "exp_capture"])
    assert proc.returncode == 0
    assert sent == "slot=exp_capture", (
        f"mutation did not reproduce the wrong-lane defect (sent {sent!r})")

    # ...and the valid shape is unaffected either way, which is why the defect
    # went unnoticed: every correct call site kept working.
    proc, sent = _run(scripts, ["spark_capture"])
    assert proc.returncode == 0 and sent == "slot=spark_capture"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
