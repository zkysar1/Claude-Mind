#!/usr/bin/env bash
# domain-leak-exempt: this scanner's PATTERNS array is a sentinel list of the EXACT
# leak strings the framework wants to find at the destination after transformations.
# Transformations (especially G1/G2 — X-Ayoai-→X-Mind-, AYOAI_→MIND_) MUST NOT rewrite
# these patterns, or the scanner will look for the post-transform values at destination
# and report the GOOD (correctly-renamed) tokens as leaks. The marker exempts this whole
# file from G1/G2/G3/G4 transformations during /seed transplant.
# /seed verify check 7 — scan destination for source-repo path self-references.
#
# Usage: seed-path-self-reference-scan.sh <destination>
#
# Exit codes:
#   0 — clean (no references found)
#   1 — references found (printed to stdout)
#   2 — script error
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEST="${1:-}"
if [ -z "$DEST" ] || [ ! -d "$DEST" ]; then
    echo "Usage: seed-path-self-reference-scan.sh <destination>" >&2
    exit 2
fi
DEST="$(cd "$DEST" && pwd)"

# Patterns that should NOT survive into a clean seed at destination
PATTERNS=(
    "Ayoai-Mind"
    "ZakNoCloud"
    "Zachary"
    "AyoAI"
    "AYOAI_"
    "X-Ayoai-"
)

HITS=0
for pat in "${PATTERNS[@]}"; do
    # Skip our own scanning script and the seed templates (which legitimately mention these tokens)
    MATCHES="$(grep -rln --include='*.md' --include='*.py' --include='*.sh' --include='*.yaml' --include='*.json' \
        --exclude-dir='.git' --exclude-dir='node_modules' --exclude-dir='.history' \
        --exclude-dir='seed-templates' \
        "$pat" "$DEST" 2>/dev/null || true)"
    if [ -n "$MATCHES" ]; then
        # Filter out:
        #  - .seed-backup-* dirs (pre-transplant backups)
        #  - self-references in seed source / manifest
        #  - .claude/settings.local.json (per-machine, NOT shipped via manifest — pre-existing at destination)
        #  - .env.local (per-deployment secrets file, not shipped)
        #  - per-deployment session files that pre-exist at destination
        #  - files carrying the `domain-leak-exempt:` marker (intentional domain
        #    references in regex patterns, sentinels, fixtures — see
        #    `_seed_transforms.py::_has_domain_leak_exempt_marker`)
        FILTERED="$(echo "$MATCHES" \
            | grep -v "\.seed-backup-" \
            | grep -v "seed-path-self-reference-scan.sh" \
            | grep -v "/seed-manifest.yaml" \
            | grep -v "/settings.local.json" \
            | grep -v "/\.env\.local" \
            | grep -v "/active-agent-" \
            || true)"
        # Further filter: drop files that carry the domain-leak-exempt marker
        if [ -n "$FILTERED" ]; then
            FINAL=""
            while IFS= read -r f; do
                if [ -n "$f" ] && [ -f "$f" ]; then
                    if grep -q "domain-leak-exempt:" "$f" 2>/dev/null; then
                        continue
                    fi
                    FINAL="${FINAL}${f}
"
                fi
            done <<<"$FILTERED"
            FILTERED="${FINAL%$'\n'}"
        fi
        if [ -n "$FILTERED" ]; then
            echo "  Pattern '$pat' found in:"
            echo "$FILTERED" | sed 's/^/    /'
            HITS=$((HITS + 1))
        fi
    fi
done

if [ $HITS -eq 0 ]; then
    exit 0
fi
exit 1
