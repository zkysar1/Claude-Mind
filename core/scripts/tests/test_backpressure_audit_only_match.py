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


# ── the FILE half of the same expression () ────────────────────────
#
#  fixed the FIELD lookup and  ported it to the daemon. Both
# left the FILE lookup an EXACT dict get:
#
#     file_audit = audit_only_fields.get(monitor["strategy_file"], []) or []
#
# `strategy_file` is stored VERBATIM from whatever the registrar was handed.
# meta-yaml.py::cmd_set — the live registrar — decides a file is a strategy file
# with a SUBSTRING test (`any(s in args.file for s in [...])`) and then passes
# `args.file` through unchanged, so `meta-yaml.sh set --file meta/reflection-
# strategy.yaml ...` registers the PREFIXED key. The daemon's `body["file"]` path
# normalizes nothing either.
#
# MEASURED, live store, 2026-08-17 (zeta, cc-02), all 38 rollback_history records
# read uncapped: ONE file appears under TWO spellings —
#
#     8x  'goal-selection-strategy.yaml'
#     2x  'meta/goal-selection-strategy.yaml'
#
# So both spellings genuinely reach this gate; the prefixed form is not
# hypothetical. What is NOT claimed: that a record has already been destroyed by
# it. goal-selection-strategy.yaml is absent from the allowlist, so those two
# rollbacks were correct either way. The defect is LATENT and one registration
# away — a `meta/`-prefixed monitor on an ALLOWLISTED file (reflection-strategy
# .yaml, step-attribution.yaml) resolves to `[]` and re-opens the exact
# destroy-the-evidence class the two prior goals closed, with audit_only_skips
# sitting at 0 the whole time. That counter is the same silent-zero tell as the
# 99-day miss documented at the top of this file (guard-1093).

# The live allowlist keys, verbatim from core/config/meta.yaml (fields trimmed —
# only the KEY shape is under test here).
FILE_ALLOWLIST = {
    "reflection-strategy.yaml": ["roi_history", "reflection_quality_log"],
    "step-attribution.yaml": ["step_attribution"],
}


@pytest.mark.parametrize("stored", [
    "reflection-strategy.yaml",       # exact — must stay byte-identical
    "meta/reflection-strategy.yaml",  # the live registrar's prefixed shape
    "./meta/reflection-strategy.yaml",
    "/abs/path/to/meta/reflection-strategy.yaml",
    "meta\\reflection-strategy.yaml",  # Windows separator — os.path.basename
                                       # does NOT split this on POSIX
])
def test_allowlist_resolves_regardless_of_path_shape(stored):
    """Every spelling of one file must reach that file's allowlist."""
    assert MOD._audit_allowlist_for(stored, FILE_ALLOWLIST) == [
        "roi_history", "reflection_quality_log"]


def test_exact_key_wins_before_any_basename_scan():
    """Back-compat: an exact hit must not be re-resolved through the scan.

    Two config keys can share a basename. The exact key is the caller's stated
    intent and must win, or this fix would silently re-point existing monitors.
    """
    cfg = {
        "meta/reflection-strategy.yaml": ["prefixed_only"],
        "reflection-strategy.yaml": ["bare_only"],
    }
    assert MOD._audit_allowlist_for("meta/reflection-strategy.yaml", cfg) == ["prefixed_only"]
    assert MOD._audit_allowlist_for("reflection-strategy.yaml", cfg) == ["bare_only"]


@pytest.mark.parametrize("stored", [
    "goal-selection-strategy.yaml",       # live, and deliberately NOT allowlisted
    "meta/goal-selection-strategy.yaml",  # live, prefixed, also not allowlisted
    "encoding-strategy.yaml",
    "meta/reflection-strategy.yaml.bak",
    "reflection-strategy.yml",            # different extension is a different file
    "xreflection-strategy.yaml",
    "meta/",
    "",
])
def test_unallowlisted_files_get_nothing(stored):
    """Negative control: over-matching here disables backpressure per-FILE.

    A predicate that returned the first allowlist for anything would pass every
    positive test above while making every tunable in the repo unrollbackable.
    """
    assert MOD._audit_allowlist_for(stored, FILE_ALLOWLIST) == []


@pytest.mark.parametrize("stored", [None, 123, ["reflection-strategy.yaml"], {"a": 1}])
def test_malformed_strategy_file_returns_empty_never_raises(stored):
    """Same reachability argument as the field half: backpressure.yaml is a
    shared own-cloud store with a merge handler, so a null field is reachable,
    and one raise aborts the cycle for EVERY monitor including live rollbacks."""
    assert MOD._audit_allowlist_for(stored, FILE_ALLOWLIST) == []


@pytest.mark.parametrize("cfg", [None, [], "reflection-strategy.yaml", 7])
def test_malformed_config_returns_empty(cfg):
    """Absent/garbled audit_only_fields -> legacy unconditional-rollback."""
    assert MOD._audit_allowlist_for("meta/reflection-strategy.yaml", cfg) == []


def test_non_list_allowlist_value_is_ignored():
    """A scalar under a key is not an allowlist; matching it would put a string
    into `field in file_audit` and silently substring-match field names."""
    assert MOD._audit_allowlist_for(
        "meta/reflection-strategy.yaml",
        {"reflection-strategy.yaml": "roi_history"}) == []


def test_prefixed_registration_is_skipped_end_to_end(monkeypatch, capsys):
    """THE REGRESSION. Pre-fix this rolled back and destroyed the record.

    Identical to test_indexed_element_is_skipped_end_to_end except the monitor
    registered the path the live registrar actually hands over.
    """
    result, monitor, state = _run_check(
        monkeypatch, capsys, "roi_history[81]",
        strategy_file="meta/reflection-strategy.yaml")

    assert monitor["status"] == "audit_only_skipped"
    assert result["rollback_actions"] == []
    assert len(result["audit_only_skipped"]) == 1
    assert len(state["rollback_history"]) == 0
    assert result["audit_only_skipped"][0]["strategy_file"] == "meta/reflection-strategy.yaml", (
        "the recorded path must be the ACTUAL registered spelling — "
        "audit_only_skips is the recovery source, so it must not be normalized")


def test_prefixed_tunable_still_rolls_back(monkeypatch, capsys):
    """Negative control end-to-end: resolving the allowlist must not make the
    whole FILE exempt. A tunable under the prefixed spelling still rolls back."""
    result, monitor, state = _run_check(
        monkeypatch, capsys, "depth_allocation.max_depth",
        strategy_file="meta/reflection-strategy.yaml")
    assert monitor["status"] == "rolled_back"
    assert len(result["rollback_actions"]) == 1
    assert len(state["rollback_history"]) == 1


FILE_PARITY_CORPUS = [
    "reflection-strategy.yaml", "meta/reflection-strategy.yaml",
    "./meta/reflection-strategy.yaml", "meta\\reflection-strategy.yaml",
    "/abs/meta/step-attribution.yaml", "step-attribution.yaml",
    "goal-selection-strategy.yaml", "meta/goal-selection-strategy.yaml",
    "reflection-strategy.yml", "meta/", "", None, 123,
]


@pytest.mark.parametrize("stored", FILE_PARITY_CORPUS)
def test_cli_and_daemon_file_lookup_agree(stored):
    """The daemon is the LIVE path (no-python-cli-fallback.md). A CLI-only fix
    here is inert in production — which is precisely how the FIELD half of this
    same expression stayed broken after g-115-4552 (guard-2323)."""
    dmod = _import_daemon()
    cli = MOD._audit_allowlist_for(stored, FILE_ALLOWLIST)
    daemon = dmod._audit_allowlist_for(stored, FILE_ALLOWLIST)
    assert cli == daemon, (
        f"CLI/daemon file-lookup divergence on {stored!r}: "
        f"CLI={cli} daemon={daemon}")


def test_both_call_sites_route_through_the_file_helper():
    """Symbol-present is not call-site-wired — the same one-layer-in shape the
    field-half test above guards, now for the file half, on BOTH copies."""
    root = SCRIPTS_DIR.parent.parent
    for path, var in (
        (SCRIPTS_DIR / "meta-backpressure.py", "monitor"),
        (root / "mind_api" / "src" / "meta" / "meta_backpressure.py", "mon"),
    ):
        src = path.read_text(encoding="utf-8")
        assert f'_audit_allowlist_for({var}["strategy_file"], audit_only_fields)' in src, (
            f"{path.name} no longer routes the file lookup through the helper")
        assert f'audit_only_fields.get({var}["strategy_file"]' not in src, (
            f"{path.name}: the exact-match dict lookup is back at the call site")
