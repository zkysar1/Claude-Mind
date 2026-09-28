"""Tests for birth-contract-check.py and its init-mind.sh wiring ().

The check reports which pre-landed (headless) birth-contract elements a mind
lacks. Three things are pinned here:

1. Each element, one at a time: a fully provided birth is the positive control,
   and every missing-element case asserts that ONLY its own element flips, so
   the control stays green under each mutant (guard-4166).
2. Expectations read from the other component at runtime (guard-1220): the hook
   slots come from the real core/config/templates, the curriculum seed from the
   real core/config/curriculum.yaml, the curriculum predicate from curriculum.py
   cmd_status, the agent-state reading from session-state-get.sh, and the
   element list from the convention's own table.
3. The wiring, run for real: the production init-mind.sh inside a sandbox whose
   neighbours are stubs, so the RUNNING gate, the call's literal argument shape
   (guard-920) and the never-fail-init property are measured, not grepped.
4. The NAMED agent's world (g-377-38): two agents on two worlds, run through the
   real _paths pair in both call shapes, with the binding planted in the child
   (guard-2337).
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPTS.parents[1]
SCRIPT = SCRIPTS / "birth-contract-check.py"
INIT_MIND = SCRIPTS / "init-mind.sh"
TEMPLATES = PROJECT_ROOT / "core" / "config" / "templates"
CONVENTION = PROJECT_ROOT / "core" / "config" / "conventions" / "session-state.md"
CURRICULUM_CONFIG = PROJECT_ROOT / "core" / "config" / "curriculum.yaml"

# core/scripts on path (conftest also inserts this; explicit for `py -3` direct runs).
sys.path.insert(0, str(SCRIPTS))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

_spec = importlib.util.spec_from_file_location("birth_contract_check", SCRIPT)
bcc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcc)

SLOTS = sorted(p.name[:-len("-default.md")] for p in TEMPLATES.glob("*-default.md"))
ABSENT_AGENT = "zz-birth-contract-test-absent-agent"


def _born(tmp_path, state="IDLE"):
    """A fully provided pre-landed birth. Returns (agent_dir, state_dir, world_dir)."""
    agent = tmp_path / "agents" / "a1"
    state_dir = agent / "session"
    world = tmp_path / "world"
    state_dir.mkdir(parents=True)
    (world / "conventions").mkdir(parents=True)
    (agent / ".initialized").touch()
    (state_dir / "agent-state").write_text(state + "\n")
    (agent / "self.md").write_text("---\nname: a1\n---\n# Who I am\nA mind.\n")
    (agent / "curriculum.yaml").write_text(yaml.safe_dump({
        "current_stage": "cur-01", "stage_history": [],
        "stages": [{"id": "cur-01", "name": "First stage"}]}))
    (world / "program.md").write_text("# The Program\nLearn the place.\n")
    for slot in SLOTS:
        (world / "conventions" / f"{slot}.md").write_text(f"# {slot}\n## Step 1\nDo it.\n")
    return agent, state_dir, world


def _missing(elements):
    return {e["id"]: e["reason"] for e in elements if not e["present"]}


def test_templates_yield_the_canonical_slots():
    # Positive control on the derivation: an empty glob would make every
    # hook-slot case below vacuous.
    assert {"pre-execution", "post-execution"} <= set(SLOTS)


@pytest.mark.parametrize("state", ["IDLE", "RUNNING"])
def test_fully_provided_birth_is_all_present(tmp_path, state):
    agent, state_dir, world = _born(tmp_path, state)
    elements = bcc.check(agent, state_dir, world, TEMPLATES)
    assert _missing(elements) == {}
    assert [e["id"] for e in elements] == (
        list(bcc.ELEMENT_IDS) + [bcc.HOOK_SLOT_PREFIX + s for s in SLOTS])


def _curriculum_seed(agent, state_dir, world):
    # The seed init-agent.sh extracts, read from the real framework config.
    seed = yaml.safe_load(CURRICULUM_CONFIG.read_text())["initial_state"]
    (agent / "curriculum.yaml").write_text(yaml.safe_dump(seed))


def _set(relpath_fn, content):
    def mutate(agent, state_dir, world):
        target = relpath_fn(agent, state_dir, world)
        if content is None:
            target.unlink()
        elif isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    return mutate


_MARKER = lambda a, s, w: a / ".initialized"
_STATE = lambda a, s, w: s / "agent-state"
_SELF = lambda a, s, w: a / "self.md"
_PROGRAM = lambda a, s, w: w / "program.md"
_CURRICULUM = lambda a, s, w: a / "curriculum.yaml"

CASES = [
    ("agent-initialized", _set(_MARKER, None), "marker absent"),
    ("agent-state", _set(_STATE, "PAUSED\n"), "not IDLE, RUNNING or UNINITIALIZED"),
    ("agent-state", _set(_STATE, b""), "not IDLE, RUNNING or UNINITIALIZED"),
    ("self-md", _set(_SELF, None), "file absent"),
    ("self-md", _set(_SELF, b""), "empty"),
    ("self-md", _set(_SELF, " \n\t\n"), "empty"),
    ("program-md", _set(_PROGRAM, None), "file absent"),
    ("program-md", _set(_PROGRAM, b""), "empty"),
    ("curriculum-stage", _set(_CURRICULUM, None), "file absent"),
    ("curriculum-stage", _curriculum_seed, "no current_stage"),
    ("curriculum-stage", _set(_CURRICULUM, "current_stage: cur-09\nstages:\n- id: cur-01\n"),
     "not among stages"),
    ("curriculum-stage", _set(_CURRICULUM, "current_stage: [unclosed\n"), "unreadable"),
    ("curriculum-stage", _set(_CURRICULUM, "- cur-01\n"), "not a mapping"),
    ("curriculum-stage", _set(_CURRICULUM, "current_stage: cur-01\nstages: 5\n"), "not a list"),
]
for _slot in SLOTS:
    _slot_path = (lambda slot: lambda a, s, w: w / "conventions" / f"{slot}.md")(_slot)
    CASES.append((bcc.HOOK_SLOT_PREFIX + _slot, _set(_slot_path, None), "file absent"))
    CASES.append((bcc.HOOK_SLOT_PREFIX + _slot, _set(_slot_path, b""), "empty"))


@pytest.mark.parametrize("eid,mutate,reason_part", CASES,
                         ids=[f"{c[0]}:{c[2]}" for c in CASES])
def test_each_missing_element_flips_only_itself(tmp_path, eid, mutate, reason_part):
    agent, state_dir, world = _born(tmp_path)
    mutate(agent, state_dir, world)
    missing = _missing(bcc.check(agent, state_dir, world, TEMPLATES))
    assert set(missing) == {eid}, missing
    assert reason_part in missing[eid]


def test_crlf_agent_state_still_reads_as_present(tmp_path):
    agent, state_dir, world = _born(tmp_path)
    (state_dir / "agent-state").write_bytes(b"IDLE\r\n")
    assert _missing(bcc.check(agent, state_dir, world, TEMPLATES)) == {}


@pytest.mark.parametrize("state", [None, "UNINITIALIZED\n"], ids=["absent", "literal"])
def test_uninitialized_with_the_marker_is_the_headless_a0_resume(tmp_path, state):
    # /start Phase A-0 resumes a marked agent with no agent-state as an existing one.
    agent, state_dir, world = _born(tmp_path)
    _set(_STATE, state)(agent, state_dir, world)
    assert _missing(bcc.check(agent, state_dir, world, TEMPLATES)) == {}


def test_uninitialized_without_the_marker_is_the_interview(tmp_path):
    agent, state_dir, world = _born(tmp_path)
    (agent / ".initialized").unlink()
    (state_dir / "agent-state").unlink()
    missing = _missing(bcc.check(agent, state_dir, world, TEMPLATES))
    assert set(missing) == {"agent-initialized", "agent-state"}
    assert "interview" in missing["agent-state"]


@pytest.mark.parametrize("content", [
    None, b"IDLE\n", b"RUNNING\r\n", b"UNINITIALIZED\n", b"", b"PAUSED\n"],
    ids=["absent", "idle", "running-crlf", "literal-uninitialized", "empty", "other"])
def test_state_reading_agrees_with_session_state_get(tmp_path, content):
    # /start routes on what session-state-get.sh prints, so run the real script
    # against the same file and derive the route from ITS output.
    agent, state_dir, world = _born(tmp_path)
    _set(_STATE, content)(agent, state_dir, world)
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "session-state-get.sh", scripts / "session-state-get.sh")
    r = subprocess.run([BASH, (scripts / "session-state-get.sh").as_posix()],
                       env=dict(os.environ, MIND_AGENT=agent.name),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    printed = r.stdout.strip()
    for initialized in (True, False):
        headless = printed in ("IDLE", "RUNNING") or (printed == "UNINITIALIZED" and initialized)
        assert bcc._check_state(state_dir / "agent-state", initialized)["present"] is headless


def test_a_stat_error_is_reported_missing_not_raised(tmp_path):
    # guard-7231. pathlib re-raises stat errors other than ENOENT and ENOTDIR,
    # such as a permission-denied parent or a name too long. A 300-character
    # directory name provokes one without chmod, which root ignores.
    agent = tmp_path / ("a" * 300)
    missing = _missing(bcc.check(agent, agent / "session", tmp_path, TEMPLATES))
    assert {"agent-initialized", "agent-state", "self-md", "curriculum-stage"} <= set(missing)


def test_unreadable_element_is_reported_missing_not_skipped(tmp_path):
    # guard-7231: a directory where a file belongs cannot be read on any
    # platform, and must surface as missing with the read error.
    agent, state_dir, world = _born(tmp_path)
    (agent / "self.md").unlink()
    (agent / "self.md").mkdir()
    missing = _missing(bcc.check(agent, state_dir, world, TEMPLATES))
    assert set(missing) == {"self-md"}
    assert missing["self-md"].startswith("unreadable:")


def test_no_templates_is_reported_not_read_as_no_slots_required(tmp_path):
    agent, state_dir, world = _born(tmp_path)
    empty = tmp_path / "no-templates"
    empty.mkdir()
    missing = _missing(bcc.check(agent, state_dir, world, empty))
    assert set(missing) == {bcc.HOOK_SLOT_PREFIX + "*"}
    assert "cannot be derived" in missing[bcc.HOOK_SLOT_PREFIX + "*"]


def test_unconfigured_world_reports_every_world_element(tmp_path):
    agent, state_dir, _ = _born(tmp_path)
    missing = _missing(bcc.check(agent, state_dir, None, TEMPLATES))
    assert set(missing) == {"program-md"} | {bcc.HOOK_SLOT_PREFIX + s for s in SLOTS}
    assert all("not configured" in r for r in missing.values())


@pytest.mark.parametrize("doc", [
    {"current_stage": None, "stage_history": [], "stages": []},
    {"current_stage": "", "stages": [{"id": "cur-01"}]},
    {"current_stage": "cur-09", "stages": [{"id": "cur-01"}]},
    {"current_stage": "cur-02", "stages": [{"id": "cur-01"}, {"id": "cur-02"}]},
], ids=["seed", "blank-stage", "stage-not-listed", "configured"])
def test_curriculum_predicate_agrees_with_curriculum_cmd_status(tmp_path, monkeypatch, capsys, doc):
    import curriculum  # noqa: E402 -- conftest locks AGENT_DIR for its import-time assert
    path = tmp_path / "curriculum.yaml"
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(curriculum, "CURRICULUM_PATH", path)
    curriculum.cmd_status(None)
    status = json.loads(capsys.readouterr().out)
    configured = status.get("configured") is True and "error" not in status
    assert bcc._check_curriculum(path)["present"] is configured


def test_convention_table_lists_exactly_the_checked_elements():
    text = CONVENTION.read_text(encoding="utf-8")
    start = text.index("\n# Pre-Landed Birth Contract\n")
    section = text[start:text.index("\n# ", start + 1)]
    ids = re.findall(r"^\| `([^`]+)` \|", section, flags=re.M)
    assert ids == list(bcc.ELEMENT_IDS) + [bcc.HOOK_SLOT_PREFIX + "<slot>"]
    assert "core/scripts/birth-contract-check.py" in section
    assert "Pre-Landed Birth Contract" in bcc.CONTRACT_REF


def _cli(args, **env_overrides):
    env = dict(os.environ, STORAGE_BACKEND="local")
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def test_cli_in_the_production_call_shape_reports_each_missing_element(tmp_path):
    # init-mind.sh calls `python3 birth-contract-check.py "$AGENT_NAME_ARG"`.
    # An agent with no directory reads nothing live; the world is a full fixture.
    assert not (PROJECT_ROOT / "agents" / ABSENT_AGENT).exists()
    _, _, world = _born(tmp_path)
    r = _cli([ABSENT_AGENT], MIND_WORLD=str(world))
    assert r.returncode == 1, r.stderr
    reported = set(re.findall(r"MISSING (\S+) \(", r.stdout))
    assert reported == {"agent-initialized", "agent-state", "self-md", "curriculum-stage"}
    assert f"4 of {len(bcc.ELEMENT_IDS) + len(SLOTS)}" in r.stdout


def test_cli_prints_a_non_ascii_path_on_a_narrow_stdout(tmp_path):
    # A Windows pipe gives stdout the ANSI code page, which cannot encode every
    # path character. The report must still print, not end in a traceback.
    _, _, world = _born(tmp_path / "wörld")
    (world / "program.md").unlink()
    r = _cli([ABSENT_AGENT], MIND_WORLD=str(world), PYTHONIOENCODING="ascii")
    assert "Traceback" not in r.stderr, r.stderr
    assert "MISSING program-md" in r.stdout


@pytest.mark.parametrize("args,env", [([], {"MIND_AGENT": None}), (["../escape"], {})],
                         ids=["no-agent", "path-in-name"])
def test_cli_usage_errors_exit_2(args, env):
    r = _cli(args, **env)
    assert r.returncode == 2
    assert "agent name is required" in r.stderr


def _sandbox(tmp_path, state_script):
    """The production init-mind.sh with stub neighbours that log each call."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(INIT_MIND, scripts / "init-mind.sh")
    log = tmp_path / "calls.log"
    # newline="\n": a CRLF stub fails in bash on the \r, and write_text emits CRLF on Windows.
    (scripts / "_paths.sh").write_text(
        'CORE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n', newline="\n")
    for name in ("init-world.sh", "init-agent.sh", "init-meta.sh"):
        (scripts / name).write_text(f'echo "{name} $*" >> "{log.as_posix()}"\n', newline="\n")
    (scripts / "session-state-get.sh").write_text(state_script, newline="\n")
    (scripts / "birth-contract-check.py").write_text(
        "import os, sys\n"
        f"open({str(log)!r}, 'a').write('check ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('STUB_CHECK_RC', '0')))\n")
    return scripts / "init-mind.sh", log


def _run_init_mind(script, check_rc=0):
    env = dict(os.environ, STUB_CHECK_RC=str(check_rc))
    return subprocess.run([BASH, script.as_posix(), "a1"], env=env,
                          capture_output=True, text=True, timeout=120)


def test_wiring_runs_the_check_after_init_on_a_booting_mind_and_never_fails_init(tmp_path):
    script, log = _sandbox(tmp_path, 'echo "RUNNING"\n')
    r = _run_init_mind(script, check_rc=1)
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [
        "init-world.sh ", "init-agent.sh a1", "init-meta.sh ", "check a1"]


@pytest.mark.parametrize("state_script", [
    'echo "IDLE"\n', 'echo "UNINITIALIZED"\n', "exit 3\n"],
    ids=["idle", "interview-uninitialized", "state-read-fails"])
def test_wiring_skips_the_check_unless_running(tmp_path, state_script):
    script, log = _sandbox(tmp_path, state_script)
    r = _run_init_mind(script)
    assert r.returncode == 0, r.stderr
    assert "check" not in log.read_text()


# ---- : the named agent's world, in both call shapes ------------------

WORLD_IDS = {"program-md"} | {bcc.HOOK_SLOT_PREFIX + s for s in SLOTS}


def _two_worlds(tmp_path):
    """A sandbox root whose two agents name different worlds in local-paths.conf.

    aa sorts first and has a full world; bb's holds only the placeholder
    program.md. The real check, init-mind.sh and _paths pair are copied in, so
    the sandbox is its own PROJECT_ROOT; with no .mind-data the world resolves
    from the agent's conf. Returns the sandbox core/scripts dir.
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("birth-contract-check.py", "init-mind.sh", "_paths.py", "_path_helpers.py", "_paths.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        (scripts / name).write_text(text, encoding="utf-8", newline="")
    if (SCRIPTS / ".python-shim").is_dir():  # _paths.sh's Windows python3 shim
        shutil.copytree(SCRIPTS / ".python-shim", scripts / ".python-shim")
    templates = tmp_path / "core" / "config" / "templates"
    templates.mkdir(parents=True)
    for slot in SLOTS:
        shutil.copy2(TEMPLATES / f"{slot}-default.md", templates / f"{slot}-default.md")
    for name in ("init-world.sh", "init-agent.sh", "init-meta.sh"):
        (scripts / name).write_text("exit 0\n", newline="\n")
    (scripts / "session-state-get.sh").write_text('echo "RUNNING"\n', newline="\n")
    for agent, full in (("aa", True), ("bb", False)):
        world = tmp_path / f"world-{agent}"
        (world / "conventions").mkdir(parents=True)
        (world / "program.md").write_text("# The Program\nLearn the place.\n" if full else "")
        for slot in SLOTS if full else ():
            (world / "conventions" / f"{slot}.md").write_text(f"# {slot}\n## Step 1\nDo it.\n")
        (tmp_path / "agents" / agent).mkdir(parents=True)
        (tmp_path / "agents" / agent / "local-paths.conf").write_text(
            f"WORLD_PATH={world.as_posix()}\nMETA_PATH={(tmp_path / f'meta-{agent}').as_posix()}\n",
            newline="\n")
    return scripts


@pytest.mark.parametrize("shape", ["cli", "init-mind"])
@pytest.mark.parametrize("named,bound,hollow", [
    ("bb", None, True),
    ("bb", "aa", True),
    ("aa", "bb", False),
    ("aa", None, False),  # positive control: the first agent unbound, green with or without the fix
], ids=["bb-unbound", "bb-bound-to-aa", "aa-bound-to-bb", "control-aa-unbound"])
def test_the_named_agents_world_is_checked(tmp_path, shape, named, bound, hollow):
    # Before  the check read the caller's binding's world, or the first
    # agent's when unbound, so bb's hollow world reported PRESENT. init-mind.sh
    # also sourced _paths.sh before binding, and the MIND_WORLD it exported
    # overrode the check's own binding.
    scripts = _two_worlds(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    env["STORAGE_BACKEND"] = "local"
    if bound:
        env["MIND_AGENT"] = bound
    if shape == "cli":
        argv = [sys.executable, str(scripts / "birth-contract-check.py"), named]
    else:  # the production call site (guard-920), through the real _paths.sh
        argv = [BASH, (scripts / "init-mind.sh").as_posix(), named]
    r = subprocess.run(argv, env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert f"[birth-contract] WARNING {named}:" in r.stdout, (r.stdout, r.stderr)
    assert r.returncode == (1 if shape == "cli" else 0), r.stderr
    missing = dict(re.findall(r"MISSING (program-md|hook-slot:\S+) \(([^)]*)\)", r.stdout))
    if hollow:
        assert set(missing) == WORLD_IDS, r.stdout
        assert all("world-bb" in Path(p).parts for p in missing.values()), missing
    else:
        assert missing == {}, r.stdout
