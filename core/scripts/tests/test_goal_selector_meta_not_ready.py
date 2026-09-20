"""test_goal_selector_meta_not_ready.py — the selector names its state and remedy.

Measured 2026-09-19: a served loop reached the selector in a world whose boot
Phase -2 never ran. `WEIGHTS = load_weights()` runs at module import over a bare
open(), so every invocation died the same way — 9 of 9 — with a raw traceback
that named the missing file and never what to do about it.

What is pinned here:
  (1) absence raises MetaNotReadyError, whose message names WHICH state (never
      initialized vs. an initialized tier missing a seed) and the remedy;
  (2) it stays a FileNotFoundError, so no existing handler changes behaviour;
  (3) run as the CLI through its production wrapper it exits 10 with NO
      traceback and nothing on stdout;
  (4) POSITIVE CONTROL — a present file still loads. Without it every assertion
      above would pass on a selector that simply always refuses (guard-4166).

Import pattern mirrors test_goal_selector_weights_contract.py.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


@pytest.fixture()
def empty_meta(tmp_path, monkeypatch):
    """A meta dir with NO strategy file, wired into the module under test."""
    meta = tmp_path / "meta"
    meta.mkdir()
    monkeypatch.setattr(gs, "META_DIR", meta)
    monkeypatch.setattr(gs, "META_GOAL_SELECTION", meta / "goal-selection-strategy.yaml")
    return meta


# ── (1) the two states are told apart, and both name the remedy ─────────────

def test_never_initialized_world_is_named_as_such(empty_meta):
    with pytest.raises(gs.MetaNotReadyError) as exc:
        gs.load_weights()
    msg = str(exc.value)
    assert "NEVER INITIALIZED" in msg
    assert "init-mind.sh" in msg, "the remedy must be IN the failure, not beside it"
    assert str(empty_meta / "goal-selection-strategy.yaml") in msg
    assert f"rc={gs.EXIT_META_NOT_READY}" in msg


def test_initialized_tier_missing_its_seed_is_a_different_state(empty_meta):
    """Marker present + file absent means something REMOVED a seed from a live
    tier. Same remedy (init's backfill is additive), different diagnosis — a
    reader must not be told a damaged world is a fresh one."""
    (empty_meta / ".initialized").touch()
    with pytest.raises(gs.MetaNotReadyError) as exc:
        gs.load_weights()
    msg = str(exc.value)
    assert "MISSING" in msg and "NEVER INITIALIZED" not in msg
    assert "init-mind.sh" in msg


def test_the_message_says_it_is_the_world_not_the_selector(empty_meta):
    """Without this sentence the failure reads as a selector bug, and the next
    reader goes looking for a defect that is not there."""
    with pytest.raises(gs.MetaNotReadyError) as exc:
        gs.load_weights()
    assert "not a selector defect" in str(exc.value)


# ── (2) compatibility: still the exception the bare open() raised ──────────

def test_it_is_still_a_file_not_found_error(empty_meta):
    assert issubclass(gs.MetaNotReadyError, FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        gs.load_weights()


# ── (4) POSITIVE CONTROL: a present file loads exactly as before ───────────

def test_a_present_strategy_file_still_loads(empty_meta):
    known = sorted(gs.KNOWN_CRITERIA)[0]
    (empty_meta / "goal-selection-strategy.yaml").write_text(
        f"weights:\n  {known}: 1.25\n", encoding="utf-8"
    )
    assert gs.load_weights() == {known: 1.25}


# ── (3) the CLI, through its PRODUCTION wrapper ────────────────────────────

def _run_wrapper(meta_dir: Path):
    from _runtime_bash import bash_cmd  # guard-580: never a bare "bash" argv

    env = os.environ.copy()
    env.update({
        "MIND_META": str(meta_dir),
        "MIND_AGENT": "testagent",
        "STORAGE_BACKEND": "local",  # guard-955: a test subprocess never inherits own-cloud
    })
    return subprocess.run(
        bash_cmd(CORE_SCRIPTS / "goal-selector.sh", "select"),
        capture_output=True, text=True, timeout=120, env=env,
    )


def test_cli_exits_10_with_no_traceback_and_empty_stdout(tmp_path):
    meta = tmp_path / "meta"
    meta.mkdir()
    r = _run_wrapper(meta)
    assert r.returncode == gs.EXIT_META_NOT_READY, (r.returncode, r.stderr[-400:])
    assert "Traceback" not in r.stderr, "the traceback is what buried the remedy"
    assert r.stdout == "", "a refusing selector must not print a ranking"
    assert "REMEDY" in r.stderr and "init-mind.sh testagent" in r.stderr


def test_exit_code_collides_with_no_documented_selector_code():
    """goal-selector.sh documents 1, 2, 3, 7, 8, 9 and 124. A collision would
    make a caller's rc log ambiguous, which is the whole reason for a distinct
    code."""
    assert gs.EXIT_META_NOT_READY not in {0, 1, 2, 3, 7, 8, 9, 124}
