""" — fresh-eyes-record-tick.sh must REFUSE an unrecognised --flag
instead of silently stamping the default slot.

THE DEFECT THIS PINS. `SLOT_NAME="${1:-last_fresh_eyes_review}"` accepts any
first argument, and the next line coerced a leading `--token` back to the
default slot:

    [[ "$SLOT_NAME" == --* ]] && SLOT_NAME="last_fresh_eyes_review"

That coercion has a legitimate purpose — never bind a flag to the slot
positional — but on its own it turned EVERY unrecognised flag into a REAL
cadence stamp. Measured twice on the same agent/script/slot: `--help`
(2026-08-02) and `--print-current --world-only` (2026-08-04). The prior value
was unrecoverable both times; this WM file has no history.

WHY CASE 2 IS NOT REDUNDANT WITH CASE 1. A `--help` handler alone would have
fixed only the first instance. Write #2 used flags that are not `--help` at
all — they are real flags of the DELEGATE (fresh-eyes-cadence-check.sh) that
this wrapper never parsed. Case 2 is the one that fails against a
`--help`-only fix, so dropping it would leave the recurrence uncovered.

WHY THE POSITIVE CONTROL EXISTS (guard-1220, anti-vacuity). Cases 1-2 assert a
REFUSAL. A script rewritten to refuse everything — including its real call
shape — passes both and is completely broken. Cases 3-5 are the other
direction: the shapes real callers actually use, taken from the live call
sites, must still be accepted. Without them this file would pass against a
one-line `exit 2` at the top of the script.

ISOLATION. Every case points BODY_WM_PATH at a tmp file (honored by
wm.py:51), so no live cadence stamp is touched — which is the very hazard the
goal is about. Driving the real .sh through subprocess is deliberate: this is
a bug in shell argument parsing, so a test that imported anything would
measure a different code path than the one that broke (guard-920).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
SCRIPT = SCRIPT_DIR / "fresh-eyes-record-tick.sh"

sys.path.insert(0, str(SCRIPT_DIR))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")

# The real call shapes, harvested from the live call sites rather than invented:
#   .claude/skills/fresh-eyes-tree/SKILL.md:587   <script> last_fresh_eyes_tree_review
#   .claude/skills/fresh-eyes-tree/SKILL.md:93    <script> last_fresh_eyes_tree_review --claim
#   felt-sense-checkin                            <script> last_felt_sense_checkin
#   fresh-eyes-review Phase 8                     <script>            (bare)
ACCEPTED_SHAPES = [
    pytest.param([], id="bare-no-args"),
    pytest.param(["last_fresh_eyes_tree_review"], id="positional-slot"),
    pytest.param(["--claim"], id="claim-alone"),
    pytest.param(["last_fresh_eyes_tree_review", "--claim"], id="slot-then-claim"),
]

REFUSED_SHAPES = [
    pytest.param(["--help"], id="help-write-1-20260802"),
    pytest.param(["--print-current", "--world-only"], id="delegate-flags-write-2-20260804"),
    pytest.param(["--anything-at-all"], id="arbitrary-unknown-flag"),
    pytest.param(["last_fresh_eyes_tree_review", "--help"], id="slot-then-unknown-flag"),
]


@pytest.fixture
def wm(tmp_path):
    """A throwaway working-memory file, wired in via BODY_WM_PATH."""
    p = tmp_path / "working-memory.yaml"
    p.write_text("slots: {}\n", encoding="utf-8")
    return p


def _run(args, wm_path):
    env = dict(os.environ)
    env["BODY_WM_PATH"] = str(wm_path)
    env["STORAGE_BACKEND"] = "local"  # guard-955: never let a tmp write reach the shared store
    return subprocess.run(
        bash_cmd(SCRIPT, *args),
        capture_output=True, text=True, timeout=180,
        cwd=str(PROJECT_ROOT), env=env,
    )


@pytest.mark.parametrize("args", REFUSED_SHAPES)
def test_unknown_flag_is_refused_without_writing(args, wm):
    """An unrecognised --flag exits non-zero AND leaves the WM byte-identical."""
    before = wm.read_bytes()
    r = _run(args, wm)

    assert r.returncode != 0, (
        f"{args} was ACCEPTED (rc=0). Every unrecognised flag used to become a real "
        f"cadence stamp; that is the defect.\nstdout={r.stdout}\nstderr={r.stderr}"
    )
    assert "unknown arg" in r.stderr, (
        f"expected an 'unknown arg' refusal for {args}; got stderr={r.stderr!r}"
    )
    # The load-bearing half: refusing loudly is worthless if it wrote first.
    assert wm.read_bytes() == before, (
        f"{args} MUTATED the working memory despite exiting non-zero — the refusal "
        f"fires after the write, which leaves the defect intact."
    )


def test_refusal_names_the_offending_flag_not_just_the_first_arg(wm):
    """The message must name the flag that was rejected.

    `<slot> --help` rejects argv[2], so a message built from "$1" would print the
    slot name and send the reader chasing the wrong argument.
    """
    r = _run(["last_fresh_eyes_tree_review", "--help"], wm)
    assert r.returncode != 0
    assert "--help" in r.stderr, f"refusal did not name --help; stderr={r.stderr!r}"


@pytest.mark.parametrize("args", ACCEPTED_SHAPES)
def test_real_call_shapes_are_not_refused(args, wm):
    """POSITIVE CONTROL (guard-1220).

    Without this, a script that unconditionally `exit 2`s at the top passes every
    refusal case above. These are the shapes live callers actually use, so any of
    them being refused is a production breakage.

    Asserted at the ARG-PARSE boundary rather than on a successful write: the
    no-arg path calls the cadence-check delegate for a live goal count, so a
    daemon-less environment legitimately fails LATER with a different error. That
    is not this fix's concern, and coupling to it would make the control flaky.
    A refusal is identified positively by its own marker, never by rc alone.
    """
    r = _run(args, wm)
    assert "unknown arg" not in r.stderr, (
        f"real call shape {args} was REFUSED as an unknown flag — this breaks live "
        f"callers.\nstderr={r.stderr}"
    )
    assert r.returncode != 2, (
        f"real call shape {args} exited with the usage code 2.\nstderr={r.stderr}"
    )


def test_claim_flag_still_reaches_claim_mode(wm):
    """`--claim` is the ONE flag this wrapper parses; it must survive the refusal.

    It is also the case the coercion line was originally added for: with the slot
    omitted, `$1` IS `--claim`, so the script must both accept the flag and avoid
    binding it as a slot name. Regressing this would resurrect the footgun that
    stamped a WM slot literally named "--claim".
    """
    r = _run(["--claim"], wm)
    assert "unknown arg" not in r.stderr
    combined = r.stdout + r.stderr
    assert "--claim" not in combined.replace("[--claim]", ""), (
        f"'--claim' leaked into the slot name — the coercion no longer protects the "
        f"positional.\nstdout={r.stdout}\nstderr={r.stderr}"
    )
