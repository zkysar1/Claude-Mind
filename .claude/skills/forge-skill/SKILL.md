---
name: forge-skill
description: "Meta-skill for forging new skills from recurring capability gaps"
user-invocable: false
triggers:
  - "/forge-skill"
parameters:
  - name: sub-command
    description: "skill <gap-id> | check | list | dismiss <gap-id>"
    required: true
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
  known_pitfalls: []
  reconsolidation_trigger: "After 10 invocations with declining success rate, trigger skill review"
conventions: [aspirations, tree-retrieval, board]
minimum_mode: autonomous
---

# /forge-skill — Skill Forge

Meta-skill that creates new skills from recurring capability gaps tracked in
`meta/skill-gaps.yaml`. Forged skill SKILL.md files go in `.claude/skills/` for
Claude Code discovery. Metadata is tracked in `<agent>/forged-skills.yaml` (not `_tree.yaml`).

## Sub-commands

### Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

### `/forge-skill list` — Show gaps and forged skills

1. Read `meta/skill-gaps.yaml`
2. Display table of all gaps:
   | ID | Name | Encounters | Value | Status | Forge-eligible? |
3. Read `<agent>/forged-skills.yaml` → list forged skills
4. Display list of previously forged skills with creation dates
5. Show forge eligibility summary (how many gaps meet threshold)

### `/forge-skill skill <gap-id>` — Create a new skill from a gap

**Forge Criteria** (ALL must be met):
- Curriculum contract: `Bash: curriculum-contract-check.sh --action allow_forge_skill`
  IF exit code 1: ABORT — "Forge blocked by curriculum (stage: {stage_name}). Forging unlocks at: {unlocks_at}."
- `times_encountered >= config.forge_threshold` (currently 2)
- `estimated_value >= medium`
- No existing skill covers the same procedure
- System developmental gate (type-dependent):
  - Read gap `type` from `meta/skill-gaps.yaml` (default: `analytical`)
  - Read `forge_gate` threshold from `core/config/skill-gaps.yaml` → `gap_types[type]`
  - `utility` gaps require CALIBRATE+ (confidence >= 0.30)
  - `analytical` gaps require EXPLOIT+ (confidence >= 0.60)
  - Check: capability_level of related category >= forge_gate
  node_json=$(bash core/scripts/tree-read.sh --node <category-key>)
  (extract confidence from node_json, or fall back to `<agent>/developmental-stage.yaml`)

**Forge Process**:

1. **Validate** — Check all forge criteria. If any fail, report which and abort.

2. **Extract Procedure** — Read the gap's `encounter_log` contexts and the
   `related_skill` SKILL.md to identify the repeated manual steps. Summarize
   into a procedure template:
   - API endpoints used
   - Parameters that vary per invocation
   - Output format expected by the parent skill
   - Error handling patterns observed
   - Which Claude Code tools (Bash, Write, WebFetch, etc.) the procedure requires
   - Map API endpoints to WebFetch calls
   - Map data processing steps to Bash commands (if applicable)
   - Map file creation to Write (within <agent>/ or .claude/skills/ for forged skills)
   - **Companion scripts**: If the procedure involves restricted or deterministic
     data access (SSH, API calls with read-only enforcement), create companion
     shell scripts in `<agent>/scripts/`:
     - Scripts enforce access boundaries the LLM cannot bypass (e.g., read-only
       SSH commands, download-only SCP, specific API scopes)
     - Scripts use `core/scripts/env-read.sh` for all credentials — no hardcoded secrets
     - Scripts consume credentials in the same shell invocation (variable, not disk)
     - The forged SKILL.md MUST reference companion scripts for restricted
       operations and MUST say "MUST use companion scripts, never raw [tool]"
     - Script naming: `{resource}-{verb}.sh` (e.g., `data-list.sh`, `data-download.sh`)
     - Scripts go in `<agent>/scripts/` — a new directory for agent-forged scripts.
       Lives under `<agent>/` so it's writable and removed when the agent directory
       is deleted. The forge process creates the directory: `mkdir -p <agent>/scripts/`
     - Mark scripts executable: `chmod +x <agent>/scripts/*.sh`

3. **Create SKILL.md** — Write new skill file:
   ```
   .claude/skills/{new-skill-name}/SKILL.md
   ```
   Structure:
   - YAML front matter: name, description, triggers (internal only), parameters, tools_used
   - `user_invocable: false` (sub-skills are called by parents, not users)
   - `tools_used: [Bash, WebFetch, ...]` — which Claude Code tools this skill requires
   - `companion_scripts: [<agent>/scripts/xxx.sh, ...]` — if companion scripts exist
   - If companion scripts exist: add "## Restricted Operations" section mandating
     their use. Example: "MUST use `<agent>/scripts/data-list.sh`, never raw access"
   - Step-by-step procedure extracted from encounters
   - Input/output contract with parent skill
   - Error handling section

4. **Register in Forged Skills** (`<agent>/forged-skills.yaml` + `.gitignore`):
   - Add entry under `skills:` with `parent`, `type`, `forged_date`, `gap_ref`, `triggers`
   - Add `.claude/skills/{new-skill-name}/` to `.gitignore` under the forged skills section
   - Do NOT touch `_tree.yaml` or `_triggers.yaml` — those are static framework files

5. **Update Skill Gaps** (`meta/skill-gaps.yaml`):
   - Set gap `status: forged`
   - Set `forged_into: {skill-name}`
   - Set `forged_date: {today}`

6. **Announce on Board** — Post to the message board so other agents discover the new skill:
   ```
   echo "Forged skill: {skill-name} (from gap {gap-id}). Type: {type}. Parent: {parent-skill}. Path: .claude/skills/{skill-name}/" | bash core/scripts/board-post.sh --channel general --tags forge,{skill-name},{type}
   ```
   IF board post fails: log warning, do NOT abort — board is non-critical.

7. **Register in World Skill Catalog** (`world/skill-catalog.yaml`):
   - IF file does not exist: create with header:
     ```yaml
     # World Skill Catalog — shared registry of forged skills across all agents
     # Append-only. Each agent adds entries when forging new skills.
     skills: []
     ```
   - Read file, append entry under `skills:`:
     ```yaml
     - name: {skill-name}
       forged_by: {agent-name}
       forged_date: "{today}"
       type: {type}
       parent: {parent-skill}
       gap_ref: {gap-id}
       skill_path: .claude/skills/{skill-name}/SKILL.md
       companion_scripts_private: true
       description: "{one-line from forged SKILL.md front matter}"
     ```
   - `companion_scripts_private: true` signals companion scripts live in `<agent>/scripts/`
     (agent-private). Another agent adopting this skill must create its own scripts.
   - IF write fails: log warning, do NOT abort — catalog is non-critical.

8. **Notify User** — Send dedicated notification about the newly forged skill:
   - Category: `info`
   - Subject: `"New Skill Forged: {skill-name}"`
   - Message body:
     ```
     A new skill has been forged from capability gap {gap-id}.

     Skill: {skill-name}
     Type: {type}
     Parent skill: {parent-skill}
     Location: .claude/skills/{skill-name}/SKILL.md
     {IF companion_scripts: "Companion scripts: {list of script paths}"}

     A validation goal will be created to test this skill over 3 invocations.
     ```
   - Invoke `/notify-user` with the above
   - IF notification fails: continue (notify-user has its own fallback cascade)

9. **Create Test Goal** — Add a goal to the relevant aspiration:
   - Find relevant aspiration: Bash: `load-aspirations-compact.sh` → IF path returned: Read it
     (compact data has IDs, titles, categories — no descriptions/verification)
   - Read the target aspiration: Bash: `aspirations-read.sh --id <asp-id>`
   - Add goal with subject: "Validate forged skill: {skill-name}"
   - Type: calibration
   - desiredEndState: "Skill invoked 3 times successfully by parent"
   - Priority: MEDIUM
   - Pipe updated aspiration JSON: `echo '<aspiration-json>' | bash core/scripts/aspirations-update.sh <asp-id>`
   - (User notification already sent in Step 8 — do not send a second notification here.)

10. **Report** — Summarize what was created, where it lives, and what triggers it.
   - If companion scripts were created: list them with their purpose and usage

### `/forge-skill check` — Audit both trees for coherence

Run structural integrity checks across all system registries:

1. **Forged skills audit** (`<agent>/forged-skills.yaml`):
   - Every entry has a matching SKILL.md in `.claude/skills/{name}/`
   - Every entry has a matching `.claude/skills/{name}/` line in `.gitignore`
   - No orphaned `.claude/skills/` directories missing from the registry

2. **Skill gaps audit** (`meta/skill-gaps.yaml`):
   - Gaps with `status: forged` have matching entry in `<agent>/forged-skills.yaml`
   - No gaps exceed `config.max_gaps` (20)
   - Encounter logs respect `config.encounter_log_limit` (5)

3. **Memory tree cross-check** (`world/knowledge/tree/_tree.yaml`):
   - Forged skills map to categories at EXPLOIT+ capability level

4. **Report** — List all findings: OK checks, warnings, and errors.

### `/forge-skill dismiss <gap-id>` — Reject a gap

1. Read `meta/skill-gaps.yaml`
2. Set gap `status: dismissed`
3. Set `dismissed_reason: "manual dismiss via /forge-skill dismiss"`
4. Set `dismissed_date: {today}`
5. The gap remains in the registry (never delete) but is excluded from forge eligibility

## Forge Naming Convention

New skill names follow kebab-case and describe the action:
- `check-{domain}-{data}` (e.g., check-stock-prices, check-weather-forecast)
- `fetch-{source}-{type}` (e.g., fetch-api-scores, fetch-news-sentiment)

Pattern: `{verb}-{domain}-{noun}` — keeps names scannable and predictable.

## Constraints

- Maximum 15 total skills (base + forged combined)
- Only forge when developmental gate is met (CALIBRATE+ for utility gaps, EXPLOIT+ for analytical gaps)
- Forged skills are always `user_invocable: false` (internal sub-skills)
- Never forge a skill that duplicates an existing one — before forging, check
  `skill-relations.sh read --similar {candidate_name}` to verify no existing skill
  covers the same capability. If a similar skill exists, strengthen that skill or
  register a compose_with relation instead of forging a new one.
- Always create a test aspiration goal after forging
- Gap registry is append-only (dismissed gaps stay, never deleted)
