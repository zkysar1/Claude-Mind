#!/usr/bin/env bash
# Read and manage captured insights from <agent>/insights.jsonl.
#
# Usage:
#   insights-read.sh                    # unprocessed insights (JSON array)
#   insights-read.sh --all              # all insights (JSON array)
#   insights-read.sh --mark-processed   # mark all unprocessed as processed
#   insights-read.sh --count            # count of unprocessed insights
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

INSIGHTS_AGENT_DIR="$AGENT_DIR" python3 -c "
import json, os, sys
from pathlib import Path

agent_dir = os.environ.get('INSIGHTS_AGENT_DIR', '')
if not agent_dir:
    print('0' if len(sys.argv) > 1 and sys.argv[1] == '--count' else '[]')
    sys.exit(0)

filepath = Path(agent_dir) / 'insights.jsonl'
if not filepath.exists():
    print('0' if len(sys.argv) > 1 and sys.argv[1] == '--count' else '[]')
    sys.exit(0)

entries = []
for line in filepath.read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if line:
        entries.append(json.loads(line))

mode = sys.argv[1] if len(sys.argv) > 1 else ''

if mode == '--all':
    json.dump(entries, sys.stdout, indent=2, ensure_ascii=False)
elif mode == '--count':
    print(sum(1 for e in entries if not e.get('processed', False)))
elif mode == '--mark-processed':
    for e in entries:
        e['processed'] = True
    with open(filepath, 'w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')
    print(f'Marked {len(entries)} insights as processed')
else:
    unprocessed = [e for e in entries if not e.get('processed', False)]
    json.dump(unprocessed, sys.stdout, indent=2, ensure_ascii=False)
" "$@"
