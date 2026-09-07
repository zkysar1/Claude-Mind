""" — the precheck budget meter must decide on the LIVE zone.

`aspirations-precheck-budget-meter.sh` reads the context zone ONCE, in `start`,
and stores it in precheck-budget-state.json. `check` then consulted that
SNAPSHOT forever. The event most likely to land mid-precheck is an autocompact,
and an autocompact is exactly what FREES context, so the snapshot goes stale
reliably in the RESTRICTIVE direction: a pre-compaction 'tight' is held for the
whole window while the true zone is 'fresh', and every deferrable lane -- all
seven cadence rituals, the ratchets, the reclaim lanes, every *-audit -- is
dropped on a precheck that then reports clean (guard-6050).

These tests drive the REAL script as a subprocess against a tmp agent dir, so
they exercise the shell arm-dispatch, the env plumbing and the embedded python
together rather than a re-implementation of the decision.

THE LOAD-BEARING CASE IS `test_a_stale_sensor_still_drops`. Without it, the
fresh-flips-to-run case could pass for the wrong reason -- a fail-open anywhere
in the chain produces 'run' too. That case pins that the drop path is genuinely
live in this harness AND that the mtime comparison is what admits the refresh:
same seeded 'fresh' sensor, only its mtime moved behind start_ms, opposite
verdict (guard-1220: prove the discriminator two ways, not one).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

_SCRIPTS = Path(__file__).resolve().parent.parent
_METER = _SCRIPTS / "aspirations-precheck-budget-meter.sh"

# A deferrable sweep that is NOT retrospection-class. `pending-questions-sweep`
# (the name the filing goal used) is in _precheck_budget_reserve's
# RETROSPECTION_SWEEPS, so a tight-zone drop on it can be legitimately overridden
# back to 'run' by the reservation -- which would make a 'drop' assertion pass or
# fail for reasons that have nothing to do with the zone.
DEFERRABLE = "locus-sweep"

START_MS = 1_700_000_000_000          # a fixed, long-past meter start
_START_S = START_MS / 1000.0


def _meter(agent_dir, *args):
    env = dict(os.environ)
    # _paths.sh's documented test-only override seam -- keeps the fixture out of
    # the live agents/ tree.
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env.pop("BODY_ROLE", None)
    r = subprocess.run(
        # guard-581: .as_posix(), never str(Path).
        [BASH, _METER.as_posix(), *args],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(_SCRIPTS.parent.parent),
    )
    return r.returncode, r.stdout.strip(), r.stderr


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True)
    return d


def _seed_state(agent_dir, snapshot_zone="tight"):
    """A meter window already open, frozen at `snapshot_zone`."""
    state = {
        "start_ms": START_MS,
        "cap_ms": 9000,
        "budget_pct": 10,
        "iteration_budget_ms": 90000,
        "zone": snapshot_zone,
        "zone_drop_rules": {"tight": ["deferrable"]},
        "sweeps": [],
    }
    p = agent_dir / "session" / "precheck-budget-state.json"
    p.write_text(json.dumps(state), encoding="utf-8")
    return p


def _seed_sensor(agent_dir, zone, *, newer=True, raw=None):
    """Write context-budget.json, with its mtime either side of START_MS."""
    p = agent_dir / "session" / "context-budget.json"
    p.write_text(
        raw if raw is not None else json.dumps({"zone": zone, "used_pct": 1}),
        encoding="utf-8",
    )
    stamp = _START_S + 600 if newer else _START_S - 600
    os.utime(p, (stamp, stamp))
    return p


def _read_state(agent_dir):
    return json.loads(
        (agent_dir / "session" / "precheck-budget-state.json").read_text(encoding="utf-8")
    )


# ── the defect this goal fixes ───────────────────────────────────────────────

def test_a_live_fresh_sensor_overrides_a_frozen_tight_snapshot(agent_dir):
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, "fresh", newer=True)
    rc, out, _ = _meter(agent_dir, "check", DEFERRABLE)
    assert rc == 0
    assert out == "run", f"stale 'tight' snapshot still governed: {out!r}"


def test_the_refreshed_zone_is_written_back_to_the_state(agent_dir):
    """The precheck-end record must report the zone that actually GOVERNED."""
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, "fresh", newer=True)
    _meter(agent_dir, "check", DEFERRABLE)
    st = _read_state(agent_dir)
    assert st["zone"] == "fresh"
    assert st.get("zone_source") == "live-sensor"
    assert st.get("zone_refreshed_at_ms", 0) > START_MS


# ── the anti-vacuity control: the drop path is live, and mtime is the gate ──

def test_a_stale_sensor_still_drops(agent_dir):
    """Same 'fresh' sensor, mtime moved BEHIND start_ms -> the snapshot governs.

    This is the discriminating half. It proves the drop path really fires in
    this harness (so a 'run' elsewhere is a decision, not a fail-open) and that
    the mtime comparison is what admits the refresh.
    """
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, "fresh", newer=False)
    rc, out, _ = _meter(agent_dir, "check", DEFERRABLE)
    assert rc == 0
    assert out == "drop", f"expected the frozen snapshot to govern, got {out!r}"


def test_a_live_tight_sensor_still_drops_no_over_correction(agent_dir):
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, "tight", newer=True)
    rc, out, _ = _meter(agent_dir, "check", DEFERRABLE)
    assert rc == 0
    assert out == "drop", f"over-corrected a genuinely tight window: {out!r}"


# ── fail-open: a sensor fault must never manufacture a drop ─────────────────

def test_an_absent_sensor_fails_open_to_run(agent_dir):
    _seed_state(agent_dir, "tight")
    assert not (agent_dir / "session" / "context-budget.json").exists()
    rc, out, _ = _meter(agent_dir, "check", DEFERRABLE)
    assert rc == 0
    assert out == "run", f"an absent sensor manufactured a drop: {out!r}"


def test_an_unreadable_sensor_fails_open_to_run(agent_dir):
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, None, newer=True, raw="{not json at all")
    rc, out, _ = _meter(agent_dir, "check", DEFERRABLE)
    assert rc == 0
    assert out == "run", f"an unreadable sensor manufactured a drop: {out!r}"


def test_a_sensor_with_no_zone_key_fails_open_to_run(agent_dir):
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, None, newer=True, raw=json.dumps({"used_pct": 99}))
    rc, out, _ = _meter(agent_dir, "check", DEFERRABLE)
    assert rc == 0
    assert out == "run", f"a zone-less sensor manufactured a drop: {out!r}"


# ── the always-run tier is untouched by any of this ─────────────────────────

def test_always_run_is_unaffected_by_the_zone(agent_dir):
    _seed_state(agent_dir, "tight")
    _seed_sensor(agent_dir, "tight", newer=True)
    rc, out, _ = _meter(agent_dir, "check", "tree-debt-gate")
    assert rc == 0
    assert out == "run"
