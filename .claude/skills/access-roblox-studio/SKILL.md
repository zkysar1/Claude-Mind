---
name: access-roblox-studio
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "Controls Roblox Studio programmatically via the AyoBridge plugin (localhost:28080): starts game sessions, queries the live DataModel instance tree, creates or modifies Script/LocalScript/ModuleScript instances, and streams Studio console output. Use whenever the agent needs to launch a Roblox playtest, inspect workspace or player state at runtime, push Lua scripts into an active Studio session, or verify in-game behavior. Requires Studio running with AyoBridge plugin connected."
user-invocable: false
triggers: [roblox, studio, roblox-studio, lua, bridge-server, 28080, place-file, roblox-script, instance-tree]
tools_used: [Bash]
companion_scripts: [world/scripts/roblox-bridge.py, world/scripts/roblox-studio.sh]
conventions: [infrastructure]
minimum_mode: autonomous
revision_id: "skill-bootstrap-access-roblox-studio-8dc8ee"
previous_revision_id: null
---

# /access-roblox-studio — Roblox Studio Bridge

Control Roblox Studio programmatically via the AyoBridge plugin. Start game sessions,
query the instance tree, create script instances, and stream console output.

**Prerequisite:** Roblox Studio must be running with the AyoBridge plugin enabled.
The bridge server is always the agent's responsibility (`roblox-studio.sh start-bridge`).
If the Studio plugin is not connected, create a pending-question for the user and
proceed with non-Studio work. This is NOT a blocker for the entire agent — only for
live plugin operations.

**Single source of truth for routing decisions**: `world/conventions/roblox-bridge-usage.md`
— capability matrix (what the bridge CAN and CANNOT do), the bridge-vs-ask-user
decision tree, the live-edit persistence rule (no File>Save after bridge mutations),
and the edit-time vs runtime path distinction. Consult that file before writing
user-leg instructions for any Roblox-asset task.

## Architecture

The AyoBridge plugin in Studio cannot host an HTTP server (Roblox limitation). Instead, it
polls a local bridge server for commands every 5 seconds. The bridge server is a Python
HTTP process that queues commands from the agent and delivers them to the plugin.

```
Agent → roblox-studio.sh → HTTP POST → Bridge Server (127.0.0.1:28080)
                                              ↕ poll every 5s
                                        AyoBridge Plugin (Studio)
                                              ↕ during sessions
                                        AyoTestController (console, heartbeats)
```

## Companion Scripts

- `world/scripts/roblox-bridge.py` — Bridge HTTP server (run as background process)
- `world/scripts/roblox-studio.sh <command>` — Agent-facing CLI

## Commands

### Bridge Lifecycle
```bash
Bash: world/scripts/roblox-studio.sh start-bridge    # Start bridge server (background)
Bash: world/scripts/roblox-studio.sh stop-bridge      # Stop bridge server
Bash: world/scripts/roblox-studio.sh status            # Health check + plugin connection
```

### Game Sessions
```bash
# Start a RUN-mode session (server only, no player needed)
Bash: world/scripts/roblox-studio.sh start-session --mode RUN --timeout 60

# Start a PLAY-mode session (waits 30s for player connection)
Bash: world/scripts/roblox-studio.sh start-session --mode PLAY --timeout 120

# With workspace attributes (e.g., enable integration tests)
Bash: world/scripts/roblox-studio.sh start-session --mode RUN --attrs '{"runCancelCorrectTests":true}'

# Check session status
Bash: world/scripts/roblox-studio.sh session-status <session-id>

# Read console output (streamed by AyoTestController every 5s)
Bash: world/scripts/roblox-studio.sh session-console <session-id>

# Stop a running session
Bash: world/scripts/roblox-studio.sh stop-session <session-id>
```

### Instance Tree Query (works idle AND during user F5-played sessions)
```bash
# Query ServerScriptService tree
Bash: world/scripts/roblox-studio.sh query "ServerScriptService" --depth 2

# Query a specific script
Bash: world/scripts/roblox-studio.sh query "ServerScriptService.AyoaiServerScripts.Driver" --depth 1

# Read a script's full Source (for content / freshness verification)
Bash: world/scripts/roblox-studio.sh query "<DotPath>" --depth 0 --include-properties "Source"

# Read workspace attributes (testScenario, ayoBridgeMode, ayoKey, etc.)
Bash: world/scripts/roblox-studio.sh query "Workspace" --depth 0

# Inspect live player state during a user F5-played session
Bash: world/scripts/roblox-studio.sh query "Players" --depth 2
```

QUERY response includes `Properties.<PropertyName>` for every entry passed
to `--include-properties`, AND `Attributes` (the result of `:GetAttributes()`
on the queried instance). Properties is for Roblox-defined fields (Source,
Value, Name, ClassName); Attributes is for user-defined attributes set via
`:SetAttribute()` or `--attrs` at session start.

**LIVE-QUERY-ON-F5 (verified 2026-05-07, ses-1778188522343_514)**: QUERY works
against the live Studio DataModel even during sessions started by the user
clicking the Studio Play button (F5). `roblox-studio.sh status` will show
`active_sessions: 0` and `session-status` / `session-console` will return
"session not found" for F5 sessions — those endpoints only see bridge-started
sessions. But QUERY does NOT require a bridge-tracked session: as long as
the Studio plugin is connected, the agent can inspect any DataModel state,
hash-verify deployed Source, and read Attributes during a user play session.
This is the canonical agent-side diagnostic path for client-side errors the
user reports during play — DO NOT wait for the user's session to end to
inspect state. Run query while they play.

### Studio Source Freshness Probe (the "Is Studio at this commit?" check)

Bridge QUERY with `--include-properties "Source"` returns the script's full
Lua body. Hash-compare against the local repo file (LF-normalized — Studio
strips CRLF on import) to verify Studio is at-or-past a specific commit:

```bash
Bash: world/scripts/roblox-studio.sh query "<DotPath>" --depth 0 \
  --include-properties "Source" > /tmp/studio.json
Bash: py -3 -c "
import json, hashlib
with open('/tmp/studio.json', encoding='utf-8') as f:
    src = json.load(f)['result']['result']['Properties']['Source']
with open('<local-path>', 'r', encoding='utf-8', newline='') as f:
    local = f.read().replace('\r\n', '\n')
match = hashlib.sha256(src.encode()) == hashlib.sha256(local.encode())
print('MATCH' if match else 'DIFFERS')
"
```

Pick a script the target commit modified for a commit-specific probe.
This is the agent-runnable equivalent of `deploy-sha-probe.sh` for the
Studio side. Defer reasoning of the form "no Roblox-side equivalent of
deploy-sha-probe.sh exists" is stale — use this pattern instead.

### Create Script Instances
```bash
# Create a new Script (required before GitHub Actions can deploy new .lua files)
Bash: world/scripts/roblox-studio.sh create-instance "ServerScriptService.AyoaiServerScripts.NewScript" Script --source "-- placeholder"
```

### Delete Script Instances
```bash
# Delete an instance from Studio (requires plugin with DELETE_INSTANCE support)
Bash: world/scripts/roblox-studio.sh delete-instance "ServerScriptService.AyoaiServerScripts.ServiceGlobalScripts.OldScript"

# Fallback if plugin lacks DELETE_INSTANCE: use create-instance with Folder+overwrite trick
# overwrite=true Destroys existing, Folder creation fails at Source setter, net effect: deletion
curl -s -X POST http://127.0.0.1:28080/api/create-instance \
  -H "Content-Type: application/json" \
  -d '{"path":"<dotted.path>","scriptType":"Folder","source":"","overwrite":true}'
```

## Workspace Attributes

### Set (session-start only)
Via `--attrs` on start-session. The plugin's START handler iterates the
attributes map and calls `game.Workspace:SetAttribute(key, value)` BEFORE
play mode begins (AyoBridge.server.lua L85-103). Common keys:
- `runCancelCorrectTests` (bool) — enable integration test suite
- `ayoKey` (string) — API key for AyoAI streaming connection
- `LogLevelFilter` (string) — log verbosity level
- `ayoBridgeMode` (string) — set automatically by AyoTestController
- `testScenario` (string) — test-scenario rotation key (action_jump,
  corridor_remember, conflicting_iaus, etc.) for capability harness

```bash
Bash: world/scripts/roblox-studio.sh start-session --mode RUN \
  --attrs '{"testScenario":"action_jump"}' --timeout 240
```

### Read (any time)
Via `query` (does not require an active session — uses the plugin's QUERY
handler which calls `:GetAttributes()` and returns the result):

```bash
Bash: world/scripts/roblox-studio.sh query "Workspace" --depth 0
```

### Limitations
- Workspace-level only at session start. There is currently no
  on-demand `SET_ATTRIBUTE` command in the plugin (would require a 5th
  command type beyond START / CREATE_INSTANCE / QUERY / DELETE_INSTANCE).
- Per-instance attributes mid-session are not exposed.
- Defer reasoning of the form "workspace.X attribute is operator-set;
  bridge cannot SetAttribute" is FALSE for the session-start path —
  only true if the goal genuinely needs mid-session writes.

## Session Lifecycle

1. Agent calls `start-session` → command queued in bridge
2. Plugin polls `/next-command` → receives START command
3. Plugin sets workspace attributes, calls `StudioTestService:ExecuteRunModeAsync()`
4. AyoTestController activates → sends heartbeats (1s) + console (5s) to bridge
5. Agent reads console via `session-console`, checks status via `session-status`
6. Session ends via timeout, agent stop signal, or internal completion
7. Plugin posts final result to bridge

## Script Sync Safety (guard-010)

There are TWO paths to edit Roblox scripts. They MUST NOT cross.

| Path | Creates? | Edits? | Syncs? |
|------|----------|--------|--------|
| Git repo → GitHub Actions → Roblox Open Cloud | No | Yes | Git → Studio |
| AyoBridge → Studio directly | Yes | Yes | No sync back |

**Rules:**
- `ServerScriptService`, `ServerStorage`, `ReplicatedFirst`, `StarterGui` scripts: **git-only**
  - NEVER edit these via bridge — silent desync with git repo
  - Bridge `CREATE_INSTANCE` allowed ONLY for bootstrapping new instances
  - After bridge creation: create `.lua` file in git, push, then all future edits through git
- `Workspace` scripts: **bridge-only** (ephemeral, not in git)
  - Temporary test scripts, activity generators, experiment drivers
  - Safe to create and edit freely via bridge

**Procedure for adding a new script:**
1. `roblox-studio.sh create-instance "<Service>.<Path>.<Name>" <Type> --source "-- placeholder"`
2. Create `GameScripts/<Service>/<Path>/<Name>.<ext>.lua` in git repo with real source
3. `git add && git commit && git push` → GitHub Actions deploys real source
4. All future edits: git only

## Infra-Health Component

Component: `roblox-studio`. Probe: `curl http://127.0.0.1:28080/api/health` — checks
bridge is running and plugin has polled recently (within 15s).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The last `roblox-studio.sh` invocation (or the `git push` in Step 3 of the add-script
procedure) is the terminal tool call. Never end with a text summary.
