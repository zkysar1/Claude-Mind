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

### Standard for daemon endpoints and long-running Python processes

Every `mind_api/src/` endpoint MUST resolve paths through the per-request
context (`ctx.paths.world`, `ctx.paths.meta`, `ctx.paths.agent`) — never
import module-level constants and never re-read `local-paths.conf`
in-endpoint. `mind_api/src/agent_paths.py` (and `core/scripts/_paths.py` for
CLI consumers) pass every value through `_absolutize()` so the returned `Path`
is absolute; a Windows-absolute string parses RELATIVE on POSIX Python and a
string-joined relative fragment silently mirrors a tree under cwd (g-115-733).
Endpoints must NEVER call `os.chdir()`, `Path.cwd()`, or `os.getcwd()` to derive
paths; new endpoint path resolution must extend `agent_paths.py` and route
through `_absolutize()`, never re-implement the env-var/conf/fallback chain
inline.

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

The L1 path-resolution hook (`core/scripts/path-resolution-hook.py`) refuses a
Write/Edit/MultiEdit that would create a NEW top-level entry (file or
directory) immediately under any governed root: `WORLD_PATH`, `META_PATH`, or
the bound agent's directory. It exists because an LLM blocked from a desired
location invents a plausible-looking new top-level directory instead
(`world/handoffs/`, `bravo/handoffs/`, `alpha/scratch/`).

- Fires: a new top-level dir or file under a governed root that does not
  already exist on disk.
- Does NOT fire: writes into existing top-level dirs; edits to existing files;
  writes anywhere under `agents/<agent>/sessions/<SID>/` for a BOUND session
  (the sanctioned scratch home — a never-bound SID is still refused); shell
  `mkdir`/`cp`/`touch` (bypass the hooks); writes inside `PROJECT_ROOT` outside
  the bound agent's dir; writes inside `AGENT_WRITE_PATH`.
- Cross-agent writes are not covered by design — route through `world/board/`
  or `world/team-state.yaml` per `coordination.md`.

**There is no agent-side override flag.** To add a top-level entry
legitimately: ask the user (they create it or approve a path under an existing
top-level dir), or update an `init-*.sh` script; once the directory exists on
disk, writes pass. The friction is the point — silent invention is the failure
mode being prevented.

## Cross-references

- `core/config/conventions/external-paths.md` — resolution priority, script
  APIs, and the moved mechanism/incident record
- `world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md`
  — concrete cruft catalogue, IDs, and dates
- CLAUDE.md "Agent-dir Resolution" + `core/config/conventions/agent-dir-resolution.md`
