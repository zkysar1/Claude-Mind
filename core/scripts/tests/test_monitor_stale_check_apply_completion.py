#!/usr/bin/env python3
"""Behavioral regression pins for monitor-stale-check.py::_apply_completion ().

THE DEFECT THIS PINS. `_apply_completion` built its argv as
`[aspirations.py, "update-goal", "--source", src, ...]`. `--source` is registered on
aspirations.py's TOP-LEVEL parser, not on the `update-goal` subparser, so that order
exits rc=2 ("unrecognized arguments") before any goal lookup runs. The function checks
`if rc != 0: return False`, so it ALWAYS reported failure and the sweep could never
auto-complete a superseded Monitor goal. Live path: aspirations-precheck Phase 0 runs
`monitor-stale-check.sh --apply` every iteration.

WHY THESE TESTS DRIVE THE REAL SUBPROCESS. The goal's own instruction was that an
arg-order SHAPE assertion would re-admit the same class — a test asserting
`argv[1] == "--source"` passes against any argv the author happens to write, and
encodes the author's belief about argparse rather than argparse's behavior. So
test_apply_completion_flips_status_in_tmp_world runs `_apply_completion` end to end
against a seeded tmp world and asserts the STATUS ACTUALLY FLIPPED on disk. It goes RED
if the argv order regresses, and it would also go red if aspirations.py moved `--source`
onto the subparser — which is the correct behavior for a contract pin.

test_source_after_subcommand_is_an_argparse_error pins the argparse contract itself, so
that if the ordering requirement ever DISAPPEARS this file tells you the pin is now
vacuous rather than silently passing forever (guard-1760).

STORAGE_BACKEND: conftest.py pins `local` for the whole pytest session and subprocesses
inherit os.environ, so the tmp-world writes here cannot collide on a production S3 key
(guard-955 / rb-2983). This module re-pins it explicitly so it is also safe when run
main()-style outside pytest, where that conftest never loads.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent

# Safe when run outside pytest too — see module docstring.
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ["STORAGE_BACKEND"] = "local"


def _load_module():
    """Import monitor-stale-check.py (hyphenated filename -> spec loader)."""
    path = CORE_SCRIPTS / "monitor-stale-check.py"
    spec = importlib.util.spec_from_file_location("monitor_stale_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_world(world_dir: Path, goal_id: str) -> None:
    """Write a minimal aspirations.jsonl carrying one pending Monitor goal."""
    asp = {
        "id": "asp-999",
        "title": "tmp fixture aspiration",
        "status": "active",
        "priority": "MEDIUM",
        "source": "test",
        "scope": "test",
        "motivation": "fixture",
        "tags": [],
        "progress": {},
        "goals": [
            {
                "id": goal_id,
                "title": "Monitor: proc-1700000000 fixture run",
                "description": "fixture",
                "status": "pending",
                "priority": "MEDIUM",
                "participants": ["agent"],
                "category": "framework-maintenance",
                "created_at": "2026-08-01T00:00:00",
                "last_modified": "2026-08-01T00:00:00",
            }
        ],
    }
    (world_dir / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8"
    )


def _read_goal(world_dir: Path, goal_id: str) -> dict:
    for line in (world_dir / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals") or []:
            if g.get("id") == goal_id:
                return g
    raise AssertionError(f"goal {goal_id} vanished from the tmp world")


def test_apply_completion_flips_status_in_tmp_world():
    """END-TO-END: the real subprocess must run and the status must change on disk.

    This is the assertion the arg-order bug fails. Mocking _py here would defeat the
    entire point — the bug lives in what argparse does with the argv, so the argv has
    to reach a real argparse.
    """
    mod = _load_module()
    goal_id = "g-999-01"
    prev_world = os.environ.get("MIND_WORLD")
    prev_agent = os.environ.get("MIND_AGENT")
    with tempfile.TemporaryDirectory(prefix="monitor-stale-apply-") as td:
        world = Path(td)
        _seed_world(world, goal_id)
        # Scoped to this test and restored in `finally` — an import-time mutation
        # leaks into every later-collected module (see test_applies_to_required.py).
        os.environ["MIND_WORLD"] = str(world)
        os.environ.pop("MIND_AGENT", None)
        try:
            ok, detail = mod._apply_completion(
                {"id": goal_id, "_source": "world"}, "proc-1800000000"
            )

            # PRIMARY, HERMETIC ASSERTION — this is the defect's exact signature and
            # it holds regardless of the state of the surrounding repo. Under the bug
            # the argv never reached the business logic at all: argparse rejected it
            # and wrote "unrecognized arguments: --source world" to stderr, which
            # _apply_completion returns as `detail`.
            assert "unrecognized arguments" not in (detail or ""), (
                f"argv was rejected by argparse — --source is being passed after the "
                f"subcommand again: {detail!r}"
            )

            # SECONDARY ASSERTION — the full disk flip. aspirations.py's
            # uncommitted-work gate scans the REAL framework repo (CORE_ROOT-derived,
            # with no env seam to redirect it), so this leg cannot be made hermetic:
            # a dirty working tree legitimately refuses the close. Skip LOUDLY rather
            # than conditionally asserting, so a reader can never mistake a skipped
            # leg for a passed one.
            if not ok and "uncommitted framework code" in (detail or ""):
                import pytest
                pytest.skip(
                    "disk-flip leg not run: aspirations.py's uncommitted-work gate "
                    "refused the close because THIS repo's working tree is dirty. "
                    "The primary argparse assertion above still ran and passed — "
                    "reaching that gate is itself proof the argv got past argparse. "
                    "Commit the tree to exercise this leg."
                )

            assert ok is True, f"_apply_completion reported failure: {detail!r}"
            goal = _read_goal(world, goal_id)
            assert goal["status"] == "completed", (
                f"status did not flip on disk: {goal['status']!r}"
            )
            # Second call site carries the same defect independently — pin it too,
            # or a half-fix passes.
            assert "superseded-by-newer-run" in (goal.get("outcome_note") or ""), (
                "outcome_note was not written — the SECOND _py call site is still "
                "passing --source after the subcommand"
            )
        finally:
            if prev_world is None:
                os.environ.pop("MIND_WORLD", None)
            else:
                os.environ["MIND_WORLD"] = prev_world
            if prev_agent is not None:
                os.environ["MIND_AGENT"] = prev_agent


def test_source_after_subcommand_is_an_argparse_error():
    """Contract pin: the ordering requirement that makes the test above meaningful.

    If aspirations.py ever registers --source on the update-goal subparser, this test
    goes RED — telling you the pin above has become vacuous, rather than letting it
    pass forever against a requirement that no longer exists.
    """
    argv_base = [sys.executable, str(CORE_SCRIPTS / "aspirations.py")]
    tail = ["__nonexistent_goal__", "status", "completed"]

    bad = subprocess.run(
        argv_base + ["update-goal", "--source", "world"] + tail,
        capture_output=True, text=True,
    )
    assert bad.returncode == 2, (
        f"--source AFTER the subcommand no longer errors (rc={bad.returncode}). "
        f"The ordering requirement changed; re-derive whether the fix is still needed."
    )

    good = subprocess.run(
        argv_base + ["--source", "world", "update-goal"] + tail,
        capture_output=True, text=True,
    )
    assert good.returncode != 2, (
        "--source BEFORE the subcommand is now an argparse error — the correct form "
        "regressed"
    )


def test_no_call_site_passes_source_after_subcommand():
    """Cheap whole-file guard: catches a THIRD call site added later in the wrong order.

    Deliberately secondary to the behavioral test — this is the shape assertion the
    goal warns is insufficient alone, kept only as breadth over call sites the
    end-to-end test does not exercise.
    """
    src = (CORE_SCRIPTS / "monitor-stale-check.py").read_text(encoding="utf-8")
    # Strip comments first — the prose above deliberately quotes the bad ordering.
    src = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    # Whitespace-NORMALIZED token match, not a literal with baked-in indentation.
    # The first version of this assertion hardcoded exactly 12 spaces between the
    # two tokens, so a call site added at any other nesting depth would have
    # slipped past silently — a shape guard that cannot see the shape is worse
    # than no guard, because it reads as coverage. Found by the fresh-eyes pass
    # on this very file.
    normalized = " ".join(src.split())
    assert '"update-goal", "--source"' not in normalized, (
        "a call site still passes --source after the update-goal subcommand"
    )


if __name__ == "__main__":
    test_apply_completion_flips_status_in_tmp_world()
    test_source_after_subcommand_is_an_argparse_error()
    test_no_call_site_passes_source_after_subcommand()
    print("3 passed")
