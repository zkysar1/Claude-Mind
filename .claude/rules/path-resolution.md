# External Path Resolution

## Problem

Skill pseudocode uses virtual prefixes `meta/` and `world/` (e.g., "Read meta/foo.yaml").
These are NOT relative to the project root or the world directory's parent. They map to
user-configured external paths that can be named anything (e.g., `My-Meta`, not `meta`).

## Rule

When using Read, Write, or Edit tools on files under `meta/` or `world/`:

1. Read `<agent>/local-paths.conf` (or recall the values if already read this session)
2. Replace the virtual prefix with the configured path:
   - `meta/foo.yaml` → `{META_PATH}/foo.yaml`
   - `world/bar.yaml` → `{WORLD_PATH}/bar.yaml`
3. NEVER derive meta or world paths by navigating from one to the other
4. NEVER assume `meta/` is a sibling directory of `world/` — they are independently configured

When using Bash scripts (meta-set.sh, retrieve.sh, etc.), paths resolve automatically
via `_paths.sh` — no manual resolution needed.

## Why This Matters

On 2026-04-02, the LLM resolved `meta/reflection-strategy.yaml` by going up from the world
directory and appending `meta/` — creating a stale directory at the wrong path. The configured
`META_PATH` pointed to a custom directory name, not `meta`. The stale file went undetected for two days.
