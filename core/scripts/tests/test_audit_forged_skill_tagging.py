"""audit-forged-skill-tagging.sh classifies registry-ahead-of-repo as WARN ().

The audit exists so a packaging pass can tell framework skills from forged-out
domain skills by the in-file `forged: true` tag. A registry key whose folder is
NOT on this checkout (a peer forged it and has not pushed yet) cannot be
mis-packaged — it is push hygiene, reported by name, never a promotion-blocking
FAIL. The FAIL set is the real leak risk: a present-but-untagged folder, or a
tagged folder nobody registered. Measured 2026-09-03: two absent entries
withheld a release from staging for 2h on a box that owned neither.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _bash_helpers import BASH  # guard-580: never a bare "bash" argv[0]

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "audit-forged-skill-tagging.sh"


def _skill(skills: Path, name: str, *, forged: bool):
    d = skills / name
    d.mkdir(parents=True)
    fm = "---\nname: %s\n%s---\n# %s\n" % (name, "forged: true\n" if forged else "", name)
    (d / "SKILL.md").write_text(fm, encoding="utf-8")


def _run(tmp_path: Path, registry_keys, skills: Path):
    reg = tmp_path / "forged-skills.yaml"
    reg.write_text("skills:\n" + "".join(f"  {k}:\n    forged_by: test\n" for k in registry_keys), encoding="utf-8")
    env = {**os.environ, "FORGED_SKILL_AUDIT_REGISTRY": reg.as_posix(),
           "FORGED_SKILL_AUDIT_SKILLS_DIR": skills.as_posix(), "MIND_AGENT": "alpha"}
    p = subprocess.run([BASH, SCRIPT.as_posix()], env=env, cwd=str(REPO), capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def test_registered_but_absent_is_a_named_warn_and_passes(tmp_path):
    skills = tmp_path / "skills"
    _skill(skills, "present-tagged", forged=True)
    rc, out = _run(tmp_path, ["present-tagged", "peer-not-pushed-yet"], skills)
    assert rc == 0, out
    assert "WARN: registered-but-absent" in out and "peer-not-pushed-yet" in out
    assert "PASS:" in out and "FAIL" not in out


def test_present_but_untagged_still_fails(tmp_path):
    skills = tmp_path / "skills"
    _skill(skills, "present-tagged", forged=True)
    _skill(skills, "present-untagged", forged=False)
    rc, out = _run(tmp_path, ["present-tagged", "present-untagged", "peer-not-pushed-yet"], skills)
    assert rc == 1, out
    assert "registered-but-untagged: ['present-untagged']" in out
    assert "peer-not-pushed-yet" in out and "WARN" in out  # the absent one is still only a warning
    assert "'peer-not-pushed-yet'" not in out.split("registered-but-untagged")[1].splitlines()[0]


def test_tagged_but_unregistered_still_fails(tmp_path):
    skills = tmp_path / "skills"
    _skill(skills, "present-tagged", forged=True)
    _skill(skills, "rogue-tagged", forged=True)
    rc, out = _run(tmp_path, ["present-tagged"], skills)
    assert rc == 1, out
    assert "tagged-but-unregistered: ['rogue-tagged']" in out


def test_consistent_registry_passes_clean(tmp_path):
    skills = tmp_path / "skills"
    _skill(skills, "present-tagged", forged=True)
    _skill(skills, "framework-skill", forged=False)  # untagged AND unregistered = a framework skill, fine
    rc, out = _run(tmp_path, ["present-tagged"], skills)
    assert rc == 0 and "PASS:" in out and "WARN" not in out, out
