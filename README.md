# Claude-Mind

**A Claude "skill-system" that aspires to grow knowledge and judgement in any domain.**

Give Claude Code a persistent mind — goals it sets for itself, knowledge it accumulates across sessions, hypotheses it forms and tests, and skills it builds when it finds gaps. Clone the repo, type `/start`, tell it what domain to explore, and it runs itself from there.

No code to write. Everything is YAML, JSONL, and Markdown files that Claude reads and updates autonomously.

> **Status: Alpha** — Actively developed. The core loop works and has been tested across multi-week autonomous sessions, but APIs and file formats may change.

## Prerequisites

- **Python 3 + PyYAML** — `pip install pyyaml` (all other dependencies are stdlib)
- **Claude Code CLI** — the agent runs as a Claude Code session
- **Optional: `.env.local`** — credentials for external integrations (API keys, SSH, email). Copy `.env.example` to `.env.local` and fill in values. This file is gitignored and survives factory reset.

## Getting Started

```bash
git clone <this-repo>
cd <this-repo>
# launch Claude Code, then:
/start
```

1. The agent asks you to define its **Self** — what is this agent for? What domain should it explore?
2. The agent asks for your **initial aspirations** — or generates them from your Self description
3. `init-mind.sh` creates the `mind/` directory and seeds it from framework definitions
4. The autonomous learning loop begins — researching, hypothesizing, reflecting, evolving

That's it. The agent runs itself from here. It continues across sessions automatically.

## What Makes This Different

Most agent frameworks run once and forget. This one compounds.

- **Persistent across sessions** — The agent resumes where it left off with full memory of what it learned, what it tried, and what failed. Session 50 builds on everything from sessions 1-49.
- **Prediction-driven, not just accumulation** — It doesn't just collect facts. It forms hypotheses with confidence levels, tracks outcomes, and calibrates from being wrong. The hypothesis pipeline is the core learning mechanism.
- **Self-evolving** — When the agent detects a recurring capability gap, it forges a new skill to fill it. The agent you have after months of running is structurally different from the one you started with — it has skills that didn't exist in the original repo.
- **Pure data, no code** — The entire agent is configuration and state files. Claude reads and writes them autonomously. You never touch code — just define what domain to explore and watch.
- **Clean separation of learned vs framework** — Everything the agent learns lives in `mind/`. The framework that defines *how* it learns lives in `core/` and `.claude/`. `rm -rf mind/` is a full factory reset — the framework stays, the learned state goes. Fork the framework for a new domain without carrying over old knowledge.

## How It Works

```
  /start
    │
    ▼
┌─────────────────────────────────────────────┐
│  Define Self  →  "What am I for?"           │
│  Seed Aspirations  →  initial goals         │
└──────────────────┬──────────────────────────┘
                   │
    ┌──────────────▼──────────────┐
    │     Aspirations Loop        │◄──────────────────────┐
    │  (the perpetual heartbeat)  │                       │
    └──────────────┬──────────────┘                       │
                   │                                      │
    ┌──────────────▼──────────────┐                       │
    │  Select Goal (scored +      │                       │
    │  exploration noise)         │                       │
    └──────────────┬──────────────┘                       │
                   │                                      │
    ┌──────────────▼──────────────┐                       │
    │  Execute  →  research,      │                       │
    │  test, build, investigate   │                       │
    └──────────────┬──────────────┘                       │
                   │                                      │
    ┌──────────────▼──────────────┐                       │
    │  Reflect  →  hypotheses,    │                       │
    │  patterns, calibration      │                       │
    └──────────────┬──────────────┘                       │
                   │                                      │
    ┌──────────────▼──────────────┐                       │
    │  Encode  →  knowledge tree, │                       │
    │  reasoning bank, guardrails │                       │
    └──────────────┬──────────────┘                       │
                   │                                      │
    ┌──────────────▼──────────────┐                       │
    │  Spark  →  new questions,   │                       │
    │  new goals, evolution check │──────────────────────►┘
    └─────────────────────────────┘
```

1. **Bootstrap** — Define the agent's Self and seed initial aspirations
2. **Research** — Build foundational knowledge in the memory tree via web research
3. **Hypothesize** — Form testable predictions with confidence levels
4. **Track** — Monitor outcomes, resolve hypotheses as confirmed or disconfirmed
5. **Learn** — Reflect on outcomes, extract patterns, update reasoning and guardrails
6. **Evolve** — Spark checks generate new goals. Decompose complex goals. Forge new skills. Adjust strategy.
7. **Repeat** — The loop never stops. Completion of one thing seeds the next.

## What It Looks Like

After a few sessions, the agent's `mind/` directory contains real, accumulated state:

```
mind/
  self.md                    # "I exist to master competitive Pokémon strategy"
  aspirations.jsonl          # 4 active aspirations, 12 goals across them
  pipeline.jsonl             # 8 hypotheses: 3 active, 2 resolved, 3 discovered
  knowledge/tree/            # 23 nodes across 4 domains
  reasoning-bank.jsonl       # 11 learned reasoning entries
  guardrails.jsonl           # 6 safety rules discovered from mistakes
  journal/                   # 9 daily entries documenting what happened
  developmental-stage.yaml   # Stage: "developing" (was "exploring" on day 1)
```

Here's what a hypothesis lifecycle looks like in `pipeline.jsonl`:

```
Session 3:  hypothesis formed — "Weather teams are more effective in doubles
            than singles because both slots benefit from weather"
            confidence: 0.70, horizon: short, stage: discovered

Session 5:  moved to active — designed test criteria, began tracking matches

Session 8:  resolved CONFIRMED — 68% win rate in doubles vs 41% in singles
            surprise: 2 (low — matched expectation)
            → encoded findings to knowledge tree node "weather-strategy"
            → reflected: extracted reasoning entry rb-014
            → updated developmental stage for "team-building" category
```

The knowledge tree, reasoning bank, guardrails, and forged skills all grow the same way — incrementally, from real outcomes, across sessions.

## Core Systems

| System | What it does |
|--------|-------------|
| **Goal engine** | Perpetual loop that generates, decomposes, executes, and evolves goals. Completion of one goal seeds the next. |
| **Knowledge tree** | Dynamic tree that grows as the agent learns. Nodes split when too large, sprout children for subtopics, merge when redundant, prune when stale. |
| **Hypothesis pipeline** | Forms testable predictions, assigns confidence, tracks outcomes, learns from surprises. `discovered → evaluating → active → resolved → archived` |
| **Memory pipeline** | Observations flow through staged encoding: sensory buffer → working memory → encoding gate → consolidation → long-term tree. Filtered by novelty, surprise, goal relevance. |
| **Reflection engine** | After outcomes resolve, reflects on expected vs. actual, extracts patterns and strategies, replays past outcomes to strengthen or revise knowledge. |
| **Pattern signatures** | Distinguishes situations that look similar but require different responses. Completes partial cues from past experience. |
| **Skill forging** | Detects recurring capability gaps and creates new skill definitions to fill them. Forged skills persist across sessions. |
| **Developmental stages** | Competence-based maturity model (exploring → developing → applying → mastering) that adjusts exploration vs. exploitation. |

## Controlling the Agent

| Command | What it does |
|---------|-------------|
| `/start` | Initialize or resume the autonomous loop |
| `/stop` | Stop the loop — chat normally with full knowledge access |
| `/reset` | Factory reset — wipe all learned state and forged skills |
| `/escapePersona` | Disable agent persona, act as standard Claude assistant |
| `/enterPersona` | Re-enable agent persona and knowledge tree |
| `/verify-learning` | Post-test verification — check agent state against checklist |
| `/completion-report` | Show what changed since last status report |
| `/backlog-report` | Sprint planning backlog as markdown |

When stopped, you can chat normally — ask the agent to review its knowledge, explain what it's learned, restructure strategy, or do regular coding tasks. Run `/start` to resume the loop.

## Architecture

```
core/                 # Shareable cognitive framework (the "skill-system")
  config/             # Framework definitions (18 files — immutable)
    conventions/      # On-demand reference docs (17 files)
  scripts/            # Utility scripts (100 shell + 25 Python)

.claude/
  skills/             # 35 base skill definitions (+ forged skills created by agent)
  rules/              # Behavioral rules

mind/                 # All mutable agent state (rm -rf to reset)
  self.md             # Agent's core purpose
  aspirations.jsonl   # Goals and aspirations
  pipeline.jsonl      # Hypothesis lifecycle
  knowledge/tree/     # Dynamic knowledge tree
  session/            # Ephemeral session state
  ...                 # + reasoning bank, guardrails, journal, experiences, etc.
```

### Framework vs State

- **`core/`** — The cognitive framework. Defines *how* the agent learns. Immutable during operation. Copy `core/` + `.claude/` + `CLAUDE.md` into any project to bootstrap a new agent.
- **`mind/`** — Everything the agent learns, discovers, and tracks. Mutable. Domain-specific. `rm -rf mind/` is a full factory reset.

### Key Design Principles

- **No terminal state** — Completion of one thing seeds the next. The loop runs until you say `/stop`.
- **Manager, not intern** — The agent makes decisions and continues. It logs significant calls for your review but never blocks waiting for permission.
- **Autocompact-resilient** — Long sessions survive context compression. Hooks checkpoint state before compaction and restore it after.
- **Script-mediated data** — All JSONL stores are accessed through scripts, never directly by the LLM. This keeps data consistent and validated.

## Credentials

The agent can use external services (APIs, SSH, email) via a simple secrets system:

1. Copy `.env.example` to `.env.local`
2. Fill in the values you need
3. Scripts access credentials via `core/scripts/env-read.sh`

`.env.local` is gitignored and survives factory reset. If a skill needs a credential that's missing, it creates a user-action goal and falls back gracefully.

## Factory Reset

Run `/reset` in Claude Code, or manually:

```bash
bash core/scripts/factory-reset.sh
```

This wipes `mind/` (all learned state) and removes forged skill directories. Framework files (`.claude/`, `core/`) are unchanged.

## Forking for a New Domain

The framework is domain-agnostic. To adapt it:

1. **Clone or fork** this repo
2. **Optionally customize** entry points:
   - `core/config/aspirations-initial.jsonl` — bootstrap aspirations (what the agent starts working on)
   - `core/config/profile.yaml` — system identity and strategy parameters
   - `core/config/spark-questions.yaml` — seed questions that drive curiosity
3. **Run `/start`** — the agent asks for its Self and begins learning

No code changes required. The agent builds everything else — knowledge tree, hypotheses, patterns, reasoning entries, guardrails, and forged skills — through its own learning loop.

## License

MIT — see [LICENSE](LICENSE).
