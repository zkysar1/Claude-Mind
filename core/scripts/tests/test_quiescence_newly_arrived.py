"""test_quiescence_newly_arrived.py — Magic Wand 2 regression.

Exercises the _check_newly_arrived_work helper added to quiescence-gate.py.
Each case calls the helper with a snapshot + config + mocked counts and
asserts the drifted list mutation matches the decision rule.

Decision rule:
  - newly_arrived_work.enabled == False → no-op
  - snapshot lacks goal_count_at_entry → no-op (backward compat)
  - current_count - goal_count_at_entry < min_delta → no-op
  - delta >= min_delta but selector returns < min_executable → no-op
  - delta >= min_delta and selector returns >= min_executable → append drift

Pattern: monkey-patch _total_goal_count and the dynamically-imported
goal-selector module's collect_candidates at module level. No real file I/O.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GATE_PATH = CORE_SCRIPTS / "quiescence-gate.py"
spec = importlib.util.spec_from_file_location("quiescence_gate", GATE_PATH)
qg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qg)


class FakeSelector:
    """Stub for goal-selector module; collect_candidates returns N stub dicts."""

    def __init__(self, n_world: int, n_agent: int):
        self._n_world = n_world
        self._n_agent = n_agent

    def collect_candidates(self, asps, known_blockers=None, source="world", **kw):
        n = self._n_world if source == "world" else self._n_agent
        return [{"goal_id": f"stub-{source}-{i}"} for i in range(n)]


def _setup_mocks(qg_module, total_count: int, fake_selector: FakeSelector):
    """Install module-level mocks on the quiescence_gate module."""
    qg_module._total_goal_count = lambda: total_count
    qg_module._known_blockers = lambda: []
    # _load_aspirations_from is called inside _check_newly_arrived_work's tier 2
    # to feed collect_candidates. Return non-empty stubs so the world+agent
    # branches both fire.
    qg_module._load_aspirations_from = lambda p: [{"id": "asp-stub", "status": "active", "goals": []}]

    # Patch the selector module that _check_newly_arrived_work imports lazily.
    # importlib.import_module("goal-selector") is what the helper calls.
    sys.modules["goal-selector"] = fake_selector


CASES = [
    {
        "id": "disabled-noop",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {"newly_arrived_work": {"enabled": False, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 20,
        "selector": FakeSelector(5, 5),
        "expected_drift_count": 0,
    },
    {
        "id": "no-snapshot-field-backward-compat",
        "snap": {},  # legacy snapshot — no goal_count_at_entry
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 20,
        "selector": FakeSelector(5, 5),
        "expected_drift_count": 0,
    },
    {
        "id": "delta-below-min",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 11,  # delta 1 < 2
        "selector": FakeSelector(5, 5),
        "expected_drift_count": 0,
    },
    {
        "id": "delta-meets-min-but-selector-empty",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 12,  # delta 2 — meets min
        "selector": FakeSelector(0, 0),  # no executable candidates
        "expected_drift_count": 0,
    },
    {
        "id": "delta-meets-min-selector-below-min-executable",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 12,
        "selector": FakeSelector(1, 0),  # only 1 candidate, < min_executable=2
        "expected_drift_count": 0,
    },
    {
        "id": "delta-meets-min-selector-meets-min-flag-drift",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 12,
        "selector": FakeSelector(1, 1),  # 2 total, meets min_executable=2
        "expected_drift_count": 1,
    },
    {
        "id": "negative-delta-goals-archived-no-flag",
        "snap": {"goal_count_at_entry": 20},
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 2,
                                        "min_executable": 2}},
        "current_count": 18,  # net loss of goals during sleep — archival case
        "selector": FakeSelector(5, 5),
        "expected_drift_count": 0,
    },
    {
        "id": "default-config-min-2-applies-when-cfg-block-missing",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {},  # no newly_arrived_work block at all → defaults apply
        "current_count": 12,
        "selector": FakeSelector(2, 0),
        "expected_drift_count": 1,
    },
    {
        "id": "custom-thresholds-min-delta-5",
        "snap": {"goal_count_at_entry": 10},
        "cfg": {"newly_arrived_work": {"enabled": True, "min_delta": 5,
                                        "min_executable": 1}},
        "current_count": 13,  # delta 3 < custom min_delta 5
        "selector": FakeSelector(10, 10),
        "expected_drift_count": 0,
    },
]


def main() -> int:
    failures = []

    for case in CASES:
        cid = case["id"]
        # Reset module mocks per-case
        _setup_mocks(qg, case["current_count"], case["selector"])

        drifted = []
        try:
            qg._check_newly_arrived_work(case["snap"], case["cfg"], drifted)
        except Exception as e:
            failures.append(f"[FAIL] {cid}: raised {type(e).__name__}: {e}")
            continue

        if len(drifted) != case["expected_drift_count"]:
            failures.append(
                f"[FAIL] {cid}: expected {case['expected_drift_count']} "
                f"drift entries, got {len(drifted)}: {drifted!r}"
            )
            continue

        # If we expected a drift entry, validate its shape
        if case["expected_drift_count"] == 1:
            entry = drifted[0]
            if entry.get("external_id") != "__new_work_arrived__":
                failures.append(
                    f"[FAIL] {cid}: drift entry external_id wrong: {entry!r}"
                )
                continue
            if entry.get("type") != "newly_executable_goals":
                failures.append(
                    f"[FAIL] {cid}: drift entry type wrong: {entry!r}"
                )
                continue
            if not isinstance(entry.get("count"), int) or entry["count"] < 1:
                failures.append(
                    f"[FAIL] {cid}: drift entry count invalid: {entry!r}"
                )
                continue

        print(f"  [PASS] {cid}: drifted_count={len(drifted)}")

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(CASES)} test(s) failed")
        return 1
    print(f"All {len(CASES)} newly-arrived-work cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
