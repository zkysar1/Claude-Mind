# Mind

Give Claude Code a persistent mind. It sets its own goals, researches topics, forms predictions, learns from outcomes, and grows smarter across sessions. You tell it what domain to explore — it runs itself from there.

> **Status: Alpha** — The core loop works across multi-week autonomous sessions. APIs and file formats may change.

## Prerequisites

- **Python 3.10+ and PyYAML** — `pip install -r requirements.txt` (or `py -3 -m pip install -r requirements.txt` on Windows)
- **bash 4+** — already present on macOS / Linux; on Windows use Git for Windows (bundles bash 5.x)
- **Claude Code** — CLI, desktop app, or IDE extension
- **git** (optional) — enables per-iteration audit trail; framework runs without it (auto-detected)

`/start` runs `core/scripts/check-prerequisites.sh` automatically and prints a friendly error block listing anything missing. New deployment? See `SETUP.md` for the full 5-minute install walkthrough.

## Quick Start

```bash
git clone <this-repo>
cd <this-repo>
pip install -r requirements.txt
```

Then in Claude Code:

```
/start <agent-name>
```

Pick a short lowercase name (`teacher`, `alpha`, `pokemon-research`). If the name is invalid (uppercase, special characters, or a reserved word like `core`), the agent says so immediately — no time wasted on the rest of setup.

The agent then walks you through:

1. **Where to store shared data** — Two paths: one for collective knowledge ("world"), one for improvement strategies ("meta").
   > **Suggested default**: `./world` and `./meta` (inside the project root, alongside `core/`, `mind_api/`, `agents/`) — simplest single-machine / single-repo layout. For multi-machine or multi-repo sharing, point at a shared remote (NAS, OneDrive / SharePoint / Dropbox / iCloud) instead.
   > **`/start` never picks for you** — it suggests the default and waits for your explicit reply. Even in "auto mode", path selection is a non-skippable user-confirmation gate.
   > Use forward slashes on every platform, including Windows (`C:/Users/you/OneDrive/my-mind-world`). If you paste a Windows backslash path from Explorer, `/start` normalizes it for you.
2. **Permissions confirmation** — The agent shows you the exact `.claude/settings.local.json` rules it wants to add (read/write on your world + meta + project paths, plus the framework's constitutional safety baseline). One yes/no.
3. **What is this program about?** — The domain or purpose (e.g., "master competitive Pokemon strategy", "learn quantum computing", "analyze our codebase architecture")
4. **What should this agent focus on?** — The agent's role and specialization
5. **What should it work on first?** — Its initial aspiration and curriculum

That's it. The agent runs itself from here. It continues across sessions automatically — just reopen Claude Code and it picks up where it left off.

> **Just want knowledge access?** Use `/start <agent-name> --mode reader` for read-only access (opt-in safe floor), or `--mode assistant` to learn only when you teach it.

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
| `/start <name> --mode reader` | Read-only mode — access all accumulated knowledge without writing anything. Opt-in safe floor. |
| `/start <name> --mode assistant` | Assistant mode — learns when you teach it, writes when you ask, but doesn't self-direct. |
| `/stop <agent-name>` | Consolidate session and drop to assistant mode (reconciliation-ready). Agent name is REQUIRED. Pass `--reader` for read-only. |
| `/stop <agent-name> --reader` | Consolidate session and drop to reader mode (walking-away safe floor). |
| `/start <name>` (after a stop) | Resume the same agent in autonomous mode. Same command as creation — `/start` always takes an agent name. |
| `/start <name> --mode <mode>` | Resume after stopping and explicitly choose mode (reader / assistant / autonomous). |
| `/agent-completion-report` | See what the agent accomplished recently. |
| `/backlog-report` | See the agent's current task queue and priorities. |
| `/open-questions` | See decisions the agent logged for your review. |
| `/priority-review` | Review and reorder the agent's aspiration priorities. |
| `/verify-learning` | Run a diagnostic check on the agent's state. |
| `/encode-session` | After a productive chat in assistant mode, run a structured pass that captures insights into the knowledge tree, reasoning bank, and guardrails so the work doesn't evaporate when the window closes. |
| `/tree <subcommand>` | Knowledge tree operations: `read`, `find`, `add`, `edit`, `stats`, `validate`, and more. Always route tree edits through this — never edit `_tree.yaml` directly. |
| `/strategic-pulse` | Surface portfolio-shape patterns: tail consolidation pressure, work-class skew, aged or all-blocked aspirations. Auto-fires every 50 completed goals; invoke directly for an on-demand readout. |
| `/felt-sense-checkin` | Force a deep 7-lane self-audit: memory hygiene, insight curation, unblocks, forward backlog, `/verify-learning` gaps, meta tuning. Auto-fires every 75 completed goals. |
| `/fresh-eyes-review` | Periodic portfolio self-audit — assembles a briefing under `agents/<agent-name>/temp/`. Auto-fires every 25 completed goals; invoke directly when you want the agent to step back. |
| `/seed plant <dest>` | Plant (publish) this domain-free framework into another repo — grows a fresh, empty environment (applies generic-name transforms; no agents/world/learned state copied). |

### Three Modes

- **Reader** (safe floor) — Read-only access to everything the agent has learned. Ask questions, search the knowledge tree, view dashboards. No writes. Opt in via `/stop <agent-name> --reader` when you're walking away.
- **Assistant** (post-stop default) — Everything reader can do, plus the agent learns when you teach it. Say "remember this", "learn about X", or "research Y" and it writes to its knowledge base. It never self-initiates work.
- **Autonomous** — Full perpetual learner. The agent sets its own goals, executes them, reflects, and evolves. This is the original mode.

When stopped, you're in assistant mode — mark a missed goal, edit a tree node, or add a guardrail without a mode-switch ceremony. Run `/start <agent-name>` to resume autonomous, or `/stop <agent-name> --reader` next time for a read-only landing.

## Multiple Agents

Run `/start <name>` in separate Claude Code sessions to have multiple agents working on the same domain simultaneously. Each agent has its own identity, experience, and task queue, but they share collective knowledge — what one agent learns, the others can use.

Use `/start <other-name> --mode <mode>` to switch which agent a session controls.

## Shared Workspace

The agent stores shared data (knowledge, hypotheses, message board) at a location you choose during setup. This can be:
- A folder on a shared drive or NAS
- A cloud-synced folder (OneDrive, Dropbox, iCloud, etc.)
- A local directory (for single-machine use)

Multiple machines can point to the same shared folder. Agents communicate via a message board and all file changes are automatically versioned — browse `world/.history/` to see previous versions of any file, or check `world/changelog.jsonl` for an audit trail of everything that changed.

> **Heads-up if you use cloud-sync storage** — The framework writes JSONL state on nearly every goal step (aspirations, journal, pipeline, experience, reasoning bank, `.history/` snapshots). When your world/meta paths live inside a syncing folder, every write may trigger your antivirus AND the sync client AND antivirus again on the cache touch. On Windows + Defender this routinely pins `MsMpEng` at 100–200% CPU (multi-core) sustained. One-time fix in an **elevated** PowerShell:
> ```powershell
> Add-MpPreference -ExclusionPath '<WORLD_PATH parent folder>'
> Add-MpPreference -ExclusionPath '<this repo>'
> Add-MpPreference -ExclusionProcess 'claude.exe'
> Add-MpPreference -ExclusionExtension 'jsonl'
> ```
> macOS / Linux: same idea — exclude the framework's working paths from your AV product's real-time scanner. The trade-off is reasonable: the AV stops deep-scanning plaintext logs the framework constantly rewrites, while the rest of your disk stays protected.

## Project Layout

```
.claude/         # Claude Code config (hooks, skills, rules)
core/            # Framework: scripts, conventions, config schemas
  scripts/       #   All shell + Python helpers (no inline JSONL editing)
  config/        #   Convention files, mode profiles, gate registry
mind_api/        # Local agent-API daemon: source (mind_api/src/), runtime state
                 #   (mind_api/state/, gitignored).
agents/          # Parent directory holding all per-agent state (one subdir per agent)
  <agent-name>/  #   session, journal, experience, aspirations, curriculum
.env.example     # Template for .env.local (secrets — gitignored)
CLAUDE.md        # Full architecture reference + skill catalog
LICENSE          # MIT
README.md        # This file
SETUP.md         # 5-minute install walkthrough
pytest.ini       # Pytest config
requirements.txt # Python dependencies
```

External (configured per-agent in `agents/<agent-name>/local-paths.conf`, can point to a shared drive / NAS / cloud-sync folder):

```
world/           # Collective domain state (shared across all agents)
meta/            # Agent-editable strategies + meta-knowledge
```

## Removing Data

Each agent is a self-contained directory. To remove data, delete the relevant directory:

| What to remove | What to delete |
|----------------|---------------|
| One agent | Delete `agents/<agent-name>/` |
| Shared knowledge | Delete the world directory at its external path |
| Improvement strategies | Delete the meta directory at its external path |
| All local agents | Delete the `agents/` directory |

If agents created forged skills (check `world/forged-skills.yaml`), those live in `.claude/skills/` and should be manually removed.

## Going Deeper

- **`CLAUDE.md`** — Full architecture reference, file formats, conventions, and the complete skill catalog. This is the agent's own instruction manual.
- **`core/config/conventions/`** — Detailed documentation for each subsystem (aspirations, pipeline, knowledge tree, etc.)
- **`core/config/architecture-reference.md`** — Skill chaining map and self-evolution loop

## License

MIT — see [LICENSE](LICENSE).
