"""Tests for box_capability_probe ().

The invariant worth pinning is not that the probe finds things — it is that it
keeps ABSENT and UNKNOWN apart. A probe that reports a broken probe as "not
here" is worse than no probe: the three failures that motivated this all had
true-looking reasons, and this one would manufacture another.
"""
import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "box_capability_probe",
    Path(__file__).resolve().parent.parent / "box_capability_probe.py",
)
bcp = importlib.util.module_from_spec(_SPEC)
sys.modules["box_capability_probe"] = bcp
_SPEC.loader.exec_module(bcp)


class TestPathProbe:
    def test_present_for_a_directory_that_exists(self):
        st, _ = bcp.probe_path(str(Path(__file__).resolve().parent))
        assert st == bcp.PRESENT

    def test_absent_for_a_path_that_does_not(self):
        st, why = bcp.probe_path("/nonexistent/definitely/not/here")
        assert st == bcp.ABSENT
        assert "this box" in why


class TestPeerWorldProbe:
    def test_unconfigured_is_absent_not_unknown(self):
        # Nothing configured is a genuine "not here" — the probe ran fine.
        st, why, path = bcp.probe_peer_world("nonexistent-peer-env")
        assert st == bcp.ABSENT
        assert path is None
        assert "PEER_WORLD_NONEXISTENT_PEER_ENV" in why

    def test_env_var_wins_and_must_exist_on_disk(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PEER_WORLD_FAKE_PEER", str(tmp_path))
        st, why, path = bcp.probe_peer_world("fake-peer")
        assert st == bcp.PRESENT
        assert path == str(tmp_path)
        assert "$PEER_WORLD_FAKE_PEER" in why

    def test_configured_but_missing_is_distinguished_from_unconfigured(self, monkeypatch):
        # More alarming than unconfigured: something CLAIMS this path and it is
        # not there. The detail string must say so, or the two collapse.
        monkeypatch.setenv("PEER_WORLD_FAKE_PEER", "/nope/not/a/real/world")
        st, why, path = bcp.probe_peer_world("fake-peer")
        assert st == bcp.ABSENT
        assert path is None
        assert "configured" in why and "no such directory" in why


class TestSecretProbe:
    def test_missing_accessor_is_unknown_not_absent(self, monkeypatch, tmp_path):
        # THE load-bearing test. If env-read.sh cannot run, the secret's presence
        # is unestablished. Reporting ABSENT here would let a tooling fault read
        # as a capability gap and send work to another box for no reason.
        monkeypatch.setattr(bcp, "PROJECT_ROOT", tmp_path)
        st, why = bcp.probe_secret("ANY_NAME")
        assert st == bcp.UNKNOWN
        assert "cannot probe" in why


class TestExitCodes:
    def test_absent_exits_1_and_unknown_exits_2(self, capsys):
        rc = bcp.main(["x", "--path", "/nonexistent/xyz"])
        assert rc == 1
        assert "cannot_execute_here" in capsys.readouterr().out

    def test_all_present_exits_0(self, capsys):
        rc = bcp.main(["x", "--path", str(Path(__file__).resolve().parent)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "can_execute" in out
        # Guidance must name DECLINE-not-defer somewhere in the contract; on the
        # clean path it should NOT, or a caller learns the wrong reflex.
        assert "DECLINE" not in out

    def test_decline_not_defer_is_stated_on_the_absent_path(self, capsys):
        bcp.main(["x", "--path", "/nonexistent/xyz"])
        out = capsys.readouterr().out
        assert "DECLINE" in out and "do NOT defer" in out
