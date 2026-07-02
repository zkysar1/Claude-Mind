# DynamoDB Shared-State & Coordination API — Ayoai-Mind decision + status

**Responds to:** omni's BRD `DynamoDB-Backed Shared-State & Coordination API`
(Zak-Data-Solutions-Mind/agents/omni/temp/, 2026-07-01).
**Decided/built in:** Ayoai-Mind (dev/frontier of the promotion chain
Ayoai-Mind → Claude-Mind → ZDS-Mind). Promotes downstream.
**Date:** 2026-07-01.

---

## 0. Re-baseline first (per BRD §10)

The BRD's line numbers were verified against a ZDS snapshot; Ayoai-Mind is the
frontier and had moved ahead. A read-only re-baseline of every FR against the
**live Ayoai-Mind tree** found the BRD is ~75% already implemented here — most
notably the provisioner it lists as MISSING already exists (`asp-328` shipped it).

| FR | BRD status | **Actual Ayoai-Mind state** |
|----|-----------|------------------------------|
| FR-1 activate ownership | inert | **done-inert.** `runner-claim.sh acquire` is wired into `/start` IDLE→RUNNING (start/SKILL.md), halts on a peer-held live claim. Zero code; env cutover only. |
| FR-2 single-runner lifecycle | wire it | **done-inert.** acquire (/start) + heartbeat (heartbeat-tick.sh dynamic branch) + release (graceful-stop D6.7/D6.8) + stale reclaim all wired + tested (`test_ownership_cutover.py`). |
| FR-3 provisioner | MISSING | **done.** `mind_api/scripts/provision_aws.py` — S3 bucket + DDB lock/sessions tables (TTL) + scoped env-id IAM user, idempotent, `--apply`/dry-run/`--mint-key`, dual-grant migration. |
| FR-6 env isolation | needs setup | **done.** `ENVIRONMENT_ID` threaded through `_session_key`; `list_runner_claims` env-scoped; tested. |
| FR-4 daemon auth | MISSING (biggest) | **was genuinely MISSING — built this session.** |
| FR-5 guarded remote bind | MISSING | **was genuinely MISSING — built this session.** |
| FR-7 runner-claims read endpoint | MISSING | **was MISSING — built this session.** |

Cutover runbooks already exist: `lodestar-machine-2-bring-up.md`,
`lodestar-two-machine-readiness.md`, `lodestar-dynamic-ownership-design.md`.

---

## 1. Architecture decision (BRD §7 / OQ-A): **Option A**

**Ship Option A** — scale the current own-cloud model: each machine runs its own
**local** daemon; all cross-machine safety (single-runner + mutual exclusion)
comes from the already-built **DynamoDB** lock + sessions tables; shared STATE is
S3 objects (eventually consistent, 120s sweep + real-time push).

Rationale:
- It is the safety-correct model (strong mutual-exclusion + single-runner via DDB
  conditional writes) and is **already built** — running more agents in parallel
  needs **no new code**, only real-AWS provisioning + the per-container env cutover.
- It directly removes the file-sync contention pain (S3+DDB replaces the
  OneDrive-synced JSONL store whose lock contention is already observable).
- Option B (one canonical remote daemon, strong read-consistency) is a larger
  build (remote bind + auth + SPOF hardening). Move to it **only if** a measured
  workload shows eventual read-lag on shared state causes coordination errors the
  DDB locks don't already prevent (BRD §7 recommendation). Ayoai-Mind already
  proves N-agents-one-world on 7 agents via exactly this machinery.

The FR-4/FR-5 auth + guarded-bind work below is the **enablement layer for Option
B / remote reach**, built now because it is the only genuine code gap and is
back-compatible (no-op unless enabled) — it does **not** change the Option-A
go-live path.

---

## 2. Built this session (Ayoai-Mind, agent-doable, no real AWS touched)

All new behavior is **opt-in via env var and back-compat** (NFR-1): with the new
env vars unset, the daemon is byte-identical to before (localhost-only, no auth).

- **FR-4 — daemon bearer-token auth.** `mind_api/src/server.py` `_serve()`: when
  `MIND_API_TOKEN` is set, every request must present a matching
  `Authorization: Bearer <token>` or is refused **401** before any handler/tenant
  check (constant-time compare). Clients attach it: `core/scripts/_runtime.sh`
  `rt_curl` + `core/scripts/_rt.py`. Daemon loads the token from `.env.local` via
  the `__main__.py` `_N3_ALLOWED_EXACT` allowlist.
- **FR-5 — guarded remote bind.** `server.py` `start()`: binds `127.0.0.1` by
  default; `MIND_API_BIND=<iface>` opts into a non-loopback bind and is
  **fail-closed** — refused unless `MIND_API_TOKEN` is set. So the daemon can
  never expose a routable interface without authentication.
- **FR-7 — runner-claims read endpoint.** `GET /v1/admin/runner-claims`
  (`mind_api/src/endpoints/admin.py`) returns the env-scoped runner-claim list
  (agent, machine_id, agent_state, heartbeat_at) for a fleet-health view. No-op
  empty list on the local backend.
- **FR-6 refinement — IAM `dynamodb:Scan`.** `provision_aws.py` `build_policy`
  now grants `dynamodb:Scan` on the sessions table (required by
  `list_runner_claims` / dynamic ownership + the FR-7 endpoint; was absent under
  static single-machine ownership).
- **AC6 — provisioner moto test.** `mind_api/tests/test_provision_aws.py` (new):
  bucket+tables+TTL creation, idempotency, dry-run, env-scoped policy + Scan grant,
  dual-grant, fail-fast. Closes the one pre-existing test gap on the provisioner.
- **FR-4/5/7 tests.** `mind_api/tests/test_runtime_auth.py` (new): 401 on
  missing/wrong/non-bearer token, 200 with valid token, back-compat when unset,
  bind-guard fail-closed, runner-claims endpoint.

Acceptance-criteria coverage (AC1 single-runner, AC2 failover, AC3 write
serialization, AC4 continuity, AC8 back-compat) already had thorough moto/e2e
tests; AC6 is now closed too.

---

## 3. Go-live checklist — run more agents in parallel (Option A)

The **code** is ready. What remains is operational and gated:

### Requires user (Zachary) sign-off — real AWS, per BRD §9 + guard-795
1. **Provision the fleet's AWS resources** (dry-run first, then apply):
   ```
   python mind_api/scripts/provision_aws.py --env-id <fleet-env-id> \
       --user <FleetUser> --bucket <fleet-bucket> \
       --lock-table <fleet-locks> --sessions-table <fleet-sessions>          # dry-run
   #   ... review the plan, then re-run with --apply --mint-key
   ```
   Use a **distinct `ENVIRONMENT_ID`** (and, recommended, distinct table names)
   so fleet coordination rows never collide with ZDS prod's `zds-*` rows (FR-6).
   **✓ DONE 2026-07-01** (user sign-off given) — see §6 for the actual
   provisioned resource names.
2. **Distribute the scoped `MIND_AWS_*` creds** (from `--mint-key`) to the fleet
   containers. Never into a tracked file (guard-724).
   **✓ DONE 2026-07-01** — written to the EFS **jar-keys** store (the canonical
   shared cred store fleet deploys read); see §6 for the key names + the
   jar-keys → container-env mapping omni consumes at bring-up.

### Owned by omni / zakbox1 control (fleet buildout, not the promotion chain)
3. Per-container `.env.local`: `STORAGE_BACKEND=own-cloud`, `MACHINE_ID=<unique>`,
   `ENVIRONMENT_ID=<fleet-env-id>`, the `STORAGE_S3_BUCKET`/`STORAGE_DDB_*` names,
   `MIND_AWS_*` creds, and `OWNERSHIP_MODE=dynamic` (activates FR-1/FR-2).
4. Follow `lodestar-machine-2-bring-up.md` for the sweep-off-first bring-up.
5. Validate against BRD §8 AC1–AC4 on the real fleet.

### Not needed for Option A go-live (only for Option B / remote reach)
- `MIND_API_TOKEN` + `MIND_API_BIND` (FR-4/FR-5) stay **unset** under Option A —
  each agent talks only to its own local daemon. Set them only when moving to a
  canonical remote daemon.

---

## 4. Deferred (Option B follow-on, if/when measured need arises)
- **Remote-client plumbing.** The FR-5 **server** bind is done; the **client**
  remote-reach path (pointing `rt_call`/`rt_curl` at a remote daemon URL) is not
  wired — it also touches the local auto-spawn logic and deserves its own change,
  not a half-build in the IRREDUCIBLY-LOCAL `_runtime.sh` hot path. Track as an
  Option-B goal.
- **Faster stale-reclaim tuning** for a dense fleet (BRD OQ-E) — the 900s default
  is env-tunable (`OWNERSHIP_STALE_SECONDS`); revisit under load.
- **Capacity/cost model** (BRD OQ-B): tables are PAY_PER_REQUEST (on-demand);
  measure the lock-write rate at 16 agents before considering provisioned mode.

---

## 5. Verification
- New: `mind_api/tests/test_runtime_auth.py`, `mind_api/tests/test_provision_aws.py`.
- Run: `python -m pytest mind_api/tests core/scripts/tests -q -m "not daemon_integration"`.
- Back-compat: with the new env vars unset, the daemon is byte-identical to before.

---

## 6. Provisioned + distributed — actual state (2026-07-01)

Real AWS resources are live and the scoped creds are distributed. This closes
go-live checklist items 1–2; the remaining work (items 3–5) is omni's fleet
bring-up, and the mapping table below is exactly what it needs.

### 6.1 Provisioned resources (account **891377285145**, region **us-east-2**)
| Resource | Name | Notes |
|----------|------|-------|
| `ENVIRONMENT_ID` | `claude-mind-fleet` | distinct from ZDS prod `ayoai-mind`/`zds-*` (FR-6 isolation) |
| S3 bucket | `claude-mind-own-cloud-data` | shared-state objects |
| DDB lock table | `claude-mind-locks` | PK `lock_key`; **TTL on `ttl`** (GC) |
| DDB sessions table | `claude-mind-sessions` | PK `session_key`; single-runner CAS rows |
| IAM user | `ClaudeMindFleet` | env-scoped least-privilege policy (S3 prefix `/claude-mind-fleet/*` + DDB Get/Put/Update/Scan on the two tables) |

Validated end-to-end with the scoped creds: S3 `Scan`/`GetItem` + lock
`PutItem`/`DeleteItem` succeed; `DescribeTable` correctly denied (not granted).

### 6.2 Cred distribution — how it works
The two **secret** creds live in the EFS **jar-keys** store
(`~/AyoAi-Efs/mnt/AyoAi/Jars/jar-keys`) — the canonical shared cred store the
DeployAyoaiOperator lambda (`read_jar_keys()`) and multiple repos read at
deploy/bring-up time. They are stored under **purpose-namespaced** key names
(matching the existing `GITHUB_AWS_*` precedent — never the bare `AWS_*`, which
belongs to the operator deploy):

| jar-keys key (secret, on EFS) | → maps to container env var (what the code reads) |
|-------------------------------|---------------------------------------------------|
| `CLAUDE_MIND_FLEET_AWS_ACCESS_KEY_ID` | `MIND_AWS_ACCESS_KEY_ID` |
| `CLAUDE_MIND_FLEET_AWS_SECRET_ACCESS_KEY` | `MIND_AWS_SECRET_ACCESS_KEY` |

`core/scripts/owncloud_backend.py::from_env()` reads `MIND_AWS_ACCESS_KEY_ID` +
`MIND_AWS_SECRET_ACCESS_KEY` from the process env (fail-closed unless
`MIND_AWS_ALLOW_DEFAULT_CHAIN=1`). So the bring-up flow is:

1. Fleet bring-up reads the two `CLAUDE_MIND_FLEET_AWS_*` values from jar-keys
   (`efs-ssh.sh "grep '^CLAUDE_MIND_FLEET_AWS_...=' ~/AyoAi-Efs/.../jar-keys"`),
   piping each value straight into the container env — **never** into a tracked
   file (guard-724).
2. Each container's daemon reads `MIND_AWS_*` from its env and connects to the
   `claude-mind-*` S3/DDB with the scoped creds.

Retrieve for a container (value → stdout, never chat/tracked file):
```
efs-ssh.sh "grep '^CLAUDE_MIND_FLEET_AWS_ACCESS_KEY_ID=' ~/AyoAi-Efs/mnt/AyoAi/Jars/jar-keys | cut -d= -f2-"
```

### 6.3 Full per-container env (items 3–5, omni-owned)
Secret (2 keys, from jar-keys per §6.2) plus these **non-secret** config values:
```
STORAGE_BACKEND=own-cloud
ENVIRONMENT_ID=claude-mind-fleet
STORAGE_S3_BUCKET=claude-mind-own-cloud-data
STORAGE_DDB_LOCK_TABLE=claude-mind-locks
STORAGE_DDB_SESSIONS_TABLE=claude-mind-sessions
AWS_DEFAULT_REGION=us-east-2
OWNERSHIP_MODE=dynamic          # activates FR-1/FR-2 single-runner enforcement
MACHINE_ID=<unique-per-container>
MIND_AWS_ACCESS_KEY_ID=<from CLAUDE_MIND_FLEET_AWS_ACCESS_KEY_ID>
MIND_AWS_SECRET_ACCESS_KEY=<from CLAUDE_MIND_FLEET_AWS_SECRET_ACCESS_KEY>
```
The non-secret block is safe to template into a runbook; only the two
`MIND_AWS_*` lines carry secrets and MUST come from jar-keys at launch.

---

## 7. Ayoai finish-pass findings (2026-07-01)

A live-daemon verification pass (dogfooding the FR-7 endpoint against the
running 6-agent Ayoai fleet) surfaced three things worth recording.

### 7.1 FR-7 hardened — actionable hint on the Scan-permission failure
Dogfooding `GET /v1/admin/runner-claims` on the live daemon returned
`AccessDeniedException ... dynamodb:Scan on zds-sessions`. The endpoint already
degraded (500 + error, never a stack), but the raw boto message is poor operator
UX. It now detects the AccessDenied-on-Scan case and adds an actionable `hint`
("grant dynamodb:Scan … the acquire/heartbeat/release trio is unaffected").
Covered by `test_runner_claims_scan_accessdenied_returns_actionable_hint`
(mocked denied backend; no real AWS).

### 7.2 Live IAM gap — the EXISTING dev creds lack `dynamodb:Scan` (user sign-off)
The running Ayoai fleet uses `own-cloud` against the **prod `zds-*` tables** under
`ENVIRONMENT_ID=ayoai-mind` with IAM user `Zak_first_test`, whose policy predates
FR-6/FR-7 and grants Get/Put/Update but **not `Scan`** on `zds-sessions`. So the
FR-7 fleet-health read errors *on the current dev fleet* — while the ownership
trio (acquire/heartbeat/release, which don't Scan) works fine. This is NOT a code
gap: the provisioner policy (`build_policy`) already grants Scan, so the NEW
`ClaudeMindFleet` user (and any freshly-provisioned env) is unaffected. Closing it
for the dev fleet needs an **IAM change on `Zak_first_test` (grant `dynamodb:Scan`
on `zds-sessions`) — a user-sign-off action per §9**, not an autonomous edit. It
does not block omni's fleet.

### 7.3 Verification state — what's proven vs. what only the fleet can prove
| AC | Status | Note |
|----|--------|------|
| AC5 auth (FR-4/5) | ✅ unit-verified | `test_runtime_auth.py` (401/200, fail-closed bind) |
| AC6 provisioner | ✅ verified on real AWS | moto tests + the live provisioning run |
| AC7 isolation | ✅ provisioned | distinct env + `claude-mind-*` tables |
| FR-7 observability | ✅ built + hardened | works on a Scan-granted env; degrades with a hint otherwise |
| AC8 back-compat | ◑ intent met, clause blocked | own-cloud-off = byte-identical (DynamoDB suite green). The literal "full `core/scripts/tests` green" clause is blocked by **unrelated, heterogeneous** test-double regressions in OTHER features (e.g. `_FakePaths` missing `wm_path` in wm_write tests, missing `world` in curriculum tests) — each the owning feature's fix, NOT this work. |
| AC1 single-runner | ○ code-ready, unproven | `OWNERSHIP_MODE` is `static` (OFF) fleet-wide; unit-tested (`test_ownership_cutover.py`) only |
| AC2 failover | ○ code-ready, unproven | `reclaim_if_stale` unit-tested; never run on real containers |
| AC3 cross-machine write serialization | ○ not exercisable here | the whole Ayoai fleet is single-machine (`MACHINE_ID=DESKTOP-O91DLK2`) |
| AC4 continuity across machines | ○ code-ready, unproven | single machine |

**Bottom line.** Everything agent-doable + everything under user sign-off
(provision + distribute) is done and verified at the code/resource level. FR-1/2
lifecycle wiring is complete (acquire at `/start`, heartbeat at `heartbeat-tick`,
release at graceful-stop) but **inert** (`OWNERSHIP_MODE=static`). AC1–AC4 — the
cross-machine single-runner guarantees the BRD exists for — are **code-ready but
unprovable on the Ayoai side** (single machine, ownership off). They move to
"proven" only on omni's ≥2-container fleet with `OWNERSHIP_MODE=dynamic`. Cheapest
first proof: one second container (distinct `MACHINE_ID`) + dynamic → run AC1.
