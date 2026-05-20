# SETUP

Five-minute install for a brand-new agent. If you've already cloned a working
deployment and just want to add an agent, you can skip straight to step 4.

## What you're setting up

You're copying two folders (`core/` + `.claude/`) into a fresh directory and
launching Claude Code there. The agent's memory and knowledge will live in
two other folders you choose. After this, type one slash-command and the
agent walks you through the rest.

## 1. Prerequisites

Install these first. The framework checks for them on startup and refuses to
proceed if anything is missing — but it's friendlier to install them up front.

| What | Why | How |
|------|-----|-----|
| **Python 3.8 or newer** | The framework's scripts and the local helper daemon are Python. | https://www.python.org/downloads/ (on Windows, check "Add to PATH" during install) |
| **PyYAML** | Reads the framework's config files. | `pip install pyyaml` (or `py -3 -m pip install pyyaml` on Windows) |
| **bash 4 or newer** | Shell scripts. Already present on macOS and Linux. | On Windows: install **Git for Windows** (which bundles bash 5.x). Same install gives you git, which is optional but helpful. |
| **Claude Code** | The UI you'll type slash-commands into. | https://claude.ai/code or the desktop app |

Optional but nice:
- **git** — enables the framework's per-iteration audit trail (commits each
  goal). Without it, learning still works fine; you just lose the per-iteration
  commit log. The framework auto-detects whether git is installed and skips
  the audit step when absent.
- **psutil** (Python package) — enables richer process inspection in the
  agent-watchdog. Degrades gracefully if missing. `pip install psutil`.

You can verify the prereqs at any time with:
```bash
bash core/scripts/check-prerequisites.sh
```

## 2. Pick where to put things

You'll be asked for two paths during agent setup. Pick them now so you have
them ready:

- **A "world" directory** — where shared knowledge lives. This can be:
  - A folder on a shared drive or NAS (multiple machines can point to it)
  - A OneDrive / SharePoint / Dropbox folder (synced across your machines)
  - A local directory if you'll only use one machine

  Example: `C:/Users/you/OneDrive/my-mind-world` or `/Users/you/Documents/my-mind-world`

- **A "meta" directory** — where the agent's improvement strategies live.
  Same options. Usually next to the world directory for convenience.

  Example: `C:/Users/you/OneDrive/my-mind-meta`

> **Use forward slashes on every platform**, including Windows. Bash sources
> the path file at runtime, and backslashes get interpreted as escape sequences.
> If you copy a path from Explorer's address bar (`C:\Users\...`), convert
> the `\` to `/` before pasting — or just paste it; `/start` will normalize.

Both directories will be created on first `/start` if they don't exist yet.

## 3. Copy the framework into a fresh directory

Pick where you want to keep the framework itself. This is a different folder
from the world/meta directories — it holds the code, not the agent's memory.

```bash
mkdir my-mind-project
cd my-mind-project
```

Copy these two folders from the source repo into your fresh directory:
- `core/`
- `.claude/`

That's the whole framework. You can also copy `README.md`, `requirements.txt`,
and this `SETUP.md` if you want them handy — but `core/` + `.claude/` are
the only things that have to be there.

> **About `.claude/settings.local.json`** — this file is per-machine and
> per-deployment. If you have one from another machine, copy it too; if not,
> it'll be created automatically.

Run the prereq check once more from the new directory to make sure everything
landed:
```bash
bash core/scripts/check-prerequisites.sh
```

## 4. Launch Claude Code and start the agent

In your fresh `my-mind-project` directory:

```bash
claude
```

Then type:
```
/start <pick-a-name>
```

A good name is short, lowercase, no spaces. Examples: `teacher`, `pokemon`,
`alpha`, `quantum`. The name becomes the agent's identity and the folder
where its private state lives.

The agent will walk you through:

1. **World and meta paths** — paste the paths you picked in step 2.
2. **What is this program about?** — one or two sentences. Example:
   *"Master kindergarten math instruction — explore curriculum design,
   common student misconceptions, and effective lesson sequencing."*
3. **What should this agent focus on?** — the agent's specific role.
   For a single-agent deployment this is often the same as the program.
   For multi-agent setups, agents specialize.
4. **First aspiration** — a high-level goal to start with. The agent
   decomposes this into smaller goals as it goes.

When the agent says it's ready, it's ready. It'll start working on its first
goal immediately.

## 5. Day-to-day use

- **Resume**: Reopen Claude Code in the project directory and type
  `/start <name>`. The agent picks up where it left off.
- **Pause**: Type `/stop <name>`. The agent finishes its current goal,
  consolidates what it learned, and drops to assistant mode (you can still
  ask it questions and give corrections).
- **Walk away**: Type `/stop <name> --reader` for read-only safe mode.
- **Talk to it**: At any time, just type a message. The agent responds,
  applies any directives, then resumes work.
- **Check progress**: `/agent-completion-report` for recent activity,
  `/backlog-report` for what's queued, `/open-questions` for decisions
  it logged for you.

See `README.md` for the full command reference and architecture overview.

## 6. Adding a second agent (later)

Each agent is one directory. To add another:

```
/start <second-name>
```

It'll ask you for its specialization and first aspiration. Both agents share
the same world/meta — what one learns, the other can use.

You can have many agents working on the same domain simultaneously, each in
its own Claude Code window. They coordinate via a shared message board.

## 7. Removing or resetting

- **Reset one agent**: delete its directory (e.g., `rm -rf teacher/`). Next
  `/start teacher` creates it fresh.
- **Reset all shared knowledge**: delete the world directory at its external
  path. Then on next `/start`, the agent will re-init the world.
- **Reset improvement strategies**: delete the meta directory.
- **Complete uninstall**: delete the entire project directory + the world +
  meta external directories.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/start` says "prerequisites not met" with a list | Missing Python / PyYAML / bash | Install the listed item; re-run `/start`. |
| `/start` says "AGENT_WRITE_PATH not configured" | First-time setup interrupted | Re-run `/start <name>` and complete the path prompts. |
| Agent never says anything after `/start` | Daemon may not have spawned | Check `core/logs/` for errors. Try `/stop <name>` then `/start <name>`. |
| `pip install pyyaml` fails on Windows | `py` launcher needed | Run `py -3 -m pip install pyyaml` instead. |
| "permission denied" on shell scripts | Files copied without execute bit | `chmod +x core/scripts/*.sh` |

Anything else: read `core/logs/` for the most recent error, then `CLAUDE.md`
for the full framework reference.
