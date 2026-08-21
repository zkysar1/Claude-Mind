---
name: fresh-eyes-code
description: "Use whenever a specific artifact set needs adversarial bug-hunt review — on-demand fresh-eyes code review that mode-flips the reviewing agent into adversarial posture (not confirmation) and systematically probes error paths, edge cases, platform-specific behavior (Windows subprocess / CRLF), locking, race conditions, schema assumptions, and N-agent correctness for coordination code. Fires when guard-343 trips after a deep state-update with material core/ changes, when g-248-07 / g-248-08 run the shape-β cross-agent recurring review, or when the user invokes /fresh-eyes-code directly. Produces a findings report with a severity tag (invalidates|constrains|enables|informs) and publishes to the board. Distinct from /fresh-eyes-review (portfolio + self meta-review, every 25 goals) — this skill is code-level on a named artifact."
user-invocable: true
triggers:
  - "/fresh-eyes-code"
  - "fresh eyes code"
  - "code review bug hunt"
  - "adversarial code review"
tools_used: [Bash, Read, Grep, Glob, Skill]
companion_scripts: []
conventions: [board, experience, journal, reasoning-guardrails]
minimum_mode: assistant
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-fresh-eyes-code-553037"
previous_revision_id: null
---

# /fresh-eyes-code — On-Demand Adversarial Code Review

Review a specific set of files as if seeing them for the first time, with an
explicit mandate to **find bugs, not confirm correctness**. The author-at-write-time
perspective is the worst possible one for catching structural defects — this skill
schedules the mode-flip and produces a findings report regardless of outcome.

## Inputs

```
/fresh-eyes-code <path>[ <path>...]         — Review explicit paths
/fresh-eyes-code --goal <goal-id>            — Review files touched by goal's experience trace
/fresh-eyes-code --since <timestamp>         — Review files git-changed since <timestamp>
/fresh-eyes-code --since <timestamp> --author <agent>   — Only that agent's changes (β use-case)
```

All sub-commands produce the same review format and findings output.

## Step 0: Load Conventions

`Bash: load-conventions.sh board experience journal reasoning-guardrails`

Read only the paths returned. If output is empty, all conventions already loaded —
proceed to next step.

## Phase 1: Assemble Target Set

```
IF explicit paths provided:
    raw_list = list of paths (validate each exists via ls)
ELIF --goal <goal-id>:
    Bash: bash core/scripts/experience-read.sh --goal <goal-id>
    raw_list = file paths from experience.content_path + any referenced scripts/md
ELIF --since <timestamp> [--author <agent>]:
    # Canonical query — single source of truth. The script handles the
    # git-identity intersection via team-state.yaml recent_completions +
    # agents/<agent>/experience.jsonl when --author is passed. See
    # cross-agent-recent-changes.sh for semantics; do not re-describe the
    # intersection here.
    IF --author <agent>:
        Bash: bash core/scripts/cross-agent-recent-changes.sh --agent <agent> --since <timestamp>
    ELSE:
        Bash: bash core/scripts/cross-agent-recent-changes.sh --since <timestamp>
    raw_list = newline-separated stdout

# Bash-enforce the 20-file cap. Prose caps drift; a pipe does not. The cap
# preserves assembled order (recency for --since, experience order for --goal,
# argv order for explicit paths) because upstream producers already emit in
# that order — do NOT sort here.
target_files = printf '%s\n' "$raw_list" | sed '/^$/d' | head -20
skipped_count = (count of raw_list) - (count of target_files)
```

If target_files is empty, do NOT short-circuit. Fall through — Phase 4 emits a
single `informs` finding ("no target files under filter") and Phase 5 publishes
it. The board post IS the record that fresh-eyes ran; do not invent a separate
logging sink (`journal-add.sh` is a session-index, not a telemetry channel —
see Return Protocol below).

If `skipped_count > 0`, include the count (and the skipped remainder if
feasible) in the Phase 4 finding body so downstream consumers see the cap
fired. Do not raise the cap inline — if a review legitimately needs more than
20 files, invoke `/fresh-eyes-code` twice on disjoint subsets.

## Phase 2: Mode-Flip Prime

Explicitly prime the review context before reading any target file. This is the
STRUCTURAL purpose of the skill — without the mode-flip, the author-context
perspective persists and the review produces rubber-stamping instead of bugs.

```
Review posture for this invocation:
- This code is broken until proven otherwise.
- Every error path that was not explicitly tested is suspect.
- Platform-specific behavior (Windows CRLF, subprocess path mangling,
  file-locking, case-sensitivity) is a first-class concern, not an
  afterthought.
- Locking, race conditions, and schema assumptions require proof, not
  plausibility arguments.
- For coordination code: test N-agent correctness, not just the 2-agent
  author-intended case.
- A finding of "no bugs" is a POSITIVE result that must be explicitly
  asserted after exhaustive probing, not the default outcome.
```

Output the posture block at start-of-review so the reasoning trace preserves the
mode-flip (verifies it happened — see rb-384 for why this matters).

## Phase 3: Systematic Probe

For each target file, probe these dimensions in order. Most files will surface
0 findings for most dimensions — that's fine, the probe still ran.

### 3.1 Platform / Subprocess

- Does any subprocess call pass an absolute Windows path as a bash argument?
  (Tripwire: rb-350 family — Windows backslashes and `_paths.sh` CRLF mangling.)
- Does any Bash-invoked command rely on `\r\n` line endings being absent?
  (Tripwire: Git checkout translating LF→CRLF on Windows breaks shell scripts.)
- Is `sys.executable` used directly for child Python, or routed through `bash`?
  (`bash` adds a failure mode that direct `sys.executable` avoids.)
- When auditing python-invocation consistency in any file, grep for ALL
  `python3` occurrences — NOT just the `python3 -c` form. For each match,
  classify it as one of:
  - `-c` form  (e.g., `python3 -c "..."`)
  - script-path  (e.g., `python3 path/to/script.py`)
  - heredoc  (e.g., `python3 <<'PYEOF'`)
  - other  (env probes, version checks, shebangs)
  Report the full count + per-class breakdown in the finding. (Tripwire:
  g-115-478 fresh-eyes review F-004 named 6 bare `python3` callsites in
  `stop-hook.sh` when the actual count was 8 — lines 80 (`capture-insights.py`
  script-path call) and 218 (`PYEOF` heredoc) were missed because the
  reviewer's grep covered only the `-c` form. Partial coverage produces
  consistency goals that close at 75% with the remainder silently inconsistent.)

### 3.2 Concurrency / Locking

- Does any write path call a locked-append helper (`locked_append_jsonl`,
  `_fileops.with_lock`, etc.), or is it a bare `open(...,'a')` that can corrupt
  under concurrent writes?
- Does any read-then-write sequence hold a lock between read and write, or is
  there a TOCTOU race?
- Does any script read agent-local paths from env vars that can race with
  PreToolUse hook injection?

### 3.3 Error Paths

- Does every `subprocess.run(...)` check `returncode` or use `check=True`?
- Does every `json.loads` guard `JSONDecodeError`? Does every `yaml.safe_load`
  guard `YAMLError`?
- Does every "skipped (tool error)" branch actually propagate the error message
  in the reason field, or does it silently swallow the exception?
- Does fail-open policy mean "we don't block real work" or "we silently produce
  false positives"? (Different failure modes — the code should be explicit.)

### 3.4 Schema Assumptions

- Does the code read a JSONL/YAML field by name without verifying the field
  exists in the current schema? (Tripwire: rb-245 zero-count-gate — auditing a
  field that was renamed.)
- For positive file-state claims: does the code read the file in-turn before
  asserting its contents? (Tripwire: rb-365 positive-state-gate.)

### 3.5 N-Agent Correctness (Coordination Code Only)

Applies to files under `core/scripts/` that touch `team-state.yaml`, `board/`,
`insight-trigger`, `goal-duplication`, or any `agent_status.<name>` reference.

- Does the code filter by `completed_by != self_agent`, or does it enumerate a
  hardcoded "partner"? (Tripwire: recent `goal-duplication-gate.py` refactor —
  `_partner_of` was N-agent incorrect.)
- Does any git filter use `--author=<name>`? If yes, AND both agents share a
  git identity, the filter is a no-op.
- Does any check iterate `others[0]` or `others[0:1]` where an N-agent world
  would need iteration over all others?

### 3.6 Silent Failure Catalog

- Any command with `-sf`, `-q`, `2>/dev/null`, `--silent`, `--quiet`? (Tripwire:
  .claude/rules/verify-before-assuming.md Rule 4 — silent failures are ZERO
  signals, not one.)

### 3.7 Drift-Invited Patterns

- Does the code's behavior depend on LLM-discretionary step compliance, or is
  it script-enforced? (Tripwire: drift from rules/learning-philosophy.md —
  LLM-gated steps drift, bash-gated steps don't.)
- Does the code re-validate a claim that bash already enforced, or accept the
  claim at face value?

## Phase 4: Record Findings

For each bug found, produce a finding record. Group related bugs under the same
file heading.

```
finding_id: <agent>-fec-<short-title-slug>-<YYYYMMDDhhmm>
file: <path>
severity: invalidates | constrains | enables | informs
  - invalidates: the bug makes the file wrong for its stated purpose (cross-agent
    insight_trigger should route)
  - constrains: the bug narrows the file's correct use (partner should note)
  - enables: no bug, but a latent capability worth highlighting (informational)
  - informs: observation only, no action needed
summary: <1-line what is wrong>
evidence: <file:line or line range, plus exact symbol/pattern matched>
probe_dimension: platform | concurrency | error-paths | schema | n-agent | silent | drift
suggested_fix: <concrete action or null if "investigate first">
```

IF no bugs found after exhaustive probe — OR target_files was empty (Phase 1):
produce ONE finding with `severity: informs`, summary "No bugs surfaced under
7-dimension fresh-eyes probe ({probe list})" for the probed case, or
"fresh-eyes-code invoked with no target files under the given filter" for the
empty case. No evidence. Zero-finding and zero-target are BOTH positive signal
— a review that ran is not the same as a review that was skipped. Always emit
at least one finding so Phase 5's board-post has something to publish.

## Phase 5: Publish Findings

```
FOR EACH finding in findings:
    IF finding.severity in ["invalidates", "constrains"]:
        # Route via insight-trigger infrastructure (consumes via insight-trigger-gate.py).
        #
        # `requires_action_by:` is LOAD-BEARING, not decoration. insight-trigger-gate.py
        # `_collect_triggers` SKIPS any trigger whose requires_action_by is absent, by
        # documented policy ("if requires_action_by is absent, BOTH agents see it and
        # neither is required to act — skip to avoid duplicate work"). This skill omitted
        # the tag, so its own findings were unroutable by the consumer its own Phase 5
        # names. Measured 2026-08-07 (alpha, hostname cc-04, uname -r 6.8.0-136-generic)
        # over the live findings channel, 5282 records: 1139 fresh-eyes-code posts, 496
        # carrying invalidates|constrains, and only 15 carrying requires_action_by: —
        # so 481 actionable findings were dropped in silence. Counterfactual on a real
        # post (msg-20260807-195917-alpha-5270): identical tags + requires_action_by:
        # → routes.
        #
        # `affects:<file-path>` is NOT the blocker, contrary to what g-115-5265 was filed
        # claiming: the gate parses affects: with a generic prefix parser and extracted
        # the file path fine. (The sibling insight-trigger-sweep.py DOES require
        # affects:g-NNN-NN, but it drops these posts one clause earlier anyway — it needs
        # requires_action_by: AND action_type:, and neither is emitted here. See the
        # "Two consumers, two contracts" note below.)
        IF a reviewed agent is known (β path — `--author <agent>` was passed):
            route_tag = "requires_action_by:{reviewed_agent},"
        ELSE:
            # α (own post-state-update files) and γ (user-directed): the author IS the
            # actor and is already present. The gate skips self-triggers by design
            # (`author == self_agent` → continue), so an address here would route to
            # nobody. Deliberately unaddressed — this is the "separated, recorded" half.
            route_tag = ""
        echo "<body>" | bash core/scripts/board-post.sh --channel findings --type finding \
            --tags "insight_trigger,{route_tag}severity:{finding.severity},affects:{finding.file},fresh-eyes-code,{source_tag}"
    ELIF finding.severity == "enables":
        echo "<body>" | bash core/scripts/board-post.sh --channel findings --type finding \
            --tags "enables,fresh-eyes-code,{source_tag}"
    ELSE (informs, including the zero-finding case):
        echo "<body>" | bash core/scripts/board-post.sh --channel findings --type finding \
            --tags "informs,fresh-eyes-code,{source_tag}"
```

`source_tag` = `guard-343` (shape α), `g-248-07` or `g-248-08` (shape β),
`user-invocation` (shape γ). Lets downstream auditors filter by trigger source.

### Two consumers, two contracts — know which one you are writing for (g-115-5265)

`insight_trigger` posts are read by two scripts with **deliberately different**
predicates. A finding that satisfies one is not thereby routed by the other, and
neither reports what it declined to admit — so a post that reaches nobody looks
exactly like a post with nothing to route.

| | `insight-trigger-gate.py` | `insight-trigger-sweep.py` |
|---|---|---|
| role | immediate, narrow severity | periodic, broader severity, idempotent |
| requires | `insight_trigger` + `requires_action_by:` + severity ∈ {invalidates, constrains} | `requires_action_by:` **and** `action_type:` |
| skips self-triggers | yes (`author == self_agent`) | no |
| parses `affects:` as | any string (generic prefix parser) | `^affects:(g-\d+-\d+)$` only |

**This skill writes for the GATE.** It emits no `action_type:`, so the sweep will
never admit its posts — that is intended, not a second bug to fix: the sweep is for
explicitly-addressed agent-to-agent routing, and a code-review finding is not that.

Consequences worth carrying:

- The gate is the reason `requires_action_by:` is mandatory above. It is also why
  addressing a finding to yourself accomplishes nothing.
- An `affects:<file-path>` value survives the gate but has no goal to probe, so
  `_act_on_trigger` files the Investigate goal with an
  `affects target not found in any queue` warning. That is a **degraded**, not
  broken, path. Emitting `affects:<goal-id>` instead — when the finding is
  attributable to a specific goal — upgrades it to a real target-status re-probe
  and suppresses the warning. Prefer the goal-id form when a goal is known.
- Do not "fix" the gap by widening the sweep's `AFFECTS_RE` to accept file paths.
  That widens a matcher over a live corpus (guard-2201) whose fresh-eyes-code
  population is ~1,100 posts, and it would not route a single additional finding,
  because those posts fail the `action_type:` clause first. Measured, not assumed.

Pinned by `core/scripts/tests/test_fresh_eyes_code_trigger_contract.py`, which
feeds this section's documented tag shape through the gate's own
`_collect_triggers` and carries a positive control proving the pre-fix shape was
dropped.

## Phase 5b: Cross-Agent Coverage Tracking (g-115-291 / rb-593)

After all Phase 5 board-posts complete, write the reviewed file set + timestamp
to `world/team-state.yaml` so the partner agent's next `post-state-update-gate.sh`
cooldown can subset-suppress files this review just covered. Without this write,
each agent's gate sees only its own per-agent `fresh_eyes_last_fire` (WM) and
re-dispatches files the peer already reviewed within the cooldown window.

Rationale (option b' from g-115-288 / rb-593): per-agent WM cannot bridge
cross-agent. team-state is the canonical shared world state — same pattern
`recent_completions` and `agent_status.<agent>.in_flight` already use.

```
# SKIP if target_files is empty (Phase 1 fell through with no targets) — an
# empty record adds noise without shielding anything.
IF count(target_files) > 0:
    agent = ${MIND_AGENT}                    # bound by orchestrator
    ts    = $(date +%Y-%m-%dT%H:%M:%S)        # local ISO 8601, no UTC
    files = JSON-encoded target_files (Phase 1 list, already capped at 20)
    count = len(target_files)

    # g-115-573: compute content_signatures (sha1[:12] per file) so
    # post-state-update-gate.sh cooldown can detect amend-after-review.
    # Path-match alone false-suppresses when a later commit covers the
    # same paths with different content. Pre-573 records (no
    # content_signatures) fall through to path-only check in the reader.
    Bash: sigs_json=$(py -3 core/scripts/_fresh_eyes_signatures.py --files-json "${files}")

    Bash: bash core/scripts/team-state-update.sh \
            --field "agent_status.${agent}.last_fresh_eyes_run" \
            --value "{\"files\": ${files}, \"time\": \"${ts}\", \"count\": ${count}, \"content_signatures\": ${sigs_json}}" \
            --operation set
```

**Fail-open**: a team-state write failure does NOT block the review record.
Phase 5 board posts are the durable record of fresh-eyes invocations
(retrievable via `--tag fresh-eyes-code`); `last_fresh_eyes_run` is the
cooldown signal, not the audit trail. A failure of
`_fresh_eyes_signatures.py` (e.g., file unreadable) yields an empty
`content_signatures` dict — the reader falls back to path-only coverage
for those paths (backward-compat).

**Schema** (documented in `core/config/conventions/coordination.md` →
"`last_fresh_eyes_run` Field — Cross-Agent Coverage Window"):

```
agent_status.<agent-name>.last_fresh_eyes_run:
  files: ["path/relative/to/repo", ...]   # unique reviewed paths, capped at 20
  time: "ISO 8601 local timestamp"
  count: N                                 # len(files)
  content_signatures:                      # g-115-573 amend-detection
    "path/relative/to/repo": "<sha1[:12]>" # content hash at review time
    ...                                    # missing path = no sig (path-only fallback)
```

**Consumer**: `core/scripts/post-state-update-gate.sh` cooldown block. Reads
`agent_status.*.last_fresh_eyes_run` for ALL non-self agents within
`COOLDOWN_HOURS`. For each candidate path P in the current change set:
sig-bearing records with matching hash → covered; sig-bearing records with
mismatching hash → NOT covered (amend detected, ignore path-only fallback);
records without `content_signatures` (or path P missing from the dict) →
path-only coverage (legacy / backward-compat). Suppress dispatch only when
all candidate paths are covered by at least one source.

## Phase 6: Recurring-Goal Close (Shape β Only — Terminal)

If called from g-248-07 or g-248-08:

```
summary = "reviewed N files, {F} findings ({inv} invalidates, {con} constrains, {ena} enables, {inf} informs)"
Bash: bash core/scripts/recurring-close.sh {goal-id} {routine|deep} --summary "<summary>"
# deep if any finding.severity in [invalidates, constrains]; routine otherwise
```

This Bash call IS the terminal action for the β path. Do not append any text
after it — that would violate return-protocol.md.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not
text. Terminal action depends on invocation source:

- **β path** (g-248-07 / g-248-08): Phase 6's `recurring-close.sh` call.
- **α path** (guard-343 post-state-update) and **γ path** (user `/fresh-eyes-code`):
  Phase 5b's `team-state-update.sh` call (cross-agent coverage tracking, runs
  unconditionally after Phase 5 when `count(target_files) > 0`). When
  `count(target_files) == 0` Phase 5b is skipped and the LAST `board-post.sh`
  from Phase 5's loop is the terminal — Phase 4 guarantees at least one finding
  (zero-bug case → `informs`; empty-target case → `informs`), so Phase 5 always
  fires at least one board-post.

**Critical — do not re-add a Phase 7 `journal-add.sh` call.** `journal-add.sh`
is a session-index writer expecting `{"session":N, "date":"...", "journal_file":"..."}`,
NOT a free-form telemetry sink. Two sibling skills (aspirations-spark, aspirations-select)
have explicit comments warning about this. The board posts from Phase 5 ARE the
durable record of fresh-eyes invocations; querying the board by `fresh-eyes-code`
tag retrieves them cross-agent. Single source of truth.

## Chaining

- **Called by**: User (direct `/fresh-eyes-code <path>`); guard-343 action_hint
  (post-state-update fresh-eyes on deep+large goals — shape α); g-248-07 / g-248-08
  recurring cross-agent reviews (shape β, 48h cadence).
- **Calls**: `board-post.sh` (publish findings — terminal for α/γ paths),
  `recurring-close.sh` (β path — terminal), `experience-read.sh` (--goal mode),
  `team-state-read.sh` (--author mode), `load-conventions.sh`.
- **Reads**: target files (via Read tool), experience traces (--goal mode),
  team-state recent_completions (--author mode), git history via
  `cross-agent-recent-changes.sh --since <iso>` (that is the WRAPPER's own CLI
  flag; the wrapper filters on committer timestamp internally and does NOT pass
  `git log --since`, which is a traversal cutoff — g-115-6959 / guard-4539).
- **Modifies**: board findings channel (append), journal (append), recurring
  goal state (β path only). Does NOT modify the reviewed files — findings are
  advisory; fixes happen via follow-up goals.

## Relationship to Sibling Mechanisms

| Mechanism | Scope | Trigger | Level |
|-----------|-------|---------|-------|
| `/fresh-eyes-review` | Self + portfolio meta-review | Cadence (25 goals) or user pull | Portfolio |
| `/fresh-eyes-code` (this) | **Code-level on named artifacts** | **α guardrail, β recurring, γ user** | **Code** |
| `guard-343` | Triggers /fresh-eyes-code on deep+large goals | Post-state-update | — |
| g-248-07/08 | Cross-agent recurring /fresh-eyes-code | 48h cadence | — |
| `/simplify` | Quality/reuse review | User pull | Code |

`/fresh-eyes-code` is specifically for the bug-hunt mode-flip. `/simplify` is for
readability/reuse. `/fresh-eyes-review` is for portfolio-scale "are we working on
the right things" — different question entirely.
