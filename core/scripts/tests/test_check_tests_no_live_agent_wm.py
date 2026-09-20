""" outcomes 4+5 — the live-agent-WM guard's scan surface and write vocabulary.

Outcome 4: the guard scans every DECLARED pytest testpath, not just
core/scripts/tests. Before this, 166 of 1,522 test files (11%) — everything
under mind_api/tests and core/tests/gates — were structurally invisible: not
scanned, not parsed, never reportable, while the guard printed PASS.

Outcome 5: the guard recognises the DAEMON HTTP write route (POST /v1/wm/set).
wm-set.sh and wm-append.sh are daemon-only, so that route is how production
writes working memory — and the guard's vocabulary knew only the two wrapper
basenames and the wm.py subprocess shape.

The two compose, which is the point. The files most likely to write WM through
the daemon are exactly the ones under mind_api/tests, so a test binding a live
agent and posting to the daemon was invisible on BOTH axes at once — the
original g-115-4887 incident's shape exactly.

WHY THE SAFE CASES ARE TESTED AS HARD AS THE BAD ONE. This guard's own header
says a check that "fails from the day it lands trains every reader to skim the
section it lives in". Recognising the HTTP route WITHOUT recognising the
isolation mechanisms did exactly that: measured on arrival, it flagged
test_wm_lock_spans_read_write_g115_8667.py and
test_wm_lost_update_no_stall_g115_8536.py, both of which run against a
DaemonFixture over a tmp project root and were correct. A false positive here is
not a lesser bug than a false negative — it is how the guard gets ignored.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CHECK = PROJECT_ROOT / "core" / "scripts" / "check-tests-no-live-agent-wm.py"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run(scan_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK), str(scan_dir)],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )


# --- the KNOWN-BAD fixture (outcome 5) --------------------------------------
# A live-roster agent bound, a daemon WM write, and NOTHING pointing either
# away from the live agent. This is the shape the guard was blind to.
KNOWN_BAD = '''\
import os, urllib.request
def test_writes_live_agent_wm_over_http():
    env = dict(os.environ)
    env["MIND_AGENT"] = "alpha"
    urllib.request.urlopen("http://127.0.0.1:8899/v1/wm/set?slot=current_goal")
'''

# --- the SAFE caller that redirects (outcome 5's second half) ---------------
SAFE_AGENT_DIR_REDIRECT = '''\
import os, urllib.request
def test_writes_wm_over_http_but_redirected(tmp_path):
    env = dict(os.environ)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(tmp_path)
    urllib.request.urlopen("http://127.0.0.1:8899/v1/wm/set?slot=current_goal")
'''

# --- the SAFE caller that uses the hermetic in-process daemon ---------------
SAFE_DAEMON_FIXTURE = '''\
import os, urllib.request
from _daemon_fixture import DaemonFixture
AGENT = "alpha"
def test_writes_wm_over_http_against_a_throwaway_daemon(tmp_path):
    with DaemonFixture(tmp_path / "world", agent=AGENT) as df:
        urllib.request.urlopen(f"http://127.0.0.1:{df.port}/v1/wm/set?slot=current_goal")
'''

# --- a live agent named, but NO write: must stay clean ----------------------
# The guard's deliberate narrowness — 59 test files bind a real agent name for
# path routing alone, and flagging those would make it red on arrival.
SAFE_NO_WRITE = '''\
import os
def test_binds_a_live_agent_for_path_routing_only():
    env = dict(os.environ)
    env["MIND_AGENT"] = "alpha"
    assert env["MIND_AGENT"] == "alpha"
'''


def test_daemon_http_route_is_recognised_as_a_wm_write(tmp_path):
    """The outcome-5 property, against a known-bad fixture."""
    (tmp_path / "test_known_bad.py").write_text(KNOWN_BAD, encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 1, (
        "the guard did not flag a test that binds a LIVE agent and writes "
        f"working memory over the daemon HTTP route.\nstdout:\n{proc.stdout}"
    )
    assert "test_known_bad.py" in proc.stdout
    assert "alpha" in proc.stdout


def test_agent_dir_redirect_is_not_flagged(tmp_path):
    """The existing safe caller that redirects MIND_AGENT_DIR must still pass."""
    (tmp_path / "test_safe_redirect.py").write_text(SAFE_AGENT_DIR_REDIRECT, encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 0, (
        "the guard flagged a test that redirects MIND_AGENT_DIR to a throwaway "
        f"dir — a false positive is how this guard gets ignored.\nstdout:\n{proc.stdout}"
    )
    assert "MIND_AGENT_DIR=1" in proc.stdout, (
        "the exemption must be REPORTED, not silent — a suppression nobody can "
        f"see cannot be reviewed.\nstdout:\n{proc.stdout}"
    )


def test_hermetic_daemon_fixture_is_not_flagged(tmp_path):
    """A DaemonFixture over a tmp project root cannot reach the live agent."""
    (tmp_path / "test_safe_fixture.py").write_text(SAFE_DAEMON_FIXTURE, encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 0, (
        "the guard flagged a test running against a hermetic in-process daemon "
        f"over a tmp world.\nstdout:\n{proc.stdout}"
    )
    assert "DaemonFixture=1" in proc.stdout


def test_live_agent_without_a_write_is_not_flagged(tmp_path):
    """Both halves of the defect are required — the guard's founding narrowness."""
    (tmp_path / "test_safe_no_write.py").write_text(SAFE_NO_WRITE, encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 0, (
        f"binding a live agent without writing WM must not flag.\nstdout:\n{proc.stdout}"
    )


def test_declared_testpaths_are_all_scanned(tmp_path):
    """Outcome 4: the default scan covers every testpath pytest.ini declares."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_check_wm_guard", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    declared = mod.declared_testpaths()
    names = {p.relative_to(PROJECT_ROOT).as_posix() for p in declared}
    assert "core/scripts/tests" in names
    assert "mind_api/tests" in names, (
        "mind_api/tests is declared in pytest.ini but not in the guard's scan "
        "surface — the daemon-route writers live there"
    )
    assert "core/tests/gates" in names


def test_default_run_reports_the_newly_scanned_count():
    """The widening must be REPORTED as a number, not asserted (guard-1562)."""
    proc = subprocess.run(
        [sys.executable, str(CHECK)],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    assert "declared testpath(s)" in proc.stdout, proc.stdout
    assert "newly-scanned" in proc.stdout, proc.stdout
    assert "isolation-exempted" in proc.stdout, proc.stdout


def test_unparseable_testpaths_fail_loud_rather_than_narrowing(tmp_path, monkeypatch):
    """A guard that cannot see its scan surface must not report success.

    Same fail-loud contract the roster and the write vocabulary already carry:
    exit 2, never a PASS over directories nobody opened.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_check_wm_guard2", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    empty_ini = tmp_path / "pytest.ini"
    empty_ini.write_text("[pytest]\n", encoding="utf-8")
    monkeypatch.setattr(mod, "PYTEST_INI", empty_ini)
    try:
        mod.declared_testpaths()
    except RuntimeError as exc:
        assert "underivable" in str(exc)
    else:
        raise AssertionError(
            "declared_testpaths() returned normally for a pytest.ini with no "
            "testpaths — it must raise so main() can exit 2"
        )
