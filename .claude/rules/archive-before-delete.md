---
description: "No destructive store op before an integrity-verified archive exists outside the blast radius; authorization never means delete-first."
---

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
   **When the identity cannot READ the recovery config, the layer is
   UNVERIFIABLE — treat it as ABSENT (g-115-2692).** A least-privilege storage
   identity is often granted object read/write but DENIED some bucket-level
   read that verifies a recovery layer. WHICH one varies by bucket AND by
   principal — both directions measured in one account (g-115-2692: flag
   readable, rules denied; g-115-4624: the inverse, no principal reading the
   flag) — so never predict the split. Probe it, and name the principal that
   produced the reading (guard-1787). The layer is UNVERIFIABLE either way:
   the rb-2859 trap ("versioned" ≠ "archived") made STRUCTURAL by permission.
   When the specific config reads
   step 2 requires return access-denied, do NOT treat versioning/soft-delete as
   a recovery layer at all — proceed as if it does not exist: the independent
   current-version-copy archive in step 3 (retention-immune, needs only
   object-level read/write) becomes MANDATORY, not optional, and IS the recovery
   layer. Never let ANY readable sub-part stand in for the unreadable one that
   governs survival.
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

   **Name it `RECEIPT.*` at the archive's TOP LEVEL, and if you write a READER
   for it, match extension- and case-insensitively.** Until 2026-08-08
   (g-115-3397) this step named no filename, so writers and readers disagreed:
   `_seed_engine.py` writes `RECEIPT.json`, `history_vacuum_archive.py` writes
   lowercase `receipt.json`, and the one reader in the tree
   (`temp-drain-purge.sh` Lane 3) required `RECEIPT.md` **exactly** — a name **zero** producers write, so the
   protection fired only on hand-named receipts; a live archive carrying
   `RECEIPT.json` survived a drain purge only because someone hand-marked it.

   The asymmetry is what makes this a rule rather than a preference: a missed
   sentinel DESTROYS a recovery layer, while an over-match merely retains a
   directory until someone looks. So readers widen on the PRESERVE side — but
   anchor the match (`RECEIPT` / `RECEIPT.*`, top level only). A bare
   `*receipt*` substring or an any-depth match preserves every scratch dir of
   receipt-ish notes and makes the guard unfalsifiable (guard-2860).

   **A receipt never lives INSIDE the store or directory it describes.** A
   comment line in a JSONL store breaks every parser that reads it: measured
   2026-09-02, a downstream Body wrote `# RECEIPT: …` as line 1 of a board
   channel file and every post to that channel returned `internal_error`.
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
  SET with the deletion's set. Measured (g-115-4471): `seed-transplant`'s
  orphan sweep deleted destination files with a bare `unlink()` while a
  working `do_backup()` sat in the same script — but that backup archives the
  manifest INCLUDE-set (files about to be OVERWRITTEN), and the orphan set is
  the files about to be DELETED. The two are disjoint BY DEFINITION, so the
  recoverable operation had a backup and the unrecoverable one had none. The
  backup is what made the gap invisible: a reader asking "is this script
  careful about data?" finds a real archive routine and stops. Ask instead
  "does the backup's set intersect the set this step destroys?" — where the
  intersection is empty, coverage is zero no matter how good the backup is.
  (rb-6344; twin defect from the classification side: rb-4267.)

## Cross-references

- rb-2859 — agent-retirement (graft/kill) checklist; carries the 2026-07-07
  incident trace, the archive location, and the receipt path
- `.claude/rules/verify-before-assuming.md` — "versioned = archived" was an
  unverified positive claim; recovery-layer capability requires reading the
  config, not assuming it
- `core/config/conventions/coordination.md` — multi-agent claim/registry
  surfaces that must be purged (not orphaned) at agent retirement
- g-115-2692 — the scoped-identity case: a least-privilege storage identity
  denied version-enumeration + lifecycle-config reads (while the versioning
  on/off read stays allowed) makes the versioning recovery layer unverifiable,
  so the current-version-copy archive (step 3) becomes mandatory. Verdict:
  deliberate least-privilege, not an accidental gap. Deployment-specific IAM
  action + identity details are in the reasoning-bank entry.
