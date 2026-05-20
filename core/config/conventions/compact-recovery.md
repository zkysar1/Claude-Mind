# Compact Recovery Convention

Context compaction (autocompact) is normal and expected during long sessions. This convention
defines the full-fidelity recovery protocol for Phase -0.5c of the aspirations loop.

---

## Recovery Chain

```
PreCompact hook → precompact-checkpoint.py saves ALL WM slots to compact-checkpoint.yaml
Stop hook → blocks exit, returns "invoke /aspirations loop"
SessionStart(compact) → postcompact-restore.py injects full state summary into fresh context
Phase -0.5c → compact-restore-slots.sh restores all WM slots from checkpoint
Phase -0.5d → re-reads self.md and program.md (identity context)
Phase -0.5e → resumes blocked-sleep timer if interrupted
```

---

## Runner Liveness (pure mtime)

Liveness is a single file's modification time: `agents/<agent>/session/runner-heartbeat`.
No content comparison, no identity gate, no cross-file match. If the mtime
is within `runner_heartbeat.stale_minutes`, the runner is alive.

### Writers

All three callers invoke `core/scripts/heartbeat-tick.sh`, which calls
`touch runner-heartbeat`:

| Caller | Cadence |
|---|---|
| `/start` IDLE Step 3 and Phase C8 | Once at seed, before `state=RUNNING` |
| aspirations/SKILL.md Phase -0.5 | Once per loop iteration |
| `core/scripts/interruptible-sleep.sh` | Every 60s inside the wait loop |

The interruptible-sleep tick is what makes pure mtime work during B7 waits:
the 1800s cap sleep would otherwise let mtime cross the 30-minute stale
threshold and trip recovery-gate. Ticking every 60s keeps mtime fresh.

### Freshness probe

`core/scripts/heartbeat-stale.sh` emits one of:

- `fresh` — mtime within threshold.
- `stale` — mtime older than threshold OR file missing. Runner presumed
  crashed; recovery-gate.sh Condition 2 fires.

### Why this survives autocompact

Autocompact rotates Claude Code's session-id and reruns the SessionStart
hooks. `session-save-id.sh` rewrites `running-session-id` to the new SID.
`heartbeat-tick.sh` is called from the next Phase -0.5 or (during a wait)
from interruptible-sleep's 60s tick — advancing the mtime within the
threshold. No content match is required, so there is no way for the new
SID write to be "wrong" at the heartbeat layer.

### Incident reference

2026-04-21 dual alpha+bravo RUNNING→IDLE demote after ~8h of
autonomous operation. Both agents survived many prior autocompacts
because Tier-A background jobs (long-running sessions, batch processor
runs, inference servers) held `recovery-gate.sh` Condition 4 in the suppressing
state. When the agents first entered B7 all-blocked waits and their
background jobs drained, Condition 4 flipped to permitting and a latent
heartbeat-content desync (produced by every post-compact SID rewrite)
became fatal — `heartbeat-stale.sh` returned `orphaned`, recovery-gate
treated it as stale, and live runners got demoted on the next SessionStart.

The fix has two parts that together remove the failure mode:

1. `interruptible-sleep.sh` ticks the heartbeat every 60s during waits,
   so mtime never ages past the threshold.
2. The heartbeat probe was reduced to pure mtime, removing the
   content-comparison path that produced the latent `orphaned` verdict
   in the first place.

An earlier iteration added a `runner-token` UUID as a session-id-independent
content comparison. It worked but was strictly more moving parts than pure
mtime + the 60s tick, and was removed 2026-04-21 after verification that
pure mtime handles every real scenario.

---

## Recovery-gate source matching

`recovery-gate.sh` is registered on the default SessionStart hook (no
matcher), so it fires on every SessionStart event — including `compact`,
`resume`, `startup`, and `clear`. But only `startup` and `clear` are
FRESH sessions in the crash-detection sense. `compact` and `resume`
are CONTINUATIONS of an existing session whose runner was not crashed;
it was paused by the platform (autocompact) or by the user
(`claude --resume`).

### Behavior

At the top of `recovery-gate.sh`, before the per-agent iteration — the
script reads the SessionStart payload from stdin (JSON, fields include
`source`) and branches on source:

| Source | Action |
|---|---|
| `compact` | Log `[recovery-gate] source=compact -- continuation, skipping gate`, exit 0 |
| `resume`  | Log `[recovery-gate] source=resume -- continuation, skipping gate`, exit 0 |
| `startup` | Proceed to per-agent 4-condition AND gate |
| `clear`   | Proceed to per-agent 4-condition AND gate |
| empty / missing / parse error / tty stdin / python unavailable | SOURCE is empty string → case falls through → gate runs (fail-open default) |

### Why this is not a 5th condition

The "WHY NO PID-LIVENESS CHECK" block in `recovery-gate.sh` explicitly
rejects a 5th liveness condition because a fallible probe would
systematically mis-report DEAD and auto-recover healthy sessions. The
source gate is categorically different:

- It is a **pre-filter on intent**, not a liveness probe. The 4-condition
  AND gate remains the sole crash-detection logic.
- It fails open (empty stdin / parse error → run the full gate), so any
  failure mode is strictly more permissive than today, never less.
- It does not touch the liveness signal (heartbeat mtime) — the gate's
  canonical signal remains untouched.

### Residual case (rb-432)

A conceptual signal-level ambiguity remains: a `startup`-sourced session
(user pastes a prior session's summary into a fresh `claude`) whose
heartbeat has legitimately aged past the 30-min stale threshold is still
indistinguishable from a crashed runner. The source gate does NOT close
this — by design, because a fresh `claude` genuinely IS a new terminal
and the gate's crash-detection is appropriate. The 60s `interruptible-sleep`
tick makes this window very hard to hit during active operation. If it
ever does bite, `/start <agent> --recover` remains the canonical resume
path. See `world/reasoning-bank.jsonl` rb-432 for the full trace.

---

## Phase -0.5c Protocol

When `agents/<agent>/session/compact-checkpoint.yaml` EXISTS:

### Step 1: Full Slot Restoration
```bash
Bash: compact-restore-slots.sh
```

This script:
- Reads `all_slots` from the checkpoint (ALL WM slots, including dynamic ones)
- Restores each non-null slot to working memory with merge logic:
  - **Array slots**: extend (don't overwrite) — prevents losing items added after checkpoint
  - **Map slots**: merge keys (checkpoint values take precedence for non-null keys)
  - **Scalar slots**: direct overwrite with checkpoint value
- Restores `slot_meta` timestamps for age tracking accuracy
- Restores top-level WM keys: `goals_completed_this_session`, `aspiration_touched_last`, `last_goal_category`
- **Skip list**: `archived_context` (stale by definition after compaction)
- Outputs a summary of what was restored

### Step 2: Encoding Queue Processing
Process the encoding queue with budget `min(5, queue_length)`:
- This is a lightweight mid-session encoding pass, not full consolidation
- Violations and high-surprise items get priority
- Items not processed remain in the queue for session-end consolidation

### Step 3: Cleanup
Delete `compact-checkpoint.yaml` (one-shot consumption).

---

## What Gets Checkpointed

The precompact hook (`precompact-checkpoint.py`) saves:

| Field | Source | Purpose |
|-------|--------|---------|
| `all_slots` | Full `slots` dict from WM | ALL slot types including dynamic ones |
| `slot_meta` | WM `slot_meta` dict | Slot age/activity tracking |
| `encoding_queue` | Top-level WM key | Items pending tree encoding |
| `prior_encoding_items` | Accumulated across compactions | Multi-compaction item preservation |
| `goals_completed_this_session` | Top-level WM key | Session progress tracking |
| `aspiration_touched_last` | Top-level WM key | Loop continuity |
| `last_goal_category` | Top-level WM key | Context coherence scoring |
| `retrieval_manifest` | Extracted from `active_context` | Phase 4.26 utilization feedback |
| `blocked_sleep_until` | Slot value | Sleep timer recovery |
| `pending_agents_count` | From `pending-agents.yaml` | Background agent awareness |

Legacy keys (`active_context`, `micro_hypotheses`, `knowledge_debt`, `known_blockers`) are
also included for backward compatibility with older Phase -0.5c implementations.

---

## What Gets Injected into Context

The postcompact restore (`postcompact-restore.py`) prints to stdout:
- **Full active context summary** (no truncation)
- **Loop state** counters (goals_completed, productive_goals, evolutions, etc.)
- **Goals completed this session** (list)
- **Encoding queue** (up to 10 items with scores and targets)
- **All unresolved blockers** (full details)
- **Additional slot state** (strategy, hypothesis, conclusions, sensory buffer, episode chain, domain data)
- **Retrieval manifest** (nodes loaded, deliberation state, utilization feedback status)
- **Execution diary** (last 10 entries — decision points, failures, findings)
- **Reasoning snapshot** (pre-compaction synthesis if available)
- **Pending agents**, **blocked-sleep** warnings
- **Identity reminder** + **action directive**

---

## Execution Diary Integration

The execution diary (`agents/<agent>/session/execution-diary.jsonl`) is an append-only breadcrumb
trail that survives compaction (it's on disk, not in context). The postcompact restore reads
the last 10 entries and includes them in the injected message.

Entry types: `decision`, `failure`, `finding`, `approach_change`, `observation`, `state_update`

Script: `execution-diary.sh append|read|summary|trim`

Call sites (where the framework feeds the diary):
- `aspirations-verify` — after each Q1/Q2/Q3 outcome and after standard-check pass/fail
- `aspirations-execute` — at episode-chain retries, recovery successes, provisioning
  attempts, and contradiction detections

---

## Iteration Checkpoint `phase_progress` Field (Sub-Phase Resume Protocol)

`agents/<agent>/session/iteration-checkpoint.json` has an optional `phase_progress` dict that
records sub-phase completion granularity. When verification is interrupted mid-phase
(autocompact, graceful stop), the Graceful Stop Handler reads `phase_progress` and passes
it to the re-invoked verify as `prior_checks`, letting verify skip checks that already passed.

### Shape

```json
"phase_progress": {
  "q1_passed": true,
  "q1_artifact": "path/to/artifact",
  "q2_passed": true,
  "q2_failure_mode_checked": "short description",
  "q3_scope": "unit" | "integration",
  "standard_checks_passed": "<N>/<total>"
}
```

All keys are optional. Older readers that ignore `phase_progress` still function.

### Writer

`aspirations-verify` writes each key in-place via `jq` as the corresponding check passes:
- After Q1 EVIDENCE passes: `q1_passed` + `q1_artifact`
- After Q2 NEGATIVE CHECK passes: `q2_passed` + `q2_failure_mode_checked`
- After Q3 INTEGRATION SCOPE assessed: `q3_scope`
- After standard checks evaluated: `standard_checks_passed`

### Reader

Phase -1.4 Graceful Stop Handler (`.claude/skills/aspirations/SKILL.md`) reads
`checkpoint.phase_progress` and threads it into the re-invoked verify:

```
prior_checks = checkpoint.phase_progress or {}
Skill(aspirations-verify) with: goal, result, checkpoint.source, prior_checks
```

### Invariant

The iteration checkpoint is deleted at `LOOP_CONTINUE` when an iteration completes cleanly,
so `phase_progress` auto-cleans on any successful iteration. No separate reset is needed.

---

## Reasoning Snapshot Integration

When context enters the tight zone (as defined by
`core/scripts/context-budget-status.py` `classify_zone` — distance-to-autocompact,
not raw usage), the LLM proactively writes a synthesis to
`agents/<agent>/session/reasoning-snapshot.yaml`. Before writing, `Bash: bash
core/scripts/context-budget-banner.sh` and quote its output in the reasoning
so the snapshot's trigger is evidence-based. This captures the LLM's own
understanding of:
- Current goal and approach
- Tried-and-failed approaches
- Current theory and next step
- Key decisions this session
- Emerging patterns

Script: `reasoning-snapshot.sh write|read|clear`

The postcompact restore reads this file and includes it. This is higher fidelity than WM
slots alone because it's the LLM's synthesized understanding, not just structured data.

### Framework-Forced Write Sites

In addition to the LLM's proactive tight-zone trigger (see `core/scripts/context-budget-status.py`
`classify_zone` for the current threshold), the framework forces a snapshot write at
two known risk boundaries. Both use the same stdin-JSON protocol and set a `trigger` key
to distinguish forced writes from proactive ones:

- **Before Phase 5 verify** (`.claude/skills/aspirations/SKILL.md`, before `Skill(aspirations-verify)`):
  `trigger: "pre-verify-auto"`. Captures approach/theory/next-step so an autocompact mid-verify
  still has a fresh synthesis in the injected postcompact context.
- **Before graceful-stop re-invoke** (`.claude/skills/aspirations/SKILL.md` Phase -1.4, before
  the `Skill(aspirations-verify)` re-invoke): `trigger: "pre-stop-resume-auto"`. Captures the
  phase being re-entered and the `prior_checks` being respected.

These are additive to the proactive trigger, not a replacement.

### Vivid Episodic Observations (Texture Preservation)

Alongside the structured fields (approach, theory, decisions, emerging patterns),
also write any **vivid episodic observations** freely into the snapshot — the
qualitative texture of what you were working on that pure bullet-point summaries
strip out. These are high-value for user communication and not captured
elsewhere. Think: specific identifiers (unit IDs, cadences, filenames,
timestamps), your affective reaction ("felt like a zombie", "almost rhythmic",
"surprisingly fast"), and any communicative impulse ("I wanted to show the user
X"). One sentence per observation is fine; aim for 3–10 across a session.

The failure mode this addresses: after autocompact, the agent remembers the
bullet points ("I shipped movement.py") but loses the texture ("unit 2979 at
750ms cadence looked almost rhythmic; I wanted to flag it as a possible
zombie-loop variant"). Tree nodes and experience archives recover the *facts*
but not the *feel*, and the feel is often what the user wants when asking
"how's it going?" — so the snapshot is the right home for it, because the
postcompact restore surfaces it directly into the next session's context.

---

## Boot Whitelist

These files in `agents/<agent>/session/` MUST survive boot Phase -1.5 cleanup:
- `execution-diary.jsonl`
- `reasoning-snapshot.yaml`
- `execution-diary-session-*.jsonl` (archived diaries from prior sessions)
