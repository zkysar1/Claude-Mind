"""Centralized path resolution for the cognitive core."""
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent          # core/
PROJECT_ROOT = CORE_ROOT.parent        # project root
MIND_DIR = PROJECT_ROOT / "mind"
CONFIG_DIR = CORE_ROOT / "config"
# Legacy compat
REPO_ROOT = PROJECT_ROOT
