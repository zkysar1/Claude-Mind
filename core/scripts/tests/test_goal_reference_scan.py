"""test_goal_reference_scan.py — regression for  DEFECT 2.

aspirations-evolve Step 2.75c relocates a recurring goal by creating a COPY
under a NEW id and completing the original. Every inbound reference still
points at the OLD id. This scanner is the precondition that makes "would this
orphan anything?" mechanical instead of remembered.

The load-bearing property is the BLOCKING/HISTORICAL split. A naive total-count
check is vacuous in the always-fires direction: any goal that has ever run
accumulates thousands of append-only log lines (one real recurring goal
measured 3,825 total mentions, of which only 84 were live referents). A gate
that refuses every relocation teaches the reader to ignore it, which is the
same end state as having no gate.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "goal-reference-scan.py"
GOAL = "g-777-42"


def _run(root: Path, goal: str = GOAL, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), goal, "--root", str(root), *extra],
        capture_output=True, text=True)


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# --- clear case ------------------------------------------------------------

def test_no_references_is_clear(tmp_path):
    _write(tmp_path, "core/scripts/unrelated.py", "# nothing here\n")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "CLEAR" in r.stdout


# --- blocking references ---------------------------------------------------

@pytest.mark.parametrize("rel", [
    "core/scripts/some_script.py",
    "core/scripts/tests/test_something.py",
    "core/config/rationale/some-doc.md",
    ".claude/skills/some-skill/SKILL.md",
])
def test_live_referent_blocks(tmp_path, rel):
    _write(tmp_path, rel, f"# see {GOAL} for context\n")
    r = _run(tmp_path)
    assert r.returncode == 1, r.stdout
    assert "SKIP RELOCATION" in r.stderr
    assert GOAL in r.stderr


# --- historical references must NOT block (the anti-vacuous property) ------

@pytest.mark.parametrize("rel", [
    "core/logs/changelog.jsonl",
    "core/scripts/evolution-log.jsonl",
    "core/scripts/changelog-archive.jsonl",     # matched by -archive suffix
    "core/scripts/pipeline-archive.jsonl",      # ditto, name never enumerated
    "core/scripts/gate-firings.jsonl",          # enumerated legacy telemetry
    "core/scripts/gate-firings-2026-08-17.jsonl",  # its date segment: matched by stem, name never enumerated (GATE_FIRINGS_SEGMENTED, 2026-08-17)
    "core/scripts/reasoning-bank-utilization.jsonl",  #  counter sidecar (advisory stats, RMW-flushed)
    "core/scripts/guardrails-utilization.jsonl",      # ditto
    "core/scripts/improvement-velocity.yaml",
    "core/board/findings.jsonl",
    "core/journal/2026-07-25.md",
])
def test_historical_mention_does_not_block(tmp_path, rel):
    _write(tmp_path, rel, f'{{"details": "goal {GOAL} completed"}}\n')
    r = _run(tmp_path)
    assert r.returncode == 0, (
        f"{rel} wrongly counted as a live referent — this is the always-fires "
        f"failure mode:\n{r.stderr}")
    assert "historical" in r.stdout


def test_sidecar_names_pinned_to_counters_name():
    """The two sidecar literals in _HISTORICAL_NAMES must equal
    _utilization_store.counters_name(kind) for every kind. The scanner
    deliberately does NOT import the seam (it must keep working when the seam
    module is absent), so this pin is the only thing keeping the literals from
    drifting — the same pattern _fileops uses for its snapshot blacklist
    (46a035a5b). A rename in the seam reddens here, not in production."""
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_grs", SCRIPT)
        grs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(grs)
        from _utilization_store import KINDS, counters_name
        expected = {counters_name(k) for k in KINDS}
        assert expected <= grs._HISTORICAL_NAMES, (
            f"sidecar basenames {expected - grs._HISTORICAL_NAMES} missing from "
            f"_HISTORICAL_NAMES — goal ids in a sidecar would count as live "
            f"referents (the always-fires failure mode, 3ee1cf881 class)")
    finally:
        sys.path.remove(str(SCRIPT.parent))


@pytest.mark.parametrize("rel", [
    "core/scripts/reasoning-bank-2026-08-18.jsonl",
    "core/scripts/guardrails-2026-08-18.jsonl",
])
def test_content_date_segment_stays_blocking(tmp_path, rel):
    """rb/guardrail DATE SEGMENTS are the CONTENT store under a dynamic name —
    they carry live source_goal referents exactly like the legacy file, which
    is blocking. This is the deliberate NON-entry documented beside the sidecar
    literals: it reddens if someone 'generalizes' the sidecar entries into a
    <kind>-*.jsonl glob or a stem rule (the gate-firings stem rule does NOT
    transfer — that store is append-only telemetry, these are content)."""
    _write(tmp_path, rel, f'{{"source_goal": "{GOAL}"}}\n')
    r = _run(tmp_path)
    assert r.returncode == 1, (
        f"{rel} classified historical — a content segment's goal refs must "
        f"BLOCK relocation:\n{r.stdout}\n{r.stderr}")


def test_historical_alone_still_reports_count(tmp_path):
    _write(tmp_path, "core/logs/changelog.jsonl",
           f'{{"a": "{GOAL}"}}\n{{"b": "{GOAL}"}}\n')
    r = _run(tmp_path)
    assert r.returncode == 0
    assert "2 historical" in r.stdout


def test_mixed_blocks_on_the_live_referent_only(tmp_path):
    _write(tmp_path, "core/logs/changelog.jsonl", f'{{"a": "{GOAL}"}}\n' * 50)
    _write(tmp_path, "core/scripts/real.py", f"# {GOAL}\n")
    r = _run(tmp_path, GOAL, "--json")
    payload = json.loads(r.stdout)
    assert r.returncode == 1
    assert payload["blocking_count"] == 1
    assert payload["historical_count"] == 50
    assert payload["verdict"] == "referenced"


# --- ancestor-directory names must not classify (the always-PASSES bug) ----

@pytest.mark.parametrize("ancestor", ["temp", "logs", "board", "sessions",
                                      "health", "experience", "journal"])
def test_ancestor_dir_name_does_not_reclassify(tmp_path, ancestor):
    """Classification uses the ROOT-RELATIVE path, never the absolute one.

    Found by fresh-eyes probe on this file (g-115-3096). is_historical matched
    directory names against absolute `p.parts`, which includes every ANCESTOR
    of the repo. A checkout under a dir named temp/ (or any other narration
    dir-name) silently classified EVERY file as append-only narration, so the
    precondition stopped blocking anything and always reported CLEAR.

    That is the always-PASSES inverse of the always-fires bug this same
    classifier was built to avoid — and strictly more dangerous, because a gate
    that never fires is indistinguishable from a clean repo. Measured before the
    fix: identical file, rc=1 under a normal path, rc=0 CLEAR under
    <root>/temp/repo/.
    """
    root = tmp_path / ancestor / "repo"
    _write(root, "core/scripts/real.py", f"# refers to {GOAL}\n")
    r = _run(root)
    assert r.returncode == 1, (
        f"a live referent under an ancestor dir named {ancestor!r} was "
        f"silently reclassified as narration — the gate stopped blocking:\n"
        f"{r.stdout}{r.stderr}")


def test_ancestor_skip_dir_name_does_not_hide_files(tmp_path):
    """Same class for _SKIP_DIR_PARTS: a repo under .venv/ or node_modules/
    would otherwise skip every file and report a vacuous CLEAR."""
    root = tmp_path / "node_modules" / "repo"
    _write(root, "core/scripts/real.py", f"# refers to {GOAL}\n")
    r = _run(root)
    assert r.returncode == 1, r.stdout + r.stderr


# --- the aspirations store is excluded from both classes -------------------

def test_aspirations_store_is_not_a_reference(tmp_path):
    """The goal's own record, and the queue Step 2.75c rewrites on purpose."""
    _write(tmp_path, "core/scripts/aspirations.jsonl",
           f'{{"id": "{GOAL}", "recurring": true}}\n')
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr


# --- id boundary -----------------------------------------------------------

def test_prefix_of_a_longer_id_does_not_match(tmp_path):
    """g-777-4 must not match  (or every short id blocks forever)."""
    _write(tmp_path, "core/scripts/real.py", "# refers to g-777-421 only\n")
    r = _run(tmp_path, "g-777-42")
    assert r.returncode == 0, r.stderr


def test_exact_id_at_end_of_line_matches(tmp_path):
    _write(tmp_path, "core/scripts/real.py", f"# see {GOAL}")
    r = _run(tmp_path)
    assert r.returncode == 1, r.stdout


# --- usage -----------------------------------------------------------------

def test_blank_goal_id_is_usage_error(tmp_path):
    r = _run(tmp_path, "   ")
    assert r.returncode == 2
