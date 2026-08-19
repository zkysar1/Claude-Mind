#!/usr/bin/env bash
# Read-only diagnostic: scan the journal for "OBLIGATION ABBREVIATED" claims
# gated on context_budget.zone == tight, and check whether each claim is
# accompanied by a context-budget banner line in the same journal entry.
#
# A "banner line" is any line matching `CTX: raw \d+% | of-autocompact \d+% |`.
# See core/scripts/context-budget-banner.sh for the canonical shape.
#
# Usage:
#   bash core/scripts/context-citation-audit.sh                # last 24h (default)
#   bash core/scripts/context-citation-audit.sh --days 7       # last 7 days
#   bash core/scripts/context-citation-audit.sh --json         # machine-readable
#
# Exit 0 always (diagnostic, not a gate). The numbers inform whether to
# escalate to Level 3 (auto-log non-compliance) or Level 4 (pre-turn block).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_paths.sh"

days=1
emit_json=false
while [ $# -gt 0 ]; do
  case "$1" in
    --days) days="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
    --json) emit_json=true; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

journal_dir="$AGENT_DIR/journal"
if [ ! -d "$journal_dir" ]; then
  echo "no journal directory at $journal_dir — nothing to audit"
  exit 0
fi

python3 - "$journal_dir" "$days" "$emit_json" <<'PY'
import json, re, sys, os
from datetime import date, timedelta
from pathlib import Path

journal_dir = Path(sys.argv[1])
days = int(sys.argv[2])
emit_json = sys.argv[3] == "true"

# Build candidate file list — daily journal markdowns from the last N days.
today = date.today()
candidates = []
for delta in range(days):
    d = today - timedelta(days=delta)
    p = journal_dir / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.isoformat()}.md"
    if p.is_file():
        candidates.append(p)

claim_re = re.compile(r"OBLIGATION ABBREVIATED:\s*(\w+)\s*[—-]\s*([^\n]+)")
# Banner shape is defined by core/scripts/context-budget-banner.sh — keep this
# pattern aligned with its print() format. We capture the zone field so we can
# verify it supports the claim (a banner quoting zone=fresh alongside a
# zone==tight claim is NOT a valid citation).
banner_re = re.compile(
    r"CTX:\s*raw\s*\d+%\s*\|\s*of-autocompact\s*\d+%\s*\|\s*zone\s+(\w+)\s*\|"
)

claims = 0
tight_claims = 0
cited = 0
non_cited_samples = []

for p in candidates:
    text = p.read_text(encoding="utf-8", errors="replace")
    # Split per-iteration block on any markdown heading level. Real journals
    # use `## HH:MM - Goal: ...`, synthetic tests may use `# ...`. Matching
    # `^#+\s` covers both without over-splitting on mid-line hash characters.
    blocks = re.split(r"^#+\s", text, flags=re.MULTILINE)
    for block in blocks:
        matches = list(claim_re.finditer(block))
        for i, m in enumerate(matches):
            obligation = m.group(1)
            condition = m.group(2).strip()
            claims += 1
            if "context_budget.zone" in condition and "tight" in condition:
                tight_claims += 1
                # Per-OBLIGATION scope: search only from this claim's position
                # up to the next OBLIGATION ABBREVIATED in the same block (or
                # end-of-block). Without this scoping one banner would be
                # credited to every OBLIGATION in the block — matches Phase
                # 9.5d's neighborhood logic in aspirations-learning-gate.
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
                neighborhood = block[start:end]
                banner_match = banner_re.search(neighborhood)
                banner_zone = banner_match.group(1) if banner_match else None
                if banner_zone == "tight":
                    cited += 1
                else:
                    non_cited_samples.append({
                        "file": str(p.relative_to(journal_dir.parent)),
                        "obligation": obligation,
                        "condition": condition[:80],
                        "banner_zone_found": banner_zone,  # None = no banner; other value = contradicting banner
                    })

rate = (cited / tight_claims * 100) if tight_claims else None

result = {
    "days_scanned": days,
    "files_scanned": len(candidates),
    "total_abbreviation_claims": claims,
    "tight_zone_claims": tight_claims,
    "banner_cited": cited,
    "banner_missing": tight_claims - cited,
    "citation_rate_pct": round(rate, 1) if rate is not None else None,
    "non_cited_samples": non_cited_samples[:5],
}

if emit_json:
    print(json.dumps(result, indent=2))
else:
    # ASCII-only in console output; Windows cp1252 mangles em-dash at the
    # Bash-tool boundary. JSON mode keeps Unicode (json.dumps escapes).
    print(f"Context-citation audit - last {days} day(s)")
    print(f"  files scanned:         {len(candidates)}")
    print(f"  abbreviation claims:   {claims}")
    print(f"  tight-zone claims:     {tight_claims}")
    if tight_claims:
        print(f"  banner cited:          {cited} ({rate:.1f}%)")
        print(f"  banner missing/wrong:  {tight_claims - cited}")
        if non_cited_samples:
            print("  sample problems:")
            for s in non_cited_samples[:3]:
                bz = s["banner_zone_found"]
                tag = "no banner" if bz is None else f"banner says zone={bz}"
                print(f"    - {s['file']}: {s['obligation']} [{tag}]: {s['condition']}")
    else:
        print("  (no tight-zone claims found; nothing to audit yet)")
PY
