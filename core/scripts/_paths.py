"""Centralized path resolution for the cognitive core.

4-tier architecture:
  core/          — Framework (immutable)
  meta/          — Meta-strategies (domain-agnostic, independent of domain data)
  world/         — Collective domain state (shared across agents)
  <agent-name>/  — Per-agent private state (one directory per agent)
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent          # core/
PROJECT_ROOT = CORE_ROOT.parent        # project root
CONFIG_DIR = CORE_ROOT / "config"
REPO_ROOT = PROJECT_ROOT               # legacy alias


# --- Agent-dir resolution (Phase 2.5.C/D) ---
# Empty string = agent dirs at PROJECT_ROOT (legacy layout, pre-2026-05-19).
# Currently "agents" — agent dirs live at PROJECT_ROOT/agents/<name>.
# AGENTS_PARENT_DIR MUST stay in sync across all 3 resolver layers:
#   core/scripts/_paths.py      (this file)
#   core/scripts/_paths.sh
#   mind_api/src/agent_paths.py
# Plus 2 import-cycle-proof inlined copies:
#   core/scripts/_agents.py
#   core/scripts/path-resolution-hook.py
# Test fixtures and other inline copies (search for AGENTS_PARENT_DIR / _APD)
# also need to match. See CLAUDE.md "Agent-dir Resolution" section.
AGENTS_PARENT_DIR = "agents"


def agents_root() -> Path:
    """Directory containing all agent subdirs."""
    return PROJECT_ROOT / AGENTS_PARENT_DIR if AGENTS_PARENT_DIR else PROJECT_ROOT


def agent_dir(name: str) -> Path:
    """Filesystem path for an individual agent's dir."""
    return agents_root() / name


def enumerate_agent_confs() -> list:
    """All local-paths.conf files (agent discovery)."""
    return sorted(agents_root().glob("*/local-paths.conf"))


def is_under_agent_dir(p: Path) -> bool:
    """True if p lives under any agent's directory (depth >=1 under agents_root())."""
    try:
        rel = p.resolve().relative_to(agents_root().resolve())
    except ValueError:
        return False
    return len(rel.parts) >= 1 and (agents_root() / rel.parts[0]).is_dir()


# --- Per-session dirs (Phase 2.6) ---
# Each agent has agents/<name>/sessions/<SID>/ for that session's snapshot
# state (binding, runner-token, scratch, iteration-checkpoint, etc.).
# Cross-session state stays in agents/<name>/session/ (singular).
SESSIONS_DIRNAME = "sessions"
SESSION_DIRNAME = "session"  # legacy singular dir for cross-session state


def agent_sessions_root(name: str) -> Path:
    """Parent dir for all per-session dirs for one agent.

    Phase 2.6 layout: agents/<name>/sessions/
    """
    return agent_dir(name) / SESSIONS_DIRNAME


def agent_session_dir(name: str, sid: str) -> Path:
    """Per-session dir for one (agent, sid). Dir name IS the sid.

    Phase 2.6 layout: agents/<name>/sessions/<sid>/
    """
    return agent_sessions_root(name) / sid


def agent_state_dir(name: str) -> Path:
    """Cross-session state dir for an agent (the singular 'session/' dir).

    Holds files that survive across sessions (agent-state, agent-mode,
    persona-active, handoff.yaml, pending-questions.yaml, etc.).
    Phase 2.6 keeps this as agents/<name>/session/ for compatibility.
    """
    return agent_dir(name) / SESSION_DIRNAME


# --- External path configuration ---
# world/ and meta/ live at user-supplied external paths (shared drive, NAS, etc.).
# Each agent stores its own config in <agent>/local-paths.conf.
#
# Plan v1 step 0.1 (2026-05-19) — HARD CUT: NO fallback to PROJECT_ROOT/world
# or PROJECT_ROOT/meta when unconfigured. The fallback was the RC-A root cause
# of the world/ and meta/ cruft at PROJECT_ROOT — a NO_AGENT subprocess (or an
# MIND_AGENT set to a nonexistent agent name) would silently write to the
# repo-root mirrors instead of the configured external paths. Removed because:
#   - L1 path-resolution-hook.py refuses LLM tool-call writes to those literal
#     paths (the existing PROJECT_ROOT/world|meta literal-cruft branch).
#   - Subprocess writes need the resolver-side defense: WORLD_DIR/META_DIR
#     resolve to None when no conf is found, and any caller constructing a
#     write path via `WORLD_DIR / "foo"` TypeErrors loudly at the construction
#     site instead of silently writing to PROJECT_ROOT/world/foo.
#   - The hard cut is safe because normal operation ALWAYS sets MIND_AGENT
#     (via /start or the bash-agent-inject PreToolUse hook), so _local always
#     resolves to a real agent's conf and WORLD_DIR/META_DIR are valid Paths.
#     The None case fires only in UNINITIALIZED projects (where the data-store
#     modules are not imported) or in test/manual scripts that bypass /start
#     (which set MIND_WORLD/MIND_META env vars explicitly).
def _resolve_agent_name():
    """Resolve agent name from MIND_AGENT env var. One path, no fallbacks."""
    return os.environ.get("MIND_AGENT", "").strip()

def _parse_conf(conf: Path) -> dict:
    """Parse a local-paths.conf file into a dict."""
    paths = {}
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            val = val.strip()
            # Strip matching quotes (bash source handles them, we must too)
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            paths[key.strip()] = val
    return paths


def _read_local_paths():
    """Read <agent>/local-paths.conf if agent is bound.

    When MIND_AGENT is unset (hooks, background processes), uses the first
    available local-paths.conf to prevent writes to PROJECT_ROOT/world.

    g-115-960 fix (for g-115-953 root cause): when MIND_AGENT names an agent
    with NO local-paths.conf (test polluter pattern: "test-alpha-zzz" etc),
    emit a stderr warning AND fall through to the first-available conf
    instead of returning {} silently. Returning {} caused WORLD_DIR/META_DIR
    to resolve to PROJECT_ROOT/world and PROJECT_ROOT/meta via the
    `os.environ.get("MIND_WORLD", _local.get("WORLD_PATH", str(PROJECT_ROOT / "world")))`
    chain below, producing stray-root cruft. looks_like_cruft in
    _path_helpers.py does NOT match the `C:/repo/world` shape (drive-letter
    is stripped before the colon scan), so the tripwire never fired.
    """
    import sys
    agent = _resolve_agent_name()
    if agent:
        conf = agent_dir(agent) / "local-paths.conf"
        if conf.exists():
            return _parse_conf(conf)
        # MIND_AGENT names a nonexistent agent — fail-loud + fall through.
        # Falling through (rather than raising) preserves the loop when a
        # test polluter sets MIND_AGENT without restoring; the warning
        # surfaces the polluter for the next sweep / audit.
        print(
            f"[_paths] WARN: MIND_AGENT={agent!r} but {conf} does not exist. "
            f"Falling through to first-available local-paths.conf to prevent "
            f"PROJECT_ROOT/world,meta cruft. Likely a test polluter set "
            f"MIND_AGENT without restoring the env var (g-115-960 / g-115-953).",
            file=sys.stderr,
        )
        # fall through to the first-available glob below

    # MIND_AGENT unset OR named a nonexistent agent — use the first available
    # conf that actually resolves a WORLD_PATH. An empty or partial conf (e.g.
    # an abandoned `/start <agent>` that created the agent dir + an empty
    # local-paths.conf at Phase A2 but never completed Phase B path config)
    # must NOT poison resolution by sorting first. Skipping WORLD_PATH-less
    # confs lets the next usable conf win regardless of alphabetical ordering.
    # (2026-06-14: an empty agents/delta/local-paths.conf sorted before omni's,
    # resolving WORLD_DIR=None and crashing the daemon at tree.py import when
    # the daemon ran agent-agnostic without MIND_AGENT — the SessionStart hook
    # spawns it before any agent binding exists.)
    for conf in sorted(agents_root().glob("*/local-paths.conf")):
        parsed = _parse_conf(conf)
        if parsed.get("WORLD_PATH"):
            return parsed
    return {}

_local = _read_local_paths()


def _absolutize(value: str) -> Path:
    """Convenience wrapper that binds PROJECT_ROOT for CLI callers.

    The two-stage cruft defense lives in `_path_helpers.absolutize`
    (g-115-733). This wrapper exists so CLI scripts that already import
    `_paths` don't have to also import `_path_helpers` + PROJECT_ROOT to
    absolutize a single value. Daemon code passes project_root explicitly
    via `_path_helpers.absolutize` — there is no daemon wrapper.
    """
    from _path_helpers import absolutize  # local import — avoids cycle
    return absolutize(value, PROJECT_ROOT)


# --- .mind-data/ local storage root ( M1, ) ---
# When PROJECT_ROOT/.mind-data/ exists, it is the local storage root by
# convention: world -> .mind-data/world, meta -> .mind-data/meta. An optional
# .mind-data/.env.local (same key=value format as local-paths.conf) overrides
# per-tier paths via the WORLD_PATH / META_PATH keys. This is the portable
# default for a fresh-cloned or transplanted repo -- _transplant_pack.py already
# writes world/meta under <dest>/.mind-data/ and a WORLD_PATH pointing there.
# The whole tier is GATED on the .mind-data/ dir existing, so a repo WITHOUT it
# keeps the legacy env -> local-paths.conf -> None chain byte-for-byte. Live
# agents configured via external local-paths.conf have no .mind-data/ dir, so
# resolution skips this tier and reaches their conf entry unchanged.
# MUST stay in sync with the .mind-data/ blocks in _paths.sh and
# mind_api/src/agent_paths.py (the 3 resolver layers cannot share code: shell
# vs python-module vs import-cycle-proof daemon resolver).
MIND_DATA_DIR = PROJECT_ROOT / ".mind-data"
_MIND_DATA_SUBDIR = {"WORLD_PATH": "world", "META_PATH": "meta"}


def _read_mind_data_env() -> dict:
    """Parse .mind-data/.env.local into a dict (same format as local-paths.conf).
    Returns {} when the file is absent or unreadable."""
    envf = MIND_DATA_DIR / ".env.local"
    if envf.exists():
        try:
            return _parse_conf(envf)
        except OSError:
            return {}
    return {}


_mind_data_env = _read_mind_data_env()


def _resolve_tier(env_key, conf_key, *, mind_data_dir, mind_data_env, local_conf):
    """Pure tier resolver (roots injected so it is testable without touching the
    real PROJECT_ROOT/.mind-data). Priority chain (asp-330 M1):
      1. MIND_{WORLD,META} env var
      2. .mind-data/.env.local {WORLD,META}_PATH   (only when .mind-data/ exists)
      3. .mind-data/{world,meta} bare default      (only when .mind-data/ exists)
      4. local-paths.conf {WORLD,META}_PATH        (legacy fallback)
      5. None (2026-05-19 hard-cut preserved when nothing configured; guard-551)
    """
    val = os.environ.get(env_key)
    if val:
        return _absolutize(val)
    if mind_data_dir.is_dir():
        val = mind_data_env.get(conf_key)
        if val:
            return _absolutize(val)
        sub = _MIND_DATA_SUBDIR.get(conf_key)
        if sub:
            return mind_data_dir / sub
    val = local_conf.get(conf_key)
    if val:
        return _absolutize(val)
    return None


# --- Tier 2/3: Meta-strategies + Collective domain state ---
# Module-bound wrapper over _resolve_tier. The previous PROJECT_ROOT/{world,meta}
# fallback was removed (2026-05-19) to refuse cruft; the None return is still the
# unconfigured terminus and callers MUST guard it (assert_world_dir /
# assert_meta_dir; guard-551). Two callers (META_DIR, WORLD_DIR below).
def _resolve_external(env_key: str, conf_key: str):
    return _resolve_tier(
        env_key,
        conf_key,
        mind_data_dir=MIND_DATA_DIR,
        mind_data_env=_mind_data_env,
        local_conf=_local,
    )


META_DIR = _resolve_external("MIND_META", "META_PATH")

# --- Tier 3: Collective domain state ---
# Priority: env var MIND_WORLD > _local.get("WORLD_PATH") > None.
# Plan v1 step 0.1 (2026-05-19): removed PROJECT_ROOT/world fallback.
WORLD_DIR = _resolve_external("MIND_WORLD", "WORLD_PATH")

# --- Tier 4: Per-agent private state ---
# Resolution: MIND_AGENT_DIR env (test override) > MIND_AGENT under PROJECT_ROOT.
# MIND_AGENT_DIR exists for unit tests that need a tmp agent dir; production
# code never sets it.
AGENT_NAME = _resolve_agent_name()
_agent_dir_override = os.environ.get("MIND_AGENT_DIR", "").strip()
if _agent_dir_override:
    AGENT_DIR = Path(_agent_dir_override)
elif AGENT_NAME:
    AGENT_DIR = agent_dir(AGENT_NAME)
else:
    AGENT_DIR = None


def read_agent_conf(name: str = None) -> dict:
    """Parsed `local-paths.conf` for an agent (default: the bound agent).

    Public accessor over `_parse_conf` for callers that need NON-PATH config
    out of the conf -- e.g. the per-box `RUNNER_CAPABILITIES_*` declarations
    read by `_runner_capabilities.box_config_from_conf`. Exists so those
    callers do not re-implement the KEY=VALUE + quote-stripping parse; this
    module stays the single source of truth for conf syntax.

    `local-paths.conf` is the ONLY genuinely per-box config surface: it is
    gitignored AND listed in `owncloud_sync._EXCLUDE_NAMES`, so it never
    reaches git or S3. `core/config/aspirations.yaml` (git-shared) and
    `meta/config-overrides.yaml` (S3-shared) both apply fleet-wide and so
    cannot express "THIS box has a live Studio session".

    Fail-open to {}: an unbound agent, a missing file, or an unreadable conf
    means "no per-box overrides", never an exception into a caller.
    """
    try:
        agent = name or AGENT_NAME
        if not agent:
            return {}
        conf = agent_dir(agent) / "local-paths.conf"
        if not conf.is_file():
            return {}
        return _parse_conf(conf)
    except Exception:
        return {}


# --- World contract vars ---
# ENVIRONMENT_ID and COMMONS_POLICY live in .env.local (not local-paths.conf).
# Priority for each: env var override > .env.local > default.
# Fail-closed default for COMMONS_POLICY: 'private' (world-contract.md Rule 4).
def _read_env_local() -> dict:
    """Parse PROJECT_ROOT/.env.local, returning active KEY=value pairs.

    Called once at import time to extract world-contract vars.
    Returns {} when .env.local is absent (CI, fresh checkout).
    Does not surface secrets — use env-read.sh for credential lookups.
    """
    import re
    env_local = PROJECT_ROOT / ".env.local"
    if not env_local.exists():
        return {}
    result = {}
    # Name class is the real shell/dotenv rule — lowercase and a leading
    # underscore are both legal. An uppercase-only class does not REJECT such a
    # key, it SILENTLY SKIPS the line (). Third of three copies of
    # this parser: the others are core/scripts/env.py (COMMENTED_KEY_RE /
    # ACTIVE_KEY_RE) and core/scripts/storage_backend.py — keep all three in
    # sync. Inlined rather than shared because this module is the
    # import-cycle-proof base others import.
    _active = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for raw in env_local.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _active.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            result[key] = val
    return result


_env_local_vars = _read_env_local()

# ENVIRONMENT_ID: stable world identity.
# Used by: cross-world guardrails G4/G5, multi-world DDB isolation (Step 3.1),
# generalization engine provenance (originWorldId). None when not configured.
ENVIRONMENT_ID = (
    os.environ.get("ENVIRONMENT_ID")
    or _env_local_vars.get("ENVIRONMENT_ID")
    or None
)

# COMMONS_POLICY: knowledge egress dial for the generalization engine.
# Valid values: 'private' | 'selective' | 'public'.
# Any unrecognized value fails closed to 'private' (world-contract.md Rule 4).
_raw_policy = (
    os.environ.get("COMMONS_POLICY") or _env_local_vars.get("COMMONS_POLICY") or ""
).lower().strip()
COMMONS_POLICY = _raw_policy if _raw_policy in ("private", "selective", "public") else "private"


def assert_world_dir(module_name: str = "<unknown>") -> None:
    """Guard: raise loud RuntimeError if WORLD_DIR is None (no external path resolved).

    Plan v1 step 0.1 (2026-05-19): the PROJECT_ROOT/world fallback was removed
    so module-level path constants like `LIVE_PATH = WORLD_DIR / "aspirations.jsonl"`
    fail loudly when WORLD_DIR is unresolved. This helper provides a clear,
    single-source diagnostic instead of an opaque
    `TypeError: unsupported operand type(s) for /: 'NoneType' and 'str'` deep
    inside import machinery. Mirrors `assert_agent_dir` for the same purpose.

    When WORLD_DIR IS set: silent no-op (return None). Module-level callers
    see byte-identical behavior to the pre-guard path.
    """
    if WORLD_DIR is None:
        msg = (
            f"ERROR: {module_name}: WORLD_DIR unresolved (no MIND_WORLD env "
            f"var, no WORLD_PATH in <agent>/local-paths.conf). This module "
            f"builds module-level path constants from WORLD_DIR and REQUIRES "
            f"WORLD_DIR to resolve to a real external path. Typical fix: bind "
            f"an agent via /start, or set MIND_WORLD explicitly for one-off "
            f"scripts. The PROJECT_ROOT/world fallback was removed in plan v1 "
            f"step 0.1 (2026-05-19) to refuse silent cruft creation.\n"
        )
        sys.stderr.write(msg)
        raise RuntimeError(
            f"{module_name}: WORLD_DIR unresolved — external path not configured"
        )


def assert_meta_dir(module_name: str = "<unknown>") -> None:
    """Guard: raise loud RuntimeError if META_DIR is None. Mirror of
    assert_world_dir for the meta/ tier. See that function for full rationale."""
    if META_DIR is None:
        msg = (
            f"ERROR: {module_name}: META_DIR unresolved (no MIND_META env "
            f"var, no META_PATH in <agent>/local-paths.conf). Plan v1 step "
            f"0.1 (2026-05-19) removed the PROJECT_ROOT/meta fallback.\n"
        )
        sys.stderr.write(msg)
        raise RuntimeError(
            f"{module_name}: META_DIR unresolved — external path not configured"
        )


def assert_agent_dir(module_name: str = "<unknown>") -> None:
    """Guard: raise loud RuntimeError if AGENT_DIR is None (MIND_AGENT not set).

    Class-B core/scripts modules that build module-level path constants
    from AGENT_DIR (e.g., `WM_PATH = AGENT_DIR / "session" / "working-memory.yaml"`
    at import time) MUST call this BEFORE the first join — otherwise an absent
    MIND_AGENT yields a silent `AGENT_DIR=None`, then `None / "subpath"` raises
    an opaque `TypeError: unsupported operand type(s) for /: 'NoneType' and 'str'`
    deep inside the import machinery with no hint of the missing env var.

    This helper replaces that opaque TypeError class with a single-source
    diagnostic that NAMES `MIND_AGENT` and points at the typical fix
    (`/start <agent>`). No agent-name fallback is introduced — the helper
    only raises when unset; single-source-of-truth (the MIND_AGENT env var)
    is preserved.

    Behavior when MIND_AGENT IS set: silent no-op (`return None`). Module-level
    callers see byte-identical behavior to the pre-guard path. g-115-794 /
    paired Apply for zeta g-115-793.
    """
    if AGENT_DIR is None:
        import sys
        msg = (
            f"ERROR: {module_name}: MIND_AGENT environment variable not set; "
            f"cannot resolve agent directory. This module builds module-level "
            f"path constants from AGENT_DIR and REQUIRES MIND_AGENT to be set "
            f"before import (typically via `/start <agent>` or "
            f"`MIND_AGENT=<name> <command>`). No fallback agent name is "
            f"defaulted — single-source-of-truth preserved.\n"
        )
        sys.stderr.write(msg)
        raise RuntimeError(
            f"{module_name}: MIND_AGENT not set — agent directory unresolved"
        )


def assert_environment_id(module_name: str = "<unknown>") -> None:
    """Guard: raise loud RuntimeError if ENVIRONMENT_ID is None.

    Consumers that require a world identity (cross-world guardrails G4/G5,
    multi-world DDB isolation Step 3.1, provenance metadata) call this before
    using ENVIRONMENT_ID to get a clear diagnostic instead of a silent None.

    When ENVIRONMENT_ID IS set: silent no-op. Mirrors assert_world_dir pattern.
    """
    if ENVIRONMENT_ID is None:
        msg = (
            f"ERROR: {module_name}: ENVIRONMENT_ID not configured. Set "
            f"ENVIRONMENT_ID=<world-name> in .env.local or as an env var. "
            f"See core/config/conventions/world-contract.md.\n"
        )
        sys.stderr.write(msg)
        raise RuntimeError(
            f"{module_name}: ENVIRONMENT_ID not configured — world identity unknown"
        )


def _longpath_safe(p: Path) -> Path:
    """Wrap a Windows path with the `\\\\?\\` extended-length prefix when it
    would exceed legacy MAX_PATH (260). Without this, Win32 exists/stat/open
    report FileNotFound on valid long paths. Idempotent; UNC paths not handled.
    """
    if os.name != "nt":
        return p
    s = str(p)
    if len(s) < 260 or s.startswith("\\\\?\\"):
        return p
    return Path("\\\\?\\" + s)


def resolve_file_path(virtual_path: str) -> Path:
    """Resolve a virtual file path to an absolute path.

    Contract: `world/...` → WORLD_DIR, `meta/...` → META_DIR, anything else →
    PROJECT_ROOT. Canonicalization of tree-node paths (ensuring they start
    with `world/knowledge/tree/`) is the writer's job — see
    `tree.py::normalize_virtual_path`. This reader has NO fallback for bare
    paths; they resolve to PROJECT_ROOT and fail visibly if the file is
    missing. Do not add prefix-recovery logic here — it masks writer bugs.

    Plan v1 step 0.1 (2026-05-19): raises RuntimeError when called with a
    `world/`- or `meta/`-prefixed path while the corresponding *_DIR is None
    (no MIND_WORLD/MIND_META env, no conf entry). The previous code did
    `None / virtual_path[6:]` which produced an opaque TypeError; the guard
    here turns it into a single-source diagnostic.
    """
    if virtual_path.startswith("world/"):
        assert_world_dir(f"resolve_file_path({virtual_path!r})")
        return _longpath_safe(WORLD_DIR / virtual_path[6:])
    if virtual_path.startswith("meta/"):
        assert_meta_dir(f"resolve_file_path({virtual_path!r})")
        return _longpath_safe(META_DIR / virtual_path[5:])
    return _longpath_safe(PROJECT_ROOT / virtual_path)
