#!/usr/bin/env python3
"""Session Snapshot — atomic stat of every file in <agent>/session/.

Reads core/config/session-manifest.yaml, stats every listed file inside the
resolved AGENT_DIR/session/ directory, and emits a JSON record:

  {
    "agent": "<name>",
    "timestamp": "<ISO>",
    "files": [
      {"file": "agent-state", "exists": true, "mtime": "<ISO>", "size": 7,
       "recovery_action": "preserve", "required_when_idle": true,
       "required_when_running": true},
      ...
    ],
    "orphans": ["<unlisted-file-in-session/>", ...],
    "invariants": [ ... raw copy of manifest invariants ... ]
  }

Also emits orphan detection: files present in session/ that are NOT in the
manifest. Those are warning candidates for the desync checker (new session
files that were added but forgot to be registered).

Consumers:
  - /start --recover Phase 0.7 (iterates files with recovery_action=clear)
  - session-desync-check.sh (compares against invariants)
  - /boot Phase -0.5 (future: sanity-check on startup)

Fail-open on read errors: prints a diagnostic to stderr and emits a partial
snapshot. Snapshot consumers must handle missing fields gracefully.
"""

import argparse
import datetime
import fnmatch
import json
import os
import sys
from pathlib import Path

# Resolve framework paths via _paths (the canonical loader)
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    from _paths import AGENT_DIR, CORE_ROOT  # type: ignore
except Exception as e:
    print(f"ERROR: failed to import _paths: {e}", file=sys.stderr)
    sys.exit(2)


def _load_manifest(manifest_path):
    """Minimal YAML parser for the manifest. Avoids PyYAML dependency.

    The manifest is a hand-curated config with a narrow schema: top-level
    keys `files:` and `invariants:`, each a list of dicts with scalar values.
    We parse just what we need. On any structural surprise, raise — the
    manifest is framework config; corrupt parse is a bug, not data drift.
    """
    text = Path(manifest_path).read_text(encoding="utf-8")
    lines = text.splitlines()

    files = []
    invariants = []
    section = None  # "files" | "invariants" | None
    current = None

    def _flush():
        # CRITICAL: must run at every section boundary and at end-of-file.
        # Without the transition flush, the LAST entry of section A gets
        # appended to section B's list when the new list-item line is
        # encountered (because `current` still holds it, and the append
        # uses the NEW section). Removing this helper re-introduces the
        # silent-drop bug where `compact-checkpoint.json` landed in the
        # invariants list with no `id`.
        nonlocal current
        if current is not None:
            (files if section == "files" else invariants).append(current)
            current = None

    for raw in lines:
        line = raw.rstrip()
        stripped = line.lstrip()
        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue
        # Section headers — flush the in-flight item before retargeting.
        if line == "files:":
            _flush()
            section = "files"
            continue
        if line == "invariants:":
            _flush()
            section = "invariants"
            continue
        if section is None:
            continue
        # New list item starts with "- "
        if stripped.startswith("- "):
            if current is not None:
                (files if section == "files" else invariants).append(current)
            current = {}
            # First field on the same line as "- ": "- file: agent-state"
            rest = stripped[2:]
            if ":" in rest:
                k, _, v = rest.partition(":")
                current[k.strip()] = _coerce(v.strip())
            continue
        # Continuation of current item: "  file: foo"
        if current is not None and ":" in stripped:
            k, _, v = stripped.partition(":")
            current[k.strip()] = _coerce(v.strip())

    _flush()
    return {"files": files, "invariants": invariants}


def _coerce(v):
    """Coerce common scalar forms: true/false, integers, strings."""
    if v == "":
        return ""
    # Strip surrounding quotes if present
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    return v


def _iso_mtime(st):
    return datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Session snapshot — stat every manifest-listed file in "
            "<agent>/session/ and emit JSON. Also detects orphan files."
        )
    )
    ap.add_argument("--manifest", default=None,
                    help="Path to session-manifest.yaml (default: core/config/session-manifest.yaml).")
    ap.add_argument("--agent-dir", default=None,
                    help="Override AGENT_DIR (default: resolved via _paths).")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    args = ap.parse_args(argv)

    agent_dir = Path(args.agent_dir) if args.agent_dir else Path(AGENT_DIR) if AGENT_DIR else None
    if agent_dir is None or not agent_dir.is_dir():
        print(f"ERROR: agent dir not found: {agent_dir}", file=sys.stderr)
        sys.exit(2)
    session_dir = agent_dir / "session"
    if not session_dir.is_dir():
        # Not an error — fresh agent with no session yet.
        result = {
            "agent": agent_dir.name,
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "session_dir_exists": False,
            "files": [],
            "orphans": [],
            "invariants": [],
        }
        return _emit(result, args.output)

    manifest_path = Path(args.manifest) if args.manifest else Path(CORE_ROOT) / "config" / "session-manifest.yaml"
    try:
        manifest = _load_manifest(manifest_path)
    except Exception as e:
        print(f"ERROR: manifest parse failed ({manifest_path}): {e}", file=sys.stderr)
        sys.exit(2)

    known_names = set()
    glob_patterns = []   # list of fnmatch patterns from glob: true entries
    files_out = []
    for entry in manifest["files"]:
        name = entry.get("file")
        if not name:
            continue
        known_names.add(name)
        # type=dir entries (e.g. scratch/) check is_dir() instead of is_file().
        # Size for dirs is None — recursive sizing would slow down every
        # snapshot call and the consumer (recovery clear, desync check) only
        # needs existence, not size.
        # glob: true entries treat `name` as an fnmatch pattern (e.g.
        # aspirations-incremented-session-*.txt). Existence = ANY match present.
        # Matched filenames are also added to known_names equivalents (via
        # glob_patterns) so the orphan scan does not re-flag them.
        entry_type = entry.get("type", "file")
        is_dir_entry = entry_type == "dir"
        is_glob_entry = bool(entry.get("glob"))
        if is_glob_entry:
            glob_patterns.append(name)
            # Lexical sort for deterministic display order. The newest-by-mtime
            # is computed separately below — DO NOT collapse the two: lexical
            # order puts session-9 after session-10, so matches[-1] would be
            # the wrong file for the mtime field.
            matches = sorted(session_dir.glob(name))
            record = {
                "file": name,
                "type": entry_type,
                "glob": True,
                "exists": len(matches) > 0,
                "match_count": len(matches),
                "matches": [m.name for m in matches],
                "mtime": None,
                "size": None,
                "recovery_action": entry.get("recovery_action"),
                "required_when_idle": entry.get("required_when_idle"),
                "required_when_running": entry.get("required_when_running"),
                "purpose": entry.get("purpose"),
            }
            if matches:
                try:
                    stats = [(m, m.stat()) for m in matches]
                    _, newest_stat = max(stats, key=lambda ms: ms[1].st_mtime)
                    record["mtime"] = _iso_mtime(newest_stat)
                except Exception as e:
                    record["stat_error"] = f"{type(e).__name__}: {e}"
        else:
            p = session_dir / name
            record = {
                "file": name,
                "type": entry_type,
                "exists": p.is_dir() if is_dir_entry else p.is_file(),
                "mtime": None,
                "size": None,
                "recovery_action": entry.get("recovery_action"),
                "required_when_idle": entry.get("required_when_idle"),
                "required_when_running": entry.get("required_when_running"),
                "purpose": entry.get("purpose"),
            }
            if record["exists"]:
                try:
                    st = p.stat()
                    record["mtime"] = _iso_mtime(st)
                    if not is_dir_entry:
                        record["size"] = st.st_size
                except Exception as e:
                    record["stat_error"] = f"{type(e).__name__}: {e}"
        files_out.append(record)

    # Orphans: files present in session/ but not in manifest.
    # Only count regular files at the top level — subdirs (e.g., journal/)
    # are tracked elsewhere and would be noise.
    #
    # CRITICAL: glob_patterns must be checked here — every glob entry's
    # pattern, regardless of recovery_action or whether it currently has
    # matches. Removing this check causes registered glob-pattern files
    # (e.g. aspirations-incremented-session-N.txt) to be re-flagged as
    # orphans on every snapshot, defeating the registration.
    orphans = []
    try:
        for entry in session_dir.iterdir():
            if entry.is_file() and entry.name not in known_names:
                # Ignore dotfiles and common noise
                if entry.name.startswith("."):
                    continue
                if any(fnmatch.fnmatch(entry.name, pat) for pat in glob_patterns):
                    continue
                orphans.append(entry.name)
    except Exception as e:
        print(f"WARN: orphan scan failed: {e}", file=sys.stderr)

    result = {
        "agent": agent_dir.name,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "session_dir_exists": True,
        "files": files_out,
        "orphans": sorted(orphans),
        "invariants": manifest["invariants"],
    }
    return _emit(result, args.output)


def _emit(result, output_format):
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"Agent: {result['agent']}")
        print(f"Timestamp: {result['timestamp']}")
        print(f"Session dir exists: {result.get('session_dir_exists')}")
        print(f"Files tracked: {len(result['files'])}  Orphans: {len(result['orphans'])}")
        for f in result["files"]:
            flag = "✓" if f["exists"] else "·"
            mtime = f["mtime"] or "---"
            print(f"  [{flag}] {f['file']:<32} mtime={mtime}  action={f['recovery_action']}")
        if result["orphans"]:
            print("Orphans:")
            for o in result["orphans"]:
                print(f"  ? {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
