"""Tests for core/scripts/precheck-always-run-battery.py ().

THE CENTREPIECE IS THE BLIND-LANE BRANCH, not the happy path. A findings-only
battery earns its keep by staying quiet, and the failure mode of "quiet" is that
a lane which could not be READ renders identically to a lane with nothing to
report. That is guard-4093: "a zero with ANY blind lane is UNREACHABLE, not
EMPTY." Four tests here exist solely to pin that a partially-blind run can never
print, or JSON-report, an all-clear:

    test_any_blind_lane_makes_completeness_partial
    test_one_reachable_clean_lane_cannot_outvote_a_blind_one   <- the ANY-vs-ALL bug
    test_blind_run_with_no_findings_does_not_say_clean
    test_unparseable_output_is_blind_not_clean

test_one_reachable_clean_lane_cannot_outvote_a_blind_one is the discrimination
test (guard-1943): it fails against the NATURAL aggregator ("if ALL lanes blind
-> unreachable"), which looks correct and is the exact shape guard-4093 was
written about. Without it the other three would pass against the buggy form.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_precheck_always_run_battery.py -q
"""

import importlib.util
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(os.path.dirname(_HERE), "precheck-always-run-battery.py")
_spec = importlib.util.spec_from_file_location("precheck_always_run_battery", _MOD_PATH)
bat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bat)


# --------------------------------------------------------------------------
# fake lane runner
# --------------------------------------------------------------------------

def make_runner(payloads, blind=(), calls=None):
    """(argv, timeout) -> (rc, stdout, err), driven by a {script: payload} map.

    `blind` names scripts that cannot be run at all (timeout / spawn failure).
    Meter calls are answered generically so a test never has to model them.
    """
    def _runner(argv, timeout):
        if calls is not None:
            calls.append(list(argv))
        script = argv[0]
        if script.endswith("budget-meter.sh"):
            return 0, "run\n", None
        if script in blind:
            return None, "", f"{script}: simulated spawn failure"
        body = payloads.get(script, {})
        if isinstance(body, str):        # raw (possibly non-JSON) stdout
            return 0, body, None
        return 0, json.dumps(body), None
    return _runner


def _clean_payloads():
    """Every lane reporting genuinely nothing."""
    return {
        "inbox-alert-age-check.sh": {"candidate_count": 0, "failed": []},
        "user-blocker-escalation-check.sh": {"all_clear": True},
        "dependency-timeout-check.sh": {
            "candidates": [], "escalated": [], "needs_user_notification": [], "failed": []
        },
        "handoff-aging-check.sh": {"candidate_count": 0, "failed": []},
        "completed-not-closed-slate.sh": {"slate": []},
    }


def _report(capsys, **kw):
    """Run in --json mode and return the parsed report."""
    kw.setdefault("as_json", True)
    rc = bat.run(**kw)
    assert rc == 0, "the battery must ALWAYS exit 0 — it may never block the loop"
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# guard-4093 — the blind-lane branch
# --------------------------------------------------------------------------

def test_any_blind_lane_makes_completeness_partial(capsys):
    r = _report(capsys, lane_runner=make_runner(_clean_payloads(),
                                                blind={"handoff-aging-check.sh"}))
    assert r["completeness"] == "partial"
    assert [b["name"] for b in r["blind"]] == ["handoff-aging-check"]


def test_one_reachable_clean_lane_cannot_outvote_a_blind_one(capsys):
    """DISCRIMINATION (guard-1943): fails against the NATURAL aggregator.

    Four of five lanes are blind and the ONE reachable lane reports nothing. The
    wrong-but-natural rule ("elif ALL lanes unreachable -> unreachable") sees a
    reachable lane and downgrades the whole run to a clean zero. guard-4093's
    rule is ANY, not ALL. If this test ever passes while the other three fail,
    the aggregator has been rewritten into exactly the bug.
    """
    blind = {
        "inbox-alert-age-check.sh", "user-blocker-escalation-check.sh",
        "dependency-timeout-check.sh", "handoff-aging-check.sh",
    }
    r = _report(capsys, lane_runner=make_runner(_clean_payloads(), blind=blind))
    assert len(r["blind"]) == 4
    assert r["findings"] == []
    assert r["completeness"] == "partial", (
        "one reachable empty lane must NOT outvote four blind ones — this is the "
        "ALL-vs-ANY collapse guard-4093 documents"
    )


def test_blind_run_with_no_findings_does_not_say_clean(capsys):
    """The human line is where the collapse would actually mislead a reader."""
    bat.run(as_json=False, lane_runner=make_runner(_clean_payloads(),
                                                   blind={"dependency-timeout-check.sh"}))
    out = capsys.readouterr().out
    assert "NO FINDINGS REACHED" in out
    assert "UNREACHABLE, not clean" in out
    assert "lanes clean" not in out, "a partially-blind run must never read as all-clear"


def test_unparseable_output_is_blind_not_clean(capsys):
    """A lane whose shape changed has not been READ, whatever its exit code."""
    p = _clean_payloads()
    p["inbox-alert-age-check.sh"] = "not json at all\n"
    r = _report(capsys, lane_runner=make_runner(p))
    assert [b["name"] for b in r["blind"]] == ["inbox-alert-age-check"]
    assert r["completeness"] == "partial"
    assert "unparseable" in r["blind"][0]["reason"]


def test_json_array_body_is_blind_not_clean(capsys):
    """Valid JSON of the WRONG TYPE is still unread. `json.loads` succeeds on a
    list, and `.get` would then explode or silently return nothing."""
    p = _clean_payloads()
    p["handoff-aging-check.sh"] = "[]"
    r = _report(capsys, lane_runner=make_runner(p))
    assert [b["name"] for b in r["blind"]] == ["handoff-aging-check"]


def test_fully_clean_run_says_clean(capsys):
    """The positive control for all of the above: with nothing blind, a clean run
    IS allowed to say so. Without this, the blind-lane tests could be satisfied by
    a battery that never says 'clean' at all."""
    r = _report(capsys, lane_runner=make_runner(_clean_payloads()))
    assert r["completeness"] == "complete"
    assert r["status"] == "clean"
    assert r["findings"] == []
    bat.run(as_json=False, lane_runner=make_runner(_clean_payloads()))
    assert "all 5 lanes clean" in capsys.readouterr().out


# --------------------------------------------------------------------------
# findings extraction
# --------------------------------------------------------------------------

def test_each_finds_spec_type_fires(capsys):
    """counts / lists / false — one lane per spec kind, so a broken branch cannot
    hide behind another."""
    p = _clean_payloads()
    p["inbox-alert-age-check.sh"] = {"candidate_count": 4, "failed": []}      # counts
    p["dependency-timeout-check.sh"] = {                                       # lists
        "candidates": [1, 2], "escalated": [], "needs_user_notification": [], "failed": []
    }
    p["user-blocker-escalation-check.sh"] = {"all_clear": False}               # false
    r = _report(capsys, lane_runner=make_runner(p))
    got = {f["name"]: f["detail"] for f in r["findings"]}
    assert got["inbox-alert-age-check"] == ["candidate_count=4"]
    assert got["dependency-timeout-check"] == ["candidates=2"]
    assert got["user-blocker-escalation-check"] == ["all_clear=False"]
    assert r["status"] == "findings"
    assert r["completeness"] == "complete", "findings and blindness are ORTHOGONAL"


def test_all_clear_true_is_not_a_finding(capsys):
    """The `false` spec must fire on False ONLY — a True flag is the healthy case.
    Testing truthiness instead would invert this lane permanently."""
    p = _clean_payloads()
    p["user-blocker-escalation-check.sh"] = {"all_clear": True}
    r = _report(capsys, lane_runner=make_runner(p))
    assert r["findings"] == []


def test_missing_key_is_not_a_finding(capsys):
    """An absent key is not a zero and not a finding — it must not fire, and it
    must not crash."""
    p = _clean_payloads()
    p["inbox-alert-age-check.sh"] = {}
    r = _report(capsys, lane_runner=make_runner(p))
    assert r["findings"] == []


def test_failed_list_is_universally_a_finding(capsys):
    """Every lane carries `failed`; a lane that half-worked is never clean, even
    when its own finding keys are all zero."""
    p = _clean_payloads()
    p["handoff-aging-check.sh"] = {"candidate_count": 0, "failed": ["boom"]}
    r = _report(capsys, lane_runner=make_runner(p))
    assert [f["detail"] for f in r["findings"]] == [["failed=1"]]


# --------------------------------------------------------------------------
# registry invariants
# --------------------------------------------------------------------------

def test_meter_name_is_not_derived_from_script_name():
    """The  trap, pinned. 0.5g.7's meter sweep is
    `completed-not-closed-drain` while its script is `completed-not-closed-slate.sh`.
    A registry that derived one from the other would miss the meter's sweep_tier()
    case arm, WARN-default to `medium`, and make an always-run lane DROPPABLE in a
    tight zone — silently."""
    lane = [l for l in bat.LANES if l["script"] == "completed-not-closed-slate.sh"][0]
    assert lane["meter_name"] == "completed-not-closed-drain"
    assert lane["meter_name"] != lane["script"].replace(".sh", "")


def test_every_lane_is_metered(capsys):
    """The meter call is telemetry, not a gate (always-run never drops) — which is
    exactly why it is easy to drop by accident and worth pinning."""
    calls = []
    bat.run(as_json=True, lane_runner=make_runner(_clean_payloads(), calls=calls))
    metered = {c[2] for c in calls if c[0].endswith("budget-meter.sh") and c[1] == "check"}
    assert metered == {l["meter_name"] for l in bat.LANES}


def test_report_only_lane_never_receives_apply(capsys):
    """completed-not-closed-slate has NO --apply and must never be handed one:
    argparse would refuse it and the lane would go blind on every applied run."""
    calls = []
    bat.run(as_json=True, apply=True,
            lane_runner=make_runner(_clean_payloads(), calls=calls))
    slate = [c for c in calls if c[0] == "completed-not-closed-slate.sh"][0]
    assert "--apply" not in slate
    assert "--json" in slate


def test_apply_reaches_the_four_notification_lanes(capsys):
    """The complement of the test above — if --apply stopped propagating, the
    loop's escalation lanes would become silent no-ops with no error anywhere."""
    calls = []
    bat.run(as_json=True, apply=True,
            lane_runner=make_runner(_clean_payloads(), calls=calls))
    applied = {c[0] for c in calls if "--apply" in c}
    assert applied == {l["script"] for l in bat.LANES if l["apply_flag"]}
    assert len(applied) == 4


def test_mode_is_always_reported(capsys):
    """Dry-run is the default, so the mode must be visible on EVERY report — a
    reader must never mistake a dry-run for a real escalation pass."""
    assert _report(capsys, lane_runner=make_runner(_clean_payloads()))["mode"] == "dry_run"
    assert _report(capsys, apply=True,
                   lane_runner=make_runner(_clean_payloads()))["mode"] == "apply"


def test_uncovered_lanes_are_named_in_the_report(capsys):
    """"5 lanes" must never be read as "the whole always-run tier". The four
    sentinel-dispatched rows have no standalone script and are deliberately not
    run here; naming them is what keeps the split visible instead of inferred
    (guard-1760: a runner that reports only what it ran reads as total coverage)."""
    r = _report(capsys, lane_runner=make_runner(_clean_payloads()))
    names = {u["name"] for u in r["uncovered"]}
    assert names == {"tree-debt-gate", "experience-archival-gate",
                     "evolution-finalize-gate", "fresh-eyes-code-gate"}
    assert all(u["dispatched_by"] == "precheck-sentinel-battery.sh" for u in r["uncovered"])


def test_registry_lane_names_are_unique():
    assert len({l["name"] for l in bat.LANES}) == len(bat.LANES)


@pytest.mark.parametrize("field", ["name", "phase", "meter_name", "script", "finds"])
def test_every_lane_declares_every_required_field(field):
    for lane in bat.LANES:
        assert field in lane, f"{lane.get('name')} is missing {field!r}"


# --------------------------------------------------------------------------
# fail-open
# --------------------------------------------------------------------------

def test_runner_exception_does_not_propagate(capsys):
    """A lane runner that RAISES (rather than returning an err) must not take the
    loop entry down with it."""
    def exploding(argv, timeout):
        if argv[0].endswith("budget-meter.sh"):
            return 0, "run", None
        raise RuntimeError("detonate")
    with pytest.raises(RuntimeError):
        bat.run(as_json=True, lane_runner=exploding)   # run() itself propagates...
    # ...and main()'s wrapper is what converts it to a fail-open exit 0.
    argv_backup = sys.argv[:]
    sys.argv = ["precheck-always-run-battery.py", "--json"]
    try:
        real_run = bat.run
        bat.run = lambda **kw: (_ for _ in ()).throw(RuntimeError("detonate"))
        assert bat.main() == 0
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(out)["error"].startswith("battery_failed:")
    finally:
        bat.run = real_run
        sys.argv = argv_backup
