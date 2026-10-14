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

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CHECK = PROJECT_ROOT / "core" / "scripts" / "check-tests-no-live-agent-wm.py"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# The fixtures below bind "alpha". Inject it as the roster instead of deriving the
# live one: the derivation reads the daemon-backed team-state-read.sh, which a
# deployment's pytest run cannot reach (-a), and a test of the predicate
# should not depend on which agents happen to be live today.
ROSTER_ARGS = ["--roster", "alpha"]


def _run(scan_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK), str(scan_dir), *ROSTER_ARGS],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )


def _run_argv(*argv: str) -> subprocess.CompletedProcess:
    """Run the check with an ARBITRARY argv, for the shapes _run cannot express."""
    return subprocess.run(
        [sys.executable, str(CHECK), *argv],
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
        [sys.executable, str(CHECK), *ROSTER_ARGS],
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


def test_roster_seam_is_explicit_and_the_default_still_fails_closed(tmp_path, monkeypatch, capsys):
    """--roster makes a daemon-free run possible; WITHOUT it a roster that cannot be derived still exits 2.

    The seam exists so the predicate tests above need no daemon (g-374-628-a). It must
    not weaken the contract in the guard's header: a check that cannot see its roster
    has no opinion and must not report success. Pinned in-process against a
    live_roster() that cannot derive, so the test needs no daemon either way.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_check_wm_guard3", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _cannot_derive():
        raise RuntimeError("no daemon (simulated)")

    monkeypatch.setattr(mod, "live_roster", _cannot_derive)
    (tmp_path / "test_clean.py").write_text(SAFE_NO_WRITE, encoding="utf-8")

    assert mod.main(["check", str(tmp_path)]) == 2
    assert "could not derive the live roster" in capsys.readouterr().out

    assert mod.main(["check", str(tmp_path), "--roster", "alpha"]) == 0
    assert "INJECTED" in capsys.readouterr().out

    assert mod.main(["check", str(tmp_path), "--roster", ""]) == 2


def test_live_roster_derivation_is_pinned_without_a_daemon(monkeypatch):
    """The layer the --roster seam steps over: live_roster() itself.

    The predicate tests inject their roster, so nothing above exercises how the live
    one is DERIVED -- a seam's placement is a silent scope declaration (guard-1462) and
    the derivation of an injected value is covered by nothing unless it is pinned
    (rb-6003). Pinned here against a faked team-state-read.sh run, so it needs no
    daemon: the agent_status keys are the roster, and every shape that is not a
    non-empty mapping raises, which main() turns into exit 2. The real end-to-end
    derivation is the default (no --roster) run the precheck lane makes every cycle.
    """
    import importlib.util
    import subprocess as sp
    import types

    spec = importlib.util.spec_from_file_location("_check_wm_guard4", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def fake_run(rc, out, err=""):
        return types.SimpleNamespace(
            run=lambda cmd, **kw: sp.CompletedProcess(cmd, rc, out, err))

    monkeypatch.setattr(mod, "subprocess", fake_run(0, '{"alpha": {}, "zeta": {"last_active": "x"}}'))
    assert mod.live_roster() == {"alpha", "zeta"}

    # rc 1 WITH a valid payload: a failed wrapper must not read as a roster.
    for rc, out in ((1, '{"alpha": {}}'), (0, ""), (0, "{}"), (0, "[]"), (0, '"alpha"')):
        monkeypatch.setattr(mod, "subprocess", fake_run(rc, out, "no daemon"))
        try:
            mod.live_roster()
        except RuntimeError:
            continue
        raise AssertionError(f"live_roster() returned normally for rc={rc} stdout={out!r}")


# --- argv and scan-surface refusals (-a fresh-eyes follow-through) ---
# Each shape below used to exit 0 with a PASS, or crash, because the check hand-parses
# argv and trusted whatever population it was handed. A check that cannot see its
# population must not report success (the header's own contract). Each is pinned by
# its SPECIFIC refusal text and not by the rc alone: exit 2 is also what an
# underivable roster returns, so a bare rc would pass for the wrong reason on a box
# with no daemon.

_UNRECOGNISED = ("unrecognised argument", "usage: check-tests-no-live-agent-wm.py")
_VERDICT_PASS = "PASS: no test writes working memory"


def test_unparseable_file_is_scanned_not_a_crash(tmp_path):
    """One file that does not parse must not take the whole check down.

    analyze() returned a bare `[]` on SyntaxError where main() unpacks a pair, so a
    single unparseable file anywhere in the scan raised ValueError: rc 1 with an empty
    stdout, which the precheck lane's battery table (rcs=(1,)) reads as an OFFENDER.
    The clean sibling proves the scan went on past it.
    """
    (tmp_path / "test_broken.py").write_text("def (:\n", encoding="utf-8")
    (tmp_path / "test_clean.py").write_text(SAFE_NO_WRITE, encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "scanned: 2 python file(s)" in proc.stdout, proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr


@pytest.mark.parametrize("make_argv, expected", [
    pytest.param(lambda d: [str(d / "nope"), *ROSTER_ARGS],
                 ("is not a directory",), id="missing-directory"),
    pytest.param(lambda d: ["--rostr", "alpha", *ROSTER_ARGS],
                 _UNRECOGNISED, id="mistyped-flag"),
    pytest.param(lambda d: [str(d), *ROSTER_ARGS, "--roster=alpha"],
                 _UNRECOGNISED, id="equals-form-beside-the-real-flag"),
    pytest.param(lambda d: [str(d), str(d), *ROSTER_ARGS],
                 _UNRECOGNISED, id="second-directory"),
    pytest.param(lambda d: ["--roster", str(d)],
                 ("do not look like agent names",), id="roster-took-the-directory"),
])
def test_bad_argv_is_refused_rather_than_scanned_as_nothing(tmp_path, make_argv, expected):
    """A mistyped flag, the `=` form, a missing or doubled directory, or a roster
    that swallowed the directory must exit 2 with a FAIL, never `scanned: 0` + PASS.

    The old hand-parse took args[0] as the directory and dropped the rest, so the
    mistyped flag BECAME the scan directory (a glob over nothing), and `--roster <dir>`
    made the directory the roster and ran the full default scan against nobody.
    """
    (tmp_path / "test_clean.py").write_text(SAFE_NO_WRITE, encoding="utf-8")
    proc = _run_argv(*make_argv(tmp_path))
    assert proc.returncode == 2, f"rc={proc.returncode}\nstdout:\n{proc.stdout}"
    for text in expected:
        assert text in proc.stdout, f"{text!r} missing from:\n{proc.stdout}"
    assert _VERDICT_PASS not in proc.stdout, proc.stdout


def test_empty_scan_surface_is_refused_not_passed(tmp_path):
    """A directory with no test file is an empty population, not a clean one."""
    proc = _run(tmp_path)
    assert proc.returncode == 2, f"rc={proc.returncode}\nstdout:\n{proc.stdout}"
    assert "the scan opened no file" in proc.stdout, proc.stdout
    assert _VERDICT_PASS not in proc.stdout, proc.stdout


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_still_works_beside_the_refusal(flag):
    """guard-2680: the arm that now refuses every unknown flag must not catch --help.

    It prints the accepted arguments and exits 0 without scanning anything.
    """
    proc = _run_argv(flag)
    assert proc.returncode == 0, f"rc={proc.returncode}\nstdout:\n{proc.stdout}"
    assert proc.stdout.startswith("usage: check-tests-no-live-agent-wm.py"), proc.stdout
    assert "scanned:" not in proc.stdout, proc.stdout


def test_agent_shaped_roster_names_are_still_accepted(tmp_path):
    """The accepting side of the name check: real agent names, several at once."""
    (tmp_path / "test_clean.py").write_text(SAFE_NO_WRITE, encoding="utf-8")
    proc = _run_argv(str(tmp_path), "--roster", "alpha,test-agent_2")
    assert proc.returncode == 0, f"rc={proc.returncode}\nstdout:\n{proc.stdout}"
    assert "['alpha', 'test-agent_2']" in proc.stdout, proc.stdout
    assert "INJECTED" in proc.stdout, proc.stdout
