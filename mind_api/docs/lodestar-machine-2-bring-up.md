# Lodestar Machine-2 Bring-Up Runbook

How to bring a **second machine** onto the same own-cloud store and run agents
on both at once. The two-machine test readiness doc is the entry point
(`lodestar-two-machine-readiness.md`); this is the step-by-step detail.

---

## The mental model (read this first)

**Git repo = firmware. S3 = memory.**

| Layer | What lives there | How it gets to a new machine |
|-------|-----------------|------------------------------|
| Framework (skills, scripts, rules, CLAUDE.md) | Git-tracked in the repo | `git clone` / `git pull` |
| Agent data (world, meta, agents state) | S3 (own-cloud) or local (fully-local) | Auto-syncs on first daemon read |
| Machine identity | `.env.local` (gitignored) | You create it |

A new machine needs exactly three things: the repo, creds, and a start command.
No manual data copying. No ownership env vars to manage. No first-boot ceremony.

**Why git for the framework?** Skills and scripts are code that Claude Code
reads directly from the local filesystem — they must be present on disk.
They're also version-controlled: forging a new skill, updating a rule, or
patching a script is a commit that `git pull` propagates to every machine.
World/meta/agent data is the inverse: dynamic per-session, too large for git,
and already fully managed by S3 (own-cloud) or local disk (fully-local).

Each git repo is effectively an environment — the same skills and conventions,
the same world. Agents sharing a repo share a capability set.

---

## 0. Safety model

The own-cloud backend is a **cache model**: local files are a cache of S3,
which is authoritative. Two facts make multi-machine safe:

1. **Reads self-correct.** The first daemon-routed read of any file HEADs S3,
   finds no recorded ETag, GETs the object, and overwrites the local cache.
   A stale git-cloned agent dir is replaced with current S3 content the moment
   it is read. No bulk pre-pull needed.

2. **The sweep only pushes what this machine owns.** Ownership is derived live
   from DDB runner claims (g-115-1337/1338/1339/1340, completed 2026-06-07):
   `do-I-own-X = this machine holds a live DDB runner claim for X`. No claim =
   no push. On a fresh machine before any `/start`, there are no claims → sweep
   touches nothing → safe from boot. There is no first-boot ceremony.

**If S3 sync needs to be paused:** stop the daemon (`bash core/scripts/mind-api-start.sh --stop`). That's the escape valve — no env var needed.

---

## 1. Machine-1 pre-test action

**Nothing.** Dynamic ownership is live. Machine-1's sweep already derives which
agents it pushes from its active DDB runner claims, not from a static env var.
When machine-2 starts an agent, machine-1 automatically stops pushing that
agent's data (its claim expires). No `MACHINE_OWNED_AGENTS` edit, no daemon
restart required on machine-1.

Confirm machine-1 is healthy: `bash core/scripts/mind-api-start.sh --status`.

---

## 2. Decide the agent split + privacy boundary

- **Agent ownership:** each agent runs on exactly ONE machine at a time. The DDB
  runner claim enforces this: a `/start <agent>` on machine-2 fails if machine-1
  holds an active claim for that agent. The claim IS the ownership signal.
- **Privacy boundary = `ENVIRONMENT_ID`.** Agents on the same `ENVIRONMENT_ID` share
  the world (one key prefix on the bucket/tables). Keep the same `ENVIRONMENT_ID`
  (`ayoai-mind`) on both machines for the first test so they share one world.

---

## 3. Provision machine-2

1. **Clone the repo** (brings `core/`, `.claude/`, and the committed `agents/`).
   `world/` and `meta/` are **external paths** (not in git) — do NOT copy
   machine-1's world/meta. Point machine-2 at **fresh, empty** WORLD_PATH /
   META_PATH dirs (configured in each agent's `agents/<name>/local-paths.conf`,
   which is machine-local and gitignored). The cache populates from S3 on first
   read. (`/start` Phase A-0 auto-detects the cloned agent and resumes it without
   re-initialization.)

2. **Install Python deps.** Own-cloud machines need `boto3` on top of the base deps:
   ```
   py -3 -m pip install -r mind_api/requirements-owncloud.txt
   ```
   The daemon will not start without `boto3`. `check-prerequisites.sh` fails
   loudly on a missing `boto3` when `STORAGE_BACKEND=own-cloud`, so this is
   caught at entry rather than as a silent dead daemon.

3. **Create `.env.local`** on machine-2 (copy `.env.example`, fill in):
   ```
   STORAGE_BACKEND=own-cloud
   ENVIRONMENT_ID=ayoai-mind                    # SAME as machine-1 (shared world)
   MACHINE_ID=<machine-2 hostname>              # MUST differ from machine-1 (G5)
   STORAGE_S3_BUCKET=<same bucket as machine-1>
   STORAGE_DDB_LOCK_TABLE=<same locks table>
   STORAGE_DDB_SESSIONS_TABLE=<same sessions table>
   MIND_AWS_ACCESS_KEY_ID=<scoped key id>       # least-privilege user, NOT root AWS_*
   MIND_AWS_SECRET_ACCESS_KEY=<scoped secret>
   AWS_DEFAULT_REGION=us-east-2
   ```
   - The scoped `MIND_AWS_*` are the only creds the daemon uses. Do NOT reuse
     the root `AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY`.
   - **Never** put these secrets on a command line or in a committed file —
     `.env.local` is gitignored and the daemon self-loads it.

4. **Keep the runtime dir off synced storage (G4).** Ensure `RUNTIME_DIR`
   (daemon.port/pid + sweep manifest) resolves to a local, non-OneDrive/NAS path.

---

## 4. Env/cred propagation (the SSOT)

The daemon self-loads all storage config + `MIND_AWS_*` creds from `.env.local`
(`mind_api/src/__main__.py::_load_env_local`) regardless of how it is spawned.
No per-command injection needed.

For direct CLI use of `owncloud_sync.py` (manual ops only):
```
set -a; source .env.local; set +a
source core/scripts/_paths.sh
```

---

## 5. First boot

1. Start the daemon: `bash core/scripts/mind-api-start.sh`. Confirm it serves
   storage (no `MACHINE_ID` / `MIND_AWS_*` RuntimeError in the log).

2. `/start <agent> --mode reader`. Phase A-0 auto-detects the cloned agent and
   resumes it — no re-initialization.
   > Reader mode for first boot is optional but useful for verifying S3 reads
   > before writing. A bare `/start <agent>` straight to autonomous is safe.

3. **Verify the pull:** spot-check 2–3 shared items (an aspiration, a tree node).
   Values must match machine-1's current state, pulled fresh from S3.
   > Raw `Read`-tool reads of cold world/meta files that have not been daemon-routed
   > yet may miss until warmed (known residual #44 — bounded, non-blocking). All
   > critical framework reads (retrieval, aspirations, tree, board) are
   > daemon-routed and self-correct on demand.

4. Confirm a write round-trips through the DDB lock (add a throwaway journal line
   via the normal wrapper; verify it appears on machine-1 after cache TTL).

That's the whole bring-up. The sweep is already running; it only pushes agents
for which this machine holds a RUNNING DDB claim (acquired by `/start`).

---

## 6. Running both machines

`/start` machine-2's agents. Run machine-1's agents as usual. They share one
world over S3, serialized by the DDB lock. Ownership is live — when you stop
an agent on machine-2 and start it on machine-1, ownership transfers
automatically.

---

## 7. What to expect during the test (known, non-blocking)

- **Rare cross-machine `ConflictError`.** If a non-locked writer races a
  daemon read-modify-write, the endpoint can return an error for that one op.
  It is **loud and recoverable** — the agent retries. Not silent corruption.
  Transparent daemon-side auto-retry is a tracked hardening follow-up (#48).
- **Cache lag.** A write on one machine becomes visible on the other after that
  machine's `OWNCLOUD_CACHE_TTL` (default 30s) or the next force-fresh read.
  Expected; not a bug.
- **Sweep cadence.** Real-time LLM Write/Edit-tool writes reach S3 on the next
  periodic sweep (default 120s). Lower `OWNCLOUD_SYNC_INTERVAL` for tighter
  convergence during the test.

---

## 8. Teardown / moving an agent between machines

`/stop <agent>` on A — this flushes `agents/<agent>/` to S3 and releases the
DDB runner claim. Then `/start <agent>` on B — pulls fresh state from S3 and
acquires the claim. No env edits, no daemon restarts. Ownership transfers via
the claim.

A crashed runner that never released does not pin ownership — B's `/start`
reclaims the stale claim and acquires.

> **NEVER manually copy `agents/<name>/session/` or `agents/<name>/sessions/`
> between machines.** They hold per-machine runner identity (`running-session-id`,
> `runner-token`, PID/SID). Copying creates phantom runners. `/start` scaffolds
> the directory fresh from templates on the new machine.
>
> Cross-machine continuity files (`handoff.yaml`, `pending-questions.yaml`) are
> synced to S3 via continuity tier and load automatically on the new machine.

---

## Checklist (quick reference)

- [ ] Machine-1 healthy: `mind-api-start.sh --status` clean
- [ ] Agent split decided; same agent never on two machines at once
- [ ] `ENVIRONMENT_ID` SAME on both (shared world) — or distinct to practice isolation
- [ ] Machine-2: `git clone <repo>`
- [ ] Machine-2: `py -3 -m pip install -r mind_api/requirements-owncloud.txt`
- [ ] Machine-2: `.env.local` with distinct `MACHINE_ID`, scoped `MIND_AWS_*`, same bucket/tables
- [ ] `RUNTIME_DIR` off synced storage (G4)
- [ ] First boot: daemon starts clean, reads pull current S3 state, write round-trips
- [ ] `/start` agents; confirm cross-machine writes converge within cache TTL
