#!/usr/bin/env python3
"""tree-front-matter-sync.py — Layer A of the tree-edit consistency stack.

PostToolUse hook helper, invoked by tree-sync-check.sh whenever a tree node
.md file is written or edited. Mutates ONLY the mechanical front-matter
fields where today's date or current SID is the unambiguous right answer:

  - last_updated → today's date (always overwrite)
  - last_update_trigger.session → current MIND_SID (always ensure)
  - last_update_trigger.source → in-flight goal_id from team-state
                                 (fill only if missing)

Layer A NEVER touches semantic fields — those need LLM judgment and belong
to Layer B (/tree edit Step 4):

  - topic
  - _tree.yaml summary
  - last_update_trigger.type

Also bumps the matching node's last_updated in _tree.yaml so retrieval
surfaces see fresh metadata, via locked_modify_yaml (the canonical write
path tree.py uses).

Refusal cases (the script EXITS without mutation, prints advisory):
  - Front-matter YAML is malformed
  - last_update_trigger exists in non-dict form (legacy scalar/list)

Fail-open on any unexpected error: stderr line, exit 0. Never blocks the edit.
"""

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
    from _paths import WORLD_DIR  # type: ignore
    from _fileops import locked_modify_yaml  # type: ignore
except Exception as e:
    print(f"tree-front-matter-sync: setup error: {e}", file=sys.stderr)
    sys.exit(0)


# Match the front-matter block but DO NOT consume any trailing newline after
# the closing '---'. We preserve the body verbatim so writes don't strip
# reader-friendly whitespace. Pattern handles both LF and CRLF (we normalize
# to LF before matching anyway, but keeping `\r?\n` is defense-in-depth).
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)

# Sentinel — distinguish empty FM (safe to populate) from malformed FM
# (must abort to avoid silently dropping user content).
_PARSE_ERROR = object()


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _read_file_lf(path):
    """Return (lf_text, line_ending) where lf_text uses '\\n' regardless of
    on-disk convention, and line_ending is '\\r\\n' or '\\n' for restoration.

    CRITICAL — DO NOT switch to read_text. Path.read_text on Windows
    converts CRLF to LF on read AND write_text converts LF back to CRLF on
    write, silently changing line endings for any file whose original
    convention disagrees with the platform default. Empirically verified
    2026-05-10: 977 of 985 tree node files are LF; using read_text/write_text
    would mass-convert them to CRLF on first hook fire. Binary I/O + explicit
    line-ending detection is the only safe pattern.
    """
    raw = path.read_bytes()
    lineend = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    return text, lineend


def _write_file_atomic(path, lf_text, lineend):
    """Atomic write: encode lf_text with the original line ending, write to
    tmp, os.replace into place. Binary mode — see _read_file_lf docstring."""
    out = lf_text.replace("\n", lineend).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp-fmsync")
    tmp.write_bytes(out)
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Front-matter parsing (read-only — never used for output)
# ---------------------------------------------------------------------------


def _split_front_matter(content):
    """Return (fm_text, body) or (None, content) if no front matter."""
    m = _FRONT_MATTER_RE.match(content)
    if not m:
        return None, content
    return m.group(1), content[m.end():]


def _parse_fm(fm_text):
    """Return parsed dict, {} if FM is empty, or _PARSE_ERROR on YAML error."""
    if not fm_text or not fm_text.strip():
        return {}
    try:
        data = yaml.load(fm_text, Loader=yaml.CSafeLoader)
    except yaml.YAMLError:
        return _PARSE_ERROR
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Surgical text mutators (preserve unrelated lines byte-for-byte)
# ---------------------------------------------------------------------------


def _set_root_scalar(fm_text, field, value):
    """Replace `field: <anything>` at column 0; insert at end if missing.
    Returns the new fm_text. Caller passes value pre-formatted (e.g. quoted)."""
    pat = re.compile(rf"^{re.escape(field)}:[ \t]*[^\n]*$", re.MULTILINE)
    new_line = f"{field}: {value}"
    new_text, n = pat.subn(new_line, fm_text, count=1)
    if n > 0:
        return new_text
    sep = "" if not fm_text or fm_text.endswith("\n") else "\n"
    return fm_text + sep + new_line


def _block_end_offset(fm_text, after_parent_offset):
    """Given offset just after a `parent:` line's newline, return the offset
    where the parent's block ends — first subsequent line at column 0, or end."""
    after = fm_text[after_parent_offset:]
    m = re.search(r"^[^ \t\n]", after, re.MULTILINE)
    return after_parent_offset + (m.start() if m else len(after))


def _set_nested_scalar(fm_text, parent, child, value, default_indent="  "):
    """Replace `<indent>child: <anything>` inside the block under `parent:`.
    Inserts the child line at end of block if missing. Inserts the entire
    block (parent + child) at end of fm_text if parent is missing.

    Returns new fm_text. Returns None if `parent:` exists in INLINE form
    (e.g. `parent: foo` or `parent: {a: 1}`) — caller should refuse.
    """
    parent_line = re.search(rf"^{re.escape(parent)}:[ \t]*$", fm_text, re.MULTILINE)
    if not parent_line:
        # Maybe inline form?
        if re.search(rf"^{re.escape(parent)}:[ \t]+\S", fm_text, re.MULTILINE):
            return None
        # Parent missing — append a fresh block at end
        sep = "" if not fm_text or fm_text.endswith("\n") else "\n"
        return f"{fm_text}{sep}{parent}:\n{default_indent}{child}: {value}"

    # Parent exists as block. Move past its terminating newline (if any).
    after_parent = parent_line.end()
    if after_parent < len(fm_text) and fm_text[after_parent] == "\n":
        after_parent += 1

    block_end = _block_end_offset(fm_text, after_parent)
    block = fm_text[after_parent:block_end]

    # Detect indent from first existing child line
    indent = default_indent
    for line in block.split("\n"):
        if line and line[:1] in (" ", "\t"):
            stripped = line.lstrip(" \t")
            indent = line[:len(line) - len(stripped)]
            break

    # Try to replace existing child within the block
    pat = re.compile(rf"^{re.escape(indent)}{re.escape(child)}:[ \t]*[^\n]*$", re.MULTILINE)
    new_child_line = f"{indent}{child}: {value}"
    new_block, n = pat.subn(new_child_line, block, count=1)
    if n > 0:
        return fm_text[:after_parent] + new_block + fm_text[block_end:]

    # Child not found — append at end of block, before any trailing newlines
    block_stripped = block.rstrip("\n")
    trailing = block[len(block_stripped):]  # preserve trailing newlines
    new_block = f"{block_stripped}\n{new_child_line}{trailing}" if block_stripped else f"{new_child_line}\n"
    return fm_text[:after_parent] + new_block + fm_text[block_end:]


# ---------------------------------------------------------------------------
# External lookups
# ---------------------------------------------------------------------------


def _read_in_flight_goal(agent):
    """Read the agent's in_flight.goal_id from its team-state row; '' if absent.
    g-328-27 sharding: row file first, core-file residual fallback."""
    if not agent:
        return ""
    try:
        from _team_state import read_agent_row
        info = read_agent_row(Path(WORLD_DIR), agent,
                              core_path=Path(WORLD_DIR) / "team-state.yaml") or {}
    except Exception:
        return ""
    inflight = info.get("in_flight") or {}
    gid = inflight.get("goal_id")
    return str(gid) if gid else ""


def _find_tree_key(virtual_path):
    """Scan _tree.yaml for the node whose `file:` matches virtual_path. '' if none."""
    tree_yaml = Path(WORLD_DIR) / "knowledge" / "tree" / "_tree.yaml"
    if not tree_yaml.is_file():
        return ""
    try:
        data = yaml.load(tree_yaml.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader) or {}
    except (OSError, yaml.YAMLError):
        return ""
    nodes = data.get("nodes") or {}
    for key, node in nodes.items():
        if isinstance(node, dict) and node.get("file") == virtual_path:
            return key
    return ""


def _bump_tree_yaml_last_updated(key, today):
    """Atomically set nodes[key].last_updated AND top-level last_updated.
    Returns True on success, False on any failure (fail-open hook)."""
    if not key:
        return False
    tree_yaml = Path(WORLD_DIR) / "knowledge" / "tree" / "_tree.yaml"
    if not tree_yaml.is_file():
        return False

    def _do(data):
        if not isinstance(data, dict):
            return data
        nodes = data.get("nodes") or {}
        if key in nodes and isinstance(nodes[key], dict):
            nodes[key]["last_updated"] = today
            data["nodes"] = nodes
            data["last_updated"] = today
        return data

    # Broad except is intentional: hook MUST fail open per guard-141.
    # Lock timeout, YAML parse error, file-permission failure — all silently
    # surface to stderr and continue. Never block the user's edit.
    try:
        locked_modify_yaml(str(tree_yaml), _do)
        return True
    except Exception as e:
        print(f"tree-front-matter-sync: _tree.yaml update failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True, help="Absolute path to .md file just edited")
    p.add_argument("--virtual-path", required=True, help="world/knowledge/tree/... virtual path")
    args = p.parse_args()

    md_path = Path(args.file)
    if not md_path.is_file():
        sys.exit(0)

    try:
        text, lineend = _read_file_lf(md_path)
    except OSError as e:
        print(f"tree-front-matter-sync: read failed: {e}", file=sys.stderr)
        sys.exit(0)

    if not text.strip():
        sys.exit(0)

    today = date.today().isoformat()
    sid = os.environ.get("MIND_SID", "").strip()
    # Subprocess-per-hook model: MIND_AGENT at this point is the same value
    # _paths read at module import (single process, no env mutation between
    # import and here). No need to dual-read from AGENT_NAME.
    agent = os.environ.get("MIND_AGENT", "").strip()
    inflight_goal = _read_in_flight_goal(agent)

    fm_text, body = _split_front_matter(text)
    fm_dict = _parse_fm(fm_text) if fm_text is not None else {}

    if fm_dict is _PARSE_ERROR:
        # Refuse — silently rewriting would drop user content.
        print(
            f"FRONT-MATTER-SYNC: {args.virtual_path} — REFUSED: front-matter YAML is malformed. "
            f"Fix the YAML at the top of the file, then re-edit to trigger Layer A.",
            file=sys.stderr,
        )
        sys.exit(0)

    existing_trigger = fm_dict.get("last_update_trigger")
    if existing_trigger is not None and not isinstance(existing_trigger, dict):
        # Refuse — non-dict trigger is legacy/unexpected; coercing would
        # destroy the existing scalar (4 such nodes exist as of 2026-05-10).
        # Layer B's /tree edit walk is the right place to migrate.
        print(
            f"FRONT-MATTER-SYNC: {args.virtual_path} — REFUSED: last_update_trigger is "
            f"{type(existing_trigger).__name__} (expected dict). Migrate to dict form via "
            f"/tree edit before Layer A can update it.",
            file=sys.stderr,
        )
        sys.exit(0)

    # All checks passed — perform surgical mutations on the FM text.
    # We use the parsed dict ONLY for read decisions (refusal checks above,
    # and the source-already-set check below). All writes are surgical so
    # unrelated lines remain byte-identical.
    new_fm_text = fm_text or ""
    new_fm_text = _set_root_scalar(new_fm_text, "last_updated", f"'{today}'")

    # Layer A's PRIMARY job is keeping last_updated in sync (.md front matter
    # above via _set_root_scalar, and _tree.yaml below via
    # _bump_tree_yaml_last_updated). Auto-filling trigger.session /
    # trigger.source is SECONDARY and only works when the trigger is in BLOCK
    # form (`last_update_trigger:` on its own line with `  type: ...` children).
    # _set_nested_scalar returns None for an INLINE-form dict
    # (`last_update_trigger: {type: ...}`) — the form most skill instructions
    # actually emit. The original code exited(0) on that None, which ABORTED
    # the primary last_updated sync and silently left _tree.yaml stale for
    # every inline-trigger node. (The deleted "should not reach here" comment
    # proved the path was believed unreachable: an inline dict passes the
    # is-dict refusal check at line ~302 but still hits this nested-write None.)
    # Fix (2026-06-04, _tree.yaml drift incident, node
    # windows-maxpath-pathresolution lagged 5 months): a None result skips
    # ONLY the nested write and FALLS THROUGH to the last_updated bump. Block
    # form still gets session/source auto-filled; inline form keeps whatever
    # session/source it was authored with (skills emit those inline already)
    # and still stays date-synced. The string-form refusal at line ~302 is
    # deliberate (legacy → Layer-B /tree-edit migration) and is preserved.
    inline_trigger_seen = False
    if sid:
        result = _set_nested_scalar(new_fm_text, "last_update_trigger", "session", sid)
        if result is None:
            inline_trigger_seen = True  # inline form — skip nested, keep date sync
        else:
            new_fm_text = result

    existing_source = (existing_trigger or {}).get("source") if isinstance(existing_trigger, dict) else None
    if inflight_goal and not existing_source:
        result = _set_nested_scalar(new_fm_text, "last_update_trigger", "source", inflight_goal)
        if result is None:
            inline_trigger_seen = True
        else:
            new_fm_text = result

    # Reconstruct file content (still in LF land)
    if fm_text is None:
        original = text.lstrip("\n")
        new_text = (
            f"---\n{new_fm_text}\n---\n\n{original}"
            if original
            else f"---\n{new_fm_text}\n---\n"
        )
    else:
        new_text = f"---\n{new_fm_text}\n---{body}"

    if new_text != text:
        try:
            _write_file_atomic(md_path, new_text, lineend)
        except OSError as e:
            print(f"tree-front-matter-sync: write failed {md_path}: {e}", file=sys.stderr)
            sys.exit(0)

    # Bump _tree.yaml node last_updated (independent of .md write success)
    key = _find_tree_key(args.virtual_path)
    bumped_yaml = _bump_tree_yaml_last_updated(key, today) if key else False

    # Advisory: surface what changed and whether trigger.type is unset
    trigger_type = (existing_trigger or {}).get("type") if isinstance(existing_trigger, dict) else None
    parts = [f"last_updated→{today}"]
    if sid and not inline_trigger_seen:
        parts.append(f"session→{sid[:12]}…")
    if existing_source:
        parts.append("source preserved")
    elif inflight_goal and not inline_trigger_seen:
        parts.append(f"source→{inflight_goal}")
    if inline_trigger_seen:
        parts.append("trigger inline — session/source not auto-filled (run /tree edit to fill, Layer B)")
    if key and bumped_yaml:
        parts.append(f"_tree.yaml[{key}]✓")
    summary = ", ".join(parts)

    if not trigger_type:
        print(
            f"FRONT-MATTER-SYNC: {args.virtual_path} — {summary}. "
            f"⚠ trigger.type unset — if this was a semantic edit, run /tree edit "
            f"to evaluate summary/topic/type (Layer B).",
            file=sys.stderr,
        )
    else:
        print(
            f"FRONT-MATTER-SYNC: {args.virtual_path} — {summary} (trigger.type={trigger_type})",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
