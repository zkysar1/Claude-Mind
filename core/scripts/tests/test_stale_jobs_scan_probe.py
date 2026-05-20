"""Regression test for probe_responding_bridge_pids multi-port logic ().

Pre-fix: function probed only AYOBRIDGE_PORT (default 28080). When that probe
succeeded it protected ALL bridge PIDs; when it failed it protected NONE — even
though bridges on other ports might have active plugins. Caught by g-115-625:
4 long-lived bridges on 28080-28083 flagged as Tier-B orphans, 2 of them had
plugin_connected=true with sub-second last_plugin_poll.

Post-fix: each canonical bridge port (28080/28081/28082/28083) probed
independently. Each bridge protected only when ITS OWN port responds with
plugin_connected=true.
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2].parent
WORLD_SCRIPTS = REPO_ROOT / ".." / "Ayoai-World" / "scripts"
# Fallback: locate via local-paths.conf if the relative path doesn't exist.
if not WORLD_SCRIPTS.exists():
    agent_name = (REPO_ROOT / ".active-agent-test").read_text().strip() if (REPO_ROOT / ".active-agent-test").exists() else "zeta"
    # Phase 2.5.D layout: agent dirs live under agents/ parent.
    conf = REPO_ROOT / "agents" / agent_name / "local-paths.conf"
    if conf.exists():
        for line in conf.read_text().splitlines():
            if line.strip().startswith("WORLD_PATH="):
                WORLD_SCRIPTS = Path(line.split("=", 1)[1].strip().strip('"')) / "scripts"
                break

sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
sys.path.insert(0, str(WORLD_SCRIPTS))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "stale_jobs_scan", WORLD_SCRIPTS / "stale-jobs-scan.py"
)
sjs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sjs)


def _mock_urlopen_factory(responding_ports):
    """Return a fake urllib.request.urlopen that responds plugin_connected=true
    only for ports in `responding_ports`. Other ports raise URLError."""
    import urllib.error

    def fake_urlopen(url, timeout=None):
        for port in responding_ports:
            if f"localhost:{port}" in url or f"127.0.0.1:{port}" in url:
                resp = mock.MagicMock()
                resp.status = 200
                resp.read.return_value = json.dumps({
                    "plugin_connected": True, "last_plugin_poll": 1234567890
                }).encode("utf-8")
                resp.__enter__ = lambda self: self
                resp.__exit__ = lambda self, *a: None
                return resp
        raise urllib.error.URLError("connection refused")
    return fake_urlopen


def _make_bridge_proc(pid, port):
    return {
        "ProcessId": pid,
        "ParentProcessId": 1,
        "Name": "python.exe",
        "CommandLine": (
            f"C:\\Python\\python.exe C:/Users/Foo/roblox-bridge.py --port {port}"
        ),
        "CreationDate": None,
    }


class ProbeRespondingBridgePidsTest(unittest.TestCase):
    def test_all_four_ports_respond_all_protected(self):
        procs = [_make_bridge_proc(p + 1000, p) for p in (28080, 28081, 28082, 28083)]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory({28080, 28081, 28082, 28083}),
        ):
            result = sjs.probe_responding_bridge_pids(procs)
        self.assertEqual(result, {29080, 29081, 29082, 29083})

    def test_canonical_incident_only_28080_fails(self):
        """ /  incident: source port fails, others have
        active plugins. Pre-fix: ALL bridges lost protection. Post-fix: the
        three bridges with live plugins stay protected."""
        procs = [_make_bridge_proc(p + 1000, p) for p in (28080, 28081, 28082, 28083)]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory({28081, 28082, 28083}),  # source down
        ):
            result = sjs.probe_responding_bridge_pids(procs)
        self.assertEqual(result, {29081, 29082, 29083})
        self.assertNotIn(29080, result, "bridge on dead port 28080 should NOT be protected")

    def test_only_one_port_responds(self):
        procs = [_make_bridge_proc(p + 1000, p) for p in (28080, 28081, 28082, 28083)]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory({28082}),
        ):
            result = sjs.probe_responding_bridge_pids(procs)
        self.assertEqual(result, {29082})

    def test_no_ports_respond_empty_set(self):
        procs = [_make_bridge_proc(p + 1000, p) for p in (28080, 28081, 28082, 28083)]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(set()),
        ):
            result = sjs.probe_responding_bridge_pids(procs)
        self.assertEqual(result, set())

    def test_bridge_without_port_arg_defaults_to_28080(self):
        proc = {
            "ProcessId": 5555,
            "ParentProcessId": 1,
            "Name": "python.exe",
            "CommandLine": "C:\\Python\\python.exe C:/Users/Foo/roblox-bridge.py",
            "CreationDate": None,
        }
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory({28080}),
        ):
            result = sjs.probe_responding_bridge_pids([proc])
        self.assertEqual(result, {5555})

    def test_non_bridge_process_not_protected(self):
        proc = {
            "ProcessId": 9999,
            "ParentProcessId": 1,
            "Name": "python.exe",
            "CommandLine": "C:\\Python\\python.exe C:/Users/Foo/some-other-script.py --port 28080",
            "CreationDate": None,
        }
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory({28080, 28081, 28082, 28083}),
        ):
            result = sjs.probe_responding_bridge_pids([proc])
        self.assertEqual(result, set())

    def test_plugin_disconnected_not_protected(self):
        """Bridge responds but plugin_connected=false — bridge running w/o
        Studio plugin; treat as not-actively-serving, do not protect."""
        proc = _make_bridge_proc(7777, 28080)
        def fake_urlopen(url, timeout=None):
            resp = mock.MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps({
                "plugin_connected": False, "last_plugin_poll": 0
            }).encode("utf-8")
            resp.__enter__ = lambda self: self
            resp.__exit__ = lambda self, *a: None
            return resp
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = sjs.probe_responding_bridge_pids([proc])
        self.assertEqual(result, set())


if __name__ == "__main__":
    unittest.main()
