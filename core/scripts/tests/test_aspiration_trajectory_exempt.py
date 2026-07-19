#!/usr/bin/env python3
"""test_aspiration_trajectory_exempt.py - regression test (7).

Pins the record-level plateau exemption: an aspiration carrying
`plateau_exempt: true` must have BOTH plateau_detected and diminishing_returns
suppressed by build_trajectory, while velocity stays computed (informative)
and detection stays unchanged for non-exempt records.

Canonical incident: evolve Step 1.5 flagged asp-115 (the recurring
infrastructure-maintenance queue, ~0 learning velocity by design) as PLATEAU
on every 12h cadence pass; no agent ever filed the prescribed Investigate,
so every pass silently re-made the same skip-judgment. The exemption encodes
that judgment once, at the data (aspiration record), not in framework config
(aspiration IDs are domain data).
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# aspiration-trajectory.py is hyphenated -> load by path
_spec_at = importlib.util.spec_from_file_location(
    "aspiration_trajectory", SCRIPT_DIR / "aspiration-trajectory.py")
at = importlib.util.module_from_spec(_spec_at)
_spec_at.loader.exec_module(at)

CONFIG = {
    "velocity_window": 5,
    "plateau_threshold": 0.2,
    "diminishing_returns_window": 5,
}


def _make_asp(asp_id, plateau_exempt=None, n_goals=6):
    """Aspiration with n_goals completed zero-artifact goals (velocity 0.0)."""
    goals = [
        {
            "id": f"g-999-{i:02d}",
            "title": f"goal {i}",
            "status": "completed",
            "category": "framework-meta",
            "started": f"2026-07-{10 + i:02d}T10:00:00",
        }
        for i in range(1, n_goals + 1)
    ]
    asp = {"id": asp_id, "title": f"test {asp_id}", "status": "active", "goals": goals}
    if plateau_exempt is not None:
        asp["plateau_exempt"] = plateau_exempt
    return asp


def _shared_for(*asps):
    """Fully-supplied shared dict — no filesystem access in build_trajectory."""
    return {
        "config": dict(CONFIG),
        "reasoning_bank": [],
        "guardrails": [],
        "pattern_sigs": [],
        "tree_data": {},
        "tree_attribution": {},
        "script_convention_attribution": {},
        "asp_sources": [list(asps), []],
    }


def test_exempt_record_suppresses_both_flags():
    asp = _make_asp("asp-900", plateau_exempt=True)
    t = at.build_trajectory("asp-900", shared=_shared_for(asp))
    assert t["plateau_exempt"] is True
    assert t["plateau_detected"] is False
    assert t["diminishing_returns"] is False
    # Velocity stays computed + reported (informative), only flags suppressed.
    assert t["current_velocity"] == 0.0


def test_non_exempt_record_detection_unchanged():
    asp = _make_asp("asp-901")  # no plateau_exempt field at all
    t = at.build_trajectory("asp-901", shared=_shared_for(asp))
    assert t["plateau_exempt"] is False
    # 6 zero-artifact goals >= window 5, velocity 0.0 < 0.2 -> plateau fires.
    assert t["plateau_detected"] is True


def test_exempt_false_behaves_like_absent():
    asp = _make_asp("asp-902", plateau_exempt=False)
    t = at.build_trajectory("asp-902", shared=_shared_for(asp))
    assert t["plateau_exempt"] is False
    assert t["plateau_detected"] is True


def test_truthy_string_does_not_exempt():
    """Strict-boolean contract: 'False'/'no'/string-'true' must NOT exempt.

    bool() on any non-empty string is True — a capital-F 'False' typed at the
    CLI (parse_value converts only lowercase) would have silently SUPPRESSED
    detection. Malformed values keep detection ON (fail-safe direction).
    """
    for bad in ("False", "no", "true", "yes"):
        asp = _make_asp("asp-903", plateau_exempt=bad)
        t = at.build_trajectory("asp-903", shared=_shared_for(asp))
        assert t["plateau_exempt"] is False, f"string {bad!r} must not exempt"
        assert t["plateau_detected"] is True, f"string {bad!r} must not suppress"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
