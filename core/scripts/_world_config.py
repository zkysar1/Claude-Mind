"""Read domain-specific config from world/config/ overlay.

Pattern: core ships generic; world ships domain. `core/scripts/_world_config.py`
is the single entry point for reading world overlay files with safe defaults.

The framework's design principle is that `core/` ships behaviorally complete
with safe-empty defaults — a fresh deployment gets a clean classifier that
returns "either"/"uncertain" rather than wrong-domain answers. The host
deployment's `world/config/*.yaml` files contain the domain-specific tables
(agent names, product category prefixes, classification heuristics) and
override the core defaults at script-load time.

When `world/config/<name>.yaml` is missing OR malformed, the helper returns
the `default` dict — the script behaves as if the overlay were empty, which
is the safe-defaults posture. Errors are swallowed (telemetry-only), never
raised — a broken world overlay must never crash core script execution.

Usage:
    from _world_config import load_world_config
    cfg = load_world_config(
        "scaffolded-exploration",
        default={"product_category_prefixes": []},
    )
    prefixes = tuple(cfg.get("product_category_prefixes") or [])

Cached per-process. Daemon endpoints that need to react to a hot world-config
edit can call `clear_cache()` (e.g., after a post-commit reload signal).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

_CACHE: Dict[str, Dict[str, Any]] = {}


# --- Agent-dir resolution (Phase 2.5.C) ---
# Inlined here (not imported from _paths.py) to stay import-cycle-proof.
# MUST stay in sync with core/scripts/_paths.py, _paths.sh, _agents.py, and
# mind_api/src/agent_paths.py. See CLAUDE.md "Agent-dir Resolution" section.
AGENTS_PARENT_DIR = "agents"


def _project_root() -> Path:
    # Resolve without importing _paths.py — keeps this helper import-cycle-proof.
    return Path(__file__).resolve().parent.parent.parent


def _agents_root(project_root: Path) -> Path:
    """Directory containing all agent subdirs. Import-cycle-proof inline."""
    return project_root / AGENTS_PARENT_DIR if AGENTS_PARENT_DIR else project_root


def _resolve_world_dir() -> Optional[Path]:
    """Return the absolute path to world/, or None if not resolvable.

    Priority:
      1. MIND_WORLD env var (matches _paths.sh resolution)
      2. WORLD_PATH from the bound agent's local-paths.conf
      3. WORLD_PATH from any agent's local-paths.conf (first found)
      4. PROJECT_ROOT/world fallback
    """
    env = os.environ.get("MIND_WORLD", "").strip()
    if env:
        p = Path(env)
        if p.is_dir():
            return p

    root = _project_root()
    agents_root = _agents_root(root)
    agent = os.environ.get("MIND_AGENT", "").strip()
    candidates: list[Path] = []
    if agent:
        candidates.append(agents_root / agent / "local-paths.conf")
    try:
        for child in sorted(agents_root.iterdir()):
            if child.is_dir() and (child / "local-paths.conf").is_file():
                conf = child / "local-paths.conf"
                if conf not in candidates:
                    candidates.append(conf)
    except OSError:
        pass

    for conf in candidates:
        if not conf.is_file():
            continue
        try:
            for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("WORLD_PATH="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    p = Path(val)
                    if p.is_dir():
                        return p
        except OSError:
            continue

    fallback = root / "world"
    return fallback if fallback.is_dir() else None


def load_world_config(name: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load `world/config/<name>.yaml` and return its parsed dict.

    Args:
        name: Config file basename without extension (e.g., "capability-routing").
        default: Returned (a copy) when the file is missing, empty, malformed,
            or unparseable. None is normalized to {}.

    Returns:
        The parsed dict, or a copy of `default` on any failure path.
    """
    if default is None:
        default = {}
    if name in _CACHE:
        # Defensive copy on cache hit — without this, the second caller
        # would inherit the first caller's mutations. The cache holds the
        # canonical snapshot; every caller gets a fresh shallow copy so
        # in-process callers cannot pollute each other or the cache.
        # rb-1050 /  (fresh-eyes-code finding 2026-05-18).
        return dict(_CACHE[name])

    world = _resolve_world_dir()
    if world is None:
        result = dict(default)
    else:
        path = world / "config" / f"{name}.yaml"
        # own-cloud read-path fix (2026-07-02): materialize an S3-only overlay on
        # a fresh box BEFORE the is_file() gate, else every world/config overlay
        # silently degrades to defaults (the  config-404 class). Lazy,
        # fail-open import (this loader is defensive by design and stays import-
        # cycle-proof); no-op on LocalBackend and for out-of-root paths (keystone).
        try:
            from storage_backend import get_backend  # noqa: PLC0415
            get_backend().ensure_local(path)
        except Exception:
            pass
        if not path.is_file():
            result = dict(default)
        else:
            try:
                import yaml  # noqa: PLC0415
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    # Empty file or top-level non-mapping (e.g. list). Treat
                    # as missing — silently use defaults. Common when overlay
                    # stub is just "key: {}\n" placeholder.
                    result = dict(default)
                else:
                    # Defensive copy of the parsed payload — protects the
                    # cache slot from yaml.safe_load's output being shared
                    # with anything else (rb-1050 asymmetry).
                    result = dict(data)
            except Exception as e:
                # Malformed YAML (syntax error, encoding issue, etc.) —
                # distinct from the missing-file case checked above. Surface
                # to stderr so users notice their overlay is broken; still
                # return safe defaults so the caller doesn't crash.
                # Fresh-eyes review MEDIUM M4 (2026-05-18): silent swallow
                # made corrupted overlays indistinguishable from missing.
                import sys  # noqa: PLC0415
                try:
                    print(
                        f"[_world_config] WARN: failed to parse {path}: "
                        f"{type(e).__name__}: {e} — using safe defaults "
                        f"({sorted(default.keys())})",
                        file=sys.stderr,
                    )
                except Exception:
                    pass  # never let logging crash the loader
                result = dict(default)

    _CACHE[name] = result
    # Defensive copy on first-store return — symmetric with the cache-hit
    # path above. Without this, the first caller's mutations would pollute
    # the cached snapshot for every subsequent in-process call.
    return dict(result)


def clear_cache(name: Optional[str] = None) -> None:
    """Invalidate the cache. Pass `name` to clear one entry, omit to clear all."""
    global _CACHE
    if name is None:
        _CACHE = {}
    else:
        _CACHE.pop(name, None)


if __name__ == "__main__":
    # Smoke test / diagnostic — `py -3 core/scripts/_world_config.py <name>`
    import json
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "capability-routing"
    cfg = load_world_config(name, default={"_default": True})
    print(json.dumps({"name": name, "resolved_keys": sorted(cfg.keys())}, indent=2))
