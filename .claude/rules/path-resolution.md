---
description: "world/ and meta/ resolve via local-paths.conf (Read/Edit prefixes only, never bare bash paths); agents under agents/; no new top-level dirs."
---

# External Path Resolution

## Problem

Skill pseudocode uses virtual prefixes `meta/` and `world/` (e.g., "Read meta/foo.yaml").
These are NOT relative to the project root or the world directory's parent. They map to
user-configured external paths that can be named anything (e.g., `Custom-Meta`, not `meta`).

Mechanism detail and the incident record (2026-04-02 stale-meta dir, 2026-05-08
cruft roots, 2026-05-09 `world/handoffs/` invention, the g-115-733 daemon
cwd cruft) live in `core/config/conventions/external-paths.md`
(`load-conventions.sh external-paths`). This file keeps the imperatives.

## Rule

When using Read, Write, or Edit tools on files under `meta/` or `world/`:

1. Read `agents/<agent>/local-paths.conf` (or recall the values if already read this session)
2. Replace the virtual prefix with the configured path:
   - `meta/foo.yaml` → `{META_PATH}/foo.yaml`
   - `world/bar.yaml` → `{WORLD_PATH}/bar.yaml`
3. NEVER derive meta or world paths by navigating from one to the other
4. NEVER assume `meta/` is a sibling directory of `world/` — they are independently configured

When using Bash scripts (meta-set.sh, retrieve.sh, etc.), paths resolve automatically
via `_paths.sh` — no manual resolution needed — **but ONLY because the invoked
script sources `_paths.sh` internally. Bash hooks do NOT rewrite `world/`/`meta/`
prefixes** (g-115-1056): `bash-agent-inject.py` only prepends env exports and
`bash-path-resolution-hook.sh` only denies cruft. So `bash world/scripts/<name>.sh`
is a literal relative path from cwd, where no `world/` exists — use
`source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/<name>.sh" ...` or the
script's daemon wrapper. Only Read/Write/Edit/MultiEdit `file_path` prefixes are
hook-resolved.

### Daemon endpoints and long-running Python processes

Endpoints MUST resolve via per-request context (`ctx.paths.*`), never
module-level constants. MUST NOT call `os.chdir()` / `Path.cwd()` /
`os.getcwd()`. Full rules: `core/config/conventions/external-paths.md`
§ "Standard for daemon endpoints".

## Agent Paths

Agent directories live at `PROJECT_ROOT/<AGENTS_PARENT_DIR>/<agent-name>` —
currently `PROJECT_ROOT/agents/<agent-name>`, resolved by `agent_dir(name)`
(see CLAUDE.md "Agent-dir Resolution"). They are **NOT** under `WORLD_DIR` or
`META_DIR`, and **NOT** under `dirname WORLD_DIR` or `dirname META_DIR`.

1. The basenames of `WORLD_DIR` and `META_DIR` are user-chosen. Whatever
   convention is used for them does NOT extend to agent dir names. Do not
   generate an agent dir name by analogy with the world/meta basenames.
2. Never `mkdir -p <prefixed-agent-name>/...` in any context where
   `<prefixed-agent-name>` was derived by pattern-matching the world/meta basenames.
3. Never compute an agent path under `dirname WORLD_DIR` or `dirname META_DIR`.
4. When typing `<agent>/<sub>` in ad-hoc Bash, the implicit base must be
   `PROJECT_ROOT`. Confirm CWD or use a `PROJECT_ROOT`-rooted absolute path.

This is not enforced by `_paths.py::resolve_file_path` (which defends `world/`
and `meta/` prefixes only) — agent paths in shell commands fall through unchecked.

## L1 Cruft Prevention: New Top-Level Entries Require Approval

`core/scripts/path-resolution-hook.py` refuses Write/Edit/MultiEdit creating a
NEW top-level entry under `WORLD_PATH`, `META_PATH`, or the bound agent's dir.
Does NOT fire on writes into existing dirs, edits to existing files, writes
under `agents/<agent>/sessions/<SID>/` for a bound session, shell mkdir/cp/touch,
or `AGENT_WRITE_PATH`. Cross-agent writes: advisory only (g-375-04).

**No agent-side override.** To add a top-level entry: ask the user, or update
an `init-*.sh` script; once the directory exists on disk, writes pass.

## Cross-references

- `core/config/conventions/external-paths.md` — resolution priority, script
  APIs, and the moved mechanism/incident record
- `world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md`
  — concrete cruft catalogue, IDs, and dates
- CLAUDE.md "Agent-dir Resolution" + `core/config/conventions/agent-dir-resolution.md`
