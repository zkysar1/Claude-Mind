---
name: transplant
description: "Relocates a LIVING mind — its agents, world, meta, and identity — onto another machine. Two modes. own-cloud: the repo (git) carries code + tracked agent identity and S3 carries world/meta, so the source side just prepares and prints a destination bring-up checklist (no archive). offline: packs a portable archive (git-archive of the repo PLUS the external world/ and meta/ data, minus the .history bloat and machine-local session state) that can be copied or emailed, plus a RESTORE guide. The destination resumes via /start (Phase A-0 auto-detects the cloned/unpacked agent). This is NOT the seed: a seed is domain-free and grows a NEW empty environment (use /seed). A transplant keeps the living mind intact and moves it. Use when the user says transplant, transplant the mind, move the mind to another computer, relocate the mind, move to a new machine, pack the mind, or email the mind. Sub-commands: own-cloud, offline, land, verify."
user-invocable: true
triggers:
  - "/transplant"
  - "transplant"
  - "transplant the mind"
  - "move the mind to another computer"
  - "relocate the mind"
  - "move to a new machine"
  - "pack the mind"
  - "email the mind"
parameters:
  - name: sub-command
    description: "own-cloud | offline | land | verify"
    required: true
  - name: target
    description: "For offline: output path for the pack. For land/verify: path to the pack or unpacked destination repo."
    required: false
  - name: flags
    description: "--agents <list>, --out <path>, --include-history, --dry-run, --force, --no-flush"
    required: false
tools_used: [Bash, Read, Glob, Grep, Write, Edit]
conventions: [session-state, external-paths]
minimum_mode: assistant
applies_to: any
forged: false
---

# /transplant — Relocate a Living Mind to Another Machine

Moves a *living* deployment — its agents (identity, curriculum, journal,
experience), its `world/` (knowledge tree, reasoning bank, guardrails,
aspirations, board), and its `meta/` (improvement strategies) — onto a second
machine, with the agent able to RESUME as itself rather than re-initialize.

This is the opposite of `/seed`. A **seed** is domain-free; planting it grows a
NEW, empty environment (`claude-mind`) with no agents and no learned state. A
**transplant** keeps everything and relocates it. If the user wants a fresh
empty framework, that is `/seed plant` — not this skill.

## How a mind is partitioned for transport

| Layer | Lives in git? | Lives at external path? | On S3 (own-cloud)? | Travels how |
|-------|---------------|--------------------------|---------------------|-------------|
| `core/`, `.claude/`, `CLAUDE.md` (framework) | YES (tracked) | — | — | `git clone` / `git archive` |
| `agents/<name>/` tracked identity (`self.md`, `aspirations.jsonl`, `curriculum.yaml`, journal/, experience/, `.initialized`) | YES (tracked) | — | continuity tier | `git clone` / `git archive` |
| `agents/<name>/session/`, `sessions/`, `local-paths.conf` | NO (gitignored) | — | machine-local | **never travels** — `/start` Phase A-0 scaffolds fresh |
| `world/`, `meta/` (learned data) | NO (external) | YES | YES (authoritative) | own-cloud: S3 rehydrate · offline: packed archive |
| `.env.local` (secrets + `MACHINE_ID`) | NO (gitignored) | — | — | **never travels** — recreated per machine (the one manual step) |

The two never-travel rows are the whole reason a transplant needs care: secrets
can't ride along, and session/runner state is machine-local (copying it creates
phantom runners — see the machine-2 runbook §8). Everything else is either in git
or rehydratable.

## Mode Gate

```
Bash: session-mode-get.sh
IF mode is "reader":
    Bash: echo "Skill requires assistant mode. Run /start <agent> --mode assistant to enable."
    STOP.
```

## Step 0: Load Conventions

`Bash: load-conventions.sh session-state external-paths`

Read only the paths returned (files not yet in context). If output is empty, all
conventions already loaded — proceed.

## Sub-Commands

```
/transplant own-cloud [--agents <list>] [--no-flush] [--dry-run]
/transplant offline [--out <path>] [--include-history] [--dry-run]
/transplant land <pack-or-dir> [--dest <repo>] [--force]
/transplant verify <dest>
```

---

### `/transplant own-cloud` — Prepare source + emit destination checklist

When the mind runs on the own-cloud backend (`STORAGE_BACKEND=own-cloud`),
the second machine joins the SAME S3 world. There is **no archive** — git carries
code + identity, S3 carries world/meta (reads self-correct on the new machine).
The source-side job is to make the destination's clone current and hand the user
an exact bring-up checklist.

**Step 1**: Confirm the backend is own-cloud.
  `Bash: bash core/scripts/transplant-pack.sh own-cloud --dry-run`
  The wrapper reads `STORAGE_BACKEND` (from `.env.local`). If it is NOT
  `own-cloud`, it exits non-zero with a pointer to `/transplant offline`. STOP
  and relay that message.

**Step 2**: Verify the repo is committed and pushed (git is the transport).
  The wrapper runs `git status --porcelain` and compares local `main` to
  `origin/main`. If the tree is dirty or local is ahead of origin, it REFUSES
  (the destination clone would be stale). Resolve by committing + pushing
  (agent-capable per `world/conventions/post-execution.md`), then re-run.
  `--force` downgrades this to a warning.

**Step 3**: Flush continuity to S3 (skip with `--no-flush`).
  `Bash: bash core/scripts/owncloud-flush.sh`
  Pushes the latest `handoff.yaml`, `pending-questions.yaml`, working-memory,
  and any other `sync_tier: continuity` files so the destination picks up the
  agent's most recent state on first daemon read. (These are continuity-tier in
  `core/config/session-manifest.yaml`; the rest of `session/` is intentionally
  machine-local and is NOT flushed.)

**Step 4**: Emit the destination bring-up checklist.
  The wrapper prints the distilled `mind_api/docs/lodestar-machine-2-bring-up.md`
  procedure, parameterized for the agents being moved:
  1. `git clone <origin-url>` on the new machine.
  2. **Install Python deps**: `py -3 -m pip install -r mind_api/requirements-owncloud.txt`
     (includes the AWS SDK + base deps). Daemon will not start without the own-cloud deps.
  3. Create `.env.local` from `.env.example` with: same `ENVIRONMENT_ID`, same
     bucket/DDB tables, the scoped `MIND_AWS_*` creds, and a **DISTINCT
     `MACHINE_ID`** (G5 fail-closed — must differ from the source machine).
  4. Point each moved agent's `local-paths.conf` at FRESH empty WORLD_PATH/
     META_PATH dirs (cache populates from S3 on read). `/start` Phase A-0 writes a
     sane default if absent.
  5. `/stop <agent>` on the source machine (flushes to S3 + releases DDB claim).
  6. `/start <agent>` on the new machine — Phase A-0 detects the cloned agent and
     resumes it; dynamic ownership means this machine's claim IS the ownership signal.
     No env edits needed on either machine.

**Step 5**: Report (final Bash echo). State that no archive was produced (own-cloud
  uses git + S3), the continuity flush result, and the checklist location.

---

### `/transplant offline <out>` — Build a portable archive (no S3)

When there is no shared cloud (or the user wants an emailable/USB-able copy), the
world/ and meta/ data must travel WITH the repo. This produces ONE archive:
`git archive HEAD` (the tracked repo — code + agent identity, automatically
excluding gitignored session/`.history`/secrets) PLUS the external `world/` and
`meta/` data with the heavy `.history/` snapshots and any machine-local/transient
files stripped.

**Step 1**: Build the pack.
  `Bash: bash core/scripts/transplant-pack.sh offline --out <out> [--include-history] [--dry-run]`
  The wrapper (→ `_transplant_pack.py`):
  a. Resolves `WORLD_PATH` / `META_PATH` via `_paths.sh`.
  b. Stages `repo/` from `git archive HEAD` (tracked files only).
  c. Copies `world/` and `meta/` into the stage, EXCLUDING `.history/` (unless
     `--include-history`), `*.lock`, `__pycache__/`, and any `session/`-shaped
     transient dirs.
  d. **Secret-scan gate**: greps the staged world/meta for credential patterns
     (cloud access-key IDs, secret keys, API tokens, private-key headers — the
     exact regexes live in `_transplant_pack.py`). Any hit → REFUSE and delete the
     stage (secrets must never travel — they belong only in `.env.local`, which is
     not packed).
  e. Writes `RESTORE.md` into the stage (the `land` checklist, below).
  f. Archives the stage: `.zip` on Windows, `.tar.gz` elsewhere, at `<out>`.
  g. Prints the archive path + size, and WARNS if size > 25 MB (email-attachment
     territory — suggest `--include-history` was probably NOT wanted, or use a
     drive/transfer service).
  If `--dry-run`: stage + report size + secret-scan, but do NOT create the final
  archive; delete the stage. STOP.

**Step 2**: Report (final Bash echo) — archive path, size, file count, what was
  excluded, and "next: copy to the new machine, then `/transplant land <archive>`."

---

### `/transplant land <pack-or-dir>` — Resume the mind at the destination

Run this ON THE NEW MACHINE.

- **own-cloud**: there is nothing to unpack — `land` just confirms the checklist
  was followed (repo cloned, deps installed, `.env.local` present with a distinct
  `MACHINE_ID`) and points the user at `/start <agent>`. Phase A-0 does the
  resume. If `.env.local` is missing or `MACHINE_ID` matches the source,
  REFUSE with the specific fix.

- **offline**: unpack the archive and wire the external paths.
  `Bash: bash core/scripts/transplant-pack.sh land <pack> --dest <repo> [--force]`
  The wrapper:
  a. Extracts `repo/` to `<dest>` (the cloned/empty repo location).
  b. Places the packed `world/` and `meta/` at chosen local paths
     (default: `<dest>/.mind-data/{world,meta}`).
  c. Writes each moved agent's `local-paths.conf` pointing at those paths, so
     `/start` Phase A-0 honors it instead of writing the own-cloud cache default.
  d. Prints: "next: create `.env.local` if your domain needs API keys (storage is
     local — no S3 creds needed), then `/start <agent>`."

**Step (both)**: Final Bash echo — what landed and the exact `/start` command.

---

### `/transplant verify <dest>` — Post-land smoke test

`Bash: bash core/scripts/transplant-pack.sh verify --dest <dest>`

Checks at the destination:
1. **Identity intact** — each expected `agents/<name>/.initialized`, `self.md`,
   `aspirations.jsonl`, `curriculum.yaml` present.
2. **No machine-local leakage** — no `session/`, `sessions/`, `.active-agent-*`,
   or `local-paths.conf` was carried from the source (offline: must be absent in
   the pack; own-cloud: never in git).
3. **No secret leakage** — `.env.local` absent from the pack/clone; secret-scan of
   world/meta clean.
4. **World/meta reachable** — own-cloud: a daemon-routed read self-corrects from
   S3; offline: `local-paths.conf` points at existing, non-empty world/meta dirs.
5. **Prereqs** — `bash core/scripts/check-prerequisites.sh` + (own-cloud) the
   cloud SDK importable on the daemon interpreter.
6. **Report** — structured PASS/FAIL/WARN. Final Bash echo.

## Constraints

- NEVER pack `.env.local`, `**/session/`, `**/sessions/`, `**/local-paths.conf`,
  or `**/.history/` (the last only with explicit `--include-history`). Secrets and
  machine-local runner state must never travel.
- NEVER run the same agent on two machines at once (own-cloud DDB lock prevents
  file corruption, NOT the same-identity goal-claim collision). The own-cloud
  checklist always includes `/stop` on source before `/start` on destination.
- The source mind is READ-ONLY to this skill except the own-cloud continuity
  flush (a normal sync op). It does NOT edit `.env.local` (the user sets
  `MACHINE_ID` per the checklist — `.env.local` is machine-local config the
  agent does not auto-edit). Sync ownership is derived from live DDB runner
  claims — there is no `MACHINE_OWNED_AGENTS` env list to set.
- Resume at the destination is `/start`'s job (Phase A-0). This skill never sets
  `agent-state`/`agent-mode` (guard-340) and never calls `/start`.

## Implementation

- `core/scripts/transplant-pack.sh` — wrapper: arg/flag parse, sources `_paths.sh`,
  dispatches the four sub-commands. Mirrors the `/seed` → `seed-transplant.sh`
  pattern (NOT daemon-routed — this is a filesystem/packaging utility, not a
  data-layer op, so the no-python-cli-fallback rule does not apply; it calls the
  Python engine directly via `py -3`).
- `core/scripts/_transplant_pack.py` — engine: own-cloud checklist, offline
  stage/secret-scan/archive, land/unpack, verify.

## Chaining

- **Called by**: User (`/transplant <sub-command>`).
- **Calls**: `transplant-pack.sh` (+ its Python engine), `owncloud-flush.sh`
  (own-cloud continuity flush).
- **Reuses at destination**: `core/scripts/agent-resume-scaffold.sh` +
  `/start` Phase A-0 (already built — the resume path).
- **Does NOT call**: `/start`, `/stop`, `init-*.sh`, or the seed engine
  (`_seed_engine.py` strips domain content — the opposite of transplant).
- **Does NOT modify**: source identity/world/meta content (own-cloud flush is a
  sync, not a content edit); `.env.local`; any session-state file.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not
text. Terminal action for each sub-command is the final Bash echo carrying the
structured report (checklist location / archive path+size / land result /
verify PASS-FAIL summary). NEVER end a sub-command with text output.
