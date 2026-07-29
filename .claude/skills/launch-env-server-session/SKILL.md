---
name: "launch-env-server-session"
forged: true
forged_by: zeta
forged_date: "2026-07-27"
forged_from: gap-027
description: "Launches an unattended env-server session end-to-end and tears it down: assembles the launch payload, invokes the customer cold-start path, polls EFS for startup completion or the Driver timeout, terminates, and verifies termination. Use whenever the agent needs a live env-server run — proving a deploy, reproducing a startup failure, running a FileWorld or web-playground or cli smoke session, or producing a fresh live-session measurement point. Fires on 'launch a smoke session', 'cold-start an env server', 'run a live env-server proof', 'verify the env terminated', or 'is the instance still billing'. MUST use this skill and its companion script rather than hand-rolling the API call — it encodes the client_type snake_case trap and the customer-path requirement, each of which cost a failed launch. NOT for Roblox Studio bridge sessions; use run-game-session for those. Emits structured facts only — diagnosis stays with the agent."
user-invocable: false
minimum_mode: autonomous
tools_used: [Bash]
companion_scripts:
  - "world/scripts/env-session-lifecycle.sh"
  - "world/scripts/efs-ssh.sh"
  - "world/scripts/aws-exec.sh"
conventions:
  - fileworld-smoke-launch-recipe
  - capability-routing
  - secrets
---

# Launch Env-Server Session

Mechanical harness for the env-server launch lifecycle: payload → invoke →
startup-poll → teardown → verify-terminated, plus the two diagnostic sub-steps
that make a failed run readable.

**Scope boundary — read this first.** This skill produces *facts*. Deciding what
they mean, root-causing a failure, and judging whether a run is a valid
measurement point all stay with the agent. A harness that also editorialises is
a harness you stop trusting.

## Restricted Operations

MUST use `world/scripts/env-session-lifecycle.sh` for every step below — never a
raw `curl`, raw `ssh`, or raw `aws` call. The script enforces boundaries the LLM
cannot bypass:

- Credentials resolve through `core/scripts/env-read.sh` and are consumed in the
  same invocation, never written to disk and never echoed (guard-724). The
  response is regex-redacted before it is printed.
- The launch body is validated against the server-side key patterns and the
  allowed client-type list *before* the call, so a malformed payload fails
  locally instead of as an opaque HTTP 400.
- Teardown and verification are idempotent, so re-running after an interrupted
  session cannot double-terminate or report a false "still billing".

Resolve the script path through `$WORLD_PATH` — `world/` is an external path and
the Bash hooks do NOT rewrite path arguments, so a bare `bash world/scripts/...`
dies rc=127 and reads exactly like a dead connection:

```
source core/scripts/_paths.sh
bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" <subcommand> ...
```

## Two launch gotchas this skill exists to encode

Both cost a failed attempt before they were written down. Neither is guessable.

1. **The request field is `client_type` (snake_case)** while every neighbouring
   field on the same endpoint is camelCase. Sending `clientType` returns HTTP 400
   and starts NO instance. Source of truth:
   `CollectAyoEnvironmentInBatchesOnStartUp/lambda_function.py:1023`.
2. **Use the CUSTOMER path, not a direct lambda invoke.** The customer endpoint
   is what writes `AyoServerEnvironment_OnStartup.json`. Without that sentinel the
   env-server polls for it silently and dies ~95s later emitting a 13-component
   false-flag list that reads as a broad remote/auth failure (g-335-297).

The auth header is `AYOAI-API-KEY`; its value comes from env var
`AYO_OPERATOR_KEY`. **These names differ on purpose** — conflating them produced a
false-absence blocker once already (g-335-152, rb-2515).

## Procedure

### Step 1 — Assemble and validate the payload

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" payload {envKey} {serverKey} [clientType] [worldFlagsJson]
```

Fails locally on a bad key pattern or a client type outside the allowed list.
Add `worldFlags` only for a non-file world.

### Step 2 — Launch

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" launch {envKey} {serverKey} [clientType] [worldFlagsJson]
```

Expect `{"http":200,...}` with `"status":"starting"`. On a 400, the script prints
the snake_case hint — check that before anything else. Record the serverKey; it
names the per-session EFS dir.

### Step 3 — Poll for startup

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" poll {envKey} {serverKey} [timeoutSec]
```

Exit codes: `0` ready · `2` Driver startup-timeout · `3` already terminated ·
`4` poll timeout. On `2`, go to Step 6 — the component list is the highest-signal
artifact the run produces.

### Step 4 — Teardown (never skip)

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" teardown {envKey} {serverKey}
```

HTTP 404 is SUCCESS here — already-gone is the desired end state.

### Step 5 — Verify terminated (the safety step)

`launch` returns `"instance_id": null` — the cold-start is async, so no instance
exists yet at response time. Resolve it by tag instead; the two calls chain:

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" verify-terminated \
        "$(bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" resolve-instance {envKey} {serverKey})"
```

`resolve-instance` prints a bare instance id, or `absent` when nothing matches —
and `verify-terminated absent` is a clean `verified: true`, because an instance
that does not exist IS terminated. So the chain is safe to run even when the
launch failed before any instance appeared.

**This is the step most likely to be dropped**, because a run that fails early
pulls attention to diagnosis and the instance keeps billing. Run it on every
launch, including — especially — failed ones. A non-terminated state returns
exit 1 with an explicit STILL BILLING action.

Arm a teardown owner BEFORE launching (guard-795). A hard-TTL watchdog is the
form that survives this session dying mid-run — a passive note is not a net:

```
nohup bash -c "sleep 900; bash '$WORLD_PATH/scripts/env-session-lifecycle.sh' teardown {envKey} {serverKey}" &
```

Register that live PID via `background-jobs.sh register` so `has-pending` returns
0 and the stop hook cannot let the session end quietly while an instance bills.
Registering a DEAD pid leaves `has-pending` at rc=1 — a record, not an obligation.

### Step 6 — Parse the Driver timeout list (only on a startup timeout)

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" parse-timeout {file|-}
```

Emits `complete` / `incomplete` sets plus a verdict:

- `root_missing` — `isCompleted_AyoEnvironmentList` is false. **Read the list
  topologically, not as peers.** The flags descend from an event DAG rooted at
  `completed.ayoEnvironmentOnStartup`; one missing sentinel fails every descendant
  at once. Treat N incomplete flags as ONE root cause, not N faults. Mis-reading
  this shape produced a credential-class hypothesis, a retracted 403 false
  positive, and a HIGH investigation goal — for a missing file (g-335-260).
- `downstream_failure` — root is complete, so the named flags are genuine
  independent failures.
- `all_complete` — nothing incomplete parsed.

### Step 7 — Probe the memory window (never trust mtime)

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" memory-window {envKey} {startEpochMs} {endEpochMs}
```

**mtime is a lie on this path.** The termination merge touches
`CellArchive.jsonl` on EVERY run whether or not cells formed, so a fresh mtime
reads as success and is not (rb-190, guard-1504). Compare row content against the
run window instead.

The schema key is `lastCompletedAt`, **not** `timestamp`. A timestamp-keyed filter
returns zero and looks authoritative — the rb-245 class. The script distinguishes
the two zeros for you: `no_content_in_window` (real) vs `schema_mismatch` (your
probe is wrong). Never report a bare zero without checking which one you have.

## Reading terminations: heal vs reap

```
Bash: bash "$WORLD_PATH/scripts/env-session-lifecycle.sh" lifecycle {envKey} [sinceIso]
```

A run's `TERMINATED` events are not all failures:

| shape | meaning |
|---|---|
| fixed second-boundary cluster, ~10–15 min gaps, `note: emergency-shutdown-hook` | documented SELF-HEAL, `MAX_HEAL_ATTEMPTS=3` |
| no `note`, `uptimeMs` ≈ 2014000 (1800s idle cutoff + 180s reaper) | genuine idle reap |
| no `note`, uptime under the cutoff | unclassified — investigate |

Do NOT collapse the heal gaps to a point estimate (rb-5223); two runs measured
12.9/10.0/15.0 and 11.7/10.0/15.0 min. The verified table lives in
`world/conventions/fileworld-smoke-launch-recipe.md` § VERIFIED SELF-REAP — read
it rather than re-deriving it. Treating a heal cluster as an unknown reaper
caused a redundant Investigate goal once already (g-335-301, closed as skipped).

## Error handling

| Symptom | Cause | Action |
|---|---|---|
| HTTP 400, no instance | `clientType` instead of `client_type` | fix the field name |
| env dies ~95s, 13 false flags | direct lambda invoke, sentinel never written | relaunch via the customer path |
| `AYO_OPERATOR_KEY absent` | looking for a var named `AYOAI_API_KEY` | that var does not exist; the header name is not the var name |
| rc=127 on the script | bare `world/scripts/...` path | source `_paths.sh`, use `$WORLD_PATH` |
| memory-window returns 0 | possibly the wrong schema key | check `schema_mismatch` vs `no_content_in_window` |

Authorization: a SINGLE bounded on-demand cold-start is agent-autonomous
(grant-004 / grant-006). A fleet or warm-pool launch is NOT. Arm an explicit
teardown owner on every launch (guard-795, permanent) — an explorer-ENABLED
session never goes idle and bills for ~24h (rb-4178).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the `verify-terminated` Bash call (or, when handing control
back mid-lifecycle, a Bash echo naming the next step). Never end with a text
summary of the run.
