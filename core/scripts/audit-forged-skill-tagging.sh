#!/usr/bin/env bash
# audit-forged-skill-tagging.sh — Forged-Skill Tagging consistency audit.
#
# Verifies bidirectional consistency between world/forged-skills.yaml and
# .claude/skills/<name>/SKILL.md `forged: true` front-matter tags:
#   - Every entry in forged-skills.yaml has `forged: true` in its SKILL.md
#   - Every SKILL.md with `forged: true` is registered in forged-skills.yaml
#
# Without the in-file tag, a packaging pass cannot distinguish framework-
# essential skills from forged-out domain skills.
#
# Exit: 0 = PASS, 1 = FAIL (drift detected), 2 = script error
#
# Origin: extracted from verify-learning/SKILL.md Section FST 2026-05-20
# so both /verify-learning AND seed-preflight can call it without duplication.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "ERROR: failed to source _paths.sh" >&2
    exit 2
}
# Convert to native Windows path for Python's import system (Git-Bash's
# /c/ mount notation isn't recognized by Windows Python's sys.path).
SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR" 2>/dev/null || echo "$SCRIPT_DIR")"

SCRIPT_DIR_NATIVE="$SCRIPT_DIR_NATIVE" py -3 -c "
import os, sys, re, pathlib
sys.path.insert(0, os.environ['SCRIPT_DIR_NATIVE'])
from _paths import WORLD_DIR, PROJECT_ROOT
try:
    import yaml
except ImportError:
    print('ERROR: PyYAML required', file=sys.stderr)
    sys.exit(2)

# A seed-role source (bare publication mirror) has no world overlay, so _paths
# leaves WORLD_DIR unset (None). There is no forged-skills registry to audit
# against, and the source's domain skills are stripped during transplant anyway,
# so bidirectional consistency is trivially satisfied. Treat as PASS (N/A) rather
# than crash. This is what lets a clean seed->downstream (PPE->prod) promotion
# pass WITHOUT --skip-preflight. (Without this guard, the next line raises
# TypeError: unsupported operand type(s) for /: 'NoneType' and 'str'.)
if not WORLD_DIR:
    print('INFO: no world overlay at source (seed-role mirror) — forged-skill registry audit N/A')
    sys.exit(0)

# Test seams (): point the audit at a tmp registry / skills dir.
_reg_env = os.environ.get('FORGED_SKILL_AUDIT_REGISTRY')
forged_path = pathlib.Path(_reg_env) if _reg_env else (WORLD_DIR / 'forged-skills.yaml')
if not forged_path.is_file():
    print(f'INFO: no forged-skills.yaml at {forged_path} — nothing to audit')
    sys.exit(0)

reg = yaml.safe_load(forged_path.read_text(encoding='utf-8')) or {}
registered = set((reg.get('skills') or {}).keys())

_skills_env = os.environ.get('FORGED_SKILL_AUDIT_SKILLS_DIR')
skills_dir = pathlib.Path(_skills_env) if _skills_env else (PROJECT_ROOT / '.claude' / 'skills')
present = {p.parent.name for p in skills_dir.glob('*/SKILL.md')}
tagged = {
    p.parent.name for p in skills_dir.glob('*/SKILL.md')
    if re.search(r'^forged:\s*true\b', p.read_text(encoding='utf-8'), re.MULTILINE)
}

# : a registry key with NO SKILL.md on this checkout is the registry
# running AHEAD of the repo — a peer forged the skill into the shared world
# registry and has not pushed the folder yet. Nothing absent can be
# mis-packaged, so that is a WARN (named, so the push can be chased), never a
# promotion-blocking FAIL. The FAIL set stays the actual leak risk: a folder
# that EXISTS without the in-file tag, or a tagged folder nobody registered.
# Measured 2026-09-03: two such entries withheld a release from staging for
# 2h on a box that owned neither.
untagged = (registered - tagged) & present
absent = (registered - tagged) - present
missing_reg = tagged - registered
ok = (not untagged and not missing_reg)

if absent:
    print(f'WARN: registered-but-absent on this checkout (peer has not pushed the folder yet — '
          f'push hygiene, not a packaging risk): {sorted(absent)}')
if ok:
    print(f'PASS: forged-skill tagging consistent ({len(tagged)} skills tagged, all in registry)')
    sys.exit(0)

print(f'FAIL: forged-skill tagging drift detected')
if untagged:
    print(f'  registered-but-untagged: {sorted(untagged)}')
if missing_reg:
    print(f'  tagged-but-unregistered: {sorted(missing_reg)}')
print(f'  fix per .claude/rules/forged-skill-resolution.md')
sys.exit(1)
"
