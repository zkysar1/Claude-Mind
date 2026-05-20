# External Path Resolution

## Problem

Skill pseudocode uses virtual prefixes `meta/` and `world/` (e.g., "Read meta/foo.yaml").
These are NOT relative to the project root or the world directory's parent. They map to
user-configured external paths that can be named anything (e.g., `Custom-Meta`, not `meta`).

## Rule

When using Read, Write, or Edit tools on files under `meta/` or `world/`:

1. Read `agents/<agent>/local-paths.conf` (or recall the values if already read this session)
2. Replace the virtual prefix with the configured path:
   - `meta/foo.yaml` → `{META_PATH}/foo.yaml`
   - `world/bar.yaml` → `{WORLD_PATH}/bar.yaml`
3. NEVER derive meta or world paths by navigating from one to the other
4. NEVER assume `meta/` is a sibling directory of `world/` — they are independently configured

When using Bash scripts (meta-set.sh, retrieve.sh, etc.), paths resolve automatically
via `_paths.sh` — no manual resolution needed.

### Standard for daemon endpoints and long-running Python processes

Every `mind_api/src/` endpoint MUST resolve paths through the per-request
context (`ctx.paths.world`, `ctx.paths.meta`, `ctx.paths.agent`) — never
import module-level constants and never re-read `local-paths.conf`
in-endpoint. The resolver behind `ctx.paths` is `mind_api/src/agent_paths.py`
which passes every string through `_absolutize()` to guarantee the
returned `Path` is absolute. Same guarantee in `core/scripts/_paths.py`
for CLI-path consumers.

Rationale (g-115-733, 2026-05-13/14 cruft incident):
1. A `Path("C:/Users/...")` value is absolute on Windows Python but
   parses as RELATIVE on POSIX Python (and on some MSYS Python builds).
   If a daemon endpoint takes the value and joins it via string
   concatenation (`base + "/" + sub`), the resulting string may be
   interpreted as relative-to-cwd by a downstream `mkdir -p` or
   `Path().parent.mkdir()` — silently producing a mirror tree under
   the wrong root.
2. The `_absolutize()` helper in both `_paths.py` and `agent_paths.py`
   forces absoluteness BEFORE the value escapes the resolver. Any
   relative fragment is anchored to PROJECT_ROOT, never to cwd.
3. Endpoints must NEVER call `os.chdir()`, `Path.cwd()`, or
   `os.getcwd()` to derive paths. cwd is mutable and not under
   per-request control.
4. New daemon endpoints adding their own path resolution (e.g. for
   resource locators) must extend `agent_paths.py` and route through
   `_absolutize()` — never re-implement the env-var/conf/fallback
   chain inline.

The convention is enforced by:
- `core/scripts/_paths.py::_absolutize()` for CLI-side imports.
- `mind_api/src/agent_paths.py::_absolutize()` for daemon-side resolver.
- This rule + `world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md`
  for human authors of new daemon code.

## Agent Paths

Agent directories live at `PROJECT_ROOT/<AGENTS_PARENT_DIR>/<agent-name>` — currently
`PROJECT_ROOT/agents/<agent-name>` (the lowercase name written into `.active-agent-<SID>`
and resolved by `_paths.py` as `AGENT_DIR = agent_dir(AGENT_NAME)`, where `agent_dir(name)`
expands to `PROJECT_ROOT / AGENTS_PARENT_DIR / name`). The `AGENTS_PARENT_DIR` constant is
the single sync point for relocating agent dirs in the future — see CLAUDE.md "Agent-dir
Resolution" section. They are **NOT** under `WORLD_DIR` or `META_DIR`, and **NOT** under
`dirname WORLD_DIR` or `dirname META_DIR`.

Rules:

1. The basenames of `WORLD_DIR` and `META_DIR` are user-chosen (e.g. `Custom-Meta`, or any other
   name). Whatever convention is used for them does NOT extend to agent dir names. Do not
   generate an agent dir name by analogy with the world/meta basenames.
2. Never `mkdir -p <prefixed-agent-name>/...` in any context where `<prefixed-agent-name>`
   was derived by pattern-matching the world/meta basenames.
3. Never compute an agent path under `dirname WORLD_DIR` or `dirname META_DIR`.
4. When typing `<agent>/<sub>` in ad-hoc Bash, the implicit base must be `PROJECT_ROOT`.
   Confirm CWD or use a `PROJECT_ROOT`-rooted absolute path.

This rule is not enforced by `_paths.py::resolve_file_path` — that function defends `world/`
and `meta/` prefixes only. Agent paths in shell commands fall through unchecked.

## L1 Cruft Prevention: New Top-Level Entries Require Approval

Beyond the configured-root check, the L1 path-resolution hook
(`core/scripts/path-resolution-hook.py`) ALSO refuses writes that would
create a new top-level entry (file or directory) immediately under any of
the three governed roots:

1. `WORLD_PATH`
2. `META_PATH`
3. The bound agent's directory (`PROJECT_ROOT/<MIND_AGENT>/`)

This catches the failure mode where an LLM, blocked from a desired
location (e.g., user's OneDrive root), invents a new top-level subdirectory
under `WORLD_PATH` (or under `<agent>/`) that satisfies the surface
root-check but constitutes cruft. WORLD/META was the original 2026-05-09
incident (`world/handoffs/`); the agent-dir extension landed the same day
after the user observed the same failure mode would reproduce as
`bravo/handoffs/`, `alpha/scratch/`, etc.

### What "new top-level entry" means

The first path segment under the governed root:
- `WORLD_PATH/handoffs/foo.txt` → top-level entry is `handoffs/`
- `WORLD_PATH/scratch.md` → top-level entry is `scratch.md`
- `PROJECT_ROOT/agents/bravo/handoffs/foo.txt` → top-level entry inside
  the agent dir is `handoffs/`

If that segment doesn't exist on disk at write time, the hook denies the
write and lists the standard alternatives.

### When this fires

- LLM tries to write to a new top-level directory never established in the
  canonical `world/`, `meta/`, or `<agent>/` structure
- LLM tries to drop a new top-level file at any of the governed roots

### When this does NOT fire

- Writes to existing top-level directories (e.g., `WORLD_PATH/knowledge/...`,
  `WORLD_PATH/board/...`, `agents/<agent>/journal/...`, `agents/<agent>/session/...`)
- **Phase 2.6 sanctioned scratch**: writes anywhere under
  `agents/<agent>/sessions/<SID>/` where `<SID>` is a bound session
  (the dir was created by `/start` via `session-binding-write.py`).
  Per-session dirs are the explicitly-approved spot for ephemeral scratch,
  experiment outputs, iteration checkpoints, and any other transient files
  scoped to a single Claude Code session. New sub-paths under a bound
  session dir do NOT trigger the new-top-level cruft check. Writes to
  `agents/<agent>/sessions/<UNKNOWN-SID>/...` (a SID that was never bound)
  remain refused — silent invention of new SID dirs is the same cruft
  class the rest of L1 prevents.
- Edits to existing files anywhere in the tree
- Init scripts that use shell `mkdir`, `cp`, or `touch` (those bypass the
  Write/Edit/MultiEdit hooks entirely)
- Writes inside `PROJECT_ROOT` OUTSIDE the bound agent's dir
  (`core/`, `.claude/`, the project root itself, OTHER agent dirs) —
  those are governed by their own conventions and protected by L2
  permission rules, and remain git-tracked so cruft surfaces in `git status`
- Writes inside `AGENT_WRITE_PATH` (sibling product repos — also git-tracked)

### How to legitimately add a new top-level entry

1. Ask the user. They can create the directory manually with shell, or
   approve a path under an existing top-level dir.
2. Update an `init-*.sh` script (which runs outside the hook on first-time
   setup).
3. Once the directory exists on disk, subsequent writes pass the check.

There is no agent-side override flag. The only way past this gate is for
the directory to already exist, which means either (a) the user created
it, (b) a sanctioned shell init-step created it, or (c) the agent and user
agreed on it and the user manually `mkdir`'d. This intentional friction is
the entire point — silent invention is the failure mode being prevented.

### Cross-agent writes

The check fires only on the BOUND agent's dir
(`PROJECT_ROOT/<MIND_AGENT>/`), not on every agent dir in the project.
Cross-agent writes (e.g., an `MIND_AGENT=bravo` session writing to
`alpha/handoffs/`) are governed by other rules (see
`core/config/conventions/coordination.md` — cross-agent communication
should go through `world/board/` or `world/team-state.yaml`, not direct
file writes into another agent's dir). If cross-agent cruft becomes a
real problem, this check can be extended to enumerate all agent dirs
(any sibling under PROJECT_ROOT containing a `local-paths.conf`).

## Why This Matters

On 2026-04-02, the LLM resolved `meta/reflection-strategy.yaml` by going up from the world
directory and appending `meta/` — creating a stale directory at the wrong path. The configured
`META_PATH` pointed to a custom directory name, not `meta`. The stale file went undetected for two days.

On 2026-05-08, an audit found two cruft roots in this repo (concrete paths in the
domain-specific tree node — see cross-reference below):

- A sibling of `PROJECT_ROOT` (under `dirname PROJECT_ROOT`) — 4 stale files dated 2026-04-17
  through 2026-04-20. Plausible mechanism: world/meta virtual-prefix drift (inferred from
  path shape — no transcript trace pinpoints the originating command). Path-shape consistent
  with the failure mode that `resolve_file_path` was made strict against on 2026-04-20.
- Agent-shaped names directly under `dirname WORLD_DIR` — 3 empty `mkdir -p` skeletons,
  with mtimes ranging 2026-04-20 through 2026-05-08. Plausible mechanism: agent-dir
  pattern-match (inferred from path shape). The most recent mtime falls within the same
  week as the audit, so the path-shape that produced the skeletons is still being generated.

Both locations were cleaned. The agent-dir variant is now covered by a project-level
guardrail and a domain-specific tree node — see
`world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md`
for the concrete catalogue, IDs, and dates.

On 2026-05-09, Bravo was asked to write a handoff document to user's OneDrive root.
The L1 gate correctly blocked the OneDrive-root write (outside configured roots).
Bravo then invented `WORLD_PATH/handoffs/` as a "satisfies the root check"
alternative without confirming that `handoffs/` was an established convention. The
user deleted the cruft and directed Bravo to harden the gate. The
"L1 Cruft Prevention" check above (new-top-level-entry detection in
`is_new_toplevel`, `core/scripts/path-resolution-hook.py`) is the result —
the original allow-by-root-match logic accepted the invented path; the new
check rejects it with an educational message listing alternatives.

Same day, the user observed that the new check excluded `PROJECT_ROOT` and asked
whether the same failure mode would reproduce as `bravo/handoffs/`,
`alpha/scratch/`, etc. inside an agent dir. Verification confirmed the gap (a
test write to `bravo/test-agent-toplevel-zzz/...` landed on disk). The check was
extended (Option A — surgical) to also fire when the matched root is
`PROJECT_ROOT` AND the target lies under the bound agent's dir
(`PROJECT_ROOT/<MIND_AGENT>/`). Cross-agent writes (one agent's session
writing into another agent's dir) are not covered by this check by design;
those route through `world/board/` per `core/config/conventions/coordination.md`.
