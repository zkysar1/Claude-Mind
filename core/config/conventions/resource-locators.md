# Resource Locators Convention

A resource locator is a stable operational value required to access an
external resource: a path on remote storage, an account or environment
identifier, an endpoint URL, a connection string, a resource ID. Locators
are discovered once and reused across sessions. This convention defines
the schema, the retrieval-before-discovery protocol, and how locators
relate to other stores.

## What Counts as a Locator

A value IS a locator when it is:
- Stable across sessions (same value next month unless infrastructure changes)
- Required to access an external resource
- Discoverable by a reproducible command or lookup
- Not secret (see `secrets.md` for credential handling)

A value is NOT a locator when it is:
- Session-scoped (tokens, one-off IDs, in-flight request IDs)
- A behavior lesson or outcome pattern (use reasoning bank)
- A domain concept or learned fact (use knowledge tree)
- A safety rule (use guardrails)
- An agent-configured path already in `agents/<agent>/local-paths.conf`

## Relationship to Other Stores

| Store                          | Holds                                       | Retrieval        |
|--------------------------------|---------------------------------------------|------------------|
| Knowledge tree                 | Articles, concepts, learned domain knowledge| Slow, semantic   |
| Reasoning bank                 | Lessons, success/failure patterns           | Episodic         |
| Guardrails                     | Safety rules and enforcement                | Trigger-based    |
| Working memory                 | Session-scoped facts                        | Ephemeral        |
| `agents/<agent>/local-paths.conf`     | Agent's world/meta paths                    | Bootstrap-only   |
| **Locator conventions**        | **Stable external-resource values**         | **Fast key lookup** |

Locator conventions fill the gap between working memory (too ephemeral)
and the knowledge tree (too slow for direct lookup). They are the
"stable-fact encoding lane."

## File Layout

Locators live under `world/conventions/` with file names chosen by the
domain. One file per resource kind. Example names (illustrative — each
domain picks its own):

- `world/conventions/remote-paths.md` — paths on shared or remote storage
- `world/conventions/service-endpoints.md` — API endpoints and URLs
- `world/conventions/account-ids.md` — environment and account identifiers
- `world/conventions/connection-strings.md` — database and queue connection strings

A single file should cover values with the same schema and audit cadence.
Mixing unrelated kinds (endpoints + remote paths in the same file) reduces
retrievability.

## Entry Schema

Each locator entry is a markdown section with this structure:

```markdown
## {stable-key-name}

- **Value**: `{the locator value}`
- **Kind**: {path | endpoint | identifier | connection-string | code-location}
- **Scope**: {production | staging | dev | local}
- **Discovered**: {YYYY-MM-DD} via `{exact command or steps}`
- **Last verified**: {YYYY-MM-DD}
- **Notes**: {optional — what this resource holds, who uses it}
```

The stable-key-name is the lookup handle (e.g., `prod-data-root`,
`widget-service-endpoint`). It is NOT the value itself, and it should be
descriptive enough to be meaningful without reading `Notes`.

### Kind `code-location` — remote-primary, REQUIRED fields

A locator that names **where source code lives** uses `Kind: code-location`
and carries two extra fields. Both are REQUIRED; an entry missing either is
not a locator (see "A path without a machine is a note" below):

```markdown
## {stable-key-name}

- **Remote**: `{canonical clone URL}`      # PRIMARY — true on every box
- **Kind**: code-location
- **Local path**: `{absolute path}` **on** `{machine identifier}`   # per-box convenience
- **Discovered**: {YYYY-MM-DD} via `{exact command}`
- **Last verified**: {YYYY-MM-DD}
- **Notes**: {what this code is, which sub-paths matter}
```

Worked example (generic):

```markdown
## widget-service-code

- **Remote**: `https://github.com/acme/widget-service.git`
- **Kind**: code-location
- **Local path**: `/opt/src/widget-service` **on** `build-host-01`
- **Discovered**: 2026-01-15 via `git -C /opt/src/widget-service remote -v`
- **Last verified**: 2026-01-15
- **Notes**: service handlers under `src/handlers/`; tests under `tests/`.
```

**`Remote` is the primary field and `Local path` is secondary.** The remote is
the only value that is true from every box; a local path is true on exactly one
machine and silently false everywhere else. Write the remote first so a reader
scanning the entry reaches the universal value before the box-scoped one.

**A path without a machine is a note, not a locator.** An absolute path recorded
with no machine identifier is *implicitly box-scoped while reading as though it
were universal* — which is the whole defect. If you cannot name the box a path
was verified on, you do not know the path; record the remote and omit the path.

Multiple boxes hold the same checkout at different roots. List them as separate
`Local path` lines, each with its own machine — never collapse them into one
"the path" line, and never assume another box uses the same root.

## Retrieval-Before-Discovery Protocol

Before running discovery commands for an external resource:

1. **Identify the resource kind**: path, endpoint, identifier, etc.
2. **Read the matching convention file**: Glob `world/conventions/*.md`
   or use `world-cat.sh conventions/{file}.md`.
3. **Look up by key**: if the key exists, use the value. See the
   `Freshness and Re-verification` section for when to re-verify.
4. **If missing**: run discovery, then encode (see below).
5. **If the convention file does not exist**: create it the first time
   you encode a value. The framework does not pre-create domain files.

## Encoding Protocol

After successful discovery:

1. Determine the target file. Use an existing file if the schema matches.
   Create a new file only if the resource kind is new.
2. Add a new `## {key}` section with the full schema above. Use `Write`
   for a new file, `Edit` for an existing one.
3. Include the exact discovery command in the `Discovered` field. The
   next reader must be able to re-verify independently.
4. Set `Last verified` to today.
5. Commit the change if the domain's post-execution convention runs
   commit-and-push. Locator conventions are team-visible — other agents
   benefit from the encoding.

## Freshness and Re-verification

A locator with `Last verified` older than 30 days should be re-verified
before use if the goal is high-cost or production-touching. A re-verify
is the same discovery command rerun; success bumps `Last verified`.

Failed re-verify means the value has drifted. Mark the entry with
`**Status**: stale` and encode the new value as a fresh entry. Do not
silently overwrite — provenance matters.

## What NOT to Do

- Do not put secrets in locator conventions. Secrets live in `.env.local`
  (see `secrets.md`).
- Do not encode session-scoped values (temporary tokens, one-off IDs).
  Those belong in working memory and vanish at session end.
- Do not duplicate values across multiple convention files. One
  canonical entry per locator; cross-reference by key if needed.
- Do not encode into the knowledge tree. Tree nodes are articles with
  confidence scores; locators are lookup data and do not need scoring.
- **Do not record a code path without a machine identifier.** It reads as
  universal and is true on one box. Add the machine, or record only the remote.
- **Do not report code as unreachable because a local path is missing.** A
  missing local path means *this box has no checkout yet* — the correct response
  is `git clone <remote>` (or `git fetch` in an existing checkout), then re-read.
  Reporting "I cannot see X" from one failed local stat is a capability-absence
  claim drawn from a single silent signal, which
  `.claude/rules/verify-before-assuming.md` forbids: the remote is the second
  signal, and it is in the entry. **A locator lookup that yields a path you do
  not have has SUCCEEDED, not failed** — it told you what to clone.

## Enforcement Points

1. **Three-probe threshold** (inline): When the agent finds itself running
   three or more discovery commands for one resource, it checks the
   locator convention before continuing.
2. **Post-execution encoding**: After a goal whose execution discovered
   a reusable locator, the agent edits the matching convention file
   before declaring completion.
3. **Pre-execution retrieval**: Skills that access external resources
   consult `world/conventions/` as part of their prime/setup step.

## Consumers

This convention is referenced by:
- `.claude/rules/encode-stable-facts.md` — the trigger rule
- Any skill that performs external-resource discovery (skills that access
  remote storage, external services, or deployed environments)

## Seeding

The framework does not seed any domain files. The first encoding by any
agent creates the file. The `world/conventions/` directory is domain-owned.
