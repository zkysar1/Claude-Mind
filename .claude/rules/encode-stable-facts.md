# Encode Stable Facts

## Principle

Stable operational values — remote storage paths, account or environment
identifiers, endpoint URLs, connection strings, resource IDs — should be
retrieved, not rediscovered. An agent that runs multiple discovery commands
to find a value it has found before has failed to encode a stable fact.

## Scope

Applies to "resource locators": values that are stable across sessions and
required to access an external resource. Does NOT apply to transient values
(session tokens, one-off IDs), secrets (see `core/config/conventions/secrets.md`
and `.claude/rules/no-auto-memory.md`), or values already provided by
`agents/<agent>/local-paths.conf`.

## Rules

1. **Retrieve before discovering**: Before running shell/SSH/API commands
   to locate an external resource, check `world/conventions/` for a
   matching locator.
2. **Three-probe threshold**: If you find yourself running three or more
   discovery commands (SSH, ls, find, `describe-*`, `list-*`) to locate a
   single resource, STOP and check the locator convention first.
3. **Encode after discovering**: When discovery produces a value that will
   be needed again, append it to `world/conventions/<resource-kind>.md`
   before proceeding with the goal. Use Edit (not Write) for existing files.
4. **Record provenance**: Every locator entry must include how it was
   discovered (exact command, date, verifier). Without provenance, the next
   reader cannot re-verify freshness.
5. **Domain owns the file names**: The framework provides the schema;
   each domain chooses file names that match its resource kinds
   (e.g., `world/conventions/remote-paths.md`,
   `world/conventions/service-endpoints.md`).

## Anti-patterns

- Running six discovery commands to find a path, using it once, moving on
- Encoding a locator into working memory only (it vanishes at session end)
- Encoding into the knowledge tree as an article (locators are lookup
  data, not learned knowledge — mixing them bloats retrieval)
- Hard-coding discovered values inside a goal's execution step rather than
  a reusable convention file
- Re-running a `describe-*` or `list-*` command twice in one session to
  re-fetch the same value (a sign the value belongs in a locator convention)

**Detail:** `core/config/conventions/resource-locators.md` for the schema,
what counts as a locator, and the retrieval-before-discovery protocol.
