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
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2].parent


def _find_world_scripts():
    """Locate the domain world/scripts dir that actually contains
    stale-jobs-scan.py, robustly across boxes (g-115-1807). The prior logic
    hardcoded agent "zeta" + a .active-agent-test file that exist on no current
    box, so this regression test silently SKIPPED everywhere and the g-115-1518
    ForeignRootOrphanScopingTest coverage (3 tests) went dormant. Resolution
    order: (1) the product-box sibling checkout, (2) the bound agent's world
    (MIND_AGENT, injected on this box), (3) any agent's local-paths.conf whose
    WORLD_PATH actually carries the script."""
    sibling = REPO_ROOT / ".." / "Ayoai-World" / "scripts"
    if (sibling / "stale-jobs-scan.py").exists():
        return sibling
    confs = []
    bound = os.environ.get("MIND_AGENT")
    if bound:
        confs.append(REPO_ROOT / "agents" / bound / "local-paths.conf")
    confs.extend(sorted((REPO_ROOT / "agents").glob("*/local-paths.conf")))
    for conf in confs:
        if not conf.exists():
            continue
        for line in conf.read_text().splitlines():
            if line.strip().startswith("WORLD_PATH="):
                ws = Path(line.split("=", 1)[1].strip().strip('"')) / "scripts"
                if (ws / "stale-jobs-scan.py").exists():
                    return ws
                break
    return sibling  # not found — the module-level skip below reports this path


WORLD_SCRIPTS = _find_world_scripts()

sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
sys.path.insert(0, str(WORLD_SCRIPTS))

import importlib.util

_TARGET = WORLD_SCRIPTS / "stale-jobs-scan.py"
if not _TARGET.exists():
    import pytest
    pytest.skip(
        f"stale-jobs-scan.py not found at {_TARGET} — domain-specific "
        "regression test, no equivalent in this domain.",
        allow_module_level=True,
    )

spec = importlib.util.spec_from_file_location("stale_jobs_scan", _TARGET)
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


class MultiEnvBridgePidsTest(unittest.TestCase):
    """: multi_env_bridge_pids protects roblox-bridge processes on the
    canonical multi-env ports (28080-28083) UNCONDITIONALLY -- regardless of
    probe-response / plugin_connected state. Closes the g-115-106 / g-115-625 gap
    where a bridge whose plugin was disconnected (probe_responding returns empty)
    fell through to a Tier-B --auto-kill candidate and the working source bridge
    was killed (twice)."""

    def test_all_multi_env_ports_protected_unconditionally(self):
        procs = [_make_bridge_proc(p + 1000, p) for p in (28080, 28081, 28082, 28083)]
        # No probe mocking -- protection is unconditional (port-based), not probe-based.
        result = sjs.multi_env_bridge_pids(procs)
        self.assertEqual(result, {29080, 29081, 29082, 29083})

    def test_hung_bridge_still_protected_when_probe_fails(self):
        """The canonical incident: a 28083 bridge with no live plugin (probe
        fails) is NOT protected by probe_responding_bridge_pids, but IS protected
        by multi_env_bridge_pids -- the unconditional layer that prevents the kill."""
        proc = _make_bridge_proc(2083, 28083)
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(set()),  # no port responds
        ):
            probe_result = sjs.probe_responding_bridge_pids([proc])
        self.assertEqual(probe_result, set(),
                         "probe-based protection should NOT cover a non-responding bridge")
        self.assertEqual(sjs.multi_env_bridge_pids([proc]), {2083},
                         "multi_env protection must cover it regardless of probe state")

    def test_non_protected_port_not_protected(self):
        proc = _make_bridge_proc(2999, 29999)
        self.assertEqual(sjs.multi_env_bridge_pids([proc]), set())

    def test_bridge_without_port_defaults_to_28080_protected(self):
        proc = {
            "ProcessId": 5556,
            "ParentProcessId": 1,
            "Name": "python.exe",
            "CommandLine": "C:\\Python\\python.exe C:/Users/Foo/roblox-bridge.py",
            "CreationDate": None,
        }
        self.assertEqual(sjs.multi_env_bridge_pids([proc]), {5556})

    def test_non_bridge_on_protected_port_not_protected(self):
        proc = {
            "ProcessId": 9998,
            "ParentProcessId": 1,
            "Name": "python.exe",
            "CommandLine": "C:\\Python\\python.exe C:/Users/Foo/some-other-script.py --port 28083",
            "CreationDate": None,
        }
        self.assertEqual(sjs.multi_env_bridge_pids([proc]), set())

    def test_port_28083_candidate_reported_but_not_killed(self):
        """Goal verification (): an aged 28083 bridge that WOULD be a
        Tier-B kill candidate is excluded from candidates because build_do_not_kill
        now contains it -- so --auto-kill never reaps it. Negative control: an
        identically-aged bridge on a non-protected port (29999) IS a candidate,
        proving the test would catch a regression that removed the protection."""
        old = datetime.now() - timedelta(hours=25)  # past the 24h roblox-bridge threshold
        protected = _make_bridge_proc(2083, 28083)
        protected["CreationDate"] = old
        unprotected = _make_bridge_proc(2999, 29999)
        unprotected["CreationDate"] = old
        procs = [protected, unprotected]
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(set()),  # deterministic: no probe protection
        ):
            dnk = sjs.build_do_not_kill(procs, [])
            candidates = sjs.identify_candidates(procs, None, [], dnk, sjs.DEFAULT_THRESHOLDS)
        self.assertIn(2083, dnk, "28083 bridge must be in do_not_kill (multi-env protect)")
        self.assertNotIn(2999, dnk, "29999 (non-multi-env) bridge must NOT be protected by port rule")
        cand_pids = {c["pid"] for c in candidates}
        self.assertNotIn(2083, cand_pids,
                         "protected 28083 bridge must NOT be a kill candidate (reported but not killed)")
        self.assertIn(2999, cand_pids,
                      "unprotected 29999 bridge SHOULD be a candidate (regression-catch control)")


def _make_hook_python_proc(pid, ppid, age_hours, cmdline=None, name="python.exe"):
    """A python.exe whose cmdline references core/scripts (inline `py -c`,
    PreToolUse hook, or .python-shim). Default cmdline is a forward-slash
    inline-py-c; pass cmdline to exercise the backslash / shim shapes."""
    return {
        "ProcessId": pid,
        "ParentProcessId": ppid,
        "Name": name,
        "CommandLine": cmdline or (
            "C:\\Python312\\python.exe -c \"import sys; "
            "sys.path.insert(0, 'core/scripts'); from _paths import PROJECT_ROOT\""
        ),
        "CreationDate": datetime.now() - timedelta(hours=age_hours),
    }


class HookPythonOrphanTest(unittest.TestCase):
    """g-?? (2026-05-28 chat-mode): hook-python Tier-B signature closes the
    windows-process-orphan-blind-spot. A python.exe referencing core/scripts
    whose parent (claude/bash) is dead is a stuck orphan; one with a live
    parent is an active hook (protected). Threshold 0.5h. The parent-liveness
    check reads the process-table snapshot (console-independent), NOT
    os.kill(pid,0) — see _parent_is_live and the lifecycle.is_pid_alive fix."""

    def test_classify_hook_python_forward_slash(self):
        proc = _make_hook_python_proc(7001, 6001, 1.0)
        self.assertEqual(sjs.classify_orphan(proc["CommandLine"], proc["Name"]),
                         "hook-python")

    def test_classify_hook_python_backslash_and_shim(self):
        shim = ("C:\\Python312\\python.exe "
                "C:\\repo\\core\\scripts\\.python-shim\\python3 retrieve.sh")
        self.assertEqual(sjs.classify_orphan(shim, "python.exe"), "hook-python")

    def test_bash_wrapper_mentioning_core_scripts_not_classified(self):
        """`bash core/scripts/foo.sh` has Name=bash.exe — the name filter must
        exclude it so we only ever reap true python.exe processes."""
        cmd = "C:\\Program Files\\Git\\bin\\bash.exe core/scripts/retrieve.sh --category x"
        self.assertIsNone(sjs.classify_orphan(cmd, "bash.exe"))

    def test_daemon_not_classified_as_hook_python(self):
        """The daemon is `python -m mind_api.src` — no core/scripts in cmdline,
        so it must NOT match (would be catastrophic to reap)."""
        cmd = "C:\\Python312\\python.exe -m mind_api.src --port 51763"
        self.assertIsNone(sjs.classify_orphan(cmd, "python.exe"))

    def test_parent_is_live_dead_recycled_alive(self):
        child = _make_hook_python_proc(7001, 6001, 1.0)
        # (a) parent absent from table -> dead
        self.assertFalse(sjs._parent_is_live(child, {7001: child}))
        # (b) parent present, born BEFORE child -> live
        live_parent = {"ProcessId": 6001, "ParentProcessId": 1, "Name": "bash.exe",
                       "CommandLine": "bash.exe",
                       "CreationDate": datetime.now() - timedelta(hours=2.0)}
        self.assertTrue(sjs._parent_is_live(child, {7001: child, 6001: live_parent}))
        # (c) parent present but born AFTER child -> recycled PID -> dead
        recycled = {"ProcessId": 6001, "ParentProcessId": 1, "Name": "notepad.exe",
                    "CommandLine": "notepad.exe",
                    "CreationDate": datetime.now() - timedelta(hours=0.25)}
        self.assertFalse(sjs._parent_is_live(child, {7001: child, 6001: recycled}))

    def test_dead_parent_aged_orphan_is_candidate(self):
        """Aged hook-python whose parent is gone -> reap candidate."""
        child = _make_hook_python_proc(7001, 6001, 1.0)  # parent 6001 absent
        cands = sjs.identify_candidates([child], None, [], set(), sjs.DEFAULT_THRESHOLDS)
        pids = {c["pid"] for c in cands}
        self.assertIn(7001, pids)
        self.assertEqual(next(c for c in cands if c["pid"] == 7001)["type"], "hook-python")

    def test_live_parent_protected(self):
        """Aged hook-python with a LIVE parent must NOT be a candidate. This is
        the regression-catch: deleting the parent-gate makes 7001 a candidate."""
        child = _make_hook_python_proc(7001, 6001, 1.0)
        parent = {"ProcessId": 6001, "ParentProcessId": 1, "Name": "bash.exe",
                  "CommandLine": "C:\\Program Files\\Git\\bin\\bash.exe",
                  "CreationDate": datetime.now() - timedelta(hours=2.0)}
        cands = sjs.identify_candidates([child, parent], None, [], set(),
                                        sjs.DEFAULT_THRESHOLDS)
        self.assertNotIn(7001, {c["pid"] for c in cands})

    def test_recycled_parent_aged_orphan_is_candidate(self):
        """Parent PID present but born after child (recycled) -> true parent
        dead -> candidate."""
        child = _make_hook_python_proc(7001, 6001, 1.0)
        recycled = {"ProcessId": 6001, "ParentProcessId": 1, "Name": "notepad.exe",
                    "CommandLine": "notepad.exe",
                    "CreationDate": datetime.now() - timedelta(hours=0.25)}
        cands = sjs.identify_candidates([child, recycled], None, [], set(),
                                        sjs.DEFAULT_THRESHOLDS)
        self.assertIn(7001, {c["pid"] for c in cands})

    def test_young_hook_python_below_threshold_not_candidate(self):
        """Dead parent but only 12 min old (< 0.5h threshold) -> not yet a
        candidate. Isolates the threshold from the parent gate and cooldown."""
        child = _make_hook_python_proc(7001, 6001, 0.2)  # 12 min, parent absent
        cands = sjs.identify_candidates([child], None, [], set(), sjs.DEFAULT_THRESHOLDS)
        self.assertNotIn(7001, {c["pid"] for c in cands})


class ForeignRootOrphanScopingTest(unittest.TestCase):
    """: a hook-python orphan rooted in a SIBLING Mind install
    (.../Zak-Data-Solutions-Mind/core/scripts/...) belongs to that install's
    scanner, not ours. It must be EXCLUDED from our candidate set so it cannot
    form a permanent floor that pins MAX_KILLS_PER_RUN and blocks auto-reap of
    OUR own orphans (canonical g-115-106: 8 Tier-B orphans, 4 rooted in the
    sibling install permanently held the count >3). Our own absolute-rooted
    orphans, and relative/ambiguous ones, stay candidates."""

    def _our_root(self):
        from _paths import PROJECT_ROOT
        return str(PROJECT_ROOT).replace("\\", "/")

    def _abs_orphan(self, pid, root, script="presence-tick.py", age=40.0):
        # ppid 999999 is absent from the procs list -> dead parent -> reap-eligible.
        return _make_hook_python_proc(
            pid, 999999, age,
            cmdline=f"C:\\Windows\\py.exe {root}/core/scripts/{script}",
        )

    def test_orphan_is_foreign_helper(self):
        ours = f"C:\\Windows\\py.exe {self._our_root()}/core/scripts/presence-tick.py"
        foreign = ("C:\\Windows\\py.exe "
                   "<REPO_ROOT>/"
                   "Zak-Data-Solutions-Mind/core/scripts/presence-tick.py")
        relative = ("C:\\Python312\\python.exe -c "
                    "\"import sys; sys.path.insert(0, 'core/scripts')\"")
        self.assertFalse(sjs.orphan_is_foreign(ours), "our own absolute root is not foreign")
        self.assertTrue(sjs.orphan_is_foreign(foreign), "sibling-Mind absolute root is foreign")
        self.assertFalse(sjs.orphan_is_foreign(relative),
                         "relative core/scripts (no absolute root) is not foreign")
        self.assertFalse(sjs.orphan_is_foreign(""), "empty cmdline is not foreign")

    def test_foreign_orphan_excluded_own_kept(self):
        """Canonical  shape: our orphan + a sibling-Mind orphan, both
        aged + dead-parent. Only ours is a candidate; the sibling is scoped out."""
        ours = self._abs_orphan(18456, self._our_root(), "presence-tick.py")
        foreign = self._abs_orphan(
            4428,
            "<REPO_ROOT>/Zak-Data-Solutions-Mind",
            "context-reads.py",
        )
        cands = sjs.identify_candidates([ours, foreign], None, [], set(),
                                        sjs.DEFAULT_THRESHOLDS)
        pids = {c["pid"] for c in cands}
        self.assertIn(18456, pids, "our own absolute-rooted orphan stays a candidate")
        self.assertNotIn(4428, pids,
                         "sibling-Mind orphan must be scoped out (g-115-1518)")

    def test_relative_core_scripts_still_candidate(self):
        """Regression-catch: the inline `py -c` relative-core/scripts shape (no
        absolute root) must remain a candidate -- the fix excludes only
        POSITIVELY-foreign absolute roots, never relative/ambiguous ones."""
        child = _make_hook_python_proc(7001, 6001, 1.0)  # default relative cmdline; parent absent
        cands = sjs.identify_candidates([child], None, [], set(), sjs.DEFAULT_THRESHOLDS)
        self.assertIn(7001, {c["pid"] for c in cands})

    def test_foreign_floor_does_not_pin_gate(self):
        """The whole point: N sibling orphans must NOT count toward the candidate
        total. With 4 foreign + 2 ours, the candidate set is {ours} (size 2),
        NOT 6 -- so a count-gated reaper sees only what it owns."""
        ours = [self._abs_orphan(100 + i, self._our_root(), f"s{i}.py") for i in range(2)]
        foreign = [self._abs_orphan(
            200 + i,
            "<REPO_ROOT>/Zak-Data-Solutions-Mind",
            f"f{i}.py") for i in range(4)]
        cands = sjs.identify_candidates(ours + foreign, None, [], set(),
                                        sjs.DEFAULT_THRESHOLDS)
        pids = {c["pid"] for c in cands}
        self.assertEqual(pids, {100, 101},
                         "only our 2 orphans count; the 4 sibling orphans are scoped out")


def _unallocatable_pid():
    """A PID the kernel can never assign, so /proc/<pid> is guaranteed absent.

    guard-1699: do NOT hardcode a plausible PID as a stand-in for the category
    "a process that does not exist". Measured on cc-03: pid_max is 4194304 and
    live PIDs already exceed 3.1M, so a hardcoded 999999 sits squarely inside
    the allocatable range -- absent by luck, not by construction. The day the
    counter wraps onto it, CreationDate stops being None and the test below
    inverts, reading as a regression in the scanner rather than as fixture rot.
    """
    try:
        with open("/proc/sys/kernel/pid_max") as fh:
            return int(fh.read().strip()) + 1
    except Exception:
        # No /proc at all (non-Linux): every /proc read the scanner attempts
        # fails anyway, which is exactly the state these cases are pinning.
        return 2 ** 31 - 1


class UnixProcessAgeCorruptionTest(unittest.TestCase):
    """. `ps -o etimes` is not trustworthy for young processes.

    Measured 2026-08-06 (hostname cc-03, Linux 6.8.0-136-generic): for a process
    younger than the box's btime/uptime skew (~16s there) etimes returns
    4123168576 -- an exact 32-bit unsigned wrap of a negative elapsed
    (4294967296 - 4123168576 = 171798720). `ps -o lstart` and /proc were both
    correct for the same PID at the same instant.

    The consequence INVERTS this scanner: the YOUNGEST processes report as the
    OLDEST (~130 years => 1145324h), past every threshold at once. The
    MIN_PROCESS_AGE_SECONDS newborn cooldown cannot catch it, because that guard
    is computed FROM the corrupted age -- the one check written to protect young
    processes is the one the defect disables. Demonstrated live before the fix: a
    1-SECOND-old healthy `python3 ... core/scripts ...` was reported as a
    hook-python B-orphan at elapsed=1145324.6h against a 0.5h threshold, i.e. a
    SIGTERM/SIGKILL candidate under `scan --auto-kill`, in its first second.

    These tests drive a SYNTHETIC ps table so they pin the defect on every box,
    including ones whose clocks show no skew and where the bug cannot reproduce.
    """

    _PS_HEADER = "    PID    PPID  ELAPSED COMMAND COMMAND"
    _WRAPPED = 4123168576
    _NO_SUCH_PID = _unallocatable_pid()

    def _run_with_ps(self, ps_stdout):
        fake = mock.MagicMock(returncode=0, stdout=ps_stdout, stderr="")
        with mock.patch.object(sjs, "IS_WINDOWS", False), \
                mock.patch.object(sjs.subprocess, "run", return_value=fake):
            return sjs.get_all_processes()

    def test_wrapped_etimes_never_yields_a_geologic_age(self):
        """The core regression. A live PID (this test process) carrying a wrapped
        etimes must not come back ~130 years old -- /proc is authoritative and is
        consulted first, so the wrapped value never reaches CreationDate."""
        pid = os.getpid()
        procs = self._run_with_ps(
            f"{self._PS_HEADER}\n{pid} 1 {self._WRAPPED} python3 python3 -c pass\n"
        )
        self.assertEqual(len(procs), 1)
        age_h = sjs.process_age_hours(procs[0])
        self.assertIsNotNone(age_h, "a live PID must remain ageable via /proc")
        self.assertLess(
            age_h, 24,
            f"wrapped etimes leaked into CreationDate: age={age_h}h. This is the "
            "defect that made a 1s-old process a SIGKILL candidate.",
        )

    def test_unageable_process_is_never_a_candidate(self):
        """Fail-safe half. A PID with no /proc entry AND a corrupt etimes must
        yield CreationDate=None, and None must keep it OUT of the candidate set
        rather than defaulting it to something reapable."""
        pid = self._NO_SUCH_PID
        self.assertFalse(
            os.path.exists(f"/proc/{pid}"),
            f"precondition: /proc/{pid} must not exist, else this case silently "
            "stops testing the un-ageable path (guard-1699)",
        )
        procs = self._run_with_ps(
            f"{self._PS_HEADER}\n"
            f"{pid} 1 {self._WRAPPED} python3 python3 /x/core/scripts/hook.py\n"
        )
        self.assertEqual(len(procs), 1)
        self.assertIsNone(procs[0]["CreationDate"])
        self.assertIsNone(sjs.process_age_hours(procs[0]))
        cands = sjs.identify_candidates(procs, None, [], set(),
                                        sjs.DEFAULT_THRESHOLDS)
        self.assertEqual(
            [c for c in cands if c["pid"] == pid], [],
            "an un-ageable process must never be reaped -- unknown age is not old age",
        )

    def test_unageable_process_still_enumerated_for_ancestry(self):
        """None must mean 'age unknown', NOT 'drop the row'. Dropping it would
        punch a hole in by_pid, making a live parent look dead and turning its
        children reapable -- a fix that creates the failure it prevents."""
        pid = self._NO_SUCH_PID
        procs = self._run_with_ps(
            f"{self._PS_HEADER}\n"
            f"{pid} 1 {self._WRAPPED} bash bash /x/wrapper.sh\n"
        )
        self.assertEqual([p["ProcessId"] for p in procs], [pid])


class SsmRemoteSignatureTest(unittest.TestCase):
    """. The ssh-shaped detector went blind when operator access moved
    to SSM: nothing in the live chain is an `ssh` process any more.

    The negative half is the load-bearing one. Measured against a real long call,
    the chain is TWO durable bash wrappers (efs-ssh.sh, ssm-run.sh) that persist
    for the whole operation, plus `aws-exec.sh ssm get-command-invocation` and its
    `aws` child, which are ~2s subprocesses RESPAWNED EVERY POLL by the wait loop.
    So the obvious remedy -- 'add an aws ssm pattern' -- fires on perfectly
    HEALTHY in-flight calls, and every one of those poll processes is young enough
    to also hit the etimes corruption above. These tests pin the durable-wrapper
    choice so a future well-meaning edit cannot quietly re-introduce it."""

    def test_durable_wrappers_are_classified(self):
        for cmd in (
            "bash /w/scripts/efs-ssh.sh sleep 120; echo done",
            "bash /w/scripts/ssm-run.sh i-04063a4d5a9ded043 'uptime'",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(sjs.classify_orphan(cmd, "bash"), "ssm-remote")

    def test_transient_poll_subprocesses_are_NOT_classified(self):
        """These respawn every ~2s during a HEALTHY call. Matching them would
        reap working operator connections."""
        for cmd, name in (
            ("bash /w/scripts/aws-exec.sh ssm get-command-invocation --command-id x",
             "bash"),
            ("/snap/aws-cli/2312/bin/aws ssm get-command-invocation --command-id x",
             "aws"),
            ("/snap/aws-cli/2312/bin/aws ssm send-command --instance-ids i-abc",
             "aws"),
        ):
            with self.subTest(cmd=cmd):
                self.assertNotEqual(
                    sjs.classify_orphan(cmd, name), "ssm-remote",
                    "transient per-poll subprocess must not be reapable",
                )

    def test_ssh_signature_retained(self):
        """Kept deliberately: other hosts may still be reached by ssh, so
        replacing rather than adding would trade one blind spot for another."""
        self.assertEqual(
            sjs.classify_orphan("ssh -o Foo=bar ec2-user@10.0.0.1 uptime", "ssh"),
            "ssh-efs",
        )

    def test_ssm_remote_has_a_threshold(self):
        """A label with no threshold silently inherits `default` (12h), which
        would make a wedged 1h connection invisible for half a day."""
        self.assertEqual(sjs.DEFAULT_THRESHOLDS["ssm-remote"], 1)


if __name__ == "__main__":
    unittest.main()
