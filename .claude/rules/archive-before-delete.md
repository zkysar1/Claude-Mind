---
description: "No destructive store op before an integrity-verified archive exists outside the blast radius; authorization never means delete-first."
---

# Archive Before Delete (MANDATORY)

The incidents and measured cases behind steps 2, 3(c) and 6 and the
adjacent-backup anti-pattern live in `core/config/rationale/archive-before-delete.md`.
This file keeps the imperatives.

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
   don't assume.** "Versioned" is not "archived" until the retention config
   says so (rb-2859). **Unreadable config = UNVERIFIABLE = ABSENT**
   (g-115-2692, guard-1787): the step 3 archive becomes MANDATORY.
   Detail: `core/config/rationale/archive-before-delete.md`.
3. **Archive independently, outside the blast radius.** COPY (never move) to
   a location that (a) the live system does not read, sync, or restore from,
   and (b) no retention clock touches: a cold archive prefix outside the
   governed roots (e.g. `<env-prefix>/graveyard/<date>-<event>/...`), a git
   snapshot commit for tracked files, or an offline tarball. Where
   noncurrent-version expiry rules exist, current-version copies are the
   retention-immune form.
   **(c) STAGING is part of the choice, and the obvious spot is the worst
   one.** `agents/<agent>/temp/` is git-ignored ENTIRELY (`agents/*/temp/*`,
   only `.gitkeep` re-included) and purged after 120 minutes.
   If you stage there, write the sentinel FIRST (`_has_archive_receipt`
   preserves dirs carrying `.archive-marker` or a top-level `RECEIPT`/`RECEIPT.*`).
   Staging is never archiving — step 4 still applies to the copy that survives.
   Detail: `core/config/rationale/archive-before-delete.md`.
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

   **Name it `RECEIPT.*` at top level; match case-insensitively in readers
   (g-115-3397, guard-2860). A receipt never lives INSIDE the store it
   describes.**
7. **Blast-radius check before the delete fires**: enumerate what READS this
   data. Read-through/restore-on-miss sync layers, session-binding caches,
   and registry rows can re-materialize or depend on "deleted" data (the
   donor-agent resurrection class, rb-2859).

## Anti-patterns

- Deleting first and verifying the recovery layer afterward (the exact
  ordering failure the canonical incident demonstrates)
- "The store is versioned, so it's safe" without reading its retention rules
- Reading whichever sub-part your principal CAN see as the verdict, when any
  other one is permission-denied — the readable half never proves noncurrent
  versions survive, whichever half that is; the layer is unverifiable, so the
  current-version-copy archive is mandatory (g-115-2692, g-115-4624)
- Treating user authorization ("go ahead and purge") as waiving the archive
  step — authorization sets the GOAL; this protocol sets the METHOD
- Archiving by moving (a move is a delete of the original)
- Verifying by sample instead of full count+bytes+checksum
- No receipt: an archive nobody can find or restore from is not an archive
- Treating an ADJACENT backup routine as coverage without intersecting its
  SET with the deletion's set. Ask "does the backup's set intersect the set
  this step destroys?" — where the intersection is empty, coverage is zero no
  matter how good the backup is (g-115-4471, rb-6344; twin: rb-4267).

## Cross-references

- rb-2859 — agent-retirement (graft/kill) checklist; carries the 2026-07-07
  incident trace, the archive location, and the receipt path
- `.claude/rules/verify-before-assuming.md` — "versioned = archived" was an
  unverified positive claim; recovery-layer capability requires reading the
  config, not assuming it
- `core/config/conventions/coordination.md` — multi-agent claim/registry
  surfaces that must be purged (not orphaned) at agent retirement
- g-115-2692 — the scoped-identity case behind step 2's UNVERIFIABLE-is-ABSENT
  clause; detail in `core/config/rationale/archive-before-delete.md`
