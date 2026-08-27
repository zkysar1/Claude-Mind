---
name: forge-skill
description: "Forges a new SKILL.md from a recurring capability gap recorded in meta/skill-gaps.yaml, registers it in world/forged-skills.yaml, creates companion scripts for restricted operations, announces on the message board, and adds a validation goal. Use whenever a gap reaches the forge threshold (times_encountered >= 2, estimated_value >= medium, no duplicate skill exists) and the curriculum permits forging, or the user runs /forge-skill list / skill {gap-id} / check / dismiss {gap-id}. Wraps Anthropic's generic skill-authoring pattern (see anthropics/skills/skill-creator) with this agent's gap-detection, registry, and validation loop."
user-invocable: true
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
minimum_mode: assistant
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
- Registration in `world/forged-skills.yaml` + git-commit of the skill body (fleet distribution)
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

  Corrected 2026-07-25 (g-250-269): these parentheticals previously read
  `>= 0.30` and `>= 0.60`. Both were wrong, and both erred LOW — an agent
  trusting the gloss would forge BELOW the real bar. Caught live: the
  `npc-intelligence` node reads `confidence 0.7429` / `capability_level
  CALIBRATE`. The stale `>= 0.60` gloss says PASS for an analytical gap; the
  real EXPLOIT threshold (0.75) says BLOCK, by 0.0071. There is no automated
  script for THIS gate — this text IS the enforcement — so a wrong number here
  silently authorizes under-qualified forges. (Precision, g-115-3131: a
  DIFFERENT forge gate IS automated — `curriculum-contract-check.sh --action
  allow_forge_skill`, resolved in `core/scripts/curriculum.py`, called from
  aspirations-spark's forge-criteria block. The two are independent: the
  curriculum contract gates whether forging is unlocked AT ALL for the agent's
  stage; the type/confidence gate here decides whether THIS gap clears its bar.
  Only the second is text-only.)

  **Typeless default — decided g-115-3131 (2026-07-25, bravo).** A gap with no
  `type` defaults to `utility` (CALIBRATE), not `analytical` (EXPLOIT).

  Evidence at decision time: 22 of 24 registered gaps carried no `type` at all,
  so the default was not an edge case — it was the operative policy for 92% of
  the corpus, chosen by omission. Classifying all 22 against the two
  `gap_types` descriptions in `core/config/skill-gaps.yaml` (utility =
  "well-defined procedures… retrieval workflows"; analytical = "requiring
  domain understanding… pattern recognition") gave **20 utility / 2 analytical**
  (the analytical two: gap-008 derives win-condition semantics from recordings,
  gap-015 designs pre-registration thresholds). The old default was therefore
  inverted against ~91% of the population it governed.

  Why it went unnoticed for 24 gaps: it errs STRICT, and strict is the safe
  direction. 790 of 1246 capability-bearing tree nodes (63%) already sit at
  EXPLOIT, so the harder bar usually passed anyway — which is also why 9
  typeless gaps were forged without anyone noticing a gate had been applied by
  accident. It only bites a utility-shaped gap in a CALIBRATE category, i.e.
  the narrow 0.50–0.75 band (the live case: `npc-intelligence` at 0.7429).

  Rejected alternatives, with reasons:
  - *"The default is right and the historical forges were under-gated"* —
    REFUTED on its premise. Those forges cleared the STRICTER bar because most
    categories are EXPLOIT; there is no under-gating to backfill away.
  - *"Retire the gate; category confidence is the wrong proxy"* — a real
    critique (a category's maturity does not measure whether the agent
    understands the specific procedure being mechanized), but retiring a gate
    with no replacement trades a narrow false-block for an open door. Filed
    separately rather than acted on here.

  Safety of lowering the default: the flip only matters for a gap that is BOTH
  typeless AND in a CALIBRATE category. All 22 existing gaps were backfilled
  with an explicit `type` in the same change, and the registration site in
  `aspirations-spark` now sets `type` at gap-creation, so a typeless gap should
  be rare going forward. When one does appear, `utility` matches the modal
  shape and the explicit `type: analytical` opts INTO the higher bar.

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
       DOMAIN resources (named services, product APIs, SSH targets, branded
       workflows) go in `world/scripts/` — shared across all agents in the
       domain. The forge process creates the directory:
       `mkdir -p "$WORLD_DIR/scripts/"`. Scripts that are pure FRAMEWORK
       helpers (storage backend, session state, governed-store plumbing —
       domain-free by the `domain-leak-check.sh` test) go in `core/scripts/`
       instead: git-tracked, portable, and they ride the repo's commit flow.
       Precedent: `core/scripts/backend-cat.sh` (probe-governed-store).
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

3.6. **Companion-script dogfood gate** (correctness-critical forges only; g-115-2665):
   Step 3.5 scores the SKILL.md TEXT, not whether the companion script produces
   CORRECT OUTPUT — a script can score `good` on all five dims and still emit the
   wrong verdict. For a gap whose companion script carries correctness-critical
   logic — a **verifier** (emits a pass/fail verdict), a **computation** (derives a
   value other code trusts), or a **state-mutating** op (writes/restores files,
   moves records) — dogfood the script on synthetic fixtures BEFORE registration:

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
     A one-line aggregate ("N distinct verdicts across N fixtures", "pass-rate
     differs", "scores spread") is the tempting cheap guard because it covers every
     fixture at once. But an aggregate summarises ONE axis, and a defect that
     corrupts a DIFFERENT axis leaves it untouched — so it reads green through the
     exact bug it was written to catch. Measured (g-335-439): a
     `4 distinct floors across 4 fixtures` line stayed green through two deliberate
     mutations that each reintroduced a real production bug, because both corrupted
     the at-floor *enumeration* while leaving the *floor* intact; only the
     per-fixture assertions fired. Test: re-run each mutation and check whether the
     AGGREGATE moves. If it does not, the aggregate is not a health check — it is a
     number that happens to be printed, and it must not be the thing you rely on.
     Distinct from the three failure modes below it: not a self-supplied expectation
     (guard-1220), not a wrong input shape (guard-920), not an absent layer
     (guard-1462) — the layer IS covered, just not by the assertion you trusted.
   - **NAME the layers your fixture seam EXCLUDES** (guard-1462). Wherever the
     fixture is injected is a silent scope declaration: everything UPSTREAM of the
     injection point is structurally unfalsifiable by ANY fixture, and a green run
     announces nothing about where that line fell. State the excluded layers
     explicitly in the forge log. The common split is a script that both SELECTS
     records and INTERPRETS them — a seam between the two tests only the
     interpreter, leaving enumeration, filtering, ordering and the limit/cap
     with no coverage at all.
   - **Run it LIVE at least once before registration** when the script's
     correctness depends on reading an EXTERNAL SYSTEM (remote filesystem, API,
     remote store, another host) — one real end-to-end invocation against the
     real source, not a fixture. This ADDS to the fixture requirement, it does not
     replace it: fixtures prove the interpreter discriminates, the live run proves
     the excluded upstream layers work. The thin API-wrapper exemption in SCOPE
     below applies to this bullet exactly as it does to the fixtures.
     BUDGET THE LIVE RUN AS A SUCCESS-PATH AUDIT, not a smoke test that the thing
     runs. For every write the tool performs, read the record back from the store
     and DIFF the stored fields against what was supplied; for every non-zero exit,
     read the script's own contract before calling it a failure. A fixture supplies
     its own expectation, so a call that SUCCEEDS while quietly storing something
     other than what you passed matches that expectation exactly — which is why
     this class is structurally unreachable by fixtures and shows up only here.
     Measured g-115-4466 (2026-08-01): one 3-item live run found three defects,
     ALL on the success path (a silently-rewritten origin_signal, a read-back keyed
     on the value that was never stored, and an rc=3 that means success), and none
     was reachable by any fixture. rb-6343 / guard-2329.

   Why the live run is not redundant with a green fixture suite (g-250-269, the
   incident behind guard-1462): a forge followed Step 3.6 exactly — 7/7 fixtures
   including a VALID two-way vacuity proof whose decisive pair differed in exactly
   one field — and still shipped a real defect, because the fixture substituted the
   payload AFTER enumeration. A non-session directory both consumed a `--limit`
   slot and tripped the guard-1214 positive control, and no fixture could reach
   that layer. One voluntary live run surfaced it in seconds. This is a THIRD
   failure mode, distinct from its neighbours: guard-920 is the right layer with
   the wrong input shape, guard-1220 is the right layer with a self-supplied
   expectation, and this one is a layer that is not in the suite at all — so
   satisfying both of those does NOT protect you here.

   For a verifier / state-mutating script the `mutation-proof-regression-test`
   forged skill (`core/scripts/mutation-proof-test.sh`) IS this harness — invoke it
   on one of the script's guarded targets; for a pure computation script a
   3-fixture inline assertion suffices. SCOPE: verification / computation /
   state-mutating gaps ONLY — a thin API wrapper (shells one documented command,
   no correctness-critical branch) is EXEMPT; note the exemption in the forge log
   and proceed to Step 4.

   JUDGE THE EXEMPTION PER SUBCOMMAND, NEVER PER SCRIPT (g-115-3475, rb-5355).
   A companion script is a BUNDLE of subcommands with heterogeneous risk, so one
   whole-artifact verdict launders the riskiest member through the average — and
   the riskiest member is exactly the one carrying the harness's safety claim.
   Measured while forging launch-env-server-session: 4 of 8 subcommands were
   exempted as thin wrappers, and TWO were ineligible by the classes named right
   above — `verify-terminated` returns exit 1 plus a STILL-BILLING action on a
   non-terminated instance (a pass/fail verdict, i.e. a VERIFIER), and `teardown`
   is state-mutating. Both shipped unvalidated through an exemption neither
   qualified for. Walk the subcommand list and write one verdict per entry;
   a bundle-level "it's a thin wrapper" is not a verdict. (guard-1220, rb-4004, rb-4124 — done manually for gap-019,
   now required by the process.)

4. **Register in Forged Skills** (`world/forged-skills.yaml` + git-commit the body):
   - Add entry under `skills:` with `parent`, `type`, `forged_date`, `forged_by: {agent-name}`, `gap_ref`, `triggers`
   - **AMENDING an EXISTING row (adding a trigger, fixing a `companion_scripts`
     path, appending a `note`): you MUST also set `amended_at` to the current
     naive ISO timestamp** (`date +%Y-%m-%dT%H:%M:%S`). This is not bookkeeping —
     it is what makes the amendment SURVIVE. `merge_forged_skills` resolves a
     same-name conflict WHOLE-RECORD, and an amendment bumps no `forged_date`
     and adds no FIELD, so without the stamp it falls to a `_canon` lexicographic
     tiebreak and can lose DETERMINISTICALLY to an untouched peer copy — every
     write path reporting success while nothing lands. Measured on cc-05
     2026-07-28 (g-115-3506 → g-115-3638): a 4-trigger addition lost 10-to-6 via
     the Edit tool, a plain python write, AND `OwnCloudBackend.write_text`, and
     was byte-identical with the merge arguments swapped, so retrying could never
     win. `amended_at` is tier 0 of `_merge_forged_skill` (guard-1153: LWW on a
     timestamp written BY THE SAME MUTATION that writes the field).
   - **Verify the row landed** — do not assume. `bash core/scripts/backend-cat.sh
     head world/forged-skills.yaml` prints the authoritative size/version plus a
     `[match]` / `[DRIFT ...]` local-mirror verdict, then re-read the row. Two
     registry rows have already been lost this way (`probe-governed-store`,
     `reconcile-fleet-fork` — both carry a `restored:` field recording it), and
     `mirror-health.sh` reports `healthy` throughout, so nothing warns you.
   - **Git-commit the skill body for fleet distribution** (g-115-2373, 2026-07-16):
     `git add .claude/skills/{new-skill-name}/` — the iteration close-commit sweeps
     it to origin, and every fleet box picks it up on its next `iteration-push`
     pull. Rationale: the registry syncs fleet-wide through the governed store and
     advertises triggers on every box, but `.claude/` is NOT an own-cloud governed
     root — a gitignored body existed ONLY on its birth box, so trigger resolution
     dispatched to un-invokable skills on 4/5 boxes (found by g-115-2358 validation).
     Do NOT write a nested `.claude/skills/{name}/.gitignore` and do NOT add a
     ROOT `.gitignore` line — both ignore forms are retired (the g-115-2272
     parallel-forge collision was the shared root-.gitignore FILE; disjoint new
     skill DIRS cannot conflict). Promotion-seed purity is unaffected:
     `_seed_engine.py` auto-derives seed exclusions from the registry (g-306-88),
     so a committed forged body still never leaks into the domain-free seed.
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
permanent: Claude Code loads every skill's name and description into the system
prompt at startup, so each new SKILL.md is standing per-turn weight on every agent
forever, while extension costs zero. The corpus is a pure additive ratchet — forging
adds, nothing subtracts — and `max_skills` is ratchet-down-only by construction
(`modifiable.max_skills` is `{min: 10, max: 100, default: 100}`, so the modifiable
maximum equals the default and the cap cannot be raised). Measured 2026-08-11: **116
skill dirs against a cap of 100**, and 16 of 130 gaps had already been resolved as
`satisfied-by-extension` — the practice was established and load-bearing while this
file mentioned it **zero** times, so the cheaper path existed and was invisible at
exactly the moment it was needed. Enforced-by-visibility only: `/verify-learning`
check `skill-corpus-count-under-cap` counts the directories, and nothing refuses a
forge (see `## Constraints`).

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

If any item is FAIL, the forge is not ready — fix it first or abort and
re-queue the gap with updated notes. Forging a skill that never fires does not
clear the gap; it just moves the problem from the skill-gaps registry to the
skill registry while the actual capability remains missing.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `aspirations-add-goal.sh` (test goal) or
`skill-relations.sh` write. Never end with a text summary of the forge.
