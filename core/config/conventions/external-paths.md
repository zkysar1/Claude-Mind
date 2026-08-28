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


## Path-resolution mechanism and incident record (moved from `.claude/rules/path-resolution.md`, 2026-08-17, g-115-6581)

The rule keeps the imperatives (virtual-prefix resolution for Read/Write/Edit,
never derive meta from world or vice versa, Bash hooks do not rewrite paths,
daemon endpoints resolve through `ctx.paths`, agent paths are never derived
from world/meta basenames, the L1 new-top-level-entry gate). This section holds
the mechanism detail and the incidents behind them.

### Why "resolve automatically" holds only inside scripts (g-115-1056, verified 2026-05-27)

**Bash hooks do NOT rewrite `world/`/`meta/` prefixes (g-115-1056, verified 2026-05-27).**
The "resolve automatically" above holds ONLY because the invoked script sources
`_paths.sh` INTERNALLY (which exports `$WORLD_PATH`/`$META_PATH`). The two
PreToolUse[Bash] hooks do NOT touch path arguments: `bash-agent-inject.py` only
PREPENDS env exports (`export PATH=...; MIND_AGENT=...; export MIND_SID=...`) to
the command (`bash-agent-inject.py:367`), and `bash-path-resolution-hook.sh` only
DENIES new-top-level-entry cruft (it never rewrites a path). So a bare positional
invocation like `bash world/scripts/<name>.sh ...` is NOT prefix-resolved — `world/`
stays a literal relative path from cwd (PROJECT_ROOT), where no `world/` directory
exists (the local repo holds only `core/`, `.claude/`, and `agents/`; `world/` lives
at an external `$WORLD_PATH`), so the command fails to find the script. Use
`source core/scripts/_paths.sh` then `bash "$WORLD_PATH/scripts/<name>.sh" ...`, or
invoke the script's daemon wrapper. Contrast Read/Write/Edit/MultiEdit (rule above),
whose `file_path` virtual prefixes ARE resolved by the PreToolUse[Write|Edit] path
hook — that resolution does NOT extend to Bash tool arguments.

### Daemon endpoints and long-running Python processes — rationale (g-115-733, 2026-05-13/14 cruft incident)

Every `mind_api/src/` endpoint MUST resolve paths through the per-request
context (`ctx.paths.world`, `ctx.paths.meta`, `ctx.paths.agent`); the resolver
is `mind_api/src/agent_paths.py`, which passes every string through
`_absolutize()`. Same guarantee in `core/scripts/_paths.py`.

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

### L1 cruft prevention — full detail

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

### Why this matters — the incident record

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

On 2026-08-27/28, bravo authored the owner-approved asp-370 SDLC charter into a
literal `PROJECT_ROOT/world/conventions/` path — created by a `py -3` patch
script whose paths were built INSIDE the script body, a lane no write-time hook
can see (the Write/Edit L1 hook denies the same absolute path when it arrives
as a tool `file_path`; verified live the same day). The stray was gitignored,
outside every governed root (so no post-write verifier keyed on it), and
self-confirming on an own-cloud box: every local read succeeded while S3 never
had the file. The nine goals citing it (written via daemon store scripts, which
write S3 directly) arrived on every box, masking the gap. Cost: asp-370
foundationally blocked fleet-wide ~7h until a peer agent probed S3. Diagnosis
of the hook was itself delayed by shell-quoting: six probes fed the hook JSON
whose backslashes a shell layer had collapsed (`\Z` -> invalid escape), and the
hook fail-open-approves a parse error — build hook probe payloads with
json.dumps, never hand-quoted shell strings. Closures: (1)
`bash-path-resolution-hook.py` now emits a persistent 4-channel ADVISORY on
every write-shaped Bash call while a stray `PROJECT_ROOT/world|meta` exists
(detection within a call or two of creation, regardless of lane); (2)
`validate-paths.sh` (L3) warns on the stray at session start; (3) guard-5362 —
verify a world/meta file against the AUTHORITATIVE store before citing its
path in any artifact other agents will read. Tests:
`core/scripts/tests/test_bash_hook_stray_root_advisory.py`.
