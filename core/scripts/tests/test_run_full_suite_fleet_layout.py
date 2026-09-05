#!/usr/bin/env python3
"""fleet_layout marker plumbing in run-full-suite ().

Filed by omni from ZDS-Mind: a single-agent deployment's full suite reported 132
GENUINE unowned failures across 45 files, every one a fixture assuming a
POPULATED MULTI-AGENT layout. The code under test behaves correctly there; the
fixtures' premise is false. Since run-full-suite-after-deep-code makes a green
suite the closure criterion for every deep core/scripts change, that criterion is
unsatisfiable on such a deployment.

Two defects are pinned here, and the SECOND is the dangerous one:

  1. The roster predicate must never report a confident "single-agent" when it
     could not actually look (the `mandatory-step-vacuity` class: a step that
     silently has nothing to do is indistinguishable from one that ran clean).

  2. pytest's -m flags DO NOT AND TOGETHER -- the last one wins and the earlier
     is silently discarded. Measured 2026-08-19 (zeta, hostname cc-02, uname -r
     6.8.0-137-generic) against core/scripts/tests/test_daemon_orphan_prevention.py:
     `-m "not daemon_integration"` alone collected 0 files; adding a second,
     harmless `-m` collected 1 -- the daemon exclusion had vanished. Emitting the
     fleet_layout clause as a second -m would therefore silently repeal the
     Live-Daemon Exception and let the suite hijack the live daemon out from
     under the running fleet (two daemon storms on 2026-05-31). These tests fail
     RED against the two-flag form.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = SCRIPT_DIR.parent / "run-full-suite.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_full_suite_fleet", TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_full_suite_fleet"] = mod
    spec.loader.exec_module(mod)
    return mod


RFS = _load()


# ── the roster predicate ────────────────────────────────────────────────────

def test_stub_agent_dirs_do_not_count_as_populated(tmp_path):
    """The ZDS shape: one real agent, three dirs holding only session/.

    This is the exact layout omni measured -- `agents/alpha`, `agents/delta` and
    `agents/zeta` exist but contain only `session/`. If stubs counted, the box
    would read as a 4-agent fleet and the clause would never fire where it is
    needed.
    """
    (tmp_path / "omni").mkdir()
    (tmp_path / "omni" / "self.md").write_text("x", encoding="utf-8")
    for stub in ("alpha", "delta", "zeta"):
        (tmp_path / stub / "session").mkdir(parents=True)

    names, err = RFS._populated_agents(tmp_path)

    assert err is None
    assert names == ["omni"], "stub dirs must not count as populated agents"


def test_multiple_identities_read_as_a_fleet(tmp_path):
    for name in ("alpha", "omni"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "self.md").write_text("x", encoding="utf-8")

    names, err = RFS._populated_agents(tmp_path)

    assert err is None
    assert names == ["alpha", "omni"]


def test_unreadable_root_returns_unknown_not_zero(tmp_path):
    """UNKNOWN must be distinguishable from single-agent.

    Without the root probe, a missing or misresolved agents root makes every
    lookup a confident False -- N negatives instead of N unknowns -- which reads
    as "single-agent" and silently narrows the suite on every box. Mirrors the
    root probe in fleet_config_parity._has_agent_identity, which acquired it for
    the same reason.
    """
    names, err = RFS._populated_agents(tmp_path / "does-not-exist")

    assert names is None, "a root that cannot be read must not report a count"
    assert err and "not a directory" in err


# ── the marker expression ───────────────────────────────────────────────────

def _capture_marker(monkeypatch, tmp_path, argv, populated):
    """Run main() with subprocess stubbed; return the -m expression it built."""
    fake_tests = tmp_path / "tests"
    fake_tests.mkdir()
    (fake_tests / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    seen = []

    def _fake_run(cmd, *a, **k):
        seen.append(list(cmd))
        return None

    # BOTH, and _testpaths is the load-bearing one (). TESTS_DIR
    # stopped being the collection root when the three-testpaths resolver
    # landed () -- it is now only _testpaths()'s last-resort
    # fallback -- so patching it alone left main() collecting the REAL 1,376
    # files. That was merely wasteful until the argv-budget refusal landed
    # (): one chunk of 1,376 real paths is a 72,588-char argv
    # against a 28,000 budget, so main() returned 2 BEFORE spawning pytest and
    # all six tests here failed "main() never invoked pytest" -- an assertion
    # about the runner's fleet_layout decision, reddened by the size of a tree
    # it was never supposed to look at.
    monkeypatch.setattr(RFS, "TESTS_DIR", fake_tests)
    monkeypatch.setattr(RFS, "_testpaths", lambda: [fake_tests])
    # main() narrates its roots as `d.relative_to(PROJECT_ROOT)`, so a
    # collection root outside the repo raises ValueError before the chunk
    # loop. Re-root at tmp_path -- the only other PROJECT_ROOT readers here
    # are the two git probes, and both fail open under the stub above.
    monkeypatch.setattr(RFS, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(RFS, "_populated_agents", lambda *a, **k: populated)
    monkeypatch.setattr(RFS.subprocess, "run", _fake_run)

    RFS.main(["--out", str(out), "--chunks", "1"] + argv)

    # main() also shells out to `git rev-parse HEAD` for the tree-move check
    # BEFORE the chunk loop, so seen[0] is git, not pytest. Select by content.
    # AND SKIP THE @argfile CAPABILITY PROBE, which is ALSO a pytest argv and
    # ALSO precedes the loop (_pytest_expands_argfile). It is not a chunk
    # invocation and carries no marker, so including it would make these four
    # tests assert about the wrong command -- which is exactly what happened
    # when the probe landed. Identify it by its own argfile name rather than by
    # position, so a third pre-loop call cannot silently re-break the selector.
    pytest_cmds = [c for c in seen
                   if "pytest" in c
                   and not any("argfile-probe" in str(tok) for tok in c)]
    assert pytest_cmds, "main() never invoked pytest (saw: %r)" % (seen,)
    cmd = pytest_cmds[0]
    dash_m = [i for i, tok in enumerate(cmd) if tok == "-m"]
    # "-m pytest" is the interpreter's module flag, not a marker selector.
    marker_idx = [i for i in dash_m if cmd[i + 1] != "pytest"]
    return cmd, marker_idx


def test_single_agent_box_ands_both_clauses_into_ONE_marker_flag(monkeypatch, tmp_path):
    """The load-bearing test. Two -m flags would silently drop the first."""
    cmd, marker_idx = _capture_marker(
        monkeypatch, tmp_path, [], (["omni"], None))

    assert len(marker_idx) == 1, (
        "exactly ONE marker -m may be emitted; pytest keeps only the last -m and "
        "silently discards earlier ones, so a second flag would repeal the "
        "daemon_integration exclusion (Live-Daemon Exception)")
    expr = cmd[marker_idx[0] + 1]
    assert "not daemon_integration" in expr
    assert "not fleet_layout" in expr
    assert " and " in expr, "clauses must be ANDed inside one expression"


def test_fleet_box_does_not_narrow_the_suite(monkeypatch, tmp_path):
    """auto on a real fleet must leave fleet_layout tests RUNNING.

    A false "fleet" costs some red tests on a single-agent box; a false
    "single-agent" silently deletes coverage everywhere else. This asserts the
    safe direction is the default on the boxes where the suite is trusted.
    """
    cmd, marker_idx = _capture_marker(
        monkeypatch, tmp_path, [], (["alpha", "bravo", "zeta"], None))

    expr = cmd[marker_idx[0] + 1]
    assert "not daemon_integration" in expr
    assert "fleet_layout" not in expr


def test_undeterminable_roster_does_not_narrow_the_suite(monkeypatch, tmp_path):
    """UNKNOWN must fail toward running MORE tests, never fewer."""
    cmd, marker_idx = _capture_marker(
        monkeypatch, tmp_path, [], (None, "agents root unresolved (boom)"))

    expr = cmd[marker_idx[0] + 1]
    assert "fleet_layout" not in expr, (
        "an undeterminable roster must not be read as a single-agent verdict")


def test_force_exclude_overrides_a_fleet_reading(monkeypatch, tmp_path):
    cmd, marker_idx = _capture_marker(
        monkeypatch, tmp_path, ["--fleet-layout", "exclude"],
        (["alpha", "bravo", "zeta"], None))

    assert "not fleet_layout" in cmd[marker_idx[0] + 1]


def test_force_include_overrides_a_single_agent_reading(monkeypatch, tmp_path):
    cmd, marker_idx = _capture_marker(
        monkeypatch, tmp_path, ["--fleet-layout", "include"], (["omni"], None))

    assert "fleet_layout" not in cmd[marker_idx[0] + 1]


def test_decision_is_always_narrated(monkeypatch, tmp_path, capsys):
    """A runner that narrows what it runs must say so (guard-1760).

    The mind_api gap hid for five weeks because the runner reported what it RAN
    and never what it declined to look for.
    """
    _capture_marker(monkeypatch, tmp_path, [], (["omni"], None))
    out = capsys.readouterr().out

    assert "fleet_layout excluded" in out
    assert "omni" in out, "the evidence behind the verdict must be printed too"
