"""Shared helpers for parsing .claude/skills/*/SKILL.md front matter.

Extracted so that capability-gate.py and blocker-create-gate.py can share a
single regex + YAML parse path. The front matter is a YAML block delimited by
`---` lines at the top of the file. Everything after the second `---` is the
markdown body and is NOT parsed here.

Fail-open: any read/parse error yields an empty dict or empty list, never an
exception that would take down a calling gate. Silent blocking from framework
bugs would be worse than the retrieval miss this helper is meant to surface.
A stderr warning fires only when the file CONTAINS `---` somewhere but not at
line 1 — the canonical "almost-front-matter" shape that means a bug, not
"file has no front matter at all" (which is the normal fail-open path).
"""

import re
import sys
from pathlib import Path

import yaml  # PyYAML — same hard dependency as capability-gate.py

# Per-process dedup so the same file doesn't spam stderr across every consumer
# call (capability-gate, blocker-create-gate, audits often hit the same path).
_WARNED_PATHS: set = set()
_BROKEN_FRONTMATTER_RE = re.compile(r"^---\s*$", re.MULTILINE)

# DO NOT relax the `\A` anchor — it requires `---` on line 1. Anything inserted
# above it (HTML comment, blank line) silently breaks front-matter parsing for
# EVERY consumer (capability-gate, blocker-create-gate, Claude Code's own skill
# loader). Per-file metadata comments belong INSIDE the front matter as YAML
# `#`-comments. Regression caught 2026-05-11 by the fresh-eyes review when
# 7 SKILL.md files had `<!-- domain-leak-exempt -->` HTML comments inserted
# above `---` by an LLM session that pattern-matched marker placement from
# `.claude/rules/probe-before-defer.md` (a rule file with no front matter,
# where HTML-comment-at-top is valid) and copy-pasted it to SKILL.md files
# (which DO have front matter and break under strict-anchor parsing). The
# commit message at the time misattributed it to a "skill-loading subsystem"
# — no such subsystem exists; the misattribution was an LLM confabulation.
# Placement convention now documented in `.claude/rules/domain-free-examples.md`
# "Marker Placement" section.
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)


def parse_front_matter(skill_path) -> dict:
    """Return parsed YAML front matter as a dict; {} on any error.

    skill_path may be str or pathlib.Path. Does NOT require the file exist —
    a missing file is treated as "no front matter" and yields {}.
    """
    p = Path(skill_path)
    if not p.is_file():
        return {}
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = _FRONT_MATTER_RE.match(raw)
    if not m:
        # Distinguish "no front matter at all" (silent {}) from "broken front
        # matter" (warn once). If the file contains a `---` line anywhere but
        # the strict \A---  anchor didn't match, content was inserted above
        # the delimiter — every consumer now sees empty metadata silently.
        path_str = str(p)
        if path_str not in _WARNED_PATHS and _BROKEN_FRONTMATTER_RE.search(raw):
            _WARNED_PATHS.add(path_str)
            sys.stderr.write(
                f"_skill_md WARNING: {p} contains '---' but not at line 1 — "
                "front matter unparseable. Move leading content INSIDE the "
                "block as YAML `#`-comments (see _FRONT_MATTER_RE comment).\n"
            )
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def get_triggers(skill_path) -> list:
    """Convenience accessor: list of trigger strings from a SKILL.md."""
    fm = parse_front_matter(skill_path)
    return list(fm.get("triggers") or [])


def get_companion_scripts(skill_path) -> list:
    """Convenience accessor: list of companion_scripts from a SKILL.md.

    Used by the blocker-create gate to check whether a probe command invokes
    the skill's canonical companion script (rb-226 / guard-147 enforcement).
    """
    fm = parse_front_matter(skill_path)
    return list(fm.get("companion_scripts") or [])


def find_skill_md(skills_dir, skill_name) -> Path:
    """Resolve a skill name (without leading /) to its SKILL.md path.

    Returns None if the skill dir or SKILL.md doesn't exist. Does NOT strip
    a leading slash — the caller's responsibility to pass a clean name.
    """
    p = Path(skills_dir) / skill_name / "SKILL.md"
    return p if p.is_file() else None
