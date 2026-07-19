"""test_skill_discovery_companion_scripts.py — regression test for .

Pins the rb-314 / g-115-798 fix: skill-discovery.py must count companion-
script invocations from per-agent execution-diary.jsonl files toward the
owning forged skill's invocation_count. Without this, infra-wrapper
forged skills (access-roblox-studio, access-operator-api, access-aws-
services, run-game-session) appear silently_undertriggering even though
their underlying capability is production-active — the brief from
zeta/reports/g-115-798-investigate-skill-discovery-silent-flags.md showed
26 roblox-studio.sh + roblox-bridge.py invocations over 30 days were
silently invisible to the skill-discovery counter.

Cases covered:
  1. companion script in diary → count increments for owning skill
  2. genuinely cold script (no diary hits) → count stays at 0 + status
     silently_undertriggering preserved (manage-roblox-scripts shape)
  3. companion script in BOARD ONLY (not diary) → count NOT incremented
     (board excluded by design per docstring; manage-roblox-scripts had
     1 historical board mention but the brief's acceptance criterion
     requires count to stay at 0)
  4. companion script shared across multiple skills → all owners credited
     (roblox-studio.sh belongs to BOTH access-roblox-studio AND
     run-game-session per forged-skills.yaml)
  5. same-second event in diary → dedup'd via set() upstream (single
     date in the final sorted list even when two records share a
     timestamp)

Run: py -3 -m pytest core/scripts/tests/test_skill_discovery_companion_scripts.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest.mock
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_module():
    """Load skill-discovery.py by spec — filename has hyphens."""
    spec = importlib.util.spec_from_file_location(
        "skill_discovery",
        CORE_SCRIPTS / "skill-discovery.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load skill-discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_diary(path: Path, records: list[dict]) -> None:
    """Write a list of records as JSONL to the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_companion_script_in_diary_increments_count(tmp_path: Path = None):
    """Case 1: a companion script appearing in execution-diary.jsonl
    increments the invocation_count of the owning forged skill."""
    mod = _load_module()
    tmpdir = Path(tempfile.mkdtemp(prefix="sd-companion-1-"))
    try:
        # alpha diary with two roblox-studio.sh invocations
        alpha_diary = tmpdir / "agents" / "alpha" / "session" / "execution-diary.jsonl"
        _write_diary(alpha_diary, [
            {"entry_type": "phase_start", "phase": "phase-4-execute",
             "content": "bash world/scripts/roblox-studio.sh start-bridge",
             "timestamp": "2026-05-01T10:00:00"},
            {"entry_type": "finding", "goal_id": "g-115-15",
             "content": "world/scripts/roblox-studio.sh start-session --duration 15",
             "timestamp": "2026-05-02T10:00:00"},
        ])

        forged = {
            "access-roblox-studio": {
                "companion_scripts": [
                    "world/scripts/roblox-bridge.py",
                    "world/scripts/roblox-studio.sh",
                ],
            },
        }
        with unittest.mock.patch.object(mod, "agents_root", lambda: tmpdir / "agents"), \
             unittest.mock.patch.object(mod, "WORLD_DIR", tmpdir / "world"):
            companion_dates = mod.collect_companion_script_dates(
                ["access-roblox-studio"], forged
            )

        assert "access-roblox-studio" in companion_dates
        dates = companion_dates["access-roblox-studio"]
        assert len(dates) == 2, (
            f"expected 2 invocations of roblox-studio.sh in alpha diary, got {len(dates)}: {dates}"
        )
        assert all(isinstance(d, datetime) for d in dates), \
            f"all entries must be datetime objects, got {[type(d).__name__ for d in dates]}"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_genuinely_cold_script_stays_at_zero():
    """Case 2: a companion script with no diary hits returns 0 invocations,
    preserving the silently_undertriggering / cold signal (canonical
    incident: manage-roblox-scripts at 0 in brief §2 diary table)."""
    mod = _load_module()
    tmpdir = Path(tempfile.mkdtemp(prefix="sd-companion-2-"))
    try:
        # alpha diary with NO roblox-manage-script.sh invocations
        alpha_diary = tmpdir / "agents" / "alpha" / "session" / "execution-diary.jsonl"
        _write_diary(alpha_diary, [
            {"entry_type": "finding",
             "content": "something unrelated to roblox-manage-script",
             "timestamp": "2026-05-01T10:00:00"},
        ])

        forged = {
            "manage-roblox-scripts": {
                "companion_scripts": ["world/scripts/roblox-manage-script.sh"],
            },
        }
        with unittest.mock.patch.object(mod, "agents_root", lambda: tmpdir / "agents"), \
             unittest.mock.patch.object(mod, "WORLD_DIR", tmpdir / "world"):
            companion_dates = mod.collect_companion_script_dates(
                ["manage-roblox-scripts"], forged
            )

        dates = companion_dates.get("manage-roblox-scripts", [])
        assert len(dates) == 0, (
            f"expected 0 invocations for cold script, got {len(dates)}: {dates}"
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_board_mentions_do_not_count():
    """Case 3: a companion script mentioned ONLY in
    world/board/coordination.jsonl (and NOT in any execution diary) must
    NOT increment the count. The brief explicitly recommends diary-only
    counting because board posts can mention a script in discussion
    ("we should retire X") without invoking it. manage-roblox-scripts had
    1 historical board mention; if counted, the brief's "stays at 0"
    acceptance breaks."""
    mod = _load_module()
    tmpdir = Path(tempfile.mkdtemp(prefix="sd-companion-3-"))
    try:
        # Empty diary
        alpha_diary = tmpdir / "agents" / "alpha" / "session" / "execution-diary.jsonl"
        _write_diary(alpha_diary, [])

        # Board mention only
        board_path = tmpdir / "world" / "board" / "coordination.jsonl"
        _write_diary(board_path, [
            {"id": "msg-001", "author": "bravo", "channel": "coordination",
             "text": "we should review roblox-manage-script.sh — possibly retire",
             "timestamp": "2026-05-01T10:00:00"},
        ])

        forged = {
            "manage-roblox-scripts": {
                "companion_scripts": ["world/scripts/roblox-manage-script.sh"],
            },
        }
        with unittest.mock.patch.object(mod, "agents_root", lambda: tmpdir / "agents"), \
             unittest.mock.patch.object(mod, "WORLD_DIR", tmpdir / "world"):
            companion_dates = mod.collect_companion_script_dates(
                ["manage-roblox-scripts"], forged
            )

        dates = companion_dates.get("manage-roblox-scripts", [])
        assert len(dates) == 0, (
            f"board mention must NOT count toward invocation_count; "
            f"got {len(dates)} invocations: {dates}"
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_shared_companion_script_credits_all_skills():
    """Case 4: a single companion script that belongs to multiple forged
    skills increments the count for ALL owning skills. roblox-studio.sh
    is referenced by BOTH access-roblox-studio AND run-game-session per
    forged-skills.yaml — a single invocation must surface in both."""
    mod = _load_module()
    tmpdir = Path(tempfile.mkdtemp(prefix="sd-companion-4-"))
    try:
        alpha_diary = tmpdir / "agents" / "alpha" / "session" / "execution-diary.jsonl"
        _write_diary(alpha_diary, [
            {"entry_type": "finding",
             "content": "bash world/scripts/roblox-studio.sh start-session",
             "timestamp": "2026-05-01T10:00:00"},
        ])

        forged = {
            "access-roblox-studio": {
                "companion_scripts": [
                    "world/scripts/roblox-bridge.py",
                    "world/scripts/roblox-studio.sh",
                ],
            },
            "run-game-session": {
                "companion_scripts": [
                    "world/scripts/roblox-studio.sh",
                    "world/scripts/operator-api.sh",
                ],
            },
        }
        with unittest.mock.patch.object(mod, "agents_root", lambda: tmpdir / "agents"), \
             unittest.mock.patch.object(mod, "WORLD_DIR", tmpdir / "world"):
            companion_dates = mod.collect_companion_script_dates(
                ["access-roblox-studio", "run-game-session"], forged
            )

        assert len(companion_dates["access-roblox-studio"]) == 1, (
            f"access-roblox-studio should get credit for the shared script; "
            f"got {len(companion_dates['access-roblox-studio'])}"
        )
        assert len(companion_dates["run-game-session"]) == 1, (
            f"run-game-session should also get credit for the shared script; "
            f"got {len(companion_dates['run-game-session'])}"
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_same_second_events_dedup_upstream():
    """Case 5: same-second invocations are dedup'd by set() in
    collect_invocation_dates. The raw collect_companion_script_dates may
    return duplicates (it does not dedup itself), but the upstream caller
    collapses them. Verify the upstream end-to-end path by feeding two
    identical-timestamp records and asserting collect_invocation_dates
    returns a single date for the skill.

    Also pins the new 'companion_script' source key in the sources dict
    so a future refactor cannot silently drop it without failing this
    test."""
    mod = _load_module()
    tmpdir = Path(tempfile.mkdtemp(prefix="sd-companion-5-"))
    try:
        alpha_diary = tmpdir / "agents" / "alpha" / "session" / "execution-diary.jsonl"
        # Two records, identical timestamps — represent the same execution
        # journaled twice (e.g., once by phase-start and once by a finding
        # in the same second).
        _write_diary(alpha_diary, [
            {"entry_type": "phase_start",
             "content": "world/scripts/aws-exec.sh GET /status",
             "timestamp": "2026-05-01T10:00:00"},
            {"entry_type": "finding",
             "content": "aws-exec.sh returned 200",
             "timestamp": "2026-05-01T10:00:00"},
        ])

        forged = {
            "access-aws-services": {
                "companion_scripts": ["world/scripts/aws-exec.sh"],
            },
        }
        with unittest.mock.patch.object(mod, "agents_root", lambda: tmpdir / "agents"), \
             unittest.mock.patch.object(mod, "WORLD_DIR", tmpdir / "world"):
            companion_dates = mod.collect_companion_script_dates(
                ["access-aws-services"], forged
            )
            dates, sources = mod.collect_invocation_dates(
                "access-aws-services",
                quality_data={},
                relations_data={},
                journal_dates={"access-aws-services": []},
                companion_dates=companion_dates,
                ledger_dates={"access-aws-services": []},
            )

        assert "companion_script" in sources, (
            f"'companion_script' must be a key in sources dict; got {sources}"
        )
        assert sources["companion_script"] == 2, (
            f"sources['companion_script'] tracks raw counts before dedup; "
            f"expected 2 (both records), got {sources['companion_script']}"
        )
        assert len(dates) == 1, (
            f"same-second events must dedup to a single date in the final "
            f"sorted list; got {len(dates)}: {dates}"
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_init_meta_seeds_skill_discovery_strategy():
    """skill-discovery.py + mind_api/src/endpoints/skill_discovery.py hard-REQUIRE
    meta/skill-discovery-strategy.yaml ("DO NOT add fallback defaults"; CLI exit 3
    / raise when absent), so init-meta.sh MUST seed it — else fresh-box init
    FileNotFoundErrors the moment aspirations-evolve Step 9.5.5 runs skill-discovery.

    Init-seed-COVERAGE assertion (rb init-seed-parity, echo-2744 / g-328-26): the
    fixture tests prove the CONSUMER reads the yaml, NOT that a fresh clone HAS it.
    This is the SECOND gap the systematic audit found after cognitive-horizons.yaml
    (test_precheck_cognitive_horizons.py carries the twin assertion).
    """
    core = CORE_SCRIPTS.parent  # core/
    # 1. The git-tracked seed SOURCE exists.
    src = core / "config" / "skill-discovery-strategy.yaml"
    assert src.exists(), f"seed source missing: {src}"
    # 2. init-meta.sh actually copies it into meta/ (cp from $CONFIG to $META).
    body = (CORE_SCRIPTS / "init-meta.sh").read_text(encoding="utf-8")
    assert 'cp "$CONFIG/skill-discovery-strategy.yaml" "$META/skill-discovery-strategy.yaml"' in body, (
        "init-meta.sh does not seed skill-discovery-strategy.yaml — fresh-box init "
        "would FileNotFoundError / exit-3 in skill-discovery"
    )


def main():
    """Manual test runner for environments without pytest."""
    import traceback
    tests = [
        ("companion_script_in_diary_increments_count", test_companion_script_in_diary_increments_count),
        ("genuinely_cold_script_stays_at_zero", test_genuinely_cold_script_stays_at_zero),
        ("board_mentions_do_not_count", test_board_mentions_do_not_count),
        ("shared_companion_script_credits_all_skills", test_shared_companion_script_credits_all_skills),
        ("same_second_events_dedup_upstream", test_same_second_events_dedup_upstream),
        ("init_meta_seeds_skill_discovery_strategy", test_init_meta_seeds_skill_discovery_strategy),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failed.append(name)

    if failed:
        print(f"\n{len(failed)}/{len(tests)} test(s) failed: {failed}")
        return 1
    print(f"\n{len(tests)}/{len(tests)} test(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
