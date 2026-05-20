#!/usr/bin/env bash
# audit-toplevel-allowlist.sh — Core Framework Entries surveillance.
#
# The path-resolution L1 hook covers WORLD/META/agent-dir top-level cruft
# but excludes core/ and .claude/ by design (those are git-tracked, so
# cruft surfaces in `git status`). This check is the periodic surveillance
# pass: enumerate top-level entries of core/ and .claude/ and refuse any
# not on the framework's allowlist.
#
# A new top-level entry indicates either:
#   (a) intentional framework extension that needs the allowlist updated, or
#   (b) cruft that bypassed Write/Edit/MultiEdit via Bash redirect/touch/cp/mkdir.
#
# Either way the human needs to see it. Catches the same failure mode as
# the L1 hook's WORLD-side check, but at audit cadence rather than write time.
# Gitignored entries are skipped (intentional caches, runtime artifacts).
#
# Exit: 0 = PASS, 1 = FAIL (unexpected entries), 2 = script error
#
# Origin: extracted from verify-learning/SKILL.md Section CFE 2026-05-20
# so both /verify-learning AND seed-preflight can call it without duplication.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "ERROR: failed to source _paths.sh" >&2
    exit 2
}
SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR" 2>/dev/null || echo "$SCRIPT_DIR")"

SCRIPT_DIR_NATIVE="$SCRIPT_DIR_NATIVE" py -3 -c "
import os, sys, subprocess, pathlib
sys.path.insert(0, os.environ['SCRIPT_DIR_NATIVE'])
from _paths import PROJECT_ROOT

allow = {
    'core': {'BOUNDARY.md', 'config', 'runtime', 'scripts', 'githooks', 'logs', 'tests'},
    '.claude': {
        'rules', 'skills', 'settings.json', 'settings.local.json', '_tree.yaml',
        'agents', 'hooks', 'memory', 'output-styles', 'plugins', 'statusline',
        'worktrees', 'scheduled_tasks.lock',
    },
}

# Collect gitignored entries (so caches/build artifacts get skipped)
ignored = set()
try:
    result = subprocess.run(
        ['git', 'ls-files', '--others', '--ignored', '--exclude-standard',
         '--directory', '-z', 'core', '.claude'],
        capture_output=True, text=True, encoding='utf-8',
        cwd=str(PROJECT_ROOT),
    )
    for p in result.stdout.split('\0'):
        if p:
            ignored.add(p)
except Exception:
    pass  # If git fails, fail open (still report unexpected entries)

hits = []
for root, allowed in allow.items():
    root_path = PROJECT_ROOT / root
    if not root_path.is_dir():
        continue
    for p in root_path.iterdir():
        name = p.name
        if name in allowed:
            continue
        if name.startswith('.'):
            continue  # dotfiles ignored
        rel = f'{root}/{name}'
        if rel in ignored or f'{rel}/' in ignored:
            continue
        hits.append(rel)

if not hits:
    print(f'PASS: core/ and .claude/ top-level entries on allowlist (gitignored entries skipped)')
    sys.exit(0)

print(f'FAIL: unexpected top-level entries:')
for h in hits[:10]:
    print(f'  {h}')
if len(hits) > 10:
    print(f'  ... and {len(hits) - 10} more')
print(f'  either extend the Section CFE allowlist (intentional addition)')
print(f'  or remove the cruft (Bash redirect/touch/cp/mkdir bypassing file-save gate)')
sys.exit(1)
"
