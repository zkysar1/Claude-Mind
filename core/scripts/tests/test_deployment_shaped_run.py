"""deployment-shaped-run.py: the reshape, the failure classifier and the seeded control ().

The control is two-sided on purpose. The probe tests must be GREEN in a dev-like tree and
RED, each for its own reason, in a reshaped one: a probe that is red everywhere proves
nothing, and one that is green in the shape means the harness is blind to that factor.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deployment-shaped-run.py"
_spec = importlib.util.spec_from_file_location("deployment_shaped_run", SCRIPT)
dsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dsr)

DEV_AGENT, DEV_GOAL = "alpha", "g-115-1"


def _dev_tree(root):
    """A linked-worktree look-alike that holds every dev-origin fact the probes assert on."""
    root.mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (root / "agents" / DEV_AGENT).mkdir(parents=True)
    (root / ".gitignore").write_text(
        "a\ncore/scripts/.platform-memo.sh\n# Same memo as core/scripts/.platform-memo.sh, but it can\n"
        "/.platform-memo.sh\n# Same remedy as /.platform-memo.sh above.\nb\n", encoding="utf-8")
    (root / "core" / "scripts" / "tests").mkdir(parents=True)
    world, meta = root / "devworld", root / "devmeta"
    world.mkdir()
    meta.mkdir()
    (world / "aspirations.jsonl").write_text('{"id": "%s"}\n' % DEV_GOAL, encoding="utf-8")
    strategy = meta / "goal-selection-strategy.yaml"
    strategy.write_text("weights:\n  per_goal_saturation: 0.8\n  other: 1\n", encoding="utf-8")
    (root / "agents" / DEV_AGENT / "local-paths.conf").write_text(
        "WORLD_PATH=%s\nMETA_PATH=%s\n" % (world, meta), encoding="utf-8")
    return strategy


def _probe_verdicts(tree):
    probe = tree / dsr.PROBE_REL
    probe.write_text(dsr.probe_source(DEV_AGENT, DEV_GOAL), encoding="utf-8")
    xml = tree / "probe.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", "-q",
                    "--junitxml=%s" % xml, dsr.PROBE_REL], cwd=tree, capture_output=True, text=True)
    return dsr.summarize(xml)["probes"]


def test_reshape_applies_every_factor(tmp_path):
    wt = tmp_path / "wt"
    strategy = _dev_tree(wt)
    assert dsr.reshape(wt, tmp_path / "skel", strategy) == list(dsr.FACTORS)
    assert not (wt / "agents" / DEV_AGENT).exists()
    conf = (wt / "agents" / dsr.HUSK / "local-paths.conf").read_text(encoding="utf-8")
    assert str(tmp_path / "skel" / "world") in conf and str(tmp_path / "skel" / "meta") in conf
    ignore = (wt / ".gitignore").read_text(encoding="utf-8")
    assert not any(dsr.is_memo_ignore(ln) for ln in ignore.splitlines())
    assert "# Same remedy as /.platform-memo.sh above." in ignore and "a\n" in ignore and "b\n" in ignore
    compat = tmp_path / "skel" / "world" / "config" / "compatibility.yaml"
    assert compat.read_text(encoding="utf-8") == "self_role: downstream\n"
    meta = (tmp_path / "skel" / "meta" / "goal-selection-strategy.yaml").read_text(encoding="utf-8")
    assert "per_goal_saturation" not in meta and "other: 1" in meta


def test_reshape_refuses_a_main_checkout(tmp_path):
    wt = tmp_path / "wt"
    strategy = _dev_tree(wt)
    (wt / ".git").unlink()
    (wt / ".git").mkdir()
    with pytest.raises(SystemExit):
        dsr.reshape(wt, tmp_path / "skel", strategy)
    assert (wt / "agents" / DEV_AGENT).is_dir()


def test_summarize_classifies_failures_and_reads_probe_verdicts(tmp_path):
    probe = "core.scripts.tests." + dsr.PROBE_NAME
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="core.scripts.tests.test_promote" name="t1">'
        '<failure message="assert 1 == 0">promote: working tree is dirty</failure></testcase>'
        '<testcase classname="core.scripts.tests.test_promote" name="t2"/>'
        '<testcase classname="core.scripts.tests.test_promote" name="t4"><skipped message="why"/></testcase>'
        '<testcase classname="core.scripts.tests.test_notify_dispatch.TestX" name="t3">'
        '<failure message="WARN agents/alpha/local-paths.conf"/></testcase>'
        '<testcase classname="%(p)s" name="test_probe_agents"><failure message="PROBE-FACTOR:agents"/></testcase>'
        '<testcase classname="%(p)s" name="test_probe_gitignore"/>'
        '<testcase classname="%(p)s" name="test_probe_world"><failure message="FileNotFoundError"/></testcase>'
        '<testcase classname="%(p)s" name="test_probe_meta"><failure message="PROBE-FACTOR:meta"/></testcase>'
        '</testsuite></testsuites>' % {"p": probe}, encoding="utf-8")
    res = dsr.summarize(xml)
    assert res["files"] == {"test_promote.py": {"tests": 3, "red": 1, "skipped": 1},
                            "test_notify_dispatch.py": {"tests": 1, "red": 1, "skipped": 0}}
    assert res["classes"] == {"dirty-tree": 1, "agent-conf": 1}
    assert res["probes"] == {"agents": "red", "gitignore": "green", "world": "broken", "meta": "red"}
    assert dsr.control_verdict(res["probes"]) == (False, {"gitignore": "green", "world": "broken"})
    assert dsr.control_verdict({}) == (False, {f: "missing" for f in dsr.FACTORS})
    assert dsr.control_verdict({f: "red" for f in dsr.FACTORS}) == (True, {})


def test_stop_daemons_stops_only_a_process_that_carries_this_runtime_dir(tmp_path):
    """A pid file alone is never trusted: the process must be VERIFIABLE as carrying this RUNTIME_DIR.

    In a PID namespace without a mounted /proc (the `unshare --fork --pid --net` launch shape,
    g-358-244 rerun-3) a child's /proc/<pid>/environ does not exist from the test's view, so the
    harness's only safe move is to refuse (kill nothing it cannot verify) — that refusal IS the
    contract, and this test pins it: kill the verified owner, kill no stranger, and when
    unverifiable, kill neither."""
    rt = tmp_path / "rt"
    rt.mkdir()
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    mine = subprocess.Popen(sleeper, env={**os.environ, "RUNTIME_DIR": str(rt)})
    other = subprocess.Popen(sleeper, env={k: v for k, v in os.environ.items() if k != "RUNTIME_DIR"})
    try:
        (rt / "daemon.pid").write_text(str(other.pid), encoding="utf-8")
        assert dsr.stop_daemons(rt) == [] and other.poll() is None, "a stale pid file must not kill a stranger"
        (rt / "daemon.pid").write_text(str(mine.pid), encoding="utf-8")
        blind = not (Path("/proc") / str(mine.pid) / "environ").exists()
        assert dsr.stop_daemons(rt) == ([] if blind else [mine.pid]), (
            "blind /proc: unverifiable pid must be refused, not killed" if blind
            else "verifiable owner with this RUNTIME_DIR must be stopped")
        if blind:
            assert mine.poll() is None, "the refused process must still be alive"
        else:
            assert mine.wait(timeout=10) is not None
    finally:
        for proc in (mine, other):
            proc.kill()
            proc.wait()


def test_parse_invisible_reads_the_runner_verdicts():
    text = ("PASS test_a.py\nFAIL(rc=1) test_b.py\nQUARANTINED test_c.py — open goal\n"
            "PASS test_d.sh (shell)\nFAIL(rc=3) test_e.sh (shell)\n"
            "SKIP test_f.sh (shell) — SKIP: needs a reachable daemon\n"
            "invisible-suites: 2/5 files passed, 1 quarantined\n")
    assert dsr.parse_invisible(text) == {"test_a.py": "PASS", "test_b.py": "RED", "test_c.py": "QUARANTINED",
                                         "test_d.sh": "PASS", "test_e.sh": "RED", "test_f.sh": "SKIP"}


def test_probes_are_green_in_a_dev_tree(tmp_path):
    _dev_tree(tmp_path / "dev")
    assert _probe_verdicts(tmp_path / "dev") == {f: "green" for f in dsr.FACTORS}


def test_probes_are_red_for_their_own_reason_in_a_reshaped_tree(tmp_path):
    wt = tmp_path / "wt"
    strategy = _dev_tree(wt)
    dsr.reshape(wt, tmp_path / "skel", strategy)
    verdicts = _probe_verdicts(wt)
    assert verdicts == {f: "red" for f in dsr.FACTORS}
    assert dsr.control_verdict(verdicts) == (True, {})


def test_two_leg_split_keeps_the_probe_out_of_the_directory_collection(tmp_path):
    """Pin the probe-leg / main-leg split (measured 2026-10-08 zc-10).

    Pre-fix the probe was an explicit arg NEXT TO the directory targets. On this box's
    pytest 7.4.4 an explicit file path and a directory that contains it are TWO collection
    roots, so the probe ran twice (8 items vs 4, same args); the late pass read a
    suite-dirtied tree and flipped agents=green over an otherwise 4/4-red control. The
    fix: probe leg = probe path alone (exactly 4 cases); main leg = directories alone
    (0 probe cases, the probe name matches no python_files pattern).
    """
    def _probe_items(args, tree):
        r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                            "--no-header", "-q", "--collect-only"] + args,
                           cwd=tree, capture_output=True, text=True)
        return [ln for ln in r.stdout.splitlines()
                if dsr.PROBE_NAME in ln and ("::" in ln or ": 4" in ln or ": 8" in ln)]

    wt = tmp_path / "wt"
    strategy = _dev_tree(wt)
    dsr.reshape(wt, tmp_path / "skel", strategy)
    (wt / dsr.PROBE_REL).write_text(dsr.probe_source(DEV_AGENT, DEV_GOAL), encoding="utf-8")
    # probe leg: the explicit probe path is the ONLY arg
    assert len(_probe_items([dsr.PROBE_REL], wt)) == 4
    # main leg: directories only; the probe must not appear in the collection at all
    assert _probe_items(["core/scripts/tests", "core/tests"], wt) == []
