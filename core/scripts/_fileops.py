"""Shared file operations: locking, history snapshots, changelog, and locked writes.

Imported by all write scripts to provide:
  - File locking (acquire_lock / release_lock)
  - Copy-on-write history (.history/ snapshots before overwriting)
  - Changelog auto-append (base_dir/changelog.jsonl)
  - Locked write functions (locked_write_jsonl, locked_append_jsonl, etc.)
"""

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from _paths import WORLD_DIR, META_DIR


# ---------------------------------------------------------------------------
# File Locking
# ---------------------------------------------------------------------------

def acquire_lock(lock_path, timeout=10):
    """Acquire a file lock. Breaks stale locks older than 30 seconds."""
    lock_path = Path(lock_path)
    start = time.time()
    while lock_path.exists():
        # Break stale locks (older than 30 seconds)
        try:
            if time.time() - lock_path.stat().st_mtime > 30:
                lock_path.unlink(missing_ok=True)
                break
        except FileNotFoundError:
            break  # Lock was released between exists() and stat()
        if time.time() - start > timeout:
            raise TimeoutError(f"Could not acquire lock: {lock_path}")
        time.sleep(0.1)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def release_lock(lock_path):
    """Release a file lock."""
    Path(lock_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# History Snapshots
# ---------------------------------------------------------------------------

def save_history(path, base_dir, agent_name, summary=""):
    """Save a copy of the current file to .history/ before overwriting.

    Args:
        path: The file being overwritten.
        base_dir: The root directory (e.g., WORLD_DIR or META_DIR).
            History is stored at base_dir/.history/<relative-path>/.
        agent_name: Who is making the change (for the filename).
        summary: Optional one-line description (stored in sidecar).
    """
    # Both must be resolved — callers may pass unresolved paths while
    # resolve_base_dir returns resolved paths. Mismatch breaks relative_to.
    path = Path(path).resolve()
    base_dir = Path(base_dir).resolve()
    if not path.exists():
        return  # Nothing to version — new file

    rel = path.relative_to(base_dir)
    history_dir = base_dir / ".history" / str(rel)
    history_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    ext = path.suffix
    snapshot = history_dir / f"{timestamp}_{agent_name}{ext}"
    shutil.copy2(str(path), str(snapshot))

    # Write optional summary sidecar (same name + .meta)
    if summary:
        meta_file = snapshot.with_suffix(snapshot.suffix + ".meta")
        meta_file.write_text(summary + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

def append_changelog(base_dir, agent_name, file_path, action, summary="", lines_changed=0):
    """Append an entry to base_dir/changelog.jsonl.

    Args:
        base_dir: Directory containing changelog.jsonl (typically WORLD_DIR).
        agent_name: Who made the change.
        file_path: The file that was changed (absolute or relative).
        action: One of: "create", "edit", "delete", "restore".
        summary: One-line description.
        lines_changed: Approximate number of lines affected.
    """
    base_dir = Path(base_dir)
    changelog = base_dir / "changelog.jsonl"

    # Make file_path relative to base_dir for readability
    try:
        rel_path = str(Path(file_path).resolve().relative_to(base_dir.resolve()))
    except ValueError:
        rel_path = str(file_path)

    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent_name,
        "file": rel_path,
        "action": action,
        "summary": summary,
        "lines_changed": lines_changed,
    }

    with open(changelog, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")


# ---------------------------------------------------------------------------
# Path Resolution
# ---------------------------------------------------------------------------

def resolve_base_dir(path):
    """Determine which base directory (WORLD_DIR or META_DIR) a path belongs to.

    Returns the base dir Path, or None if the path is under AGENT_DIR
    or doesn't match any configured directory.
    """
    path = Path(path).resolve()
    if WORLD_DIR:
        try:
            if path.is_relative_to(WORLD_DIR.resolve()):
                return WORLD_DIR.resolve()
        except (ValueError, OSError):
            pass
    if META_DIR:
        try:
            if path.is_relative_to(META_DIR.resolve()):
                return META_DIR.resolve()
        except (ValueError, OSError):
            pass
    return None


def _agent_name():
    """Get the current agent name, defaulting to 'system'."""
    return os.environ.get("AYOAI_AGENT", "system")


# ---------------------------------------------------------------------------
# Locked Write Operations
# ---------------------------------------------------------------------------
# These wrap lock + history + atomic write + changelog into single calls.
# Scripts delegate their write_jsonl/write_yaml/etc. to these.

def locked_write_jsonl(path, items):
    """Lock → history → atomic JSONL rewrite → changelog → unlock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=True) + "\n")
        os.replace(str(tmp), str(path))
        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=len(items))
    finally:
        release_lock(lock_path)


def locked_append_jsonl(path, item):
    """Lock → history → JSONL append → changelog → unlock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=True) + "\n")
        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=1)
    finally:
        release_lock(lock_path)


def locked_write_json(path, data):
    """Lock → history → atomic JSON rewrite → changelog → unlock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=True)
            f.write("\n")
        os.replace(str(tmp), str(path))
        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


def locked_write_yaml(path, data):
    """Lock → history → atomic YAML rewrite → changelog → unlock."""
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
        os.replace(str(tmp), str(path))
        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)
