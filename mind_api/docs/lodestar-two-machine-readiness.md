# Lodestar — Two-Machine Test Readiness (START HERE)

**Status: READY for the two-machine test.** This is the wake-up handoff. It says
what is ready, the few things *you* do to bring up machine-2, and where to stop.

- **Detailed how-to:** `lodestar-machine-2-bring-up.md` (the step-by-step runbook).
- **Gate/status SSOT:** `lodestar-rollout-status.md` §5 (the G1–G5 table).
- Written 2026-06-03 at the end of the cruft-cleanup + readiness pass. Machine-1
  is cutover and stable; the two-machine test has **not** been run (that's yours).

---

## 1. Go status at a glance

| Area | State |
|------|-------|
| Machine-2 gate G1 (ConflictError retry) | ✅ closed (P8 locked_rmw + 412→ConflictError) |
| G2 (PUT retry-backoff + write-order) | ✅ closed (boto3 retries + cache-after-put) |
| G3 (env/cred SSOT) | ✅ closed (`.env.local` self-load; no injection) |
| G4 (cache off OneDrive) | ✅ closed (cache at `C:\ZakNoCloud\AyoaiCache`) |
| G5 (machine_id fail-closed) | ✅ closed (`MACHINE_ID` required + set) |
| Cruft / dead-code | ✅ clean — all detectors report 0 (see §5) |
| Daemon + own-cloud soak | ✅ healthy — serving, S3 sync live, logs clean |
| Machine-1 `.env.local` | ✅ own-cloud-ready (one pre-test edit, §3) |

---

## 2. Bring up machine-2 (summary — full detail in the runbook)

1. **Clone the repo** on machine-2. `world/` and `meta/` are NOT in git — point
   machine-2 at **fresh, empty** `WORLD_PATH`/`META_PATH` (in each agent's
   `local-paths.conf`). The cache populates from S3 on first read.
2. **Install Python deps**: `py -3 -m pip install -r mind_api/requirements-owncloud.txt`
3. **Create `.env.local`** (copy `.env.example`). Critical values: same
   `STORAGE_S3_BUCKET` / DDB tables / `ENVIRONMENT_ID=ayoai-mind` as machine-1, a
   **distinct** `MACHINE_ID`, and the scoped `MIND_AWS_*` creds.
4. **Start the daemon + `/start` an agent.** The sweep derives ownership from live
   DDB runner claims — no first-boot ceremony, no ownership env var to set.

The runbook §0 explains *why* this is safe (cache model + reads self-correct +
the H4a/H4b/G5 sweep defenses).

---

## 3. Machine-1 pre-test action

**Nothing.** Dynamic ownership is live (g-115-1337/1338/1339/1340, completed
2026-06-07). Machine-1's sweep derives which agents to push from its active DDB
runner claims, not from a static env var. When machine-2 starts an agent,
machine-1 automatically stops pushing that agent's data. No env edit, no restart.

Confirm machine-1 is healthy: `bash core/scripts/mind-api-start.sh --status`.

---

## 4. Reclaim local disk

**✅ DONE (2026-06-03, with your authorization) — the OneDrive orphan was deleted.**
`C:\Users\Zachary\OneDrive\Zak\SmartNPCs\Ayoai-Mind\` (`Ayoai-World` +
`Ayoai-Meta`, **14.22 GB measured**, mostly old `.history`) — the **pre-G4 cache**,
superseded by the live cache at `C:\ZakNoCloud\AyoaiCache\` and by S3 (the
authoritative store). It was verified fully redundant first (all six agents'
`local-paths.conf` point at `AyoaiCache`; zero live code/config references it),
then recycle-deleted via the Windows shell API (`FOF_ALLOWUNDO`, headless).

**Recovery (kept for 30 days, two independent nets):**
- **Local Windows Recycle Bin** — the whole folder fit; restore in one click
  (`Ayoai-Mind` ← `C:\Users\Zachary\OneDrive\Zak\SmartNPCs`).
- **OneDrive online Recycle Bin** — OneDrive (running) syncs the deletion to
  the cloud Recycle Bin (~30-day retention). The cloud-side sync trails the
  local delete; if you want to confirm it landed, check **onedrive.com → Recycle
  Bin** after the next sync cycle.

The recycle route (not a hard delete) was deliberate: a prior G4 audit noted a
couple of files that couldn't migrate due to Windows long-path limits — the
30-day nets neutralize any risk they were OneDrive-unique.

**The live cache (`AyoaiCache`)** is NOT a removal target — it's the working
cache that backs every read. (You *can* shrink it later: any file whose local
md5 == its S3 ETag is pure cache and re-pulls identically, but that's a
performance trade-off, not cleanup, and is best left until after the test.)

---

## 5. What this session did (cruft-cleanup + readiness pass)

- **Batch 1** (`42c91430`): removed repo-root stray artifacts + `mind_api/state`
  scratch (gsel dumps, stackdumps, exp/hyp/rb scratch, tracked strays).
- **Batch 2** (`d7f426a9`): resolved all 7 orphan scripts the dead-code detector
  found — **1 deleted** (`meta-transfer.py`, daemon-superseded), **2 wired**
  (`session-manifest-gate.sh`→pre-commit Gate 9, a *multi-machine sync-safety*
  gate; `goal-script-orphan-gate.sh`→verify-learning), **4 exempted** with
  documented channels. Also fixed the detector itself (was a 5-min timeout → 20s)
  and a latent crash in `goal-script-orphan-gate`.
- **Detectors now all green:** scripts-referenced (0 orphans), goal-script-orphan
  (0), orphan-root-sweep (0), toplevel-allowlist (PASS), no-python-cli-fallback,
  no-daemon-wrapper-reparse, domain-leak (clean).
- **Runbook accuracy fixed:** §1 (machine-1 already G5-ready; real step is
  narrowing `MACHINE_OWNED_AGENTS`) and §8 (`pending-questions.yaml` now syncs to S3
  via continuity tier — GAP-3 done).
- **Soak:** daemon healthy (port 61623, ~5h stable), S3 sync live (manifest
  06:18), logs clean.
- Each batch got a fresh-eyes review; the only bug found (wrong wiring home for
  Gate 9) was fixed before its commit.

---

## 6. Known residuals (non-blocking for the test)

- **#44 — read-through coverage (VERIFIED 2026-06-03, code-level):** Daemon-routed
  reads self-correct from S3 (`OwnCloudBackend._refresh` HEADs→GETs→overwrites the
  local cache), but **raw `Read`-tool / `cat` reads of world/meta files do NOT** —
  they read the local cache file directly. Four signals confirm it: neither
  PreToolUse nor PostToolUse `Read` hook routes through the backend (they are the
  context-reads dedup/record gates); the path hook only rewrites the `world/`→
  `$WORLD_PATH/` prefix (no S3 refresh); `owncloud_sync.py` is push-only (no bulk
  pull in its CLI); `pull_continuity`/`pull_temp` warm only the continuity+temp
  tiers. On machine-1's warm cache this is invisible; on machine-2's **fresh empty
  cache**, a raw read of a not-yet-daemon-read file misses until something
  daemon-routed populates it. **Impact is bounded** — the framework's critical
  reads (retrieval, guardrails, reasoning-bank, aspirations, tree, board) are all
  daemon-routed and self-correct on demand, so core agent operation on machine-2
  is safe. The residual is LLM raw-`Read`s of *cold* world/meta files (e.g. a
  convention or tree-node `.md` a skill reads directly), which return missing until
  warmed. **Mitigation options:** (a) accept on-demand warming — the §5 step-3
  spot-check surfaces any critical cold file; or (b) warm the cache on first boot
  with a bulk S3→local pull before starting agents. No one-command warm tool exists
  today; a `--pull-all` for `owncloud_sync.py` reusing its `_pull_one` primitive
  would close it fully (flagged as the durable fix; not built — beyond "ready").
  **Not a test blocker; just know it going in.**
- **#48** — transparent daemon-side conflict-retry on `aspirations_write` +
  pipeline JSONL handlers is a tracked hardening. Until then a cross-machine
  write race surfaces as a **loud, recoverable** `ConflictError` (the agent
  retries) — not silent corruption. Acceptable for the test per runbook §7.
- **#46** — 3 pre-existing `goal-duplication-gate` test failures in
  `mind_api/tests` (unrelated to own-cloud; predate this work).

None of these block the two-machine test.

---

## 7. STOP point

Per your instruction, I stopped here — everything is ready for the two-machine
test, and **you** run that test (set up machine-2 per §2, do the machine-1 edit
in §3). I did not start the test. I **did** delete the OneDrive orphan to the
Recycle Bin (§4) — that was the one local-disk reclaim you explicitly authorized
("if so, delete it"), done only after verifying redundancy and the 30-day
recovery nets. I stayed out of the reports folder (the other agent owns that).
