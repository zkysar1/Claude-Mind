"""stranded_deploy_held: an open PR on a repo under an ACTIVE deploy hold is
PARKED, not stranded (g-115-7865).

Every classification test routes through classify_stranded in its production
arg shape rather than hand-building an entry. That is not style: this file's
`pull_request` dict is rebuilt field by field, and hand-built entries let two
separate forks ship INERT — g-115-6295 (`draft`) and g-306-304 (`body`) —
with green tests throughout (guard-920 / rb-5235).

Falsification: the held-case test FAILS against the pre-fix module (no
deploy_hold_status kwarg at all -> TypeError). The False/absent cases are the
positive controls and pass in BOTH worlds, so a green run is not vacuous.
"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "completed-not-committed-sweep.py"
NOW = dt.datetime(2026, 8, 26, 5, 0, 0)
_ON_DEFAULT = "a" * 40
_OFF_DEFAULT = "b" * 40


def _import():
    spec = importlib.util.spec_from_file_location(
        "completed_not_committed_sweep_dh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["completed_not_committed_sweep_dh"] = mod
    spec.loader.exec_module(mod)
    return mod


def _pr(number=53, hours_old=143.9):
    return {
        "state": "OPEN",
        "number": number,
        "url": f"https://github.com/zkysar1/Vinheim-Web-App/pull/{number}",
        "title": "fix(watch): CSP allow :443 ALB watchUrl",
        "created_at": (NOW - dt.timedelta(hours=hours_old)).isoformat(
            timespec="seconds") + "Z",
    }


def _goal():
    return {
        "id": "g-335-190",
        "status": "completed",
        "work_class": "product",
        "title": "Live watch validation",
        # 100h < lookback_hours (168) — inside the window. At 200h every case
        # short-circuits to None and even the positive controls "fail", which
        # reads as a broken fix rather than a broken fixture.
        "completed_at": (NOW - dt.timedelta(hours=100)).isoformat(
            timespec="seconds"),
        "_aspiration_id": "asp-335",
        "outcome_note": f"landed {_OFF_DEFAULT}",
    }


def _classify(mod, deploy_hold_status="__omit__"):
    kw = {"goalid_status": {"g-335-190": {_OFF_DEFAULT: True}}}
    if deploy_hold_status != "__omit__":
        kw["deploy_hold_status"] = deploy_hold_status
    return mod.classify_stranded(
        _goal(), NOW,
        {_OFF_DEFAULT: True},
        {_OFF_DEFAULT: False},
        {_OFF_DEFAULT: _pr()},
        **kw)


# ---------------------------------------------------------------- classify

def test_active_hold_reclassifies_to_benign_and_carries_probe_holders():
    """THE FIX. held=True -> benign tier, holders carried from the PROBE."""
    mod = _import()
    entry = _classify(mod, {"Vinheim-Web-App": {
        "held": True, "holders": ["g-326-660", "g-368-29"]}})
    assert entry is not None
    assert entry["reason"] == "stranded_deploy_held"
    assert entry["deploy_holders"] == ["g-326-660", "g-368-29"]
    # Top level, NOT inside the field-by-field pull_request rebuild.
    assert "deploy_holders" not in (entry["pull_request"] or {})


def test_held_entry_is_excluded_from_the_filing_set():
    """The filing set is keyed on reason == stranded_open_pr, so the carve-out
    must change the REASON — not merely annotate the entry."""
    mod = _import()
    entry = _classify(mod, {"Vinheim-Web-App": {"held": True, "holders": []}})
    assert entry["reason"] != "stranded_open_pr"


def test_decisive_clear_still_files():
    """POSITIVE CONTROL — passes pre- and post-fix. held=False is DECISIVE and
    must NOT be swallowed into the benign tier (guard-4028)."""
    mod = _import()
    entry = _classify(mod, {"Vinheim-Web-App": {"held": False, "holders": []}})
    assert entry["reason"] == "stranded_open_pr"
    assert "deploy_holders" not in entry


def test_fixture_produces_a_real_stranded_entry_without_the_new_kwarg():
    """THE BOTH-WORLDS POSITIVE CONTROL — the only test here that passes
    against BOTH the pre-fix and post-fix module, because it never mentions
    deploy_hold_status.

    Without it this file proves nothing: every other test touches the new
    kwarg, so 16/16 failing pre-fix is equally consistent with "the fix works"
    and with "the API simply did not exist yet". This one pins that the FIXTURE
    itself yields a genuine stranded_open_pr entry, so the other tests are
    discriminating behavior rather than API presence. (It also catches the
    lookback-window fixture bug that made all 16 short-circuit to None.)"""
    mod = _import()
    entry = _classify(mod)
    assert entry is not None, "fixture must yield a real entry, not None"
    assert entry["reason"] == "stranded_open_pr"
    assert entry["pull_request"]["number"] == 53


def test_unprobed_caller_behaves_exactly_as_before():
    """Backward-compat: an explicit None/{} map keeps the pre-existing verdict.
    This is merge_default_status's documented contract."""
    mod = _import()
    assert _classify(mod, None)["reason"] == "stranded_open_pr"
    assert _classify(mod, {})["reason"] == "stranded_open_pr"


def test_repo_absent_from_a_populated_map_is_no_information():
    """A map that answers about OTHER repos says nothing about this one."""
    mod = _import()
    entry = _classify(mod, {"Some-Other-Repo": {"held": True, "holders": ["x"]}})
    assert entry["reason"] == "stranded_open_pr"


def test_malformed_hold_value_does_not_reclassify():
    """guard-3616: a non-dict / truthy-but-shapeless value is NO information,
    never a definite verdict. Truthiness alone must not fire the carve-out."""
    mod = _import()
    for bad in (True, "HELD", 3, ["g-1"], {"holders": ["g-1"]},
                {"held": "yes"}, {"held": None}):
        assert _classify(mod, {"Vinheim-Web-App": bad})["reason"] == \
            "stranded_open_pr", f"reclassified on {bad!r}"


# ------------------------------------------------------------------ _pr_repo

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/zkysar1/Vinheim-Web-App/pull/53", "Vinheim-Web-App"),
    ("https://github.com/o/Ayoai-Environment-Server/pull/268",
     "Ayoai-Environment-Server"),
    ("", None),
    ("https://github.com/zkysar1/Vinheim-Web-App", None),
    ("not a url", None),
])
def test_pr_repo_extraction(url, expected):
    mod = _import()
    assert mod._pr_repo({"url": url}) is expected or \
        mod._pr_repo({"url": url}) == expected


def test_pr_repo_handles_missing_record():
    mod = _import()
    assert mod._pr_repo(None) is None
    assert mod._pr_repo({}) is None


# ------------------------------------------- build_deploy_hold_status contract

def test_builder_returns_empty_without_roots():
    """Domain-free default: no configured root -> empty map -> no behavior
    change anywhere. This is what keeps core/ free of a hardcoded repo path."""
    mod = _import()
    assert mod.build_deploy_hold_status({_OFF_DEFAULT: _pr()}, []) == {}
    assert mod.build_deploy_hold_status({_OFF_DEFAULT: _pr()}, None) == {}


def test_builder_omits_on_plumbing_error_never_records_a_clear(monkeypatch,
                                                               tmp_path):
    """rc=1 is usage/plumbing. Recording held=False there would manufacture a
    definite CLEAR from a broken run — and a wrong CLEAR is what FILES a goal.
    The probe's 3-not-1 split exists precisely to keep these distinguishable."""
    mod = _import()
    repo = tmp_path / "Vinheim-Web-App"
    (repo / ".git").mkdir(parents=True)
    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)
    (world / "scripts" / "deploy-hold-check.sh").write_text("#!/bin/sh\n")

    class R:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    out = mod.build_deploy_hold_status(
        {_OFF_DEFAULT: _pr()}, [str(tmp_path)], world_path=str(world))
    assert out == {}, "a plumbing error must be OMITTED, not recorded as CLEAR"


def test_builder_records_held_and_clear_from_the_rc_contract(monkeypatch,
                                                             tmp_path):
    """rc=3 -> held, holders read from the PAYLOAD (they rotate); rc=0 -> clear."""
    mod = _import()
    repo = tmp_path / "Vinheim-Web-App"
    (repo / ".git").mkdir(parents=True)
    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)
    (world / "scripts" / "deploy-hold-check.sh").write_text("#!/bin/sh\n")

    for rc, expected_held, holders in ((3, True, ["g-326-660"]), (0, False, [])):
        class R:
            returncode = rc
            stdout = json.dumps({"verdict": "HELD" if rc == 3 else "CLEAR",
                                 "holds": [{"goal_id": h} for h in holders]})
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
        out = mod.build_deploy_hold_status(
            {_OFF_DEFAULT: _pr()}, [str(tmp_path)], world_path=str(world))
        assert out == {"Vinheim-Web-App": {"held": expected_held,
                                           "holders": holders}}, f"rc={rc}"


def test_builder_only_probes_repos_with_open_prs(monkeypatch, tmp_path):
    """A CLOSED/MERGED PR cannot be parked behind a hold — do not spend a
    remote round trip on it."""
    mod = _import()
    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)
    (world / "scripts" / "deploy-hold-check.sh").write_text("#!/bin/sh\n")
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: calls.append(a) or None)
    closed = dict(_pr(), state="MERGED")
    assert mod.build_deploy_hold_status(
        {_OFF_DEFAULT: closed}, [str(tmp_path)], world_path=str(world)) == {}
    assert calls == []
