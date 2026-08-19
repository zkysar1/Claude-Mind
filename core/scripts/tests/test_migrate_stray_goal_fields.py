""" item 2 — dispositions for retiring unregistered goal fields.

`decide()` is the whole judgement of the migration: every destructive choice
routes through it, so it is tested rather than the file walk. The two tests that
matter most are the CLOBBER guards, because both were plans I actually held and
the data falsified:

  1. I intended to RENAME `created` -> `created_at` and `lastAchieved` ->
     `lastAchievedAt`. On all four live records the canonical field already held
     a NEWER value, so the rename would have overwritten it with a stale one. On
     the two recurring goals that means rewinding the last-achieved clock ~40
     days, which makes the cadence engine treat them as wildly overdue and fire
     them — a live behavioural consequence, not untidiness.

  2. I intended to RENAME the hyphen/camelCase strays into their obvious
     snake_case twins. `complete_by` / `schedule_type` / `desired_end_state` are
     NOT registered fields and are read by ZERO files under core/scripts and
     mind_api/src, so that rename would have minted a NEW stray while looking
     exactly like a fix — and the census would not have moved.

Both guards therefore live in code, and these tests are what keep them there
after the reasoning above has been forgotten.
"""
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import importlib.util  # noqa: E402

# The script's filename is kebab-case (house convention for scripts), so it is
# not importable by name — load it by path.

_spec = importlib.util.spec_from_file_location(
    "migrate_stray_goal_fields", _SCRIPTS / "migrate-stray-goal-fields.py")
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


@pytest.mark.parametrize("stray", ["__probe__", "__noop", "_probe"])
def test_content_free_probe_artifacts_are_dropped(stray):
    disp, _ = migrate.decide({"id": "g-1", stray: stray}, stray)
    assert disp == "drop"


def test_empty_value_is_dropped_not_folded():
    """Folding an empty string would append a marker announcing nothing."""
    disp, _ = migrate.decide({"id": "g-1", "complete-by": None}, "complete-by")
    assert disp == "drop"


def test_stale_duplicate_never_clobbers_a_live_canonical_value():
    """THE guard. Measured on /754: lastAchievedAt was ~40 days NEWER.

    A rename here rewinds a recurring goal's cadence clock and makes the engine
    fire it as overdue.
    """
    goal = {"id": "g-115-106", "status": "pending",
            "lastAchieved": "2026-07-09T09:16:56",
            "lastAchievedAt": "2026-08-17T15:25:53"}
    disp, reason = migrate.decide(goal, "lastAchieved")
    assert disp == "drop", f"would have clobbered a newer canonical value: {reason}"
    assert "STALE" in reason


def test_redundant_duplicate_is_dropped():
    goal = {"id": "g-1", "status": "pending",
            "created": "2026-04-20T17:00:00", "created_at": "2026-04-20T17:00:00"}
    assert migrate.decide(goal, "created")[0] == "drop"


def test_rename_is_refused_when_the_target_is_not_a_registered_field():
    """Otherwise the migration mints a new stray and the census does not move."""
    migrate.RENAME_TARGETS["__test_unregistered"] = "definitely_not_a_field"
    try:
        goal = {"id": "g-1", "status": "pending", "__test_unregistered": "x"}
        disp, reason = migrate.decide(goal, "__test_unregistered")
        assert disp == "fold"
        assert "not a registered field" in reason
    finally:
        migrate.RENAME_TARGETS.pop("__test_unregistered", None)


@pytest.mark.parametrize("status", ["completed", "skipped", "expired"])
def test_terminal_goals_fold_rather_than_rename(status):
    """A rename restores FUNCTION; a terminal goal has none left to restore.

    Measured on g-115-4869 (completed): renaming `defer_until` ->
    `deferred_until` would hand a defer date for finished work to any sweep that
    selects on the canonical field.
    """
    goal = {"id": "g-115-4869", "status": status, "defer_until": "2026-08-14"}
    disp, reason = migrate.decide(goal, "defer_until")
    assert disp == "fold", reason


def test_live_goal_with_absent_target_does_rename():
    """The control: without it, the guards above could pass by never renaming."""
    goal = {"id": "g-1", "status": "pending", "defer_until": "2026-08-14"}
    disp, reason = migrate.decide(goal, "defer_until")
    assert disp == "rename", reason
    assert "deferred_until" in reason


def test_unknown_stray_folds_rather_than_being_silently_retained():
    """decide() is TOTAL — a stray matching no rule must still be disposed of."""
    goal = {"id": "g-1", "status": "pending", "some_new_stray": "real content"}
    assert migrate.decide(goal, "some_new_stray")[0] == "fold"


def test_fold_text_carries_the_value_verbatim_and_an_idempotency_marker():
    """Re-running must not double-append; the marker is what makes that testable."""
    text = migrate._fold_text("execution_note", "VERIFIED five facts", "2026-08-18")
    assert "VERIFIED five facts" in text
    assert f"[{migrate.MARKER}:execution_note" in text


def test_non_string_values_survive_the_fold_as_json():
    """evidence_refs / prior_instances / related_reasoning are LISTS."""
    text = migrate._fold_text("evidence_refs", ["bible-9.7", "bible-14.11-P3"], "2026-08-18")
    assert "bible-9.7" in text and "bible-14.11-P3" in text


def test_terminal_statuses_are_imported_not_redefined():
    """A local copy is the drift item 1 of this goal exists to prevent."""
    from aspirations import TERMINAL_GOAL_STATUSES
    assert migrate.TERMINAL_STATUSES is TERMINAL_GOAL_STATUSES


def test_apply_without_an_archive_dir_is_refused():
    """archive-before-delete.md: deletion is the LAST step, never the first."""
    src = (_SCRIPTS / "migrate-stray-goal-fields.py").read_text(encoding="utf-8")
    assert "--apply requires --archive-dir" in src
    assert "RECEIPT.json" in src, "the receipt must be anchored at the archive top level"
