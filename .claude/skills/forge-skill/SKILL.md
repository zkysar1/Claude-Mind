---
name: forge-skill
description: "Forges a new SKILL.md from a recurring capability gap recorded in meta/skill-gaps.yaml, registers it in world/forged-skills.yaml, creates companion scripts for restricted operations, announces on the message board, and adds a validation goal. Use whenever a gap reaches the forge threshold (times_encountered >= 2, estimated_value >= medium, no duplicate skill exists) and the curriculum permits forging, or the user runs /forge-skill list / skill {gap-id} / request {name}: {purpose} / check / dismiss {gap-id}. A user asking in chat or by directive for a new skill IS a request: run the request sub-command, never make them wait for a recurring gap. Wraps Anthropic's generic skill-authoring pattern (see anthropics/skills/skill-creator) with this agent's gap-detection, registry, and validation loop."
user-invocable: true
triggers:
  - "/forge-skill"
parameters:
  - name: sub-command
    description: "skill <gap-id> | request <name>: <purpose> | check | list | dismiss <gap-id>"
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
minimum_mode: assistant
revision_id: "skill-bootstrap-forge-skill-2a3db3"
previous_revision_id: null
---

# /forge-skill — Skill Forge

Meta-skill that creates new skills from recurring capability gaps tracked in
`meta/skill-gaps.yaml`. Forged skill SKILL.md files go in `.claude/skills/` for
Claude Code discovery. Metadata is tracked in `world/forged-skills.yaml` (not `_tree.yaml`).

## How Claude Code discovers skills (read first)

At startup the harness loads each skill's `name`+`description` into the system prompt — ALL
Claude knows until it fires — so the description is the ONLY signal that decides whether a
skill fires; a vague one silently undertriggers (see ## Writing Effective Descriptions).

## Prior art — Anthropic's `skill-creator`

Anthropic publishes a generic skill-authoring skill at
`anthropics/skills/skills/skill-creator/SKILL.md`
(https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md)
and a normative best-practices doc at
https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices.

`/forge-skill` layers THIS agent's domain on top of that generic reference:

- Gap detection via `meta/skill-gaps.yaml` (encounter counts, value estimates)
- Curriculum and developmental gates (CALIBRATE+ / EXPLOIT+ thresholds)
- Companion-script generation for restricted operations (SSH, API scopes)
- Registration in `world/forged-skills.yaml` + git-commit of the skill body (fleet distribution)
- Message-board announcement and aspirations-loop validation goal

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

### `/forge-skill request <name>: <purpose>` — Forge a skill the USER asked for

A user asking for a skill — in chat, in a directive, or in a goal they filed — IS the
justification the recurring-gap gates below exist to approximate. Do NOT make the user
wait for `times_encountered` to climb, and NEVER bump an existing gap's counter to clear
the threshold: a gap's identity is its procedure (guard-3177) and a forced count is a
false record.

1. Bash: `meta-read.sh skill-gaps.yaml` → next id `gap-{max+1}` and `<len>` = current
   number of gaps. Bash: `load-conventions.sh aspirations` if not loaded.
2. Register the gap with the request as its evidence — ONE whole-element append (the
   aspirations-spark shape; never a per-field dotpath on a NEW gap):
   ```
   Bash: meta-set.sh skill-gaps.yaml "gaps[<len>]" '{"id":"gap-NNN","status":"registered","type":"utility","procedure_name":"<name>","description":"<purpose>","estimated_value":"high","times_encountered":<forge_threshold>,"requested_by":"user","encounter_log":[{"date":"<YYYY-MM-DD>","context":"user request: <the user's words, verbatim>"}]}'
   ```
   `times_encountered` is set to `forge_threshold` because the request satisfies the
   threshold by definition; `requested_by: user` records WHY, so no auditor reads it as an
   organic count. Gate the verdict on rc and read it back (the aspirations-spark
   write-integrity block applies verbatim).
3. Continue at `/forge-skill skill gap-NNN` below. For a `requested_by: user` gap the two
   AUTONOMOUS-readiness gates are WAIVED and the waiver is written into the Step 9 report:
   the curriculum contract (`allow_forge_skill`) and the category capability-level gate
   both measure whether the agent may forge ON ITS OWN, and a user request answers that.
   Every other step applies unchanged — overlap check, description check, Step 3.5 quality
   gate, Step 3.6 dogfood, registration, board, test goal. The user asked for a skill, not
   for a bad one.

### `/forge-skill skill <gap-id>` — Create a new skill from a gap

**Forge Criteria** (ALL must be met):
- **User-requested gap** (`requested_by: user`, filed by `/forge-skill request`): the
  curriculum contract and the developmental gate in this list are WAIVED — log
  `gate waived: user request` and continue; every other criterion still applies.
- Curriculum contract: `Bash: curriculum-contract-check.sh --action allow_forge_skill`
  IF exit code 1: ABORT — "Forge blocked by curriculum (stage: {stage_name}). Forging unlocks at: {unlocks_at}."
- `times_encountered >= config.forge_threshold` (currently 2)
- `estimated_value >= medium`
- No existing skill covers the same procedure
- System developmental gate (type-dependent):
  - Read gap `type` from skill-gaps.yaml via meta-read.sh (default: `utility` —
    see "Typeless default" below; was `analytical` until g-115-3131)
  - Read `forge_gate` threshold from `core/config/skill-gaps.yaml` → `gap_types[type]`
  - `utility` gaps require CALIBRATE+ (**confidence >= 0.50**)
  - `analytical` gaps require EXPLOIT+ (**confidence >= 0.75**)
  - Check: capability_level of related category >= forge_gate
  node_json=$(bash core/scripts/tree-read.sh --node <category-key>)
  (extract confidence from node_json, or fall back to `agents/<agent>/developmental-stage.yaml`)

  **The confidence numbers above are NOT free-standing — they mirror
  `core/config/tree.yaml` → `domain_health.competence_mapping`, which is the
  SSOT (`EXPLORE 0.25 / CALIBRATE 0.50 / EXPLOIT 0.75 / MASTER 1.00`; guard-1195
  — capability_level/confidence travel together in `_tree.yaml`). If that
  mapping is retuned, update these two lines with it. Prefer reading the node's
  stored `capability_level` string over re-deriving a level from `confidence`;
  the resolver is `_graduate_from_confidence` in
  `core/scripts/backfill-tree-node-fields.py` (highest threshold whose value the
  confidence meets or exceeds).**

  The `>= 0.30`/`>= 0.60` gloss once here was wrong and erred LOW (g-250-269). Rationale
  (SSOT, the correction, why this gate is text-only): core/config/rationale/forge-skill-gates.md.

  **Typeless default — decided g-115-3131 (2026-07-25, bravo).** A gap with no
  `type` defaults to `utility` (CALIBRATE), not `analytical` (EXPLOIT).
   Rationale (evidence: 20 of 22 typeless gaps classify utility; rejected alternatives;
   safety of lowering): core/config/rationale/forge-skill-gates.md.

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
     - Placement fork (framework/domain split, g-115-1982): scripts that touch
       DOMAIN resources (named services, product APIs, SSH targets) go in `world/scripts/`
       (`mkdir -p "$WORLD_DIR/scripts/"`, shared across agents); pure FRAMEWORK helpers
       (domain-free per `domain-leak-check.sh`) go in `core/scripts/`. Precedent: `backend-cat.sh`.
     - PID files live alongside scripts in `world/scripts/` (single-writer, `kill -0` liveness checks)
     - Mark scripts executable: `chmod +x "$WORLD_DIR/scripts/"*.sh`

3. **Create SKILL.md** — Write new skill file:
   ```
   .claude/skills/{new-skill-name}/SKILL.md
   ```

   PLACEMENT CHECK: if the procedure references domain-specific infrastructure (services,
   product APIs, account IDs, hostnames), route it into a `world/conventions/*.md` file named
   in the skill's `conventions:` front matter — not inline, not a rule file. Keeps it portable.

   Structure:
   - YAML front matter: name, description, triggers (internal only), parameters, tools_used
   - **`forged: true`** — MANDATORY self-identifying tag. Without this, the skill
     is indistinguishable from a framework-essential skill. Pair with `forged_by:{agent}`,
     `forged_date:"{YYYY-MM-DD}"`, `forged_from:{gap-id}` if gap-derived. `/verify-learning`
     Section FST enforces consistency with `world/forged-skills.yaml`.
   - `user-invocable: false` (hyphen per Claude Code spec — underscore is NOT recognized)
   - `tools_used: [Bash, WebFetch, ...]` — which Claude Code tools this skill requires
   - `companion_scripts: [world/scripts/xxx.sh, ...]` — if companion scripts exist
   - `description:` — THE most important field. Follow the ## Writing Effective
     Descriptions section exactly — a vague description that rarely fires is worse than not forging.
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
     Non-optional — `/verify-learning` greps for it; skipping it fails verification and kills
     the loop if the skill runs mid-iteration.

3.1. **Post-write tag check** — IMMEDIATELY after writing the new SKILL.md,
   verify the `forged: true` tag landed (Step 3 is fallible under context pressure; without
   this the forge->detection gap is unbounded — Section FST fires only when `/verify-learning` runs).

   ```
   Bash: grep -E "^forged:\s*true\b" .claude/skills/{new-skill-name}/SKILL.md \
       || { echo "ERROR: forged: true tag missing from new SKILL.md — fix before Step 4" >&2; exit 1; }
   ```

   On failure: re-open the SKILL.md, add the `forged: true` + `forged_by:` +
   `forged_date:` + (if applicable) `forged_from:` block right after `name:`,
   then re-run this check.

3.5. **Tier-1 skill-quality gate** (earn-the-keep Phase 1, gate `eval-harness-forge-accept`):
   Before registration, the new skill must clear the 5-dimension quality bar.
   Score it on `skill-evaluate.sh`'s five dims (safety / completeness / executability /
   maintainability / cost_awareness), each good|average|poor -> 1.0|0.5|0.0. A new forge has no
   'before', so it is gated vs the human-competent baseline (all average=0.5) under
   `strict_improve` (epsilon=0): it must BEAT the mean — ties rejected.

   ```
   Bash: bash core/scripts/skill-edit-gate.sh gate \
       --new-judgments '{"safety":"<good|average|poor>","completeness":"<good|average|poor>","executability":"<good|average|poor>","maintainability":"<good|average|poor>","cost_awareness":"<good|average|poor>"}' \
       --skill-name "{new-skill-name}" --caller "forge-skill:Step3.5"
   # skill-edit-gate.sh wraps core/scripts/skill_edit_gate.py and resolves python3 via
   # _paths.sh; the former `py -3` spelling needs a box-level `py` shim (python-invocation.md).
   # Each value is the FULL WORD good, average or poor -- the gate refuses
   # abbreviations (a Body copied "g" from an earlier <g|a|p> placeholder here
   # and read the refusal as a BLOCK; 2026-08-30).
   # exit 0 = PASS  -> proceed to Step 4 registration.
   # exit 1 = BLOCK -> the gate already logged the verdict to meta/gate-firings.jsonl
   #          (id eval-harness-forge-accept) AND appended the rejected edit to
   #          meta/skill-rejected-edits.jsonl (negative memory). Do NOT register:
   #          STOP, revise the SKILL.md to fix the weakest dim(s), then re-run.
   # exit 2 = MALFORMED CALL, not a verdict (bad JSON or a judgment outside the
   #          three words); nothing was logged. Fix the call and re-run.
   ```
   For a refactor of an EXISTING skill pass `--old-judgments` + `--policy no_regression
   --epsilon 0.02` instead. Every verdict is telemetered to `meta/gate-firings.jsonl`.

3.6. **Companion-script dogfood gate** (correctness-critical forges only; g-115-2665):
   Step 3.5 scores the SKILL.md TEXT, not whether the companion script produces CORRECT
   OUTPUT. For a gap whose companion script is a verifier (emits pass/fail), a computation
   (derives a value other code trusts), or a state-mutating op, dogfood it on synthetic
   fixtures BEFORE registration:

   - Build the smallest PASS fixture, FAIL fixture, and (if the script has an edge
     mode) one EDGE fixture that should each drive a distinct verdict.
   - Run the script on each; assert the emitted verdict/value matches the expected
     one, AND for state-mutating scripts that the side-effect landed and any
     restore left no residue (byte-verify against a backup).
   - A script returning the SAME verdict on the PASS and FAIL fixtures is VACUOUS
     (no discriminating power — mirrors guard-1220's two-way proof + rb-4133); do
     NOT register it. Fix, re-run, register only when PASS→pass / FAIL→fail and
     side-effects verify.
   - **If the suite carries a SUMMARY assertion as its anti-vacuity guard, mutate
     against THAT ASSERTION ALONE — not against the suite as a whole** (guard-1793).
     Rationale (g-335-439; distinct from guard-1220 self-supplied / guard-920 wrong-shape /
     guard-1462 absent-layer): core/config/rationale/forge-skill-gates.md.
   - **NAME the layers your fixture seam EXCLUDES** (guard-1462). Wherever the
     injection point is a silent scope declaration — everything UPSTREAM is unfalsifiable by any
     fixture (the SELECT-vs-INTERPRET split). State excluded layers in the forge log. Rationale:
     core/config/rationale/forge-skill-gates.md.
   - **Run it LIVE at least once before registration** when the script's
     correctness depends on an EXTERNAL SYSTEM (remote fs, API, remote store) — one real
     end-to-end run against the real source. It ADDS to the fixtures (they prove the interpreter
     discriminates; the live run proves the excluded upstream layers). BUDGET IT AS A
     SUCCESS-PATH AUDIT: for every write, read the record back and DIFF stored vs supplied; for
     every non-zero exit, read the contract first — a success that stores the wrong thing is
     unreachable by fixtures (g-115-4466; rb-6343/guard-2329). Rationale (why not redundant, a
     THIRD failure mode, g-250-269): core/config/rationale/forge-skill-gates.md.

   For a verifier / state-mutating script the `mutation-proof-regression-test` forged skill
   (`core/scripts/mutation-proof-test.sh`) IS this harness; a pure computation needs a 3-fixture
   inline assertion. SCOPE: verification / computation / state-mutating gaps ONLY — a thin API
   wrapper (one documented command, no correctness-critical branch) is EXEMPT (note it in the log).

   JUDGE THE EXEMPTION PER SUBCOMMAND, NEVER PER SCRIPT (g-115-3475, rb-5355).
   A companion script BUNDLES subcommands of heterogeneous risk; a whole-artifact verdict
   launders the riskiest through the average. Walk the subcommand list, one verdict per entry
   (guard-1220, rb-4004, rb-4124). Rationale (launch-env-server-session): core/config/rationale/forge-skill-gates.md.

4. **Register in Forged Skills** (`world/forged-skills.yaml` + git-commit the body):
   - **BODY GATE FIRST — the registry row may not be written until this exits 0**
     (g-115-9043; the gate, not this sentence, is what enforces it — guard-399):
     ```bash
     bash core/scripts/forged-skill-body-gate.sh --skill {new-skill-name}
     ```
     rc=0 → proceed. rc=1 → **STOP**: do not add the row, do not post to the
     board, do not report the forge done. Write the body at
     `.claude/skills/{new-skill-name}/SKILL.md` and re-run. rc=2 → you passed no
     skill name; a usage error is NOT approval.
     WHY: a registry row over an absent body is a PHANTOM registration (guard-2242, rb-10227);
     the gate checks DISK PRESENCE at the load path, not catalog listing (guard-2335). Rationale:
     core/config/rationale/forge-skill-gates.md.
   - Add entry under `skills:` with `parent`, `type`, `forged_date`, `forged_by: {agent-name}`, `gap_ref`, `triggers`
   - **AMENDING an EXISTING row (adding a trigger, fixing a `companion_scripts`
     path, appending a `note`): you MUST also set `amended_at` to the current
     naive ISO timestamp** (`date +%Y-%m-%dT%H:%M:%S`). This is not bookkeeping —
     it is what makes the amendment SURVIVE: `merge_forged_skills` resolves same-name conflicts
     WHOLE-RECORD, and an amendment (no new field, no bumped date) can lose to an untouched peer
     via a `_canon` tiebreak. `amended_at` is tier 0 of `_merge_forged_skill` (guard-1153).
     Rationale (the cc-05 10-to-6 loss): core/config/rationale/forge-skill-gates.md.
   - **Verify the row landed** — do not assume. `bash core/scripts/backend-cat.sh
     head world/forged-skills.yaml` prints the authoritative size/version plus a
     `[match]`/`[DRIFT]` verdict, then re-read the row. Two rows were lost this way while
     `mirror-health.sh` read `healthy` throughout.
   - **Git-commit the skill body for fleet distribution** (g-115-2373, 2026-07-16):
     `git add .claude/skills/{new-skill-name}/` — the iteration close-commit sweeps
     it to origin; every fleet box pulls it on its next `iteration-push`. `.claude/` is NOT an
     own-cloud governed root, so a gitignored body strands on its birth box (g-115-2373). Do NOT
     write a nested `.gitignore` and do NOT add a ROOT `.gitignore` line — both ignore forms are
     retired; disjoint new skill DIRS cannot conflict (g-115-2272); seed purity is auto-derived
     (g-306-88). Rationale: core/config/rationale/forge-skill-gates.md.
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
   (Check world/forged-skills.yaml for a skill whose triggers match "notify the user" and
   invoke it — subject "New Skill Forged: {skill-name}", message naming the skill/type/parent/
   location + "a validation goal will test it over 3 invocations". If none is registered, fall
   back to a `participants:[agent,user]` goal via aspirations-add-goal.sh. Never block on notify failure.)
   - IF notification fails: continue (best-effort)

8. **Create Test Goal** — ONE call, the goal JSON on stdin. Never rewrite the whole
   aspiration to add one goal (`aspirations-add-goal.sh` is this skill's terminal action).
   - Find the relevant aspiration: Bash: `load-aspirations-compact.sh` → IF path returned:
     Read it (IDs, titles, categories) and pick the aspiration whose category the skill serves.
   - Bash:
     ```
     echo '{"title":"Validate forged skill: {skill-name}","priority":"MEDIUM","participants":["agent"],"category":"{category}","description":"Invoke {skill-name} 3 times via its parent skill; each run succeeds and the skill fires from its description alone.","origin_signal":"idea:forge-skill-{skill-name}"}' \
       | bash core/scripts/aspirations-add-goal.sh <asp-id>
     ```
   - (User notification already sent in Step 7 — do not send a second notification here.)

9. **Report** — Summarize what was created, where it lives, and what triggers it.
   - If companion scripts were created: list them with their purpose and usage

### `/forge-skill check` — Audit both trees for coherence

Run structural integrity checks across all system registries:

1. **Forged skills audit** (`world/forged-skills.yaml`):
   - Every entry has a matching SKILL.md in `.claude/skills/{name}/` — a missing
     dir on THIS box means the birth box has not yet committed the body
     (pre-g-115-2373 forge) or the pull hasn't landed; check `git log --all --
     .claude/skills/{name}/` before concluding the body is lost fleet-wide.
   - Every LOCALLY-PRESENT entry is git-TRACKED (`git ls-files
     .claude/skills/{name}/` non-empty) and NOT ignored (`git check-ignore
     .claude/skills/{name}/SKILL.md` exits 1). Forged bodies ride the fleet git
     channel as of 2026-07-16 (g-115-2373); both ignore forms (nested
     per-skill `.gitignore`, root-`.gitignore` lines) are retired. The
     regression this audit catches is a present-but-ignored or
     present-but-untracked forge — invisible to the fleet, birth-box-only.
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

Normative excerpts from Anthropic's best-practices doc
(https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices); the
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

### Be "pushy" — counter the undertrigger bias

Claude tends to undertrigger skills. Use assertive phrasing so borderline
cases still fire:

- "Use whenever the user says ..." (preferred)
- "Fires when ..." (for event-driven skills)
- "MUST use this skill — never raw X ..." (for canonical-path requirements)

### Good / bad examples

Good (pushy, third-person, what + explicit triggers):

```yaml
description: "Generates a commit message by analyzing the staged git diff.
  Use whenever the user asks for help writing a commit message, says 'commit
  this', or the agent has staged changes ready to commit but no message yet.
  Always prefer this skill over asking the user to write the message by hand."
```

Bad: `"I can help you generate commit messages"` (1st person), `"Helps with documents"`
(vague), `"Commit message generator for git diffs"` (3rd person, no triggers).

### Trigger phrases to include verbatim

When the skill addresses a known user-phrasing pattern, quote the user's literal words in the
description — Claude matches surface form as well as semantics (e.g. `notify-user` lists
"notify the user", "alert the user", "email the user").

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

When forging from a gap, include the gap's `encounter_log` user phrases verbatim.

## Forge Naming Convention

New skill names follow kebab-case and describe the action:
- `check-{domain}-{data}` (e.g., check-stock-prices, check-weather-forecast)
- `fetch-{source}-{type}` (e.g., fetch-api-scores, fetch-news-sentiment)

Pattern: `{verb}-{domain}-{noun}` — keeps names scannable and predictable.

## Constraints

- Maximum 100 total skills (base + forged combined)
- Only forge when developmental gate is met (CALIBRATE+ for utility gaps, EXPLOIT+ for analytical gaps)
- Forged skills are always `user-invocable: false` (internal sub-skills — hyphen per Claude Code spec)
- Never forge a skill that duplicates an existing one. Do the overlap check BY
  HAND. Per guard-4841 and guard-2119, do NOT reach for
  `skill-relations.sh read --similar {candidate_name}` here: it returns `[]` for
  EVERY input (guard-4841) — ZERO signal, not evidence of no overlap; even a working matcher
  wouldn't suffice (overlapping procedures share no name tokens; guard-2119). Instead grep the
  PROCEDURE'S OWN vocabulary across `.claude/skills/` and `core/scripts/`, grep `forged_from`
  across `.claude/skills/*/SKILL.md`, read the nearest neighbours' front matter. If a similar
  skill exists, strengthen it or register a `compose_with` relation. Rationale: core/config/rationale/forge-skill-gates.md.
- Always create a test aspiration goal after forging
- Gap registry is append-only (dismissed gaps stay, never deleted)

## Pre-Forge Checklist (run before committing to a forge)

Before executing the Forge Process, run this quality gate. If any item is FAIL, iterate — a
rushed forge becomes a zombie skill that undertriggers forever.

### Extension before forge — DO THIS FIRST (decided g-115-5533, 2026-08-11)

- [ ] Named the closest EXISTING skill and stated why it cannot absorb this gap.
      "None is close" is a valid answer; not having looked is not.
- [ ] Re-read the GAP RECORD itself, not a goal's paraphrase of it. gap-074 carried
      an "evaluate satisfied-by-extension first" instruction that the forge GOAL's
      description had dropped — executing from the goal text alone would have forged
      a skill the gap record said not to.
- [ ] If extension wins: add the capability to the existing skill (a companion script
      and an amended registry row is the usual shape), set the gap's status to
      `satisfied-by-extension`, and do NOT create a SKILL.md.

**Why this is a gate and not a suggestion.** A forge is not free and its cost is
permanent: Claude Code loads every description at startup, so each SKILL.md is standing
per-turn weight forever, while extension costs zero; the corpus is an additive ratchet and
`max_skills` is ratchet-down-only. Enforced-by-visibility only (`/verify-learning`
`skill-corpus-count-under-cap` counts dirs; nothing refuses a forge). Rationale (the measured
116-vs-100 overrun): core/config/rationale/forge-skill-gates.md.

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
- [ ] Skill body staged for the fleet: `git add .claude/skills/{new-skill-name}/` ran clean, `git check-ignore .claude/skills/{new-skill-name}/SKILL.md` exits 1, and NO nested `.gitignore` was written (ignore forms retired — bodies are git-distributed; g-115-2373)

If any item is FAIL, fix it or abort and re-queue the gap. Forging a skill that never fires
does not clear the gap; it just moves the problem to the skill registry.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `aspirations-add-goal.sh` (test goal) or
`skill-relations.sh` write. Never end with a text summary of the forge.

**Before reporting the forge done, re-run the body gate** (g-115-9043). Step 4
gated the registry write; this re-run gates the CLAIM OF COMPLETION, and they
are not the same moment — the coach incident ended with a model announcing
success over an empty skill directory, so "I registered it" and "a body exists"
must be established separately:

```bash
bash core/scripts/forged-skill-body-gate.sh --skill {new-skill-name}
```

rc≠0 → the forge is NOT done. Say so plainly, leave the validation goal open,
and do not post a "forge-skill,complete" board message. Note what this can and
cannot prove: it confirms a loadable body, NOT that the skill will trigger — descriptions load
at STARTUP, so trigger behaviour is only testable in a session started AFTER the forge commit
(guard-2335). Never revise a description because the new skill did not fire in its own session.
