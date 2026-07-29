---
name: reconcile-owncloud-conflicts
description: "Reconciles own-cloud conflict-persistent (both-diverged frozen) files between the local mirror and the authoritative S3 store using the proven S3-base RMW recipe (rb-3679). MUST use whenever mind_api/state/owncloud-conflict-streaks.json is non-empty, sync telemetry shows diverged_skipped streaks at/past threshold 3, a forged-skills or governed-store row vanished after a peer push (registry-row loss), or a local write sits frozen un-pushed across many sweeps (strand-by-freeze). Fires on phrases like 'reconcile the conflict backlog', 'clear owncloud conflicts', 'diverged_skipped', 'conflict-persistent files', 'frozen store', 'restore the lost row'. Never blind-push local over S3 and never hand-build s3:// URIs — the recipe pulls the authoritative object via backend-cat.sh, unions per file class, pushes through the owncloud-push-on-write hook, and byte-verifies the read-back."
forged: true
forged_by: bravo
forged_date: "2026-07-16"
forged_from: gap-013
user-invocable: false
minimum_mode: assistant
tools_used: [Bash, Read, Write, Edit]
companion_scripts:
  - core/scripts/backend-cat.sh
  - core/scripts/owncloud-push-on-write.sh
  - core/scripts/owncloud-flush.sh
triggers:
  - reconcile owncloud conflicts
  - clear the conflict backlog
  - diverged_skipped streak
  - conflict-persistent file
  - frozen governed store
  - restore lost registry row
  - strand-by-freeze
---

# /reconcile-owncloud-conflicts — S3-Base RMW Conflict Repair

Repairs files stuck in the own-cloud **conflict-persistent freeze**: both the
local mirror and the authoritative S3 object moved past the manifest baseline,
the basename has no registered merge handler in `coordination_merge._HANDLERS`,
so every sweep logs `diverged_skipped` and the file freezes — local edits never
push (strand-by-freeze), peer edits never pull, and a later blind push in either
direction loses one side's content (the lane that ate the check-production-health
registry row, g-115-2328). Proven single-file (g-115-2328) then bulk ×13
(g-115-2346, 14→0 conflicts, 13/13 byte-identical read-backs).

## When NOT to use this skill

- The basename HAS a registered merge handler (rb/guardrails/pattern-signatures
  jsonl etc.) — let the sync's merge lane resolve it; investigate only if a
  freeze recurs post-registration.
- The divergence is single-sided (stale-pull or authored-push lanes) — the sweep
  resolves those itself.
- The file is another agent's PRIVATE state under `agents/<other>/` — coordinate
  via the board first; never python-rewrite another agent's files.

## Restricted Operations

- MUST read the authoritative side via `bash core/scripts/backend-cat.sh cat|head <path>`,
  never raw `aws-exec.sh s3 cp` with a hand-built `s3://` URI. backend-cat routes
  through the backend's own `_s3_key` mapping (no URI-derivation mistakes) and its
  `read_authoritative_bytes` is a pure to-memory read that never mutates the local
  mirror. (If aws-exec is ever unavoidable, output paths MUST be absolute —
  aws-exec cd's internally and relative paths break; rb-3683.)
- MUST push the merged result via the Write/Edit tool (the PostToolUse hook
  auto-pushes governed writes) OR the manual hook invocation shown in Step 5 —
  never a direct S3 PUT. The hook path stamps the manifest baseline
  (`sync_file(multi_machine=False)`), which is what CLEARS the conflict.
- MUST byte-verify the pushed object (Step 6) before claiming the conflict
  resolved. A push without read-back is an unverified positive claim.

## Procedure (6 steps per file)

### Step 1 — Enumerate the backlog

```
Bash: cat mind_api/state/owncloud-conflict-streaks.json
```

Each entry = repo-relative path → consecutive diverged_skipped sweep count
(monotone until merged; threshold 3). Empty file/`{}` = nothing to do —
UNLESS the daemon restarted in the last few sweeps: the tracker rebuilds
from zero after restart and needs ~3 consecutive sweeps per file to re-list
it, and `owncloud-flush`'s `conflicts:` counter counts a different lane than
the refresh-side conflict-skips. After any restart, cross-check the
spawn/sync log's `skip (CONFLICT ...)` lines or run 2-3 flushes before
concluding clean (rb-3905; the streak-file instance of guard-1064).

### Step 2 — Fetch the authoritative side

```
Bash: bash core/scripts/backend-cat.sh head <path>     # exists, ETag, size, drift vs mirror
Bash: bash core/scripts/backend-cat.sh cat <path> > <ABS-scratch>/s3-side   # authoritative bytes
```

### Step 3 — Three-way understanding

Diff S3-side vs local. Identify per-side unique content: what did I author
locally that never pushed; what did peers push that I never pulled. Read git
history / journal / board when authorship is unclear.

### Step 4 — Union with a per-class verdict

| File class | Verdict |
|---|---|
| Append-only JSONL (ledgers, logs, invocations) | LINE-UNION: base = bigger side, append other-side-only lines; preserve order; dedup exact lines |
| Both-sides-edited markdown (tree nodes, conventions) | HAND-UNION: splice each side's unique sections by heading boundaries into one body. NEVER trust an opcode/difflib auto-merge unverified — it can DROP an interleaved section body while keeping its heading (rb-3683). After merging, grep the result for BOTH sides' distinguishing content; rebuild by hand on any miss |
| Peer superseded local (their newer edit of my content) | S3-ADOPT: take the authoritative side verbatim |
| Local authored / local strictly newer | LOCAL-WINS: push local (only after confirming the S3 side has no unique content) |
| Registry/keyed YAML (forged-skills.yaml etc.) | S3-BASE RMW: start FROM the authoritative side, splice the local-only rows/keys in — a local-base push deletes peer rows (guard-1050) |
| File embedding a CAPPED rolling structure (fixed-length history array, windowed log — tell: both sides show the same suspiciously-round entry count, e.g. 200/200) | S3-ADOPT the current window: rolled-off entries are MEANT to be evicted; a line-union would violate the cap (rb-3904, goal-selection-strategy.yaml 2026-07-18) |

### Step 5 — Install + push

Write the union to the real path (Write/Edit tool — the hook auto-pushes), or
for a file installed by bash, invoke the hook manually:

```
Bash: printf '{"tool_input":{"file_path":"%s"}}' "<abs-path>" | bash core/scripts/owncloud-push-on-write.sh
```

**Direct daemon route (g-115-2447 durable fix, landed 2026-07-16):** the
per-file governed push is also reachable as a daemon admin route — the same
`sync_file(multi_machine=False)` baseline-stamping entry, run with the
daemon's registry-derived creds, so it works on environment-config
deployments where the bare CLI has no backend config:

```
Bash: port=$(cat mind_api/state/daemon.port); curl -s -X POST "http://127.0.0.1:${port}/v1/admin/owncloud-sync-file?path=<urlencoded-abs-path>"
# response: {"ok": true, "pushed": 1, ...} — ok:false or "reason" explains the skip
```

The hook invocation above now uses this route itself (daemon-first, CLI
fallback with registry-derived env). Probe hook liveness with the
`OWNCLOUD_PUSH_HOOK_DRYRUN=1` invocation — it must print a `would push` line;
silence means the backend gate fast-exited (pre-2447 shim, or no backend
resolvable).

**Daemon-down fallback — the two-sweep sequence** (proven 4/4 on the
g-115-2446 backlog: 2 hand-unions, 1 S3-adopt, 1 line-union): when neither
the hook nor the daemon route is reachable, decompose the baseline-stamp into
two sweep-legal steps using only the flush endpoint:

```
1. cp <union>  <scratch>/union-X       # stage the union you composed in Step 4
2. cp <scratch>/s3-side  <real-path>   # adopt authoritative bytes locally
3. Bash: bash core/scripts/owncloud-flush.sh   # sweep sees local==S3 → in_sync → baseline ADVANCES, streak clears
4. cp <scratch>/union-X  <real-path>   # install the union
5. Bash: bash core/scripts/owncloud-flush.sh   # sweep sees S3==baseline + local moved → local-authored PUSH
```

### Step 6 — Verify + confirm streak clear

```
Bash: bash core/scripts/backend-cat.sh head <path>       # drift vs mirror must be none
Bash: bash core/scripts/owncloud-flush.sh                # force full sweep
Bash: cat mind_api/state/owncloud-conflict-streaks.json  # entry must be GONE
```

If the entry persists: the push did not advance the baseline — re-read the sync
log for the path, do not re-push blindly.

## Error handling

- `backend-cat.sh head` exits 1 (object absent): the S3 side never existed —
  this is a plain un-pushed local write, not a conflict; Step 5 alone suffices.
- Fenced-PUT 412 during push: a peer wrote concurrently — re-fetch (Step 2) and
  re-union; never retry the same bytes in a loop.
- Union produced byte-identical content to one side: still push via the hook —
  the point is advancing the manifest baseline, not changing bytes.
- Bulk backlogs: process files one at a time through Steps 2-6; a single batch
  fetch that fails mid-way leaves un-diffed installs (g-115-2346 batch lesson).

## Aftermath (mandatory)

- If any content was restored from S3 history or a row was re-spliced, journal
  which side lost what and why (the loss-lane taxonomy node
  `owncloud-write-path-loss-lanes` may need a new specimen).
- If the frozen basename will keep taking concurrent writes, file a goal to
  register a merge handler for it in `coordination_merge._HANDLERS` — this
  skill is the REPAIR; handler registration is the CURE.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Step 6 verification Bash call (backend-cat head +
streaks read) for the last reconciled file. Never end with a text summary.
