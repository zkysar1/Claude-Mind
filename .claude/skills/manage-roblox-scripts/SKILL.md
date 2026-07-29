---
name: manage-roblox-scripts
forged: true
forged_by: user
forged_date: "2026-04-04"
description: "Adds, verifies, or deletes Roblox Lua scripts across the two-channel deployment model: Studio Bridge (live in-memory) and CI/CD (committed repo). Handles conflict detection between channels. Use whenever the user says \"add roblox script\", \"create lua file\", \"delete roblox script\", when Roblox deployment fails with \"Could not find X in path\", or the agent needs to push/remove a Script/LocalScript/ModuleScript cleanly across both channels."
user-invocable: false
triggers:
  - "add roblox script"
  - "create roblox script"
  - "new lua file"
  - "delete roblox script"
  - "roblox deployment failed"
  - "Could not find X in path"
tools_used: [Bash, Read, Edit, Write]
companion_scripts: [world/scripts/roblox-manage-script.sh]
conventions: []
minimum_mode: assistant
revision_id: "skill-bootstrap-manage-roblox-scripts-d867e1"
previous_revision_id: null
---

# /manage-roblox-scripts — Roblox Script Lifecycle Management

**See also**: `world/conventions/roblox-bridge-usage.md` for the broader bridge
routing index (capability matrix, decision tree, persistence model). This skill
covers the **Lua script lifecycle** specifically; non-script asset mutations
(Sound, Part, Model, etc.) route through the bridge directly per that convention.

## The Core Constraint

The Roblox Open Cloud API (used by CI/CD) can **only UPDATE** existing script instances.
It **cannot CREATE or DELETE** instances. This is an architectural limitation of the API.

**Two-channel deployment model**:
- **Channel 1: Studio Bridge** (AyoBridge plugin) — creates and deletes instances
- **Channel 2: CI/CD** (GitHub Actions) — updates script Source on push to main

Therefore:
- **Adding a script** = create instance via Bridge FIRST, then commit code
- **Deleting a script** = delete from git, then delete instance via Bridge
- **Modifying a script** = just edit + commit + push (CI/CD handles it)

## Companion Script

`world/scripts/roblox-manage-script.sh` — automates path mapping and Bridge API calls.

```
roblox-manage-script.sh create <lua-path>    # Create instance via Bridge
roblox-manage-script.sh verify <lua-path>    # Check if instance exists
roblox-manage-script.sh check-bridge         # Test Bridge connectivity
roblox-manage-script.sh map-path <lua-path>  # Show dotted Roblox path
```

All paths are relative to the Roblox repo root (`C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration`).

## Adding a New Script (MANDATORY — follow EXACTLY)

### Pre-flight

```bash
# Step 0: Verify Bridge is reachable
ROBLOX_REPO="C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration" \
  bash world/scripts/roblox-manage-script.sh check-bridge

# IF unreachable:
#   1. Bash: world/scripts/roblox-studio.sh start-bridge  (ensure agent-side bridge is running)
#   2. Wait 15s, retry check-bridge
#   3. IF still unreachable: Studio plugin not connected (Studio may be closed).
#      Create pending-question: "Please open Roblox Studio with AyoBridge plugin"
#      Return INFRASTRUCTURE_UNAVAILABLE with reason "studio-plugin-not-connected"
#      Do NOT create participants:[user] goal.
# Do NOT commit the .lua file until the instance exists in Studio.
```

### Create Instance

```bash
# Step 1: Create the Roblox instance via Bridge
ROBLOX_REPO="C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration" \
  bash world/scripts/roblox-manage-script.sh create "GameScripts/<path>/<ScriptName>.<type>.lua"
```

The script automatically:
- Maps `.server.lua` → Script, `.client.lua` → LocalScript, `.lua` → ModuleScript
- Converts the file path to a dotted Roblox instance path
- Sends a create-instance request to the Bridge
- Polls for completion

### Verify Instance Exists

```bash
# Step 2: Verify the instance was created
ROBLOX_REPO="C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration" \
  bash world/scripts/roblox-manage-script.sh verify "GameScripts/<path>/<ScriptName>.<type>.lua"
```

If verification times out, create pending-question for user about checking Roblox Studio Output window. Continue execution — do not block.

### Commit and Push

```bash
# Step 3: NOW it's safe to commit and push
cd C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration
git add "GameScripts/<path>/<ScriptName>.<type>.lua"
git commit -m "Add <ScriptName> script"
git push origin main
```

CI/CD will PATCH the instance's Source property with the file content.

### Verify Deployment

```bash
# Step 4: Check CI/CD succeeded
cd C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration
gh run list --limit 1 --json conclusion,name -q '.[0] | "\(.conclusion) (\(.name))"'
# Expected: "success (Update Roblox Scripts)"
```

If CI fails with "Could not find X in path", the instance wasn't created in Step 1.
Go back to Step 1.

## Deleting a Script

```bash
# Step 1: Delete from git
cd C:/ZakNoCloud/GitHub/Ayoai/Ayoai-Roblox-Integration
git rm "GameScripts/<path>/<ScriptName>.<type>.lua"
git commit -m "Remove <ScriptName> script"
git push origin main

# Step 2: Delete from Studio via Bridge
# CI/CD ignores deletions — the instance persists in Roblox.
# Attempt deletion via bridge: roblox-manage-script.sh delete (if supported).
# If bridge lacks deletion support: create pending-question asking user to
# delete the instance manually in Roblox Studio Explorer.
# Continue execution in either case — do not block.
```

## Rojo Naming Conventions (Quick Reference)

| Local Extension | Roblox Class | Notes |
|----------------|-------------|-------|
| `.server.lua` | Script | Server-side execution |
| `.client.lua` | LocalScript | Client-side execution |
| `.lua` | ModuleScript | Shared/required module |
| `init.server.lua` | Script | Parent folder becomes the instance |
| `init.client.lua` | LocalScript | Parent folder becomes the instance |
| `init.lua` | ModuleScript | Parent folder becomes the instance |

## Path Mapping Examples

| Local Path | Roblox Instance Path |
|-----------|---------------------|
| `GameScripts/ServerScriptService/AyoaiServerScripts/ServiceGlobalScripts/HeadTrackPlayers.server.lua` | `ServerScriptService.AyoaiServerScripts.ServiceGlobalScripts.HeadTrackPlayers` |
| `GameScripts/StarterGui/AyoaiPlayerGuis/SomeClient.client.lua` | `StarterGui.AyoaiPlayerGuis.SomeClient` |
| `GameScripts/ServerScriptService/AyoaiServerScripts/AyoaiListActors/AyoBehavior/init.server.lua` | `ServerScriptService.AyoaiServerScripts.AyoaiListActors.AyoBehavior` |

## Common Failure: "Could not find X in path"

This CI error means the Roblox instance doesn't exist. The file was committed to git
but never created in Studio. Fix:

1. Run `roblox-manage-script.sh create <path>` to create the instance
2. Re-run the failed CI workflow: `gh run rerun <run-id>` or push an empty commit

## Important Rules

1. **NEVER commit a new .lua file without creating the instance first** — CI will fail
2. **NEVER edit script Source via the Bridge** — git repo becomes stale, next push overwrites
3. **Bridge requires Studio in EDIT mode** — not play mode, not closed
4. **Team Create must be enabled** in the Roblox place for API access
5. **After creating instances, PUBLISH the place** in Studio (File → Publish to Roblox)

## Chaining

- **Called by**: Any skill that creates or modifies Roblox Lua scripts
- **Calls**: `world/scripts/roblox-manage-script.sh`
- **Triggered by**: CI failure email with "Could not find X in path"

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The `roblox-manage-script.sh` or `git push` is the terminal tool call. Never end
with a text summary of what scripts were added/verified/deleted.
