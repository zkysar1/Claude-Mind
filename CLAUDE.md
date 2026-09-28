# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Domain-agnostic continual learning base agent. Forms hypotheses, tracks outcomes, builds memory, and self-evolves reasoning. Reusable foundation for any domain needing autonomous learning, reflection, and improvement.

## Architecture

**Claude-native data repository** — no source code or build tools. State lives in YAML, JSONL, and Markdown files that Claude reads, reasons over, and updates autonomously.

### Framework vs State Split (4-Tier Architecture)

- **`core/`** — Immutable framework: config definitions (`core/config/`, including `conventions/` reference files), scripts (`core/scripts/` — all JSONL stores accessed exclusively via these).
- **`meta/`** — Agent-editable meta-strategies, domain-agnostic. External path via `agents/<agent>/local-paths.conf`.
- **`world/`** — Collective domain state shared across agents. External path via `local-paths.conf`. Contains knowledge tree, aspirations, pipeline, reasoning bank, guardrails, board, conventions, `program.md`.
- **`agents/<agent>/`** — Per-agent private state: session, journal, experience, `self.md`, curriculum. Parent dir configurable via `AGENTS_PARENT_DIR` (see Agent-dir Resolution).

**External paths**: `world/` and `meta/` paths configured per-agent in `agents/<agent>/local-paths.conf` (gitignored). See `core/config/conventions/external-paths.md`.

Full project structure tree, Core Systems file-location table: `core/config/architecture-reference.md`.

### Core Design Principle: No Terminal State

Perpetual loop. Completion seeds the next cycle. `/aspirations loop` is the heartbeat — it never exits. *(Full rules in `core/config/modes/autonomous.md`)*

### Core Design Principle: Consolidate Before Expand

Depth over breadth. An aspiration 90% complete pulls harder than a new one. New directions require >25% average completion or explicit justification.
*(Full rules in `.claude/rules/consolidate-before-expand.md`)*

### Mode System

Three operational modes. Mode is the single user-facing control — state and persona derived automatically.

| Mode | State | Persona | Capabilities |
|------|-------|---------|-------------|
| `reader` (safe floor) | IDLE | ON (light) | Read knowledge, prime, answer questions. No writes. Opt-in via `/stop <agent-name> --reader`. |
| `assistant` (post-stop default) | IDLE | ON (full) | Reader + write to tree, remember things, research when asked, accept directives. No loop. |
| `autonomous` | RUNNING | ON (full) | Everything. Self-directed perpetual learning loop. |

Agent name REQUIRED on `/stop` — bare `/stop` is refused. Disk default (absence of `agent-mode`) is reader.
Mode rules: `core/config/modes/{mode}.md` (loaded on demand). Signal file: `agents/<agent>/session/agent-mode`.
Scripts: `session-mode-get.sh`, `session-mode-set.sh` (only /start and /stop may write).

### Cognitive Primitives

Four goal types the agent can create anytime via `aspirations-add-goal.sh`:
- **Unblock** (`"Unblock: ..."`, HIGH) — created by CREATE_BLOCKER protocol when a problem can't be fixed inline
- **Investigate** (`"Investigate: ..."`, MEDIUM) — diagnostic, something seems off
- **Idea** (`"Idea: ..."`, MEDIUM) — creative insight, improvement opportunity
- **Maintain** (`"Maintain: ..."`, MEDIUM) — in-flight framework correction the agent JUST performed inline; filed with `status: completed` so the standard encoding pipeline fires

Not mutually exclusive. A single event can spawn all four. See `aspirations-execute/SKILL.md` Cognitive Primitives section.

## Agent-dir Resolution

**Rule**: Never write `PROJECT_ROOT / agent_name` or `$PROJECT_ROOT/$AGENT` directly. Use `agent_dir(name)` or `$(agent_dir "$AGENT")` instead. For per-session paths use `agent_session_dir(name, sid)`.

Helper API (`_paths.sh` / `_paths.py`): `agents_root()`, `agent_dir(name)`, `agent_sessions_root(name)`, `agent_session_dir(name, sid)`, `agent_state_dir(name)`, `enumerate_agent_confs()`, `is_under_agent_dir(p)`.

**Read `core/config/conventions/agent-dir-resolution.md` BEFORE changing `AGENTS_PARENT_DIR`/`SESSIONS_DIRNAME`/`SESSION_DIRNAME` or adding any glob that sweeps agent dirs** — sync-site tables, cross-agent glob consumer table, audit greps.

## Session Binding (Phase 2.6)

Binding lives at `agents/<name>/sessions/<SID>/binding.yaml` (legacy `.active-agent-<SID>` is migration fallback). Resolver: `session-binding-read.sh <SID>`. Writer: `session-binding-write.sh` (called by /start with `--retire-legacy`).

Two-tier layout: `agents/<name>/sessions/<SID>/` (per-session scratch, L1-sanctioned) vs `agents/<name>/session/` (singular, cross-session state: `agent-state`, `agent-mode`, `handoff.yaml`, etc.). Detail: `core/config/conventions/session-state.md` "Two-Tier Session Layout".

## Convention Index

Schema, script API, or protocol details for any subsystem — read from `core/config/conventions/`:

| File | Topics |
|------|--------|
| `aspirations.md` | Aspiration JSONL schema, script API, archival |
| `pipeline.md` | Pipeline JSONL schema, script API |
| `experience.md` | Experience archive schema, script API |
| `reasoning-guardrails.md` | Reasoning bank + guardrails JSONL |
| `pattern-signatures.md` | Pattern signatures schema, script API |
| `spark-questions.md` | Spark questions schema, script API |
| `journal.md` | Journal index schema, script API |
| `tree-retrieval.md` | Retrieval engine, tree scripts |
| `goal-schemas.md` | Goal verification, scoring |
| `goal-selection.md` | Mandatory goal-selector.sh |
| `session-state.md` | State machine, session scripts, two-tier layout, background jobs |
| `agent-dir-resolution.md` | Sync-site tables, cross-agent glob consumers — **read BEFORE changing constants or adding agent-dir globs** |
| `infrastructure.md` | Error response, infra health, knowledge reconciliation |
| `secrets.md` | Credentials, env-read.sh |
| `working-memory.md` | WM schema, wm-*.sh API |
| `curriculum.md` | Curriculum schema, gate types |
| `handoff-working-memory.md` | Handoff schema, blocker tracking |
| `compact-recovery.md` | Compact recovery, slot restoration |
| `meta-strategies.md` | Meta-strategy schemas, imp@k |
| `skill-quality.md` | Skill quality evaluation |
| `board.md` | Message board schema, directive payload |
| `history.md` | File versioning `.history/`, changelog |
| `external-paths.md` | `local-paths.conf` format, external path config |
| `precision-encoding.md` | Precision manifest, Verified Values |
| `agent-spawning.md` | Spawning context injection, repo safety tiers |
| `retrieval-escalation.md` | 3-tier retrieval: tree → codebase → web |
| `exhaustive-search-before-negation.md` | Search protocol before negative conclusions |
| `resource-locators.md` | Stable-fact locator schema |
| `coordination.md` | Multi-agent: claims, board, circuit breaker, directives, team state |
| `partner-liveness.md` | Liveness-check.sh verdicts, corroboration protocol |
| `defer-routing.md` | defer_reason chokepoint, Layer-D auto-conversion |
| `loop-terminal-protocol.md` | Terminal-call contract, ScheduleWakeup facts |
| `stop-signal-writers.md` | Authorized `stop-requested` / `agent-state` writers: triggers, invariants |
| `constitutional-rings.md` | Three-ring governance model |
| `learning-routing.md` | "Where does this learning go?" — ten-store decision tree |
| `encoding-triggers.md` | Encoding/tree-update trigger catalog |
| `python-invocation.md` | Windows Python shim, `py -3` fallback |
| `gate-overrides.md` | `--override-all`, flag precedence, audit ledger |
| `audit-baselines.md` | `meta/audit-baselines.yaml` schema, verdicts (seeded/stable/ratcheted/regressed), /verify-learning integration |
| `temp-store.md` | Agent temp store, temp-vs-scratch lifecycle |
| `artifact-reference-integrity.md` | D3 decision: dangling-ref remedy is FOLDING |
| `domain-recipe-seed-purity.md` | Domain-specific upgrade recipes in the seed |
| `fleet-secret-provisioning.md` | Bootstrap-key vault provisioner |
| `transfer-bundle-export-shape.md` | OKF-aligned export shape for transfer bundles |
| `governed-store-write-classes.md` | Merge-protected vs fence-only store classification |
| `world-contract.md` | ENVIRONMENT_ID / COMMONS_POLICY, cross-world guardrails |
| `cross-deployment-channel.md` | **This world is not alone.** Peer deployments, `environments/*.yaml` registry, live board channel, `peer-board-post.sh` |
| `hot-path-size-budget.md` | Always-loaded prose may not GROW, and CLAUDE.md + unscoped rules stay ≤150k chars: the `commit-msg` hook refuses both; `hot-path-size-gate.sh --check` reports |

Additional files exist in `core/config/conventions/` beyond those listed. On-demand specs: `core/config/{hypothesis,knowledge}-conventions.md`, `core/config/architecture-reference.md`, `core/config/verification-checklist{,-domain-specific}.md`, `core/config/status-output.md`.

## Universal Conventions

### File Formats
YAML for config/indexes. JSONL for lifecycle records (aspirations, pipeline, experiences, reasoning bank, guardrails, journal). JSON for metadata. Markdown + YAML front matter for knowledge articles and journal entries.

### Domain-Free Cognitive Core
`world/` = shared domain state. `<agent>/` = per-agent private state. `meta/` = domain-agnostic strategies. `core/` + `.claude/` = immutable framework (describes INTENT, never domain-specific implementation). Domain knowledge lives in `world/` (conventions, guardrails, reasoning bank, tree, forged skills). Agent state lives in `<agent>/` (experience, journal, session).

### Naming Rules
- All filenames: **lowercase, kebab-case** (hyphens, no spaces, no underscores except pipeline/experience record IDs)
- ISO 8601 dates everywhere. Timestamps: naive UTC via `$(date +%Y-%m-%dT%H:%M:%S)`, enforced by `.claude/settings.json` env `TZ=UTC`. After changing TZ posture, restart daemons. Why: `core/config/architecture-reference.md` § Timestamp Posture.

### ID Formats
- Aspirations: `asp-NNN` | Goals: `g-NNN-NN` | Prep tasks: `pt-NNN` | Guardrails: `guard-NNN` | Reasoning bank: `rb-NNN` | Beliefs: `bel-NNN` | Transitions: `trans-NNN` | Spark questions: `sq-NNN` (candidates: `sq-cNN`) | Pattern signatures: `sig-NNN` | Strategy archive: `sa-NNN` | Experiences: `exp-{source-id-or-slug}` | Pipeline: `YYYY-MM-DD_slug`

### Priority Values
- `HIGH`, `MEDIUM`, `LOW` (uppercase)

### Status Values

Goals: `pending`, `in-progress`, `completed`, `blocked`, `skipped`, `expired`, `decomposed`, `superseded` | Pipeline: `discovered`, `active`, `resolved`, `archived` | Aspirations: `active`, `completed`, `paused`, `retired`. Full per-entity status lists: see convention files.

### Pipeline Rules
- **Never delete** pipeline records — move via `pipeline-move.sh`
- Journal entries are **append-only**
- Hypothesis horizons: `micro`, `session`, `short`, `long`
- Hypothesis types: `high-conviction`, `calibration`, `exploration`, `contrarian`

### Python Invocation (Windows)
`python3` hits a Microsoft Store stub (exit 49). **Rule**: use `bash core/scripts/<wrapper>.sh` or `py -3 -c "..."` for direct Python. Use `python3` only inside `.sh` scripts that source `_paths.sh`. Detail: `core/config/conventions/python-invocation.md`.

### Daemon-Only Architecture
35 wrappers are daemon-only (no Python CLI fallback). See `.claude/rules/no-python-cli-fallback.md`.

### Self File Format and The Program
Shared purpose: `world/program.md`. Agent identity: `agents/<agent>/self.md` (YAML front matter + markdown body). Schema: `.claude/rules/self.md`.

### Skill Invocation Rules
- **Control skills** (/start, /stop, /open-questions): user-only — Claude MUST NOT invoke
- **Hybrid skills** (/agent-completion-report, /backlog-report, /encode-session, /forge-skill, /priority-review, /sprint-planning, /verify-learning, /generate-domain-goals, /update-framework): user AND agent
- **Internal skills** (`user-invocable: false`): agent-only during RUNNING
- **No blocking on user input in RUNNING state**

### Code Change Verification (MANDATORY)
After ANY code change: read the project's CLAUDE.md, run tests, fix errors. Never declare ready until build passes.

### Knowledge Reconciliation
After any action that changes the world, check if knowledge tree nodes need updating. Detail: `core/config/conventions/infrastructure.md`.

### Tool Usage + Write Permissions

- Use `Write` only for NEW files. Use `Edit` for existing files.
- All JSONL stores accessed exclusively via scripts. See convention files for APIs.
- Working memory (`agents/<agent>/session/working-memory.yaml`) accessed exclusively via `wm-*.sh` scripts. See `core/config/conventions/working-memory.md`.

| Path | Permission | Purpose |
|------|-----------|---------|
| `world/**` | Create, write, edit, delete | Collective domain state |
| `<agent>/**` | Create, write, edit, delete | Per-agent private state |
| `meta/**`          | Create, write, edit    | Agent-editable meta-strategies    |
| `.claude/skills/**`, `.claude/rules/**`, `core/scripts/**`, `core/config/**`, `CLAUDE.md`, `.claude/settings.json` | Create, write, edit | **Framework — agent-editable, git-audited.** Git-tracked + loop-committed. `settings.json` gated by fail-closed `settings-structural-validator`. Be surgical (`implementation-discipline.md`). |
| `.claude/settings.local.json`, `core/scripts/settings-structural-validator.{py,sh}` | **CONSTITUTIONAL ANCHOR — agent MUST NOT edit** | Keystone making everything else safely editable. Changes need a user-authorized maintenance path, never autonomous. Detail: `core/config/conventions/constitutional-rings.md`, rb-931. |

**The two-file settings rule**: `.claude/settings.json` = **agent-editable** (hooks, env, permissions). `.claude/settings.local.json` = **agent-MUST-NOT-edit** constitutional anchor. Do NOT user-gate framework patches to `.claude/skills/**`, `.claude/rules/**`, `core/**`, or `CLAUDE.md` — the agent applies these itself; user-gating is a capability-routing violation (see `.claude/rules/capability-before-user.md`).

**Mode-based capability gating**: Each skill has `minimum_mode` front matter (reader, assistant, autonomous). Skills check mode at entry and refuse if insufficient.

## Session Start Protocol

1. Bash: `session-state-get.sh` → read state
2. Branch on state (check state BEFORE loading mode — avoids contradictions):
   - **If NO_AGENT**: No agent bound. Suggest: `/start <agent-name>` to create/resume. DONE.
   - **If UNINITIALIZED**: Follow `.claude/rules/user-interaction.md` UNINITIALIZED protocol. DONE.
   - **If RUNNING**: If this is a new session (not an autocompact resume), suggest `/start <agent> --mode reader` or `--mode assistant`. DONE — do not invoke boot or auto-resume.
   - **If IDLE**: Bash: `session-mode-get.sh` → read mode (default: `reader`).
     **Interrupted-stop check**: Bash: `bash core/scripts/stop-checkpoint.sh resume-needed`.
       - Exit 0 + `mode == autonomous`: invoke `/aspirations-graceful-stop --resume`. DONE.
       - Exit 0 + `mode` is assistant/reader: `bash core/scripts/stop-checkpoint.sh clear`, then continue below.
       - Exit 1 (common case): continue below.
     Read `core/config/modes/{mode}.md`. Invoke `/prime`, then ready for user.

### Agent-Session Binding

`MIND_AGENT` env var is the ONLY agent resolution mechanism. The PreToolUse[Bash] hook (`bash-agent-inject.sh`) auto-injects it from the Session Binding above. Override: `MIND_AGENT=<other> <cmd>`. Multiple terminals work independently.

## Knowledge Retrieval (All States)

When persona is active, consult knowledge before answering domain questions (`core/config/conventions/retrieval-escalation.md`):
1. **Tier 1 — Knowledge Tree**: `retrieve.sh --category {category} --depth medium`
2. **Tier 2 — Codebase**: Grep/Glob/Read on the primary workspace
3. **Tier 2.5 — Peer Worlds**: `peer-retrieve.sh` — only `status: empty` at `completeness: complete` licenses a negative
4. **Tier 3 — Web Search**: WebSearch/WebFetch (assistant/autonomous only)

Stop at first sufficient tier. Never say "I don't have context" without attempting all eligible tiers.

## External Knowledge Hubs

Reference bundles for agent-cognition grounding (distinct from retrieval escalation). Clone and read when improving self, framework, or agent design.

| Hub | Source | Consult when |
|-----|--------|--------------|
| **Ayoai-Research-Analyst** | `https://github.com/zkysar1/Ayoai-Research-Analyst` (PRIVATE — `gh auth` clone; local path varies) — OKF v0.1: perception, cognitive-architecture, memory, planning, learning/adaptation, ABC modeling | Improving self, framework, or agent design |

Registered at dev source (Mind-Mind); flows downstream via promotion (why: `core/config/architecture-reference.md`).

## User Control Commands

| Command | Effect | Valid From |
|---------|--------|-----------|
| `/start <name>` | Create/resume agent in autonomous mode (default) | UNINITIALIZED, IDLE |
| `/start <name> --mode reader` | Reader mode (read-only) | UNINITIALIZED, IDLE, RUNNING* |
| `/start <name> --mode assistant` | Assistant mode (user-directed) | UNINITIALIZED, IDLE, RUNNING* |
| `/stop <agent-name> [--reader]` | Consolidate → drop to assistant (or reader with `--reader`) → IDLE. Agent name REQUIRED — bare `/stop` is refused. | RUNNING, IDLE |
| `/verify-learning` | Post-test verification | ANY |
| `/open-questions` | Show open questions | ANY |
| `/agent-completion-report` | Show what changed *(agent-callable)* | ANY |
| `/backlog-report` | Sprint planning backlog *(agent-callable)* | ANY |
| `/priority-review` | Priority dashboard *(agent-callable)* | ANY |
| `/sprint-planning` | Sprint-planning pass: backlog, directives, hygiene, verified writes. `--ultra` = multi-agent *(agent-callable)* | ANY (assistant+) |
| `/encode-session` | 7-lane learning pass on current chat: tree/rb/guardrails/experience encoding, Maintain goals, blocker re-probe *(agent-callable)* | IDLE (assistant) |
| `/generate-domain-goals` | Supply-side goal generation: six-lens recon, evidence-backed candidates, adversarial verification, dedup filing. `--ultra` = multi-agent *(agent-callable)* | ANY (assistant+) |
| `/update-framework` | Pull newest framework release from staging Mind via `pull-promotion.md`. Never web-search *(agent-callable — recurring g-002-02)* | ANY (assistant+) |

\*RUNNING + reader/assistant = **observer session** (coexists with autonomous loop, no state writes). See `core/config/conventions/session-state.md`.

### Enforcement Rules

1. Claude MUST NOT invoke /start, /stop, or /open-questions.
2. Claude MUST NOT invoke boot or start the aspirations loop without RUNNING state and autonomous mode.
3. In reader mode: read-only assistant. May read state but MUST NOT execute write operations or workflow skills.
4. In assistant mode: user-directed assistant. May read and write when asked but MUST NOT self-initiate or run the loop.
5. In autonomous mode (RUNNING state): autonomous via aspirations loop.
6. Auto-resume after autocompact is handled by the stop hook (unconditional BLOCK + LOOP_CONTINUE), NOT by the Session Start Protocol. A new session that finds RUNNING state must show the error (or start an observer session if `--mode reader|assistant` is requested), not auto-resume.

### Autonomous Loop Rules

See `core/config/modes/autonomous.md` (loaded on demand in autonomous mode).

## Auto-Session Continuation

Signal files (`agents/<agent>/session/`): `agent-state`, `agent-mode`, `persona-active`, `stop-loop`, `stop-requested`, `handoff.yaml`, `runner-token`, etc. Full table, compact checkpoint, context dedup: `core/config/conventions/session-state.md`.

## Available Skills

The harness lists every skill, with its description, each turn. Internal skills (`user-invocable: false`) are agent-only, RUNNING state; hybrid skills are listed under Skill Invocation Rules; forged skills: `world/forged-skills.yaml`. Skill chaining map: `core/config/architecture-reference.md`.
