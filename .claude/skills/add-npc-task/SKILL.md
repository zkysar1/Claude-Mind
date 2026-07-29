---
name: add-npc-task
user-invocable: false
minimum_mode: autonomous
conventions:
  - infrastructure
  - secrets
forged: true
forged_by: alpha
forged_date: "2026-04-04"
forged_from: g-213-07
triggers:
  - "add npc task"
  - "register task"
  - "new task type"
description: "Registers a new NPC task type end-to-end across all four AyoAI layers in one pass: Lambda API routes, Java server (IAUS) handlers, Python Processor (BT generation) workers, and Roblox Lua task definitions. Use whenever the agent is adding a new NPC behavior, action, or capability (speak, wait, sit, examine, dance, etc.) that must be wired through the full stack. Editing layers manually risks inconsistent task IDs and silent NPC-action misfires — always use this skill."
revision_id: "skill-bootstrap-add-npc-task-3c3efe"
previous_revision_id: null
---

# /add-npc-task — Register a New NPC Task Type

Automates the 5-step protocol for adding a new NPC task across the full AyoAI stack.
Each NPC task (speak, wait, sit, examine, dance, etc.) must be registered in 4 layers:
Lambda (API), Server (IAUS), Processor (BT generation), Roblox (execution).

## Parameters

- `task_key` (required): The ayoTaskKey (e.g., "patrol", "craft", "fish"). Must match `^[a-zA-Z0-9_-]+$`, max 64 chars. **Case-sensitive** — must match the Roblox `TaskCapabilities.register(...)` name.
- `description` (required): What the task does (stored as `ayoTaskDesc`, max 128 chars). Shown to NPCs and used by BT generation.
- `node_params` (optional): JSON object mapping param name → TYPE STRING (a schema, not values). Allowed types: `vector3`, `decimal`, `string`, `ayoKey`.
  Example: `{"target": "vector3", "duration": "decimal"}`
- `environment_key` (optional): Target environment. Default: NPCDemoExperiment.
- `dry_run` (optional): If true, show what would be done without executing. Default: false.

**API reference**: `world/conventions/environment-task-api.md` is the verified locator
for the registration endpoint, auth, and schema (g-310-02/g-310-05, 2026-06-28).
Read it before changing Steps 1-2 — it supersedes any endpoint memory.

## Step 0: Validate Inputs

```
IF task_key is empty OR does not match ^[a-zA-Z0-9_-]+$:
    ABORT: "Invalid task_key — must be alphanumeric with hyphens/underscores, max 64 chars"
IF description is empty:
    ABORT: "Description is required"
```

## Step 1: Register Task in Lambda API

The ManageEnvironmentsAndTasks Lambda stores tasks per environment. Registration
goes to the **httpV1 Lambda directly** — NOT the operator service, which has no
task-registration route (g-310-05 correction; the pre-2026-07 version of this
skill targeted `{operator_host}:8080/api/v1/...` and would fail every time).

```
# Build task object per Lambda validate_task (see environment-task-api.md)
task_object = {
    "ayoTaskKey": task_key,          # case-sensitive, <=64, ^[a-zA-Z0-9_-]+$
    "ayoTaskDesc": description       # required, non-empty, <=128 chars
}
IF node_params:
    # Values are TYPE STRINGS from ALLOWED_NODE_PARAM_TYPES:
    #   vector3 | decimal | string | ayoKey
    # (ayoKey = entity-identifier for target refs; decimal for numerics)
    task_object["nodeParams"] = node_params

# Credential: header name is AYOAI-API-KEY but the env var is AYO_OPERATOR_KEY
# (rb-2515 — there is NO var literally named AYOAI_API_KEY; the wrong name
# produced a false-absence escalation on g-310-02/05).
Bash: set -a; source .env.local; set +a   # then use $AYO_OPERATOR_KEY, never echo it

curl -s -X POST "https://api.ayoai.com/httpV1/environments/{environment_key}/tasks" \
    -H "AYOAI-API-KEY: $AYO_OPERATOR_KEY" \
    -H "Content-Type: application/json" \
    -d '{task_object_json}'

IF response.status == "success" (HTTP 201):
    Output: "Step 1 PASS: Task '{task_key}' registered in Lambda"
ELIF HTTP 409 (duplicate ayoTaskKey):
    Output: "Step 1 SKIP: Task '{task_key}' already exists"
ELSE:
    Output: "Step 1 FAIL: {response.body}"
    ABORT with error details
```

## Step 2: Verify Registration

```
curl -s "https://api.ayoai.com/httpV1/environments/{environment_key}/tasks" \
    -H "AYOAI-API-KEY: $AYO_OPERATOR_KEY"

Parse response: find task where ayoTaskKey == task_key
IF found:
    Output: "Step 2 PASS: Task '{task_key}' verified in task list"
ELSE:
    Output: "Step 2 FAIL: Task not found after registration"
    # Rollback Step 1: DELETE task
    curl -X DELETE ".../tasks/{task_key}" ...
    ABORT
```

## Step 3: Add Lua Expansion Stub

**Box requirement**: Steps 3-4 need the Ayoai-Roblox-Integration product repo
(Windows dev box) — they cannot run on repo-less containers (guard-949 class).
When executing from such a box, complete Steps 1-2 (API-only) and file the Lua
half as a handoff goal instead of aborting the whole skill.

The task needs a Lua implementation in `task_capabilities.lua` so Roblox knows how to execute it.

```
TASK_CAPS_PATH = "C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration/GameScripts/ServerScriptService/AyoaiServerScripts/AyoaiListActors/AyoBehavior/BehaviorTree/CapabilitySeeds/default_action_seeds.lua"

Read TASK_CAPS_PATH
# Find the task registration section (getCapabilities function)
# Add entry for the new task

# Each task has a function that returns BT-compatible actions.
# Minimal stub:
new_entry = '''
    -- {task_key}: {description}
    ["{task_key}"] = function(params, context)
        return {{
            type = "{task_key}",
            params = params or {{}},
            description = "{description}"
        }}
    end,
'''

# Find insertion point (before the closing "}" of the capabilities table)
# Insert the new entry
Edit TASK_CAPS_PATH: insert new_entry at appropriate location

Output: "Step 3 PASS: Lua expansion added for '{task_key}'"
```

## Step 4: Commit and Push

```
cd C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration
git add GameScripts/.../default_action_seeds.lua
git commit -m "Add {task_key} task type to NPC capabilities

Registered via /add-npc-task skill. Description: {description}

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
git push

IF push succeeded:
    Output: "Step 4 PASS: Changes committed and pushed ({commit_sha})"
ELSE:
    Output: "Step 4 FAIL: Push failed — {error}"
    # Do NOT rollback Lambda registration — task exists but lacks Lua impl
    # The task will be non-functional until Lua is deployed
```

## Step 5: Verify End-to-End (Deferred)

Full E2E verification requires a running game server to confirm the Server loads
the task and Roblox can execute it. This step is logged as a follow-up goal.

```
# Create verification goal
echo '{"title":"Verify: {task_key} task works end-to-end in game session","description":"Run a game session and verify: (1) Server loads {task_key} from tasks.json, (2) IAUS can select it, (3) BT generation includes it, (4) Roblox executes the Lua stub. Created by /add-npc-task skill.","priority":"MEDIUM","category":"npc-intelligence","participants":["agent"],"origin_signal":"investigate:add-npc-task-{task_key}-e2e"}' | Bash: aspirations-add-goal.sh --source world {target_aspiration}

Bash: echo "Step 5 DEFERRED: E2E verification goal created — needs game session"
```

## Rollback Protocol

If failure occurs at any step:
- Step 1 fails: nothing to rollback
- Step 2 fails: DELETE the task from Lambda
- Step 3 fails: revert Lua file changes (`git checkout -- {file}`)
- Step 4 fails: task registered but Lua not deployed — non-functional until manual fix

## Summary Output

```
=== ADD NPC TASK: {task_key} ===
Step 1: Register in Lambda .... {PASS/FAIL/SKIP}
Step 2: Verify registration .... {PASS/FAIL}
Step 3: Add Lua expansion ..... {PASS/FAIL}
Step 4: Commit and push ....... {PASS/FAIL}
Step 5: E2E verification ...... DEFERRED (needs game session)
==========================================
```

## Chaining

- **Called by**: Aspiration goals, `/aspirations-execute` when matching trigger
- **Calls**: `env-read.sh`, `infra-health.sh`, `aspirations-add-goal.sh`
- **Reads**: task_capabilities.lua, Operator API
- **Writes**: task_capabilities.lua (Lua expansion), Lambda tasks.json (via API)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The Summary Output block is a markdown report — the skill MUST follow it with a Bash
call. When Step 4 (commit and push) succeeds, that git push IS the terminal call.
Otherwise end with `Bash: echo "add-npc-task complete"`.
