"""Pins the five verdict transitions of core/scripts/domain-term-ratchet.py
plus its persist-failure contract (g-115-10049 outcome 5).

The census is STUBBED rather than run. That is deliberate and is why this file
exists instead of a row in test_ratchet_persist_failure_family.py: the real
`domain-term-census.py` scans ~3,300 files and voids itself unless a FULL world
is present (forged-skills.yaml, program.md, conventions/ -- guard-2081, "no
partial census"). Satisfying all of that would make a ~90s test whose green
depends on registry contents that have nothing to do with the ratchet's logic.

THE `skipped` TEST IS THE LOAD-BEARING ONE. A gap count of 0 has two causes
that are byte-identical in the output -- every registry id is blocklisted, or
the registry read returned nothing -- and only the POPULATION separates them.
Because a ratchet never raises its baseline, one blind run seeding 0 would pin
the floor at 0 forever and every later real regression would sit under a
baseline it can never reach. So `registry_terms == 0` must leave the baseline
untouched, and the assertion here is on the FILE, not just the verdict string.
"""
import importlib.util
import json
import sys

import pytest
import yaml

from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "domain_term_ratchet", CORE_SCRIPTS / "domain-term-ratchet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dtr():
    return _load()


def _census(terms=6, blocklisted=1, files=3313, gaps=("a", "b", "c", "d", "e")):
    """A census payload shaped exactly like domain-term-census.py --json."""
    return {
        "files_scanned": files,
        "by_source": {"environments/id": {"terms": terms,
                                          "blocklisted": blocklisted}},
        "rows": [{"term": t, "sources": ["environments/id"],
                  "blocklisted": False} for t in gaps],
    }


def _run(dtr, monkeypatch, capsys, tmp_path, census, prior=None):
    """Run main() against a temp baselines file; return (result, on_disk)."""
    baselines = tmp_path / "audit-baselines.yaml"
    if prior is not None:
        baselines.write_text(
            yaml.safe_dump({dtr.KEY: {"baseline": prior}}), encoding="utf-8")
    monkeypatch.setattr(dtr, "BASELINES_PATH", baselines)
    monkeypatch.setattr(dtr, "_run_census", lambda: census)
    monkeypatch.setattr(sys, "argv", ["domain-term-ratchet.py", "--json"])
    rc = dtr.main()
    assert rc == 0, "advisory ratchet must exit 0 without the hard gate"
    result = json.loads(capsys.readouterr().out)
    on_disk = (yaml.safe_load(baselines.read_text(encoding="utf-8"))
               if baselines.exists() else None)
    return result, on_disk


def test_seeded_when_no_prior_baseline(dtr, monkeypatch, capsys, tmp_path):
    result, on_disk = _run(dtr, monkeypatch, capsys, tmp_path, _census())
    assert result["verdict"] == "seeded"
    assert result["baseline"] == 5          # 6 registry ids - 1 blocklisted
    assert on_disk[dtr.KEY]["baseline"] == 5
    assert on_disk[dtr.KEY]["scope"] == "environments/id"


def test_stable_when_unchanged(dtr, monkeypatch, capsys, tmp_path):
    result, _ = _run(dtr, monkeypatch, capsys, tmp_path, _census(), prior=5)
    assert result["verdict"] == "stable"
    assert result["baseline"] == 5


def test_regressed_never_raises_the_baseline(dtr, monkeypatch, capsys, tmp_path):
    """A new peer id leaking into core must WARN without moving the floor.

    guard-7161: merge_audit_baselines merges `baseline` by MIN, so a raised
    baseline is reverted at the next merge and every peer then reads a false
    REGRESSED. The ratchet must therefore hold the old floor itself.
    """
    census = _census(terms=7, gaps=("a", "b", "c", "d", "e", "f"))
    result, on_disk = _run(dtr, monkeypatch, capsys, tmp_path, census, prior=5)
    assert result["verdict"] == "regressed"
    assert result["current"]["gaps"] == 6
    assert result["baseline"] == 5, "baseline must NOT be raised on regression"
    assert on_disk[dtr.KEY]["baseline"] == 5


def test_ratcheted_lowers_the_baseline(dtr, monkeypatch, capsys, tmp_path):
    census = _census(blocklisted=3, gaps=("a", "b", "c"))
    result, on_disk = _run(dtr, monkeypatch, capsys, tmp_path, census, prior=5)
    assert result["verdict"] == "ratcheted"
    assert result["baseline"] == 3
    assert on_disk[dtr.KEY]["baseline"] == 3


def test_empty_registry_is_skipped_and_leaves_the_baseline_untouched(
        dtr, monkeypatch, capsys, tmp_path):
    """The vacuous-run guard (see module docstring) -- the one that matters."""
    census = _census(terms=0, blocklisted=0, files=0, gaps=())
    result, on_disk = _run(dtr, monkeypatch, capsys, tmp_path, census, prior=5)
    assert result["verdict"] == "skipped", (
        "a census that measured ZERO registry ids must not be ratcheted")
    assert result["baseline"] == 5
    assert on_disk[dtr.KEY]["baseline"] == 5, "baseline was modified"

    # POSITIVE CONTROL: the same run shape with a real population does NOT
    # skip. Without this, the test passes against a main() that skips
    # unconditionally -- the failure direction a lone negative cannot see.
    result2, _ = _run(dtr, monkeypatch, capsys, tmp_path, _census(), prior=5)
    assert result2["verdict"] == "stable"


def test_failed_persist_reports_error_not_the_computed_verdict(
        dtr, monkeypatch, capsys, tmp_path):
    """ family contract: never claim a write that did not land."""
    def _boom(path, modifier_fn, initial=None):
        modifier_fn(dict(initial or {}))      # populate `captured` first
        raise OSError("simulated write failure AFTER the modifier ran")

    monkeypatch.setattr(dtr, "BASELINES_PATH", tmp_path / "audit-baselines.yaml")
    monkeypatch.setattr(dtr, "_run_census", lambda: _census())
    monkeypatch.setattr(dtr, "locked_modify_yaml", _boom)
    monkeypatch.setattr(sys, "argv", ["domain-term-ratchet.py", "--json"])
    assert dtr.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "error"
    assert result["baseline"] is None
    assert "did NOT take effect" in result["message"]
