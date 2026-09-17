""": tree.py's post-remove sweep must surface the repair's REFUSED reason.

Drives the real subprocess path end to end: tree.py's _post_remove_sweep_dangling
spawns learning-routing-repair.py --apply, a per-agent read falls back (forced by
an unknown storage backend), and the repair refuses with rc=3. The sweep used to
print only the first 200 chars of stderr, which cut the reason off.

Isolation is the load-bearing part, not decoration. agents_root() is PROJECT_ROOT
based and has no env override, so a run against the live tree would read (and,
if the refusal ever regressed, NULL refs in) real agent experience files. The
test therefore builds a throwaway project root: real copies of the modules that
derive paths from __file__ (plus the two under test), symlinks for everything
else, a fake agent, and an empty-tree world. Every write the child could make
lands under tmp_path.
"""
import importlib.util
import json
import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent  # core/scripts
COPIED = ("_paths.py", "tree.py", "learning-routing-repair.py", "learning-routing-audit.py")


def _build_project(tmp_path):
    scripts = tmp_path / "proj" / "core" / "scripts"
    scripts.mkdir(parents=True)
    for src in SRC.glob("*.py"):
        dst = scripts / src.name
        if src.name in COPIED:
            shutil.copy2(src, dst)  # copy follows a symlinked source
        else:
            try:
                dst.symlink_to(src.resolve())
            except OSError:  # Windows without symlink privilege ()
                shutil.copy2(src, dst)
    agent = tmp_path / "proj" / "agents" / "fake-g358146"
    agent.mkdir(parents=True)
    rec = {"id": "exp-g358146-fixture", "type": "goal_execution",
           "tree_nodes_related": ["no-such-node-g358146"]}
    (agent / "experience.jsonl").write_text(json.dumps(rec) + "\n")
    world = tmp_path / "world"
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text("nodes: {}\n")
    # The audit reads the agent corpus only for a world some agent's conf
    # declares (world_owns_agent_corpus); without this every run reads CLEAN.
    (agent / "local-paths.conf").write_text("WORLD_PATH={}\n".format(world))
    return scripts, agent, world


def test_sweep_prints_the_repair_refusal_through_the_real_subprocess(
        tmp_path, monkeypatch, capsys):
    scripts, agent, world = _build_project(tmp_path)
    monkeypatch.setenv("MIND_WORLD", str(world))
    monkeypatch.setenv("STORAGE_BACKEND", "bogus-g358146")
    monkeypatch.delenv("MIND_AGENT_DIR", raising=False)

    spec = importlib.util.spec_from_file_location("tree_g358146", scripts / "tree.py")
    tree = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tree)
    tree._post_remove_sweep_dangling(["no-such-node-g358146"])

    err = capsys.readouterr().err
    line = next((ln for ln in err.splitlines() if "post-remove sweep" in ln), "")
    assert "(exit 3)" in line, err
    assert "REFUSED --apply, nothing written" in line, err
    # The fake agent's path is the TAIL of the refusal sentence: a head slice
    # cuts it, and seeing it proves the child read the isolated agents root.
    assert str(agent / "experience.jsonl") in line, err
    assert not (world / ".history").exists()
    assert "no-such-node-g358146" in (agent / "experience.jsonl").read_text()


def test_sweep_prints_the_tail_of_a_crashing_repair(tmp_path, capsys):
    # : a child that dies with a traceback puts its cause on the LAST
    # line, behind more than 200 chars of preamble; the sweep must show it.
    scripts, _agent, _world = _build_project(tmp_path)
    repair = scripts / "learning-routing-repair.py"
    repair.write_text(
        "import sys\n"
        "print('[note] ' + 'x' * 300, file=sys.stderr)\n"
        "raise RuntimeError('g358158-crash-marker')\n")

    spec = importlib.util.spec_from_file_location("tree_g358158", scripts / "tree.py")
    tree = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tree)
    tree._post_remove_sweep_dangling(["no-such-node-g358158"])

    err = capsys.readouterr().err
    line = next((ln for ln in err.splitlines() if "post-remove sweep" in ln), "")
    assert "(exit 1)" in line, err
    assert "RuntimeError: g358158-crash-marker" in err, err
