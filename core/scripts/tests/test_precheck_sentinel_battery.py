"""Tests for precheck-sentinel-battery.py + _sentinel_registry.py ().

Pins:
  1. Battery lists a set sentinel and omits null/absent ones.
  2. fired_key dicts with fired!=true are NOT listed (matches the consumer
     phases' `IF signal.fired == true` gate and the canary's _is_set).
  3. Registry parity with stale-sentinel-canary: same tracked slot set, same
     consumption-aware map, same is_set semantics (the canary derives from the
     registry; these tests fail if either side re-hardcodes).
  4. Fail-open: missing WM file still exits 0 with structured JSON.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
BATTERY = SCRIPTS_DIR / "precheck-sentinel-battery.py"

sys.path.insert(0, str(SCRIPTS_DIR))

import _sentinel_registry as registry  # noqa: E402


def _load_canary():
    spec = importlib.util.spec_from_file_location(
        "stale_sentinel_canary", SCRIPTS_DIR / "stale-sentinel-canary.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_battery(wm_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(BATTERY), "--wm-path", str(wm_path), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_set_sentinel_listed_null_omitted(tmp_path):
    wm = {
        "slots": {
            "force_tree_maintain": {
                "triggered_at": "2026-07-16T00:00:00",
                "source": "encoding-drift",
                "threshold": 3,
            },
            "force_experience_archival": None,
            "fresh_eyes_dispatch_pending": "null",
            "pipeline_reconcile_pending": {
                "fired": True,
                "skill": "/domain-reconcile",
                "goals": ["g-1"],
            },
        }
    }
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert report["registered"] == len(registry.battery_slots())
    listed = {e["slot"] for e in report["set"]}
    assert listed == {"force_tree_maintain", "pipeline_reconcile_pending"}
    by_slot = {e["slot"]: e for e in report["set"]}
    assert by_slot["force_tree_maintain"]["phase"] == "0-pre"
    assert by_slot["force_tree_maintain"]["payload"]["source"] == "encoding-drift"
    assert "0-pre5" in by_slot["pipeline_reconcile_pending"]["phase"]
    assert all(e["dispatch"] for e in report["set"])


def test_fired_false_dict_not_listed(tmp_path):
    wm = {"slots": {"fresh_eyes_dispatch_pending": {"fired": False, "core_count": 0}}}
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert report["set"] == []


def test_all_null_reports_zero(tmp_path):
    wm = {"slots": {s["slot"]: None for s in registry.battery_slots()}}
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert report["set"] == []
    assert "error" not in report


def test_evolution_finalize_sentinel_surfaced(tmp_path):
    """ — the regression test the original gap did not have.

    Reproduces the real producer payload: evolution-stub-pending-check.sh emits
    a COUNT-shaped dict with NO `fired` key, which is why fired_key is False.
    Before registration the battery reported "all 6 registered sentinels null"
    against exactly this payload while 15 MATERIAL self.md stubs sat unseen for
    ~19h — a present consumer phase and an absent registry entry look fine in
    isolation, so asserting the registry file alone would not have caught it.
    """
    wm = {
        "slots": {
            "force_evolution_finalize": {
                "triggered_at": "2026-07-28T13:53:45",
                "count": 15,
                "material_count": 15,
                "threshold_minutes": 20.0,
                "stubs": [{"revision_id": "self-20260727T185759-bravo-4686"}],
            }
        }
    }
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    listed = {e["slot"] for e in report["set"]}
    assert "force_evolution_finalize" in listed, (
        f"battery must surface the evolution-finalize sentinel; got {listed}"
    )
    entry = next(e for e in report["set"] if e["slot"] == "force_evolution_finalize")
    assert entry["phase"] == "0-pre2.5"
    assert entry["payload"]["count"] == 15
    assert entry["dispatch"], "dispatch pointer must name the consumer phase"


def test_missing_wm_file_fails_open(tmp_path):
    report = _run_battery(tmp_path / "nope.yaml")
    assert report["error"] == "no_working_memory_file"
    assert report["set"] == []


def test_registry_parity_with_canary():
    canary = _load_canary()
    assert canary.TRACKED_SENTINELS == registry.canary_tracked_slots()
    assert canary.CONSUMPTION_AWARE == registry.consumption_aware_map()
    # The historical canary contract, pinned explicitly so a registry edit
    # that silently drops a tracked slot fails here (not just in behavior).
    # force_evolution_finalize added  — consumption-aware from
    # registration because its producer re-arms every iteration while any stub
    # is pending, so presence-count would fire on the correct never-fabricate
    # path (a stub left for the 24h expiry).
    assert set(canary.TRACKED_SENTINELS) == {
        "force_tree_encoding",
        "force_tree_maintain",
        "fresh_eyes_dispatch_pending",
        "force_metric_encoding_pending",
        "force_evolution_finalize",
    }
    assert canary.CONSUMPTION_AWARE == {
        "fresh_eyes_dispatch_pending": "fresh_eyes_last_dispatch",
        "force_tree_maintain": "force_tree_maintain_last_dispatch",
        "force_metric_encoding_pending": "force_metric_encoding_last_dispatch",
        "force_evolution_finalize": "force_evolution_finalize_last_dispatch",
    }


def test_is_set_semantics_shared():
    canary = _load_canary()
    cases = [
        (None, False),
        (False, False),
        ("null", False),
        ("", False),
        ("false", False),
        ("2026-07-16T00:00:00", True),
        ({"fired": False, "x": 1}, False),
        ({"fired": True}, True),
        ({"any": "keys"}, True),
        ({}, False),
        ([], False),
        ([1], True),
        (0, True),  # numeric zero is a meaningful value (defensive default)
    ]
    for value, expected in cases:
        assert registry.is_set(value) is expected, value
        assert canary._is_set(value) is expected, value


def test_battery_covers_all_precheck_phases():
    phases = {s["phase"] for s in registry.battery_slots()}
    # 0-pre2.5 added . Its consumer phase shipped with  but
    # was never registered, so the battery enumerated six and its "all N null —
    # no gates to dispatch" line authorised a SKIP past a gate it had not
    # checked. Measured unseen: 2 stubs / ~9h (zeta), 15 MATERIAL / ~19h (bravo).
    assert phases == {
        "0-pre", "0-pre2", "0-pre2.5", "0-pre3", "0-pre4", "0-pre5", "0-pre6",
    }


def _phase_rank(phase: str) -> float:
    """Protocol rank of a `0-preN[.M]` phase label.

    NOT a lexicographic sort. `sorted()` on these strings is wrong the moment a
    two-digit phase exists: "0-pre10" < "0-pre2" as text, so a correctly-placed
    0-pre10 would look misordered and the test would push the author to move it
    to the WRONG position to go green. Parse the numeric suffix instead.
    Caught by fresh-eyes probe on this file's own first draft (g-115-3678).
    """
    suffix = phase[len("0-pre"):]
    return float(suffix) if suffix else 0.0


def test_battery_registry_order_is_protocol_order():
    """The battery prints in REGISTRY order — it does not sort ().

    So a new entry appended at the end of SENTINELS surfaces its dispatch line
    out of protocol sequence, which is how the 0-pre2.5 entry first landed.
    Pinning order here keeps the list's implicit invariant explicit.
    """
    phases = [s["phase"] for s in registry.battery_slots()]
    ranks = [_phase_rank(p) for p in phases]
    assert ranks == sorted(ranks), (
        f"registry order must equal protocol order, got {phases}"
    )
    # Guard the guard: prove the rank function orders a two-digit phase the way
    # a lexicographic sort would not, so the fix cannot silently regress.
    assert _phase_rank("0-pre2") < _phase_rank("0-pre10")
    assert sorted(["0-pre10", "0-pre2"]) == ["0-pre10", "0-pre2"]  # text sort is wrong


# --- the composed-caller seam (2026-08-18) ----------------------------------
#
# Everything above pins what the battery COMPUTES. Nothing pinned whether the
# result REACHES the script that runs it, and that is where it broke: the
# battery emitted only `set`, while iteration-open.py lifts `payload["findings"]`
# / `payload["blind"]` by those key names. So `iteration-open.sh --apply` ran the
# lane, got rc=0, surfaced nothing, and printed "no findings; all dispatched
# lanes clean" while FOUR always-run gates sat set and undispatched (measured
# 2026-08-18, zeta/cc-02: force_tree_maintain, force_experience_archival,
# fresh_eyes_dispatch_pending, force_metric_encoding_pending).
#
# Every test above passed throughout — they had to, since the computation was
# correct the whole time. guard-1943: a green suite certifies the FUNCTION,
# never the WIRING. These two tests are the wiring.


def _load_iteration_open():
    spec = importlib.util.spec_from_file_location(
        "iteration_open", SCRIPTS_DIR / "iteration-open.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_set_sentinels_reach_the_composed_caller(tmp_path):
    """A SET sentinel must survive the hop into iteration-open's findings list.

    Asserted through the REAL consumer function against the REAL battery
    payload, not against a hand-built dict — a fixture of my own shape would
    re-encode the same assumption that caused the defect (guard-318: confirm
    the producer's shape, never the caller's intuition).
    """
    wm = {
        "slots": {
            "force_tree_maintain": {"triggered_at": "2026-08-18T00:00:00",
                                    "source": "encoding-drift", "threshold": 3},
            "fresh_eyes_dispatch_pending": {"fired": True, "core_count": 3,
                                            "loc_changed": 431},
        }
    }
    wm_path = tmp_path / "working-memory.yaml"
    wm_path.write_text(yaml.safe_dump(wm), encoding="utf-8")

    report = _run_battery(wm_path)
    assert len(report["set"]) == 2, "precondition: both sentinels are set"

    itopen = _load_iteration_open()
    lifted = itopen._findings_from("sentinel-battery", report)
    names = {f["name"] for f in lifted}
    assert names == {"force_tree_maintain", "fresh_eyes_dispatch_pending"}, (
        f"the composed caller must see every set sentinel, got {names} -- if this "
        f"is empty the battery is invisible inside iteration-open again and its "
        f"run will render as an all-clear"
    )
    assert all(f["detail"] for f in lifted), "a finding with no detail is unactionable"

    # NEGATIVE CONTROL: reproduce the pre-fix shape and prove it lifts nothing.
    # Without this the assertion above passes for a trivially green reason and
    # cannot distinguish a working seam from a consumer that accepts anything
    # (guard-2903/guard-2536: assert the path was REACHED, not just that no
    # error occurred).
    pre_fix = {k: v for k, v in report.items() if k != "findings"}
    assert itopen._findings_from("sentinel-battery", pre_fix) == [], (
        "the pre-fix payload must lift ZERO -- if it lifts anything, this test "
        "cannot detect the regression it exists for"
    )


def test_fail_open_reports_blind_rather_than_clean(tmp_path):
    """Fail-open must not read as fail-SILENT to a composed caller.

    An errored battery reaching iteration-open with zero findings AND zero
    blind entries is indistinguishable from a genuinely clean run, which would
    defeat that script's own "NO FINDINGS REACHED -- N lane(s) blind" branch.
    guard-4093: a lane that FAILED must never read as clean.
    """
    missing = tmp_path / "does-not-exist.yaml"
    report = _run_battery(missing)

    assert report.get("error"), "a missing WM file is an error condition"
    itopen = _load_iteration_open()
    blind = itopen._blind_from("sentinel-battery", report)
    assert blind, (
        "an errored battery must surface as BLIND to the composed caller; "
        "otherwise its failure is rendered as an all-clear"
    )
    assert itopen._findings_from("sentinel-battery", report) == [], (
        "and it must not manufacture findings it never computed"
    )
