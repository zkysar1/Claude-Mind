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
            print(f"    - {name}  (mtime {date})")
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
