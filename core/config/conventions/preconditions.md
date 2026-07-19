# Structured Goal Preconditions

Goals may declare dependencies that must be satisfied before the goal is
eligible for claim or execution. The framework supports two forms side by
side under `verification.preconditions`:

- **Natural-language strings** — free-text conditions evaluated by the LLM in
  aspirations-select Phase 2.2 (judgment calls, soft conditions).
- **Structured predicates** — typed objects evaluated mechanically by
  `core/scripts/predicate.py` in the goal selector's COLLECT filter (cheap,
  automatic, no LLM involvement).

Both forms may appear in the same list. All must pass (AND semantics) for the
goal to be eligible.

## Why structured

Free-text deferrals (`defer_reason: "waiting for X"`) are opaque — the selector
cannot evaluate them, so it keeps surfacing the goal until something else
times out. Structured predicates close that loop: the selector evaluates them
each pass, filters unsatisfied goals, and auto-clears the deferral when they
flip to satisfied.

## Predicate Types (v1)

Every predicate is a dict with `type`, optional `id` (for stable referencing
in defer-reason strings), optional `description`, plus type-specific fields.

### `file_exists_after`

At least `min_count` files matching `path` must have mtime ≥ the resolved
cutoff timestamp. Use for "a data pipeline must have produced output more
recent than a known reference point."

```yaml
- type: file_exists_after
  id: pc-fresh-output
  description: "Integration-test output must exist for the current build"
  path: "world/artifacts/widget-service/*.log"   # glob, relative to PROJECT_ROOT
  after_ref: "git:HEAD"                          # see after_ref grammar below
  min_count: 1                                   # optional, default 1
```

### `command_succeeds`

Runs an allowlisted bash script; passes on exit 0. Use as a generic escape
hatch for anything the other predicate types don't cover (service health
probes, custom state queries, readiness checks).

```yaml
- type: command_succeeds
  id: pc-pipeline-ready
  command: "bash core/scripts/pipeline-read.sh --counts --min 1"
  timeout_seconds: 30                 # default 30, capped at 120
  selector_skip: false                # default false; set true for expensive checks
```

**Safety allowlist**: `command` MUST start with one of:
- `bash core/scripts/` — framework scripts under PROJECT_ROOT
- `bash world/scripts/` — domain scripts under WORLD_DIR (auto-rewritten)

No predicate field is interpolated into the command string. If you need a
parameterised check, add a flag-driven script to `world/scripts/` and invoke
it by that fixed path.

`selector_skip: true` tells the selector filter to ignore this predicate
(still re-evaluated at the pre-claim re-check). Use when the command is slow
enough that evaluating it every selector pass would thrash.

### `goal_completed_after`

Another goal must have completed (or — for recurring goals — most recently
achieved) after the cutoff timestamp. Strictly stronger than `blocked_by`,
which only checks status, not recency.

```yaml
- type: goal_completed_after
  id: pc-upstream-fresh
  goal_id: "g-235-70"
  after_ref: "git:56f6a55"
```

Reads `completed_date` (non-recurring) first, falling back to `lastAchievedAt`
(recurring). Looks up the goal in the live world queue, the archive, and the
current agent's queue — in that order.

### `file_check`

Simple glob-matches-exist check without a timestamp cutoff. Use for "code
file is present" or "expected artifact exists" preconditions where recency
does not matter. Distinct from `file_exists_after`, which requires an
`after_ref` cutoff.

```yaml
- type: file_check
  id: pc-spatial-memory-present
  description: "SpatialMemoryMap code file exists"
  path: "src/main/java/AyoServer/Characters/modules/SpatialMemoryMap.java"
  condition: "exists"     # default; also supports "not_exists"
  min_count: 1            # default 1 (only meaningful when `path` is a glob)
```

Supports `exists` and `not_exists` conditions. Path resolution follows the
same `resolve_file_path` rules as `file_exists_after` (supports world/, meta/,
and absolute paths; glob wildcards preserved).

### `metric_threshold`

Runs an allowlisted bash script, extracts an integer from its output, and
compares it to `min` / `max` bounds. Use for "at least N replay candidates
exist" or "cache hit ratio exceeds X" preconditions where `command_succeeds`
is insufficient because the interesting signal is not an exit code but a
count.

```yaml
- type: metric_threshold
  id: pc-replay-candidates
  description: "At least 3 replay candidates exist"
  command: "bash core/scripts/pipeline-read.sh --replay-candidates"
  extract: "json_length"    # see extract modes below
  min: 3                    # optional; at least one of min/max must be set
  max: 100                  # optional upper bound
  timeout_seconds: 30       # default 30, capped at 120
```

**Extract modes:**
- `stdout_int` — parses first signed integer from stdout (tolerates trailing text)
- `json_length` — parses stdout as JSON; `len()` of array or dict
- `exit_code` — uses the script's exit code as the metric (for scripts that
  encode the count in exit status)

**Safety:** same allowlist as `command_succeeds` (`bash core/scripts/`,
`bash world/scripts/` prefix only). No field interpolation into the command.

### `vcs_commits_since`

Passes when a git repo has at least `min_count` commits committed **strictly
after** a cutoff timestamp. The principled event-gate for recurring review
goals: instead of a time-proxy interval, fire only when the lane repo received
new commits since the goal last ran (eliminates the manual interval-rebase
class — g-001-241 / g-001-243).

```yaml
- type: vcs_commits_since
  id: pc-new-framework-commits
  description: "Fire only when framework code changed since this goal last ran"
  repo: "."                              # optional; default PROJECT_ROOT. Absolute or PROJECT_ROOT-relative.
  since_goal_last_achieved: "g-001-12"   # cutoff = this goal's lastAchievedAt (event-gate form)
  # --- OR, instead of since_goal_last_achieved: ---
  # after_ref: "git:HEAD"                # cutoff via the after_ref grammar (git:/iso:/file:)
  paths: ["core/scripts", "core/config", ".claude"]   # optional git pathspec — scope to code, exclude state churn
  author: "Some Author"                  # optional git --author filter
  grep: "Co-Authored-By: alpha"          # optional git --grep (commit-message) filter
  min_count: 1                           # optional, default 1
```

**Cutoff source** (exactly one required):
- `since_goal_last_achieved: <goal_id>` — resolves to that goal's
  `lastAchievedAt` (recurring) or `completed_date`. A recurring review goal
  wires its OWN id here so it self-gates on new commits. Looked up across the
  world live queue, the archive, and the bound agent's queue.
- `after_ref: <git:|iso:|file:>` — the shared after_ref grammar (see below).

**Strict comparison, no grace window.** Unlike `file_exists_after`, this
predicate uses a strict `commit_date > cutoff` with NO clock-skew grace. The
commit date and `lastAchievedAt` come from the same local clock, and a grace
window would re-count the triggering commit on the next pass — re-creating the
streak-contraction artifact the predicate exists to eliminate.

**`paths`** is a git pathspec (str or list). Use it to scope the count to code
files and exclude per-iteration agent-state commits (`agents/**`) that would
otherwise keep the gate permanently open in PROJECT_ROOT.

**Safety:** no field is interpolated into a shell — `git log` runs with an argv
list (`shell=False`), so `repo`/`paths`/`author`/`grep` cannot inject. Non-git
`repo`, git errors, unresolvable cutoffs, and missing cutoff source all fail
closed (predicate fails, never crashes).

### `pr_merged`

Passes when a GitHub PR is **MERGED**. The branch-per-goal estate gate
(g-115-2593 / rb-3995): in estates where each Apply goal ships on its own
branch, a foundation goal is marked `completed` when its code lands on its OWN
branch — but its PR can stay open, so the substrate is off-main and plain
`blocked_by`-on-completion falsely reads the dependency as satisfied. Stacked
goals carry this predicate (WITHOUT `selector_skip`) so the selector keeps them
out of candidates until the foundation PR actually merges; the pre-claim
re-check and `precondition-defer-recheck` (Phase 0.5b.3) resurface them
automatically once it does. Scaling note: per-TTL probe cost grows with the
number of DISTINCT concurrently-OPEN gated PRs (one 15s-timeout `gh` probe per
PR per TTL expiry — many goals gating on ONE PR share one cache entry). If an
estate accumulates many distinct open-PR gates, set `selector_skip: true` on
the long-tail entries (they then gate at pre-claim re-check / Phase 0.5b.3
instead) and keep selector-time gating for the near-frontier ones.

```yaml
- type: pr_merged
  id: pr89-merged
  repo: "owner/name"        # GitHub slug — the REMOTE owner/name, NOT the local
                            # directory name (probe `git remote get-url origin`
                            # when unsure; the two diverged in the first live use)
  pr: 89                    # positive int
  cache_ttl_minutes: 30     # optional; re-probe interval while the PR is OPEN
```

**States:** `MERGED` → pass (cached terminally — merges never un-happen).
`OPEN` → fail, re-probed after `cache_ttl_minutes`. `CLOSED`-unmerged → fail
with an abandoned-foundation warning (cached 24h, so a reopened PR recovers
within a day).

**Cache:** per-agent, at `agents/<agent>/session/pr-merge-state-cache.json`,
keyed `owner/name#pr`. Bounds `gh` network calls to ≤1 per PR per TTL per
agent even though the selector evaluates every iteration. No bound agent → no
cache (evaluates live).

**Failure posture:** probe errors (no `gh`, network, auth) trust a stale cache
entry when one exists (grace — the last observation beats flapping); with no
cache they fail closed per library convention, keeping the goal gated until a
`gh`-capable evaluation observes the merge.

## `after_ref` Grammar

Shared by `file_exists_after` and `goal_completed_after`:

| Form | Resolution |
|------|-----------|
| `git:<sha-or-ref>` | `git show -s --format=%cI <ref>` run in PROJECT_ROOT |
| `iso:<timestamp>` | Parsed as ISO 8601 (tz-aware → naive local) |
| `file:<path>` | mtime of the referenced file (absolute or relative to PROJECT_ROOT) |

A 60-second grace window is applied to all mtime/timestamp comparisons to
absorb small clock-skew between the cutoff source and the local filesystem.

## Evaluator Contract

`core/scripts/predicate.py` exposes:

```python
evaluate(predicate: dict) -> PredicateResult
evaluate_all(predicates: list, *, mode="fail_fast", include_skippable=True) -> list[PredicateResult]
```

`PredicateResult` fields: `passed`, `type`, `predicate_id`, `observed_value`,
`reason`, `evaluated_at`. Never raises — every error path returns a
PredicateResult with `passed=False` and a descriptive `reason`.

Unknown types, malformed predicates, and evaluator errors all fail closed
for the offending predicate only. The caller decides whether to block the
goal on the result.

## Fail-Open Behaviour

- **Malformed non-dict precondition entries** in a goal's list are logged and
  skipped (neither blocks the goal nor causes a crash).
- **Unknown predicate type** blocks the goal. Surface in
  `collect_blocked` diagnostics.
- **Misconfigured predicate that never passes** still expires via the existing
  `defer_reason_timeout_hours` fail-open (default 120h) in `goal-selector.py`.
  A goal can't be blocked forever.

## Deferral Flow

1. **Selector filter** — runs every pass. Filters goals with any unmet
   structured precondition out of the candidate pool. Does NOT write to
   JSONL (the goal re-enters naturally on the next pass if state flips).
   Tags `_precondition_unmet: [<ids>]` on the goal dict for diagnostics.

2. **Pre-claim re-check** — runs in `aspirations-execute` Phase 4 Preamble.
   Catches the selector→claim race. If any structured precondition fails
   now, the goal is deferred:

   ```
   defer_reason     = "precondition_unmet:<comma-separated ids>"
   defer_reason_set_at = "<now>"
   ```

   The 120h `defer_reason_timeout_hours` fail-open protects against
   misconfiguration.

3. **Auto-clear sweep** — runs in `aspirations-precheck` Phase 0.5b.3, once
   per loop iteration. For any goal whose `defer_reason` starts with
   `"precondition_unmet:"`, re-evaluate the goal's structured preconditions.
   If all now pass, null out `defer_reason` and `defer_reason_set_at`. The
   goal re-enters the candidate pool on the next selector pass.

## Contrast with Other Mechanisms

| Mechanism | What it expresses | Written when | Cleared when |
|-----------|------------------|--------------|--------------|
| **Structured precondition** (this doc) | "This *declared* dependency must hold before I can start" | Goal template / creation time | Auto on predicate flip |
| **Blocker** (CREATE_BLOCKER protocol) | "I was surprised by an infrastructure failure during execution" | Post-failure by the agent | Unblocking goal completes |
| **`blocked_by`** | "This goal depends on another goal reaching terminal status" | Goal creation | Prereq goal completes |
| **Free-text `defer_reason`** | "Some condition I can only describe in prose" | Manually or by LLM | Manually or 120h timeout |

The four exist side by side. Preconditions are the right tool when the
condition is declared up front AND machine-checkable. Blockers remain for
unexpected failures. `blocked_by` remains for simple goal-order dependencies.
Free-text `defer_reason` remains for LLM-judged soft conditions.

## Worked Example

Goal depends on a fresh run of a domain pipeline after a code change:

```yaml
- id: g-test-01
  title: "Verify widget-service behavior under the new retry logic"
  verification:
    preconditions:
      - id: pc-fresh-log
        type: file_exists_after
        path: "world/artifacts/widget-service/runs/*.log"
        after_ref: "git:HEAD"
        min_count: 1
      - id: pc-upstream-done
        type: goal_completed_after
        goal_id: "g-test-00"
        after_ref: "git:HEAD"
    outcomes:
      - "Retry logic exercised end-to-end in the test output"
```

The selector filters `g-test-01` until both predicates pass. When a new log
appears and `g-test-00` re-completes, the goal appears in the ranked pool
automatically.
