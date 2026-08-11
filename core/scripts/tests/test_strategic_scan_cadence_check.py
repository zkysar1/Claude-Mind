"""Tests for strategic-scan-cadence-check.py decide() + its wiring ().

The strategic-scan cadence had NO bash gate at all: orchestrator Phase 1.5 is an
LLM-enumerated conditional, and nothing in bash read `last_strategic_scan` for a
cadence decision — so the ritual starved (19.5h against a 4h cadence, alpha /
cc-04 / 2026-08-02). This gate is the bash half, registered in
_cadence_registry so the Phase 0.5e battery runs it unconditionally.

Two halves, deliberately:
  * decide() — the pure decision (fire/noop, unset vs unparseable, boundary).
  * wiring   — STRUCTURAL assertions that the gate is actually reachable. Learned
    from g-306-124: a registration pinned only by asserting downstream BEHAVIOUR
    is vacuous when a default already produces that behaviour. Here the analogous
    trap is the budget meter — an unregistered sweep name silently falls through
    sweep_tier()'s `*)` arm to `medium` with only a stderr WARN, so every
    behavioural check still passes while the tier is wrong. That has to be
    asserted on the wiring or not at all.

Pattern: same importlib + sys.path shape as test_evolution_cadence_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "strategic-scan-cadence-check.py"

# Fixed reference "now" so age math is deterministic across machines.
NOW = dt.datetime(2026, 8, 2, 22, 0, 0)
NOW_EPOCH = NOW.timestamp()
CADENCE = 4.0   # strategic_scan.hours_cadence


def _import():
    spec = importlib.util.spec_from_file_location("strategic_scan_cadence_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["strategic_scan_cadence_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _epoch(iso: str) -> float:
    return dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").timestamp()


# ------------------------------------------------------------------ decide ----

def test_fire_when_stale():
    """The measured starvation shape: 19.5h against a 4h cadence -> fire."""
    m = _import()
    last = "2026-08-02T02:30:00"  # 19.5h before NOW
    code, msg, warn = m.decide(last, _epoch(last), CADENCE, NOW_EPOCH)
    assert code == 0, msg
    assert "fire" in msg and "19.5h" in msg
    assert warn is None


def test_noop_when_fresh():
    """The live cc-02 reading at fix time: 3.9h against a 4h cadence -> noop.

    Pinned as a real observation rather than a round number: this is the value
    the gate returned on its first canonical invocation, so a regression that
    makes the gate fire early reddens against something that actually happened.
    """
    m = _import()
    last = "2026-08-02T18:05:00"  # 3.9166h before NOW
    code, msg, warn = m.decide(last, _epoch(last), CADENCE, NOW_EPOCH)
    assert code == 1, msg
    assert "noop" in msg and "3.9h" in msg
    assert warn is None


def test_fire_when_unset():
    """No stamp at all -> the scan never ran -> fire (not a silent noop)."""
    m = _import()
    code, msg, warn = m.decide(None, None, CADENCE, NOW_EPOCH)
    assert code == 0
    assert "unset" in msg
    assert warn is None


def test_unparseable_stamp_is_loud_noop():
    """A corrupt stamp fails open to NOOP and WARNS (guard-424).

    NOOP, not FIRE: re-firing the scan every iteration on a corrupt stamp is the
    worse failure. Phase 1.5 or a corrected stamp still fires it.
    """
    m = _import()
    code, msg, warn = m.decide("not-a-timestamp", None, CADENCE, NOW_EPOCH)
    assert code == 1
    assert msg == ""
    assert warn and "unparseable" in warn


def test_cadence_boundary_is_inclusive():
    """Exactly at the cadence fires — `>=`, matching every sibling gate."""
    m = _import()
    last = "2026-08-02T18:00:00"  # exactly 4.0h before NOW
    code, msg, _ = m.decide(last, _epoch(last), CADENCE, NOW_EPOCH)
    assert code == 0, msg
    assert "4.0h >= cadence 4h" in msg


def test_gate_never_writes_the_slot():
    """guard-155: Phase S5 is the single writer of last_strategic_scan.

    Asserted on the SOURCE, not on behaviour: decide() is pure, so no behavioural
    test can distinguish "does not write" from "was not asked to write".

    Scans EXECUTABLE lines only — the module docstring names `verified-wm-set.sh`
    when explaining who the single writer IS, and a whole-file substring scan
    reddened on that sentence. A source-scanning assertion has to exclude the
    prose that legitimately discusses the thing it forbids, or documenting the
    rule breaks the test for the rule (guard-1099 class: an unanchored scan that
    counts comments as code, here in the opposite direction).
    """
    import ast

    src = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    doc = ast.get_docstring(tree, clean=False) or ""
    code_lines = [
        ln for ln in src.replace(doc, "", 1).splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    for writer in ("wm-set", "wm_set", "verified-wm-set", "wm_write"):
        assert writer not in code, f"gate must not write WM (found {writer!r})"
    assert "_rt.wm_read" in code, "gate should read the slot via the daemon client"


# ------------------------------------------------------------------ wiring ----

def test_registered_in_cadence_registry_with_this_script():
    """The gate is reachable from the Phase 0.5e battery, and points HERE."""
    sys.path.insert(0, str(SCRIPTS))
    import _cadence_registry as reg

    entries = [c for c in reg.cadences() if c["name"] == "strategic-scan"]
    assert len(entries) == 1, "strategic-scan must be registered exactly once"
    e = entries[0]
    assert e["check_cmd"] == ["strategic-scan-cadence-check.sh"]
    assert e["meter_name"] == "strategic-scan-cadence"
    assert "aspirations-strategic-scan" in e["fire_dispatch"]
    assert (SCRIPTS / e["check_cmd"][0]).exists()


def test_meter_classifies_the_sweep_as_deferrable_not_the_default():
    """The silent-default trap this test exists for.

    aspirations-precheck-budget-meter.sh `sweep_tier()` routes any unknown name
    to `medium` via its `*)` arm, warning only on stderr. So forgetting the
    registration leaves the gate fully working and merely mis-tiered — invisible
    to every behavioural check. Assert the name is IN the deferrable arm, which
    is the one thing that cannot be inferred from the gate running correctly.
    """
    meter = (SCRIPTS / "aspirations-precheck-budget-meter.sh").read_text(encoding="utf-8")
    deferrable_arm = [
        ln for ln in meter.splitlines()
        if ln.lstrip().startswith("pending-questions-sweep|")
    ]
    assert len(deferrable_arm) == 1, "could not locate sweep_tier()'s deferrable case arm"
    assert "strategic-scan-cadence" in deferrable_arm[0], (
        "strategic-scan-cadence is missing from sweep_tier()'s deferrable arm — "
        "it would silently default to medium tier"
    )
