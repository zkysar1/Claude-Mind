# Rationale: Archive Before Delete — the incidents and measured cases behind the protocol

Referenced from `.claude/rules/archive-before-delete.md`. That rule is loaded into every session, so it keeps only the imperatives. This file holds the incidents and measured cases behind step 2, step 3(c), step 6 and the adjacent-backup anti-pattern. The passages were moved word for word by g-353-118 cut 5 (2026-09-26); nothing was reworded.

## Why step 2 says "read the config, don't assume"

Canonical incident (2026-07-07, rb-2859): a purge of 2,461 retired-agent objects from the remote store relied on "the store is versioned"; the store's retention policy would have permanently expired every noncurrent version in 90 days. "Versioned" meant "delayed permanent deletion", not "archived".

## Why an unreadable recovery config counts as ABSENT (g-115-2692)

A least-privilege storage identity is often granted object read/write but DENIED some bucket-level read that verifies a recovery layer. WHICH one varies by bucket AND by principal — both directions measured in one account (g-115-2692: flag readable, rules denied; g-115-4624: the inverse, no principal reading the flag) — so never predict the split. Probe it, and name the principal that produced the reading (guard-1787). The layer is UNVERIFIABLE either way: the rb-2859 trap ("versioned" ≠ "archived") made STRUCTURAL by permission.

g-115-2692 — the scoped-identity case: a least-privilege storage identity denied version-enumeration + lifecycle-config reads (while the versioning on/off read stays allowed) makes the versioning recovery layer unverifiable, so the current-version-copy archive (step 3) becomes mandatory. Verdict: deliberate least-privilege, not an accidental gap. Deployment-specific IAM action + identity details are in the reasoning-bank entry.

## Why step 3(c) warns against staging in the agent temp store

(ZDS g-001-349: two live instances 2026-08-03, one of them IAM-policy rollback material 10h into a deletion window.)

## Why step 6 names the receipt file

Until 2026-08-08 (g-115-3397) this step named no filename, so writers and readers disagreed: producers write `RECEIPT.json` (`_seed_engine.py`) and lowercase `receipt.json` (`history_vacuum_archive.py`), while the one reader (`temp-drain-purge.sh`) required `RECEIPT.md` **exactly** — a name zero producers write, so the protection fired only on hand-named receipts.

## Why a receipt never lives inside the store it describes

A comment line in a JSONL store breaks every parser that reads it: measured 2026-09-02, a downstream Body wrote `# RECEIPT: …` as line 1 of a board channel file and every post to that channel returned `internal_error`.

## The measured case behind the adjacent-backup anti-pattern (g-115-4471)

Measured (g-115-4471): `seed-transplant`'s orphan sweep deleted destination files with a bare `unlink()` while a working `do_backup()` sat in the same script — but that backup archives the manifest INCLUDE-set (files about to be OVERWRITTEN), and the orphan set is the files about to be DELETED. The two are disjoint BY DEFINITION, so the recoverable operation had a backup and the unrecoverable one had none. The backup is what made the gap invisible: a reader asking "is this script careful about data?" finds a real archive routine and stops. Ask instead "does the backup's set intersect the set this step destroys?" — where the intersection is empty, coverage is zero no matter how good the backup is. (rb-6344; twin defect from the classification side: rb-4267.)

## Cross-references

- `.claude/rules/archive-before-delete.md` — the imperatives this file explains
- rb-2859 — agent-retirement (graft/kill) checklist; carries the 2026-07-07 incident trace, the archive location, and the receipt path
- rb-6344, rb-4267 — the adjacent-backup defect and its classification-side twin
- guard-1787 — name the principal that produced a permission reading
- g-353-118 — the post-compaction context-floor diet that moved this text out of the always-loaded rule
