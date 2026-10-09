"""test_pending_questions_stale.py — regression test for .

Asserts that pending-questions-sweep.py's `stale` subcommand ages OPEN
(status=pending) questions via the SAME fallback chain the sweep's own
heuristics use (_age_days: created, created_at, date, asked_at, logged_at),
NOT `asked_at` alone.

Background (g-115-11656): the aspirations-all-blocked Step B2.5 S1 signal
selected pending questions whose `asked_at` was older than 24h. On ZDS that
field was present on 1 of 26 live questions, so S1 could count at most 1 of 26
whatever their age — the signal was effectively dead. The fix routes S1's
aging through the sweep's `stale` subcommand, which reuses `_age_days`, so the
two can never drift apart.

The two verification outcomes this test pins:
  1. A pending question OLDER than 24h that carries `created` but NO
     `asked_at` IS counted by S1 (the exact defect the goal names).
  2. A question YOUNGER than 24h under EVERY date field is still not counted
     (negative control — the fix must not over-report fresh questions).

Also pinned:
  3. A settled question (status=resolved) older than the threshold is NOT
     counted — S1 is about UNANSWERED questions only.
  4. A pending question with NO parseable date field is NOT counted (unknown
     age is not stale).
  5. The default threshold is 24h (1.0 day) — the S1 signal's contract.
  6. --all-agents is REFUSED for `stale` (SystemExit 2): S1 acts on the bound
     agent's file only.
  7. The returned `age_field` names the FIRST field in the chain that parses,
     so a reader can see the basis of the age (created beats a younger
     date/asked_at on the same entry).

Pattern: importlib + sys.path shape matching test_pending_questions_sweep.py —
the hyphenated filename is loaded via spec_from_file_location.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_sweep():
    """Load pending-questions-sweep.py via importlib (hyphenated name)."""
    spec = importlib.util.spec_from_file_location(
        "pending_questions_sweep_mod_stale",
        CORE_SCRIPTS / "pending-questions-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for pending-questions-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ago(days: float) -> str:
    """ISO stamp `days` in the past, in _parse_date's first format."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _run_stale(tmp_path, entries, all_agents=False, max_age_days=1.0):
    """Write a pending-questions.yaml with `entries` and call cmd_stale.

    Returns the cmd_stale result dict. Uses the `questions: [...]` wrapper
    shape, the first of _load_questions's three tolerated shapes.
    """
    mod = _import_sweep()
    pq = tmp_path / "pending-questions.yaml"
    pq.write_text(
        yaml.safe_dump({"questions": entries}, allow_unicode=True),
        encoding="utf-8",
    )
    args = mod.argparse.Namespace(
        subcommand="stale",
        pq_path=str(pq),
        all_agents=all_agents,
        apply=False,
        apply_cleanup=False,
        max_age_days=max_age_days,
    )
    return mod.cmd_stale(args)


# --- Outcome 1: pending + created (no asked_at) + >24h IS counted ----------

def test_outcome1_pending_created_no_asked_at_is_counted(tmp_path):
    """The exact defect: created present, asked_at ABSENT, older than 24h."""
    result = _run_stale(
        tmp_path,
        [
            {
                "id": "pq-old-created",
                "question": "old, created only",
                "status": "pending",
                "created": _ago(3.0),
            }
        ],
    )
    assert result["counts"]["stale"] == 1
    ids = [e["id"] for e in result["entries"]]
    assert ids == ["pq-old-created"]
    assert result["entries"][0]["age_field"] == "created"


# --- Outcome 2: negative control, young under EVERY date field -------------

def test_outcome2_young_under_every_date_field_is_not_counted(tmp_path):
    """Every date field present and fresh: must NOT be counted."""
    result = _run_stale(
        tmp_path,
        [
            {
                "id": "pq-fresh",
                "question": "fresh under all fields",
                "status": "pending",
                "created": _ago(0.2),           # 0.2d < 1.0d
                "created_at": _ago(0.3),
                "date": (datetime.now() - timedelta(days=0.4)).strftime("%Y-%m-%d"),
                "asked_at": _ago(0.5),
                "logged_at": _ago(0.6),
            }
        ],
    )
    assert result["counts"]["stale"] == 0
    assert result["entries"] == []


# --- Settled / unknown-age exclusions ---------------------------------------

def test_resolved_old_question_is_not_counted(tmp_path):
    """A settled (status=resolved) question is not UNANSWERED, so not S1."""
    result = _run_stale(
        tmp_path,
        [
            {
                "id": "pq-resolved-old",
                "question": "resolved long ago",
                "status": "resolved",
                "created": _ago(90.0),
            }
        ],
    )
    assert result["counts"]["stale"] == 0


def test_pending_no_date_field_is_not_counted(tmp_path):
    """No parseable date field -> unknown age -> not stale (no crash)."""
    result = _run_stale(
        tmp_path,
        [
            {
                "id": "pq-no-date",
                "question": "no date field",
                "status": "pending",
            }
        ],
    )
    assert result["counts"]["stale"] == 0


# --- Default threshold is 24h ------------------------------------------------

def test_default_threshold_is_24h(tmp_path):
    """A question 30h old IS stale at the default; 10h old is not."""
    result = _run_stale(
        tmp_path,
        [
            {"id": "pq-30h", "question": "30h", "status": "pending", "created": _ago(30 / 24)},
            {"id": "pq-10h", "question": "10h", "status": "pending", "created": _ago(10 / 24)},
        ],
    )
    ids = [e["id"] for e in result["entries"]]
    assert ids == ["pq-30h"]  # only the >24h one
    assert result["max_age_days"] == 1.0


# --- --all-agents is refused for stale (exit 2) -----------------------------

def test_all_agents_is_refused_for_stale(tmp_path):
    with pytest.raises(SystemExit) as ei:
        _run_stale(tmp_path, [], all_agents=True)
    assert ei.value.code == 2


# --- age_field reports the chain's FIRST matching field ----------------------

def test_age_field_prefers_earlier_chain_field(tmp_path):
    """created (first in chain) is the basis over a present but younger date."""
    result = _run_stale(
        tmp_path,
        [
            {
                "id": "pq-multi",
                "question": "multi date fields",
                "status": "pending",
                "created": _ago(5.0),
                "date": (datetime.now() - timedelta(days=2.0)).strftime("%Y-%m-%d"),
            }
        ],
    )
    assert result["counts"]["stale"] == 1
    assert result["entries"][0]["age_field"] == "created"
    assert result["entries"][0]["age_days"] > 4.9  # aged from created (5d), not date (2d)
