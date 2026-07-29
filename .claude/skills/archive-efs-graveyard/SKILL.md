---
name: archive-efs-graveyard
description: "Executes archive-before-delete against the shared-storage (EFS) _graveyard convention: enumerate (file/dir count, bytes, md5 manifest), tar to Accounts/{account}/_graveyard/{date}-{event}.tar.gz, verify the tar listing against the enumeration, install a receipt.json, and only then delete with a read-back check. MUST use whenever deleting environment dirs, session dirs, or any data on shared EFS storage — never raw rm -rf over efs-ssh. Fires on phrases like 'clean up EFS', 'delete the env dir', 'GC stale sessions', 'archive before delete', 'graveyard this', or when a platform delete lane fails and an owner-side (uid-1000) deletion is needed (stuck-deletion unblock, g-115-2388 shape). The companion script gates the delete leg on a verified archive plus installed receipt, so the archive-before-delete rule cannot be skipped."
forged: true
forged_by: zeta
forged_date: "2026-07-17"
forged_from: gap-014
user-invocable: false
minimum_mode: assistant
tools_used: [Bash]
companion_scripts:
  - world/scripts/efs-graveyard.sh
  - world/scripts/efs-ssh.sh
triggers:
  - archive before delete
  - graveyard this
  - clean up EFS
  - delete the env dir
  - GC stale sessions
  - stuck deletion
  - owner-side delete
---

# /archive-efs-graveyard — Shared-Storage Archive-Before-Delete

Executes the `.claude/rules/archive-before-delete.md` protocol against the EFS
`_graveyard` convention. Every account dir on shared storage carries a
`_graveyard/` cold-archive subdir; nothing under an account is deleted until an
integrity-verified tarball + receipt sit there. Proven manually twice before
forging (vinheim session GC 2026-07-13; pearl-test stuck-deletion g-115-2388)
and end-to-end by the forge smoke test (2026-07-17).

## When NOT to use this skill

- The target is NOT on shared EFS storage (local repo files → git is the
  archive; governed stores → own-cloud history/.history).
- The target IS a `_graveyard` path — the graveyard itself is append-only;
  the script refuses these.
- A secret-bearing artifact (e.g. a heap dump holding an api_key) — archiving
  SPREADS the secret. Delete it outright and record the exception in the
  receipt of the surrounding event (`hprof_deleted_without_archive` precedent,
  vinheim receipt 2026-07-13).

## Restricted Operations

- MUST use `world/scripts/efs-graveyard.sh` for the enumerate/archive/receipt/
  delete chain — never raw `rm -rf` via efs-ssh.sh, never hand-rolled tar. The
  script's delete leg refuses to run without a tar-verified archive
  (`.verified` marker) AND an installed receipt for the event slug; that gate
  IS the rule enforcement.
- MUST route remote access through `world/scripts/efs-ssh.sh` (the script does
  this internally) — never raw ssh (host-key ceremony lives in the wrapper).
- Targets MUST sit at least one level below `Accounts/{account}/` — the script
  refuses account roots and anything outside Accounts.

## Procedure (5 steps)

Pick a kebab-case `event-slug` that names the deletion event (it becomes the
graveyard filename stem): e.g. `vinheim-session-gc`, `pearl-test-stuck-deletion`.

### Step 1 — Blast-radius check (LLM judgment, BEFORE any script call)

Enumerate what READS the target: live server dirs, platform pollers, registry
rows, sibling envs. If a live reader expects the dir (e.g. the platform's
delete lane has NOT already tried to remove it), stop and coordinate first.
Record the conclusion — it becomes `blast_radius_note` in Step 4's receipt.

### Step 2 — Enumerate

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-graveyard.sh" enumerate <abs-efs-path> <event-slug>
# -> {"files": N, "dirs": N, "bytes": N, "manifest": ".../<date>-<event>.md5-manifest.txt"}
```

Writes the md5 manifest into `_graveyard/` — this is the integrity baseline.

### Step 3 — Archive (copy, never move)

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-graveyard.sh" archive <abs-efs-path> <event-slug>
# -> {"archive": "....tar.gz", "entries": N, "sha256": "...", "verified": true}
```

Tars the target, verifies tar file-entry count == enumeration count, records
sha256, drops the `.verified` marker the delete gate requires. A mismatch
exits 5 and removes the marker — fix before proceeding, never force.

### Step 4 — Receipt (LLM-composed, script-installed)

Compose the receipt JSON — the restore instructions and blast-radius reasoning
are judgment, not boilerplate. Required keys (script-enforced): `why`, `when`,
`by`, `restore`, `blast_radius_note`. Follow the established schema (real
examples live in `Accounts/b1fb6520-*/_graveyard/*.receipt.json`):

```
Bash: set -o pipefail; source core/scripts/_paths.sh && printf '%s' '{"deleted": "<repo-relative-ish path>", "why": "<what commanded this deletion + root cause>", "when": "<ISO-8601>", "by": "<agent> via efs-graveyard.sh, goal <goal-id>", "enumeration": {"files": N, "dirs": N, "bytes": N, "md5_manifest": "<manifest basename>"}, "archive": "<tarball basename> (N file entries, tar-listing-verified)", "restore": "<exact tar -xzf command + where NOT to restore and why>", "blast_radius_note": "<what reads this data and why deletion is safe now>"}' \
  | bash "$WORLD_PATH/scripts/efs-graveyard.sh" receipt <abs-efs-path> <event-slug>
# pipefail: efs-graveyard.sh is the LAST pipeline stage so its rc already reaches $?,
# but without pipefail a printf failure would be swallowed and the receipt silently
# skipped — the exact archive-before-delete step-6 failure mode (guard-776).
```

The `restore` field MUST name where NOT to restore to when live-path
restoration could confuse a reader (per-boot session dirs, re-created envs).

### Step 5 — Delete + read-back (gated)

```
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-graveyard.sh" delete <abs-efs-path> <event-slug>
# -> {"deleted": "...", "readback": "absent", "ok": true}
```

Refuses (exit 6) without the verified archive + receipt. A target that
survives `rm -rf` (partial delete — the uid/permission lane of g-115-2388)
exits 7 loudly; do NOT retry blindly, diagnose ownership first (a failed
platform delete can consume marker files and orphan the env — rb-3771).

## Batch mode (large-N GC — g-115-2457)

MUST use for multi-directory GC (>~3 targets): N single-target chains cost
~4 SSH round trips EACH, which in practice tempts a raw tar+rm bypass (the
g-115-2085 shape: 361 dirs hand-tarballed pre-forge). Batch mode is 4 round
trips TOTAL under one event with identical gate semantics — validated
end-to-end 2026-07-17 (3 dirs/5 files smoke: pre-archive delete exit 6,
no-receipt delete exit 6, union count 5==5, per-target readback, artifacts
intact).

```
# Targets: a LOCAL file (one absolute EFS path per line, # comments ok)
#          OR a remote glob (expanded server-side; every match validated).
# All targets must live under ONE account.
Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-graveyard.sh" batch-enumerate <targets-file|glob> <event-slug>
# -> {"targets": N, "files": N, "dirs": N, "bytes": N, "manifest": ..., "targets_file": ...}
#    installs targets.txt (account-relative — the authoritative batch list) + union md5 manifest

Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-graveyard.sh" batch-archive <account> <event-slug>
# -> ONE tarball via tar -T targets.txt; entries verified against the union manifest

# Receipt: same schema as Step 4 PLUS required "deleted_dirs" array (the
# vinheim precedent) enumerating every account-relative target.
Bash: set -o pipefail; source core/scripts/_paths.sh && printf '%s' '{... "deleted_dirs": ["<rel-1>", "<rel-2>", ...]}' \
  | bash "$WORLD_PATH/scripts/efs-graveyard.sh" batch-receipt <account> <event-slug>

Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-graveyard.sh" batch-delete <account> <event-slug>
# -> per-target rm -rf + read-back loop in one transaction; ANY survivor exits 7
#    listing every still-present path. Gate refusals (exit 6) identical to single mode.
```

Restore form is uniform: `tar -xzf <tarball> -C Accounts/<account>/` (entries
are account-relative). Post-enumerate legs take the ACCOUNT name, not a path —
the installed `targets.txt` is the single source of truth for what the event
covers. Batch and single events share a graveyard namespace: one event-slug
per deletion event, never reused across shapes.

## Error handling

- `REFUSED: target must be under .../Accounts/<account>/<...>` — the guard is
  correct; do not work around it. Account roots and non-Accounts paths are
  out of scope by design.
- Archive VERIFY FAILED (exit 5) — file count changed between enumerate and
  archive (concurrent writer?). Re-enumerate, re-archive; investigate the
  writer if it recurs.
- Delete exit 7 (target survives rm -rf) — ownership/mode lane: check
  uid/modes (`ls -na`), see g-115-2388 (755 uid-1000 dirs undeletable by the
  platform identity; owner-side chmod/delete is the fix).
- efs-ssh connection failure — probe with
  `source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/efs-ssh.sh" "echo ok"`
  (canonical probe per `.claude/rules/probe-with-canonical-code-path.md`) before
  filing a blocker. The prefix resolution is load-bearing HERE specifically: a bare
  `bash world/...` fails with "No such file or directory", which reads exactly like
  a dead connection and produces the false-positive blocker that rule exists to
  prevent (rb-246 / guard-147).

## Aftermath (mandatory)

- Journal the deletion event: what was deleted, why, receipt path.
- If the deletion unblocked a platform lane or revealed a new failure shape,
  encode it (reasoning bank / tree node) and cross-reference rb-2859
  (agent-retirement archive checklist) when the event was a retirement.
- The graveyard is append-only cold storage — never GC `_graveyard/` itself.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Step 5 delete Bash call (or the journal write when
Aftermath encoding applies). Never end with a text summary.
