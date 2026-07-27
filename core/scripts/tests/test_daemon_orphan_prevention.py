"""test_daemon_orphan_prevention.py —  v3 regression test.

Verifies the bulletproof daemon kill+spawn pipeline:
  1. daemon.parent.pid is written at startup
  2. N consecutive `mind-api-start.sh --restart` cycles leave exactly ONE
     daemon pair alive (the latest), no orphans
  3. The standalone daemon-orphan-sweep.sh reports clean state after each
     cycle
  4. The orphan-sweep can clean up an injected orphan

Skipped on POSIX — the orphan failure mode is Windows-specific (py.exe
launcher + MSYS kill semantics). POSIX gets the same code defensively but
the regression class only ever fired on Windows.

The test calls the real shell scripts and asserts on real OS process state.
This is the test that would have caught the 36-orphan accumulation the
user surfaced 2026-05-22, had it existed before.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent                # core/scripts
PROJECT_ROOT = CORE_SCRIPTS.parent.parent       # repo root (NOT just core/)
START_SH = CORE_SCRIPTS / "mind-api-start.sh"
SWEEP_SH = CORE_SCRIPTS / "daemon-orphan-sweep.sh"


def _is_windows() -> bool:
    return sys.platform == "win32"


# B16: daemon_integration — this file spawns REAL subprocess daemons via
# mind-api-start.sh AND counts system-wide mind_api.src processes (Get-CimInstance),
# so it conflicts with any live daemon (it would count/kill it). Exclude it from
# live-daemon runs: pytest -m "not daemon_integration". Run only in a quiescent
# window (agents stopped). The system-wide process count is why RUNTIME_DIR
# isolation alone can't make this one safe — the marker is the right tool here.
pytestmark = [
    pytest.mark.skipif(
        not _is_windows(),
        reason="g-115-764 orphan class is Windows-specific (py.exe launcher + MSYS kill).",
    ),
    pytest.mark.daemon_integration,
]


def _bash_path(p: Path) -> str:
    """Convert a Windows path to /c/... form for Git Bash."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _count_mind_api_processes() -> dict:
    """Return {'py.exe': N, 'python.exe': M, 'pids': [...]} for live
    mind_api.src processes."""
    ps_cmd = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name='py.exe' OR Name='python.exe'\" "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -match 'mind_api\\.src' } | "
        "ForEach-Object { Write-Output \"$($_.Name)|$($_.ProcessId)|$($_.ParentProcessId)\" }"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
        capture_output=True, text=True, timeout=15,
    )
    counts = {"py.exe": 0, "python.exe": 0, "pids": [], "pairs": []}
    by_name = {"py.exe": [], "python.exe": []}
    for line in proc.stdout.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name, pid, ppid = parts
        try:
            pid_i = int(pid)
            ppid_i = int(ppid)
        except ValueError:
            continue
        if name in counts:
            counts[name] += 1
            counts["pids"].append(pid_i)
            by_name[name].append((pid_i, ppid_i))
    # Pair up: each python.exe should have a py.exe parent.
    py_pids = {p for p, _ in by_name["py.exe"]}
    for child_pid, child_ppid in by_name["python.exe"]:
        if child_ppid in py_pids:
            counts["pairs"].append((child_ppid, child_pid))
    return counts


def _read_state_files() -> dict:
    state = PROJECT_ROOT / "mind_api" / "state"
    out = {}
    for name in ("daemon.pid", "daemon.port", "daemon.parent.pid"):
        p = state / name
        out[name] = p.read_text(encoding="utf-8").strip() if p.exists() else None
    return out


def _run_start(args=()):
    """Invoke mind-api-start.sh and return CompletedProcess."""
    cmd = [BASH, _bash_path(START_SH), *args]
    # : this file is the deliberate-operator exception to the
    # shared-runtime claim gate in mind-api-start.sh. It spawns REAL daemons
    # into the SHARED mind_api/state on purpose — it counts system-wide
    # mind_api.src processes, so RUNTIME_DIR isolation cannot make it safe
    # (see the pytestmark note above). Opt in explicitly; every OTHER test
    # must isolate with RUNTIME_DIR instead of taking this hatch.
    env = dict(os.environ)
    env["MIND_ALLOW_SHARED_DAEMON_FROM_TEST"] = "1"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                          cwd=str(PROJECT_ROOT), env=env)


def _run_sweep(args=()):
    """Invoke daemon-orphan-sweep.sh and return CompletedProcess."""
    cmd = [BASH, _bash_path(SWEEP_SH), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT))


def _wait_for_pid_change(prev_pid: str, max_seconds: float = 10.0) -> str:
    """Poll daemon.pid until it changes from prev_pid. Returns the new pid."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        cur = _read_state_files()["daemon.pid"]
        if cur and cur != prev_pid:
            return cur
        time.sleep(0.1)
    raise TimeoutError(f"daemon.pid did not change from {prev_pid} within {max_seconds}s")


def test_parent_pid_file_is_written_at_startup():
    """The daemon must write daemon.parent.pid alongside daemon.pid.

    Without this, the kill path falls back to a Win32 .ParentProcessId
    lookup that silently no-ops once the child has exited — the root
    cause of the 17% orphan rate.
    """
    # Ensure a fresh daemon is running.
    rc = _run_start(["--restart"])
    # On --restart, mind-api-start.sh waits up to 10s for the daemon to
    # bind + respond to /health before exiting 0. Either condition (rc 0 OR
    # daemon.pid present) is sufficient evidence the daemon is alive.
    state = _read_state_files()
    if rc.returncode != 0:
        # Spawn timed out (e.g., test machine slow). Re-read after a short
        # settling wait — the file may have landed between exit and our read.
        time.sleep(2.0)
        state = _read_state_files()
    assert state["daemon.pid"], (
        f"daemon.pid not written. rc={rc.returncode} "
        f"stdout={rc.stdout!r} stderr={rc.stderr!r} state={state}"
    )
    assert state["daemon.parent.pid"], (
        "daemon.parent.pid not written — kill path will fall back to "
        "Win32 lookup which silently no-ops on graceful exit (g-115-764 v3 bug)"
    )
    # parent_pid should be different from child_pid (different process).
    assert state["daemon.pid"] != state["daemon.parent.pid"], (
        "daemon.parent.pid == daemon.pid; expected separate py.exe parent"
    )


def test_n_restarts_leave_exactly_one_pair():
    """N back-to-back --restart calls must leave exactly one daemon pair
    FROM THIS REPO. Other repos' daemons (same cmdline, different cwd) are
    ignored — Win32_Process exposes no cwd/env discriminator, so we scope
    by THIS repo's published daemon.pid + daemon.parent.pid.

    Empirical history: 204 restarts in 37 hours leaked 35 orphan pairs
    (~17% rate) on the pre-fix code. This test runs 10 cycles and asserts
    a single pair survives. Even a 1% leak rate would produce ≥1 orphan
    across 10 cycles 9.6% of the time — so a sufficient regression signal.
    """
    # Baseline: ensure daemon is up + count concurrent daemons from
    # OTHER repos so we don't false-fail on a multi-repo machine.
    rc = _run_start(["--restart"])
    assert rc.returncode == 0, f"baseline --restart failed: {rc.stderr}"
    state = _read_state_files()
    baseline_pairs = _count_mind_api_processes()["pairs"]
    legit_pair = (int(state["daemon.parent.pid"]), int(state["daemon.pid"]))
    cross_repo_pairs = [p for p in baseline_pairs if p != legit_pair]

    N = 10
    for i in range(N):
        prev_pid = _read_state_files()["daemon.pid"]
        rc = _run_start(["--restart"])
        assert rc.returncode == 0, f"cycle {i} --restart failed: {rc.stderr}"
        new_pid = _wait_for_pid_change(prev_pid, max_seconds=10.0)
        assert new_pid != prev_pid, f"cycle {i}: daemon.pid did not advance"

    # Give a moment for any in-flight orphans to settle.
    time.sleep(1.0)
    counts = _count_mind_api_processes()
    pairs = counts["pairs"]
    # Subtract cross-repo pairs (alive throughout the test, unchanged).
    this_repo_pairs = [p for p in pairs if p not in cross_repo_pairs]
    assert len(this_repo_pairs) == 1, (
        f"after {N} restarts expected exactly 1 daemon pair from THIS repo, "
        f"found {len(this_repo_pairs)}: this_repo={this_repo_pairs}, "
        f"cross_repo={cross_repo_pairs}, all_pairs={pairs}"
    )
    # The surviving pair must be the latest published one.
    final_state = _read_state_files()
    final_legit = (int(final_state["daemon.parent.pid"]), int(final_state["daemon.pid"]))
    assert final_legit in this_repo_pairs, (
        f"published pair {final_legit} not in this_repo_pairs {this_repo_pairs}"
    )


def test_sweep_reports_clean_when_no_orphans():
    """daemon-orphan-sweep.sh exit 0 when state is healthy.

    "Healthy" means no orphans IN THIS REPO. The sweep's CommandLine match
    doesn't distinguish daemons from different repos, so on a multi-repo
    machine the sweep will report OTHER repos' daemons as "orphans" of
    THIS repo's published state. Skip the strict 'Orphans: 0' check if
    cross-repo daemons exist — verify the sweep simply exits 0 (didn't
    crash) and reports the legit pair as KEEP.
    """
    rc = _run_start(["--restart"])
    assert rc.returncode == 0
    time.sleep(0.5)
    state = _read_state_files()
    legit_pair = (int(state["daemon.parent.pid"]), int(state["daemon.pid"]))
    pairs_before = _count_mind_api_processes()["pairs"]
    cross_repo_present = any(p != legit_pair for p in pairs_before)

    rc = _run_sweep([])
    assert rc.returncode == 0, f"sweep failed in healthy state: {rc.stderr}"

    if cross_repo_present:
        # Multi-repo: sweep will report cross-repo daemons as orphans.
        # Just verify the legit pair was KEPT (not flagged as orphan).
        assert f"KEEP PID={legit_pair[1]}" in rc.stdout, (
            f"legit child PID {legit_pair[1]} not KEPT by sweep:\n{rc.stdout}"
        )
        assert f"KEEP PID={legit_pair[0]}" in rc.stdout, (
            f"legit parent PID {legit_pair[0]} not KEPT by sweep:\n{rc.stdout}"
        )
    else:
        # Single-repo: strict 'Orphans: 0' check applies.
        assert "Orphans (not in published state): 0" in rc.stdout, (
            f"expected 'Orphans: 0' in healthy report; got:\n{rc.stdout}"
        )


def test_sweep_strict_detects_and_clean_reaps_injected_orphan():
    """Sweep --strict should detect a true orphan; --clean should reap it.

    Inject a long-lived `py -3 -c "...mind_api.src..."` process. Its
    CommandLine literally contains the string "mind_api.src" (the regex
    the sweep matches on), so PowerShell's
    `Where-Object { $_.CommandLine -match 'mind_api\\.src' }` sees it.
    py.exe spawns python.exe as child — both end up matching.

    The previous version of this test used `py -3 -m mind_api.src` which
    exits immediately because is_daemon_alive() returns True, so it never
    actually injected a persistent orphan and the assertions were
    trivially true. This rewrite ensures we exercise the sweep's KILL
    path, not just its no-op path.
    """
    rc = _run_start(["--restart"])
    assert rc.returncode == 0
    legit_state = _read_state_files()
    legit_pid = legit_state["daemon.pid"]
    legit_parent = legit_state["daemon.parent.pid"]
    assert legit_pid, "legit daemon.pid not written"
    assert legit_parent, "legit daemon.parent.pid not written (Stage 1 fix missing?)"

    # Baseline orphan count — anything matching the regex now is "real"
    # leakage from another test session and shouldn't fail us.
    counts_before = _count_mind_api_processes()
    baseline_pairs = len(counts_before["pairs"])

    # CROSS-REPO SAFETY: daemon-orphan-sweep.sh matches on cmdline
    # 'mind_api.src' alone — it cannot distinguish between daemons from
    # DIFFERENT repos (same cmdline, no cwd/env exposed via Win32_Process).
    # Skip only if a baseline pair is NOT in the sweep's keepset — meaning
    # it belongs to another repo and --clean would kill it as a false-positive
    # orphan.  Pairs fully inside the keepset are THIS repo's protected
    # daemons; --clean will never touch them, so this test is safe to run.
    if baseline_pairs > 1:
        ks_rc = _run_sweep(["--print-keepset"])
        keepset_pids: set[int] = set()
        for line in ks_rc.stdout.splitlines():
            if line.startswith("KEEPSET_PIDS="):
                raw = line.split("=", 1)[1].strip()
                if raw:
                    keepset_pids = {int(p) for p in raw.split(",") if p.strip().isdigit()}
                break
        unprotected = [
            pair for pair in counts_before["pairs"]
            if not (pair[0] in keepset_pids and pair[1] in keepset_pids)
        ]
        if unprotected:
            pytest.skip(
                f"multi-repo daemon environment: {len(unprotected)} unprotected pair(s) "
                f"alive ({unprotected}); --clean would kill them — skipping to avoid "
                f"cross-repo collateral"
            )

    # Inject a real orphan: long-lived python whose CommandLine contains
    # the literal 'mind_api.src' substring (with the dot — that's what the
    # sweep regex matches on). The marker is a string LITERAL not a comment,
    # because `# anything` would make `; time.sleep()` part of the comment
    # and the process would exit immediately after `import time`.
    #
    # Do NOT use creationflags=DETACHED_PROCESS — empirically on this Windows
    # build, DETACHED_PROCESS subprocess of `py -3 -c "..."` doesn't surface
    # in Get-CimInstance Win32_Process within the test's polling window, so
    # the sweep cannot see it. Plain Popen produces both py.exe and python.exe
    # with the full -c args in CommandLine, matching the regex.
    inject_payload = (
        '_marker = "mind_api.src injection for test_daemon_orphan_prevention"; '
        'import time; time.sleep(120)'
    )
    inject = subprocess.Popen(
        ["py", "-3", "-c", inject_payload],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Track the PID for guaranteed teardown if anything below raises.
    inject_root_pid = inject.pid
    try:
        # Let py.exe spawn python.exe and both stabilize.
        time.sleep(1.5)

        # Verify the orphan is actually visible to the sweep BEFORE
        # asserting against it (catches injection-failed cases early).
        counts_with_inject = _count_mind_api_processes()
        injected_count = len(counts_with_inject["pairs"]) - baseline_pairs
        if injected_count < 1:
            # Injection didn't produce a detectable orphan. Skip rather
            # than false-positive — the sweep CAN'T clean what it can't
            # see, and the cmdline-match behavior is what we're indirectly
            # testing here.
            pytest.skip(
                f"injection did not produce a detectable orphan "
                f"(baseline={baseline_pairs}, now={len(counts_with_inject['pairs'])}) "
                f"— likely a Windows env where -c args don't appear in "
                f"CommandLine, or py.exe didn't spawn"
            )

        # The legit daemon must still be alive.
        state_now = _read_state_files()
        assert state_now["daemon.pid"] == legit_pid, (
            "injection killed the legit daemon — bad test"
        )

        # sweep --strict must exit 1 (orphans present).
        rc_strict = _run_sweep(["--strict"])
        assert rc_strict.returncode == 1, (
            f"sweep --strict should exit 1 when orphans present; "
            f"got rc={rc_strict.returncode}\n{rc_strict.stdout}\n{rc_strict.stderr}"
        )
        assert "Orphans (not in published state): 0" not in rc_strict.stdout, (
            f"sweep reported 0 orphans but we injected one:\n{rc_strict.stdout}"
        )

        # sweep --clean must exit 0 and actually kill the orphan.
        rc_clean = _run_sweep(["--clean"])
        assert rc_clean.returncode == 0, (
            f"--clean failed: rc={rc_clean.returncode}\n"
            f"stdout={rc_clean.stdout}\nstderr={rc_clean.stderr}"
        )

        # Settle, then verify the orphan is gone.
        time.sleep(1.0)
        counts_after = _count_mind_api_processes()
        # The legit pair should still be present.
        assert len(counts_after["pairs"]) >= 1, (
            f"clean swept the legit daemon! pairs={counts_after['pairs']}"
        )
        # The legit pair's PIDs are still in counts_after.
        legit_pair = (int(legit_parent), int(legit_pid))
        assert legit_pair in counts_after["pairs"], (
            f"legit pair {legit_pair} not in counts_after['pairs']={counts_after['pairs']}"
        )
        # The injected PID should NOT be in counts_after.
        assert inject_root_pid not in counts_after["pids"], (
            f"injected PID {inject_root_pid} still alive after --clean; "
            f"pids={counts_after['pids']}"
        )

        # And sweep --strict should now exit 0 (no orphans).
        rc_strict2 = _run_sweep(["--strict"])
        assert rc_strict2.returncode == 0, (
            f"sweep --strict should exit 0 after --clean; "
            f"got rc={rc_strict2.returncode}\n{rc_strict2.stdout}"
        )
    finally:
        # Guaranteed teardown: kill the injected process if --clean didn't.
        try:
            inject.terminate()
            inject.wait(timeout=2)
        except Exception:
            try:
                inject.kill()
                inject.wait(timeout=2)
            except Exception:
                pass
        # Also force-kill by PID via taskkill (handles detached descendants).
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(inject_root_pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


if __name__ == "__main__":
    # Allow running directly: py -3 test_daemon_orphan_prevention.py
    pytest.main([__file__, "-v", "-s"])
