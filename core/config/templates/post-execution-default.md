# Domain Post-Execution Convention (default template)

Steps to follow after each goal execution during Phase 4.2 of the
aspirations loop. Evaluate conditions against the current goal context —
skip steps that do not apply.

This file was seeded from `core/config/templates/post-execution-default.md`
by `/start` Phase C0.5 on fresh-world setup. Edit freely; the convention
evolves through the feedback paths registered in
`core/config/conventions/domain-hooks.md` (Evolution → Mutation Sources)
with each change recorded in `world/conventions/convention-changes.jsonl`.

## Step 1: Infrastructure Health Recording

IF the goal involved infrastructure components (deployment, external
service interaction, API calls):
  Record outcome: Bash: bash core/scripts/infra-health.sh record <component> <success|failure> "<summary>"
  Pass result as behavioral_observation to Phase 4.5.

## Step 1.5: Run Testing Circuits

IF the goal produced code changes in the primary workspace:

  Invoke `/run-test-circuit post-change`
  (The skill detects changed repos, looks up circuits, runs lightweight-first.)

  IF result is FAIL:
    Do NOT proceed to Step 2 (Commit and Push).
    Fix the failing tests first.
    Re-run `/run-test-circuit post-change` after fixing.

  IF result is PARTIAL (some circuits skipped due to infrastructure):
    Log which circuits were skipped and why.
    Proceed to Step 1.75 — infrastructure-gated circuits should not block commits.

  IF result is PASS:
    Proceed to Step 1.75.

## Step 1.75: Fresh Eyes Code Review (Structured)

IF the goal produced code changes (new code written OR existing code
modified in any repo of the primary workspace):

  This step delegates to the structured `/fresh-eyes-code` skill — the
  single source of truth for the adversarial probe. Do NOT re-implement
  the review inline.

  Scope: reviews UNCOMMITTED code only. Step 1.75 + the post-state-update
  fresh-eyes failover form a pre/post-commit gate pair documented in
  `core/config/conventions/domain-hooks.md`.

  ### 1.75a. Capture pre-invocation timestamp
  Bash: date +%Y-%m-%dT%H:%M:%S

  Capture the output (a timestamp like `2026-05-09T13:42:11`). You will
  substitute this LITERAL value into Step 1.75d's command. Bash variables
  do not persist across separate Bash tool calls — the value lives only
  in your conversation context, not in any shell. Do not write `$pre_ts`
  into 1.75d's command; substitute the literal timestamp string.

  ### 1.75b. Build the changed-files list
  For each repo in the primary workspace:
    Bash: git -C <repo> diff --name-only HEAD
    IF output is non-empty: collect each path, prefixed with <repo>/ to
                            make it absolute.
  Concatenate all collected absolute paths into a space-separated list.

  IF the changed-files list is empty: skip Step 1.75 entirely — the goal
  reported code changes but no uncommitted diff exists. Proceed to Step 2.

  ### 1.75c. Invoke the structured fresh-eyes probe
  Invoke /fresh-eyes-code <absolute-path-1> <absolute-path-2> ...

  The skill runs the adversarial probe (platform, concurrency, error-paths,
  schema, n-agent, silent-failure, drift) and posts findings to
  `world/board/findings.jsonl` tagged `fresh-eyes-code`.

  ### 1.75d. Read findings from this invocation
  Bash: bash core/scripts/board-read.sh --channel findings --since "<TIMESTAMP-FROM-1.75a>" --tag fresh-eyes-code --author $MIND_AGENT --json

  Substitute the literal timestamp from 1.75a in place of
  `<TIMESTAMP-FROM-1.75a>`. The `--author $MIND_AGENT` filter scopes
  results to this agent — partner agents running /fresh-eyes-code in
  parallel would otherwise leak findings into this gate's input.

  ### 1.75e. Decision gate
  - IF any finding has severity in [invalidates, constrains]:
      Fix the bugs surfaced. Re-run Step 1.5 testing circuits.
      Do NOT proceed to Step 2 (Commit and Push).
      IF a fix is non-trivial: create an Unblock goal and stop.
  - ELIF only [enables, informs] findings: proceed to Step 2.
  - IF no fresh-eyes-code findings appear since the timestamp:
      Treat as review-not-run. Create an Unblock goal and stop. Failing
      open here would push unreviewed code, defeating the gate.

## Step 2: Commit and Push Code Changes

IF the goal produced code changes in the primary workspace:

  ### 2a. Scan for uncommitted changes
  For each repo directory in the primary workspace:
    Bash: git -C <repo> status --porcelain
    If output is non-empty → repo has uncommitted changes.

  ### 2b. For each repo with uncommitted changes from THIS goal

  **Pre-conditions (all must pass before pushing):**
  1. Changes are complete (not mid-refactor with broken state).
  2. Step 1.5 testing circuits passed (or were not applicable).
  3. Changes are on the correct branch (typically main).

  **IF all pre-conditions met:**
    git -C <repo> add <changed-files>
    git -C <repo> commit -m "<type>: <description>"
      Conventional commit format: feat / fix / test / ci / refactor.
      Include goal ID in commit body when relevant.

    **2b.1: Pre-Push Build Gate (MANDATORY — no bypass, no exceptions)**

    After commit, before push: run the repo's full build/test suite as
    specified in that repo's CLAUDE.md. This catches compilation errors
    and test failures BEFORE they reach CI.

    IF build FAILS: Do NOT push. Fix, amend or re-commit, re-test.
    IF build PASSES: proceed to push.

    This step is NOT redundant with Step 1.5 — Step 1.5 runs BEFORE code
    review (Step 1.75) and may pass on stale state. This step runs on the
    COMMITTED state and is the final gate before code enters CI.

    git -C <repo> push origin main
    Log: "Committed and pushed to <repo>: <commit-hash> (pre-push build verified)"

    **2b.2: Post-Push Deploy Verification (MANDATORY when the push triggers a deploy)**

    A push only places code on the remote. If pushing triggers a CI/CD
    pipeline that deploys to a live environment, the pipeline run — not the
    push — is what actually deploys. Verify the triggered run reached success
    BEFORE treating the deploy as done and moving on.

    IF the repo's push triggers a deploy pipeline (its CLAUDE.md or CI config
    defines one):
      Verify the run triggered by THIS push finished successfully, using the
      domain-specified deploy-verification mechanism (a companion script or
      pipeline-status query named in the domain post-execution convention).
      IF the run FAILED (or is still failing): the latest code is on the
        remote but NOT deployed. Do NOT proceed to Step 2c or Phase 5 verify —
        create an Unblock goal:
          "Unblock: <repo> deploy pipeline failed after push — <run/failure ref>"
      IF the run SUCCEEDED: Log "Deploy verified for <repo>: <run ref>" and
        proceed.
    ELSE (push does not trigger a deploy pipeline): skip — nothing to verify.

    This is NOT redundant with 2b.1: the pre-push build gate proves the code
    builds locally; this gate proves the remote pipeline actually deployed it.
    A green local build with a red deploy run leaves prod on the OLD code.

    **2b.3: Post-Deploy Service-Health Assertion (MANDATORY when the deploy affects a live service)**

    2b.2 proves the deploy RUN reached success — i.e. the code was DEPLOYED. It
    does NOT prove the deployed SERVICE is HEALTHY. A green run can still leave a
    service that crashed on startup, fails its health endpoint, or brought a
    dependency down. "Deployed" and "healthy" are different claims.

    IF the deploy affects a LIVE running service (not a pure client/config/docs
    push):
      After the run SUCCEEDED, assert the deployed service is healthy using the
      domain-specified service-health probe (a companion health/status script
      named in the domain post-execution convention) — probe with the CANONICAL
      companion script, never a synthetic curl/ssh (which misses the wrapper's
      auth headers / connection flags and false-negatives).
      IF healthy: Log "Service healthy post-deploy: <repo> <signal>" and proceed.
      IF unhealthy: the code shipped but the deploy DEGRADED the service. Do NOT
        proceed to Step 2c or Phase 5 verify as clean success — fix-forward or
        create an Unblock goal:
          "Unblock: <repo> deploy degraded service-health — <degraded signal>"
      IF the probe is UNREACHABLE (the probe infra itself down, not the service):
        state "service-health unverified: <detail>" — do NOT claim healthy from
        an unreachable probe (a silent-failure probe is zero signal).
    ELSE (push does not affect a live service): skip — run success suffices.

    This is NOT redundant with 2b.2: 2b.2 proves the pipeline deployed the code;
    this gate proves the deployed service came back healthy. A green run with a
    crashed service is a failed deploy that reads as success.

  **IF pre-conditions NOT met:**
    Log which pre-condition failed and why.
    Do NOT hold silently — create an Unblock goal:
      "Unblock: <repo> has uncommitted changes — <what's blocking>"

  ### 2c. Report results
  external_changes = list of {repo, commit_hash} for repos where commits
  were pushed. Consumed by Phase 4.5 for knowledge reconciliation.

## Fail-Open Policy

A failed Step 1 (infra-health record) logs and continues. Failed test
circuits, failed fresh-eyes findings, failed pre-push build gates, and
failed post-push deploy verification hold the goal in `pending` and create
an Unblock goal — they do NOT silently proceed to Phase 5 verify. Phase 4.2
returns with whatever `external_changes` and `behavioral_observations` were
collected.

## Downstream Consumers

- `aspirations-execute/SKILL.md` Phase 4.2 — invokes this convention
- Phase 4.5 — receives `external_changes` for knowledge reconciliation
- Phase 5 verify — relies on this convention to ensure tests passed and
  code shipped before declaring the goal complete
