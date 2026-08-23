""": TOP-LEVEL working-memory keys must survive wm-reset.

`capture_evictions` is a top-level counter. wm-reset rebuilds from
`_default_wm_data()` and previously preserved only two things:

  1. SESSION_IDENTITY_FIELDS at the top level -- that set is {session_start};
  2. cadence + RESET_SURVIVING_SLOTS members found under data["slots"].

A top-level non-identity key matched neither, so every wm-reset dropped it.
wm-reset fires MID-SESSION at every autocompact (aspirations-consolidate
Step 5), so the counter reported a since-last-autocompact tally while its
consumer (array_limits cap sizing) read it as a lifetime one.

Two things this file pins that are easy to get wrong:

  * RESET_SURVIVING_SLOTS cannot express it. That constant is consulted only
    inside `for slot_name, slot_val in existing_slots.items()`, and a
    top-level key never enters that loop. It is the obvious fix and it is a
    no-op -- `test_reset_surviving_slots_cannot_reach_top_level` pins that.

  * SESSION_IDENTITY_FIELDS is the WRONG home even though it would survive
    reset, because `clear-identity` NULLS every member of that set. Putting
    the counter there would make clear-identity destroy it -- the opposite of
    the intent. `test_clear_identity_does_not_touch_top_level_survivors`
    pins that.

guard-2552: both constants are hand-mirrored in
mind_api/src/endpoints/wm_write.py and wm-reset is DAEMON-ONLY, so a
wm.py-only edit leaves the slot broken in production while the CLI suite is
green. `test_daemon_and_cli_constants_agree` pins the mirror.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def wm_mod(tmp_path, monkeypatch):
    """Import wm with its I/O redirected at a tmp file.

    BODY_WM_PATH is the sanctioned redirect for core/scripts tests.
    STORAGE_BACKEND=local is mandatory on an own-cloud box (guard-955):
    OwnCloudBackend derives its S3 key from the env id, not from the tmp
    path, so an unpinned write collides on the PRODUCTION key.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("BODY_WM_PATH", str(tmp_path / "wm.yaml"))
    import wm

    wm.cmd_init(type("A", (), {})())
    return wm


def _args():
    return type("A", (), {})()


def _seed(wm, **top_level):
    data = wm.read_wm()
    data.update(top_level)
    wm.write_wm(data)


def test_top_level_survivor_survives_reset(wm_mod):
    _seed(wm_mod, capture_evictions={"exp_capture": 7, "sensory_buffer": 45})

    wm_mod.cmd_reset(_args())

    assert wm_mod.read_wm().get("capture_evictions") == {
        "exp_capture": 7,
        "sensory_buffer": 45,
    }


def test_reset_still_preserves_identity_and_clears_slots(wm_mod):
    """The fix must not change what reset already did."""
    _seed(wm_mod, session_start="2026-08-22T00:00:00")
    data = wm_mod.read_wm()
    data["slots"]["sensory_buffer"] = [{"o": "x"}]
    wm_mod.write_wm(data)

    wm_mod.cmd_reset(_args())

    after = wm_mod.read_wm()
    assert after.get("session_start") == "2026-08-22T00:00:00"
    assert not after["slots"].get("sensory_buffer")


def test_clear_identity_does_not_touch_top_level_survivors(wm_mod):
    """The reason this is a SEPARATE constant from SESSION_IDENTITY_FIELDS.

    If capture_evictions were added to SESSION_IDENTITY_FIELDS it would
    survive reset -- and then clear-identity (graceful-stop D4.5) would null
    it. This asserts the counter outlives clear-identity while session_start
    is still correctly nulled.
    """
    _seed(
        wm_mod,
        capture_evictions={"exp_capture": 7},
        session_start="2026-08-22T00:00:00",
    )

    wm_mod.cmd_clear_identity(_args())

    after = wm_mod.read_wm()
    assert after.get("capture_evictions") == {"exp_capture": 7}
    assert after.get("session_start") is None


def test_reset_surviving_slots_cannot_reach_top_level(wm_mod, monkeypatch):
    """Negative control for the rejected obvious fix.

    Pins that adding the key to RESET_SURVIVING_SLOTS does nothing, so a
    future reader does not 'simplify' the top-level constant away into it.
    """
    monkeypatch.setattr(wm_mod, "RESET_SURVIVING_TOP_LEVEL", set())
    monkeypatch.setattr(
        wm_mod, "RESET_SURVIVING_SLOTS", set(wm_mod.RESET_SURVIVING_SLOTS) | {"capture_evictions"}
    )
    _seed(wm_mod, capture_evictions={"exp_capture": 7})

    wm_mod.cmd_reset(_args())

    assert wm_mod.read_wm().get("capture_evictions") is None


def test_reset_without_the_constant_wipes_it(wm_mod, monkeypatch):
    """Negative control: proves the suite above is not vacuous."""
    monkeypatch.setattr(wm_mod, "RESET_SURVIVING_TOP_LEVEL", set())
    _seed(wm_mod, capture_evictions={"exp_capture": 7})

    wm_mod.cmd_reset(_args())

    assert wm_mod.read_wm().get("capture_evictions") is None


def test_absent_key_is_not_materialised(wm_mod):
    """A survivor that was never set must not appear as a null after reset."""
    wm_mod.cmd_reset(_args())

    assert wm_mod.read_wm().get("capture_evictions") is None


def test_daemon_and_cli_constants_agree(wm_mod):
    """guard-2552: the daemon copy is the one that runs in production.

    Parsed textually rather than imported -- mind_api.src.endpoints.wm_write
    pulls in the server package, which is far more than this assertion needs.
    """
    import re

    daemon = (
        Path(__file__).resolve().parents[3]
        / "mind_api"
        / "src"
        / "endpoints"
        / "wm_write.py"
    ).read_text(encoding="utf-8")

    match = re.search(r"^RESET_SURVIVING_TOP_LEVEL\s*=\s*\{([^}]*)\}", daemon, re.M)
    assert match, "daemon copy of RESET_SURVIVING_TOP_LEVEL is missing (guard-2552)"

    daemon_members = {m.group(1) for m in re.finditer(r'"([^"]+)"', match.group(1))}
    assert daemon_members == wm_mod.RESET_SURVIVING_TOP_LEVEL, (
        f"daemon {daemon_members} != CLI {wm_mod.RESET_SURVIVING_TOP_LEVEL} "
        "-- wm-reset is daemon-only, so a drifted mirror is broken in production "
        "while this suite stays green"
    )
