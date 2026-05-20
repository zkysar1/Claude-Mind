#!/usr/bin/env python3
"""skill-frontmatter-audit.py — verify every `.claude/skills/*/SKILL.md`
parses with a non-empty `name` field via `_skill_md.parse_front_matter`.

Catches the regression class where SKILL.md front matter becomes silently
unparseable — e.g., an HTML comment or any non-`---` content inserted above
the front matter delimiter (see `_skill_md.py:_FRONT_MATTER_RE` for why
`\\A---` is strict). When this happens, every consumer of
`parse_front_matter` (capability-gate, blocker-create-gate, Claude Code's
own skill loader) silently sees empty metadata; the only visible symptom
is the skill list showing leading content as the description.

Exit codes:
  0  all SKILL.md files parse with non-empty `name`
  1  one or more files have unparseable or missing-name front matter
"""

import json
import sys
from pathlib import Path

from _paths import PROJECT_ROOT
from _skill_md import parse_front_matter

SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"


def audit():
    failures = []
    total = 0
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        total += 1
        fm = parse_front_matter(skill_md)
        name = fm.get("name") if isinstance(fm, dict) else None
        if not isinstance(name, str) or not name.strip():
            failures.append({
                "path": str(skill_md.relative_to(PROJECT_ROOT)),
                "reason": "front matter unparseable or missing/empty `name`",
                "parsed_keys": sorted(fm.keys()) if isinstance(fm, dict) else [],
            })
    return total, failures


def main():
    total, failures = audit()
    out = {"total": total, "failures": failures, "would_block": bool(failures)}
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        if not failures:
            print(f"PASS: all {total} SKILL.md files parse with non-empty name")
        else:
            print(f"FAIL: {len(failures)} of {total} SKILL.md have broken front matter:")
            for f in failures:
                print(f"  {f['path']}: {f['reason']} (parsed_keys={f['parsed_keys']})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
