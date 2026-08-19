""" — the watchdog escalation signals must be BOX-scoped, not agent-scoped.

MirrorWedgeProbe and GitDriftProbe both dedup their auto-filed Investigate goal
on `open_goal_exists(origin_signal)`. Both built that signal from
`ctx.agent_name` alone — which is `os.environ["MIND_AGENT"]` and nothing more.

WHY THE OLD COMMENTS WERE NOT LIES, which is what makes this class hard to see:
commit a6093f817 ("box-scope the mirror-wedge Investigate signal") moved the key
from FLEET-global to AGENT-scoped on 2026-07-18, and on that date one agent ran
on one box, so the agent name WAS a faithful box proxy. The Mind/Body split
(first live worker Body ~2026-08-05) broke the equivalence and NOTHING FAILED —
the code kept doing exactly what it had always done; only the world changed
underneath it. Both probes' comments silently became false guarantees, and
GitDriftProbe had inherited its wording verbatim from MirrorWedgeProbe, so the
assumption lived in two homes.

Consequence being pinned: one alpha box's open wedge goal suppressed wedge
filing on EVERY box alpha occupies, including boxes actively wedged.

The last test is the load-bearing one. guard-3419 makes the file path and the
close path a SINGLE feature — the file path takes a dedup lease and the close
path is the only thing that releases it — so a key they disagree on produces a
goal that can be filed and never closed, i.e. a permanently disabled detector.
MirrorWedgeProbe wrote that key out as a LITERAL in both places, which is
exactly the shape that drifts silently. It now has one builder, and that test
fails if anyone inlines it back.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pointer_freshness as pf  # noqa: E402
import _session_telemetry as st  # noqa: E402


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "watchdog_box_signal_under_test", SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wd():
    return _load_watchdog()


class _Ctx:
    def __init__(self, tmp_path, agent_name="alpha"):
        self.agent_name = agent_name
        self.agent_dir = tmp_path / "agent"
        self.project_root_path = tmp_path


def _on_box(monkeypatch, box_id):
    """Simulate running on a named box.

    `_machine_id` caches into a module global on first resolve (deliberately —
    hostname can change on DHCP mid-process), so setting the env alone is not
    enough and a test that only did that would silently measure one box twice.
    """
    monkeypatch.setenv("MACHINE_ID", box_id)
    monkeypatch.setattr(st, "_MACHINE_ID", None)


PROBES = ("MirrorWedgeProbe", "GitDriftProbe")


# ---------------------------------------------------------------------------
# the defect itself: one agent, two boxes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("probe_name", PROBES)
def test_same_agent_on_two_boxes_gets_two_signals(wd, tmp_path, monkeypatch,
                                                  probe_name):
    """THE regression. Pre-fix both boxes returned the identical key, so box A's
    open goal deduped box B's filing away while box B was actively wedged."""
    probe = getattr(wd, probe_name)(_Ctx(tmp_path, "alpha"))

    _on_box(monkeypatch, "cc-04")
    first = probe._origin_signal()
    _on_box(monkeypatch, "cc-07")
    second = probe._origin_signal()

    assert first != second, (
        f"{probe_name} returns the same dedup key on two different boxes — "
        f"one box's open goal will suppress the other's ({first})")
    assert "cc-04" in first and "cc-07" in second


@pytest.mark.parametrize("probe_name", PROBES)
def test_same_agent_same_box_is_stable(wd, tmp_path, monkeypatch, probe_name):
    """The other half: dedup must still WORK. A key that varied per call would
    make every tick file a new goal — the opposite failure, and a worse one."""
    probe = getattr(wd, probe_name)(_Ctx(tmp_path, "alpha"))
    _on_box(monkeypatch, "cc-07")
    assert probe._origin_signal() == probe._origin_signal()


@pytest.mark.parametrize("probe_name", PROBES)
def test_two_agents_on_one_box_still_differ(wd, tmp_path, monkeypatch,
                                            probe_name):
    """Agent-scoping was never wrong, only insufficient. Adding the box must not
    cost the property the 2026-07-18 change bought."""
    _on_box(monkeypatch, "cc-07")
    a = getattr(wd, probe_name)(_Ctx(tmp_path, "alpha"))._origin_signal()
    z = getattr(wd, probe_name)(_Ctx(tmp_path, "zeta"))._origin_signal()
    assert a != z


@pytest.mark.parametrize("probe_name", PROBES)
def test_signal_keeps_its_sanctioned_gate_prefix(wd, tmp_path, monkeypatch,
                                                 probe_name):
    """origin_signal.py accepts a signal only when it STARTS WITH a sanctioned
    prefix; anything else is silently REWRITTEN from the title at filing time.
    A reformat that lost `investigate:` would not fail loudly — the goal would
    file fine under a key the probe can never dedup or close against."""
    _on_box(monkeypatch, "cc-07")
    assert getattr(wd, probe_name)(_Ctx(tmp_path))._origin_signal().startswith(
        "investigate:")


# ---------------------------------------------------------------------------
# _box_id: totality
# ---------------------------------------------------------------------------

def test_box_id_never_raises_when_the_ssot_is_unavailable(wd, monkeypatch):
    """Both probes compute the signal OUTSIDE their fail-open try blocks, so a
    raise here escapes the probe and kills the whole watchdog tick."""
    monkeypatch.setitem(sys.modules, "_session_telemetry", None)
    assert wd._box_id() == "unknown"


def test_box_id_prefers_machine_id_over_hostname(wd, monkeypatch):
    _on_box(monkeypatch, "cc-42")
    assert wd._box_id() == "cc-42"


# ---------------------------------------------------------------------------
# the lease invariant (guard-3419) — file and close MUST agree on the key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("probe_name,file_method,close_method", [
    ("MirrorWedgeProbe", "_file_wedge_goal", "_close_wedge_goal"),
    ("GitDriftProbe", "_file_drift_goal", "_close_drift_goal"),
])
def test_file_and_close_paths_use_the_identical_key(wd, tmp_path, monkeypatch,
                                                    probe_name, file_method,
                                                    close_method):
    """guard-3419: open-status dedup and close-on-clear are ONE feature. The
    file path takes the lease; the close path is the only thing that releases
    it. If they compute different keys the lease is never released and the
    detector is one-shot per box, forever — while local telemetry stays healthy,
    because detection never stopped.

    This is the test that would have caught the duplicated literal, and it is
    why the key is now built in one place per probe.
    """
    seen = {}
    monkeypatch.setattr(pf, "open_goal_exists",
                        lambda sig, w, a: seen.setdefault("file", sig) and True)
    monkeypatch.setattr(pf, "open_goal_records",
                        lambda sig, w, a: (seen.setdefault("close", sig), [])[1])
    monkeypatch.setitem(sys.modules, "_paths",
                        types.SimpleNamespace(WORLD_DIR=str(tmp_path / "world")))
    monkeypatch.setitem(sys.modules, "_runtime_bash",
                        types.SimpleNamespace(BASH="bash"))
    _on_box(monkeypatch, "cc-07")

    probe = getattr(wd, probe_name)(_Ctx(tmp_path, "alpha"))
    probe.consecutive_wedged = 5
    probe.consecutive_breach = 5

    getattr(probe, file_method)({"files": {}, "wedged_count": 1, "breaches": [],
                                 "branch": "main", "ahead": 0, "behind": 0,
                                 "disk_used_pct": 1, "carrier_refs": [],
                                 "consecutive_breach": 5,
                                 "fetched_this_tick": True})
    getattr(probe, close_method)()

    assert seen.get("file") and seen.get("close"), (
        f"one of the two paths never queried a dedup key: {seen}")
    assert seen["file"] == seen["close"], (
        f"{probe_name} files under {seen['file']!r} but closes against "
        f"{seen['close']!r} — the lease can never be released (guard-3419)")
