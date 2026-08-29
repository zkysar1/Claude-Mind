"""test_iteration_close_unknown_arg_help.py -- an invented flag gets the accepted set and
the nearest accepted flag, not a bare `unknown arg` (2026-08-29).

Measured on a downstream deployment (eight Bodies on a small local model): the close
writer answered `--outcome-class deep` with `unknown arg: --outcome-class` and nothing
else, and the Body's next turn invented `--executed-by`, then a third flag -- a full
model turn each. The accepted set is read from the parser's OWN case labels at the
moment of the refusal, so the help cannot drift from the parser; the nearest flag is
the prefix relation in either direction (`--outcome-class` -> `--outcome`).

Executes the script: the parser runs before any daemon dependency and exits 2 fast.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "core" / "scripts" / "iteration-close.sh"

sys.path.insert(0, str(ROOT / "core" / "scripts"))
from _runtime_bash import BASH  # noqa: E402


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, SCRIPT.as_posix(), *args], capture_output=True, text=True, timeout=60
    )


_LABEL = re.compile(r"^\s+(--[a-z-]+)\)", re.M)


def _parser_block() -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^while \[\[ \$# -gt 0 \]\]; do$.*?^done$", src, re.M | re.S)
    assert m, "the argument-parsing `while ... done` block moved -- fix the script's sed range AND this regex"
    return m.group(0)


def _labels_in_source() -> set[str]:
    """Case labels of the argument-parsing block ONLY (guard-2172). The tripwire
    below fails the day a second `--flag)` case block appears anywhere else in the
    script, so its author decides whether the help should list it."""
    block = set(_LABEL.findall(_parser_block()))
    whole = set(_LABEL.findall(SCRIPT.read_text(encoding="utf-8")))
    assert whole == block, (
        f"flag-shaped case labels outside the parser block: {sorted(whole - block)} -- "
        "the help is bounded to the parser (guard-2172); decide whether these belong in it"
    )
    return block


def test_a_near_miss_names_the_accepted_set_and_the_nearest_flag():
    proc = _run("--phase", "verify", "--goal", "g-000-00", "--outcome-class", "deep")
    assert proc.returncode == 2
    err = proc.stderr
    assert "unknown arg: --outcome-class" in err
    accepted = re.search(r"^\s*accepted: (.*)$", err, re.M)
    assert accepted, err
    listed = set(accepted.group(1).split())
    assert {"--outcome", "--summary", "--status", "--override-residual"} <= listed
    assert re.search(r"^\s*did you mean: --outcome\s*$", err, re.M), err


def test_the_listed_set_is_the_parser_itself():
    """Drift pin: the help is generated from the case labels, never restated."""
    proc = _run("--no-such-flag")
    listed = set(re.search(r"^\s*accepted: (.*)$", proc.stderr, re.M).group(1).split())
    labels = _labels_in_source()
    assert labels, "no case labels parsed from the script -- the label regex is stale"
    assert listed == labels


def test_an_unrelated_invention_gets_the_set_but_no_guess():
    proc = _run("--executed-by", "coach")
    assert proc.returncode == 2
    assert "unknown arg: --executed-by" in proc.stderr
    assert "accepted:" in proc.stderr
    assert "did you mean" not in proc.stderr
