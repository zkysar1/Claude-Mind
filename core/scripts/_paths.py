"""Centralized path resolution for the cognitive core.

4-tier architecture:
  core/          — Framework (immutable)
  meta/          — Meta-strategies (domain-agnostic, independent of domain data)
  world/         — Collective domain state (shared across agents)
  <agent-name>/  — Per-agent private state (one directory per agent)
"""
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent          # core/
PROJECT_ROOT = CORE_ROOT.parent        # project root
CONFIG_DIR = CORE_ROOT / "config"
REPO_ROOT = PROJECT_ROOT               # legacy alias


# --- External path configuration ---
# world/ and meta/ live at user-supplied external paths (shared drive, NAS, etc.).
# Each agent stores its own config in <agent>/local-paths.conf.
# Falls back to PROJECT_ROOT/world and PROJECT_ROOT/meta when unconfigured —
# this keeps all scripts importable before /start.
def _resolve_agent_name():
    """Resolve agent name from AYOAI_AGENT env var. One path, no fallbacks."""
    return os.environ.get("AYOAI_AGENT", "").strip()

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

    When AYOAI_AGENT is unset (hooks, background processes), uses the first
    available local-paths.conf to prevent writes to PROJECT_ROOT/world.
    """
    agent = _resolve_agent_name()
    if agent:
        conf = PROJECT_ROOT / agent / "local-paths.conf"
        if conf.exists():
            return _parse_conf(conf)
        return {}

    # AYOAI_AGENT unset — use first available conf (hooks don't have the env var)
    for conf in sorted(PROJECT_ROOT.glob("*/local-paths.conf")):
        return _parse_conf(conf)
    return {}

_local = _read_local_paths()

# --- Tier 2: Meta-strategies ---
# Priority: env var > config file > PROJECT_ROOT/meta
META_DIR = Path(os.environ.get("AYOAI_META",
                _local.get("META_PATH", str(PROJECT_ROOT / "meta"))))

# --- Tier 3: Collective domain state ---
# Priority: env var > config file > PROJECT_ROOT/world
WORLD_DIR = Path(os.environ.get("AYOAI_WORLD",
                 _local.get("WORLD_PATH", str(PROJECT_ROOT / "world"))))

# --- Tier 4: Per-agent private state ---
# Resolution: AYOAI_AGENT env var. One path.
AGENT_NAME = _resolve_agent_name()
AGENT_DIR = PROJECT_ROOT / AGENT_NAME if AGENT_NAME else None


def resolve_file_path(virtual_path: str) -> Path:
    """Resolve a virtual file path (as stored in _tree.yaml) to an absolute path.

    Node file paths are stored as 'world/knowledge/tree/foo.md' but 'world/'
    is an external directory. This function maps the prefix to the real location.
    """
    if virtual_path.startswith("world/"):
        return WORLD_DIR / virtual_path[6:]
    if virtual_path.startswith("meta/"):
        return META_DIR / virtual_path[5:]
    return PROJECT_ROOT / virtual_path
