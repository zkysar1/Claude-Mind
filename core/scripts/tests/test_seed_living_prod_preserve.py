"""test_seed_living_prod_preserve.py — --living-prod deployment-local preservation.

g-xw-20260714T023400-01 (asp-306 promotion hardening, P1). Before this fix,
do_copy_staged + do_swap overwrote the full include set unconditionally, so
deployment-local files (CLAUDE.md, .claude/settings.json) present IN the include
set were clobbered at a LIVING destination even with --living-prod (spec Bug #1,
verified live 2026-07-14). The fix threads preserve_deployment_local into
do_copy_staged: a protected include-set member that already exists at dest is
NOT staged, so do_swap never overwrites the destination's own copy (filtering in
copy-staged alone is sufficient — swap only moves what copy-staged stages, and
staging is rebuilt fresh each call). do_plan §1 is made living_prod-aware to
match so the --plan overwrite list goes empty under the flag.

These tests pin, per the goal's acceptance:
  1. PLANT: divergent CLAUDE.md + settings.json survive copy-staged+swap under
     --living-prod; a dest-owned forged skill (absent from the include set) is
     never touched; a protected file ABSENT at dest is still planted fresh.
  2. NEGATIVE CONTROL: without the flag the same plant OVERWRITES them (proves
     the flag is load-bearing, not incidental).
  3. PLAN: do_plan §1 overwrites go empty under living_prod (the acceptance) and
     the files appear under 'preserved'; without the flag §1 reports them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine_livingprod_t", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)


# Include set carries two deployment-local files + one plain framework file.
MANIFEST = {
    "include": [
        {"path": "CLAUDE.md", "type": "file"},
        {"path": ".claude/settings.json", "type": "file"},
        {"path": "core/base.py", "type": "file"},
    ],
    "transformations": [],
}

SRC_CLAUDE = "# SOURCE CLAUDE\nupstream template\n"
SRC_SETTINGS = '{"source": true}\n'
# base.py: source is a strict superset of dest (dest has no source-absent lines),
# so it is diverged but NOT prod-ahead — keeps the plan verdict clean.
SRC_BASE = "BASE = 'x'\nUPSTREAM = 1\n"

DEST_CLAUDE = "# DEST CLAUDE\ntuned for this deployment\n"
DEST_SETTINGS = '{"dest": true, "tuned": 1}\n'
DEST_BASE = "BASE = 'x'\n"

FORGED_SKILL_BODY = "# domain-thing\ndest capability\n"


def _mk_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / ".claude").mkdir(parents=True)
    (src / "core").mkdir(parents=True)
    (src / "CLAUDE.md").write_text(SRC_CLAUDE, encoding="utf-8")
    (src / ".claude" / "settings.json").write_text(SRC_SETTINGS, encoding="utf-8")
    (src / "core" / "base.py").write_text(SRC_BASE, encoding="utf-8")
    return src


def _mk_living_dest(tmp_path: Path) -> Path:
    """A populated LIVING dest: divergent deployment-local files + a dest-owned
    forged skill registered in dest/world/forged-skills.yaml."""
    dest = tmp_path / "dest"
    (dest / ".claude").mkdir(parents=True)
    (dest / "core").mkdir(parents=True)
    (dest / "CLAUDE.md").write_text(DEST_CLAUDE, encoding="utf-8")
    (dest / ".claude" / "settings.json").write_text(DEST_SETTINGS, encoding="utf-8")
    (dest / "core" / "base.py").write_text(DEST_BASE, encoding="utf-8")

    # Dest-owned forged skill — NOT present in the source include set.
    skill_dir = dest / ".claude" / "skills" / "domain-thing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(FORGED_SKILL_BODY, encoding="utf-8")

    # Registry (lookup order #1: dest/world/forged-skills.yaml).
    (dest / "world").mkdir(parents=True)
    (dest / "world" / "forged-skills.yaml").write_text(
        "skills:\n  domain-thing:\n    triggers: [x]\n", encoding="utf-8")
    return dest


# ── PLANT: --living-prod preserves divergent deployment-local + forged skill ──

def test_living_prod_preserves_divergent_deployment_local(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)

    stats = _engine.do_copy_staged(src, dest, MANIFEST, preserve_deployment_local=True)
    _engine.do_swap(dest)

    # Dest's OWN deployment-local content survived — not overwritten by source.
    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == DEST_CLAUDE
    assert (dest / ".claude" / "settings.json").read_text(encoding="utf-8") == DEST_SETTINGS
    # The plain framework file WAS updated to source (that is the point of a plant).
    assert (dest / "core" / "base.py").read_text(encoding="utf-8") == SRC_BASE
    # Preservation is reported for the protected files, not the plain one.
    assert "CLAUDE.md" in stats["preserved_deployment_local"]
    assert ".claude/settings.json" in stats["preserved_deployment_local"]
    assert "core/base.py" not in stats["preserved_deployment_local"]


def test_living_prod_never_touches_dest_owned_forged_skill(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)

    _engine.do_copy_staged(src, dest, MANIFEST, preserve_deployment_local=True)
    _engine.do_swap(dest)

    # The dest-owned forged skill (absent from include set) is untouched.
    assert (dest / ".claude" / "skills" / "domain-thing" / "SKILL.md").read_text(
        encoding="utf-8") == FORGED_SKILL_BODY
    # And the shared predicate would protect it even if it entered the include set.
    pred = _engine._living_dest_preserve_predicate(dest)
    assert pred(".claude/skills/domain-thing/SKILL.md") is True


def test_fresh_deployment_local_absent_at_dest_is_planted(tmp_path):
    """A protected file ABSENT at dest is still planted (a living dest lacking a
    template gets it) — the existence gate, not the predicate, decides."""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)
    (dest / "CLAUDE.md").unlink()  # dest lacks its own CLAUDE.md

    stats = _engine.do_copy_staged(src, dest, MANIFEST, preserve_deployment_local=True)
    _engine.do_swap(dest)

    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == SRC_CLAUDE          # planted fresh
    assert "CLAUDE.md" not in stats["preserved_deployment_local"]
    assert ".claude/settings.json" in stats["preserved_deployment_local"]          # still present -> kept
    assert (dest / ".claude" / "settings.json").read_text(encoding="utf-8") == DEST_SETTINGS


# ── NEGATIVE CONTROL: without the flag, the same plant clobbers (Bug #1) ──

def test_without_flag_deployment_local_is_overwritten(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)

    stats = _engine.do_copy_staged(src, dest, MANIFEST, preserve_deployment_local=False)
    _engine.do_swap(dest)

    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == SRC_CLAUDE
    assert (dest / ".claude" / "settings.json").read_text(encoding="utf-8") == SRC_SETTINGS
    assert stats["preserved_deployment_local"] == []


def test_default_signature_still_overwrites(tmp_path):
    """Backward compatibility: the default call (no keyword) is unchanged."""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)
    stats = _engine.do_copy_staged(src, dest, MANIFEST)   # no preserve kwarg
    _engine.do_swap(dest)
    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == SRC_CLAUDE
    assert stats["preserved_deployment_local"] == []


# ── PLAN: §1 overwrites empty under --living-prod (the acceptance) ──

def test_plan_section1_empty_under_living_prod(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)

    plan = _engine.do_plan(src, dest, MANIFEST, living_prod=True)
    dl = plan["sections"]["deployment_local_overwrites"]

    # Acceptance: §1 overwrite list empty for a living dest under --living-prod.
    assert dl["all"] == []
    assert dl["diverged"] == []
    # The two deployment-local files are reported as preserved (dest content kept).
    preserved_rels = {d["rel"] for d in dl["preserved"]}
    assert preserved_rels == {"CLAUDE.md", ".claude/settings.json"}
    assert all(d["diverged"] for d in dl["preserved"])
    # Everything divergent is preserved -> verdict is SAFE (no review axis fires).
    assert plan["verdict"] == "SAFE"


def test_plan_section1_reports_overwrites_without_flag(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)

    plan = _engine.do_plan(src, dest, MANIFEST, living_prod=False)
    dl = plan["sections"]["deployment_local_overwrites"]

    # Without the flag the plan honestly surfaces the clobber (Bug #1 surface).
    over_rels = {d["rel"] for d in dl["all"]}
    assert over_rels == {"CLAUDE.md", ".claude/settings.json"}
    assert len(dl["diverged"]) == 2
    assert dl["preserved"] == []
    assert plan["verdict"] == "REVIEW REQUIRED"
    assert "diverged deployment-local" in plan["verdict_reason"]


def test_plan_report_renders_preserved_under_living_prod(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)

    report = _engine._render_plan_report(_engine.do_plan(src, dest, MANIFEST, living_prod=True))

    assert "Bug #1 fixed" in report
    assert "preserved" in report
    # No overwrite/keep-dest warning for deployment-local files under the flag.
    assert "KEEP DEST — pass --living-prod" not in report
