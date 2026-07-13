# Archive Before Delete (MANDATORY)

## Principle

No destructive operation on a data store proceeds until an independent,
integrity-verified archive of the affected data exists OUTSIDE the blast
radius. Authorization to delete is not permission to delete-first: an
authorized deletion without a verified archive is still a protocol
violation. Deletion is the LAST step of a retirement, never the first.

## Scope

Any operation that removes or overwrites records the system cannot trivially
regenerate:

- `rm -rf` (or scripted deletion) of agent dirs, world/meta subtrees, or stores
- Remote object-store deletion (bulk or single-key deletes, lifecycle-triggering
  rewrites)
- Database row/table deletion (delete-item calls, drops, truncations)
- Bulk store rewrites that DROP records (JSONL filter-rewrites, dedup passes)
- Retiring an agent (graft/kill) — the composite case containing all of the above

Does NOT apply to: content in `agents/<agent>/temp/` scratch being cleaned by
its owner, tmp files this session created, or append-only writes.

## The Protocol (ENUMERATE → VERIFY LAYERS → ARCHIVE → VERIFY ARCHIVE → DELETE → RECEIPT)

1. **Enumerate** exactly what will be destroyed: full key/path list, object
   count, total bytes, per-item checksum where available. Persist the
   enumeration — it is the future integrity baseline.
2. **Verify recovery layers BEFORE the destructive step — read the config,
   don't assume.** Soft-delete, versioning, and trash folders are NOT
   archives until the retention configuration says so: READ the storage
   layer's lifecycle/retention/expiry rules. Canonical incident (2026-07-07,
   rb-2859): a purge of 2,461 retired-agent objects from the remote store
   relied on "the store is versioned"; the store's retention policy would
   have permanently expired every noncurrent version in 90 days. "Versioned"
   meant "delayed permanent deletion", not "archived".
3. **Archive independently, outside the blast radius.** COPY (never move) to
   a location that (a) the live system does not read, sync, or restore from,
   and (b) no retention clock touches: a cold archive prefix outside the
   governed roots (e.g. `<env-prefix>/graveyard/<date>-<event>/...`), a git
   snapshot commit for tracked files, or an offline tarball. Where
   noncurrent-version expiry rules exist, current-version copies are the
   retention-immune form.
4. **Verify the archive against the enumeration**: object count, total
   bytes, and per-object checksums must ALL match. A sampled spot-check is
   not verification.
5. **Only then delete.** Prefer tombstone/move-aside over hard delete when
   the storage layer supports it. Batch delete APIs may be permission-denied
   where single deletes succeed (observed in the canonical incident) —
   degrade to sequential deletes, never to broader-permission workarounds.
6. **Write a RECEIPT stored WITH the archive**: what was deleted, why, when,
   by whom, the enumeration with checksums, and step-by-step restore
   instructions — including where NOT to restore to (restoring into live
   paths can re-arm read-through resurrection). Record the receipt location
   in a durable retrievable store (knowledge tree node + reasoning bank).
7. **Blast-radius check before the delete fires**: enumerate what READS this
   data. Read-through/restore-on-miss sync layers, session-binding caches,
   and registry rows can re-materialize or depend on "deleted" data (the
   donor-agent resurrection class, rb-2859).

## Anti-patterns

- Deleting first and verifying the recovery layer afterward (the exact
  ordering failure the canonical incident demonstrates)
- "The store is versioned, so it's safe" without reading its retention rules
- Treating user authorization ("go ahead and purge") as waiving the archive
  step — authorization sets the GOAL; this protocol sets the METHOD
- Archiving by moving (a move is a delete of the original)
- Verifying by sample instead of full count+bytes+checksum
- No receipt: an archive nobody can find or restore from is not an archive

## Cross-references

- rb-2859 — agent-retirement (graft/kill) checklist; carries the 2026-07-07
  incident trace, the archive location, and the receipt path
- `.claude/rules/verify-before-assuming.md` — "versioned = archived" was an
  unverified positive claim; recovery-layer capability requires reading the
  config, not assuming it
- `core/config/conventions/coordination.md` — multi-agent claim/registry
  surfaces that must be purged (not orphaned) at agent retirement
