# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Domain-agnostic continual learning base agent. The system forms hypotheses, tracks their outcomes, builds memory of what worked and what failed, and self-evolves its reasoning capabilities over time. It serves as a reusable foundation for any domain where an autonomous agent needs to learn, reflect, and improve through experience.

## Architecture

This is a **Claude-native data repository** — no traditional source code or build tools. Configuration and state live in YAML, JSONL, and Markdown files that Claude reads, reasons over, and updates autonomously.

### Framework vs State Split (4-Tier Architecture)

- **`core/config/`** — Framework definitions and parameter bounds (immutable). Contains templates, thresholds, pipeline configs, `initial_state:` sections, and convention reference files in `core/config/conventions/`.
- **`core/scripts/`** — Framework infrastructure scripts. All JSONL stores accessed exclusively via these scripts — the LLM never reads/edits JSONL files directly.
- **`meta/`** — Agent-editable meta-strategies and domain-agnostic data (independent of domain data). Metacognitive self-modification layer inspired by HyperAgents. Also includes: `spark-questions.jsonl`, `skill-quality.yaml`, `skill-gaps.yaml`, `evolution-log.jsonl`, `reflection-templates.yaml`, `strategy-archive.yaml`, `config-overrides.yaml`, `config-changes.yaml`, `step-attribution.yaml`, `meta-knowledge/`.
- **`world/`** — Collective domain state shared across agents within a domain. Lives at an **external user-supplied path** (shared drive, NAS, etc.), configured in `agents/<agent>/local-paths.conf`. Contains the knowledge tree, aspirations, pipeline, reasoning bank, guardrails, pattern signatures, message board, file history, changelog, conventions, sources, `program.md` (The Program — shared purpose), etc.
- **`agents/<agent-name>/`** — Per-agent private state (e.g., `agents/alpha/`). Contains session state, journal, experience traces, `self.md` (agent identity), curriculum, developmental stage, profile, infra health, and the agent's local aspiration queue. The `agents/` parent is configurable via `AGENTS_PARENT_DIR` (see "Agent-dir Resolution" below).

**External paths**: `world/` and `meta/` live at user-supplied external paths configured per-agent in `agents/<agent>/local-paths.conf` (gitignored). The local repo only contains `core/`, `.claude/`, and `<agent>/` directories. Each agent can point to different locations. See `core/config/conventions/external-paths.md` for details.

**Removing data**: Delete the relevant directory — `<agent>/` for one agent, the world directory for domain data, or the meta directory for improvement strategies. Each agent's `agents/<agent>/local-paths.conf` stores its external world/ and meta/ paths. See `core/config/conventions/external-paths.md` for details.

**Project Structure**:
```
core/                # Shareable cognitive framework (copy to any project)
  config/            # Framework definitions (immutable)
    conventions/     # On-demand convention reference files
  scripts/           # Utility scripts (framework infrastructure)
meta/                # Agent-editable meta-strategies (independent of domain data)
  goal-selection-strategy.yaml, reflection-strategy.yaml  # Strategy files
  evolution-strategy.yaml, aspiration-generation-strategy.yaml
  encoding-strategy.yaml, improvement-instructions.md
  improvement-velocity.yaml                               # imp@k metrics
  meta-log.jsonl                                          # Strategy change audit (script-only)
  spark-questions.jsonl, skill-quality.yaml, skill-gaps.yaml
  evolution-log.jsonl, reflection-templates.yaml, strategy-archive.yaml
  config-overrides.yaml, config-changes.yaml, step-attribution.yaml
  gate-firings.jsonl, gate-eval-recommendations.jsonl  # Phase 1+5 gate telemetry
  audit-baselines.yaml                                 # Advisory ratchet baselines (learning-routing drift, etc)
  meta-knowledge/    # Meta-knowledge index + entries
  experiments/       # A/B experiment tracking
  transfer/          # Cross-domain transfer bundles
world/               # Collective domain state (shared across agents, external path)
  program.md         # The Program — shared purpose
  aspirations.jsonl  # Central task list (world-level goals)
  pipeline.jsonl     # Shared hypothesis registry
  knowledge/tree/    # Collective knowledge tree
  reasoning-bank.jsonl, guardrails.jsonl, pattern-signatures.jsonl
  override-bypass-ledger.jsonl  # Phase 4 bulk-override audit ledger
  board/             # Message board channels (general, findings, coordination, decisions)
  .history/          # Self-contained file version history (copy-on-write snapshots)
  changelog.jsonl    # Auto-appended audit trail of all writes
  conventions/       # Domain-specific conventions
  forged-skills.yaml # Forged skills registry (shared across agents)
  skill-relations.yaml # Skill relationship graph (shared across agents)
  scripts/           # Domain-specific scripts (shared across agents)
agents/              # Parent directory holding all agent dirs (configurable, see Agent-dir Resolution)
  <agent-name>/      # Per-agent private state (e.g., agents/alpha/)
    self.md          # Agent identity and specialization
    aspirations.jsonl  # Agent's local work queue
    experience.jsonl   # Agent's raw interaction traces
    journal.jsonl      # Agent's activity log
    session/         # Ephemeral session state (working memory, handoff, signal files)
    curriculum.yaml  # Agent's progression
.claude/skills/      # Skill definitions
.claude/rules/       # Rule definitions
```

### Core Design Principle: No Terminal State

The system is a perpetual loop. Completion of one thing seeds the next. `/aspirations loop` is the heartbeat — it never exits, it always has work to create.

*(Full rules in `core/config/modes/autonomous.md`)*

### Core Design Principle: Consolidate Before Expand

Depth over breadth. Completion of existing work takes priority over starting new.
An aspiration 90% complete has more gravitational pull than a brand-new aspiration.
New directions require healthy existing completion rates (>25% average) or explicit
justification (user directive, critical blocker, all existing work blocked).

*(Full rules in `.claude/rules/consolidate-before-expand.md`)*

### Mode System

The framework has three operational modes. Mode is the single user-facing control — state and persona are derived automatically.

| Mode | State | Persona | Capabilities |
|------|-------|---------|-------------|
| `reader` (safe floor) | IDLE | ON (light) | Read knowledge, prime, answer questions. No writes. Opt-in via `/stop <agent-name> --reader`. |
| `assistant` (post-stop default) | IDLE | ON (full) | Reader + write to tree, remember things, research when asked, accept directives. No loop. |
| `autonomous` | RUNNING | ON (full) | Everything. Self-directed perpetual learning loop. |

Reader is the safe floor, not the routine default. `/stop <agent-name>` lands in assistant mode (reconciliation-ready) so the user can mark goals complete, edit tree nodes, or add guardrails without a mode-switch ceremony. Pass `/stop <agent-name> --reader` to drop to read-only (walking-away case). Agent name is REQUIRED on `/stop` — bare `/stop` is refused (prevents the cross-session wrong-agent stop, 2026-04-24 incident). Disk default (absence of `agent-mode`) is still reader — that handles passive/crashed sessions.

Mode-specific behavioral rules live in `core/config/modes/{mode}.md` — loaded on demand at session start.
Mode signal file: `agents/<agent>/session/agent-mode` (plain text: reader, assistant, autonomous).
Scripts: `session-mode-get.sh`, `session-mode-set.sh` (only /start and /stop may write).

### Cognitive Primitives

Four goal types the agent can create anytime via `aspirations-add-goal.sh`:
- **Unblock** (`"Unblock: ..."`, HIGH) — created by CREATE_BLOCKER protocol when a problem can't be fixed inline
- **Investigate** (`"Investigate: ..."`, MEDIUM) — diagnostic, something seems off
- **Idea** (`"Idea: ..."`, MEDIUM) — creative insight, improvement opportunity
- **Maintain** (`"Maintain: ..."`, MEDIUM) — in-flight framework correction the agent JUST performed inline; filed with `status: completed` so the standard encoding pipeline fires

Not mutually exclusive. A single event can spawn all four. See `aspirations-execute/SKILL.md` Cognitive Primitives section (Cross-Agent Insight is also described there but produces a board post, not a goal).

### Core Systems

| System | Key Files |
|--------|-----------|
| The Program (shared purpose) | `world/program.md` |
| Self (agent identity) | `agents/<agent>/self.md`, `.claude/rules/self.md`  |
| Aspirations engine | `world/aspirations.jsonl`, `agents/<agent>/aspirations.jsonl`, `core/config/aspirations.yaml` |
| Hypothesis pipeline | `world/pipeline.jsonl` |
| Experience archive | `agents/<agent>/experience.jsonl`, `agents/<agent>/experience/` |
| Memory/Knowledge tree | `world/knowledge/tree/_tree.yaml` |
| Pattern signatures | `world/pattern-signatures.jsonl` |
| Reasoning bank | `world/reasoning-bank.jsonl` |
| Guardrails | `world/guardrails.jsonl` |
| Spark questions | `meta/spark-questions.jsonl` |
| Journal | `agents/<agent>/journal.jsonl`, `agents/<agent>/journal/` |
| Working memory | `agents/<agent>/session/working-memory.yaml`, `core/scripts/wm-*.sh` |
| Session state | `agents/<agent>/session/` |
| Agent mode | `agents/<agent>/session/agent-mode`, `core/config/modes/` |
| Secrets store | `.env.example`, `.env.local` |
| Memory pipeline | `core/config/memory-pipeline.yaml` |
| Reflection engine | `/reflect` skill |
| Experiential index | `agents/<agent>/experiential-index.yaml` |
| Curriculum | `agents/<agent>/curriculum.yaml`, `core/config/curriculum.yaml` |
| Domain conventions | `world/conventions/*.md` |
| Gate registry + telemetry | `core/config/gates.yaml`, `meta/gate-firings.jsonl`, `meta/gate-eval-recommendations.jsonl`, `world/override-bypass-ledger.jsonl`, `core/scripts/_gate_log.py`, `core/scripts/_override_helpers.py`, `core/scripts/gate-retirement-eval.sh` (prescriptive evaluator), `core/scripts/gate-stats.sh` (descriptive dashboard) |
| Meta-strategies | `meta/*.yaml`, `core/config/meta.yaml` |
| Skill relations | `core/config/skill-relations.yaml`, `world/skill-relations.yaml` |
| Skill quality | `meta/skill-quality.yaml`, `meta/skill-quality-strategy.yaml` |
| Message board | `world/board/*.jsonl`, `core/scripts/board.py` |
| File history | `world/.history/`, `meta/.history/`, `core/scripts/history.py` |
| Changelog | `world/changelog.jsonl`, `core/scripts/changelog.py` |
| Background jobs | `agents/<agent>/session/background-jobs.yaml`, `core/scripts/background-jobs.sh` |
| Agent watchdog | `core/scripts/agent-watchdog.py`, `agents/<agent>/session/watchdog-prev-state.json`, `core/logs/watchdog-<agent>.jsonl` (periodic probe registry — invoked from iteration-close.sh productivity-check via `--tick`; cross-platform, no daemon, no PID file) |
| External paths | `agents/<agent>/local-paths.conf`, `core/scripts/_paths.sh`, `core/scripts/_paths.py` |
| File operations | `core/scripts/_fileops.py` (locking, history, changelog) |
| Team state | `world/team-state.yaml`, `core/scripts/team-state.py`, `team-state-update.sh`, `team-state-read.sh` |
| Execution diary | `agents/<agent>/session/execution-diary.jsonl`, `core/scripts/execution-diary.sh` |
| Reasoning snapshot | `agents/<agent>/session/reasoning-snapshot.yaml`, `core/scripts/reasoning-snapshot.sh` |
| Compact recovery | `agents/<agent>/session/compact-checkpoint.yaml`, `core/scripts/compact-restore-slots.sh` |

## Agent-dir Resolution

Agent directories are resolved through a centralized helper, not hardcoded
`PROJECT_ROOT / agent_name` joins. This indirection exists so that Phase 2.5.D
relocated all agent dirs under an `agents/` parent by flipping one constant.

**AGENTS_PARENT_DIR** — empty string means agent dirs live at `PROJECT_ROOT`
(legacy layout). Currently `"agents"` — agent dirs live at
`PROJECT_ROOT/agents/<name>`.

**Phase 2.6 added two more sync constants** for the per-session dir layout:
- `SESSIONS_DIRNAME` (currently `"sessions"`) — parent under each agent for
  per-session dirs (one per Claude Code session)
- `SESSION_DIRNAME` (currently `"session"`) — agent-wide cross-session state dir

The 6 framework-layer sync locations carry all three constants:

| Layer | File | Constants |
|-------|------|-----------|
| Python CLI | `core/scripts/_paths.py` | `AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME`, `SESSION_DIRNAME` |
| Shell CLI | `core/scripts/_paths.sh` | same |
| Daemon | `mind_api/src/agent_paths.py` | same |
| Import-cycle-proof | `core/scripts/_agents.py` | same |
| Import-cycle-proof | `core/scripts/path-resolution-hook.py` | same |
| Import-cycle-proof | `core/scripts/_world_config.py` | `AGENTS_PARENT_DIR` (added 2026-05-20 after Phase 2.5.D regression where this helper still used the pre-relocation `root/agent/local-paths.conf` shape — all `world/config/*.yaml` overlay loads silently degraded to safe defaults for ~3 weeks until pytest collection collision surfaced 25 routing-table-empty failures) |
| Import-cycle-proof | `core/scripts/_session_binding.py` | `AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME` (Phase 2.6 resolver — re-exported to `migrate-to-phase-2-6.py`, `session-binding-write.py`, `_resolve_agent_from_sid.py`) |

**Plus 5 inlined copies** that mirror the constants by hand because sourcing
`_paths.sh` (or importing `_paths.py`) would violate the script's contract —
either by exceeding the per-Bash-call latency budget (the `IRREDUCIBLY LOCAL`
annotation at the top of each shell script) or by breaking the shell→python
bridge that calls the helper via `py -3 -c "from <module> import ..."`:

| File | Constants inlined | Reason |
|------|-------------------|--------|
| `core/scripts/cleanup-stale-bindings.sh` | `AGENTS_PARENT_DIR` (`_APD`), `SESSIONS_DIRNAME` (`_SDN`) | IRREDUCIBLY LOCAL — per-Bash-call latency budget |
| `core/scripts/session-mode-get.sh` | `AGENTS_PARENT_DIR` (`_APD`) | IRREDUCIBLY LOCAL — session-state critical path |
| `core/scripts/session-signal-exists.sh` | `AGENTS_PARENT_DIR` (`_APD`) | IRREDUCIBLY LOCAL — hook hot path |
| `core/scripts/session-state-get.sh` | `AGENTS_PARENT_DIR` (`_APD`) | IRREDUCIBLY LOCAL — every loop iteration |
| `core/scripts/_wake_signals.py` | `AGENTS_PARENT_DIR` (`_AGENTS_PARENT_DIR`) | imported via `py -3 -c "from _wake_signals import ..."` from shell — must stay self-contained |

**Plus 2 literal-string hardcoders** bake the literal `agents/` path segment
directly into a glob or prefix-check WITHOUT naming the constant — so the
constant-definition audit grep below does NOT see them. They work for
`AGENTS_PARENT_DIR=agents` but would break on any rename:

| File | Hardcoded sites | Why not constant-routed |
|------|-----------------|-------------------------|
| `core/scripts/iteration-commit.sh` | `"$REPO"/agents/*/` agent-dir walk (~L221) + `[[ "$path" == agents/* ]]` namespace-filter prefix checks (~L591/657/706) | Namespace filter; never sources `_paths.sh`. Contrast `stop-hook.sh:128`, which DOES route its `*/session/` glob through `${AGENTS_PARENT_DIR}` — the gold-standard pattern these two should adopt. |
| `core/scripts/seed-transplant.sh` | `"$DEST"/agents/*/session/agent-state` walk (~L99) | Walks a FOREIGN repo root (`$DEST`), not `PROJECT_ROOT` |

When changing `AGENTS_PARENT_DIR`, `SESSIONS_DIRNAME`, or `SESSION_DIRNAME`,
update ALL 12 constant-named sites AND the 2 literal-string hardcoders above.
Audit with ALL THREE greps (the first finds constant-named sites; the second
finds literal-`agents/` glob hardcoders — it also surfaces comments/tests/bench
refs, so eyeball-filter to executable glob/prefix lines; the third finds
`.parent`-based PROJECT_ROOT re-derivations from an agent-dir variable, which
the constant-name and `agents/*` greps both miss):
`grep -rn '^[[:space:]]*\(_APD=\|_SDN=\|_\?AGENTS_PARENT_DIR\|_\?SESSIONS_DIRNAME\|_\?SESSION_DIRNAME\)' core/scripts/ mind_api/`
`grep -rn 'agents/\*' core/scripts/ mind_api/`
`grep -rnE '(agent_dir|AGENT_DIR)\.parent' core/scripts/ mind_api/`
Third-grep triage: a single `.parent` of `PROJECT_ROOT/agents/<agent>` yields
the *agents-parent* dir (correct for sibling enumeration — goal-selector.py
`collect_cross_agent_candidates`), but treating that `.parent` result AS
PROJECT_ROOT, or joining it with `core`/`config`, is the g-115-1279 bug class
(budget-meter `read_config` 404'd the config and pinned `cap_ms` to the 9000ms
fallback vs the configured 90000ms). The fix forwards `$PROJECT_ROOT` from
`_paths.sh` (SSOT); a `.parent.parent` fallback matches the current `agents/`
layout.

**Plus cross-agent glob consumers** sweep one file across ALL agent dirs via
`agents_root().glob("*/...")` (CLI) or `ctx.paths.agents_root.glob("*/...")`
(daemon). When correctly routed they auto-track an `AGENTS_PARENT_DIR` rename
(need NO edit), but they are invisible to all THREE greps above — they neither
define the constant, write a literal `agents/*`, nor use `.parent` — so a
depth-1 redrift (`PROJECT_ROOT.glob("*/...")`, which matches NOTHING
post-relocation) escapes every audit grep. This table is their only audit
surface; check it on rename:

| File(s) | Glob | Status |
|---------|------|--------|
| `core/scripts/skill-discovery.py` + `mind_api/src/endpoints/skill_discovery.py` | `*/journal.jsonl` + `*/session/execution-diary.jsonl` invocation sources | ✓ routed (`agents_root()` / `ctx.paths.agents_root`). Were depth-1 until g-115-1405 — the drift silently zeroed 2 of 4 invocation sources for EVERY skill, inflating `silently_undertriggering`. Regression-guarded by the `/verify-learning` glob-routing check + `test_skill_discovery*.py` byte-compat. |
| `core/scripts/_paths.py`, `core/scripts/utilization-stats.py`, `mind_api/src/agent_paths.py` | `*/local-paths.conf` enumeration | ✓ routed (helper / imported `agents_root`) — the reference pattern to copy for any new cross-agent glob. |
| `core/scripts/skill-coinvocation-discovery.py` | `*/skill-invocations.jsonl` ledger mining (co-invocation candidates) | ✓ routed (`read_ledger` base defaults to `agents_root()`; the `root=` param is a test-only override). Regression-guarded by the `/verify-learning` `skill-coinvocation-glob-routing` check + the `--apply` RMW tests in `test_skill_coinvocation_discovery.py` (g-304-24). |
| `mind_api/src/endpoints/utilization.py` (~L278) | `*/local-paths.conf` enumeration | ⚠ LATENT hardcode `project_root / "agents"` (literal segment — works today, breaks on rename). SHOULD route through `ctx.paths.agents_root` like its CLI sibling `utilization-stats.py`. Surfaced by the g-115-1405 audit. |

Helper API (available after sourcing `_paths.sh` or importing from `_paths`):
- `agents_root()` — parent directory containing all agent dirs
- `agent_dir(name)` — full path to a named agent's directory
- `agent_sessions_root(name)` — parent dir for per-session dirs (Phase 2.6)
- `agent_session_dir(name, sid)` — one per-session dir (Phase 2.6)
- `agent_state_dir(name)` — agent-wide cross-session state dir (Phase 2.6)
- `enumerate_agent_confs()` — sorted list of `*/local-paths.conf` paths (Python only)
- `is_under_agent_dir(p)` — whether a path is inside an agent directory (Python only)

**Rule**: Never write `PROJECT_ROOT / agent_name` or `$PROJECT_ROOT/$AGENT`
directly. Use `agent_dir(name)` or `$(agent_dir "$AGENT")` instead. For
per-session paths use `agent_session_dir(name, sid)`.

## Session Binding (Phase 2.6)

The agent-session binding lives at
`agents/<name>/sessions/<SID>/binding.yaml` (NOT at the legacy
`.active-agent-<SID>` file at PROJECT_ROOT — that form is the migration
fallback). The binding carries `agent`, `mode`, `started_at`, `started_by`.

Resolver: `core/scripts/_session_binding.py::resolve_binding(sid, root)` —
tries Phase 2.6 layout first, falls back to legacy. Backward-compatible
shell wrapper: `bash core/scripts/session-binding-read.sh <SID>` (default
output: agent name).

Writer: `core/scripts/session-binding-write.{py,sh}` — called by /start at
each of its 4 binding sites with `--retire-legacy` to delete any stale
`.active-agent-<SID>` from a prior run.

Per-session dir semantics:
- Created by /start; dir name IS the SID
- L1-sanctioned scratch home (see `.claude/rules/path-resolution.md`)
- Holds `binding.yaml` (entry) and `session-summary.yaml` (exit, written
  by graceful-stop D6.5)
- Counting subdirs under `agents/<name>/sessions/` = counting sessions
- Stale subdirs (mtime > 24h + no live runner) removed by
  `cleanup-stale-bindings.sh`

The agent-wide `agents/<name>/session/` (singular) still holds files that
must survive across sessions or represent agent-wide pointers
(`running-session-id`, `latest-session-id`, `runner-token`, `agent-state`,
`agent-mode`, `handoff.yaml`, etc.). See
`core/config/conventions/session-state.md` "Two-Tier Session Layout".

## Convention Index

When you need schema, script API, or protocol details for a subsystem, read the relevant file from `core/config/conventions/`:

| File | Topics |
|------|--------|
| `aspirations.md` | Aspiration JSONL schema, script API, archival rules |
| `pipeline.md` | Pipeline JSONL schema, script API, atomic resolve |
| `experience.md` | Experience archive JSONL schema, script API |
| `reasoning-guardrails.md` | Reasoning bank + guardrails JSONL, guardrail-check script |
| `pattern-signatures.md` | Pattern signatures JSONL schema, script API |
| `spark-questions.md` | Spark questions JSONL schema, script API |
| `journal.md` | Journal index JSONL schema, script API |
| `tree-retrieval.md` | Unified retrieval, tree scripts, category suggestion |
| `goal-schemas.md` | Goal verification, recurring/deferred fields, goal scoring |
| `goal-selection.md` | Mandatory goal-selector.sh, post-compaction fabrication guard |
| `session-state.md` | Agent state machine, session scripts, generic YAML store, background jobs tracker |
| `infrastructure.md` | Error response protocol, infra health, verify-before-assuming details, knowledge reconciliation details |
| `secrets.md` | Credentials convention, env-read.sh, security rules |
| `working-memory.md` | Working memory schema, wm-*.sh script API, slot_meta, pruning rules |
| `curriculum.md` | Curriculum YAML schema, script API, gate types, contract checks |
| `handoff-working-memory.md` | Handoff schema, working memory integration, blocker tracking, reasoning trajectory |
| `compact-recovery.md` | Full-fidelity compact recovery protocol, slot restoration, execution diary, reasoning snapshot |
| `meta-strategies.md` | Meta-strategy schemas, modification protocol, experiments, imp@k, transfer |
| `skill-quality.md` | Skill quality five-dimension evaluation, skill-evaluate.sh API, quality thresholds |
| `board.md` | Message board JSONL schema, script API, agent integration points, directive payload, execution feedback, insight triggers |
| `history.md` | File versioning `.history/` schema, script API, changelog, pruning |
| `external-paths.md` | External path configuration, `local-paths.conf` format, `/start` flow |
| `precision-encoding.md` | Precision manifest schema, extraction heuristics, Verified Values format |
| `agent-spawning.md` | Agent spawning context injection, build-agent-context.sh API, repo safety tiers, anti-patterns |
| `retrieval-escalation.md` | 3-tier retrieval escalation: tree → codebase → web search |
| `exhaustive-search-before-negation.md` | Exhaustive knowledge search protocol before negative conclusions |
| `resource-locators.md` | Stable-fact encoding lane: locator schema, retrieval-before-discovery protocol |
| `coordination.md` | Multi-agent coordination: claim protocol, board types/tags, circuit breaker, review gate, dependency chains, self-abstention, directive protocol, team state protocol |
| `constitutional-rings.md` | Three-ring governance model: Ring 1 (immutable mission), Ring 2 (standards), Ring 3 (autonomous protocols) |
| `learning-routing.md` | "Where does this learning go?" decision tree across all ten stores; multi-store encoding pairs; experience-vs-journal distinction |
| `encoding-triggers.md` | Authoritative catalog of every encoding/tree-update trigger (Txx active, Exx gaps), stores, modes, and frequency — symmetric to `retrieval-triggers.md` |
| `python-invocation.md` | Windows Python access: shim mechanism, hook PATH injection, `py -3` fallback rule for direct `-c` calls (rb-370, guard-335) |
| `gate-overrides.md` | `--override-all` bulk-bypass pattern, per-gate flag precedence, audit ledger schema, decision rule for which form to use |
| `audit-baselines.md` | `meta/audit-baselines.yaml` schema, verdicts (seeded/stable/ratcheted/regressed), when to add a baseline, /verify-learning integration |
| `temp-store.md` | Canonical agent temp store (`agents/<agent>/temp/`), temp-vs-scratch lifecycle, the agent-dir write-surface allowlist, `reports/` freeze + migration |
| `domain-recipe-seed-purity.md` | D1 decision (A): domain-specific upgrade recipes are `domain-leak-exempt` and travel in the seed; the 5 invariants (location/marker/FROM-state-guard/H3b/gate-scope), seed-down per-env-id semantics |
| `transfer-bundle-export-shape.md` | OKF-aligned export shape for `meta/transfer/` bundles: bundle=unit-of-distribution, concept=one md+YAML, one required `type` discriminator, consumers-preserve-unknown-keys, git-shippable interchange (contract-on-shape, not a field schema) |

Additional on-demand specs (not convention files):
- `core/config/hypothesis-conventions.md` — Hypothesis record schemas, horizons, context manifests
- `core/config/knowledge-conventions.md` — Knowledge articles, memory tree, entity cross-links
- `core/config/architecture-reference.md` — Skill chaining map, self-evolution loop
- `core/config/verification-checklist.md` — Post-test verification checklist (framework)
- `core/config/verification-checklist-domain-specific.md` — Foundational domain verification checks (read directly by /verify-learning)
- `core/config/status-output.md` — Status line formats for RUNNING state

## Universal Conventions

### File Formats
- **YAML** (`.yaml`) for structured data: config, indexes
- **JSONL** (`.jsonl`) for lifecycle records: aspirations, pipeline, experiences, reasoning bank, guardrails, pattern signatures, spark questions, journal index
- **JSON** (`.json`) for metadata: aspirations-meta, pipeline-meta, experience-meta
- **Markdown** (`.md`) with YAML front matter for knowledge articles and journal entries

### Domain-Free Cognitive Core
Everything in `world/` is collective domain state (shared across agents). Everything in `<agent>/` is per-agent private state. Everything in `meta/` is domain-agnostic improvement strategy. Everything in `core/` and `.claude/` is immutable framework.
The cognitive core (base skills, rules, `core/`) describes INTENT, never domain-specific
implementation. Domain knowledge lives in `world/`: conventions (`world/conventions/*.md`),
guardrails, reasoning bank, knowledge tree, forged skills (`world/forged-skills.yaml`). Agent-specific state lives in `<agent>/`: experience, journal, session.

### Naming Rules
- All filenames: **lowercase, kebab-case** (hyphens, no spaces, no underscores except pipeline/experience record IDs)
- ISO 8601 dates everywhere. Timestamps: ALWAYS local system time (never UTC). Use `$(date +%Y-%m-%dT%H:%M:%S)`.

### ID Formats
- Aspirations: `asp-NNN` | Goals: `g-NNN-NN` (supports 2-4 digit: `g-NNN-NNNN`; expanded 2026-05-19 after asp-115 hit g-115-999) | Prep tasks: `pt-NNN`
- Guardrails: `guard-NNN` | Reasoning bank: `rb-NNN` | Beliefs: `bel-NNN`
- Transitions: `trans-NNN` | Spark questions: `sq-NNN`, candidates: `sq-cNN`
- Pattern signatures: `sig-NNN` | Strategy archive: `sa-NNN`
- Experiences: `exp-{source-id-or-slug}` | Pipeline: `YYYY-MM-DD_slug`

### Priority Values
- `HIGH`, `MEDIUM`, `LOW` (uppercase)

### Status Values

Goals: `pending`, `in-progress`, `completed`, `blocked`, `skipped`, `expired` | Pipeline: `discovered`, `active`, `resolved`, `archived` | Aspirations: `active`, `completed`, `paused`, `retired`. Full per-entity status lists: see convention files.

### Pipeline Rules
- **Never delete** pipeline records — move via `pipeline-move.sh`
- Journal entries are **append-only**
- Hypothesis horizons: `micro`, `session`, `short`, `long`
- Hypothesis types: `high-conviction`, `calibration`, `exploration`, `contrarian`

### Python Invocation (Windows)
Direct `python3 -c "..."` from a Bash tool call hits a Microsoft Store stub on this machine (Exit code 49: "Python was not found"). The shim in `core/scripts/.python-shim/` plus the `bash-agent-inject.sh` PreToolUse hook defend against this, but the hook fails open on timeout — when it does, the raw command fails. **Rule**: prefer `bash core/scripts/<wrapper>.sh` or `py -3 -c "..."` for direct Python from Bash. Use `python3` only inside `.sh` scripts that source `_paths.sh`. Full detail: `core/config/conventions/python-invocation.md`.

### Daemon-Only Architecture
As of 2026-05-14, 35 wrappers are daemon-only — no Python CLI fallback. See `.claude/rules/no-python-cli-fallback.md` for the behavioral rule and recovery procedures.

### Self File Format and The Program

The shared purpose lives in `world/program.md` (The Program). Each agent's identity lives in `agents/<agent>/self.md` (YAML front matter + markdown body). Schema and maintenance: `.claude/rules/self.md`.

### Skill Invocation Rules
- **Control skills** (/start, /stop, /open-questions): user-invocable only — Claude MUST NOT invoke these
- **Mode control**: `/start <agent-name> --mode <mode>` to enter a mode, `/stop <agent-name>` to return to assistant (or `/stop <agent-name> --reader` for read-only). Agent name is REQUIRED on `/stop`.
- **Hybrid skills** (/agent-completion-report, /backlog-report, /priority-review, /verify-learning): user-invocable AND agent-callable
- **Internal skills**: `user-invocable: false` — invoked by agent during RUNNING state
- **No blocking on user input in RUNNING state** — skills must never wait for, request, or depend on user input during autonomous execution

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
| `.claude/skills/**`, `.claude/rules/**`, `core/scripts/**`, `core/config/**`, `CLAUDE.md`, `.claude/settings.json` | Create, write, edit | **Framework — agent-editable, git-audited.** The agent evolves the framework itself (skills, rules, hooks, conventions, this file). Every edit is git-tracked + loop-committed; `settings.json` is additionally gated by the fail-closed `settings-structural-validator`. Be surgical (`implementation-discipline.md`) — bad edits are `git revert`-able. |
| `.claude/settings.local.json`, `core/scripts/settings-structural-validator.{py,sh}` | **CONSTITUTIONAL ANCHOR — agent MUST NOT edit** | Hard-denied across all permission tiers, tamper-proof by a self-referential deny inside `settings.local.json` (mirrored in `~/.claude/settings.json`). The keystone that makes everything else safely editable. Changes need a user-authorized maintenance path, never an autonomous edit. Detail: `core/config/conventions/constitutional-rings.md`, rb-931. |

**The two-file settings rule** (#1 source of wrongly-user-gated goals): `.claude/settings.json` is **agent-editable** framework config (hooks, env, permission baseline) — edit it directly. `.claude/settings.local.json` is the **agent-MUST-NOT-edit** constitutional anchor + machine-local config. Do NOT create `participants:[user]` / user-gated goals to apply a verified framework patch to `.claude/skills/**`, `.claude/rules/**`, `core/**`, or `CLAUDE.md` — the agent is permitted to apply these itself (git is the safety net); user-gating them is a capability-routing violation (see `.claude/rules/capability-before-user.md`). The framework deny-list was loosened 2026-05-14 (g-115-732) but the behavioral layer was not updated to match until now — that split-brain caused the user-gated-goal spam (e.g. g-115-792) this rule eliminates.

**Mode-based capability gating**: Each skill has a `minimum_mode` front matter field (reader, assistant, autonomous). Skills check mode at entry and refuse if current mode is insufficient. See `core/config/modes/` for per-mode capabilities.

## Session Start Protocol

1. Bash: `session-state-get.sh` → read state
2. Branch on state (check state BEFORE loading mode — avoids contradictions):
   - **If NO_AGENT**: No agent bound. Suggest: `/start <agent-name>` to create/resume. DONE.
   - **If UNINITIALIZED**: Follow `.claude/rules/user-interaction.md` UNINITIALIZED protocol. DONE.
   - **If RUNNING**: Agent is in autonomous mode (another window or crashed session). If this is a new session (not an autocompact resume), suggest `/start <agent> --mode reader` for read-only access or `/start <agent> --mode assistant` for user-directed access. DONE — do not invoke boot or auto-resume.
   - **If IDLE**: Bash: `session-mode-get.sh` → read mode (default: `reader`).
     **Interrupted-stop check (FW-11, g-317-09)**: Bash: `bash core/scripts/stop-checkpoint.sh resume-needed`.
       - If it exits 0 (a `stop-checkpoint.json` is present — autocompact interrupted a prior `/stop` mid-sequence):
         - If `mode == autonomous` (the stop never reached D7, so the target mode was never set): invoke `/aspirations-graceful-stop --resume`. That handler idempotently completes the remaining stop obligations (consolidate, handoff, set target mode, clear the checkpoint), emits its own stop-complete message, and ends the turn. DONE — do not load mode rules or invoke `/prime`.
         - Else (`mode` is already `assistant`/`reader` — the stop substantially completed and only the checkpoint-clear was missed): Bash: `bash core/scripts/stop-checkpoint.sh clear` to retire the stale sentinel, then continue below.
       - If it exits 1 (the common case — no interrupted stop): continue below.
     Read `core/config/modes/{mode}.md`. Invoke `/prime`, then ready for user.

### Agent-Session Binding

Each Claude Code session is bound to one agent via `MIND_AGENT` env var:
- `MIND_AGENT` — the ONLY mechanism for agent resolution in scripts
- `.active-agent-<session_id>` — maps session to agent name
- `/start <name>` writes the binding file
- The PreToolUse[Bash] hook (`core/scripts/bash-agent-inject.sh`) auto-injects `MIND_AGENT=<name>` from the binding file before every Bash call. Override by writing `MIND_AGENT=<other> <cmd>` explicitly when you want a cross-agent probe.
- Multiple terminals work independently — no shared state files.

## Knowledge Retrieval (All States)

When persona is active, the agent MUST consult its knowledge before answering domain questions.
Follow the retrieval escalation convention (`core/config/conventions/retrieval-escalation.md`):

1. **Tier 1 — Knowledge Tree**: `retrieve.sh --category {category} --depth medium` or intelligent retrieval protocol
2. **Tier 2 — Codebase Exploration**: Grep/Glob/Read on the primary workspace (from `agents/<agent>/self.md`)
3. **Tier 3 — Web Search**: WebSearch/WebFetch (assistant/autonomous mode only)

Stop at the first tier that provides sufficient knowledge. Never say "I don't have context"
without attempting all eligible tiers.

## External Knowledge Hubs

Curated external knowledge bundles to consult when reasoning about improving
self, framework, or agents. These are reference sources (distinct from the
retrieval escalation above) — clone and read them when a goal calls for deeper
grounding in agent-cognition theory.

| Hub | Source | Contents | Consult when |
|-----|--------|----------|--------------|
| **Ayoai-Research-Analyst** | `https://github.com/zkysar1/Ayoai-Research-Analyst` (PRIVATE — authenticated clone via `gh auth` or a read-scoped token; local path varies per machine) | OKF v0.1 knowledge bundle on autonomous-agent cognition: perception, consciousness / cognitive-architecture, memory (RAG / hierarchical / temporal), planning, learning and adaptation (reflection, self-improvement), ABC modeling, market research | Reasoning about improving self, the framework, or agent design |

Registered at the dev source of the promotion cycle (Mind-Mind → Claude-Mind →
ZDS-Mind) so the reference stays ecosystem-consistent and flows downstream. The
registration lives in committed files (this file, plus a Self-Evolution pointer
each agent adds to its own `self.md`) because `world/` and `meta/` are external
and gitignored — not reachable by a cloud clone.

## User Control Commands

| Command | Effect | Valid From |
|---------|--------|-----------|
| `/start <name>` | Create/resume agent in autonomous mode (default) | UNINITIALIZED, IDLE |
| `/start <name> --mode reader` | Create/resume agent in reader mode (read-only) | UNINITIALIZED, IDLE, RUNNING* |
| `/start <name> --mode assistant` | Create/resume agent in assistant mode (user-directed learning) | UNINITIALIZED, IDLE, RUNNING* |
| `/stop <agent-name> [--reader]` | Consolidate → drop to assistant (or reader with `--reader`) → IDLE. Agent name REQUIRED — bare `/stop` is refused. | RUNNING, IDLE |
| `/verify-learning` | Post-test verification | ANY |
| `/open-questions` | Show open questions | ANY |
| `/agent-completion-report` | Show what changed *(also agent-callable)* | ANY |
| `/backlog-report` | Sprint planning backlog *(also agent-callable)* | ANY |
| `/priority-review` | Priority dashboard — reorder aspirations *(also agent-callable)* | ANY |
| `/encode-session` | Run a structured 7-lane learning pass on the current chat session: encode tree/rb/guardrails/experience, file Maintain goals for inline work, re-probe blockers, surface discoveries, propose verify-learning checks (sq-018), and check meta + Self for evolution signals *(also agent-callable; chat-mode analogue of the autonomous loop's Phase 6.5 + Phase 8)* | IDLE (assistant) |

\*When started from RUNNING state, reader/assistant create an **observer session** that coexists
with the autonomous loop. Observer sessions do not write to agent-state, agent-mode, or
persona-active. See `/start` RUNNING branch and `core/config/conventions/session-state.md`.

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

Session-keyed agent binding (project root):

| File | Purpose | Set By |
|------|---------|--------|
| `.active-agent-<session_id>` | Binds a Claude Code session to an agent | /start |

Signal files (all in `agents/<agent>/session/`):

| File | Purpose | Set By |
|------|---------|--------|
| `agent-state` | "RUNNING" or "IDLE" | /start, /stop only |
| `agent-mode` | "reader", "assistant", or "autonomous" | /start, /stop |
| `persona-active` | "true" or "false" | /start, /stop, /boot |
| `stop-loop` | Allow exit (set after obligations complete) | /stop Phase -1.4 |
| `stop-requested` | Graceful stop signal (set immediately by /stop) | /stop |
| `iteration-checkpoint.json` | In-flight obligation tracker for graceful stop recovery | aspirations loop |
| `handoff.yaml` | Cross-session state | aspirations consolidation |
| `pending-agents.yaml` | Background agent tracking (stop hook Gate 2.5) | aspirations-execute Phase 4 |
| `background-jobs.yaml` | Long-running external process tracking | forged skills with background tasks |
| `watchdog-prev-state.json` | Agent-watchdog tick-mode probe state (snapshots; diffed by next tick) | agent-watchdog.py --tick (from iteration-close.sh) |
| `runner-token` | Framework-owned UUID4 (uniqueness identity); triple-written with running-session-id + latest-session-id at /start IDLE→RUNNING. Detects Claude Code SID reuse across windows (`--continue` / `--resume`) | /start IDLE Step 3 + UNINITIALIZED C8 |
| `recovery-failure-count` | Integer counter of consecutive _perform_recovery failures (circuit breaker; threshold 3) | recovery-gate.sh |
| `recovery-failed-permanent` | Set when recovery-failure-count ≥ 3; recovery-gate refuses further auto-retry. Cleared by /start --recover --force or successful recovery | recovery-gate.sh |

Other session signals (`loop-active`, `compact-checkpoint.yaml`, `context-reads.txt`, `pending-questions.yaml`, `aspirations-compact.json`): see `core/config/conventions/session-state.md`.

### Compact Checkpoint Protocol

PreCompact/SessionStart hooks manage encoding state across autocompact. Detail: `core/config/conventions/session-state.md`.

### Context Read Deduplication

Hooks prevent redundant file reads AND skill invocations between compaction cycles.
`PreToolUse[Read]` gates file re-reads; `PreToolUse[Skill]` gates skill re-invocations
(combined gate+record since `PostToolUse` does not fire for the Skill tool).
Detail: `core/config/conventions/session-state.md`.

## Available Skills

User control commands: see User Control Commands table above.

### Internal Skills (agent-only — invoked autonomously during RUNNING state)

| Skill | Purpose |
|-------|---------|
| Boot | Session entry point: status report + prime + handoff to aspirations loop |
| Prime | Context priming — load knowledge, guardrails, reasoning |
| Aspirations | Perpetual goal loop — the heartbeat (orchestrator, includes Phase 7.5 Completion Review) |
| *Aspirations Execute* | *Phase 4 goal execution, retrieval, verification, reconciliation* |
| *Aspirations Spark* | *Spark checks, sq-XXX handlers, immediate learning* |
| *Aspirations Strategic Scan* | *Periodic environmental scan — recurring goal outputs, knowledge frontier, portfolio health, intrinsic motivation* |
| *Aspirations State Update* | *State update protocol with tree encoding + Step 8.5 Actionable Findings Gate* |
| *Aspirations Consolidate* | *Session-end consolidation, encoding, handoff* |
| *Aspirations Evolve* | *Evolution engine, developmental stage, config tuning* |
| Create Aspiration | Self-driven aspiration creation |
| Curriculum Gates | Evaluate graduation gates and promote curriculum stages |
| Respond | Handle user messages — persona, knowledge search, directive routing |
| Review Hypotheses | Resolve hypotheses, learn from outcomes, accuracy stats |
| Reflect | ABC chains, violations, hierarchical reflection, strategy extraction |
| *Reflect On Outcome* | *Hypothesis ABC chains, execution pattern signatures, batch micro-hypothesis processing* |
| *Reflect On Self* | *Pattern synthesis, strategy extraction, confidence calibration* |
| *Reflect Maintain* | *Memory curation, active forgetting, aspiration grooming* |
| *Reflect Tree Update* | *Shared tree update protocol (propagate upward)* |
| Replay | Compressed review, reconsolidation, domain transfer |
| Research Topic | Build knowledge base via web research |
| Decompose | Break compound goals into primitives |
| Forge Skill | Create new skills from capability gaps |
| Tree | Knowledge tree operations: read, find, add, edit, set, decompose, maintain, stats, validate |

*(Forged skills created via /forge-skill appear here after creation — see world/forged-skills.yaml)*
