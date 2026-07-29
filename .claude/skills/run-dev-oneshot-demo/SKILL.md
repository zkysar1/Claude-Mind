---
name: run-dev-oneshot-demo
description: "Stages and executes a one-shot server-side Luau Script inside a live DEV/PPE game session via the AyoBridge and returns greppable console marker lines as evidence, with zero place residue (created in Edit mode, runs at Play, deleted after). MUST use this skill — never hand-roll the create-instance/start-session/console-poll/cleanup sequence — whenever a goal needs server-side code executed in-game: feature demos ('demo equip/activate/place in DEV'), runtime verification ('prove the runtime path live'), or probing in-game-only endpoints (the /sis/ reach path). Fires for any asp-350-style feature demo and any 'run this Lua in a session and show me the output' request. Takes (env, lua_file, marker_prefix, timeout_seconds) and returns the captured marker lines (result AND abort markers)."
forged: true
forged_by: foxtrot
forged_date: "2026-07-16"
forged_from: gap-012
user-invocable: false
parent-skill: aspirations-execute
minimum_mode: assistant
tools_used: [Bash, Write, Read]
companion_scripts: [world/scripts/roblox-studio.sh]
conventions: [roblox-environments]
parameters:
  env: "bridge environment name (default: dev) — env→port map lives in world/conventions/roblox-environments.md"
  lua_file: "path to the one-shot .lua source; MUST begin with the RunService:IsRunning() guard (see Script Contract)"
  marker_prefix: "greppable marker namespace, e.g. F3A (script must print both {marker_prefix}-RESULT and {marker_prefix}-ABORT lines)"
  timeout_seconds: "max console-poll wall clock before forced cleanup (default 300)"
---

# /run-dev-oneshot-demo — Bridge-Staged One-Shot In-Game Execution

Universal in-game execution primitive for DEV demos and runtime verification
(validated in g-350-17 equip demo + g-350-18 activation demo; technique rb-3652).
Executes arbitrary server-side Luau in a game session when the agent box has no
network path to in-game-only endpoints (rb-3558: /sis/ endpoints are reachable
only in-game). The staged script sits inert in Edit mode, runs when the session
enters Play, self-evidences via console marker lines, and is deleted afterward.
Residue-free: runtime-created Instances do not persist after stop (verified),
and delete-instance removes the Edit-mode script.

## Restricted Operations

MUST use `world/scripts/roblox-studio.sh` for ALL bridge interaction
(create-instance, start-session, session-console, stop-session,
delete-instance, status) — never raw curl/HTTP to the bridge ports. The
companion script owns the env→port map, bridge liveness checks, and payload
encoding. Probing the bridge with hand-rolled requests is the rb-246 synthetic-
probe class: false failures that the canonical path would never see.

## Script Contract (input .lua)

The one-shot script MUST:

1. **Guard on Play mode** — first statement:
   `if not game:GetService("RunService"):IsRunning() then return end`
   (Without this, the script executes at create-instance time in Edit mode.)
2. **Print terminal markers, covering BOTH outcomes**:
   - success path: `print("{marker_prefix}-RESULT: <evidence>")`
   - every failure path: `print("{marker_prefix}-ABORT: <reason>")`
   A script that only prints a success marker makes the poll loop wait the
   full timeout on failure — always emit the abort marker on error paths
   (wrap risky sections in pcall and print the error).
3. **Be self-contained** — no ModuleScript dependencies staged separately.

## Procedure

```
0. INPUTS: env (default "dev"), lua_file, marker_prefix, timeout_seconds (default 300)
   script_name = unique demo name, e.g. AyoaiOneshotDemo{marker_prefix}
   script_path = "ServerScriptService.{script_name}"

1. SYNTAX CHECK (rb-3568 — @lune/luau.compile, NOT load()):
   Write a temp checker .luau:
     local luau = require('@lune/luau'); local fs = require('@lune/fs')
     local ok, err = pcall(function() return luau.compile(fs.readFile('{lua_file}')) end)
     if ok then print('SYNTAX-OK') else print('SYNTAX-ERR: ' .. tostring(err)); process.exit(1) end
   Run via the workspace lune runner (Ayoai-Roblox-Integration/Tools/lune/lune.exe;
   if 'Permission denied', chmod +x it — git checkout resets the exec bit, rb-3568).
   ABORT the whole skill on SYNTAX-ERR — never stage a script that does not parse.

2. VERIFY BRIDGE:
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" status --env {env}
   IF bridge not healthy: attempt start-bridge --env {env}; if still down, return
   error "bridge_unreachable" (do NOT file a blocker from inside this skill —
   the caller decides).

3. STAGE (Edit mode — script sits inert behind the IsRunning guard):
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" create-instance --env {env} \
         {script_path} Script --source "$(cat {lua_file})"

4. START SESSION (capharness-* prefix keeps harness scenarios ENABLED — a ses-
   prefix would disable them; g-350-17):
   session_id = "capharness-{marker_prefix}-$(date +%s)"
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" start-session --env {env} --session-id {session_id}

5. POLL CONSOLE for terminal markers (background until-loop, ~10s interval,
   bounded by timeout_seconds). Poll for BOTH "{marker_prefix}-RESULT" AND
   "{marker_prefix}-ABORT" — either terminates the wait:
   Bash (run_in_background or bounded loop):
     source core/scripts/_paths.sh   # hoisted: resolve $WORLD_PATH once, not per-iteration
     for i in $(seq 1 {timeout_seconds/10}); do
       out=$(bash "$WORLD_PATH/scripts/roblox-studio.sh" session-console --env {env} {session_id} 2>/dev/null)
       echo "$out" | grep -E "{marker_prefix}-(RESULT|ABORT)" && break
       sleep 10
     done
   Capture ALL marker lines (grep -E "{marker_prefix}-") — they are the
   skill's return value and the goal's verification evidence.

6. CLEANUP (ALWAYS runs — success, abort, or timeout):
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" stop-session --env {env} {session_id}
   Bash: source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/roblox-studio.sh" delete-instance --env {env} {script_path}
   Residue check (MANDATORY — g-350-23 validation caught a live failure): a
   delete-instance issued immediately after stop-session can land on the
   still-in-run-mode DataModel and be REVERTED by the play-mode rollback when
   Studio returns to Edit ("deleted" response yet the script survives —
   observed 1 of 3 invocations, 2026-07-16). After the session settles, query
   the parent (depth 1) and confirm {script_name} is absent; if present,
   delete-instance AGAIN and re-verify — the second delete (fully in Edit
   mode) persists.

7. RETURN to caller: {status: ok|abort|timeout|bridge_unreachable|syntax_error,
   marker_lines: [...], session_id, console_tail (last ~20 lines on non-ok)}
```

## Error Handling

| Failure | Action |
|---|---|
| Syntax error at Step 1 | Return syntax_error with lune output; nothing was staged — no cleanup needed |
| Bridge down at Step 2 | Return bridge_unreachable after one start-bridge attempt; caller decides blocker/defer |
| create-instance fails | Return error verbatim; nothing staged — no cleanup needed |
| start-session fails | delete-instance the staged script (Step 6 partial), then return error |
| Timeout with no markers | Run FULL cleanup, return timeout + console tail — the tail usually shows why (script never ran = missing IsRunning guard is the common cause; markers absent = script errored before printing, check for red error lines) |
| ABORT marker seen | Run FULL cleanup, return abort + marker lines — this is a SUCCESSFUL skill run reporting a failed demo; do not retry blindly |

Never leave a session running or a staged script behind on ANY exit path —
cleanup (Step 6) is unconditional.

## Input/Output Contract

- Caller (usually a feature-demo or verification goal under aspirations-execute)
  supplies the .lua file and marker prefix; this skill owns staging, session
  lifecycle, evidence capture, and cleanup.
- The returned marker_lines are verification-grade evidence: quote them
  verbatim in the goal's verify summary (they are greppable in session-console
  history if re-verification is needed).

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Step 6 cleanup Bash call (or the Bash echo returning
the result object to the caller). Never end with a text summary.
