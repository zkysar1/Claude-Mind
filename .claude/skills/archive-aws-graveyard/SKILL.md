---
name: archive-aws-graveyard
description: "Executes archive-before-delete against NON-EFS AWS stores — S3 prefixes and DynamoDB tables: enumerate (per-item checksums, count, bytes), copy into an s3://<bucket>/graveyard/<date>-<event>/ cold archive as CURRENT versions, verify AT THE DESTINATION on count AND bytes AND every checksum, install a receipt.json, and only then delete with a read-back check. MUST use whenever deleting S3 objects or a DynamoDB table — never a raw aws s3 rm --recursive or delete-table. Fires on phrases like 'delete the table', 'purge the bucket', 'clean up S3', 'drop the DynamoDB table', 'archive before delete', 'graveyard this', or 'tear down the AWS resource'. The companion script gates the delete leg on a destination-verified archive plus an installed receipt (exit 6), so the ordering cannot be skipped under time pressure. Sibling of archive-efs-graveyard, which covers shared EFS storage."
forged: true
forged_by: echo
forged_date: "2026-07-28"
forged_from: gap-016
user-invocable: false
minimum_mode: assistant
tools_used: [Bash]
companion_scripts:
  - world/scripts/aws-graveyard.sh
  - world/scripts/efs-ssh.sh
triggers:
  - archive before delete
  - graveyard this
  - delete the table
  - drop the DynamoDB table
  - purge the bucket
  - clean up S3
  - tear down the AWS resource
  - delete S3 objects
---

# /archive-aws-graveyard — Non-EFS AWS Archive-Before-Delete

Executes `.claude/rules/archive-before-delete.md` against S3 prefixes and
DynamoDB tables. Sibling of `/archive-efs-graveyard` (gap-014), which covers
shared EFS storage; between them, every store the fleet deletes from is
script-gated rather than honor-system.

Forged from gap-016 after the ceremony was executed **fully by hand twice**:
rb-2859 (2026-07-07, 2,461 retired-agent S3 objects) and g-335-259 (2026-07-26,
DynamoDB table + Cognito identity pool + IAM role). Both executions were
*correct* — and that is exactly the problem being solved. Their correctness
rested entirely on the operator remembering the order. This skill moves the
ordering into a gate.

## The one thing that matters

**The delete leg refuses (exit 6) unless BOTH a destination-verified archive
AND an installed receipt exist.** Everything else here is adapter detail. If you
change one thing about this skill, do not change that.

## Usage

```bash
G=world/scripts/aws-graveyard.sh   # resolve via $WORLD_PATH; see path-resolution.md

bash "$G" enumerate <s3|ddb> <target> <event-slug>
bash "$G" archive   <s3|ddb> <target> <event-slug>
bash "$G" receipt   <s3|ddb> <target> <event-slug>   # receipt JSON on stdin
bash "$G" delete    <s3|ddb> <target> <event-slug>
```

- `<target>` for `s3` = `s3://<bucket>/<prefix>` — the prefix is **required**; a
  bare bucket is refused (exit 3), because a typo there is the difference
  between deleting a folder and deleting a store.
- `<target>` for `ddb` = the bare table name.
- `<event-slug>` is kebab-case and becomes an S3 key segment. The graveyard dir
  is `graveyard/<YYYY-MM-DD>-<event-slug>/`, resolved once at enumerate and
  re-discovered by glob afterwards — so `delete` may run on a later day than
  `archive` without losing the trail.

## Exit codes

| code | meaning |
|---|---|
| 2 | usage / bad event-slug / bad store kind |
| 3 | target guard violation (bare bucket, non-`s3://`, a `graveyard/` path, bad table name) |
| 4 | missing precondition (no manifest, no graveyard dir, incomplete receipt) |
| 5 | **verify failed** — the archive does NOT match the enumeration; the `.verified` marker is removed, so delete will refuse |
| 6 | **DELETE REFUSED** — no verified archive, or no receipt |
| 7 | delete incomplete — the read-back still found data |

Mirrors `efs-graveyard.sh` so the two scripts read alike.

## Why every AWS call routes through `efs-ssh.sh`

Not a style choice — the only principal **measured** to hold the needed
permissions. From a fleet box the two reachable identities are *asymmetric*, and
the split lands exactly on the archive leg (measured 2026-07-28, g-115-3268,
both identities probed, no silent-failure flags):

| action | `user/ayoai-fleet-agent` (local `AWS_*`) | instance role (via `efs-ssh.sh`) |
|---|---|---|
| `s3:HeadBucket` on the store bucket | **ALLOWED** | allowed |
| `s3:PutObject` under `graveyard/` | **DENIED** (policy is prefix-scoped) | **ALLOWED** |
| `s3:ListAllMyBuckets` | **DENIED** | — |
| DynamoDB table lifecycle | **DENIED** (row 29, g-335-386) | **ALLOWED** |

A naive implementation using local credentials fails at archive time. Because
the ordering is fail-safe that aborts *before* any delete — no data loss — but
the ceremony never completes and every run logs an abort (the guard-1305 latent
abort-trap shape).

The general rule this instantiates: **an AccessDenied is evidence about a
PRINCIPAL, never about a capability** (`world/conventions/capability-routing.md`
row 29, rb-5174). If a leg reports denied, re-probe the *other* principal before
writing "cannot".

## Composing the receipt

The receipt is refused (exit 4) unless it parses as JSON and carries all of
`why`, `when`, `by`, `restore`, `blast_radius_note`. Those five are not
bureaucracy:

- **`restore`** must be runnable by someone who was not here. For a DynamoDB
  table, `delete-table` does not preserve schema — so if the receipt does not
  record the key schema, the archived items cannot be replayed into anything.
  Carry the schema.
- **`blast_radius_note`** must name where **NOT** to restore to. Restoring into
  live paths can re-arm read-through resurrection (rb-2859) — the failure mode
  where "deleted" data comes back because a sync layer restores on miss.

## Verification is at the DESTINATION, never staging

`archive` re-reads what actually landed in S3 and compares **count AND bytes AND
every checksum** against the manifest. Not a sample — a sampled spot-check is
not verification (`archive-before-delete.md`). A green local staging check says
nothing about what landed remotely, and the destination copy is the one the
delete is trading against (guard-1308, sharpened by g-335-259).

Copies are written as **current versions**, not relying on bucket versioning: a
noncurrent-version lifecycle rule expires old versions (guard-939), so an
archive resting on versioning would silently expire. Current-version copies are
the retention-immune form.

## Before flipping any caller to destructive mode

Verify the graveyard target exists **and accepts a write** — HeadBucket plus a
real PUT+HEAD round-trip on the exact configured bucket/prefix (guard-1305). A
passing refusal test proves the *gate* is fail-safe; it does not prove the
archive *path* works. Those are different claims, and only the second one keeps
data.

## Two bypasses the presence-checks alone do NOT catch

"A verified archive and a receipt exist under this slug" is a weaker claim than
"this archive is of this target". Both gaps below were found in pre-completion
review of the script, not in testing — a single clean run cannot produce either.

1. **Slug reuse resolves the wrong dir.** Re-using an event-slug on a later day
   leaves two dated dirs matching it. Resolving by "take the first" lands on the
   *older* one, whose `.verified` and `receipt.json` already exist from the
   completed first ceremony — so `delete` sails through the gate against a
   target that was never archived. Now refused (exit 3) naming both dirs:
   ambiguity is not resolved by picking, because picking is how the gate gets
   bypassed.
2. **The archive may be of a different target.** The gate must prove the archive
   is *of this target*, not merely that some verified archive exists under a
   matching slug — otherwise a correct-looking ceremony deletes B while holding
   an archive of A. `delete` now compares `manifest.source` against the live
   target and refuses (exit 6) naming both.

## Proven

Smoke-tested end-to-end 2026-07-28 against a throwaway DynamoDB table (created
and torn down by the same run — guard-1518, the creator issues the teardown):

- delete before enumerate → **rc=4**, names the missing precondition
- delete after enumerate, before archive → **rc=6** (`.verified` missing)
- delete after archive, before receipt → **rc=6** (receipt not installed)
- receipt → delete → **rc=0**, `readback: absent`
- **after** the delete: all 5 rows re-read from the graveyard, receipt present

Both bypasses above proven closed against the *completed* smoke graveyard —
the state where `.verified` and `receipt.json` both exist, i.e. exactly where
the presence-checks alone would have said yes:

- delete a **different** table with that slug → **rc=6**, naming the archived
  source and the requested target
- the **original** target still passes the source check and fails downstream
  only because the smoke delete already removed it (the discriminating twin,
  rb-4133 — proof the refusal is specific to mismatch, not universal)
- a second dated dir seeded under the same slug → **rc=3**, naming both

Local guards (no AWS): 10 usage/target refusals and 4 receipt-completeness
refusals, each with the documented exit code; `bash -n` clean.

## Chaining

- **Called by**: any goal deleting S3 objects or a DynamoDB table; CREATE_BLOCKER
  teardown paths; agent-retirement (rb-2859) for its non-EFS legs.
- **Calls**: `world/scripts/aws-graveyard.sh` → `world/scripts/efs-ssh.sh`.
- **Reads**: `STORAGE_S3_BUCKET` via `core/scripts/env-read.sh` (never hardcoded,
  never echoed). Override the graveyard bucket with `AWS_GRAVEYARD_BUCKET`.
- **Modifies**: the graveyard prefix (additive) and, at the gated leg only, the
  target store.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not
text. Terminal action is the final `aws-graveyard.sh` invocation (usually
`delete`), or the `board-post.sh` announcing the completed ceremony.
