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
    # The shared predicate protects it: dest-owned (never in source_skill_names).
    pred = _engine._living_dest_preserve_predicate(dest)
    assert pred(".claude/skills/domain-thing/SKILL.md") is True
    # And the bound is what keeps that protection from overreaching: were the
    # same name promoted from source, it would become plantable.
    pred_bounded = _engine._living_dest_preserve_predicate(dest, {"domain-thing"})
    assert pred_bounded(".claude/skills/domain-thing/SKILL.md") is False


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


# ── PLAN: skill-dir cruft-sweep is ALWAYS dangerous ( regression) ──

# A cruft pattern that targets a forged-skill dir — the shape that lists
# .claude/skills/notify-user/ in the live seed manifest (16 of 35 cruft_patterns
# are .claude/skills/<name>/ dirs stripped from the domain-free seed).
MANIFEST_WITH_SKILL_CRUFT = {
    "include": [
        {"path": "CLAUDE.md", "type": "file"},
        {"path": ".claude/settings.json", "type": "file"},
        {"path": "core/base.py", "type": "file"},
    ],
    "transformations": [],
    "cruft_patterns": [".claude/skills/notify-user/"],
}


def _add_unregistered_skill(dest: Path, name: str = "notify-user") -> None:
    """A forged-skill dir present at dest but ABSENT from dest/world/forged-skills.yaml
    — the notify-user-at-ZDS shape: a promoted skill dir the dest registry does not
    list (registration is external/gitignored, so it never traveled with the git
    promotion). It carries a SKILL.md, so the root-cause-A fix (g-115-2739) protects
    it by presence — would_delete=False, protection_class='forged-skill'."""
    sk = dest / ".claude" / "skills" / name
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        f"# {name}\ndomain notification transport\n", encoding="utf-8")


def test_plan_unregistered_skill_dir_with_skillmd_protected_from_cruft(tmp_path):
    """Root cause A fix (; supersedes the  flag-as-dangerous
    behavior). A .claude/skills/<name>/ dir carrying a SKILL.md but ABSENT from
    the dest registry is now PROTECTED (would_delete=False, protection_class=
    'forged-skill') even when a cruft pattern matches it — SKILL.md-presence
    protection, not registry membership (guard-1271). The danger the earlier
    regression surfaced is ELIMINATED at the source, not merely flagged: the
    promoted-but-unregistered skill (the notify-user-at-ZDS shape) survives.
    The real seed-manifest.yaml no longer lists skill dirs as cruft at all
    (g-115-2739); this custom manifest exercises the engine's defense-in-depth.
    See test_plan_flags_no_skillmd_skill_dir_cruft_sweep_as_dangerous for the
    still-flagged case (a skill dir WITHOUT a SKILL.md is not a real skill).

    (Supersedes the retired test_plan_flags_unregistered_skill_dir_cruft_sweep_
    as_dangerous, which asserted the pre-fix would_delete=True behavior.)"""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)
    _add_unregistered_skill(dest, "notify-user")   # has a SKILL.md

    plan = _engine.do_plan(src, dest, MANIFEST_WITH_SKILL_CRUFT, living_prod=True)
    cruft = plan["sections"]["cruft_sweep_deletions"]

    entry = next(c for c in cruft["all"] if c["rel"] == ".claude/skills/notify-user")
    assert entry["would_delete"] is False                 # SKILL.md protects it
    assert entry["protection_class"] == "forged-skill"    # recognized via SKILL.md presence
    # Correctly protected -> NOT in the dangerous list, no false REVIEW from this skill.
    assert not any(c["rel"] == ".claude/skills/notify-user" for c in cruft["dangerous"])


def test_plan_flags_no_skillmd_skill_dir_cruft_sweep_as_dangerous(tmp_path):
    """ honest-plan coverage, PRESERVED under the  fix. A
    `.claude/skills/<name>/` dir WITHOUT a SKILL.md is NOT a real skill, so the
    SKILL.md-presence protection does not cover it. If a cruft pattern would
    delete it under --living-prod, the plan MUST still surface it as dangerous
    (skills-prefix clause) — the plan never silently under-reports a skill-dir
    deletion, even though real (SKILL.md-bearing) skills are now protected."""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)
    # skill dir with NO SKILL.md — not protected by presence
    sk = dest / ".claude" / "skills" / "malformed"
    sk.mkdir(parents=True)
    (sk / "notes.txt").write_text("no SKILL.md here\n", encoding="utf-8")
    manifest = dict(MANIFEST_WITH_SKILL_CRUFT)
    manifest["cruft_patterns"] = [".claude/skills/malformed/"]

    plan = _engine.do_plan(src, dest, manifest, living_prod=True)
    cruft = plan["sections"]["cruft_sweep_deletions"]

    entry = next(c for c in cruft["all"] if c["rel"] == ".claude/skills/malformed")
    assert entry["would_delete"] is True                 # no SKILL.md -> not protected
    assert entry["protection_class"] is None
    assert any(c["rel"] == ".claude/skills/malformed" for c in cruft["dangerous"])
    assert plan["verdict"] == "REVIEW REQUIRED"


def test_plan_registered_skill_dir_cruft_pattern_not_flagged(tmp_path):
    """Negative control: a REGISTERED dest forged skill matched by a cruft
    pattern is protected (would_delete=False) and never enters 'dangerous' —
    the fix flags only GENUINE skill-dir deletions, no false positives."""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)   # registers domain-thing in dest/world/forged-skills.yaml
    manifest = dict(MANIFEST_WITH_SKILL_CRUFT)
    manifest["cruft_patterns"] = [".claude/skills/domain-thing/"]

    plan = _engine.do_plan(src, dest, manifest, living_prod=True)
    cruft = plan["sections"]["cruft_sweep_deletions"]

    entry = next(c for c in cruft["all"] if c["rel"] == ".claude/skills/domain-thing")
    assert entry["would_delete"] is False              # registry protects it
    assert entry["protection_class"] == "forged-skill"
    assert cruft["dangerous"] == []                    # correctly protected -> not flagged


# ── PLANT: base skills in the source include set must never be frozen ──
# The 2026-07-19..31 partial-plant incident: the  SKILL.md-presence
# union marked EVERY dest skill dir protected, so copy-staged under
# --living-prod silently skipped every base skill present at both ends.
# Claude-Mind's 51 base SKILL.md files sat at 2026-07-19 content across three
# consecutive plants (#13, #14, v2.8.7) while core/ files planted normally —
# and the freeze is self-concealing, because each partial plant makes the dest
# copy look MORE dest-ahead to the next plan verdict.

BASE_SKILL_SRC = "# prime skill\nupstream v2 content\n"
BASE_SKILL_DEST_STALE = "# prime skill\nstale july-19 content\n"


def _with_base_skill(src: Path, dest: Path) -> dict:
    """Add .claude/skills/prime/SKILL.md to both trees (src fresh, dest stale)
    and return a manifest whose include set carries it."""
    for root, body in ((src, BASE_SKILL_SRC), (dest, BASE_SKILL_DEST_STALE)):
        d = root / ".claude" / "skills" / "prime"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return {
        "include": list(MANIFEST["include"]) + [
            {"path": ".claude/skills/prime/SKILL.md", "type": "file"}],
        "transformations": [],
    }


def test_base_skill_in_include_set_is_planted_not_frozen(tmp_path):
    """Registry-readable branch: a skill in the SOURCE include set is by
    construction a base skill, not dest-owned — it must plant, while the
    dest-owned forged skill in the same run stays untouched."""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)
    manifest = _with_base_skill(src, dest)

    stats = _engine.do_copy_staged(src, dest, manifest, preserve_deployment_local=True)
    _engine.do_swap(dest)

    assert (dest / ".claude" / "skills" / "prime" / "SKILL.md").read_text(
        encoding="utf-8") == BASE_SKILL_SRC                    # updated, not frozen
    assert ".claude/skills/prime/SKILL.md" not in stats["preserved_deployment_local"]
    assert (dest / ".claude" / "skills" / "domain-thing" / "SKILL.md").read_text(
        encoding="utf-8") == FORGED_SKILL_BODY                 # dest-owned survives
    # Deployment-local preservation is unaffected by the skill bound.
    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == DEST_CLAUDE


def test_base_skill_plants_even_when_dest_registry_unreadable(tmp_path):
    """protect_all_skills branch (no locatable registry — Claude-Mind's actual
    state: no world/, no agents/*/local-paths.conf): the fail-safe covers
    dest-owned dirs only. Source-owned base skills still plant; the dest-only
    skill dir still survives via SKILL.md presence."""
    src = _mk_source(tmp_path)
    dest = _mk_living_dest(tmp_path)
    (dest / "world" / "forged-skills.yaml").unlink()   # registry now unlocatable
    manifest = _with_base_skill(src, dest)

    stats = _engine.do_copy_staged(src, dest, manifest, preserve_deployment_local=True)
    _engine.do_swap(dest)

    assert (dest / ".claude" / "skills" / "prime" / "SKILL.md").read_text(
        encoding="utf-8") == BASE_SKILL_SRC                    # fail-safe does not freeze base skills
    assert ".claude/skills/prime/SKILL.md" not in stats["preserved_deployment_local"]
    assert (dest / ".claude" / "skills" / "domain-thing" / "SKILL.md").read_text(
        encoding="utf-8") == FORGED_SKILL_BODY                 # dest-only dir still protected
