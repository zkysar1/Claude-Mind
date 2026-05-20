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

forged_path = WORLD_DIR / 'forged-skills.yaml'
if not forged_path.is_file():
    print(f'INFO: no forged-skills.yaml at {forged_path} — nothing to audit')
    sys.exit(0)

reg = yaml.safe_load(forged_path.read_text(encoding='utf-8')) or {}
registered = set((reg.get('skills') or {}).keys())

skills_dir = PROJECT_ROOT / '.claude' / 'skills'
tagged = {
    p.parent.name for p in skills_dir.glob('*/SKILL.md')
    if re.search(r'^forged:\s*true\b', p.read_text(encoding='utf-8'), re.MULTILINE)
}

missing_tag = registered - tagged
missing_reg = tagged - registered
ok = (not missing_tag and not missing_reg)

if ok:
    print(f'PASS: forged-skill tagging consistent ({len(tagged)} skills tagged, all in registry)')
    sys.exit(0)

print(f'FAIL: forged-skill tagging drift detected')
if missing_tag:
    print(f'  registered-but-untagged: {sorted(missing_tag)}')
if missing_reg:
    print(f'  tagged-but-unregistered: {sorted(missing_reg)}')
print(f'  fix per .claude/rules/forged-skill-resolution.md')
sys.exit(1)
"
