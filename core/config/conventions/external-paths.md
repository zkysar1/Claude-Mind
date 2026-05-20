# External Path Configuration

## Overview

`world/` and `meta/` live at user-supplied external paths (shared drive, NAS, OneDrive, etc.). Each agent stores its own path configuration in `agents/<agent>/local-paths.conf`. The local git repo contains only `core/`, `.claude/`, `mind_api/`, and `agents/<agent>/` directories.

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

1. **Environment variable**: `MIND_WORLD` / `MIND_META` (for CI/testing)
2. **Agent config file**: `agents/<agent>/local-paths.conf` WORLD_PATH / META_PATH
3. **Auto-detect**: If `MIND_AGENT` is not set but agent directories with `local-paths.conf` exist, use the first available config. This covers hooks and background processes that lack the env var.
4. **Fail loud**: If no config can be resolved, WORLD_DIR/META_DIR are unset (bash) or `None` (Python). Plan v1 step 0.1 (2026-05-19) removed the `PROJECT_ROOT/world|meta` fallback to prevent silent root-cruft creation. Module-level callers should invoke `assert_world_dir()` / `assert_meta_dir()` to fail with a clear diagnostic.

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
