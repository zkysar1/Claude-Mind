---
name: build-operator-job
forged: true
forged_by: alpha
forged_date: "2026-07-12"
description: "Author, register, deploy, and consume a new Ayoai-Operator scheduled job — the deterministic-work RAMP. Use when a recurring check/sweep/probe currently runs as a Mind recurring goal (burning LLM tokens every cycle) and could instead run DETERMINISTICALLY on the operator on a fixed interval, with Mind PULLING the results via the audit-trail API instead of doing the work. Also use to build any always-on operator monitor/cleanup/report job (2-file + test recipe: a TaskVerticleBase subclass + a TaskRegistry entry). Worked example: EnvServerWatchdogVerticle. Triggers: build operator job, add operator task, move recurring goal to operator, make it deterministic, operator ramp."
user-invocable: true
triggers:
  - "/build-operator-job"
  - "build an operator job"
  - "add an operator task"
  - "move this recurring goal to the operator"
  - "make this check deterministic"
  - "operator ramp"
parameters:
  - name: job-intent
    description: "One line: what deterministic work should run on the operator (e.g. 'sweep stale EFS session dirs older than 3 days', 'probe every env-server every 90s')."
    required: false
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [aspirations, board]
minimum_mode: assistant
revision_id: "skill-bootstrap-build-operator-job-7f3c11"
previous_revision_id: null
---

# /build-operator-job — Deterministic-Work Ramp

Move deterministic, recurring work OFF the Mind LLM loop and ONTO the
Ayoai-Operator, where it runs on a fixed schedule for zero LLM tokens. Mind then
**pulls** the results (audit trail / alerts) instead of doing the work every
cycle. This is the ramp: the operator does the doing on many frequencies; Mind
does the thinking about what the results mean.

## When to build an Operator job (vs. keep it a Mind recurring goal)

Build an operator job when ALL hold:
1. **Deterministic** — the work is a fixed procedure (probe an endpoint, sweep a
   dir, query a table, diff a state), not open-ended reasoning.
2. **Recurring on a clock** — it fires on an interval or a daily time, not in
   reaction to a novel situation.
3. **Result is checkable** — success/failure + a small payload that Mind (or a
   human) can read later, not a judgement that needs the LLM in the loop.

Keep it a Mind goal when the work needs retrieval, judgement, cross-store
synthesis, or writes to Mind's own learning stores.

**Token math (why this matters):** a recurring goal that fires 4–9×/day costs a
full LLM iteration each time (select → execute → verify → encode). The same probe
as an operator job costs ~0 LLM tokens; Mind reads the batched result once. The
2026-07 migration candidates (infra-streak-notify 9×/day, alert-sweep 4×/day,
probe-prod 4×/day, …) were estimated at ~200–240K Mind tokens/day saved.

## The minimal diff — 2 production files + 1 test

Everything below is verified against the codebase (EnvServerWatchdogVerticle,
2026-07-12). The `Driver` auto-discovers and deploys every registered task via
`Class.forName` — **there is no manifest, no Driver edit, no CI edit.**

### File 1 — the job: `Ayoai-Operator/src/main/java/AyoOperator/Tasks/<Name>Verticle.java`

- Extend **`TaskVerticleBase`** (which extends Vert.x `AbstractVerticle`).
- Implement the single abstract method: `protected abstract void executeTask(JsonObject request)`.
  `request` carries `taskName`, `triggeredAt`, `triggeredBy`, `dispatchTime`.
- Override `start()` for client init and **call `super.start()`** (registers the
  event-bus consumer on `task.<taskname_lowercase>.execute` — skip it and the job
  never fires). Override `stop()` for cleanup (close AWS/Web clients).
- Report results with the base helpers:
  - `publishTaskCompleted()` / `publishTaskCompleted(JsonObject data)` — success.
  - `handleTaskError(String msg, Throwable cause)` — recoverable failure (logs +
    publishes; does NOT kill the operator).
  - Logging levels: `basic()` `medium()` `high()` `data()`. **NEVER call
    `critical()`** unless the operator instance itself must terminate.
- Class name MUST be `<RegistryName>Verticle` in package `AyoOperator.Tasks`, with
  a public no-arg constructor (inherited).

### File 2 — register it: `Ayoai-Operator/src/main/java/AyoOperator/TaskRegistry.java`

Add one builder chain inside the `static {}` block:

```java
register("<Name>")
  .description("<one line for the dashboard/API>")
  .interval(90, TimeUnit.SECONDS)     // REQUIRED — build() fails if unset
  .timeout(2, TimeUnit.MINUTES)       // optional; default 5-min force-clean
  .category(CATEGORY_MONITORING)      // health|monitoring|reporting|cleanup|analytics|security|integration|performance|infrastructure
  .priority(2)                        // lower = higher priority (display/sort)
  .startupGroup(3)                    // 1=immediate, 2=dependent, 3=monitoring (runs after 1+2; use for probes that must NOT fire during cold start)
  .build();
```

Other builder methods: `.enabled(false)` (disable without removing),
`.preferredTime("08:00")` (daily UTC calendar schedule instead of interval),
`.exclusive(minutes)` (run alone). The registry name → class mapping is
`getVerticleClassName() = "AyoOperator.Tasks." + name + "Verticle"` — keep them in
lockstep.

### File 3 — test: `Ayoai-Operator/src/test/java/AyoOperator/Tasks/<Name>VerticleTest.java`

Test the DECISION LOGIC as **pure `static` package-visible methods with an
injected clock** (`long now`), JUnit 5, no Vert.x runner, no AWS, no mocks. Extract
the branching into `static Decision computeX(State s, …, long now)` and unit-test
every path deterministically (see EnvServerWatchdogVerticleTest — 12 tests, `NOW`
constant injected). Test class MUST be `public`.

## AWS access

Call the factory in `start()` — the factory is already initialized by the Driver
before any verticle deploys (do NOT call `AwsClientFactory.initialize()`):

```java
ec2Client = AwsClientFactory.createEc2Client();   // also createSsmClient/createS3Client/createDynamoDbClient/createLambdaClient/…
```

For async AWS calls that touch Vert.x (event bus, logging) from the callback,
bridge with the context:

```java
Future.fromCompletionStage(ec2Client.describeInstances(req), vertx.getOrCreateContext())
      .onComplete(ar -> { ... });
```

## Scheduling & serialization (free, automatic)

`ScheduledTaskVerticle` re-arms each run via `setTimer` after completion, and an
IDLE gate (`scheduler.canTaskExecute`) refuses to dispatch a task that is still
EXECUTING — so same-task runs never overlap. You get this for free; do not
re-implement it in the job.

## Deploy

Push to `main`. `.github/workflows/build-deploy-operator.yml` runs
`./gradlew test` → `shadowJar` → uploads the fat jar to
`s3://ayoai-jar-deployments/operator/<sha>/Ayoai-Operator.jar` → invokes the
`DeployAyoaiOperator` lambda, which hot-deploys to the running operator EC2 (jar
lands at EFS `/home/ec2-user/AyoAi-Efs/mnt/AyoAi/Jars/Ayoai-Operator.jar`). No
manual step. Verify via the operator API (below) that the new task appears.

## How Mind PULLS the results (the ramp payoff)

Every execution auto-records to DynamoDB **`AyoaiAdminAuditTrail`**
(PK `Task#YYYY-MM-DD`, SK `HH:mm:ss.SSS#task-<name>-<epochMs>`, 7-day TTL): a
`triggered` row at dispatch and a `success`/`failed` row with `Details` at
completion. Mind reads results WITHOUT doing the work:

```bash
# via the existing Mind→Operator client (AYOAI-API-KEY from $AYO_OPERATOR_KEY)
bash world/scripts/operator-api.sh GET "/operator/v1/audit-trail?date=$(date +%Y-%m-%d)&taskName=<Name>&limit=20"
bash world/scripts/operator-api.sh GET "/operator/v1/tasks"        # current health / next-run per task
```

Use the existing forged skills `access-operator-api` and `operator-health-trend`
for this. **Convert the retired recurring goal into a lightweight PULL goal**: a
low-frequency Mind goal that reads the audit-trail rows and only escalates (files
an Investigate / notifies) when a result is `failed` or anomalous. That is the
model flip — Mind stops doing the check and starts reacting to the operator's
verdict.

For push-style urgency, the job can email via
`LambdaAlertService.sendErrorAlert(vertx, msg, "<Name>")` /
`sendInfoAlert(...)` (→ `SendErrorAlert`/`SendInfoAlert` lambda → SES → the
`ayoaimail` bucket), which Mind reads with the `access-email` skill.

## Migration procedure (recurring goal → operator job)

1. Confirm the goal is deterministic (the 3-point test above). If it needs
   retrieval/judgement, STOP — it stays a Mind goal.
2. Author the job (Files 1–3) implementing the goal's exact procedure.
3. Deploy; confirm `GET /operator/v1/tasks` lists it and the first audit rows land.
4. Replace the Mind recurring goal with a PULL goal (lower frequency) that reads
   the audit trail and escalates only on `failed`/anomaly. Cross-link the goal to
   the job name.
5. Encode: rb entry (what moved + why), and note the token-saving in the goal.

## Worked example

`EnvServerWatchdogVerticle` (interval 90s, category monitoring, startupGroup 3):
probes each env-server's `:8686/reportapi/serverDetail`, and heals frozen
FILE-WORLD servers in place via SSM `systemctl restart` — with the entire
safety-critical decision logic (`isHealable`, `computeDecision`) as pure static
methods covered by 12 injected-clock tests. Read it end-to-end before authoring a
new job; it is the reference for every convention above.

## Return Protocol

See `.claude/rules/return-protocol.md` — terminate with a tool call, never a text
summary. When this skill finishes authoring/guiding, end with the Bash call that
runs the test or the operator-api verification (`./gradlew test` for the new job,
or `operator-api.sh GET /operator/v1/tasks` to confirm deployment), not a prose
wrap-up.

## Chaining

- **Called by**: user (`/build-operator-job`), or Mind when migrating a
  deterministic recurring goal off the LLM loop.
- **Calls / relates to**: `access-operator-api` + `operator-health-trend` (pull
  results), `access-aws-services` (lambda/aws), `access-email` (read alerts),
  `access-efs-data` (verify EFS-side effects).
- **Reads**: `Ayoai-Operator/src/.../TaskVerticleBase.java`, `TaskRegistry.java`,
  `AwsClientFactory.java`, `EnvServerWatchdogVerticle.java` (the reference job).
- **Modifies**: the Ayoai-Operator repo (new Verticle + TaskRegistry entry +
  test); on the Mind side, converts a recurring goal to a pull goal.
