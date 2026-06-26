"""Scan helper for seed-create.sh.

Reads PROJECT_ROOT and MANIFEST from env. Prints proposed manifest updates:
new skills not in exclude_children, new conventions added since manifest's
`updated` date.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("ERROR: PyYAML required\n")
    sys.exit(2)


def forged_not_excluded(forged_names, exclude_children):
    """Forged skills registered in world/forged-skills.yaml that are NOT in the
    manifest's exclude_children -- they would LEAK into the domain-agnostic seed
    (g-303-21 / zeta allowlist audit 7b, rot-risk HIGH: exclude_children is a
    hand-maintained mirror of the forged registry with NO sync enforcement).
    Returns a sorted list; empty = clean (every registered forged skill is
    excluded). Asymmetric by design: it flags forged-not-in-exclude only, never
    exclude-not-in-forged, because exclude_children legitimately carries extra
    ephemeral entries (worktrees, .history) that are not forged skills.

    forged_names=None (registry unlocatable/unparseable) -> [] : fail-safe, the
    audit cannot run so it must not false-alarm. The mtime-based new-skills scan
    still surfaces drift in that case.
    """
    if not forged_names:
        return []
    return sorted(set(forged_names) - set(exclude_children))


def main():
    project_root = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
    manifest_path = Path(os.environ.get("MANIFEST", "")).resolve()
    if not manifest_path.is_file():
        sys.stderr.write(f"Manifest not found: {manifest_path}\n")
        sys.exit(3)

    m = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    updated_str = m.get("updated", "2026-01-01")
    try:
        updated = datetime.strptime(updated_str, "%Y-%m-%d")
    except ValueError:
        updated = datetime(2026, 1, 1)

    known_excluded_children = set()
    for entry in m.get("include", []):
        if entry["path"] == ".claude/skills/":
            for child in entry.get("exclude_children", []):
                known_excluded_children.add(child)

    # ── 7b: forged-skills registry cross-reference () ──────────────
    # exclude_children mirrors world/forged-skills.yaml by hand with no sync, so
    # a forged skill registered there but absent here LEAKS into the
    # domain-agnostic seed. Query the registry (source-of-truth) and diff. Reuse
    # the seed engine's resolver (read local world/ else WORLD_PATH from
    # agents/*/local-paths.conf) rather than duplicating it -- duplication is the
    # rot class this audit fixes. Fail-safe: any import/read failure leaves
    # forged_names=None, so the 7b audit silently no-ops (the mtime scan below
    # still runs).
    forged_names = None
    try:
        from _seed_engine import _dest_forged_skill_names
        forged_names = _dest_forged_skill_names(project_root)
    except Exception:
        forged_names = None
    leaking = forged_not_excluded(forged_names, known_excluded_children)
    if leaking:
        print(f"  ROT (7b): {len(leaking)} forged skill(s) in forged-skills.yaml "
              f"NOT in seed-manifest exclude_children -- WILL LEAK into the seed:")
        for name in leaking:
            print(f"    - {name}   [add to seed-manifest.yaml exclude_children]")
    elif forged_names is not None:
        print(f"  OK (7b): all {len(forged_names)} registered forged skills are "
              f"in exclude_children")

    # New skills
    skills_dir = project_root / ".claude" / "skills"
    new_skills = []
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                mtime = datetime.fromtimestamp(skill_md.stat().st_mtime)
            except OSError:
                continue
            if mtime > updated and child.name not in known_excluded_children:
                new_skills.append((child.name, mtime.date()))

    # New conventions
    conv_dir = project_root / "core" / "config" / "conventions"
    new_conventions = []
    if conv_dir.is_dir():
        for f in sorted(conv_dir.glob("*.md")):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:
                continue
            if mtime > updated:
                new_conventions.append((f.name, mtime.date()))

    if not new_skills and not new_conventions:
        print("  (no new files since manifest 'updated' date)")
        return

    if new_skills:
        print(f"  NEW SKILLS ({len(new_skills)}):")
        for name, date in new_skills[:20]:
            # Placement hint from the registry source-of-truth ( 7b):
            # a new skill IN the forged registry belongs in exclude_children;
            # one NOT in it is a base skill that belongs in include.
            if forged_names is None:
                tag = ""
            elif name in forged_names:
                tag = "  [forged -> exclude_children]"
            else:
                tag = "  [base -> include]"
            print(f"    - {name}  (mtime {date}){tag}")
        if len(new_skills) > 20:
            print(f"    ... and {len(new_skills) - 20} more")
    if new_conventions:
        print(f"  NEW CONVENTIONS ({len(new_conventions)}):")
        for name, date in new_conventions[:20]:
            print(f"    - {name}  (mtime {date})")
        if len(new_conventions) > 20:
            print(f"    ... and {len(new_conventions) - 20} more")


if __name__ == "__main__":
    main()
