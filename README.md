# Claude-Mind

Give Claude Code a persistent mind. It sets its own goals, researches topics, forms predictions, learns from outcomes, and grows smarter across sessions. You tell it what domain to explore — it runs itself from there.

> **Status: Alpha** — The core loop works across multi-week autonomous sessions. APIs and file formats may change.

## Prerequisites

- **Python 3 + PyYAML** — `pip install pyyaml`
- **Claude Code** — CLI, desktop app, or IDE extension

## Quick Start

```bash
git clone <this-repo>
cd <this-repo>
```

Then in Claude Code:

```
/start alpha
```

The agent will walk you through setup:

1. **Where to store shared data** — Point it to a folder for collective knowledge (can be a shared drive, NAS, OneDrive, or a local directory). Then another folder for improvement strategies.
2. **What is this program about?** — The domain or purpose (e.g., "master competitive Pokemon strategy", "learn quantum computing", "analyze our codebase architecture")
3. **What should this agent focus on?** — The agent's role and specialization
4. **What should it work on first?** — Its initial aspiration and curriculum

That's it. The agent runs itself from here. It continues across sessions automatically — just reopen Claude Code and it picks up where it left off.

> **Just want knowledge access?** Use `/start alpha --mode reader` for read-only access, or `--mode assistant` to learn only when you teach it.

## What to Expect

The agent works in a continuous loop: pick a goal, execute it, reflect on what happened, encode what it learned, then pick the next goal. It never stops unless you tell it to.

**In the first session**, it researches your domain, builds initial knowledge, and starts forming hypotheses — predictions it can test later.

**Over multiple sessions**, it accumulates real state: a knowledge tree that grows and restructures itself, a bank of learned reasoning patterns, safety guardrails it discovered from mistakes, and hypotheses that get confirmed or corrected. Session 50 builds on everything from sessions 1-49.

**Over weeks**, it starts evolving structurally. It detects capability gaps and creates new skills to fill them. It tunes its own goal-selection weights and reflection strategies. The agent you have after a month is fundamentally different from the one you started with.

You don't need to do anything while it runs. But you can chat with it anytime — ask it what it's learned, point it toward new topics, or give it corrections. It incorporates your feedback immediately.

## Commands

| Command | What it does |
|---------|-------------|
| `/start <name>` | Create a new agent or resume in autonomous mode (full perpetual loop). |
| `/start <name> --mode reader` | Read-only mode — access all accumulated knowledge without writing anything. Safe default. |
| `/start <name> --mode assistant` | Assistant mode — learns when you teach it, writes when you ask, but doesn't self-direct. |
| `/stop` | Drop to reader mode. Consolidates session state and returns to safe baseline. |
| `/start` | Resume in current mode after stopping. |
| `/start --mode <mode>` | Switch mode and resume. |
| `/agent-completion-report` | See what the agent accomplished recently. |
| `/backlog-report` | See the agent's current task queue and priorities. |
| `/open-questions` | See decisions the agent logged for your review. |
| `/verify-learning` | Run a diagnostic check on the agent's state. |

### Three Modes

- **Reader** (baseline) — Read-only access to everything the agent has learned. Ask questions, search the knowledge tree, view dashboards. No writes. This is what you get after `/stop`.
- **Assistant** — Everything reader can do, plus the agent learns when you teach it. Say "remember this", "learn about X", or "research Y" and it writes to its knowledge base. It never self-initiates work.
- **Autonomous** — Full perpetual learner. The agent sets its own goals, executes them, reflects, and evolves. This is the original mode.

When stopped, you're in reader mode — chat normally, ask about knowledge, or do regular coding tasks. Run `/start` or `/start --mode assistant` to upgrade.

## Multiple Agents

Run `/start <name>` in separate Claude Code sessions to have multiple agents working on the same domain simultaneously. Each agent has its own identity, experience, and task queue, but they share collective knowledge — what one agent learns, the others can use.

Use `/start <other-name> --mode <mode>` to switch which agent a session controls.

## Shared Workspace

The agent stores shared data (knowledge, hypotheses, message board) at a location you choose during setup. This can be:
- A folder on a shared drive or NAS
- A OneDrive or SharePoint folder
- A local directory (for single-machine use)

Multiple machines can point to the same shared folder. Agents communicate via a message board and all file changes are automatically versioned — browse `world/.history/` to see previous versions of any file, or check `world/changelog.jsonl` for an audit trail of everything that changed.

## Removing Data

Each agent is a self-contained directory. To remove data, delete the relevant directory:

| What to remove | What to delete |
|----------------|---------------|
| One agent | Delete `<agent>/` (e.g., `rm -rf alpha/`) |
| Shared knowledge | Delete the world directory at its external path |
| Improvement strategies | Delete the meta directory at its external path |
| All local agents | Delete all agent directories from the project root |

If an agent created forged skills (check `<agent>/forged-skills.yaml`), those live in `.claude/skills/` and should be manually removed.

## Going Deeper

- **`CLAUDE.md`** — Full architecture reference, file formats, conventions, and the complete skill catalog. This is the agent's own instruction manual.
- **`core/config/conventions/`** — Detailed documentation for each subsystem (aspirations, pipeline, knowledge tree, etc.)
- **`core/config/architecture-reference.md`** — Skill chaining map and self-evolution loop

## License

MIT — see [LICENSE](LICENSE).
