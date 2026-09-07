""" item 3 — the forged-skill BODY gate.

THE DEFECT (coach-mind, zc-03, 2026-09-05 11:55Z): /forge-skill wrote a
registry row into world/forged-skills.yaml, the skill DIRECTORY was empty, a
test goal was filed to exercise a skill with no body, two "forge-skill,
complete" board posts went out, and the model declared success. Nothing in the
procedure asked whether the thing being registered exists (rb-10227).

Tested as a PAIR in both directions on purpose. A gate that refuses a missing
body is worthless if it also refuses a correct one — that would make every
forge un-registerable, which is a worse failure than the phantom row. So every
refusal case below has a positive control that differs by ONE property.
"""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
GATE = SCRIPTS / "forged-skill-body-gate.sh"
PROJECT_ROOT = SCRIPTS.parents[1]

GOOD_BODY = "---\nname: widget-probe\ndescription: probes a widget service\n---\n\n## Return Protocol\nBash.\n"


def _bash():
    from _runtime_bash import BASH  # guard-580: never a bare "bash" argv
    return str(BASH)


sys.path.insert(0, str(SCRIPTS))


def _run(*args, root=None):
    argv = [_bash(), GATE.as_posix(), *args]
    if root is not None:
        argv += ["--project-root", str(root)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def _seed(tmp_path, name, body=GOOD_BODY, make_dir=True, make_file=True):
    d = tmp_path / ".claude" / "skills" / name
    if make_dir:
        d.mkdir(parents=True, exist_ok=True)
    if make_file:
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- refusals

def test_the_coach_failure_mode_empty_skill_dir_is_refused(tmp_path):
    """The measured incident: `mkdir -p` succeeded, the Write was refused, and
    registration proceeded anyway against an empty directory."""
    _seed(tmp_path, "query-scouting-matchups", make_file=False)
    r = _run("--skill", "query-scouting-matchups", root=tmp_path)
    assert r.returncode == 1
    assert "no SKILL.md at the path the runtime loads" in r.stderr


def test_absent_directory_entirely_is_refused(tmp_path):
    r = _run("--skill", "never-forged", root=tmp_path)
    assert r.returncode == 1


def test_zero_byte_body_is_refused(tmp_path):
    _seed(tmp_path, "hollow", body="")
    r = _run("--skill", "hollow", root=tmp_path)
    assert r.returncode == 1
    assert "EMPTY" in r.stderr


def test_body_without_opening_front_matter_is_refused(tmp_path):
    """parse_front_matter anchors on '---' at line 1; a body that misses it is
    invisible to every consumer (rb-840 / guard-518, the 2026-05-11 incident
    where 7 SKILL.md files silently lost their front matter)."""
    _seed(tmp_path, "unparseable", body="# Title\n\ndescription: not front matter\n")
    r = _run("--skill", "unparseable", root=tmp_path)
    assert r.returncode == 1
    assert "front matter" in r.stderr


def test_front_matter_without_description_is_refused(tmp_path):
    """Loadable but unselectable — the same dead end one layer down."""
    _seed(tmp_path, "nameless", body="---\nname: x\n---\nbody\n")
    r = _run("--skill", "nameless", root=tmp_path)
    assert r.returncode == 1
    assert "description" in r.stderr


# ---------------------------------------------------------------- positives

def test_a_correct_body_passes(tmp_path):
    _seed(tmp_path, "widget-probe")
    r = _run("--skill", "widget-probe", root=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_one_variable_control_description_alone_flips_the_verdict(tmp_path):
    """The same path, the same file, differing by one key. Without this the
    refusals above could all be produced by a gate that never passes."""
    _seed(tmp_path, "flip", body="---\nname: x\n---\nbody\n")
    assert _run("--skill", "flip", root=tmp_path).returncode == 1
    _seed(tmp_path, "flip", body="---\nname: x\ndescription: d\n---\nbody\n")
    assert _run("--skill", "flip", root=tmp_path).returncode == 0


def test_crlf_front_matter_still_passes(tmp_path):
    """A body authored on Windows must not be refused for its line endings."""
    _seed(tmp_path, "crlf", body="---\r\nname: x\r\ndescription: d\r\n---\r\nbody\r\n")
    r = _run("--skill", "crlf", root=tmp_path)
    assert r.returncode == 0, r.stderr


def test_no_false_refusal_across_the_real_skill_corpus():
    """The false-positive control that matters: every skill body actually on
    this box must pass. A gate wired into forge-skill that refuses real bodies
    would block every future forge."""
    skills_dir = PROJECT_ROOT / ".claude" / "skills"
    if not skills_dir.is_dir():
        pytest.skip("no .claude/skills on this deployment")
    names = [p.name for p in skills_dir.iterdir() if p.is_dir()]
    if not names:
        pytest.skip("no skill directories present")
    refused = [n for n in names if _run("--skill", n).returncode != 0]
    assert refused == [], f"gate falsely refuses real skill bodies: {refused}"


# ---------------------------------------------------------------- contract

def test_missing_skill_name_is_a_usage_error_not_a_pass(tmp_path):
    """rc=2, never 0 — a caller that forgets the argument must not read the
    silence as approval."""
    r = _run(root=tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("bad", ["../../etc", "a/b", ".", ".."])
def test_path_traversal_names_are_refused_as_usage_errors(bad, tmp_path):
    r = _run("--skill", bad, root=tmp_path)
    assert r.returncode == 2, (bad, r.returncode, r.stderr)


def test_override_is_audited_and_permits(tmp_path):
    _seed(tmp_path, "ghost", make_file=False)
    r = _run("--skill", "ghost", "--override-body-gate", "body lives off-tree", root=tmp_path)
    assert r.returncode == 0
    assert "OVERRIDE applied" in r.stderr


def test_gate_is_wired_not_merely_present():
    """guard-399's audit recipe: grep the script name across core/ + .claude/.
    A gate that appears only in its own test has never been wired and has never
    run. This test IS that recipe, run continuously."""
    hits = []
    for root in (PROJECT_ROOT / "core", PROJECT_ROOT / ".claude"):
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix in {".pyc"} or "__pycache__" in p.parts:
                continue
            if p.name == Path(__file__).name or p.name == GATE.name:
                continue
            try:
                if "forged-skill-body-gate" in p.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(p.relative_to(PROJECT_ROOT).as_posix())
            except OSError:
                continue
    assert hits, (
        "forged-skill-body-gate.sh has NO call site outside its own test — "
        "per guard-399 that means it has never fired. Wire it into "
        ".claude/skills/forge-skill/SKILL.md Step 4 and its Return Protocol.")
