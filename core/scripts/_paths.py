"""Centralized path resolution for the cognitive core.

4-tier architecture:
  core/          — Framework (immutable)
  meta/          — Meta-strategies (domain-agnostic, survives domain reset)
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
    """Resolve agent name: AYOAI_AGENT env var > .active-agent file.
    If AYOAI_AGENT is explicitly set (even to empty), it takes priority."""
    if "AYOAI_AGENT" in os.environ:
        return os.environ["AYOAI_AGENT"].strip()
    af = PROJECT_ROOT / ".active-agent"
    if af.exists():
        return af.read_text(encoding="utf-8").strip()
    return ""

def _read_local_paths():
    """Read <agent>/local-paths.conf if agent is bound."""
    agent = _resolve_agent_name()
    if not agent:
        return {}
    conf = PROJECT_ROOT / agent / "local-paths.conf"
    if conf.exists():
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
# Resolution priority: AYOAI_AGENT env var > .active-agent file in project root.
# Pure-agent scripts (session.py, curriculum.py, journal.py, etc.) will crash
# at import time if no agent is bound — this is intentional fail-fast.
# Mixed scripts (retrieve.py, goal-selector.py) guard agent paths at use sites.
AGENT_NAME = _resolve_agent_name()
AGENT_DIR = PROJECT_ROOT / AGENT_NAME if AGENT_NAME else None

# Legacy alias — points to AGENT_DIR when set. Scripts being migrated from
# MIND_DIR should switch to WORLD_DIR (collective) or AGENT_DIR (per-agent).
MIND_DIR = AGENT_DIR
