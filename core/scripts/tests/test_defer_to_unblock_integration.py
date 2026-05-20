"""test_defer_to_unblock_integration.py — regression test for .

End-to-end integration test for the defer-time capability gate +
auto-routing pipeline (g-257-02 / g-257-03 / g-257-04). Unlike the
unit tests for `_file_unblock_under_existing_lock` (filing helper) and
`_find_existing_unblock_for` (dedup helper), this exercises the full
chain via a real `aspirations.py update_goal` subprocess against an
ephemeral world fixture:

  aspirations-update-goal.sh defer_reason "..."
    -> cmd_update_goal._is_narrative_defer  (narrative gating)
    -> _run_capability_gate_for_defer      (subprocess to gate)
    -> _file_unblock_under_existing_lock   (atomic write under lock)
    -> sys.exit(1) + Unblock visible in queue

Seven cases (1-6 per g-257-05; case 7 added by g-115-334 / rb-655):
  1. Hit + auto-file: defer with verb match → exit 1 + new Unblock in queue
  2. Hit + opt-out: same defer + --force-defer → exit 0 + defer applied + NO Unblock
  3. Miss: defer with no verb match → exit 0 + defer applied (existing behavior)
  4. Structured prefix: "blocked_on_dependency:..." → exit 0 + gate skipped
  5. Dedup: hit + pre-existing Unblock → exit 1 + NO duplicate filed
  6. Origin-signal: filed Unblock has origin_signal=unblock:<orig> AND passes
     origin-signal-gate.py validation
  7. Cross-source (rb-655): hit on a queue with NO asp-001 → exit 1 + Unblock
     routes to original goal's parent aspiration via strategy=original-parent-asp
     (regression test for hardcoded asp-001 silent-failure bug)

Pattern: ephemeral tmpdir per case, seeded with forged-skills.yaml and
capability-routing.md from the live world (so the gate's keyword
sources are present), plus a synthetic asp-001 + asp-test
aspirations.jsonl. MIND_WORLD env-override directs aspirations.py
at the temp world; production state is never touched.
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
ORIGIN_GATE_PY = CORE_SCRIPTS / "origin-signal-gate.py"

sys.path.insert(0, str(CORE_SCRIPTS))
from _paths import agent_dir as _agent_dir  # noqa: E402


def _read_local_paths_world() -> Path | None:
    """Return the live world dir from <agent>/local-paths.conf, or None."""
    agent = os.environ.get("MIND_AGENT", "bravo")
    conf = _agent_dir(agent) / "local-paths.conf"
    if not conf.is_file():
        return None
    for raw in conf.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("WORLD_PATH="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            return Path(val) if val else None
    return None


def _build_fixture(tmp_world: Path, *, with_existing_unblock: bool = False) -> None:
    """Seed the temp world with the minimum needed for the gate + cmd_update_goal.

    Copies the live world's forged-skills.yaml + capability-routing.md so the
    capability gate can match keywords identically to production. Creates a
    synthetic aspirations.jsonl with asp-001 (Unblock filing target) and
    asp-test (the goal we'll attempt to defer).

    When with_existing_unblock=True, asp-001 is pre-seeded with a pending
    Unblock carrying origin_signal=unblock:g-test-01 (case 5 dedup setup).
    """
    live = _read_local_paths_world()

    # Copy the gate's two world-rooted source files so keyword matching mirrors
    # production. If either is absent in the live world, the gate falls back
    # to .claude/skills/ scanning (still keyword-functional but narrower).
    if live and (live / "forged-skills.yaml").is_file():
        shutil.copy(live / "forged-skills.yaml", tmp_world / "forged-skills.yaml")
    conventions_dir = tmp_world / "conventions"
    conventions_dir.mkdir(parents=True, exist_ok=True)
    if live and (live / "conventions" / "capability-routing.md").is_file():
        shutil.copy(
            live / "conventions" / "capability-routing.md",
            conventions_dir / "capability-routing.md",
        )

    # Synthetic aspirations file. asp-001 is the Unblock filing target
    # (matches _file_unblock_under_existing_lock's hardcoded asp-001 lookup).
    asp_001_goals = [
        {"id": "g-001-01", "title": "Reflect", "status": "pending",
         "type": "recurring", "category": "meta", "recurring": True},
    ]
    if with_existing_unblock:
        asp_001_goals.append({
            "id": "g-001-99",
            "title": "Unblock: deploy for g-test-01",
            "description": "Pre-existing pending Unblock (case 5 dedup setup)",
            "status": "pending",
            "origin_signal": "unblock:g-test-01",
            "type": "idea",
            "category": "framework-maintenance",
            "priority": "HIGH",
            "participants": ["agent"],
        })

    asps = [
        {
            "id": "asp-001",
            "title": "Recurring meta-cadence",
            "status": "active",
            "goals": asp_001_goals,
        },
        {
            "id": "asp-test",
            "title": "Test aspiration (defer-to-unblock fixture)",
            "status": "active",
            "goals": [
                {"id": "g-test-01",
                 "title": "Original work goal",
                 "description": "Some test goal that we'll try to defer.",
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


def _run_update_goal(tmp_world: Path, goal_id: str, field: str, value: str,
                     *extra: str) -> tuple[int, str, str]:
    """Invoke aspirations.py update_goal with MIND_WORLD pointed at tmp_world."""
    env = os.environ.copy()
    env["MIND_WORLD"] = str(tmp_world)
    cmd = [sys.executable, str(ASP_PY), "update-goal", goal_id, field, value, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_world_aspirations(tmp_world: Path) -> list[dict]:
    """Parse aspirations.jsonl from the temp world dir."""
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


def _find_goal(items: list[dict], goal_id: str) -> dict | None:
    for asp in items:
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def _unblocks_in_asp001(items: list[dict]) -> list[dict]:
    asp001 = next((a for a in items if a.get("id") == "asp-001"), None)
    if asp001 is None:
        return []
    return [g for g in asp001.get("goals", [])
            if (g.get("title") or "").startswith("Unblock:")]


def _run_origin_signal_gate(goal: dict) -> tuple[int, dict]:
    """Invoke origin-signal-gate.py via subprocess; verify origin_signal validity."""
    payload = {
        "title": goal.get("title", ""),
        "description": goal.get("description", ""),
        "origin_signal": goal.get("origin_signal"),
        "source": "world",
    }
    proc = subprocess.run(
        [sys.executable, str(ORIGIN_GATE_PY)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=15,
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, result


def main() -> int:
    failures = []
    cases_run = 0

    # ---- Case 1: Hit + auto-file --------------------------------------------
    # defer_reason="deploy needs human" — "human" matches an NPC capability,
    # so the gate fires; "deploy" is the first action verb so the Unblock
    # title contains "deploy". Expect exit 1, new Unblock in asp-001, AND
    # the original goal's defer_reason still null (write was refused).
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        _build_fixture(tmp_world)
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, "g-test-01", "defer_reason", "deploy needs human"
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        orig = _find_goal(items, "g-test-01")

        if rc != 1:
            failures.append(
                f"case1 hit+auto-file: expected rc=1, got rc={rc} "
                f"(stderr head: {stderr[:200]!r})"
            )
        if not unblocks:
            failures.append(
                f"case1: expected 1 new Unblock in asp-001, found {len(unblocks)} "
                f"(stderr: {stderr[:200]!r})"
            )
        elif "deploy" not in (unblocks[0].get("title") or "").lower():
            failures.append(
                f"case1: Unblock title should contain 'deploy', got "
                f"{unblocks[0].get('title')!r}"
            )
        if orig is None:
            failures.append("case1: original goal g-test-01 vanished from queue")
        elif orig.get("defer_reason"):
            failures.append(
                f"case1: original defer_reason should be None (refused), got "
                f"{orig.get('defer_reason')!r}"
            )
        ok1 = (rc == 1 and unblocks
               and "deploy" in (unblocks[0].get("title") or "").lower()
               and orig is not None and not orig.get("defer_reason"))
        print(f"  [{'PASS' if ok1 else 'FAIL'}] hit+auto-file: rc={rc} "
              f"unblock_count={len(unblocks)} "
              f"unblock_title={(unblocks[0].get('title') if unblocks else None)!r}")

    # ---- Case 2: Hit + opt-out (--force-defer) ------------------------------
    # Same defer + --force-defer "<just>". Gate's would_block branch is
    # bypassed (override echoed to stderr); blocker-ref check then requires
    # either --blocker-ref OR --force-unstructured-defer. We supply a valid
    # --blocker-ref so the path completes naturally. Expect exit 0, defer
    # applied to original goal, NO new Unblock filed.
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        _build_fixture(tmp_world)
        blocker_ref = json.dumps({
            "type": "user_action",
            "external_id": "manual-test-justification",
        })
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, "g-test-01", "defer_reason", "deploy needs human",
            "--force-defer", "test override case2",
            "--blocker-ref", blocker_ref,
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        orig = _find_goal(items, "g-test-01")

        if rc != 0:
            failures.append(
                f"case2 hit+opt-out: expected rc=0, got rc={rc} "
                f"(stderr head: {stderr[:200]!r})"
            )
        if unblocks:
            failures.append(
                f"case2: expected 0 Unblocks (override bypasses filing), "
                f"got {len(unblocks)}"
            )
        if orig is None or orig.get("defer_reason") != "deploy needs human":
            failures.append(
                f"case2: defer_reason should be applied, got "
                f"{orig.get('defer_reason') if orig else None!r}"
            )
        ok2 = (rc == 0 and not unblocks and orig
               and orig.get("defer_reason") == "deploy needs human")
        print(f"  [{'PASS' if ok2 else 'FAIL'}] hit+opt-out: rc={rc} "
              f"unblocks={len(unblocks)} "
              f"defer_applied={orig.get('defer_reason') if orig else None!r}")

    # ---- Case 3: Miss (no verb/keyword match) -------------------------------
    # An unrelated defer_reason that matches no capability. Gate exits 0
    # (no block); blocker-ref check still requires a structured ref so we
    # supply one. Expect exit 0, defer applied, NO Unblock.
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        _build_fixture(tmp_world)
        blocker_ref = json.dumps({
            "type": "external-service",
            "external_id": "case3-unrelated-probe",
        })
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, "g-test-01", "defer_reason",
            "completely unrelated nonsense xyzzy qrstuv",
            "--blocker-ref", blocker_ref,
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        orig = _find_goal(items, "g-test-01")

        if rc != 0:
            failures.append(
                f"case3 miss: expected rc=0, got rc={rc} (stderr: {stderr[:200]!r})"
            )
        if unblocks:
            failures.append(
                f"case3: expected 0 Unblocks (no capability match), got {len(unblocks)}"
            )
        if orig is None or "xyzzy" not in (orig.get("defer_reason") or ""):
            failures.append(
                f"case3: defer_reason should be applied, got "
                f"{orig.get('defer_reason') if orig else None!r}"
            )
        ok3 = (rc == 0 and not unblocks and orig
               and "xyzzy" in (orig.get("defer_reason") or ""))
        print(f"  [{'PASS' if ok3 else 'FAIL'}] miss: rc={rc} "
              f"unblocks={len(unblocks)}")

    # ---- Case 4: Structured prefix (gate skipped) ---------------------------
    # "blocked_on_dependency:g-other" passes _is_narrative_defer's structured-
    # prefix bypass — neither the capability gate nor the blocker-ref check
    # fires. Defer applies directly. Expect exit 0, defer applied, NO Unblock,
    # AND no "[defer-gate]" stderr line (proves gate didn't run).
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        _build_fixture(tmp_world)
        rc, _stdout, stderr = _run_update_goal(
            tmp_world, "g-test-01", "defer_reason",
            "blocked_on_dependency:g-some-other-goal",
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        orig = _find_goal(items, "g-test-01")

        if rc != 0:
            failures.append(
                f"case4 structured-prefix: expected rc=0, got rc={rc} "
                f"(stderr: {stderr[:200]!r})"
            )
        if unblocks:
            failures.append(
                f"case4: expected 0 Unblocks (gate skipped), got {len(unblocks)}"
            )
        if orig is None or "blocked_on_dependency" not in (orig.get("defer_reason") or ""):
            failures.append(
                f"case4: defer_reason should be applied, got "
                f"{orig.get('defer_reason') if orig else None!r}"
            )
        if "[defer-gate]" in stderr:
            failures.append(
                f"case4: structured prefix should bypass gate, but stderr contains "
                f"'[defer-gate]': {stderr[:300]!r}"
            )
        ok4 = (rc == 0 and not unblocks and orig
               and "blocked_on_dependency" in (orig.get("defer_reason") or "")
               and "[defer-gate]" not in stderr)
        print(f"  [{'PASS' if ok4 else 'FAIL'}] structured-prefix: rc={rc} "
              f"unblocks={len(unblocks)} gate_skipped={'[defer-gate]' not in stderr}")

    # ---- Case 5: Dedup (hit + existing Unblock) -----------------------------
    # Pre-seed asp-001 with an in-flight Unblock whose origin_signal is
    # unblock:g-test-01. Same defer_reason. Gate fires (would_block=True),
    # _find_existing_unblock_for matches via strategy (a), filing skips with
    # idempotent message. Expect exit 1, NO new Unblock added (count stays
    # at 1 — the pre-existing one), original defer_reason still null.
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        _build_fixture(tmp_world, with_existing_unblock=True)
        pre_count = len(_unblocks_in_asp001(_read_world_aspirations(tmp_world)))

        rc, _stdout, stderr = _run_update_goal(
            tmp_world, "g-test-01", "defer_reason", "deploy needs human"
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        orig = _find_goal(items, "g-test-01")

        if rc != 1:
            failures.append(
                f"case5 dedup: expected rc=1 (still blocks defer), got rc={rc} "
                f"(stderr: {stderr[:200]!r})"
            )
        if len(unblocks) != pre_count:
            failures.append(
                f"case5: Unblock count should stay at {pre_count} (idempotent skip), "
                f"got {len(unblocks)}"
            )
        if orig is None or orig.get("defer_reason"):
            failures.append(
                f"case5: original defer_reason should remain None, got "
                f"{orig.get('defer_reason') if orig else None!r}"
            )
        if "idempotent" not in stderr.lower() and "existing" not in stderr.lower():
            failures.append(
                f"case5: stderr should announce idempotent skip / existing match, "
                f"got {stderr[:300]!r}"
            )
        ok5 = (rc == 1 and len(unblocks) == pre_count
               and orig is not None and not orig.get("defer_reason"))
        print(f"  [{'PASS' if ok5 else 'FAIL'}] dedup: rc={rc} "
              f"unblock_count={len(unblocks)} (pre={pre_count}) "
              f"idempotent_msg={'idempotent' in stderr.lower() or 'existing' in stderr.lower()}")

    # ---- Case 6: Origin-signal validity -------------------------------------
    # Filed Unblock from case 1 must carry origin_signal=unblock:g-test-01,
    # AND that signal must pass origin-signal-gate.py validation (so the
    # Unblock is a fully-routable goal, not a dead-end). Re-runs the case 1
    # fixture to keep the assertion isolated.
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        _build_fixture(tmp_world)
        _rc, _stdout, _stderr = _run_update_goal(
            tmp_world, "g-test-01", "defer_reason", "deploy needs human"
        )
        items = _read_world_aspirations(tmp_world)
        unblocks = _unblocks_in_asp001(items)
        if not unblocks:
            failures.append("case6: expected an Unblock to validate, got none")
            print(f"  [FAIL] origin-signal: no Unblock filed")
        else:
            unblock = unblocks[0]
            origin = unblock.get("origin_signal")
            if origin != "unblock:g-test-01":
                failures.append(
                    f"case6: origin_signal expected 'unblock:g-test-01', got {origin!r}"
                )
            gate_rc, gate_out = _run_origin_signal_gate(unblock)
            would_block = gate_out.get("would_block")
            if gate_rc != 0 or would_block:
                failures.append(
                    f"case6: origin-signal-gate.py should accept the Unblock, "
                    f"got rc={gate_rc} would_block={would_block} "
                    f"reason={gate_out.get('reason')!r}"
                )
            ok6 = (origin == "unblock:g-test-01" and gate_rc == 0 and not would_block)
            print(f"  [{'PASS' if ok6 else 'FAIL'}] origin-signal: "
                  f"signal={origin!r} gate_rc={gate_rc} would_block={would_block}")

    # ---- Case 7: Cross-source — no asp-001, fall back to parent asp --------
    # Regression test for the rb-655 silent-failure bug: when the current
    # source has NO asp-001 (e.g. the world queue holds work aspirations and
    # is not seeded with a per-agent recurring template), the defer-gate
    # used to refuse the defer correctly but fail to file the Unblock
    # ("target aspiration asp-001 not found in live file"). After the
    # three-strategy fallback, strategy (b) routes the Unblock to the
    # original goal's parent aspiration and stderr announces
    # strategy=original-parent-asp.
    #
    # Custom fixture: asp-001 is absent; the original goal's parent uses a
    # numeric asp ID (asp-555) so the goal-id auto-generator at
    # _file_unblock_under_existing_lock produces a valid g-555-NN id rather
    # than choking on a non-numeric "asp-test"-style placeholder.
    cases_run += 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_world = Path(tmp)
        live = _read_local_paths_world()
        if live and (live / "forged-skills.yaml").is_file():
            shutil.copy(live / "forged-skills.yaml",
                        tmp_world / "forged-skills.yaml")
        conventions_dir = tmp_world / "conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)
        if live and (live / "conventions" / "capability-routing.md").is_file():
            shutil.copy(
                live / "conventions" / "capability-routing.md",
                conventions_dir / "capability-routing.md",
            )
        asps = [
            {
                "id": "asp-555",
                "title": "Cross-source fixture (no asp-001 present)",
                "status": "active",
                "goals": [
                    {"id": "g-555-01",
                     "title": "Original work goal in cross-source queue",
                     "description": "Goal whose parent should receive the "
                                    "Unblock when asp-001 is absent.",
                     "status": "pending",
                     "type": "idea",
                     "category": "framework-maintenance",
                     "priority": "MEDIUM",
                     "participants": ["agent"],
                     "origin_signal": "investigate:cross-source"},
                ],
            },
        ]
        with (tmp_world / "aspirations.jsonl").open("w", encoding="utf-8") as f:
            for a in asps:
                f.write(json.dumps(a) + "\n")

        rc, _stdout, stderr = _run_update_goal(
            tmp_world, "g-555-01", "defer_reason", "deploy needs human"
        )
        items = _read_world_aspirations(tmp_world)
        asp_555 = next((a for a in items if a.get("id") == "asp-555"), None)
        parent_unblocks = [
            g for g in (asp_555.get("goals", []) if asp_555 else [])
            if (g.get("title") or "").startswith("Unblock:")
        ]
        orig = _find_goal(items, "g-555-01")

        if rc != 1:
            failures.append(
                f"case7 cross-source: expected rc=1 (defer refused), got "
                f"rc={rc} (stderr: {stderr[:300]!r})"
            )
        if not parent_unblocks:
            failures.append(
                f"case7: expected 1 Unblock filed in asp-555 via parent "
                f"fallback, found {len(parent_unblocks)} "
                f"(stderr: {stderr[:400]!r})"
            )
        elif "deploy" not in (parent_unblocks[0].get("title") or "").lower():
            failures.append(
                f"case7: parent Unblock title should contain 'deploy', got "
                f"{parent_unblocks[0].get('title')!r}"
            )
        if "original-parent-asp" not in stderr:
            failures.append(
                f"case7: stderr should announce strategy=original-parent-asp, "
                f"got {stderr[:400]!r}"
            )
        if orig is not None and orig.get("defer_reason"):
            failures.append(
                f"case7: original defer_reason should remain None (refused), "
                f"got {orig.get('defer_reason')!r}"
            )
        ok7 = (rc == 1 and parent_unblocks
               and "deploy" in (parent_unblocks[0].get("title") or "").lower()
               and "original-parent-asp" in stderr
               and orig is not None and not orig.get("defer_reason"))
        print(f"  [{'PASS' if ok7 else 'FAIL'}] cross-source: rc={rc} "
              f"parent_unblock_count={len(parent_unblocks)} "
              f"strategy_logged={'original-parent-asp' in stderr}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nAll {cases_run} defer-to-unblock integration cases verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
