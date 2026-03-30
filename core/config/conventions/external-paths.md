# External Path Configuration

## Overview

`world/` and `meta/` live at user-supplied external paths (shared drive, NAS, OneDrive, etc.). Each agent stores its own path configuration in `<agent>/local-paths.conf`. The local git repo contains only `core/`, `.claude/`, and `<agent>/` directories.

## Config File: `<agent>/local-paths.conf`

```bash
# Paths to external world and meta directories
# Written by /start — edit manually to change locations
WORLD_PATH=C:/Users/Shared/ayoai/world
META_PATH=C:/Users/Shared/ayoai/meta
```

- Location: inside each agent's directory (gitignored via `*/local-paths.conf`)
- Format: shell-sourceable key=value (use forward slashes on all platforms)
- Created by `/start` during first boot (Phase B)
- Each agent can point to different world/meta locations

## Path Resolution Priority

1. **Environment variable**: `AYOAI_WORLD` / `AYOAI_META` (for CI/testing)
2. **Agent config file**: `<agent>/local-paths.conf` WORLD_PATH / META_PATH
3. **Fallback**: `PROJECT_ROOT/world` and `PROJECT_ROOT/meta` (keeps scripts importable before `/start`)

## Resolution in Scripts

### Bash (`_paths.sh`)
```bash
if [ -n "$AGENT_NAME" ] && [ -f "$PROJECT_ROOT/$AGENT_NAME/local-paths.conf" ]; then
    source "$PROJECT_ROOT/$AGENT_NAME/local-paths.conf"
fi
WORLD_DIR="${AYOAI_WORLD:-${WORLD_PATH:-$PROJECT_ROOT/world}}"
META_DIR="${AYOAI_META:-${META_PATH:-$PROJECT_ROOT/meta}}"
```

### Python (`_paths.py`)
```python
def _read_local_paths():
    agent = os.environ.get("AYOAI_AGENT", "")
    if agent:
        conf = PROJECT_ROOT / agent / "local-paths.conf"
        if conf.exists():
            # parse key=value lines, strip quotes
            ...
    return {}

WORLD_DIR = Path(os.environ.get("AYOAI_WORLD",
                 _local.get("WORLD_PATH", str(PROJECT_ROOT / "world"))))
META_DIR = Path(os.environ.get("AYOAI_META",
                _local.get("META_PATH", str(PROJECT_ROOT / "meta"))))
```

WORLD_DIR and META_DIR are always valid Paths. The fallback ensures hooks and imports work before `/start`. The `/start` skill checks for `<agent>/local-paths.conf` existence to decide whether to ask for paths.

## /start Flow (First Boot)

When `/start <name>` runs and the agent is new:
1. **Phase A**: Validate agent name, bind to session, create `<agent>/` directory
2. **Phase B** (if `<agent>/local-paths.conf` does not exist): Ask for world path, validate, ask for meta path, validate, write `<agent>/local-paths.conf`, add permissions to `settings.local.json`
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
  core/               — Framework (git-tracked)
  .claude/            — Skills, rules, settings (git-tracked)
  alpha/              — Agent private state (local-only)
    local-paths.conf  — This agent's external path config (gitignored)
    self.md           — Agent identity
    ...
  beta/               — Another agent (can point to different paths)
    local-paths.conf
    ...
  CLAUDE.md           — Instructions (git-tracked)
```

## Shared Location Structure

```
/shared/ayoai/
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

Forged skills in `.claude/skills/` are the one exception — check `<agent>/forged-skills.yaml` before deleting.

## Path Format

Use **forward slashes** on all platforms:
- Good: `C:/Users/Shared/ayoai/world`
- Bad: `C:\Users\Shared\ayoai\world` (backslashes are escape sequences when bash sources the file)

Python handles both slash styles, but bash does not. Forward slashes work everywhere.
