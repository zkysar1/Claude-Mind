---
name: state-replay
forged: true
forged_by: user
forged_date: "2026-04-04"
description: "Replays and analyzes a recorded NPC game session from EFS data: loads the session archive, reconstructs world state at each tick, lets the agent navigate the timeline, and surfaces NPC behavior for analysis. Use whenever the agent needs to diagnose why a specific NPC behaved a certain way, validate a behavior-tree change against real sessions, audit a reported session incident, or support /analyze-npc-behavior's scoring phase. Accepts session IDs and optional step/timestamp filters."
user-invocable: false
minimum_mode: assistant
tools_used: [Bash]
triggers: [state-replay, cell-archive, replay-session, efs-session-analysis, world-state-replay, step-timestamp-replay]
conventions: []
companion_scripts: []
revision_id: "skill-bootstrap-state-replay-e90e21"
previous_revision_id: null
---

# State Replay

Replay NPC game sessions step-by-step. Load session data from EFS, reconstruct world state at each tick, navigate the timeline, and analyze NPC behavior.

## Overview

The State Replay tool is a standalone Python CLI at `C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/`. It reads the same EFS data that the Processor reads (ConflatedState, CellExecutionLog, Intent, BTEvents, etc.) and constructs a navigable timeline of world state — every NPC's position, active behavior tree, current cell, and intent decision at every point in time.

**When to use:**
- After writing NPC behavior code — verify the effects in the next game session
- After running the Processor — check if NPC aspirations and BTs produce the expected behavior
- After a game session — analyze what happened (stuck NPCs, movement patterns, cell success rates)
- When debugging NPC issues — step through the timeline to find where behavior diverged from expectations

## Prerequisites

**Clone if absent (rb-4384):** the repo is NOT guaranteed present on every
box — if `C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/` is missing, clone it
first: `git clone https://github.com/zkysar1/Ayoai-State-Replay.git` into
`C:/ZakNoCloud/GitHub/Ayoai/` (WSL `/mnt/c/...`). An uncloned tool masquerades
as a data gate — provision it, don't conclude the lane is gated (rb-4384).

The tool needs a `.env.local` with `EFS_SSH_HOST`, `EFS_SSH_USER`,
`EFS_SSH_KEY_PATH`, **and** `REMOTE_DATA_PATH` (cli.py returns "No environments
found" without the last one). Verify:
```bash
Bash: cat "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/.env.local" | grep -cE "EFS_SSH|REMOTE_DATA_PATH"
```
Should return 4 (EFS_SSH_HOST/USER/KEY_PATH + REMOTE_DATA_PATH). If missing,
copy from the Processor `.env.local`, OR — if the Processor's is absent (as on
Foxtrot's box, 2026-07-20) — from the **mind repo's** `.env.local`
(`PROJECT_ROOT/.env.local`), which the EFS scripts already source:
```bash
Bash: grep -E "^(EFS_SSH|REMOTE_DATA_PATH)" "$PROJECT_ROOT/.env.local" > "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/.env.local"
```
Then confirm: `python3 cli.py list-sessions` should print the environment table.

## EFS Path Hierarchy

All NPC session data lives on the EFS-mounted EC2 instance:

```
{REMOTE_DATA_PATH}/Accounts/{account-uuid}/{environment-key}/{session-key}/
```

**Testing environment:**
- Account: `b1fb6520-c051-70df-ca6e-6ce15efd8d47`
- Environment: `NPCDemoExperiment`
- Full path: `/home/ec2-user/AyoAi-Efs/mnt/AyoAi/Accounts/b1fb6520-c051-70df-ca6e-6ce15efd8d47/NPCDemoExperiment/`

**Session key format:** `{unix-timestamp-millis}_{sequence-id}` (e.g., `1773735875903_268`). Directories starting with digits are sessions; `memory/` at the environment level is the Processor's deployed output.

**Inside a session directory:**
```
{session-key}/
  CharacterDefinitions.jsonl     # NPC definitions (ayoKey, intelligenceModule)
  Tasks.jsonl                    # Task vocabulary (moveTo, jump, speak, etc.)
  AyoServerEnvironment_OnStartup.json
  Character_*_OnTermination.json # Per-NPC state dump at shutdown
  memory/
    ConflatedState/              # 3Hz state snapshots per NPC (the timeline clock)
    CellExecutionLog/            # Intent-driven behavioral episodes
    Intent/                      # IAUS decision logs
    BTEvents/                    # Behavior tree execution events
    BehaviorTrees/               # BT definitions
    UnitStateChanges/            # Property-level state changes
    SpatialMemory/               # Per-NPC spatial grid
    PartialCells/                # In-progress cells
    PrivateNotes/                # NPC notes/relationships
    StepTimestamp/               # Step timing data
```

## Commands

All commands run from the replay repo directory. Use `python3 cli.py` (not `ayoai-replay`).

**REPLAY_DIR:** `C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay`

### list-sessions — List available sessions on EFS

```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py list-sessions --env NPCDemoExperiment
```

Returns session keys sorted newest-first. Pick the most recent for current behavior, or a specific session that ran after a code deployment.

### download — Download a session locally

```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py download --env NPCDemoExperiment --session {SESSION_KEY} --dest "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/sessions"
```

Downloads the full session directory from EFS to `C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/sessions/{SESSION_KEY}/`. `--dest` MUST be absolute: a relative `./sessions` resolves against CWD, and any caller that runs this without the `cd` prefix (or in a separate shell) silently drops a multi-MB `sessions/` tree at the wrong root (2026-05-15 repo-root cruft incident). Subsequent commands operate on the local copy.

**Shortcut — download the latest session:**
```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && LATEST=$(python3 cli.py list-sessions --env NPCDemoExperiment 2>/dev/null | grep -oP '\d+_\d+' | head -1) && python3 cli.py download --env NPCDemoExperiment --session "$LATEST" --dest "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/sessions" && echo "Downloaded: $LATEST"
```

### summary — Session overview

```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py summary ./sessions/{SESSION_KEY}
```

Prints: frame count, duration, NPC count, event count, per-NPC cell success rates. Use this first to understand the session scope.

### query — Headless state inspection

```bash
# Text output at a specific frame
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py query ./sessions/{SESSION_KEY} --frame 0

# JSON output for a specific NPC
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py query ./sessions/{SESSION_KEY} --frame 500 --npc jose --output json
```

Shows NPC positions, active cells, active intents, and events at the specified frame. Use `--output json` for machine-readable output.

**Useful frame indices:**
- `--frame 0` — session start (verify all NPCs have initial state)
- `--frame N` (middle) — mid-session behavior
- Calculate from summary: if 2940 frames over 16 minutes, frame 1470 is the midpoint

### analyze — Run analysis modules

```bash
# Detect stuck NPCs (position unchanged for 15+ frames during active cell)
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py analyze ./sessions/{SESSION_KEY} --analysis stuck_detector

# Movement metrics (distance, speed, unique positions per NPC)
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py analyze ./sessions/{SESSION_KEY} --analysis movement

# JSON output for programmatic consumption
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py analyze ./sessions/{SESSION_KEY} --analysis stuck_detector --output json
```

### replay — Interactive TUI (user-only)

```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py replay ./sessions/{SESSION_KEY}
```

Launches the Rich terminal UI with VCR controls. **This is for interactive user sessions, not agent use.** Agents should use `query` and `analyze` instead.

## Agent Workflow: Verifying NPC Code Changes

When you've changed NPC behavior code (e.g., StuckDetectionService, BT generation, cell evaluation) and want to verify the effects:

**Step 1 — Find the right session:**
```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py list-sessions --env NPCDemoExperiment
```
Pick a session that ran AFTER your code was deployed. Session keys embed timestamps — higher = newer.

**Step 2 — Download:**
```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py download --env NPCDemoExperiment --session {KEY} --dest "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay/sessions"
```

**Step 3 — Get overview:**
```bash
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py summary ./sessions/{KEY}
```
Check: Are all expected NPCs present? Is the session long enough? What are cell success rates?

**Step 4 — Targeted inspection:**
```bash
# Check initial state
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py query ./sessions/{KEY} --frame 0 --output json

# Check mid-session behavior
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py query ./sessions/{KEY} --frame 500 --output json
```

**Step 5 — Analyze patterns:**
```bash
# Were any NPCs stuck?
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py analyze ./sessions/{KEY} --analysis stuck_detector --output json

# Movement coverage
Bash: cd "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-State-Replay" && python3 cli.py analyze ./sessions/{KEY} --analysis movement --output json
```

**Step 6 — Conclusions:**
Compare metrics against baselines (from knowledge tree `behavioral-baselines` node). Did your code change improve or regress behavior?

## Library API (Processor Integration)

The replay package is importable as a Python library:
```python
from replay.data.pipeline import SessionDataPipeline
from replay.timeline.builder import TimelineBuilder
from replay.engine.queries import npc_positions, cell_success_rate, movement_trace, total_distance

# Load and build timeline
data = SessionDataPipeline(Path("./sessions/{KEY}")).load()
timeline = TimelineBuilder(data).build()

# Query at any frame
frame = timeline.frame_at_index(500)
positions = npc_positions(frame)
rates = cell_success_rate(timeline)
trace = movement_trace(timeline, "jose")
```

## Error Handling

| Symptom | Cause | Fix |
|---------|-------|-----|
| "EFS credentials not configured" | Missing `.env.local` | Copy from Processor repo |
| SSH timeout | EC2 instance not running | Start via `/access-aws-services` or Operator API |
| "No sessions found" | Wrong environment key | Verify with `list-sessions`; check `NPCDemoExperiment` spelling |
| "No ConflatedState data" | Session too short or crashed | Try a different session; check TerminationNotes.json |
| Empty NPC states at frame 0 | ConflatedState starts after a delay | Try frame 10+ instead |

## Chaining

- **Called by:** `aspirations-execute` (post-execution verification for NPC-related goals)
- **Composes with:** `run-game-session` (game session produces the data state-replay analyzes), `run-processor` (verify processor output against actual NPC behavior)
- **Reads:** EFS session data (via SSH/SCP), local downloaded sessions
- **Does NOT modify:** Any state files, knowledge tree, or aspirations

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The last `cli.py` invocation or `scp` download is the terminal tool call. Never end
with a text summary of the replay findings.
