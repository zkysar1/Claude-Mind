---
name: start
description: "Start or resume the agent in reader, assistant, or autonomous mode"
triggers:
  - "/start"
minimum_mode: any
conventions: [session-state, curriculum]
---

# /start — Start or Resume Agent

USER-ONLY COMMAND. Claude must NEVER invoke this skill.

## Syntax

```
/start <agent-name>                    # Default: autonomous (backward compat)
/start <agent-name> --mode reader      # Read-only knowledge access
/start <agent-name> --mode assistant   # User-directed learning
/start <agent-name> --mode autonomous  # Full perpetual loop (same as no flag)
```

On resume (agent already exists):
```
/start                                 # Resume in current mode
/start --mode <mode>                   # Switch mode and resume
```

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

**Step 0.5: Parse Mode** — Extract `--mode` argument if present. Valid values: `reader`, `assistant`, `autonomous`. If omitted: default to `autonomous` (both new and existing agents).

**Step 1: Check Requested Agent's State** — The agent name comes from the `/start <name>` argument.
Check THIS agent's state specifically:

Bash: `AYOAI_AGENT=<agent-name> bash core/scripts/session-state-get.sh`

The `AYOAI_AGENT=<agent-name>` env prefix ensures we read `<agent-name>/session/agent-state`,
not another agent's state. If no `<agent-name>` was provided (bare `/start` or `/start --mode`),
omit the prefix — use the current session binding.

## Behavior by Current State

### RUNNING (agent-state contains "RUNNING")

The agent is in autonomous mode. This could mean another Claude Code window is
actively running the loop, OR a previous session crashed/closed without `/stop`.
Either way, `/start` cannot proceed — autonomous mode must be stopped first.

Output:

```
⚠ Agent '<agent-name>' is in autonomous mode (RUNNING state).

Only one autonomous session per agent is allowed.

To recover:
  1. If another window is running the loop — run /stop there
  2. If the previous session crashed — run /stop here to clean up
  3. Then: /start <agent-name> [--mode <mode>]

Reader and assistant modes are safe to run in multiple windows,
but autonomous mode must be stopped before restarting.
```

DONE. No state changes. No-op.

### IDLE (agent-state contains "IDLE")

0. **Rebind Agent to Session**

   Bash: `echo "<agent-name>" > ".active-agent-$(cat .claude/settings.local.json 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo unknown)"`

   Writes the session-keyed binding file (used by hooks to resolve agent from session ID).

   **CRITICAL — Agent Prefix Contract**: For the remainder of this session, prefix ALL
   Bash tool calls with `AYOAI_AGENT="<agent-name>"`. This is the ONLY mechanism for
   agent resolution in scripts. Example:
     `AYOAI_AGENT=<agent-name> bash core/scripts/aspirations-read.sh --active`

1. Determine target mode:
   - If `--mode` flag provided: use that mode
   - Else: `autonomous` (always — regardless of previous mode)

2. Set mode: Bash: `session-mode-set.sh <target-mode>`

3. Based on target mode:

   **Reader mode:**
   - Set persona: Bash: `session-persona-set.sh true`
   - Invoke `/prime` (with `--read-only` context — reader mode)
   - Load mode instructions: Read `core/config/modes/reader.md`
   - Output: "Reader mode active. I have access to all accumulated knowledge. Ask me anything."

   **Assistant mode:**
   - Set persona: Bash: `session-persona-set.sh true`
   - Invoke `/prime`
   - Load mode instructions: Read `core/config/modes/assistant.md`
   - Output: "Assistant mode active. I can learn when you teach me — give me directives like 'learn about X' or 'remember that Y'."

   **Autonomous mode:**
   - Bash: `session-state-set.sh RUNNING`
   - Bash: `echo "$AYOAI_SESSION_ID" > <agent>/session/running-session-id`
   - Bash: `session-signal-clear.sh stop-loop`
   - Output: "Agent resumed. Learning loop starting."
   - Invoke `/boot`

### UNINITIALIZED (agent-state doesn't exist or <agent>/ doesn't exist)

**Phase A: Agent Name and Session Binding**

The agent name from the `/start <name>` command becomes the directory name.
The agent directory must exist before path configuration (since `local-paths.conf` lives inside it).

A1. Validate the agent name (from the `/start <name>` argument):
   - Must be lowercase kebab-case (letters, digits, hyphens)
   - Must not conflict with reserved names (core, meta, world, .git, .claude, etc.)

A2. **Bind Agent to Session**

   Bash: `echo "<agent-name>" > ".active-agent-$(cat <agent-name>/session/latest-session-id 2>/dev/null || echo unknown)"`

   The session-keyed file (`.active-agent-<SID>`) is used by hooks to resolve
   which agent a session belongs to. One file per session, no shared global file.

A3. Create the agent directory (if it doesn't exist):

   Bash: `mkdir -p <agent-name>`

**Phase B: Configure External Paths** (only if `<agent>/local-paths.conf` does not exist)

Each agent stores its own path configuration. `world/` and `meta/` live at external
user-supplied paths (shared drive, NAS, OneDrive, or local directory).

B1. Ask for the **world directory** path:

   ```
   First, I need to know where to store collective knowledge.

   **World Directory** — This is where all shared domain knowledge lives:
   the knowledge tree, hypotheses, reasoning bank, aspirations, and more.
   Multiple agents and machines can share this directory.

   Point me to a directory. It can be:
   - An empty directory (I'll set up a fresh world)
   - An existing world directory (I'll connect to it)

   Examples:
   - C:/Users/Shared/my-project/world
   - /mnt/nas/projects/my-project/world
   - ./world  (local, relative to this repo)

   Where should the world directory be?
   ```

B2. AskUserQuestion (allowed — agent-state is not RUNNING yet)

B3. Validate the world path:
   - Resolve relative paths against PROJECT_ROOT
   - Check directory exists (or parent exists and is writable)
   - If **doesn't exist**: create it, confirm "Created new directory at {path}"
   - If **empty**: confirm "Empty directory — I'll set up a fresh world"
   - If **populated** (has `knowledge/` or `.initialized`): confirm "Found an existing world at {path} — I'll connect to it"
   - If **not writable**: tell user, ask for a different path

B4. Ask for the **meta directory** path:

   ```
   **Meta Directory** — This is where domain-agnostic improvement strategies
   live. It tracks how the agent gets better at learning itself, independent
   of any specific domain.

   Same rules: empty directory for fresh start, or existing meta directory.

   Where should the meta directory be?
   ```

B5. AskUserQuestion

B6. Validate the meta path (same rules as B3)

B7. Write `<agent>/local-paths.conf`:
   ```bash
   # Paths to external world and meta directories
   # Written by /start — edit manually to change locations
   WORLD_PATH={validated_world_path}
   META_PATH={validated_meta_path}
   ```
   IMPORTANT: Use forward slashes on all platforms (e.g., `C:/Users/Shared/world`,
   not `C:\Users\Shared\world`). Backslashes are interpreted as escape sequences
   when bash sources the file. Python handles both slash styles.

B8. Confirm paths:
   ```
   Paths configured:
     World: {world_path}
     Meta:  {meta_path}
   ```

B9. **Add permissions for external paths** — Ask for confirmation:
   ```
   I need to add read/write permissions for these directories to your
   local settings (.claude/settings.local.json). This file is local to
   your machine and not committed to git.

   Permissions to add:
     Read({world_path}/*)
     Write({world_path}/*)
     Edit({world_path}/*)
     Read({meta_path}/*)
     Write({meta_path}/*)
     Edit({meta_path}/*)

   OK to add these?
   ```

B10. AskUserQuestion for confirmation
   - If yes: Read `.claude/settings.local.json` (create if missing).
     Merge new allow rules into `permissions.allow` array (don't duplicate
     if already present). Write the updated file.
   - If no: warn that file access to external paths may require manual approval

If `<agent>/local-paths.conf` already exists, skip Phase B entirely — paths are already configured.

**Phase C: The Program and Agent Identity**

Phase C establishes two separate things:
- **The Program** (`world/program.md`) — The overarching mission shared by ALL agents in this
  world. Written once, shared across agents. Answers: "Why does this world exist?"
- **Self** (`<agent>/self.md`) — This specific agent's identity, role, and perspective.
  Unique per agent. Answers: "Who am I? What is my role?"

These are NOT the same thing. The Program is the world's purpose. Self is the agent's identity.

**C0. Initialize infrastructure** (all modes):
`bash core/scripts/init-mind.sh`

**C0.5. Configure domain conventions** (only if `world/conventions/` has no `.md` files):

Bash: `source core/scripts/_paths.sh && ls "$WORLD_DIR/conventions/"*.md 2>/dev/null | head -1`
IF empty (fresh world — no conventions configured yet):

  Prompt user:
  ```
  **Domain Conventions** — These control how I behave around task execution.

  **Post-Execution**: What should happen after I finish a development task?
  Common choices:
  - Commit and push code changes (read each repo's CLAUDE.md for test commands first)
  - Just commit, don't push (you push manually)
  - Hold changes for your review (most cautious)
  - Custom workflow

  **Pre-Execution**: Any checks before starting work?
  Common choices:
  - Check curriculum stage permissions
  - Pull latest from all repos first
  - Skip (no pre-execution checks)

  What are your preferences? (Or say "default" for: test per repo CLAUDE.md, then commit-and-push)
  ```

  AskUserQuestion

  Parse response and write `$WORLD_DIR/conventions/post-execution.md` with the user's
  post-execution preferences as procedural steps. The convention must include:
  - Conditions for when each step applies
  - Testing instructions (read each repo's CLAUDE.md for test commands)
  - What to do when pre-conditions aren't met (create Unblock goal, don't hold silently)

  If user provided pre-execution preferences, write `$WORLD_DIR/conventions/pre-execution.md`.

  If user said "default": write the default post-execution convention (commit-and-push
  after testing per each repo's CLAUDE.md).

IF `world/conventions/` already has `.md` files:
  Skip — conventions already configured (existing world).

**C1. The Program** (all modes):
Read `world/program.md`. If empty or only whitespace:

```
What is **The Program** for this world?

The Program is the shared purpose — the overarching mission that all agents
in this world work toward. It lives in world/program.md and is shared across
every agent.

Examples:
- "Build and ship the best project management tool in the market."
- "Research and synthesize machine learning papers into actionable knowledge."
- "Develop a robust home automation system with adaptive routines."

What should The Program be? (Or say "skip" to leave it blank for now.)
```

- AskUserQuestion
- If user provides content (not "skip"): Write to `world/program.md`
- If `world/program.md` was already populated: display it briefly and proceed

Phase C then adapts based on mode:

### Phase C for Reader Mode (simplified)

C2. AskUserQuestion:
   ```
   Setting up reader mode — read-only access to domain knowledge.

   Optional: Tell me who this agent is — its specific role and perspective.
   This helps me contextualize answers. Or say "skip" to use just The Program
   for context.
   ```

C3. If user provided an identity (not "skip"), write `<agent>/self.md`:
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

C4. Set mode and state:
   - Bash: `session-mode-set.sh reader`
   - Bash: `session-state-set.sh IDLE`
   - Bash: `session-persona-set.sh true`

C5. Invoke `/prime` (reader context — pass `--read-only` to retrieve.sh)

C6. Load mode instructions: Read `core/config/modes/reader.md`

C7. Output: "Agent initialized in reader mode. I have access to all accumulated knowledge. Ask me anything."

### Phase C for Assistant Mode

C2. Display the identity prompt:

   ```
   Now I need a few things from you:

   1. **My Self** — This is the agent's identity. It tells me WHO I am
   and WHAT my role is. This is separate from The Program (the world's
   shared purpose) — Self is about this specific agent.

   Examples:
   - "You are a QA engineer for Acme Corp."
   - "You are a personal research assistant focused on ML papers."

   2. **My Aspirations** — Your goals. I won't execute them autonomously,
   but they help me organize and prioritize when you give me directives.

   Examples:
   - "Learn the codebase thoroughly."
   - "Research competitor platforms."

   3. **My Curriculum** (optional) — Staged learning plan with graduation
   gates before attempting more complex tasks.

   Tell me these three — your Self, your Aspirations, and optionally
   your Curriculum. I'll learn when you teach me.
   ```

C3-C7. Same as autonomous Phase C steps C3-C7 (parse, echo, confirm, curriculum, self.md)

C8. Set mode and state:
    - Bash: `session-mode-set.sh assistant`
    - Bash: `session-state-set.sh IDLE`
    - Bash: `session-persona-set.sh true`

C8.5. Invoke `/prime` — load domain context before aspiration creation.

C9. Invoke `/create-aspiration from-user` with extracted aspirations

C10. Load mode instructions: Read `core/config/modes/assistant.md`

C11. Output: "Agent initialized in assistant mode. I'll learn when you teach me — give me directives like 'learn about X' or 'remember that Y'."

### Phase C for Autonomous Mode (current behavior)

C2. Display the identity and aspirations prompt:

   ```
   Now I need three things from you:

   **My Self** — This is the agent's identity. It tells me WHO I am
   and WHAT I'm for. It's the fundamental drive that shapes every decision
   I make. Think of it as the soul of the agent. This is separate from
   The Program (the world's shared purpose) — Self is about this specific agent.

   Examples:
   - "You are an autonomous QA engineer for Acme Corp. Always be looking
     for the next improvement."
   - "You need to make money or die. Find every revenue opportunity."
   - "You are a personal research assistant focused on machine learning
     papers and implementations."

   **My Aspirations** — These are your goals. Think of them as a feature
   list, or life goals, or a to-do list. They can be literally anything —
   learn something, build something, analyze something, fix something.
   I can have multiple at once and I'll break each into actionable steps.

   Examples:
   - "Learn the codebase and API surface thoroughly."
   - "Improve test coverage to 80%."
   - "Research competitor platforms and identify opportunities."

   **My Curriculum** (optional) — This is your staged learning plan.
   It defines what capabilities I unlock as I demonstrate competence.

   If you don't provide one, I'll use a sensible default:
     Stage 1 (Foundation): Learn and explore (no Self edits, no forging)
     Stage 2 (Growth): Apply knowledge (Self edits + forging enabled)
     Stage 3 (Autonomy): Full capabilities (parallel execution enabled)

   Tell me all three — your Self, your Aspirations, and optionally
   your Curriculum. The more detail, the better I can act autonomously.
   ```

C3. Parse response:
   - Extract Self (identity/purpose/drive)
   - Extract aspiration descriptions (one or more goals/directions)
   - Extract curriculum stages (if provided). If user omits curriculum or
     says "default": note "use defaults"

C4. Echo back understanding:

   ```
   Here's what I understand:

   **My Self**
   [parsed Self — the agent's own words summarizing the user's intent]

   **Aspirations I'll create:**
   1. [title] — [brief description with initial goals]
   2. [title] — ...

   **Curriculum (Learning Stages):**
   1. [Stage name] — [description]. Unlocks: [none / self-edits / etc.]
      Graduation: [gate descriptions in plain language]
   2. [Stage name] — ...
   (or: "Using default 3-stage curriculum: Foundation → Growth → Autonomy")

   Does this look right?
   ```

C5. AskUserQuestion for confirmation (yes / adjust)
   - If adjust: re-parse and echo again
   - If yes: proceed

C6. Write curriculum to `<agent>/curriculum.yaml`:
   ```
   IF user provided custom stages:
     Parse into stage objects following the schema:
       - id: cur-01, cur-02, ... (sequential)
       - name: parsed stage name
       - description: parsed description
       - unlocks: infer from user intent (default all false for early stages,
         progressively enable for later stages)
         - allow_self_edits: false/true
         - allow_forge_skill: false/true
         - allow_multi_goal_parallelism: false/true
       - graduation_gates: infer from user criteria, using gate types:
         - metric_threshold (for competence/numeric targets)
         - count_check (for goal completion counts)
         - log_scan (for event counts)
         - command_check (for script-based checks)
         If user gives vague criteria: use reasonable defaults
         (e.g., "after mastering basics" → competence >= 0.30)
       - gate_status: initialize all as {passed: false, last_checked: null, current_value: null}

   IF user said "default" or omitted curriculum:
     Read core/config/curriculum.yaml → default_stages
     Use those stages directly

   Write <agent>/curriculum.yaml (Edit the file seeded by init-mind.sh):
     current_stage: first stage ID (cur-01)
     stage_history:
       - stage_id: cur-01
         entered: "{today}"
         exited: null
     stages: [the parsed or default stage array]
   ```

C7. Write `<agent>/self.md` with parsed Self (where `<agent>` is the active agent directory):
   ```markdown
   ---
   created: "{today}"
   last_updated: "{today}"
   last_update_trigger: "initial_creation"
   source: "user"
   ---

   # Self

   {parsed Self content}
   ```

C8. Set mode and state:
    - Bash: `session-mode-set.sh autonomous`
    - Bash: `session-state-set.sh RUNNING`
    - Bash: `echo "$AYOAI_SESSION_ID" > <agent>/session/running-session-id`
    - Bash: `session-signal-clear.sh stop-loop`

C8.5. Invoke `/prime` — load domain context before aspiration creation.
    When connecting to an existing world, this ensures goal decomposition
    benefits from accumulated knowledge. On a fresh world, prime loads
    empty stores harmlessly.

C9. Invoke `/create-aspiration from-user` with the extracted aspiration descriptions

C10. Output: "Agent initialized. Learning loop starting."

C11. Invoke `/boot`

## Chaining
- Calls: /boot (autonomous mode), /prime (all modes during init; reader/assistant resume)
- Called by: User only. NEVER by Claude.
