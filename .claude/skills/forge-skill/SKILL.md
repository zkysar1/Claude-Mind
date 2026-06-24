---
name: forge-skill
description: "Forges a new SKILL.md from a recurring capability gap recorded in meta/skill-gaps.yaml, registers it in world/forged-skills.yaml, creates companion scripts for restricted operations, announces on the message board, and adds a validation goal. Use whenever a gap reaches the forge threshold (times_encountered >= 2, estimated_value >= medium, no duplicate skill exists) and the curriculum permits forging, or the user runs /forge-skill list / skill {gap-id} / check / dismiss {gap-id}. Wraps Anthropic's generic skill-authoring pattern (see anthropics/skills/skill-creator) with this agent's gap-detection, registry, and validation loop."
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
revision_id: "skill-bootstrap-forge-skill-2a3db3"
previous_revision_id: null
---

# /forge-skill — Skill Forge

Meta-skill that creates new skills from recurring capability gaps tracked in
`meta/skill-gaps.yaml`. Forged skill SKILL.md files go in `.claude/skills/` for
Claude Code discovery. Metadata is tracked in `world/forged-skills.yaml` (not `_tree.yaml`).

## How Claude Code discovers skills (read first)

At Claude Code startup, the harness scans `.claude/skills/*/SKILL.md` and loads
each skill's `name` + `description` fields from the YAML front matter into the
system prompt. That list is ALL Claude knows about the skill until it is invoked.
Everything else — the body, the companion scripts, the conventions — is loaded
only when the skill is actually used. So the description is the ONLY signal
Claude uses to decide whether to fire a skill in response to a task.

A poorly worded description means the skill silently undertriggers and the
capability gap it was forged to fill re-opens. This is the single highest-leverage
field in the whole file — treat it that way.

## Prior art — Anthropic's `skill-creator`

Anthropic publishes a generic skill-authoring skill at
`anthropics/skills/skills/skill-creator/SKILL.md`
(https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md)
and a normative best-practices doc at
https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices.

Use those as the default reference for generic skill-authoring mechanics
(frontmatter schema, progressive-disclosure patterns, script packaging,
evaluations). `/forge-skill` layers THIS agent's domain on top:

- Gap detection via `meta/skill-gaps.yaml` (encounter counts, value estimates)
- Curriculum and developmental gates (CALIBRATE+ / EXPLOIT+ thresholds)
- Companion-script generation for restricted operations (SSH, API scopes)
- Registration in `world/forged-skills.yaml` + `.gitignore`
- Message-board announcement and aspirations-loop validation goal

If you're not sure how to write the body of a skill, read skill-creator first,
then come back here for the integration requirements.

## Sub-commands

### Step 0: Load Conventions

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

### `/forge-skill list` — Show gaps and forged skills

1. Bash: meta-read.sh skill-gaps.yaml
2. Display table of all gaps:
   | ID | Name | Encounters | Value | Status | Forge-eligible? |
3. Bash: world-cat.sh forged-skills.yaml  # list forged skills
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
  - Read gap `type` from skill-gaps.yaml via meta-read.sh (default: `analytical`)
  - Read `forge_gate` threshold from `core/config/skill-gaps.yaml` → `gap_types[type]`
  - `utility` gaps require CALIBRATE+ (confidence >= 0.30)
  - `analytical` gaps require EXPLOIT+ (confidence >= 0.60)
  - Check: capability_level of related category >= forge_gate
  node_json=$(bash core/scripts/tree-read.sh --node <category-key>)
  (extract confidence from node_json, or fall back to `agents/<agent>/developmental-stage.yaml`)

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
     shell scripts in `world/scripts/` (resolved as `$WORLD_DIR/scripts/`):
     - Scripts enforce access boundaries the LLM cannot bypass (e.g., read-only
       SSH commands, download-only SCP, specific API scopes)
     - Scripts use `core/scripts/env-read.sh` for all credentials — no hardcoded secrets
     - Scripts consume credentials in the same shell invocation (variable, not disk)
     - The forged SKILL.md MUST reference companion scripts for restricted
       operations and MUST say "MUST use companion scripts, never raw [tool]"
     - Script naming: `{resource}-{verb}.sh` (e.g., `data-list.sh`, `data-download.sh`)
     - Scripts go in `world/scripts/` — shared across all agents in the domain.
       The forge process creates the directory: `mkdir -p "$WORLD_DIR/scripts/"`
     - PID files live alongside scripts in `world/scripts/` (single-writer, `kill -0` liveness checks)
     - Mark scripts executable: `chmod +x "$WORLD_DIR/scripts/"*.sh`

3. **Create SKILL.md** — Write new skill file:
   ```
   .claude/skills/{new-skill-name}/SKILL.md
   ```

   PLACEMENT CHECK (where domain knowledge goes): If this forged skill's
   procedure references domain-specific infrastructure (named services,
   product-specific APIs, branded workflows, account IDs, hostnames), route
   the domain knowledge into a `world/conventions/*.md` file referenced
   from the skill's `conventions:` front-matter list — NOT into inline
   pseudocode and NOT into a `.claude/rules/*.md` file. The SKILL.md body
   stays domain-agnostic; the domain particulars live in the convention.
   This keeps the forged skill portable when transplanted, while preserving
   the domain wiring at the deployment that needs it.

   Structure:
   - YAML front matter: name, description, triggers (internal only), parameters, tools_used
   - **`forged: true`** — MANDATORY self-identifying tag. Without this, the skill
     is indistinguishable in-file from a framework-essential skill, and a packaging
     pass cannot tell what to keep vs. what to strip. Pair with `forged_by:
     {agent-name}`, `forged_date: "{YYYY-MM-DD}"`, and `forged_from: {gap-id}` if
     gap-derived. Pattern matches `.claude/skills/<forged-skill-name>/SKILL.md`.
     `/verify-learning` Section FST enforces bidirectional consistency with
     `world/forged-skills.yaml`.
   - `user-invocable: false` (hyphen per Claude Code spec — underscore is NOT recognized)
   - `tools_used: [Bash, WebFetch, ...]` — which Claude Code tools this skill requires
   - `companion_scripts: [world/scripts/xxx.sh, ...]` — if companion scripts exist
   - `description:` — THE most important field. Follow the ## Writing Effective
     Descriptions section below exactly. A forged skill that rarely fires because
     its description is too vague is worse than not forging at all — the gap
     encounter count keeps climbing and nothing resolves it.
   - If companion scripts exist: add "## Restricted Operations" section mandating
     their use. Example: "MUST use `world/scripts/data-list.sh`, never raw access"
   - Step-by-step procedure extracted from encounters
   - Input/output contract with parent skill
   - Error handling section
   - **MANDATORY `## Return Protocol` section** (appended at end, canonical form):
     ```
     ## Return Protocol

     See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
     The terminal action is {describe the last expected tool call for this skill}.
     Never end with a text summary.
     ```
     This section is non-optional for forged skills — `/verify-learning` enforces it
     via a dynamic grep. Skipping it will fail verification and kill the autonomous
     loop if the skill is ever invoked mid-iteration.

3.1. **Post-write tag check** — IMMEDIATELY after writing the new SKILL.md,
   verify the `forged: true` tag actually landed in the file. The LLM
   pseudocode in Step 3 is fallible — context-window pressure or instruction
   drift can cause the tag to be omitted. Without this check, the gap
   between forge and detection is unbounded (Section FST only fires when
   `/verify-learning` runs, which can be days later).

   ```
   Bash: grep -E "^forged:\s*true\b" .claude/skills/{new-skill-name}/SKILL.md \
       || { echo "ERROR: forged: true tag missing from new SKILL.md — fix before Step 4" >&2; exit 1; }
   ```

   On failure: re-open the SKILL.md, add the `forged: true` + `forged_by:` +
   `forged_date:` + (if applicable) `forged_from:` block right after `name:`,
   then re-run this check.

3.5. **Tier-1 skill-quality gate** (earn-the-keep Phase 1, gate `eval-harness-forge-accept`):
   Before registration, the new skill must clear the 5-dimension quality bar.
   Score the candidate SKILL.md on `skill-evaluate.sh`'s five dims —
   safety / completeness / executability / maintainability / cost_awareness —
   judging each `good | average | poor` (the same rubric `skill-evaluate.sh score`
   uses; map good=1.0, average=0.5, poor=0.0). A brand-new forge has no "before",
   so it is gated against the human-competent baseline (all dims `average` = 0.5)
   under `strict_improve` (epsilon=0.0): the candidate must BEAT the baseline on
   the weighted mean — ties are rejected.

   ```
   Bash: py -3 core/scripts/skill_edit_gate.py gate \
       --new-judgments '{"safety":"<g|a|p>","completeness":"<g|a|p>","executability":"<g|a|p>","maintainability":"<g|a|p>","cost_awareness":"<g|a|p>"}' \
       --skill-name "{new-skill-name}" --caller "forge-skill:Step3.5"
   # exit 0 = PASS  -> proceed to Step 4 registration.
   # exit 1 = BLOCK -> the gate already logged the verdict to meta/gate-firings.jsonl
   #          (id eval-harness-forge-accept) AND appended the rejected edit to
   #          meta/skill-rejected-edits.jsonl (negative memory). Do NOT register:
   #          STOP, revise the SKILL.md to fix the weakest dim(s), then re-run.
   ```
   For a refactor/edit of an EXISTING skill (not a new forge), pass
   `--old-judgments` with the pre-edit scores and `--policy no_regression
   --epsilon 0.02` instead (the edit must not regress). The gate is registered
   in `core/config/gates.yaml` (`eval-harness-forge-accept`); every verdict is
   telemetered to `meta/gate-firings.jsonl` via `_gate_log`.

4. **Register in Forged Skills** (`world/forged-skills.yaml` + `.gitignore`):
   - Add entry under `skills:` with `parent`, `type`, `forged_date`, `forged_by: {agent-name}`, `gap_ref`, `triggers`
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

7. **Notify the user** about the newly forged skill.
   (Check world/forged-skills.yaml for a skill whose triggers match
   "notify the user" and invoke it with:
     subject: "New Skill Forged: {skill-name}"
     message: |
       A new skill has been forged from capability gap {gap-id}.

       Skill: {skill-name}
       Type: {type}
       Parent skill: {parent-skill}
       Location: .claude/skills/{skill-name}/SKILL.md
       {IF companion_scripts: "Companion scripts: {list of script paths}"}

       A validation goal will be created to test this skill over 3 invocations.
   If no matching skill is registered, fall back to a `participants: [agent, user]`
   goal via aspirations-add-goal.sh or a pending-questions entry. Never block
   skill forging on notification failure.)
   - IF notification fails: continue (best-effort)

8. **Create Test Goal** — Add a goal to the relevant aspiration:
   - Find relevant aspiration: Bash: `load-aspirations-compact.sh` → IF path returned: Read it
     (compact data has IDs, titles, categories — no descriptions/verification)
   - Read the target aspiration: Bash: `aspirations-read.sh --id <asp-id>`
   - Add goal with subject: "Validate forged skill: {skill-name}"
   - Type: calibration
   - desiredEndState: "Skill invoked 3 times successfully by parent"
   - Priority: MEDIUM
   - origin_signal: `"idea:forge-skill-{skill-name}"` (the forge event is the spawn cause)
   - Pipe updated aspiration JSON: `echo '<aspiration-json>' | bash core/scripts/aspirations-update.sh <asp-id>`
   - (User notification already sent in Step 7 — do not send a second notification here.)

9. **Report** — Summarize what was created, where it lives, and what triggers it.
   - If companion scripts were created: list them with their purpose and usage

### `/forge-skill check` — Audit both trees for coherence

Run structural integrity checks across all system registries:

1. **Forged skills audit** (`world/forged-skills.yaml`):
   - Every entry has a matching SKILL.md in `.claude/skills/{name}/`
   - Every entry has a matching `.claude/skills/{name}/` line in `.gitignore`
   - Every entry has a `forged_by` field
   - No orphaned `.claude/skills/` directories missing from the registry

2. **Skill gaps audit** (`meta/skill-gaps.yaml`):
   - Gaps with `status: forged` have matching entry in `world/forged-skills.yaml`
   - No gaps exceed `config.max_gaps` (20)
   - Encounter logs respect `config.encounter_log_limit` (5)

3. **Memory tree cross-check** (`world/knowledge/tree/_tree.yaml`):
   - Forged skills map to categories at EXPLOIT+ capability level

4. **Report** — List all findings: OK checks, warnings, and errors.

### `/forge-skill dismiss <gap-id>` — Reject a gap

1. Bash: meta-read.sh skill-gaps.yaml
2. Set gap `status: dismissed`
3. Set `dismissed_reason: "manual dismiss via /forge-skill dismiss"`
4. Set `dismissed_date: {today}`
5. The gap remains in the registry (never delete) but is excluded from forge eligibility

## Writing Effective Descriptions (MANDATORY)

Source of truth: Anthropic's skill-authoring best practices
(https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices).
The rules below are the normative excerpts every forged skill MUST satisfy. The
`pre-forge-description-check` step of Step 3 validates against this list.

### Hard constraints (from Anthropic spec)

- **Non-empty, max 1024 characters.** The harness rejects longer descriptions.
- **Front-load the key use case** — each frontmatter entry shares a 1,536-char
  budget across fields. Put what+when in the first sentence.
- **No XML tags** in the description string.
- **Third person only.** The description is injected into the system prompt.
  POV drift breaks discovery.

### What to include

Every description MUST contain BOTH:

1. **What the skill does** — a concrete action verb + object, in third person.
2. **When to use it** — explicit triggers: user phrases, internal conditions,
   upstream-skill events, file patterns, state transitions.

Both belong in the description field, NOT the body. Claude decides whether to
fire a skill from this string alone; the body is read only AFTER it fires.

### Be "pushy" — counter the undertrigger bias

Claude tends to undertrigger skills. Use assertive phrasing so borderline
cases still fire:

- "Use whenever the user says ..." (preferred)
- "Fires when ..." (for event-driven skills)
- "MUST use this skill — never raw X ..." (for canonical-path requirements)

Avoid passive/tentative phrases like "can be used to", "might help with", "is
for". They read as optional and the skill silently undertriggers.

### Good / bad examples

Good (pushy, third-person, what + explicit triggers):

```yaml
description: "Generates a commit message by analyzing the staged git diff.
  Use whenever the user asks for help writing a commit message, says 'commit
  this', or the agent has staged changes ready to commit but no message yet.
  Always prefer this skill over asking the user to write the message by hand."
```

Bad (first/second person, vague, no triggers):

```yaml
description: "I can help you generate commit messages"
description: "You can use this to work with PDFs"
description: "Helps with documents"
description: "Does stuff with files"
```

Bad (third-person but still no triggers):

```yaml
description: "Commit message generator for git diffs"
```

### Trigger phrases to include verbatim

When the skill addresses a known user-phrasing pattern, quote the user's
literal words inside the description. Claude pattern-matches on surface form
as well as semantics — literal quoted phrases fire more reliably than
paraphrases. Example from `notify-user`: the description lists "notify the
user", "reach out to the user", "alert the user", "inform the user", and
"email the user" because base skills use any of those phrasings.

### Naming (lightweight)

- `name` max 64 chars, lowercase + digits + hyphens only.
- Anthropic prefers **gerund form** (`processing-pdfs`) for new skills, but
  action-oriented (`process-pdfs`) and noun-phrase (`pdf-processing`) are
  acceptable. This agent's existing convention is `{verb}-{domain}-{noun}`
  (see Forge Naming Convention below); follow it for consistency unless the
  skill obviously maps to the gerund form.
- Reserved words forbidden: `anthropic`, `claude`.

### Pre-forge description check (enforced in Step 3)

Before writing SKILL.md, validate the candidate description against this
checklist. If any line is FAIL, rewrite the description before proceeding:

- [ ] Length: non-empty, ≤ 1024 chars
- [ ] POV: third person throughout (no "I", "you", "we")
- [ ] Contains a concrete action verb describing what the skill does
- [ ] Contains explicit triggers — user phrases, events, or conditions
- [ ] Uses pushy/assertive phrasing ("Use whenever", "MUST use", "Fires when")
- [ ] No XML-tag-shaped placeholders — use `{foo}` instead of `<foo>`. The
      description is injected verbatim into Claude's system prompt, and `<word>`
      is parsed as an unclosed XML block that corrupts downstream context.
      Math operators (`>=`, `<=`) are fine. **DO NOT WEAKEN THIS RULE** — it
      is the single highest-priority constraint on the description field.
- [ ] Front-loaded: the first sentence alone is sufficient for Claude to
      decide whether to fire the skill
- [ ] Includes the user's literal phrasing where known (verbatim quotes)
- [ ] Does NOT list implementation steps (those belong in the body)

When forging from a gap, the gap's `encounter_log` contains the exact user
phrases that led to the gap being recorded — include those verbatim in the
description.

## Forge Naming Convention

New skill names follow kebab-case and describe the action:
- `check-{domain}-{data}` (e.g., check-stock-prices, check-weather-forecast)
- `fetch-{source}-{type}` (e.g., fetch-api-scores, fetch-news-sentiment)

Pattern: `{verb}-{domain}-{noun}` — keeps names scannable and predictable.

## Constraints

- Maximum 100 total skills (base + forged combined)
- Only forge when developmental gate is met (CALIBRATE+ for utility gaps, EXPLOIT+ for analytical gaps)
- Forged skills are always `user-invocable: false` (internal sub-skills — hyphen per Claude Code spec)
- Never forge a skill that duplicates an existing one — before forging, check
  `skill-relations.sh read --similar {candidate_name}` to verify no existing skill
  covers the same capability. If a similar skill exists, strengthen that skill or
  register a compose_with relation instead of forging a new one.
- Always create a test aspiration goal after forging
- Gap registry is append-only (dismissed gaps stay, never deleted)

## Pre-Forge Checklist (run before committing to a forge)

Before executing the Forge Process above, run through this quality gate. If
any item is FAIL, iterate on the candidate skill before proceeding. A rushed
forge becomes a zombie skill that undertriggers forever.

### Discovery signal
- [ ] Description passes all items in "Writing Effective Descriptions" → Pre-forge description check
- [ ] Name follows the `{verb}-{domain}-{noun}` convention and is not a reserved word
- [ ] At least one verbatim user phrase from the gap's encounter_log appears in the description
- [ ] An independent reader of ONLY the description can predict when the skill will fire

### Body quality
- [ ] Body is ≤ 500 lines (Anthropic guidance; split into reference files if longer)
- [ ] Every action is described with imperative language, not suggestions
- [ ] Reference files (if any) are one level deep from SKILL.md, not nested
- [ ] No time-sensitive info (no "before August", "after the 2026 migration")
- [ ] Contains a `## Return Protocol` section (non-optional — see `.claude/rules/return-protocol.md`)

### Domain integration
- [ ] If the skill touches restricted resources (SSH, API, remote storage), a companion
      script under `world/scripts/` enforces the access boundary — SKILL.md
      mandates its use with "MUST use ... never raw ..."
- [ ] Credentials resolved via `core/scripts/env-read.sh` — none hardcoded
- [ ] `conventions:` front matter lists every convention the body references
- [ ] `companion_scripts:` front matter lists every script the skill shells to
- [ ] `minimum_mode` reflects the write surface (reader / assistant / autonomous)

### Validation
- [ ] A test goal is queued to exercise the skill 3 times before it is trusted
- [ ] Entry added to `world/forged-skills.yaml` with `forged_by`, `gap_ref`, `triggers`
- [ ] `.claude/skills/{new-skill-name}/` added to `.gitignore` under forged skills

If any item is FAIL, the forge is not ready — fix it first or abort and
re-queue the gap with updated notes. Forging a skill that never fires does not
clear the gap; it just moves the problem from the skill-gaps registry to the
skill registry while the actual capability remains missing.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `aspirations-add-goal.sh` (test goal) or
`skill-relations.sh` write. Never end with a text summary of the forge.
