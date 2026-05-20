#!/usr/bin/env bash
# audit-aspirations-templates-clean.sh — Agent-Aspirations Template Cleanliness.
#
# The two starter templates seed a brand-new agent's first 5-10 goals:
#   - core/config/agent-aspirations-initial.jsonl
#   - core/config/agent-aspirations-onboard.jsonl
# Domain residue in these files leaks deployment-specific work into every
# fresh agent (e.g., a brand-new agent inheriting an audit goal for some
# other deployment's service on day 1).
#
# The check matches the live deployment's domain-term blocklist against
# both templates — any hit is a regression of the publication cleanup.
# Domain-specific starter goals belong in the host's own world overlay,
# not in core templates.
#
# Exit: 0 = PASS, 1 = FAIL (domain residue found), 2 = script error
#
# Origin: extracted from verify-learning/SKILL.md Section TPL 2026-05-20
# so both /verify-learning AND seed-preflight can call it without duplication.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || {
    echo "ERROR: failed to source _paths.sh" >&2
    exit 2
}
SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR" 2>/dev/null || echo "$SCRIPT_DIR")"

SCRIPT_DIR_NATIVE="$SCRIPT_DIR_NATIVE" py -3 -c "
import os, re, sys, pathlib
sys.path.insert(0, os.environ['SCRIPT_DIR_NATIVE'])
from _paths import PROJECT_ROOT

blocklist_path = PROJECT_ROOT / 'core' / 'config' / 'domain-term-blocklist.txt'
if not blocklist_path.is_file():
    print(f'ERROR: blocklist missing at {blocklist_path}', file=sys.stderr)
    sys.exit(2)

terms = [
    ln.strip() for ln in blocklist_path.read_text(encoding='utf-8').splitlines()
    if ln.strip() and not ln.startswith('#')
]

targets = [
    PROJECT_ROOT / 'core' / 'config' / 'agent-aspirations-initial.jsonl',
    PROJECT_ROOT / 'core' / 'config' / 'agent-aspirations-onboard.jsonl',
]

hits = []
scanned = 0
for t in targets:
    if not t.is_file():
        continue
    scanned += 1
    content = t.read_text(encoding='utf-8')
    for term in terms:
        if re.search(r'\b' + re.escape(term) + r'\b', content):
            hits.append((str(t.relative_to(PROJECT_ROOT)), term))

if not hits:
    print(f'PASS: agent-aspirations templates clean ({scanned} files scanned, 0 domain leaks)')
    sys.exit(0)

print(f'FAIL: domain residue in starter templates:')
for path, term in hits[:10]:
    print(f'  {path}: \"{term}\"')
if len(hits) > 10:
    print(f'  ... and {len(hits) - 10} more')
print(f'  domain-specific starter goals belong in world overlay, not core templates')
sys.exit(1)
"
