"""Pins for probe-path co-signal exclusion in the goal-duplication gate ().

THE DEFECT. An OUTCOME declares what a goal will CHANGE; a CHECK declares how you
would CONFIRM it. `_extract_signals` did not distinguish them -- it flattened
outcomes and checks into one blob and promoted every path it found to full
target-file co-signal. So the ledger a check READS, or the canonical script it
SHELLS, was weighted exactly like a file the goal MODIFIES, and matched every
other goal that genuinely touches it. Observed twice in one boot on 2026-07-27
(g-318-77, g-250-273), both blocking legitimate non-duplicate goals, both needing
an audited --override-duplication.

WHY IT IS WORTH FIXING RATHER THAN OVERRIDING, which is the part that makes these
pins load-bearing: the better a goal's verification -- canonical scripts, real
ledgers -- the likelier the false block. The cheapest escape is therefore to write
WEAKER verification, or to reach for the override by reflex, which erodes the
audit value of every entry in the override ledger.

SCOPE IS BROADER THAN THE FILING PROPOSED, and `test_a_string_check_is_covered_not_just_a_command_field`
is the pin that encodes the correction. The goal asked to exclude paths appearing
only inside `verification.checks[].command`. Measured over the 845 asp-115 goals
with a non-empty checks list: of 1,669 check elements, 1,572 (94.2%) are plain
STRINGS and only 22 carry a `command` key. Of the 346 goals whose path co-signal
comes only from checks, that rule would have reached 15 -- 4.3% -- leaving 331
untouched. So the exclusion keys on the checks CONTAINER, which is also
shape-independent: string, dict-with-command, or a dict shape nobody has written
yet all behave the same.

`test_checks_without_outcomes_still_yield_co_signal` is the inversion guard. Left
off, the fix trades a false BLOCK for a false ADMIT -- and a false admit is
silent, so it is the worse direction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS / "gates"))

_spec = importlib.util.spec_from_file_location(
    "goal_duplication_probe", _SCRIPTS / "gates" / "goal_duplication.py")
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)

PROBE = "core/scripts/ohs-trend-ledger.py"
TARGET = "core/scripts/goal-selector.py"


def _paths(goal):
    file_paths, _kw, source = gd._extract_signals(goal)
    return file_paths, source


# ---------------------------------------------------------------------------
# The defect.
# ---------------------------------------------------------------------------

def test_a_path_only_in_a_check_is_not_target_file_co_signal():
    fp, src = _paths({
        "title": "Fix the selector", "description": "selector work",
        "verification": {"outcomes": [f"{TARGET} stops double counting"],
                         "checks": [f"run {PROBE} and confirm the trend is flat"]}})
    assert src == "verification"
    assert PROBE not in fp, "a path the check merely READS must not be co-signal"
    assert TARGET in fp, "the path the OUTCOME declares must survive"


def test_a_string_check_is_covered_not_just_a_command_field():
    """The scope correction. 94.2% of live checks are plain strings, so a rule
    keyed on checks[].command would reach almost none of the real population."""
    fp, _ = _paths({
        "title": "Fix the selector", "description": "x",
        "verification": {"outcomes": ["the selector is fixed"],
                         "checks": [f"shell {PROBE} and eyeball the output"]}})
    assert PROBE not in fp


def test_a_dict_check_command_is_covered_too():
    """The filing's proposed scope, which the container-level rule subsumes."""
    fp, _ = _paths({
        "title": "Fix the selector", "description": "x",
        "verification": {"outcomes": ["the selector is fixed"],
                         "checks": [{"type": "cmd", "command": f"py -3 {PROBE} --json"}]}})
    assert PROBE not in fp


def test_an_unknown_dict_check_shape_is_covered():
    """Shape-independence: keying on the container means a check shape nobody has
    written yet needs no new rule here."""
    fp, _ = _paths({
        "title": "Fix the selector", "description": "x",
        "verification": {"outcomes": ["the selector is fixed"],
                         "checks": [{"type": "future", "invoke": PROBE,
                                     "expect": "flat"}]}})
    assert PROBE not in fp


# ---------------------------------------------------------------------------
# No regression on genuine target files.
# ---------------------------------------------------------------------------

def test_a_path_declared_in_outcomes_is_kept():
    fp, _ = _paths({
        "title": "Fix the ledger", "description": "ledger work",
        "verification": {"outcomes": [f"{PROBE} no longer writes duplicate rows"],
                         "checks": ["confirm no dupes"]}})
    assert PROBE in fp


def test_a_path_in_both_outcomes_and_checks_is_kept():
    """Verifying the file you are changing is normal and must not be penalised."""
    fp, _ = _paths({
        "title": "Fix the ledger", "description": "x",
        "verification": {"outcomes": [f"{PROBE} is fixed"],
                         "checks": [f"run {PROBE} and confirm"]}})
    assert PROBE in fp


# ---------------------------------------------------------------------------
# The inversion guard -- do not trade a false block for a silent false admit.
# ---------------------------------------------------------------------------

def test_checks_without_outcomes_still_yield_co_signal():
    """A goal with checks and no outcomes must NOT lose all file-path co-signal;
    it would sail past duplicate detection entirely."""
    fp, src = _paths({
        "title": "Fix the ledger", "description": "x",
        "verification": {"checks": [f"run {PROBE} and confirm"]}})
    assert src == "verification"
    assert PROBE in fp


def test_empty_outcomes_list_behaves_like_absent_outcomes():
    fp, _ = _paths({
        "title": "Fix the ledger", "description": "x",
        "verification": {"outcomes": [], "checks": [f"run {PROBE}"]}})
    assert PROBE in fp


def test_prose_fallback_is_untouched():
    """No verification block at all -- the prose path must behave exactly as before."""
    fp, src = _paths({
        "title": f"Fix {PROBE}", "description": f"rewrite {PROBE} entirely",
        "verification": {}})
    assert src == "prose"
    assert PROBE in fp


# ---------------------------------------------------------------------------
# The no-op trap: this fix was nearly discarded by a later line.
# ---------------------------------------------------------------------------

def test_the_exclusion_survives_the_exclusion_context_filter():
    """The  filter that runs AFTER this one used to rebuild the set
    from file_paths_all, which would have silently discarded the probe-path
    filter and made the whole fix a no-op that reads as applied. This pin fails
    if any future filter re-sources from file_paths_all instead of narrowing.
    """
    fp, _ = _paths({
        "title": "Fix the selector",
        "description": "distinct from g-318-37",   # triggers the contrast path
        "verification": {"outcomes": [f"{TARGET} is fixed"],
                         "checks": [f"run {PROBE} to confirm"]}})
    assert PROBE not in fp
    assert TARGET in fp


def test_a_probe_path_does_not_re_enter_as_a_keyword_stem():
    """Excluding the path must remove its aboutness ENTIRELY, not shift it from
    file-path co-signal to keyword co-signal -- the same trap the strip loop was
    written to close for exclusion-context paths.
    """
    _fp, keywords, _src = gd._extract_signals({
        "title": "Fix the selector", "description": "x",
        "verification": {"outcomes": ["the selector is fixed"],
                         "checks": [f"run {PROBE} and confirm"]}})
    assert "ledger" not in keywords, "probe path leaked back in as a keyword stem"
    assert "trend" not in keywords
