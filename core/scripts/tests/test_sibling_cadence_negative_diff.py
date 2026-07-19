"""4: the negative-diff self-heal must exist in EVERY goal-count cadence gate.

THE BUG THIS PINS
-----------------
Three cadence gates fire when (current_completed_goals - goals_count_at_last_fire)
>= cadence. When the count basis moves DOWNWARD -- census double-count repair,
store surgery, archival -- the stamped WM slot ends up ABOVE the live count, diff
goes NEGATIVE, and the ritual STARVES until the count regrows past the stale stamp.

`fresh-eyes-cadence-check.py` was given a self-heal for exactly this (g-115-1936
per-agent, g-115-1941 team-layer) and is covered by
test_fresh_eyes_cadence_negative_diff.py + test_fresh_eyes_cadence_team_negative_diff.py.
Its two SIBLINGS -- same count basis, same failure mode -- never got the heal and
had no test.

MEASURED (zeta precheck, 2026-07-14), both were dead:
  * felt-sense-cadence-check.py: basis 5686 vs current 5351 -> diff=-335. The
    7-lane structured self-audit needed 335+75 = 410 MORE completed goals
    (~8 sessions) before it could fire again.
  * l1-skew-check.py:            basis 5664 vs current 5351 -> diff=-313 (+50 cadence
    => 363 more goals).

WHY IT SURVIVED SO LONG: the failure is SILENT and reads as HEALTHY. Both scripts
exit rc=1 printing "noop (cadence not crossed)" -- byte-identical to a normal
not-yet-due skip. Nothing distinguishes "not due" from "never again". And the
rituals that starved are precisely the ones whose JOB is to notice drift, so the
mechanism that would have caught this WAS the broken mechanism.

WHAT THESE TESTS PIN (beyond "the heal exists"):
  1. It RE-STAMPS (writes rebaselined_from) rather than merely clamping. A bare
     `max(diff, 0)` clamp would leave the stale basis in place and re-fire the
     ritual EVERY iteration -- trading a starved ritual for banner fatigue
     (guard-1090), the opposite failure.
  2. It PRESERVES the last real fire timestamp. A re-baseline is not a fire and
     must not masquerade as one.
  3. It NOOPS -- does not fire the ritual on the correction itself.
  4. It is scoped to diff < 0 ONLY (no write on a healthy diff).
  5. POSITIVE CONTROL: a healthy diff >= cadence STILL FIRES. A heal that broke
     the normal fire path would present exactly like the bug it fixes -- a
     permanently-quiet ritual -- and every other assertion here would still pass.

MUTATION-VERIFIED: strip `if diff < 0:` from either script and its tests fail.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load(stem: str, modname: str):
    """Load a hyphen-named script as a module."""
    spec = importlib.util.spec_from_file_location(modname, str(SCRIPT_DIR / f"{stem}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────── felt-sense-cadence-check.py ─────────────────────────

@pytest.fixture
def fs_mod():
    return _load("felt-sense-cadence-check", "felt_sense_cadence_check")


def _fs_patch(monkeypatch, mod, *, current, last_slot, cadence=75):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: {
        "felt_sense": {"goal_cadence": cadence, "wm_slot": "last_felt_sense_checkin"}
    })
    monkeypatch.setattr(mod, "count_completed_goals", lambda *_a, **_k: current)

    def fake_wm(slot_name=mod.SLOT_NAME):
        if slot_name == "loop_state":
            # min_session_goals gate reads this on the FIRE path only.
            return {"goals_completed_this_session": 999}
        return last_slot

    monkeypatch.setattr(mod, "wm_slot_value", fake_wm)
    monkeypatch.setattr(sys, "argv", ["felt-sense-cadence-check.py"])


def test_felt_sense_negative_diff_rebaselines_and_noops(fs_mod, monkeypatch, capsys):
    """THE LOAD-BEARING ASSERTION. last=5686 > current=5351 (the REAL measured
    values) -> re-stamp to 5351, preserve the timestamp, noop."""
    _fs_patch(monkeypatch, fs_mod, current=5351,
              last_slot={"timestamp": "2026-07-11T14:58:00",
                         "goals_count_at_last_fire": 5686})
    calls = []
    monkeypatch.setattr(fs_mod.subprocess, "run", lambda cmd, **kw: (
        calls.append({"cmd": cmd, "input": kw.get("input")}),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )[1])

    rc = fs_mod.main()
    out = capsys.readouterr().out

    assert rc == 1, f"a re-baseline must NOOP, not fire; got rc={rc}"
    assert "re-baselined" in out, "the heal must announce itself LOUDLY, not silently"
    assert len(calls) == 1, f"expected exactly one re-stamp write, got {len(calls)}"

    payload = json.loads(calls[0]["input"])
    assert payload["goals_count_at_last_fire"] == 5351, "basis must move to CURRENT"
    assert payload["rebaselined_from"] == 5686, (
        "must record rebaselined_from — this is what proves it RE-STAMPED rather "
        "than merely clamping diff to 0 (a clamp leaves the stale basis and "
        "re-fires every iteration)"
    )
    assert payload["timestamp"] == "2026-07-11T14:58:00", (
        "the last REAL fire timestamp must be preserved — a re-baseline is not a "
        "fire and must not masquerade as one"
    )
    assert "last_felt_sense_checkin" in calls[0]["cmd"]


def test_felt_sense_negative_diff_write_failure_still_noops(fs_mod, monkeypatch, capsys):
    """Fail-open: a failed re-stamp must not crash the precheck. Retries next check."""
    _fs_patch(monkeypatch, fs_mod, current=5351,
              last_slot={"goals_count_at_last_fire": 5686})

    def boom(cmd, **_kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(fs_mod.subprocess, "run", boom)
    rc = fs_mod.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "re-baseline write" in err, "a failed heal must be visible on stderr"


def test_felt_sense_healthy_diff_takes_no_rebaseline(fs_mod, monkeypatch, capsys):
    """Scoped to diff<0 ONLY. A healthy under-cadence diff must not write."""
    _fs_patch(monkeypatch, fs_mod, current=5361,
              last_slot={"goals_count_at_last_fire": 5351})  # diff=10 < 75
    calls = []
    monkeypatch.setattr(fs_mod.subprocess, "run", lambda cmd, **kw: (
        calls.append(cmd), subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )[1])

    rc = fs_mod.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "re-baselined" not in out
    assert calls == [], "the heal must not write on a non-negative diff"


def test_felt_sense_positive_control_healthy_diff_still_fires(fs_mod, monkeypatch, capsys):
    """POSITIVE CONTROL. A heal that broke the normal FIRE path would present
    exactly like the bug it fixes — a permanently-quiet ritual — and every
    assertion above would STILL PASS. Prove the gate can still say yes."""
    _fs_patch(monkeypatch, fs_mod, current=5451,
              last_slot={"goals_count_at_last_fire": 5351})  # diff=100 >= 75
    monkeypatch.setattr(fs_mod.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))

    rc = fs_mod.main()
    out = capsys.readouterr().out
    assert rc == 0, (
        f"a healthy diff of 100 >= cadence 75 MUST still fire; got rc={rc}. "
        f"If this fails, the self-heal broke the fire path. out={out!r}"
    )
    assert "fire" in out


# ───────────────────────────── l1-skew-check.py ─────────────────────────────

@pytest.fixture
def l1_mod():
    return _load("l1-skew-check", "l1_skew_check")


def _l1_patch(monkeypatch, mod, *, current, last_slot, cadence=50):
    monkeypatch.setattr(mod, "_load_cadence_config",
                        lambda: {"goal_cadence": cadence, "wm_slot": "last_l1_skew_check"})
    monkeypatch.setattr(mod, "_count_completed_goals", lambda: current)
    monkeypatch.setattr(mod, "_wm_read", lambda _slot: last_slot)
    writes = []
    monkeypatch.setattr(mod, "_wm_set", lambda slot, value: writes.append((slot, value)))
    return writes


def test_l1_skew_negative_diff_rebaselines_and_does_not_fire(l1_mod, monkeypatch, capsys):
    """THE LOAD-BEARING ASSERTION (real measured values: basis 5664, current 5351)."""
    writes = _l1_patch(monkeypatch, l1_mod, current=5351,
                       last_slot={"timestamp": "2026-07-11T12:05:14",
                                  "goals_count_at_last_fire": 5664,
                                  "any_flagged": True})
    fire, current, cfg, _last = l1_mod._cadence_gate()
    out = capsys.readouterr().out

    assert fire is False, (
        "a re-baseline must NOT fire the ritual — firing on every basis "
        "correction is banner fatigue (guard-1090), the opposite failure"
    )
    assert current == 5351
    assert "re-baselined" in out, "the heal must announce itself LOUDLY"
    assert len(writes) == 1, f"expected exactly one re-stamp write, got {len(writes)}"

    slot, payload = writes[0]
    assert slot == "last_l1_skew_check"
    assert payload["goals_count_at_last_fire"] == 5351
    assert payload["rebaselined_from"] == 5664
    assert payload["timestamp"] == "2026-07-11T12:05:14", "preserve the last REAL fire"


def test_l1_skew_healthy_diff_takes_no_rebaseline(l1_mod, monkeypatch, capsys):
    """Scoped to diff<0 ONLY."""
    writes = _l1_patch(monkeypatch, l1_mod, current=5361,
                       last_slot={"goals_count_at_last_fire": 5351})  # diff=10 < 50
    fire, _current, _cfg, _last = l1_mod._cadence_gate()
    assert fire is False
    assert writes == [], "the heal must not write on a non-negative diff"
    assert "re-baselined" not in capsys.readouterr().out


def test_l1_skew_positive_control_healthy_diff_still_fires(l1_mod, monkeypatch):
    """POSITIVE CONTROL — see the felt-sense twin. Prove the gate can still say yes."""
    _l1_patch(monkeypatch, l1_mod, current=5451,
              last_slot={"goals_count_at_last_fire": 5351})  # diff=100 >= 50
    fire, current, _cfg, _last = l1_mod._cadence_gate()
    assert fire is True, (
        "a healthy diff of 100 >= cadence 50 MUST still fire. If this fails, the "
        "self-heal broke the fire path — which looks identical to the bug it fixes."
    )
    assert current == 5451


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
