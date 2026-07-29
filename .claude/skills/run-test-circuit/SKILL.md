---
name: "run-test-circuit"
forged: true
forged_by: alpha
forged_date: "2026-04-04"
description: "Runs repo-aware testing circuits: detects which AyoAI repos changed, looks up each repo's test commands from its CLAUDE.md, runs lightweight (unit) tests first, escalates to integration tests on success, and reports pass/fail with failure diffs. Use whenever code changes span one or more repos and the agent needs a go/no-go signal before committing or deploying (guard-013 / guard-014 workflow). Also use when /run-game-session or deploy fails and the agent needs to pinpoint the broken repo."
user-invocable: false
minimum_mode: autonomous
tools_used: [Bash]
companion_scripts:
  - world/scripts/test-circuit-run.sh
  - world/scripts/test-circuit-detect.sh
conventions: [testing-circuits, infrastructure]
revision_id: "skill-bootstrap-run-test-circuit-d92f57"
previous_revision_id: null
---

# /run-test-circuit — Execute Testing Circuits

Determines which test circuits to run based on changed repos, executes them
in lightweight-to-heavyweight order (syntax → unit tests → smoke → integration),
and reports pass/fail. Stops cascade on first failure.

## Sub-commands

```
/run-test-circuit post-change [RepoName]   # Primary: detect changes, run circuits
/run-test-circuit check [RepoName]         # Dry-run: show circuits without executing
/run-test-circuit circuit <circuit-name>    # Run a specific circuit by name
```

## Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## `post-change [RepoName]` — Primary Invocation

Called by the post-execution convention after code changes. Also referenced in
12+ CLAUDE.md files as `/run-test-circuit post-change <RepoName>`.

### Step 1: Detect Changed Repos

```
IF RepoName argument given:
    repos = [RepoName]
ELSE:
    # Scan for repos with uncommitted changes
    repos = []
    Bash: for d in "$PRIMARY_WORKSPACE"/*/; do
            if [ -n "$(git -C "$d" status --porcelain 2>/dev/null)" ]; then
              basename "$d"
            fi
          done
    Parse output into repos list
    IF repos is empty:
        Output: "No changed repos detected."
        RETURN PASS
```

### Step 2: Detect Circuits

```
# Scripts live in the world directory (shared across agents)
# Resolve WORLD_DIR via: source core/scripts/_paths.sh
Bash: bash "$WORLD_DIR/scripts/test-circuit-detect.sh" --repos <comma-separated-repos>
# Returns JSON array: [{circuit, repo, reason}, ...]
Parse into circuit_list (already ordered lightweight-first)
Output: "▸ Circuits to run: {circuit names}"
```

### Step 3: Infrastructure Gates

Infrastructure gates are domain-specific — `test-circuit-detect.sh` output includes
gate metadata (required credentials, required infrastructure) per circuit.

For each circuit in circuit_list:
```
IF circuit.gate requires credentials:
    source "$PRIMARY_WORKSPACE/.env.local"
    IF required key is empty: mark circuit SKIP, reason "missing credentials"

IF circuit.gate requires infrastructure:
    Bash: bash core/scripts/infra-health.sh check {circuit.gate.component}
    IF status != ok: mark circuit SKIP, reason "{component} not reachable"
```

### Step 4: Execute Circuits (Lightweight → Heavyweight)

```
results = []
FOR EACH circuit in circuit_list:
    IF circuit is marked SKIP:
        results.append({circuit, status: "skip", reason})
        Output: "▸ SKIP {circuit}: {reason}"
        CONTINUE

    Bash: bash "$WORLD_DIR/scripts/test-circuit-run.sh" {circuit.name} {circuit.repo}
    Parse JSON result

    IF status == "pass":
        results.append(result)
        Output: "▸ PASS {circuit} ({duration_ms}ms)"

    IF status == "fail":
        results.append(result)
        Output: "▸ FAIL {circuit}: {details}"
        # FAIL-STOP: do not run remaining circuits
        BREAK

    IF status == "skip":
        results.append(result)
        Output: "▸ SKIP {circuit}: {details}"
```

### Step 5: Report

```
passed = count(r for r in results if r.status == "pass")
failed = count(r for r in results if r.status == "fail")
skipped = count(r for r in results if r.status == "skip")
total = len(results)

IF failed > 0:
    Output:
    "═══ TEST CIRCUITS: FAIL ═══════════════════════
    {passed}/{total} passed, {failed} failed, {skipped} skipped
    Failed: {failed_circuit_names}
    Fix failing tests before committing.
    ═══════════════════════════════════════════════"
    RETURN FAIL

ELIF skipped > 0 AND passed > 0:
    Output:
    "═══ TEST CIRCUITS: PARTIAL ════════════════════
    {passed}/{total} passed, {skipped} skipped (infra unavailable)
    Skipped: {skipped_circuit_names}
    Safe to commit — skipped circuits are infra-gated.
    ═══════════════════════════════════════════════"
    RETURN PARTIAL

ELSE:
    Output:
    "═══ TEST CIRCUITS: PASS ═══════════════════════
    {passed}/{total} passed
    ═══════════════════════════════════════════════"
    RETURN PASS
```

## `check [RepoName]` — Dry Run

Same as Steps 1-3 of `post-change`, but skip Step 4 (execution).
Output the circuit list with infra gate status. Do not execute any tests.

## `circuit <circuit-name>` — Run Specific Circuit

```
Bash: bash "$WORLD_DIR/scripts/test-circuit-run.sh" <circuit-name> [repo-name-if-needed]
Output result.
```

For mutation circuits (character-lifecycle, environment-lifecycle): warn before executing.
```
Bash: echo "WARNING: {circuit} creates real data. Proceeding..."
```

## Error Handling

| Error | Response |
|-------|----------|
| Repo not found | FAIL with "repo not found: {path}" |
| Script not executable | `chmod +x` and retry |
| Test timeout (>5 min) | FAIL with timeout details |
| Unknown circuit name | SKIP with "unknown circuit" |

## Chaining

- **Called by**: `world/conventions/post-execution.md` Step 1.5, goal execution with `skill: run-test-circuit`
- **Calls**: `world/scripts/test-circuit-run.sh`, `world/scripts/test-circuit-detect.sh`, `infra-health.sh`
- **Triggers**: "run test circuit", "test my changes", "run tests", "verify code changes"

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The `test-circuit-run.sh` invocation is the terminal tool call. If a follow-up
Unblock goal is created on failure, `aspirations-add-goal.sh` is the terminal call.
Never end with a text PASS/FAIL summary.
