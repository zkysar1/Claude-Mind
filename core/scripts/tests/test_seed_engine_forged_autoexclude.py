"""test_seed_engine_forged_autoexclude.py —  regression.

The seed engine's walk_include_entry now AUTO-DERIVES forged-skill exclusions
from world/forged-skills.yaml at seed-create and UNIONs them onto the manifest's
static exclude_children (scoped to the .claude/skills/ entry). This closes the
recurring manual-sync leak (v2.2.0 audit-roblox-deliverable, v2.4.0
build-operator-job): a forged skill registered in the registry but absent from
the manifest static list no longer travels into the domain-free seed.

These tests pin four invariants:
  1. A forged skill is excluded even when NOT in the static exclude_children.
  2. Registry unlocatable -> _dest_forged_skill_names returns None -> the union
     is a no-op and ONLY the static list applies (fail-safe floor).
  3. UNION, not replace: static non-forged entries (worktrees) AND derived
     forged names are both excluded.
  4. Scoped to .claude/skills/ only: a same-named dir under another entry
     (core/) is NOT auto-excluded.

Hermetic: builds tmp source roots with a local world/forged-skills.yaml; no
network, no daemon, no real backend.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
walk_include_entry = _engine.walk_include_entry


def _mk_skill(root: Path, name: str) -> None:
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


def _mk_registry(root: Path, forged_names) -> None:
    """Write a local world/forged-skills.yaml the engine resolver reads first."""
    w = root / "world"
    w.mkdir(parents=True, exist_ok=True)
    lines = ["skills:"]
    for n in forged_names:
        lines.append(f"  {n}:")
        lines.append("    parent: aspirations")
        lines.append("    type: utility")
    (w / "forged-skills.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _skills_entry(exclude_children=None):
    return {
        "path": ".claude/skills/",
        "type": "directory",
        "required": True,
        "exclude_children": list(exclude_children or []),
    }


def _top_skill_dirs(results):
    """Top-level skill dir names present in a walk_include_entry result list."""
    out = set()
    for rel in results:
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "skills":
            out.add(parts[2])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1. Auto-derive: forged skill excluded even when NOT in the static list
# ─────────────────────────────────────────────────────────────────────────────
def test_forged_skill_autoexcluded_when_absent_from_static_list(tmp_path):
    for s in ("notify-user", "probe-governed-store", "aspirations"):
        _mk_skill(tmp_path, s)
    # Registry lists the two forged skills; the base skill (aspirations) is NOT
    # in it. Static exclude_children is EMPTY — so any exclusion of the forged
    # skills is proof the auto-derive fired.
    _mk_registry(tmp_path, ["notify-user", "probe-governed-store"])

    results = walk_include_entry(_skills_entry([]), tmp_path)
    present = _top_skill_dirs(results)

    assert "aspirations" in present, "base skill must be INCLUDED in the seed"
    assert "notify-user" not in present, "registered forged skill must be auto-excluded"
    assert "probe-governed-store" not in present, (
        "forged skill absent from the static list must STILL be auto-excluded "
        "(the g-306-88 leak this fix closes)")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fail-safe floor: registry unlocatable -> only the static list applies
# ─────────────────────────────────────────────────────────────────────────────
def test_registry_absent_falls_back_to_static_floor(tmp_path):
    for s in ("notify-user", "probe-governed-store", "aspirations"):
        _mk_skill(tmp_path, s)
    # NO world/forged-skills.yaml and no agents/*/local-paths.conf ->
    # _dest_forged_skill_names returns None -> union is a no-op.
    entry = _skills_entry(["notify-user"])  # static list carries one forged skill

    results = walk_include_entry(entry, tmp_path)
    present = _top_skill_dirs(results)

    assert "notify-user" not in present, "static exclude_children floor must apply"
    assert "aspirations" in present
    # No registry to derive from, so the forged skill NOT in the static list is
    # NOT excluded — the fail-safe preserves everything the static list names and
    # nothing more (the downstream domain-leak preflight remains the backstop).
    assert "probe-governed-store" in present, (
        "with no registry, only the static floor applies — nothing is auto-derived")


# ─────────────────────────────────────────────────────────────────────────────
# 3. UNION, not replace: static non-forged entries + derived forged names
# ─────────────────────────────────────────────────────────────────────────────
def test_union_static_and_derived_both_excluded(tmp_path):
    for s in ("notify-user", "aspirations"):
        _mk_skill(tmp_path, s)
    # A non-forged ephemeral child that lives only in the static list.
    (tmp_path / ".claude" / "skills" / "worktrees").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "skills" / "worktrees" / "junk.txt").write_text("x\n",
                                                                            encoding="utf-8")
    _mk_registry(tmp_path, ["notify-user"])

    results = walk_include_entry(_skills_entry(["worktrees"]), tmp_path)
    present = _top_skill_dirs(results)
    all_paths = set(results)

    assert "notify-user" not in present, "derived forged exclusion must apply"
    assert not any("worktrees" in p for p in all_paths), (
        "static non-forged exclusion must survive the union (not be replaced)")
    assert "aspirations" in present


# ─────────────────────────────────────────────────────────────────────────────
# 4. Scoping: the auto-derive fires ONLY for the .claude/skills/ entry
# ─────────────────────────────────────────────────────────────────────────────
def test_autoderive_scoped_to_claude_skills_entry_only(tmp_path):
    # A same-named dir under a DIFFERENT include entry (core/) must NOT be
    # auto-excluded just because a forged skill shares its name.
    core_dir = tmp_path / "core" / "notify-user"
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "keep.py").write_text("# keep\n", encoding="utf-8")
    _mk_registry(tmp_path, ["notify-user"])

    entry = {"path": "core/", "type": "directory", "required": True,
             "exclude_children": []}
    results = walk_include_entry(entry, tmp_path)

    assert any("core/notify-user/keep.py" == p for p in results), (
        "forged auto-derive must be scoped to .claude/skills/ — a same-named dir "
        "under core/ must be preserved")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Resolver EXCEPTION must fail-safe to the static floor, never crash seed-create
#    (fresh-eyes finding on 94572b2af: the engine call was unguarded while the
#    scan caller wraps it in except Exception. A non-UTF-8 local-paths.conf raises
#    UnicodeDecodeError past _read_conf_path_key's OSError-only guard.)
# ─────────────────────────────────────────────────────────────────────────────
def test_resolver_exception_falls_back_to_static_floor(tmp_path, monkeypatch):
    for s in ("notify-user", "aspirations"):
        _mk_skill(tmp_path, s)

    def _boom(_root):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(_engine, "_dest_forged_skill_names", _boom)

    # Must NOT raise; the static list ["notify-user"] still applies as the floor.
    results = walk_include_entry(_skills_entry(["notify-user"]), tmp_path)
    present = _top_skill_dirs(results)
    assert "notify-user" not in present, "static floor must apply despite resolver crash"
    assert "aspirations" in present, "seed-create must not crash when the resolver raises"
