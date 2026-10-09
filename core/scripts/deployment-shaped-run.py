#!/usr/bin/env python3
"""Run framework tests in a checkout shaped like a downstream deployment ().

WHY: the dev origin's suite is green where a downstream deployment is red, because
tests read what only the dev origin has: its world's goal ids, its capability
catalog, its fleet's agent dirs, its meta strategy keys, and the checkout's own
.gitignore. A tag cut on a dev-only green ships those reds to the first adopter
(the v2.12.95 adopt rolled back rc 3 on 95 new red tests). This harness builds the
deployment shape FIRST, runs the named tests there, and refuses to call itself
green unless a SEEDED POSITIVE CONTROL proves it can see a deployment: four probe
tests, one per factor, each asserting a fact only the dev origin has. Each must be
RED, with its own PROBE-FACTOR marker in the failure text (red for another reason
is BROKEN, green is BLIND).

THE SHAPE (one reshape step per factor):
  agents     every tracked agent dir is removed; one husk agent holds local-paths.conf
  gitignore  the .gitignore loses its .platform-memo.sh lines (a deployment-owned
             file that predates them)
  world      a valid world holding config/compatibility.yaml and no goals
  meta       the dev strategy file minus weights.per_goal_saturation
  runtime    STORAGE_BACKEND=local, RUNTIME_DIR scratch, no daemon and RT_NO_AUTOSPAWN=1 (a
             daemon-quiesced verify: a daemon-backed wrapper fails "daemon unreachable")

USAGE: py -3 core/scripts/deployment-shaped-run.py [--ref REF] [--out DIR] [--keep]
           [--timeout SEC] [--json] <test files or dirs> [-- <pytest options>]
Runs against a COMMITTED ref (default HEAD): uncommitted edits are not in it.
Exit: 0 control ok and no red | 1 control ok, red present | 2 usage or infra | 3 control not ok.
"""
import argparse
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HUSK = "zz-harness"
# Named so pytest's directory traversal (test_*.py) does not re-collect it: the explicit arg
# runs it once, early. A second pass collected late in the session would read a suite-dirtied
# tree and flip the control (measured 2026-10-08 zc-10: the 2nd pass saw a suite-recreated
# agents/<dev-agent> dir and reported agents=green over an otherwise red 4/4 control).
PROBE_NAME = "zz_deployment_probe"
PROBE_REL = "core/scripts/tests/%s.py" % PROBE_NAME
FACTORS = ("agents", "gitignore", "world", "meta")
STRIPPED_META_KEYS = ("per_goal_saturation",)


def is_memo_ignore(line):
    """True for a .gitignore line that IGNORES the platform memo; a comment naming it is not one."""
    s = line.strip()
    return bool(s) and not s.startswith("#") and s.endswith("platform-memo.sh")


# First hit wins. Labels are the failure classes of the v2.12.95 adopt.
CLASS_SIGNATURES = (
    ("dirty-tree", re.compile(r"working tree is dirty")),
    ("dev-goal-id", re.compile(r"Could not read goal g-\d+-\d+'s live record|no Retry: line emitted"
                              r"|runnable recovery invocation|pre-fix retry line")),
    ("meta-key", re.compile(r"per_goal_saturation")),
    ("daemon", re.compile(r"daemon unreachable|REFUSING:")),
    ("catalog", re.compile(r"sole-compound recall lost|matches=\[\]")),
    ("agent-conf", re.compile(r"local-paths\.conf|agents/[a-z][a-z-]*")),
)

PROBE_SOURCE = '''"""Seeded positive control, written by deployment-shaped-run.py and never committed.
Each test asserts a fact only the dev origin has, so it must be RED in a deployment shape."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _conf(key):
    for conf in sorted((ROOT / "agents").glob("*/local-paths.conf")):
        for line in conf.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return Path(v.strip())
    raise AssertionError("PROBE-BROKEN: no %s in any agents/*/local-paths.conf" % key)


def test_probe_agents():
    assert (ROOT / "agents" / @DEV_AGENT@).is_dir(), "PROBE-FACTOR:agents"


@IS_MEMO_IGNORE@

def test_probe_gitignore():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(is_memo_ignore(ln) for ln in lines), "PROBE-FACTOR:gitignore"


def test_probe_world():
    stores = _conf("WORLD_PATH").glob("aspirations*.jsonl")
    assert any(@DEV_GOAL@ in p.read_text(encoding="utf-8", errors="ignore")
               for p in stores), "PROBE-FACTOR:world"


def test_probe_meta():
    text = (_conf("META_PATH") / "goal-selection-strategy.yaml").read_text(encoding="utf-8")
    assert "per_goal_saturation" in text, "PROBE-FACTOR:meta"
'''


def probe_source(dev_agent, dev_goal):
    # The predicate is embedded from its one definition, so probe and reshape cannot drift.
    return (PROBE_SOURCE.replace("@IS_MEMO_IGNORE@", inspect.getsource(is_memo_ignore).rstrip("\n"))
            .replace("@DEV_AGENT@", repr(dev_agent)).replace("@DEV_GOAL@", repr(dev_goal)))


def _strip_keys(node, keys):
    if isinstance(node, dict):
        for k in list(node):
            if k in keys:
                node.pop(k)
            else:
                _strip_keys(node[k], keys)
    elif isinstance(node, list):
        for v in node:
            _strip_keys(v, keys)


def reshape(wt, skel, strategy_src):
    """Turn a linked worktree into a deployment shape. Returns the factors applied."""
    import yaml
    wt, skel = Path(wt), Path(skel)
    if not (wt / ".git").is_file():
        raise SystemExit("refusing to reshape %s: not a linked worktree (.git is not a file)" % wt)
    agents = wt / "agents"
    agents.mkdir(exist_ok=True)
    for d in agents.iterdir():
        shutil.rmtree(d) if d.is_dir() else d.unlink()
    world, meta, work = skel / "world", skel / "meta", skel / "work"
    (world / "config").mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    (world / "config" / "compatibility.yaml").write_text("self_role: downstream\n", encoding="utf-8")
    data = yaml.safe_load(Path(strategy_src).read_text(encoding="utf-8"))
    _strip_keys(data, STRIPPED_META_KEYS)
    (meta / "goal-selection-strategy.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    # skill-discovery.py REQUIRES meta/skill-discovery-strategy.yaml (sys.exit 3); init-meta.sh
    # seeds every deployment from core/config/skill-discovery-strategy.yaml ( parity).
    # The shape's meta must carry it: absent, the skill-discovery tests sys.exit(3) in a
    # subprocess and read as deployment reds that no downstream deployment would have (measured
    # 2026-10-08 zc-10: 7 collection errors in test_skill_discovery_verdict_suppression).
    skel_cfg = wt / "core" / "config" / "skill-discovery-strategy.yaml"
    if skel_cfg.is_file():
        (meta / "skill-discovery-strategy.yaml").write_text(skel_cfg.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        print("WARNING: %s missing from the ref; the meta shape lacks it and skill-discovery "
              "tests will read as reds" % skel_cfg, file=sys.stderr)
    (agents / HUSK).mkdir()
    (agents / HUSK / "local-paths.conf").write_text(
        "WORLD_PATH=%s\nMETA_PATH=%s\nAGENT_WRITE_PATH=%s\n" % (world, meta, work), encoding="utf-8")
    gi = wt / ".gitignore"
    kept = [ln for ln in gi.read_text(encoding="utf-8").splitlines(keepends=True) if not is_memo_ignore(ln)]
    gi.write_text("".join(kept), encoding="utf-8")
    return list(FACTORS)


def _file_of(classname):
    m = re.search(r"\btest_\w+", classname or "")
    return (m.group(0) + ".py") if m else (classname or "?")


def summarize(xml_path):
    """Per-file counts, failure classes, and the seeded-probe verdicts from a junit file."""
    files, classes, probes = {}, {}, {}
    for tc in ET.parse(xml_path).getroot().iter("testcase"):
        bad = tc.find("failure")
        if bad is None:
            bad = tc.find("error")
        text = ((bad.get("message") or "") + (bad.text or "")) if bad is not None else ""
        # Match the probe by its module name (last dot-segment of the classname), not _file_of's
        # test_\w+ regex: the probe file is deliberately not named test_* (see PROBE_NAME), so
        # its classname carries no test_ stem. Path() would not work: it splits on "/", not ".".
        if (tc.get("classname") or "").rsplit(".", 1)[-1] == PROBE_NAME:
            factor = (tc.get("name") or "").replace("test_probe_", "")
            if bad is None:
                probes[factor] = "green"
            else:
                probes[factor] = "red" if ("PROBE-FACTOR:" + factor) in text else "broken"
            continue
        fname = _file_of(tc.get("classname"))
        row = files.setdefault(fname, {"tests": 0, "red": 0, "skipped": 0})
        row["tests"] += 1
        if tc.find("skipped") is not None:
            row["skipped"] += 1
        if bad is not None:
            row["red"] += 1
            label = next((lab for lab, rx in CLASS_SIGNATURES if rx.search(text)), "other")
            classes[label] = classes.get(label, 0) + 1
    return {"files": files, "classes": classes, "probes": probes}


def control_verdict(probes):
    """(ok, problems): ok only when every factor's probe is red for its own reason."""
    problems = {f: probes.get(f, "missing") for f in FACTORS if probes.get(f) != "red"}
    return (not problems), problems


def stop_daemons(rt_dir):
    """Stop any daemon a test spawned into this run's private RUNTIME_DIR; returns the pids stopped.

    A pid file alone is not trusted: the process must carry RUNTIME_DIR=<rt_dir> in its environment.
    """
    stopped = []
    rt_dir = str(rt_dir)
    for pid_file in Path(rt_dir).glob("daemon.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            env = Path("/proc/%d/environ" % pid).read_bytes().split(b"\0")
            if ("RUNTIME_DIR=%s" % rt_dir).encode() in env:
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
        except (OSError, ValueError):
            continue
    return stopped


def parse_invisible(text):
    """Per-file verdicts from run-invisible-suites.sh output: PASS | RED | QUARANTINED | SKIP."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^(PASS|SKIP|FAIL\(rc=\d+\)|QUARANTINED)\s+(\S+)", ln)
        if m:
            out[m.group(2)] = {"PASS": "PASS", "QUARANTINED": "QUARANTINED",
                               "SKIP": "SKIP"}.get(m.group(1), "RED")
    return out


def run_invisible(wt, env, files, log, timeout):
    """Run files pytest collects zero tests from (main()-style .py, any .sh) through the repo's own runner."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _runtime_bash import bash_cmd
    cmd = bash_cmd(Path(wt) / "core" / "scripts" / "tests" / "run-invisible-suites.sh", "--files", *files)
    with open(log, "w", encoding="utf-8") as fh:
        try:
            subprocess.run(cmd, cwd=wt, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {f: "TIMEOUT" for f in files}
    seen = parse_invisible(Path(log).read_text(encoding="utf-8", errors="replace"))
    return {f: seen.get(Path(f).name, "NO-VERDICT") for f in files}


def _dev_context(ref, repo):
    """Dev-origin facts the probes assert on: a tracked agent dir, a goal id, the strategy file."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _paths import META_DIR, WORLD_DIR
    tree = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", ref, "agents/"],
                          capture_output=True, text=True, check=True).stdout.split()
    if not tree:
        raise SystemExit("ref %s tracks no agents/ dir; the agents probe has nothing to assert" % ref)
    goal = re.search(r"g-\d+-\d+", (Path(WORLD_DIR) / "aspirations.jsonl").read_text(encoding="utf-8"))
    if not goal:
        raise SystemExit("no goal id found in the dev world's aspirations.jsonl")
    return Path(tree[0]).name, goal.group(0), Path(META_DIR) / "goal-selection-strategy.yaml"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("files", nargs="*")
    a, rest = ap.parse_known_args(argv)
    targets = a.files + [r for r in rest if r != "--"]
    if not targets:
        ap.error("name the test files or a directory to run")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _paths import AGENT_NAME, PROJECT_ROOT, agent_session_dir
    repo = Path(PROJECT_ROOT)
    out = a.out or (agent_session_dir(AGENT_NAME, os.environ["MIND_SID"]) / "scratch"
                    / ("deployment-shaped-%d" % time.time()) if os.environ.get("MIND_SID") else None)
    if not out:
        ap.error("no MIND_SID in the environment: pass --out")
    out = Path(out).resolve()
    wt, skel = out / "wt", out / "skel"
    (out / "rt").mkdir(parents=True, exist_ok=True)
    dev_agent, dev_goal, strategy = _dev_context(a.ref, repo)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), a.ref],
                   check=True, capture_output=True)
    try:
        reshape(wt, skel, strategy)
        (wt / PROBE_REL).write_text(probe_source(dev_agent, dev_goal), encoding="utf-8")
        env = dict(os.environ, MIND_AGENT=HUSK, MIND_AGENT=HUSK, STORAGE_BACKEND="local",
                   RUNTIME_DIR=str(out / "rt"), RT_NO_AUTOSPAWN="1")
        for k in ("MIND_WORLD", "MIND_META"):
            env.pop(k, None)
        # THE PROBE LEG IS ITS OWN CALL ON A PRISTINE SHAPED TREE (measured 2026-10-08 zc-10):
        # passing PROBE_REL alongside the directory targets double-collected the probe in the
        # shaped worktree (collect-only: 8 items vs 4 unshaped, same args) and the second pass,
        # late in the session, read a suite-dirtied tree and flipped agents=green over an
        # otherwise red 4/4 control (rerun 2, junit probe x8). The probe must run ALONE, BEFORE
        # any suite test dirties the shape, or the control is unmeasurable.
        pxml, plog = out / "probe-leg.xml", out / "probe-leg.log"
        cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", "-rf",
               "--junitxml=%s" % pxml, PROBE_REL]
        with open(plog, "w", encoding="utf-8") as fh:
            try:
                subprocess.run(cmd, cwd=wt, env=env, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=a.timeout)
            except subprocess.TimeoutExpired:
                print("PROBE LEG TIMEOUT after %ds; log %s" % (a.timeout, plog))
                return 2
        if not pxml.is_file():
            print("probe-leg pytest wrote no junit file; read %s" % plog)
            return 2
        pres = summarize(pxml)
        pcount = sum(1 for tc in ET.parse(pxml).getroot().iter("testcase")
                     if (tc.get("classname") or "").rsplit(".", 1)[-1] == PROBE_NAME)
        # Count guard: !=4 probe cases means the leg is malformed (double collection, a broken
        # rewrite, a collection error eating a case) -- the control is unmeasurable, exit 2 void.
        if pcount != len(FACTORS):
            print("PROBE LEG MALFORMED: %d probe cases, expected %d; read %s"
                  % (pcount, len(FACTORS), plog))
            return 2
        ok, problems = control_verdict(pres["probes"])
        res = {"probes": pres["probes"]}
        if not ok:
            # A failed control is a verdict about the shape, not an infra error: report it and
            # VOID the run (exit 3, docstring contract) without spending the main leg.
            res.update(control_ok=False, control_problems=problems, ref=a.ref, log=str(plog))
            if a.json:
                print(json.dumps(res, indent=1))
            else:
                print("deployment-shaped run: ref=%s factors=%s" % (a.ref, ",".join(FACTORS)))
                print("CONTROL: NOT OK %s (green = BLIND, broken = red for another reason)"
                      % problems)
                print("PROBE LEG VOIDED THE RUN; probe log %s" % plog)
            return 3
        res["control_ok"] = True
        res["control_problems"] = {}
        res["probe_log"] = str(plog)
        xml, log = out / "run.xml", out / "run.log"
        # The main leg carries NO probe (its leg already measured the control on a pristine
        # shape): shell tests are not pytest targets either, they reach run-invisible-suites.
        cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-header", "-rf",
               "--junitxml=%s" % xml] + [t for t in targets if not t.endswith(".sh")]
        with open(log, "w", encoding="utf-8") as fh:
            try:
                subprocess.run(cmd, cwd=wt, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=a.timeout)
            except subprocess.TimeoutExpired:
                print("TIMEOUT after %ds; log %s" % (a.timeout, log))
                return 2
        if not xml.is_file():
            print("pytest wrote no junit file; read %s" % log)
            return 2
        mainres = summarize(xml)
        mainres.pop("probes", None)  # probe verdicts stay the probe-leg's; run.xml carries no probe cases
        res.update(mainres)
        named = [t for t in targets if t.endswith((".py", ".sh"))]
        pending = [t for t in named if Path(t).name not in res["files"] and (wt / t).is_file()]
        res["invisible"] = run_invisible(wt, env, pending, out / "invisible.log", a.timeout) if pending else {}
        res["missing"] = [t for t in named if not (wt / t).is_file()]
    finally:
        stopped = stop_daemons(out / "rt")
        if not a.keep:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
    # The control verdict is the probe-leg's, decided BEFORE the main leg ran. It is not
    # re-derived from run.xml: the main leg carries no probe cases, so a re-derive would read
    # the control as 4/4 missing and void a good run (the pre-fix code did exactly this).
    res["log"] = str(log)
    res["daemons_stopped"] = stopped
    ok, problems = res["control_ok"], res["control_problems"]
    red = sum(r["red"] for r in res["files"].values()) + sum(v not in ("PASS", "QUARANTINED", "SKIP")
                                                              for v in res["invisible"].values())
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print("deployment-shaped run: ref=%s factors=%s" % (a.ref, ",".join(FACTORS)))
        print("CONTROL: " + ("%d/%d seeded probes red" % (len(FACTORS), len(FACTORS)) if ok else
                             "NOT OK %s (green = BLIND, broken = red for another reason)" % problems))
        for f, r in sorted(res["files"].items()):
            print("  %-60s %5d tests %4d red %4d skipped" % (f, r["tests"], r["red"], r["skipped"]))
        for f, v in sorted(res["invisible"].items()):
            print("  %-60s invisible to pytest, run by run-invisible-suites: %s" % (Path(f).name, v))
        for f in res["missing"]:
            print("  %-60s NOT IN THIS REF" % Path(f).name)
        if stopped:
            print("DAEMONS: stopped %d spawned into the scratch runtime dir (pids %s)"
                  % (len(stopped), ",".join(map(str, stopped))))
        print("CLASSES: " + (" ".join("%s=%d" % kv for kv in sorted(res["classes"].items())) or "none"))
        print("TOTAL: %d red of %d tests (%d skipped, NOT run) in %d files; log %s" % (
            red, sum(r["tests"] for r in res["files"].values()),
            sum(r["skipped"] for r in res["files"].values()), len(res["files"]), log))
    return 3 if not ok else (1 if red else 0)


if __name__ == "__main__":
    sys.exit(main())
