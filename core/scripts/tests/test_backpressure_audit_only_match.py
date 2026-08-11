"""Regression pins for the audit_only_fields exact-match bypass ().

DEFECT (measured 2026-08-02, live store). meta-backpressure.cmd_check gated
rollbacks with an EXACT string test:

    file_audit = audit_only_fields.get(monitor["strategy_file"], []) or []
    if monitor["field"] in file_audit:

The allowlist (core/config/meta.yaml -> strategy_schemas.backpressure.
audit_only_fields) was correctly configured with the BASE key `roi_history`,
but monitors register the shape actually WRITTEN. Four such shapes reached the
gate in the 99 days after the allowlist shipped (2026-04-25) and NONE matched:

    mc-073  roi_history_note_20260716   2026-07-16T14:37:10
    mc-079  roi_history_note            2026-07-18T05:44:00
    mc-309  roi_history[82]             2026-07-31T16:25:32
    mc-303  roi_history[81]             2026-08-01T00:21:38

All four were append-only ROI observability records (verified from
rollback_history[].failed_value — two full dicts with `note` bodies of 1,242
and 1,983 chars, two ROI note strings). Rolling them back destroyed evidence
and restored no performance, which is the exact reasoning that motivated the
allowlist (g-115-204 / rb-504).

WHY THE NARROW FIX IS WRONG. Stripping a trailing `[N]`/`.N` — the shape this
goal originally proposed — catches mc-303 and mc-309 and STILL misses mc-073
and mc-079. Hence prefix-with-boundary, pinned below by all four live shapes.

WHY IT STAYED INVISIBLE FOR 99 DAYS. `audit_only_skips` sat at 0, and that
counter is exactly what a never-matching allowlist produces. It was read as
"nothing needed skipping" and used as the resolved_evidence that closed the
watcher (wk-001 in agents/bravo/weakness-report.yaml, closed 2026-04-25),
which switched off the only observer of the next four rollbacks. A zero from
a predicate that never fires is indistinguishable from a zero from a clean
queue unless you run the predicate against the cases it failed to catch
(guard-1093) — which is what test_live_shapes_that_slipped_past does.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "meta-backpressure.py"


def _import():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("meta_backpressure_aom", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_backpressure_aom"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()

# The live allowlist, verbatim from core/config/meta.yaml.
ALLOWLIST = ["roi_history", "reflection_quality_log"]

# The four shapes that actually reached the gate and were NOT caught.
LIVE_SHAPES_THAT_SLIPPED = [
    ("roi_history_note_20260716", "mc-073"),
    ("roi_history_note", "mc-079"),
    ("roi_history[82]", "mc-309"),
    ("roi_history[81]", "mc-303"),
]


# ── predicate-level pins ─────────────────────────────────────────────────────

@pytest.mark.parametrize("field,change_id", LIVE_SHAPES_THAT_SLIPPED)
def test_live_shapes_that_slipped_past(field, change_id):
    """Every shape that reached the gate in the wild must now match.

    These are not synthetic fixtures — each is a field name read from
    meta/backpressure.yaml rollback_history, with the meta_change_id that
    destroyed the record.
    """
    assert MOD._is_audit_only_field(field, ALLOWLIST) is True, (
        f"{change_id} ({field}) would be rolled back again")


def test_exact_base_key_still_matches():
    """The pre-existing exact-match behavior is preserved, not replaced."""
    assert MOD._is_audit_only_field("roi_history", ALLOWLIST) is True
    assert MOD._is_audit_only_field("reflection_quality_log", ALLOWLIST) is True


def test_dotpath_child_matches():
    """`.` is a boundary — a dotpath child of an audit log is part of it."""
    assert MOD._is_audit_only_field("roi_history.note", ALLOWLIST) is True
    assert MOD._is_audit_only_field("roi_history[3].note", ALLOWLIST) is True


@pytest.mark.parametrize("field", [
    "roi_historyX",          # prefix does not end on a boundary
    "roi_historical_data",   # diverges mid-token ('y' vs 'i')
    "step_attributions",     # the swallow case the boundary rule exists for
    "weights.opportunity_boost",
    "selection_heuristics[3].description",
    "",
])
def test_unrelated_fields_are_not_swallowed(field):
    """A tunable must stay rollback-eligible — over-matching disables backpressure.

    `step_attributions` is the canonical case: it starts with the allowlisted
    `step_attribution` and is a different field. `selection_heuristics[3]` is
    live in rollback_history and is a genuine TUNABLE (g-115-4114) whose
    rollbacks are legitimate by design.
    """
    allowlist = ALLOWLIST + ["step_attribution"]
    assert MOD._is_audit_only_field(field, allowlist) is False


@pytest.mark.parametrize("field", [None, 123, ["roi_history"], {"a": 1}])
def test_malformed_field_returns_false_never_raises(field):
    """A bad monitor must not abort the whole check ( fresh-eyes).

    The predicate this replaced was `field in file_audit`, which returns False
    for a non-str field. startswith raises. cmd_check has no try/except around
    the call, so one malformed monitor would skip EVERY monitor in the run —
    including legitimate rollbacks. backpressure.yaml is a shared own-cloud
    store with a merge handler, so a null field is reachable.
    """
    assert MOD._is_audit_only_field(field, ALLOWLIST) is False


@pytest.mark.parametrize("allowlist", [[None], [123], [None, "roi_history"]])
def test_malformed_allowlist_entry_does_not_raise(allowlist):
    """A non-str allowlist entry raises TypeError inside startswith."""
    result = MOD._is_audit_only_field("roi_history[81]", allowlist)
    assert result is ("roi_history" in allowlist)


def test_empty_allowlist_key_does_not_over_match():
    """"" is a prefix of everything, so an empty key would swallow any field
    starting with a boundary char — disabling backpressure for that file."""
    assert MOD._is_audit_only_field("_anything", [""]) is False
    assert MOD._is_audit_only_field("[0]", [""]) is False
    assert MOD._is_audit_only_field("", [""]) is False


def test_empty_allowlist_matches_nothing():
    """Missing/empty config -> legacy unconditional-rollback behavior."""
    assert MOD._is_audit_only_field("roi_history", []) is False
    assert MOD._is_audit_only_field("roi_history[81]", []) is False


def test_boundary_set_is_a_tuple_not_a_string():
    """`"" in "[._"` is True under substring semantics.

    If _AUDIT_FIELD_BOUNDARY were a string, the empty slice taken at the exact
    match position would test True and every prefix would match, swallowing
    every tunable in an allowlisted file. Pinning the type pins the semantics.
    """
    assert isinstance(MOD._AUDIT_FIELD_BOUNDARY, tuple)
    assert "" not in MOD._AUDIT_FIELD_BOUNDARY


# ── end-to-end through cmd_check ─────────────────────────────────────────────

def _run_check(monkeypatch, capsys, field, strategy_file="reflection-strategy.yaml"):
    """Drive cmd_check to the regression branch for one monitor on `field`.

    consecutive_below_baseline starts at 4 so this single check reaches the
    regression_window of 5. learning_value 0.0 is below baseline 0.5 - 0.10.
    """
    monitor = {
        "meta_change_id": "mc-test",
        "strategy_file": strategy_file,
        "field": field,
        "old_value": None,
        "new_value": {"note": "append-only ROI record"},
        "baseline_imp_k": 0.5,
        "goals_since_change": 0,
        "imp_k_samples": [],
        "consecutive_below_baseline": 4,
        "consecutive_above_baseline": 0,
        "status": "monitoring",
        "created": "2026-08-02T00:00:00",
    }
    state = {"version": "1.0", "active_monitors": [monitor],
             "rollback_history": [], "audit_only_skips": []}

    monkeypatch.setattr(MOD, "ensure_state", lambda: state)
    monkeypatch.setattr(MOD, "write_yaml", lambda p, d: None)
    monkeypatch.setattr(MOD, "read_yaml", lambda p: {"entries": []})
    monkeypatch.setattr(MOD, "load_config", lambda: {
        "regression_window": 5,
        "graduation_window": 15,
        "baseline_tolerance": -0.10,
        "audit_only_fields": {
            "reflection-strategy.yaml": ["roi_history", "reflection_quality_log"],
            "step-attribution.yaml": ["step_attribution"],
        },
    })

    MOD.cmd_check(argparse.Namespace(learning_value=0.0))
    return json.loads(capsys.readouterr().out), monitor, state


def test_indexed_element_is_skipped_end_to_end(monkeypatch, capsys):
    """The goal's outcome 1: demonstrated by a test, not by inspection."""
    result, monitor, state = _run_check(monkeypatch, capsys, "roi_history[81]")

    assert monitor["status"] == "audit_only_skipped"
    assert result["rollback_actions"] == []
    assert len(result["audit_only_skipped"]) == 1
    assert len(state["rollback_history"]) == 0, (
        "an audit-only skip must not enter rollback_history")

    skip = result["audit_only_skipped"][0]
    assert skip["field"] == "roi_history[81]", (
        "the recorded field must be the ACTUAL indexed name, not the base key "
        "— rollback_history/audit_only_skips are the recovery source")
    assert skip["failed_value"] == {"note": "append-only ROI record"}


def test_suffixed_note_is_skipped_end_to_end(monkeypatch, capsys):
    """mc-073/mc-079's shape — the half a strip-trailing-[N] fix would miss."""
    result, monitor, _ = _run_check(monkeypatch, capsys, "roi_history_note")
    assert monitor["status"] == "audit_only_skipped"
    assert result["rollback_actions"] == []


def test_tunable_in_same_file_still_rolls_back(monkeypatch, capsys):
    """Negative control: the fix must not disable backpressure wholesale.

    Without this, a predicate that returned True unconditionally would pass
    every other test in this file.
    """
    result, monitor, state = _run_check(
        monkeypatch, capsys, "depth_allocation.max_depth")
    assert monitor["status"] == "rolled_back"
    assert len(result["rollback_actions"]) == 1
    assert result["audit_only_skipped"] == []
    assert len(state["rollback_history"]) == 1


def test_non_allowlisted_file_still_rolls_back(monkeypatch, capsys):
    """A file absent from the allowlist is unaffected regardless of field name."""
    result, monitor, _ = _run_check(
        monkeypatch, capsys, "roi_history[81]",
        strategy_file="goal-selection-strategy.yaml")
    assert monitor["status"] == "rolled_back"
    assert len(result["rollback_actions"]) == 1


# ── CLI <-> DAEMON parity () ───────────────────────────────────────
#
# EVERY TEST ABOVE PASSED FOR THE ENTIRE TIME THE LIVE SYSTEM WAS BROKEN, and
# that is the reason this section exists.
#
#  shipped the boundary-matching predicate into
# core/scripts/meta-backpressure.py — the file this module imports. But
# meta-backpressure.sh is daemon-routed (.claude/rules/no-python-cli-fallback.md),
# so the LIVE path is mind_api/src/meta/meta_backpressure.py, which kept the old
# exact-match `mon["field"] in file_audit`. Measured 2026-08-08: audit_only_skips
# held ZERO entries for its entire lifetime while 21 roi_history[N] rollbacks
# fired. The suite above was green throughout, because it only ever exercised the
# copy that was already correct.
#
# Pinning one implementation says nothing about its twin (guard-1943's class,
# moved from writer-vs-wiring to copy-vs-copy). The two files may not import each
# other — Layer 1 must not import from core/scripts (core/BOUNDARY.md) — so
# divergence is structurally possible and only a parity assertion can catch it.

def _import_daemon():
    """Import the daemon twin. Skips (never fails) if the package cannot load."""
    repo_root = SCRIPTS_DIR.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from mind_api.src.meta import meta_backpressure as dmod
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"daemon module not importable here: {type(exc).__name__}: {exc}")
    return dmod


# One corpus, both predicates. Every shape below is drawn from the pins above,
# so the parity check cannot drift away from what those tests already assert.
PARITY_CORPUS = [f for f, _ in LIVE_SHAPES_THAT_SLIPPED] + [
    "roi_history", "reflection_quality_log",
    "roi_history.note", "roi_history[3].note", "roi_history[90]",
    "roi_historyX", "roi_historical_data", "step_attributions",
    "weights.opportunity_boost", "selection_heuristics[3].description",
    "", "last_updated",
]


@pytest.mark.parametrize("field", PARITY_CORPUS)
def test_cli_and_daemon_predicates_agree(field):
    """The daemon twin must return exactly what the CLI predicate returns.

    This is the assertion whose absence let the defect live: the CLI was fixed,
    the daemon was not, and nothing compared them.
    """
    dmod = _import_daemon()
    cli = MOD._is_audit_only_field(field, ALLOWLIST)
    daemon = dmod._is_audit_only_field(field, ALLOWLIST)
    assert cli == daemon, (
        f"CLI/daemon predicate divergence on {field!r}: "
        f"CLI={cli} daemon={daemon}. The daemon is the LIVE path — a divergence "
        f"here means production behaves differently from everything this file pins.")


@pytest.mark.parametrize("field", [None, 123, object()])
def test_cli_and_daemon_agree_on_malformed_input(field):
    """Parity must hold on the type-guard path too, not only on happy shapes.

    A malformed field is reachable: backpressure.yaml is a shared own-cloud store
    with a merge handler. Both copies must fail toward False rather than raise —
    one raising would abort the cycle and skip EVERY other monitor, including
    legitimate rollbacks.
    """
    dmod = _import_daemon()
    assert MOD._is_audit_only_field(field, ALLOWLIST) is False
    assert dmod._is_audit_only_field(field, ALLOWLIST) is False


def test_daemon_call_site_actually_uses_the_predicate():
    """The function existing in the daemon is NOT the same as it being CALLED.

    Porting the helper while leaving `mon["field"] in file_audit` at the call site
    would satisfy every parity test above and change nothing in production — the
    precise shape of the original defect, one layer in. So assert on the call site
    itself, not merely on the symbol.
    """
    src = (SCRIPTS_DIR.parent.parent / "mind_api" / "src" / "meta"
           / "meta_backpressure.py").read_text(encoding="utf-8")
    assert '_is_audit_only_field(mon["field"], file_audit)' in src, (
        "daemon cmd_check no longer routes through _is_audit_only_field")
    assert 'if mon["field"] in file_audit:' not in src, (
        "the pre-g-115-4552 exact-match predicate is back at the daemon call site")
