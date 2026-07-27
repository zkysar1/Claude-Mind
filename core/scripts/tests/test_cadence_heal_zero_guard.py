"""The negative-diff self-heal must REFUSE a failure sentinel as a new basis.

THE BUG THIS PINS (fresh-eyes-code F-001, 2026-07-14 — severity: invalidates)
---------------------------------------------------------------------------
Both goal counters return **0 as a silent failure sentinel**:

  * l1-skew-check.py `_count_completed_goals()`     -> `return 0` on subprocess
    rc!=0, on a 10s TimeoutExpired, on OSError, and on unparseable stdout.
  * {fresh-eyes,felt-sense}-cadence-check.py `count_completed_goals()` -> total
    stays 0 when every candidate file is missing (`if not p.exists(): continue`)
    or unreadable (`except OSError: continue`).

The negative-diff self-heal then treats that 0 as a REAL basis:
`diff = 0 - 5664 < 0` -> it re-stamps the WM slot to `goals_count_at_last_fire:
0`. The cadence basis is DESTROYED. Next iteration, with the counter recovered,
`last_count == 0` takes the first-fire path and the ritual FIRES SPURIOUSLY.

WHY THIS IS WORSE THAN IT LOOKS. Before the heal existed, a transient 0 was
HARMLESS: `fire = diff >= cadence` was False, the gate noop'd, and NOTHING WAS
WRITTEN — the next iteration recovered by itself. The heal converts a
SELF-RECOVERING transient error into PERMANENT STATE CORRUPTION. A 10-second
subprocess timeout now costs the cadence basis. A defense that persists the
failure it should absorb is worse than no defense.

This is guard-1091 verbatim: **a FAILED measurement is not a measurement of
ZERO.** A crashed counter whose consumer substitutes 0 launders an error into a
lie that reads as data.

WHY `current == 0` IS THE RIGHT PREDICATE. Inside `diff < 0`, `current == 0`
already implies `last_count > 0` — so the guard cannot mask a legitimate basis.
A real count never falls to zero: it folds in archives + census_completed and is
eviction-invariant by design. And a genuinely-empty store is not starved either
— it self-heals the moment ONE goal completes (current >= 1 takes the normal
re-baseline).

FOUR SITES, ONE MECHANISM. This module is deliberately mechanism-scoped rather
than split per-file (rb-3452: guard the MECHANISM, not the CASE):

  1. felt-sense-cadence-check.py      per-agent WM slot          (g-115-1944)
  2. l1-skew-check.py                 per-agent WM slot          (g-115-1944)
  3. fresh-eyes-cadence-check.py      per-agent WM slot          (g-115-1936)
  4. fresh-eyes-cadence-check.py      SHARED world/team-state.yaml
                                      shared_cadences.<slot>     (g-115-1941)

Site 4 is the worst: it writes SHARED fleet state, so one agent's transient read
failure would corrupt the cadence basis for EVERY agent. Its test scenario is
also the most realistic on an own-cloud box: `count_completed_goals(world_only=
True)` reads ONLY the S3-backed world files while the full count ALSO reads
agent-local files — so a world-store read failure yields a HEALTHY per-agent diff
(reaching the team gate) alongside a FAKE ZERO world count.

PROVENANCE, honestly: sites 1-2 were introduced by g-115-1944 porting the
"proven" heal from fresh-eyes. The port audited whether the defense was PRESENT
in all three gates; it never audited whether the defense was CORRECT. Sites 3-4
are the original, and carried the bug from the start.

MUTATION-VERIFIED: delete any one `if current == 0:` / `and current_world == 0`
guard and exactly that site's test fails. The POSITIVE CONTROLS below are
load-bearing: a zero-guard written too broadly (e.g. gating on `diff < 0` itself)
would disable the heal entirely, re-introducing the g-115-1944 starvation — and
every "does not write" assertion here would STILL PASS.
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
    spec = importlib.util.spec_from_file_location(modname, str(SCRIPT_DIR / f"{stem}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══════════════════ site 1 — felt-sense-cadence-check.py ══════════════════

@pytest.fixture
def fs_mod():
    return _load("felt-sense-cadence-check", "fs_zero_guard")


def _fs_wire(monkeypatch, mod, *, current, last_count, cadence=75):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: {
        "felt_sense": {"goal_cadence": cadence, "wm_slot": "last_felt_sense_checkin"}
    })
    monkeypatch.setattr(mod, "count_completed_goals", lambda *_a, **_k: current)
    monkeypatch.setattr(mod, "wm_slot_value", lambda slot_name=mod.SLOT_NAME: (
        {"goals_completed_this_session": 999} if slot_name == "loop_state"
        else {"timestamp": "2026-07-11T14:58:00", "goals_count_at_last_fire": last_count}
    ))
    monkeypatch.setattr(sys, "argv", ["felt-sense-cadence-check.py"])
    writes = []
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: (
        writes.append(kw.get("input")),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )[1])
    return writes


def test_felt_sense_failed_measurement_does_not_rebaseline(fs_mod, monkeypatch, capsys):
    """current=0 is the counter's FAILURE SENTINEL, not a basis. Noop, no write."""
    writes = _fs_wire(monkeypatch, fs_mod, current=0, last_count=5351)

    rc = fs_mod.main()
    err = capsys.readouterr().err

    assert rc == 1, "a failed measurement must NOOP"
    assert writes == [], (
        "THE LOAD-BEARING ASSERTION: the heal must NOT persist the counter's "
        "failure sentinel as the new basis. Writing goals_count_at_last_fire=0 "
        "turns a self-recovering transient error into permanent corruption AND "
        "spuriously fires the ritual next iteration via the first-fire path."
    )
    assert "FAILED MEASUREMENT" in err, "the refusal must be LOUD on stderr, not silent"


def test_felt_sense_positive_control_real_negative_diff_still_heals(fs_mod, monkeypatch, capsys):
    """POSITIVE CONTROL. A guard written too broadly would disable the heal and
    re-introduce the g-115-1944 starvation — while the assertion above still passed."""
    writes = _fs_wire(monkeypatch, fs_mod, current=5351, last_count=5686)

    rc = fs_mod.main()
    assert rc == 1
    assert len(writes) == 1, (
        "a GENUINE negative diff (current=5351 > 0) must STILL re-baseline. If "
        "this fails, the zero-guard swallowed the heal it was meant to protect."
    )
    payload = json.loads(writes[0])
    assert payload["goals_count_at_last_fire"] == 5351
    assert payload["rebaselined_from"] == 5686


# ════════════════════════ site 2 — l1-skew-check.py ════════════════════════

@pytest.fixture
def l1_mod():
    return _load("l1-skew-check", "l1_zero_guard")


def _l1_wire(monkeypatch, mod, *, current, last_count, cadence=50):
    monkeypatch.setattr(mod, "_load_cadence_config",
                        lambda: {"goal_cadence": cadence, "wm_slot": "last_l1_skew_check"})
    monkeypatch.setattr(mod, "_count_completed_goals", lambda: current)
    monkeypatch.setattr(mod, "_wm_read", lambda _slot: {
        "timestamp": "2026-07-11T12:05:14", "goals_count_at_last_fire": last_count})
    writes = []
    monkeypatch.setattr(mod, "_wm_set", lambda slot, value: writes.append((slot, value)))
    return writes


def test_l1_skew_failed_measurement_does_not_rebaseline(l1_mod, monkeypatch, capsys):
    """_count_completed_goals() returns 0 on rc!=0, TimeoutExpired, OSError, AND
    unparseable stdout — four routine ways to manufacture a fake zero."""
    writes = _l1_wire(monkeypatch, l1_mod, current=0, last_count=5664)

    fire, current, _cfg, _last = l1_mod._cadence_gate()
    err = capsys.readouterr().err

    assert fire is False, "a failed measurement must not fire"
    assert writes == [], (
        "THE LOAD-BEARING ASSERTION: the heal must NOT persist the failure "
        "sentinel. A 10-second subprocess timeout must not cost the cadence basis."
    )
    assert current == 0
    assert "FAILED MEASUREMENT" in err, "the refusal must be LOUD on stderr"


def test_l1_skew_positive_control_real_negative_diff_still_heals(l1_mod, monkeypatch):
    """POSITIVE CONTROL — see the felt-sense twin."""
    writes = _l1_wire(monkeypatch, l1_mod, current=5351, last_count=5664)

    fire, _current, _cfg, _last = l1_mod._cadence_gate()
    assert fire is False
    assert len(writes) == 1, (
        "a GENUINE negative diff must STILL re-baseline — the zero-guard must not "
        "swallow the heal."
    )
    assert writes[0][1]["goals_count_at_last_fire"] == 5351
    assert writes[0][1]["rebaselined_from"] == 5664


# ═════════════ sites 3 & 4 — fresh-eyes-cadence-check.py ═════════════

@pytest.fixture
def fe_mod():
    return _load("fresh-eyes-cadence-check", "fe_zero_guard")


def _fe_wire(monkeypatch, mod, *, current, last_count, current_world=None,
             team_stamp=None, team_aware=False, cadence=25):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: {
        "fresh_eyes_review": {
            "goal_cadence": cadence,
            "wm_slot": "last_fresh_eyes_review",
            **({"team_aware": True} if team_aware else {}),
        }
    })
    monkeypatch.setattr(
        mod, "count_completed_goals",
        lambda world_only=False: (current_world if world_only else current),
    )
    monkeypatch.setattr(mod, "wm_slot_value", lambda slot_name: (
        {} if slot_name == "loop_state"
        else {"timestamp": "2026-06-01T00:00:00", "goals_count_at_last_fire": last_count}
    ))
    monkeypatch.setattr(mod, "team_stamp_value", lambda _slot: team_stamp)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])

    wm_writes = []
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: (
        wm_writes.append(kw.get("input")),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )[1])

    rt_writes = []
    monkeypatch.setattr(mod._rt, "rt_call",
                        lambda method, path, query=None, body=None, headers=None: (
                            rt_writes.append(query), "{}")[1])
    return wm_writes, rt_writes


def test_fresh_eyes_per_agent_failed_measurement_does_not_rebaseline(fe_mod, monkeypatch, capsys):
    """Site 3 — the ORIGINAL heal (). The bug shipped here first; the
    g-115-1944 port copied it into two more gates."""
    wm_writes, _rt_writes = _fe_wire(monkeypatch, fe_mod, current=0, last_count=5416)

    rc = fe_mod.main()
    err = capsys.readouterr().err

    assert rc == 1
    assert wm_writes == [], (
        "the ORIGINAL negative-diff heal must also refuse the failure sentinel — "
        "this is not a bug the port introduced, it is one the port PROPAGATED."
    )
    assert "FAILED MEASUREMENT" in err


def test_fresh_eyes_team_failed_measurement_does_not_rebaseline_shared_state(
        fe_mod, monkeypatch, capsys):
    """Site 4 — THE WORST CASE. This branch writes world/team-state.yaml
    shared_cadences, so re-baselining on a failure sentinel corrupts the cadence
    basis for the ENTIRE FLEET, not just this agent.

    The scenario is the most realistic of the four on an own-cloud box:
    world_only=True reads ONLY the S3-backed world files while the full count also
    reads agent-local files. A world-store read failure therefore yields a HEALTHY
    per-agent diff (100-70=30 >= 25, so the team gate is reached) alongside a FAKE
    ZERO world count."""
    _wm_writes, rt_writes = _fe_wire(
        monkeypatch, fe_mod,
        current=100, last_count=70,          # per-agent diff=30 >= 25 -> reach team gate
        current_world=0,                     # <-- the failure sentinel
        team_stamp={"timestamp": "2026-07-01T12:00:00",
                    "world_goals_count_at_last_fire": 900,
                    "fired_by": "bravo"},
        team_aware=True,
    )

    rc = fe_mod.main()
    err = capsys.readouterr().err

    assert rc == 1, "a failed world measurement must noop, not fire the ritual"
    assert rt_writes == [], (
        "THE LOAD-BEARING ASSERTION FOR SHARED STATE: the team heal must NOT "
        "re-stamp shared_cadences from a failure sentinel. One agent's transient "
        "read failure must never corrupt every other agent's cadence basis."
    )
    assert "FAILED MEASUREMENT" in err
    assert "SHARED" in err, "the shared-state blast radius must be named in the refusal"


def test_fresh_eyes_team_positive_control_real_negative_diff_still_heals(
        fe_mod, monkeypatch, capsys):
    """POSITIVE CONTROL for site 4 — a genuine downward world-count correction
    (census repair) must STILL re-stamp the shared cadence."""
    _wm_writes, rt_writes = _fe_wire(
        monkeypatch, fe_mod,
        current=100, last_count=70,
        current_world=600,                   # genuine, non-zero
        team_stamp={"timestamp": "2026-07-01T12:00:00",
                    "world_goals_count_at_last_fire": 900,
                    "fired_by": "bravo"},
        team_aware=True,
    )

    rc = fe_mod.main()
    assert rc == 1
    assert len(rt_writes) == 1, (
        "a GENUINE negative team diff (current_world=600 > 0) must STILL re-baseline "
        "the shared cadence — the zero-guard must not swallow the g-115-1941 heal."
    )
    stamp = json.loads(rt_writes[0]["value"])
    assert stamp["world_goals_count_at_last_fire"] == 600
    assert stamp["rebaselined_from"] == 900


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
