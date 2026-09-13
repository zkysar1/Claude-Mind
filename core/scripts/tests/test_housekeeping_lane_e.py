"""Lane E — mind-seed publish-key SILENCE detector ().

Lane E exists because the Claude-Mind publish workflow alerts ONLY on a FAILED
run, so it cannot fire when NO RUN HAPPENS — the failure that actually occurred
(lane deleted 2026-08-23, 100 commits with zero publishes, every live customer
environment frozen on one artifact for ~12 days, found by a human reading a code
comment). These tests pin the two properties that make it a detector rather than
decoration:

  1. The SCRIPT'S EXIT CODE is the contract, and rc=1 (STALE) must never
     collapse into rc=3 (unreachable). An unreadable probe is ZERO signals, not
     one (verify-before-assuming rule 4); collapsing them would report a
     customer-facing freeze on every credentials or network blip.
  2. An UNMEASURED run must not STAMP. Stamping on unreachable/unparseable would
     make the lane wait out its whole interval after a blip instead of retrying
     on the next tick — a detector that goes quiet exactly when it is degraded.

Plus one regression pin that no injected-`check_cmd` test can reach: the default
command must resolve `world/` through `_paths.WORLD_DIR`, never `os.environ`.
The first cut read `os.environ["WORLD_PATH"]`, which _paths.sh sets for SHELL
callers and which is ABSENT in a Python child — so the lane returned
`no-world-path` on a perfectly healthy box while every unit test passed, because
they all inject `check_cmd` and skip that branch. Measured before the fix.
"""

import importlib.util
import json
import pathlib
import sys
import tempfile

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from _runtime_bash import BASH  # guard-580: resolved binary, never a bare "bash"


def _load():
    spec = importlib.util.spec_from_file_location(
        "housekeeping_tick_lane_e", SCRIPT_DIR / "housekeeping-tick.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hk = _load()

CFG = {"mind_seed_freshness_interval_hours": 24}
PAYLOAD = {
    "verdict": "fresh", "age_hours": 33.3, "threshold_hours": 72,
    "last_modified": "2026-09-03T19:25:43+00:00",
    "bucket": "b", "key": "k",
}


def _fake(rc, out):
    """A stand-in for the detector with a chosen exit code and stdout."""
    return [BASH, "-c", f"printf %s {json.dumps(json.dumps(out))}; exit {rc}"]


def _statepath():
    return pathlib.Path(tempfile.mkdtemp()) / "state.json"


def test_rc0_is_ok_and_stamps():
    sp = _statepath()
    r = hk.run_lane_e(CFG, state_path=sp, check_cmd=_fake(0, PAYLOAD))
    assert r["verdict"] == "ok"
    assert r["age_hours"] == 33.3
    assert sp.exists(), "a measured answer must stamp"


def test_interval_gate_holds_after_a_stamp():
    sp = _statepath()
    hk.run_lane_e(CFG, state_path=sp, check_cmd=_fake(0, PAYLOAD))
    again = hk.run_lane_e(CFG, state_path=sp, check_cmd=_fake(0, PAYLOAD))
    assert again["verdict"] == "not-due"


def test_rc1_is_stale_and_stamps():
    sp = _statepath()
    r = hk.run_lane_e(CFG, state_path=sp,
                      check_cmd=_fake(1, dict(PAYLOAD, age_hours=99.0)))
    assert r["verdict"] == "stale"
    assert r["age_hours"] == 99.0
    assert sp.exists(), "STALE is a measured answer and must stamp"


def test_rc3_is_unreachable_and_does_not_stamp():
    sp = _statepath()
    r = hk.run_lane_e(CFG, state_path=sp, check_cmd=_fake(3, {}))
    assert r["verdict"] == "unreachable"
    assert not sp.exists(), (
        "an unreachable probe is unmeasured; stamping would silence the lane "
        "for a full interval after a transient blip")


def test_stale_and_unreachable_do_not_collapse():
    """POSITIVE CONTROL for the two tests above: they must differ.

    Each of them alone would still pass if both branches returned the SAME
    verdict string, so this asserts the distinction the lane exists to make.
    """
    sp1, sp2 = _statepath(), _statepath()
    stale = hk.run_lane_e(CFG, state_path=sp1,
                          check_cmd=_fake(1, dict(PAYLOAD, age_hours=99.0)))
    unreach = hk.run_lane_e(CFG, state_path=sp2, check_cmd=_fake(3, {}))
    assert stale["verdict"] != unreach["verdict"]
    assert (stale["verdict"], unreach["verdict"]) == ("stale", "unreachable")


def test_unparseable_stdout_does_not_stamp():
    sp = _statepath()
    r = hk.run_lane_e(CFG, state_path=sp,
                      check_cmd=[BASH, "-c", "echo not-json; exit 0"])
    assert r["verdict"] == "unparseable"
    assert not sp.exists()


def test_unknown_rc_is_named_not_silently_ok():
    sp = _statepath()
    r = hk.run_lane_e(CFG, state_path=sp, check_cmd=_fake(2, PAYLOAD))
    assert r["verdict"] == "unknown-rc"
    assert r["rc"] == 2


def test_zero_interval_disables_the_lane():
    r = hk.run_lane_e({"mind_seed_freshness_interval_hours": 0})
    assert r["verdict"] == "disabled"


def test_default_command_resolves_world_through_paths_not_environ(monkeypatch):
    """The regression pin: no injected check_cmd, so the real branch runs.

    Clearing WORLD_PATH/MIND_WORLD from the environment must NOT produce
    `no-world-path` — the lane resolves through `_paths.WORLD_DIR`, which is
    computed at import and does not depend on the shell exporting anything.
    """
    if not hk.WORLD_DIR:
        pytest.skip("no world configured on this box — nothing to pin")
    monkeypatch.delenv("WORLD_PATH", raising=False)
    monkeypatch.delenv("MIND_WORLD", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)  # reach the branch
    sp = _statepath()
    r = hk.run_lane_e(CFG, state_path=sp)
    assert r["verdict"] != "no-world-path", (
        "lane E must resolve world/ via _paths.WORLD_DIR; reading os.environ "
        "returns no-world-path in a Python child on a healthy box")


def test_lane_e_config_default_is_present_and_inside_the_detector_threshold():
    """24h must stay well inside the detector's own 72h staleness threshold.

    A cadence at or above the threshold catches a freeze only at its edge, which
    is the failure this lane was built to remove.
    """
    assert hk.DEFAULTS["mind_seed_freshness_interval_hours"] == 24
    assert hk.DEFAULTS["mind_seed_freshness_interval_hours"] < 72


# ── content attribution carried through the lane ( F5) ─────────────
# The detector gained a CONTENT assertion beside its age assertion, because an
# age-only check cannot tell a real publish from a truncated one or from a live
# key overwritten with nothing behind it — all three render "fresh". `out` in
# `run_lane_e` is a fixed allow-list, so a key the script emits and
# that tuple does not name is dropped in SILENCE: these pin the wiring, not the
# predicate (the predicate's own both-directions control is the domain test
# world/scripts/tests/test-mind-seed-content-attribution.sh).

CONTENT_PAYLOAD = dict(
    PAYLOAD,
    content_verdict="attributed",
    content_sha="7af1d1017ec3f346002eca171e00eb245d79c7cf",
    content_entries="3302",
    content_note="",
)


def test_content_fields_reach_the_lane_record():
    r = hk.run_lane_e(CFG, state_path=_statepath(),
                                   check_cmd=_fake(0, CONTENT_PAYLOAD))
    assert r["verdict"] == "ok"
    assert r["content_verdict"] == "attributed"
    assert r["content_sha"] == "7af1d1017ec3f346002eca171e00eb245d79c7cf"
    assert r["content_entries"] == "3302"


def test_content_mismatch_rides_rc1_and_still_stamps():
    """A content fault uses the EXISTING rc=1 rather than a fourth code.

    Lane E maps any unrecognised rc to `unknown-rc` and stamps only on rc 0/1, so
    a new exit code would retry every tick forever — the guard-6347 shape this
    detector has already suffered once ("a silence detector that was itself
    silent"). rc=1 keeps it a measured, stamped answer; the `content_verdict`
    field is what distinguishes a content fault from an age fault.
    """
    sp = _statepath()
    r = hk.run_lane_e(
        CFG, state_path=sp,
        check_cmd=_fake(1, dict(CONTENT_PAYLOAD, content_verdict="mismatch",
                                content_note="live ContentLength=1 but manifest bytes=2")))
    assert r["verdict"] == "stale"
    assert r["content_verdict"] == "mismatch"
    assert "manifest bytes" in (r["content_note"] or "")
    assert sp.exists(), "a content mismatch is a MEASURED answer and must stamp"


def test_unverified_content_on_a_fresh_run_still_stamps():
    """`unverified` means nothing was COMPARED — it must not degrade the age answer.

    The age question was measured and answered; only the secondary content probe
    declined to guess. Withholding the stamp here would make a permissions change
    on the manifest object silently convert the lane into a per-tick retry loop,
    which is the disease, not the cure (guard-1562).
    """
    sp = _statepath()
    r = hk.run_lane_e(
        CFG, state_path=sp,
        check_cmd=_fake(0, dict(CONTENT_PAYLOAD, content_verdict="unverified",
                                content_sha="", content_entries="",
                                content_note="changelog unparseable")))
    assert r["verdict"] == "ok"
    assert r["content_verdict"] == "unverified"
    assert sp.exists(), "an unverified CONTENT probe must not withhold the AGE stamp"


def test_a_detector_with_no_content_keys_is_still_handled():
    """Backward compatibility across deployments, not a hypothetical.

    This file is FRAMEWORK and promotes downstream (Ayoai-Mind -> Claude-Mind ->
    ZDS-Mind); the detector it shells out to is a DOMAIN script living under an
    external, gitignored world path, so it does NOT travel with this change. A
    downstream Mind will therefore run the new consumer against the OLD detector,
    which emits no content_* keys at all. That must read as "not reported",
    never as a crash and never as a content failure.
    """
    r = hk.run_lane_e(CFG, state_path=_statepath(),
                                   check_cmd=_fake(0, PAYLOAD))
    assert r["verdict"] == "ok"
    assert r["content_verdict"] is None
    assert r["content_sha"] is None
