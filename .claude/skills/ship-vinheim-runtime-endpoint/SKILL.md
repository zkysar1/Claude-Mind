---
name: ship-vinheim-runtime-endpoint
description: "Ships a runtime capability as an env-server + Lodestar-gateway endpoint PAIR for the Vinheim runtime, then live-verifies it end-to-end via the operator API. Fires whenever a goal adds or changes a Vinheim runtime endpoint that needs BOTH an env-server charactersRouter route AND a matching handleVinheimRuntime gateway route in one ship — goal phrasings 'apply endpoint-pair', 'add a runtime endpoint', 'ship the env-server + gateway route', 'Vinheim IA follow-up', 'two-repo runtime ship'. MUST use whenever env-server AND Lodestar must change together for one capability — never hand-run the two-repo ceremony. Covers the SIS service/verticle split (registration deferred to the completed service, never the 90s startup gate), the standard gateway fences, mirrored vitest, both full suites, env-server direct-push to main, the Lodestar PR (via ship-product-pr), operator live-verify (Collect cold-start, status poll, port-8686 probes, DELETE), and the board contract reply."
forged: true
forged_by: alpha
forged_date: "2026-07-21"
forged_from: gap-017
user-invocable: false
minimum_mode: autonomous
tools_used: [Bash, Read, Edit, Grep]
companion_scripts:
  - world/scripts/dev-envserver-available.sh
  - world/scripts/deploy-sha-probe.sh
  - world/scripts/operator-api.sh
  - world/scripts/efs-ssh.sh
  - world/scripts/product-pr-flow.sh
conventions:
  - sis-forced-aspiration-endpoint.md
  - cross-repo-lodestar.md
  - environment-task-api.md
  - post-execution.md
  - deploy-state-verification.md
  - post-deploy-behavioral-validation.md
  - operator-ramp.md
  - service-endpoints.md
compose_with:
  - ship-product-pr
---

# /ship-vinheim-runtime-endpoint — Env-server + Gateway Endpoint-Pair Ship

Ships one Vinheim runtime capability across BOTH repos at once — the env-server
(charactersRouter route) and the Lodestar gateway (`handleVinheimRuntime` route)
— then live-verifies the pair end-to-end through the operator API and replies to
the requesting agent on the board.

This is the full two-repo ceremony plus the live-verify leg. The Lodestar PR
mechanics are delegated to `/ship-product-pr` (gap-011); this skill owns the
env-server leg, the endpoint-pair contract (fences that must match on both
sides), and the operator live-verify — the expensive, error-prone part worth
standardizing. Encountered 3× (g-335-103/104/105, Vinheim IA follow-ups).

## Step 0: Load Conventions

`Bash: load-conventions.sh` with each name from the `conventions:` front matter.
Read only the paths returned. If output is empty, all conventions already loaded
— proceed.

The conventions carry the domain particulars this body references by pointer
(keeping the procedure here and the specifics there):
- `sis-forced-aspiration-endpoint.md` — SIS service/verticle split pattern
- `cross-repo-lodestar.md` — gateway repo layout + `handleVinheimRuntime` fences
- `environment-task-api.md` — env-server task API + `AYOAI-API-KEY`
- `deploy-state-verification.md` + `post-deploy-behavioral-validation.md` — verify legs
- `operator-ramp.md` + `service-endpoints.md` — operator API surface (Collect / status / DELETE)
- `post-execution.md` — commit/push/PR + full-suite gate

## Restricted Operations

This skill touches DOMAIN infrastructure (env-server main, the Lodestar repo,
the operator API, a live environment). It MUST use the companion scripts for
every restricted operation — never raw `ssh`, raw `curl`, or a hand-built
operator request:

- **Operator API** — MUST use `world/scripts/operator-api.sh <METHOD> <path> [body]`
  (adds the `AYO-OPERATOR-KEY` header). Never raw `curl` to the operator host.
- **Env inspection over EFS** — MUST use `world/scripts/efs-ssh.sh "<cmd>"`
  (carries the StrictHostKeyChecking flags). Never raw `ssh`.
- **DEV deploy-SHA confirmation** — MUST use `world/scripts/deploy-sha-probe.sh`
  (or `operator-deploy-sha.sh`) to confirm the pushed SHA landed. Never assume
  the push auto-deployed — probe it.
- **Env-server availability** — MUST use `world/scripts/dev-envserver-available.sh`
  before the live-verify leg.
- **Lodestar PR** — MUST delegate to `/ship-product-pr` (companion
  `world/scripts/product-pr-flow.sh`). Never hand-run branch/commit/push/PR for
  the gateway repo.

Credentials are resolved by the scripts via `core/scripts/env-read.sh` — never
hardcode a key here.

## Inputs

- `capability`: one-line description of the runtime capability being shipped
  (e.g., "optional agentKeys[] on POST /start — roster selection at launch").
- `env_server_repo`: path to the env-server checkout (under `AGENT_WRITE_PATH`).
- `gateway_repo`: path to the Lodestar gateway checkout (under `AGENT_WRITE_PATH`).
- `requesting_agent`: the agent that filed the contract (for the Step 9 board reply),
  or null if self-originated.
- `endpoint_contract`: the request/response shape both sides must agree on.

## Procedure

### Leg 1 — Env-server: SIS service/verticle split

The env-server route follows the SIS pattern (`sis-forced-aspiration-endpoint.md`).
Do NOT register the route in the 90-second startup gate — registration is deferred
to the completed service so a slow dependency never fails the boot gate.

1. Add a `BodyHandler` sub-router on the `charactersRouter` for the new path.
2. Register the route ONLY from `completed.CharactersListService` (the SIS
   "route registration deferred to completed service" rule) — NEVER inline in
   the verticle's `start()` / the 90s startup gate.
3. Driver: add the `isCompleted_` flag + the `deploy` method for the new service
   so the deferred registration fires when the driver reports completion.
4. Implement the handler: validate inputs, apply the capability, return the shape
   named in `endpoint_contract`.

### Leg 2 — Gateway: `handleVinheimRuntime` route cloning the standard fences

In the Lodestar gateway, add the mirror route inside `handleVinheimRuntime`
(`cross-repo-lodestar.md`). Clone the standard fence ladder EXACTLY — the pair
contract is that the gateway rejects before it ever calls env-server:

1. **Auth**: Cognito `sub` present + `ownerSub` match — else reject.
2. **Stopped env** → `409 not_running`.
3. **Hostname does not resolve** → `409 not_ready`.
4. **`envServerFetch`** with the `AYOAI-API-KEY` header (never a bare fetch).
5. **Env-server `404`** → relay as `400` NAMING the missing key (not a bare 400).
6. **Any non-200 from env-server** → `502`.

The fence order and status codes MUST match an existing `handleVinheimRuntime`
route verbatim — copy the nearest sibling route and change only the path +
payload. (implementation-discipline: touch only what the capability requires.)

### Leg 3 — Tests: mirrored vitest + both full suites

1. Add vitest cases on the runtime test file mirroring EVERY fence above
   (not_running, not_ready, 404→400, non-200→502, happy-path 200) — one case
   per fence, cloned from the sibling route's cases.
2. Run BOTH full suites (env-server AND gateway) —
   `.claude/rules/run-full-suite-after-deep-code.md`: targeted tests are
   necessary but NOT sufficient. Product-repo full suite per `post-execution.md`
   Step 2.b.1. Do NOT claim "tests pass" on a targeted subset.

### Leg 4 — Env-server ship: direct-push main (DEV auto-deploys)

Env-server DEV auto-deploys from `main` (rb-3542) — there is no PR for this leg.

1. Commit the env-server change (surgical diff, message names the capability).
2. Direct-push to `main`.
3. Confirm the DEV deploy landed: `world/scripts/deploy-sha-probe.sh`
   (or `operator-deploy-sha.sh`) — the pushed SHA MUST match the deployed SHA
   before the live-verify leg. Never assume; probe (`deploy-state-verification.md`).

### Leg 5 — Gateway ship: Lodestar PR via /ship-product-pr

The gateway leg goes through a PR (grant-002 authorizes framework/promotion
merges; a runtime endpoint PR follows the standard product-repo review gate — do
NOT auto-merge unless grant-002 covers it).

1. Invoke `/ship-product-pr` with the gateway repo + the committed change —
   it runs branch → commit → push → PR → CI, using `product-pr-flow.sh`.
2. Wait for CI green, then merge per the repo's authorization (grant-002 /
   review gate). Do NOT hand-run the branch/PR flow — delegate it.

### Leg 6 — Operator-level live VERIFY (the expensive leg)

Verify the shipped pair against a LIVE environment through the operator API
(`operator-ramp.md`, `service-endpoints.md`, `post-deploy-behavioral-validation.md`).
MUST use `operator-api.sh` for every call:

1. `dev-envserver-available.sh` — confirm the env-server is reachable.
2. **Collect** a cold-start environment:
   `operator-api.sh POST /Collect '{...}'` — capture the returned server id.
3. **Poll** `GetStreamingUrlAndStatus` until the host is ready:
   `operator-api.sh POST /GetStreamingUrlAndStatus '{"serverId":"<id>"}'` —
   loop with backoff until status=ready + a hostname is returned.
4. **Probe** the new endpoint on the live host at `https://<host>:8686` with the
   `AYO_OPERATOR_KEY` — exercise the happy path AND at least one fence (e.g. the
   404→400 relay) to prove the pair contract holds end-to-end.
5. **DELETE** the server when done:
   `operator-api.sh DELETE /server/<id>` (or the documented teardown path) —
   NEVER leak a live cold-start environment.

If any live probe fails: the ship is NOT verified. File an Unblock (do not
declare success), and — if the env-server leg is at fault — the fix re-enters at
Leg 1; if the gateway leg, at Leg 2.

### Leg 7 — Board contract reply

Reply to the requesting agent's contract on the board, stating the pair is
shipped + live-verified with evidence (the deployed SHA, the merged PR URL, the
live-probe result):

```
echo "Shipped + live-verified endpoint pair for {capability}: env-server SHA {sha} (DEV auto-deployed), gateway PR {pr-url} (merged), live-probe {result} on cold-start {server-id} (torn down)." \
  | bash core/scripts/board-post.sh --channel coordination --type status \
    --reply-to {contract-msg-id} --tags "endpoint-pair,{requesting_agent},vinheim"
```

If self-originated (no `requesting_agent`): post to `findings` instead of a reply.

## Output Contract

Returns (to the parent skill / goal execution):
- `env_server_sha`: the deployed env-server SHA (probe-confirmed).
- `gateway_pr_url`: the merged Lodestar PR URL.
- `live_verify`: `pass` | `fail` with the probe evidence.
- `server_id_torn_down`: the cold-start server id that was DELETEd.
- `board_reply_id`: the contract-reply message id.

A ship is COMPLETE only when all five are present AND `live_verify == pass`.

## Error Handling

- **Startup-gate regression** (env-server boot fails the 90s gate): the route was
  registered inline instead of deferred to `completed.CharactersListService`.
  Move registration into the completed service (Leg 1 rule 2). This is the #1
  SIS-pattern mistake.
- **Fence mismatch** (gateway returns a different code than the sibling route):
  the fence ladder was not cloned verbatim. Diff against the nearest
  `handleVinheimRuntime` route (Leg 2).
- **Deploy SHA mismatch** (pushed ≠ deployed): DEV auto-deploy has not landed
  yet — re-probe with backoff (`deploy-sha-probe.sh`); do not start live-verify
  until they match.
- **Live probe 502**: env-server returned non-200 — inspect the env-server logs
  over `efs-ssh.sh`; the env-server leg (Leg 1/4) is the fault, not the gateway.
- **Leaked cold-start env**: if any leg aborts after Collect, STILL run the
  DELETE teardown (Leg 6 step 5) before returning — never leave a live server.
- Any restricted-op failure surfaces via CREATE_BLOCKER, not a silent skip
  (`.claude/rules/error-response.md`).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Leg 7 `board-post.sh` contract reply (or the findings
post when self-originated). Never end with a text summary of the ship.
