# Verify Before Assuming

## Principle

Never accept a negative conclusion from a single signal. Negative conclusions are
claims that something CAN'T be done, IS broken, DOESN'T work, or ISN'T available.
They are uniquely dangerous because they prevent work.

## Rules

1. **Multi-signal requirement**: A negative conclusion requires 2+ independent
   verification signals before acceptance. "Independent" means different tools,
   different endpoints, or different evidence types.
2. **Cost-proportional verification**: If the conclusion blocks multiple goals
   or hours of work, require more signals and try harder to disprove it.
3. **Infrastructure-specific**: MUST NOT declare infrastructure unavailable
   without running `infra-health.sh check <component>`.
4. **Silent failure awareness**: Commands with silent-failure flags (`-sf`, `-q`,
   `2>/dev/null`) are ZERO signals, not one. A silently-failed command that
   returns empty output has told you nothing.

## Anti-patterns

- One failed curl = "it's down"
- `curl -sf` returns empty = "service not running" (silent 404 ≠ connection refused)
- SSH connection refused = "server is down" (could be stale host key)
- "I tried and it didn't work" without trying an alternative approach
- One tree search = "it's not built" (search multiple queries, categories, and data stores)

**Detail:** `core/config/conventions/negative-conclusions.md` for enforcement points,
verification tiers, and silent failure catalog.

**Knowledge-specific:** `core/config/conventions/exhaustive-search-before-negation.md`
for the exhaustive knowledge search protocol before concluding something doesn't exist.
