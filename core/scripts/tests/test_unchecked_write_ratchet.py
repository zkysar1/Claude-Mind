"""test_unchecked_write_ratchet.py — .

Pins the four verdict transitions of core/scripts/unchecked-write-ratchet.py
and, most importantly, the VACUOUS-RUN GUARD.

Why the skipped-guard test is the load-bearing one. unchecked-write-audit.py
reports verdict="skipped" when wrapper discovery or the SKILL.md glob comes
back empty; in that state its `unverified` is 0, which is indistinguishable
from a codebase with zero unchecked writes. A ratchet only ever SHRINKS, so
seeding or ratcheting on that vacuous 0 would permanently lock the baseline at
0 and silently declare the drift solved. The audit's own source names this
goal in a comment for exactly this reason (rb-245). A test that only feeds
healthy audit output cannot tell the guarded implementation from an unguarded
one, so the skipped case is asserted directly against the persisted file.

Hermetic: patches the module's BASELINES_PATH to a tmp file and patches
_run_audit, so neither the live meta/audit-baselines.yaml nor the real audit
subprocess is touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    pytest.skip("PyYAML required", allow_module_level=True)

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_module():
    """Import the hyphenated script as a module (no importable name)."""
    spec = importlib.util.spec_from_file_location(
        "unchecked_write_ratchet", CORE_SCRIPTS / "unchecked-write-ratchet.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _audit(unverified, verified, verdict="CONFIRMED", call_sites=None):
    if call_sites is None:
        call_sites = unverified + verified
    return {
        "verified": verified,
        "unverified": unverified,
        "verdict": verdict,
        "population": {
            "write_wrappers": 80,
            "read_wrappers": 28,
            "skill_files": 90,
            "call_sites": call_sites,
        },
    }


@pytest.fixture()
def ratchet(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "BASELINES_PATH", tmp_path / "audit-baselines.yaml")
    return mod


def _run(mod, audit_payload, monkeypatch):
    monkeypatch.setattr(mod, "_run_audit", lambda: audit_payload)
    # main() parses sys.argv, which under pytest is pytest's own argv — argparse
    # would exit(2) on "unrecognized arguments". Pin a clean argv instead of
    # reshaping the script away from its sibling ratchets.
    monkeypatch.setattr(sys, "argv", ["unchecked-write-ratchet.py"])
    return mod.main()


def _read(mod):
    p = mod.BASELINES_PATH
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def test_seeds_on_first_run(ratchet, monkeypatch):
    assert _run(ratchet, _audit(464, 73), monkeypatch) == 0
    entry = _read(ratchet)[ratchet.KEY]
    assert entry["baseline"] == 464
    assert entry["last_verdict"] == "seeded"
    assert entry["history"][-1]["drift_total"] == 464


def test_entry_records_which_matcher_it_counts(ratchet, monkeypatch):
    """The baseline integer is meaningless without naming the matcher.

    The audit emits both a strict count and an over-generous band. Switching
    the ratchet to the band would leave every prior history entry silently
    incomparable, so the entry itself must say which one it tracks. Pinned
    because an unpinned field can be dropped by a later edit with no symptom.
    """
    _run(ratchet, _audit(464, 73), monkeypatch)
    assert _read(ratchet)[ratchet.KEY]["matcher"] == "strict_unverified"


def test_stable_when_unchanged(ratchet, monkeypatch):
    _run(ratchet, _audit(464, 73), monkeypatch)
    assert _run(ratchet, _audit(464, 73), monkeypatch) == 0
    entry = _read(ratchet)[ratchet.KEY]
    assert entry["baseline"] == 464
    assert entry["last_verdict"] == "stable"


def test_ratchets_down_when_drift_shrinks(ratchet, monkeypatch):
    _run(ratchet, _audit(464, 73), monkeypatch)
    _run(ratchet, _audit(400, 137), monkeypatch)
    entry = _read(ratchet)[ratchet.KEY]
    assert entry["baseline"] == 400
    assert entry["last_verdict"] == "ratcheted"


def test_regression_does_not_raise_the_baseline(ratchet, monkeypatch):
    """A ratchet must never move UP — that is what makes it a ratchet."""
    _run(ratchet, _audit(464, 73), monkeypatch)
    _run(ratchet, _audit(500, 37), monkeypatch)
    entry = _read(ratchet)[ratchet.KEY]
    assert entry["baseline"] == 464, "regression must not raise the baseline"
    assert entry["last_verdict"] == "regressed"


# --- the load-bearing guard -------------------------------------------------

def test_skipped_audit_does_not_seed(ratchet, monkeypatch):
    """An empty-population audit must NOT create a baseline at 0.

    Without the guard this seeds baseline=0 and, because a ratchet only
    shrinks, every subsequent real measurement reads as a regression forever.
    """
    assert _run(ratchet, _audit(0, 0, verdict="skipped", call_sites=0),
                monkeypatch) == 0
    assert ratchet.KEY not in _read(ratchet), \
        "skipped audit must not create a baseline entry"


def test_skipped_audit_leaves_existing_baseline_untouched(ratchet, monkeypatch):
    """The worse direction: a skipped run must not ratchet an existing baseline to 0."""
    _run(ratchet, _audit(464, 73), monkeypatch)
    before = _read(ratchet)[ratchet.KEY]
    assert _run(ratchet, _audit(0, 0, verdict="skipped", call_sites=0),
                monkeypatch) == 0
    after = _read(ratchet)[ratchet.KEY]
    assert after["baseline"] == 464
    assert after["last_verdict"] == before["last_verdict"], \
        "skipped run must not record a verdict"
    assert len(after["history"]) == len(before["history"]), \
        "skipped run must not append history"


def test_history_is_bounded(ratchet, monkeypatch):
    for _ in range(60):
        _run(ratchet, _audit(464, 73), monkeypatch)
    assert len(_read(ratchet)[ratchet.KEY]["history"]) == 50


def test_population_dict_does_not_crash(ratchet, monkeypatch):
    """`population` is a DICT of sub-counts, not an int — int() on it raises.

    This is not hypothetical: the first implementation did exactly that and
    died with TypeError on its very first run.
    """
    assert _run(ratchet, _audit(464, 73), monkeypatch) == 0
    bd = _read(ratchet)[ratchet.KEY]["history"][-1]["breakdown"]
    assert bd["call_sites"] == 537
    assert bd["write_wrappers"] == 80
