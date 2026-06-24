# External Path Configuration

## Overview

`world/` and `meta/` are *storage roots* resolved at runtime. There are two
mechanisms, in precedence order:

- **`.mind-data/` (default, asp-330 convention)** — a single gitignored dir at
  `PROJECT_ROOT/.mind-data/` holding `world/` and `meta/` together. Local,
  single-machine, auto-detected, zero config. This is what `/start` creates by
  default for a new agent.
- **`local-paths.conf` (legacy / external)** — `agents/<agent>/local-paths.conf`
  pointing WORLD_PATH / META_PATH at user-supplied EXTERNAL paths (shared drive,
  NAS, OneDrive). Use this for multi-repo / multi-machine collaboration. It is no
  longer the default but is fully supported and is the ONLY mechanism for
  external/shared storage.

The local git repo itself contains only `core/`, `.claude/`, `mind_api/`, and
`agents/<agent>/` directories; both storage mechanisms keep `world/` and `meta/`
out of git (`.mind-data/` is gitignored; external paths live outside the repo).

## `.mind-data/` Local Storage (Default Convention)

`PROJECT_ROOT/.mind-data/` is the standard local storage root (asp-330
".mind-data/ Universal Storage Architecture"). Layout:

```
PROJECT_ROOT/
  .mind-data/                 (gitignored — never committed)
    world/                    (collective domain state — resolves as world/)
    meta/                     (improvement strategies — resolves as meta/)
    .env.local                (OPTIONAL — WORLD_PATH/META_PATH overrides, same
                               key=value format as local-paths.conf)
    agents/                   (RESERVED — populated only by the M4 `--agent-dirs`
                               migration; agents otherwise resolve at
                               PROJECT_ROOT/agents/ via AGENTS_PARENT_DIR)
```

Properties:
- **Auto-detected**: `_paths.py` / `_paths.sh` / `agent_paths.py` resolve
  `world -> .mind-data/world`, `meta -> .mind-data/meta` whenever the dir exists
  (see Path Resolution Priority tiers 2-3). No `local-paths.conf` needed.
- **Gitignored**: `/.mind-data/` in `.gitignore` — it mirrors one machine's full
  knowledge tree + meta strategies and must never be committed.
- **Override**: `.mind-data/.env.local` may set WORLD_PATH/META_PATH to point a
  tier elsewhere while keeping the rest under `.mind-data/`.
- **Created by `/start`**: the UNINITIALIZED flow's suggested default is
  `.mind-data/world` + `.mind-data/meta` (B1/B4); B3/B6 `mkdir -p` them on accept.
- **Migration**: an existing external-conf setup is moved into `.mind-data/` by
  `core/scripts/migrate-to-mind-data.sh` (asp-330 M4) — rsyncs WORLD_PATH ->
  .mind-data/world and META_PATH -> .mind-data/meta, writes
  `.mind-data/.env.local` with `STORAGE_BACKEND=local`, and backs up
  local-paths.conf to `.bak`.

`.mind-data/agents/` is NOT created on first boot — current path resolution keeps
agent dirs at `PROJECT_ROOT/agents/` (`AGENTS_PARENT_DIR`); only the M4
`--agent-dirs` migration relocates them under `.mind-data/`.

## Config File: `agents/<agent>/local-paths.conf`

```bash
# Paths to external world and meta directories
# Written by /start — edit manually to change locations
WORLD_PATH=C:/Users/Shared/claude-mind/world
META_PATH=C:/Users/Shared/claude-mind/meta
```

- Location: inside each agent's directory (gitignored via `**/local-paths.conf`)
- Format: shell-sourceable key=value (use forward slashes on all platforms)
- Created by `/start` during first boot (Phase B)
- Each agent can point to different world/meta locations

## Path Resolution Priority

The single source of truth is `_resolve_tier()` in `core/scripts/_paths.py`
(mirrored in `_paths.sh` and `mind_api/src/agent_paths.py`). For each of
WORLD_PATH / META_PATH the chain is (asp-330 M1, g-330-01):

1. **Environment variable**: `MIND_WORLD` / `MIND_META` (for CI/testing)
2. **`.mind-data/.env.local`**: WORLD_PATH / META_PATH keys — only when
   `PROJECT_ROOT/.mind-data/` exists (same key=value format as local-paths.conf)
3. **`.mind-data/{world,meta}` bare default**: only when `PROJECT_ROOT/.mind-data/`
   exists — `world -> .mind-data/world`, `meta -> .mind-data/meta`
4. **Agent config file** (legacy/external): `agents/<agent>/local-paths.conf`
   WORLD_PATH / META_PATH. Resolution of WHICH conf: `MIND_AGENT` names the
   agent; if unset, the first available `*/local-paths.conf` is used (covers
   hooks and background processes that lack the env var).
5. **Fail loud**: If no tier resolves, WORLD_DIR/META_DIR are unset (bash) or
   `None` (Python). Plan v1 step 0.1 (2026-05-19) removed the
   `PROJECT_ROOT/world|meta` fallback to prevent silent root-cruft creation.
   Module-level callers should invoke `assert_world_dir()` / `assert_meta_dir()`
   to fail with a clear diagnostic (guard-551).

**Key consequence**: when `.mind-data/` exists, tiers 2-3 OVERRIDE the
local-paths.conf tier (4). A repo WITHOUT `.mind-data/` keeps the legacy
env -> local-paths.conf -> None chain byte-for-byte, so existing
external-conf agents are unaffected.

## Resolution in Scripts

### Bash (`_paths.sh`)
```bash
# agent_dir() helper from _paths.sh resolves to PROJECT_ROOT/agents/<name>
if [ -n "$AGENT_NAME" ] && [ -f "$(agent_dir "$AGENT_NAME")/local-paths.conf" ]; then
    source "$(agent_dir "$AGENT_NAME")/local-paths.conf"
else
    # MIND_AGENT unset — use first available conf (hooks don't have the env var)
    for _CONF in "$(agents_root)"/*/local-paths.conf; do
        [ -f "$_CONF" ] && source "$_CONF" && break
    done
fi
WORLD_DIR="${MIND_WORLD:-${WORLD_PATH:-}}"  # empty if unconfigured (no fallback)
META_DIR="${MIND_META:-${META_PATH:-}}"
```

### Python (`_paths.py`)
```python
def _read_local_paths():
    agent = os.environ.get("MIND_AGENT", "")
    if agent:
        conf = agent_dir(agent) / "local-paths.conf"
        if conf.exists():
            return _parse_conf(conf)
        # fall through to first-available
    # MIND_AGENT unset OR named a nonexistent agent — use first available conf
    for conf in enumerate_agent_confs():
        return _parse_conf(conf)
    return {}
```

WORLD_DIR and META_DIR resolve to `None` (Python) or empty string (bash) when no
config is reachable — module-level constants like `WORLD_DIR / "foo.jsonl"`
TypeError at construction site, surfacing the missing config loudly instead of
silently writing to `PROJECT_ROOT/world/foo.jsonl`.

## /start Flow (First Boot)

When `/start <name>` runs and the agent is new:
1. **Phase A**: Validate agent name, bind to session, create `<agent>/` directory
2. **Phase B** (if `agents/<agent>/local-paths.conf` does not exist): Ask for world path, validate, ask for meta path, validate, write `agents/<agent>/local-paths.conf`, add permissions to `settings.local.json`
3. **Phase C**: Ask for program, aspirations, curriculum, init world/meta/agent, start loop

Validation:
- **Empty directory**: fresh setup (run `init-world.sh` / `init-meta.sh`)
- **Populated directory**: reuse (confirm existing files)
- **Not writable / doesn't exist**: ask for different path

Permissions for external paths are added to `.claude/settings.local.json` (with user confirmation):
- Read/Write/Edit for `{world_path}/*`
- Read/Write/Edit for `{meta_path}/*`

## Local Repo Structure

```
project-root/
  core/                  — Framework (git-tracked)
  .claude/               — Skills, rules, settings (git-tracked)
  mind_api/              — Local agent-API daemon (git-tracked source + gitignored state)
  agents/                — Parent dir holding all agent dirs (Phase 2.5.D, 2026-05-19)
    alpha/               — Agent private state (mostly gitignored)
      local-paths.conf   — This agent's external path config (gitignored)
      self.md            — Agent identity
      ...
    beta/                — Another agent (can point to different paths)
      local-paths.conf
      ...
  CLAUDE.md              — Instructions (git-tracked)
```

## Shared Location Structure

```
/shared/claude-mind/
  world/              — Collective domain knowledge
    knowledge/tree/   — Browseable by office workers
    board/            — Message board channels
    .history/         — File version history
    changelog.jsonl   — Activity audit trail
  meta/               — Domain-agnostic improvement strategies
```

## Removing Data

Each agent is self-contained. To remove:
- **One agent**: Delete `<agent>/` — removes all state including path config
- **Shared knowledge**: Delete the world directory at its external path
- **Improvement strategies**: Delete the meta directory at its external path

Forged skills in `.claude/skills/` are shared — check `world/forged-skills.yaml` before deleting. Companion domain scripts live in `world/scripts/`.

## Path Format

Use **forward slashes** on all platforms:
- Good: `C:/Users/Shared/claude-mind/world`
- Bad: `C:\Users\Shared\claude-mind\world` (backslashes are escape sequences when bash sources the file)

Python handles both slash styles, but bash does not. Forward slashes work everywhere.

## LLM Direct Tool Calls

When skill pseudocode says `Read meta/foo.yaml` or `Edit world/bar.yaml`, the LLM must
resolve the virtual prefix to the configured external path — NOT derive it from directory
structure or sibling relationships.

Resolution steps:
1. Read `agents/<agent>/local-paths.conf` (or recall values from earlier in the session)
2. Map the virtual prefix:
   - `meta/foo.yaml` → `{META_PATH}/foo.yaml`
   - `world/bar.yaml` → `{WORLD_PATH}/bar.yaml`
3. Never assume `meta/` is a child or sibling of the world directory

Bash scripts (`meta-set.sh`, `retrieve.sh`, etc.) resolve paths automatically via
`_paths.sh` — no manual resolution needed when calling scripts.

Full rule: `.claude/rules/path-resolution.md`
