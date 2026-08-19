"""test_layer_d_telemetry.py — regression test for .

Asserts that aspirations.py cmd_update_goal logs a `capability-gate-layer-d`
firing to `{META_DIR}/gate-firings.jsonl` when the defer-time capability gate
matches AND the auto-Unblock filing succeeds. Telemetry enables the 7d audit
described in the goal: counting Layer-B (write-time block in
blocker-create-gate.py / capability-gate.py) firings vs Layer-D (defer-time
auto-route in cmd_update_goal) firings to validate the 4-Layer enforcement
pattern is live in production.

Three cases:
  1. Hit + filed → exactly one capability-gate-layer-d/block record with the
     full extras schema (filed_unblock_id, original_goal_id, matched_capability,
     target_aspiration).
  2. Miss (no capability match) → zero capability-gate-layer-d records.
  3. Override (--force-defer) → zero capability-gate-layer-d records (override
     bypasses the would_block branch entirely).

Pattern: ephemeral tmpdir per case, MIND_WORLD + MIND_META env-overrides
direct aspirations.py + _gate_log at the temp dirs. Reuses _build_fixture
shape from test_defer_to_unblock_integration.py so the gate's keyword sources
mirror production.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
ASP_PY = CORE_SCRIPTS / "aspirations.py"

sys.path.insert(0, str(CORE_SCRIPTS))
from _paths import agent_dir as _agent_dir  # noqa: E402


def _read_local_paths_world() -> Path | None:
    agent = os.environ.get("MIND_AGENT", "alpha")
    conf = _agent_dir(agent) / "local-paths.conf"
    if not conf.is_file():
        return None
    for raw in conf.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("WORLD_PATH="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            return Path(val) if val else None
    return None


def _build_fixture(tmp_world: Path) -> None:
    """Seed temp world with forged-skills.yaml + capability-routing.md (so
    the gate's keyword sources match production) plus a synthetic
    aspirations.jsonl."""
    live = _read_local_paths_world()
    if live and (live / "forged-skills.yaml").is_file():
        shutil.copy(live / "forged-skills.yaml", tmp_world / "forged-skills.yaml")
    conventions_dir = tmp_world / "conventions"
    conventions_dir.mkdir(parents=True, exist_ok=True)
    if live and (live / "conventions" / "capability-routing.md").is_file():
        shutil.copy(
            live / "conventions" / "capability-routing.md",
            conventions_dir / "capability-routing.md",
        )

    asps = [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": [
                {"id": "g-001-01", "title": "Reflect", "status": "pending",
                 "type": "recurring", "category": "meta", "recurring": True},
            ],
        },
        {
            "id": "asp-test",
            "title": "Test aspiration (layer-d telemetry fixture)",
            "status": "active",
            "goals": [
                {"id": "g-test-01",
                 "title": "Original work goal",
                 "description": "Some test goal we'll try to defer.",
                 "status": "pending",
                 "type": "idea",
                 "category": "framework-maintenance",
                 "priority": "MEDIUM",
                 "participants": ["agent"],
                 "origin_signal": "investigate:test"},
            ],
        },
    ]
    with (tmp_world / "aspirations.jsonl").open("w", encoding="utf-8") as f:
        for a in asps:
            f.write(json.dumps(a) + "\n")


def _run_update_goal(tmp_world: Path, tmp_meta: Path, goal_id: str,
                     field: str, value: str, *extra: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["MIND_WORLD"] = str(tmp_world)
    env["MIND_META"] = str(tmp_meta)
    # This test POSITIVELY asserts on gate-firings records, and the writes are
    # redirected to the tmp meta dir above — opt out of the _gate_log pytest
    # suppression guard (), which would otherwise silently no-op log()
    # in this subprocess (PYTEST_CURRENT_TEST propagates via os.environ.copy()).
    env["GATE_LOG_ALLOW_PYTEST"] = "1"
    cmd = [sys.executable, str(ASP_PY), "update-goal", goal_id, field, value, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_layer_d_firings(tmp_meta: Path) -> list[dict]:
    """Return the list of capability-gate-layer-d records from the temp meta
    gate-firings store. Empty list when nothing was written.

    Reads through `_gate_log.firings_paths` — the ONE reader rule — not the
    legacy filename. Since 2026-08-18 the writer honours GATE_FIRINGS_SEGMENTED
    (settings.json sets it to 1 fleet-wide) and appends to a
    `gate-firings-YYYY-MM-DD.jsonl` segment; the pytest conftest pops that flag,
    but this is a main()-style file that pytest never collects, so the pop can
    never reach it (guard-955 class) and the legacy-filename read returned 0
    firings on every box (measured cc-09, 2026-08-18: red under the box flag,
    green with GATE_FIRINGS_SEGMENTED=0). run-invisible-suites.sh now also
    unsets the flag for the whole invisible half; this reader is the
    belt-and-braces half — correct under either flag value.
    """
    from _gate_log import firings_paths  # noqa: E402  (CORE_SCRIPTS is on sys.path)
    out = []
    for path in firings_paths(tmp_meta):
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("gate_id") == "capability-gate-layer-d":
                    out.append(rec)
    return out


def _read_world_aspirations(tmp_world: Path) -> list[dict]:
    out = []
    asp_path = tmp_world / "aspirations.jsonl"
    if not asp_path.is_file():
        return out
    with asp_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _unblocks_in_asp001(items: list[dict]) -> list[dict]:
    asp001 = next((a for a in items if a.get("id") == "asp-001"), None)
    if asp001 is None:
        return []
    return [g for g in asp001.get("goals", [])
            if (g.get("title") or "").startswith("Unblock:")]


def main() -> int:
    failures: list[str] = []
    cases_run = 0

    # ── Case 1: Hit + filed → expect exactly one Layer-D record ───────────
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp) / "world"
        tmp_meta = Path(tmp) / "meta"
        tmp_world.mkdir()
        tmp_meta.mkdir()
        _build_fixture(tmp_world)
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, tmp_meta, "g-test-01", "defer_reason", "deploy blocked until npc behavior analysis completes"
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        firings = _read_layer_d_firings(tmp_meta)

        if rc != 1:
            failures.append(
                f"case1 hit: expected rc=1 (defer refused), got rc={rc} "
                f"(stderr head: {stderr[:200]!r})"
            )
        if not unblocks:
            failures.append(
                f"case1: expected 1 Unblock filed (precondition for Layer-D log), "
                f"got {len(unblocks)} (stderr: {stderr[:200]!r})"
            )
        if len(firings) != 1:
            failures.append(
                f"case1: expected exactly 1 capability-gate-layer-d record, "
                f"got {len(firings)} (firings={firings!r})"
            )
        elif unblocks:
            rec = firings[0]
            if rec.get("decision") != "block":
                failures.append(
                    f"case1: decision should be 'block', got {rec.get('decision')!r}"
                )
            extra = rec.get("extra") or {}
            filed_id = unblocks[0].get("id")
            if extra.get("filed_unblock_id") != filed_id:
                failures.append(
                    f"case1: extra.filed_unblock_id should match filed Unblock "
                    f"id={filed_id!r}, got {extra.get('filed_unblock_id')!r}"
                )
            if extra.get("original_goal_id") != "g-test-01":
                failures.append(
                    f"case1: extra.original_goal_id should be 'g-test-01', "
                    f"got {extra.get('original_goal_id')!r}"
                )
            mc = extra.get("matched_capability") or {}
            if not isinstance(mc, dict) or "matched_keyword" not in mc:
                failures.append(
                    f"case1: extra.matched_capability should be a dict with "
                    f"matched_keyword, got {mc!r}"
                )
            if extra.get("target_aspiration") != "asp-001":
                failures.append(
                    f"case1: extra.target_aspiration should be 'asp-001' "
                    f"(filed_id g-001-NN), got {extra.get('target_aspiration')!r}"
                )
            if not rec.get("trigger_matched"):
                failures.append(
                    f"case1: trigger_matched should be set to the matched keyword, "
                    f"got {rec.get('trigger_matched')!r}"
                )
        ok1 = (rc == 1 and unblocks and len(firings) == 1
               and (firings[0].get("extra") or {}).get("original_goal_id") == "g-test-01")
        print(f"  [{'PASS' if ok1 else 'FAIL'}] hit+filed: rc={rc} "
              f"unblocks={len(unblocks)} firings={len(firings)}")

    # ── Case 2: Miss → zero Layer-D records ──────────────────────────────
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp) / "world"
        tmp_meta = Path(tmp) / "meta"
        tmp_world.mkdir()
        tmp_meta.mkdir()
        _build_fixture(tmp_world)
        blocker_ref = json.dumps({
            "type": "external-service",
            "external_id": "case2-unrelated-probe",
        })
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, tmp_meta, "g-test-01", "defer_reason",
            "completely unrelated nonsense xyzzy qrstuv",
            "--blocker-ref", blocker_ref,
        )
        firings = _read_layer_d_firings(tmp_meta)

        if rc != 0:
            failures.append(
                f"case2 miss: expected rc=0 (defer applied), got rc={rc} "
                f"(stderr: {stderr[:200]!r})"
            )
        if firings:
            failures.append(
                f"case2: expected 0 Layer-D records (no capability match), "
                f"got {len(firings)} (firings={firings!r})"
            )
        ok2 = (rc == 0 and not firings)
        print(f"  [{'PASS' if ok2 else 'FAIL'}] miss: rc={rc} firings={len(firings)}")

    # ── Case 3: Override (--force-defer) → zero Layer-D records ──────────
    # The Layer-D filing happens INSIDE the would_block branch; --force-defer
    # bypasses that branch entirely, so no Unblock is filed and no Layer-D
    # record is emitted (override is its own audit path via stderr +
    # blocker-gate-overrides.jsonl, not gate-firings.jsonl).
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp) / "world"
        tmp_meta = Path(tmp) / "meta"
        tmp_world.mkdir()
        tmp_meta.mkdir()
        _build_fixture(tmp_world)
        blocker_ref = json.dumps({
            "type": "user_action",
            "external_id": "case3-override-justification",
        })
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, tmp_meta, "g-test-01", "defer_reason", "deploy blocked until npc behavior analysis completes",
            "--force-defer", "test override case3",
            "--blocker-ref", blocker_ref,
        )
        firings = _read_layer_d_firings(tmp_meta)

        if rc != 0:
            failures.append(
                f"case3 override: expected rc=0 (override applied), got rc={rc} "
                f"(stderr: {stderr[:200]!r})"
            )
        if firings:
            failures.append(
                f"case3: expected 0 Layer-D records (override bypasses would_block "
                f"branch), got {len(firings)} (firings={firings!r})"
            )
        ok3 = (rc == 0 and not firings)
        print(f"  [{'PASS' if ok3 else 'FAIL'}] override: rc={rc} firings={len(firings)}")

    print()
    print(f"Cases run: {cases_run}")
    if failures:
        print(f"FAIL: {len(failures)} failures")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All cases PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
