"""Shared config-overrides overlay helper.

Implements the "<file-stem>.<in-file-dotted-path>" override-key convention
referenced in world/conventions/capability-routing.md and rb-302. Any script
reading a core/config/*.yaml modifiable param should route the read through
this helper so meta/config-overrides.yaml entries take effect.

Resolution rule (same as tree.py::_merged_config):
    meta/config-overrides.yaml[<file-stem>.<path>] ?? core/config/<file>[<path>]

Override entry values may be raw scalars OR dicts with a `value` field (the
shape `meta-set.sh` writes — value/previous/changed_date/reason). Dict
entries without `value` are rejected at source to prevent silent drift.

Unknown dotted paths are rejected — a typo like `tree.config.decompose_treshold`
must never silently create a bogus parameter in the merged config.

DO NOT add a fallback to avoid raising on missing override files — a quietly
skipped override is worse than a loud fail (rb-215 single-source-of-truth).
Callers should only treat `FileNotFoundError` on the override file as "no
overrides in effect", which `merged_config` handles internally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from _paths import CONFIG_DIR, META_DIR


def _apply_override(cfg: Dict[str, Any], in_file_path: str, entry: Any) -> None:
    """Navigate `cfg` to in_file_path and overwrite with entry value.

    Matches tree.py::_merged_config semantics — the target key must already
    exist as a dict entry; otherwise the override is silently ignored (guards
    against typos creating phantom keys).
    """
    val = entry["value"] if isinstance(entry, dict) else entry
    parts = in_file_path.split(".")
    target: Any = cfg
    for p in parts[:-1]:
        if not isinstance(target, dict) or p not in target:
            return
        target = target[p]
    if isinstance(target, dict) and parts[-1] in target:
        target[parts[-1]] = val


def merged_config(config_filename: str) -> Dict[str, Any]:
    """Read `core/config/<config_filename>` and overlay matching override entries.

    config_filename:
        Filename inside core/config/ (e.g. "aspirations.yaml"). The stem
        (filename minus `.yaml`) becomes the override prefix.

    Returns:
        Dict representation of the config with overrides applied.

    Raises:
        FileNotFoundError if the base config file does not exist — the caller
        is reading a param that has no config file, which is always a bug.
    """
    base_path = CONFIG_DIR / config_filename
    with open(base_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    stem = Path(config_filename).stem
    prefix = f"{stem}."

    overrides_path = META_DIR / "config-overrides.yaml"
    if not overrides_path.exists():
        return cfg

    with open(overrides_path, "r", encoding="utf-8") as f:
        override_doc = yaml.safe_load(f) or {}

    overrides = override_doc.get("overrides", {}) or {}
    for dotted_key, entry in overrides.items():
        if not dotted_key.startswith(prefix):
            continue
        in_file_path = dotted_key[len(prefix):]
        _apply_override(cfg, in_file_path, entry)
    return cfg
