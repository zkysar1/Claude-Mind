#!/usr/bin/env python3
"""PostToolUse[Write|Edit|MultiEdit] hook — Phase a of D1, finalization step.

Reads the sidecar that evolution-prepare.py wrote before the edit fired,
computes after-state, classifies the change, and appends a STUB entry
(status=awaiting_completion) to the appropriate event stream JSONL.

Stub entries are completed later by evolution-complete.sh (Phase b of D1)
when the LLM, prompted by guardrail action_hint, supplies the `reasoning` +
`signal_source` + `signal_evidence` fields.

Event stream routing (5 files in WORLD_DIR):
  - agent_self  → world/self-evolution.jsonl
  - program     → world/program-evolution.jsonl
  - skill_edit  → world/skill-evolution.jsonl
  - rule_edit   → world/rule-evolution.jsonl
  - script_edit → world/script-evolution.jsonl

SAFETY: fail-open. Any error → silent exit 0. PostToolUse hooks cannot
block (and shouldn't) — at this point the edit has already applied.

Per world/conventions/self-program-evolution.md
"""

import sys
import os
import json
import hashlib
import re
import difflib
import secrets
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from hook_helpers import (
    approve_no_mutation,
    extract_file_path,
    is_absolute_path,
    stdin_json_or_approve,
)


# ---------------------------------------------------------------------------
# Body hash (§5.4a) — shared with evolution-prepare.py
# ---------------------------------------------------------------------------

def compute_body_hash(file_path):
    if not Path(file_path).exists():
        return None
    raw = Path(file_path).read_text(encoding="utf-8")
    if raw.startswith("---\n"):
        end_idx = raw.find("\n---\n", 4)
        if end_idx >= 0:
            body = raw[end_idx + 5:]
        else:
            body = raw
    else:
        body = raw
    lines = [ln.rstrip() for ln in body.replace("\r\n", "\n").split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    normalized = "\n".join(lines) + "\n"
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def get_body_text(file_path):
    """Return the body (post-front-matter) text of a file, normalized."""
    raw = Path(file_path).read_text(encoding="utf-8") if Path(file_path).exists() else ""
    if raw.startswith("---\n"):
        end_idx = raw.find("\n---\n", 4)
        if end_idx >= 0:
            return raw[end_idx + 5:]
    return raw


# ---------------------------------------------------------------------------
# Revision-id generator (§5.2)
# ---------------------------------------------------------------------------

_PREFIX = {
    "agent_self": "self",
    "program": "program",
    "skill_edit": "skill",
    "rule_edit": "rule",
    "script_edit": "script",
}


def make_revision_id(file_kind, agent):
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    prefix = _PREFIX.get(file_kind, "evo")
    hexbits = secrets.token_hex(2)  # 4 hex chars
    return f"{prefix}-{ts}-{agent}-{hexbits}"


# ---------------------------------------------------------------------------
# Diff + classification
# ---------------------------------------------------------------------------

def diff_stats(before_text, after_text):
    """Return (added_lines, removed_lines, first_change_line_in_before)."""
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines)
    added = 0
    removed = 0
    first_change = None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            removed += i2 - i1
            if first_change is None:
                first_change = i1
        elif tag == "insert":
            added += j2 - j1
            if first_change is None:
                first_change = i1
        elif tag == "replace":
            added += j2 - j1
            removed += i2 - i1
            if first_change is None:
                first_change = i1
    return added, removed, first_change


def section_at_line(before_text, line_index):
    """Walk backward in `before_text` from `line_index` to the nearest `## ` heading.
    Returns the heading text (without `## ` prefix) or `__frontmatter_or_preamble__`.

    Aligns with Option C audit's section-walk-back algorithm — same heuristic
    used to attribute mid-section edits to their canonical heading.
    """
    if line_index is None:
        return "__frontmatter_or_preamble__"
    lines = before_text.splitlines()
    if line_index < 0:
        return "__frontmatter_or_preamble__"
    for i in range(min(line_index, len(lines) - 1), -1, -1):
        s = lines[i]
        if s.startswith("## ") and not s.startswith("### "):
            return s[3:].strip()
    return "__frontmatter_or_preamble__"


def classify_change(before_text, after_text, before_hash, after_hash, file_kind):
    """Return change_class per §5.4 / §14.3 heuristic.

    - bootstrap: before_hash is None (file didn't exist Pre)
    - empty:    body unchanged (before_hash == after_hash) — body-only equality
    - cosmetic: small diff, no structural change
    - material-rename: heading rename (Jaccard ≥0.5) — deferred; falls to material
    - material: anything else
    """
    if before_hash is None:
        return "bootstrap"
    if after_hash is None:
        return "empty"
    if before_hash == after_hash:
        return "empty"

    added, removed, _ = diff_stats(before_text, after_text)
    delta = added + removed

    if delta <= 3:
        # §5.4 cosmetic-floor check
        # No new H2 / H3 introduced
        b_h = set(re.findall(r"^##+\s+.+$", before_text, re.MULTILINE))
        a_h = set(re.findall(r"^##+\s+.+$", after_text, re.MULTILINE))
        if b_h == a_h:
            return "cosmetic"

    return "material"


# ---------------------------------------------------------------------------
# Stub write to event stream
# ---------------------------------------------------------------------------

_STREAM_FILENAME = {
    "agent_self": "self-evolution.jsonl",
    "program": "program-evolution.jsonl",
    "skill_edit": "skill-evolution.jsonl",
    "rule_edit": "rule-evolution.jsonl",
    "script_edit": "script-evolution.jsonl",
}


def write_stub_entry(world_dir, file_kind, entry):
    """Append one JSONL line to the appropriate event stream. Uses locked_append_jsonl."""
    from _fileops import locked_append_jsonl

    stream_name = _STREAM_FILENAME[file_kind]
    stream_path = Path(world_dir) / stream_name
    locked_append_jsonl(stream_path, entry)


# ---------------------------------------------------------------------------
# Agent / world resolution (shared with evolution-prepare.py shape)
# ---------------------------------------------------------------------------

def resolve_agent(project_root, session_id, env_agent):
    if env_agent:
        return env_agent
    if not session_id:
        return None
    if any(c in session_id for c in ("/", "\\", "\n", "\r", " ")) or ".." in session_id:
        return None
    # Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml.
    # Without this, the entire self-program-evolution recording pipeline is
    # silently dead for Write/Edit/MultiEdit PostToolUse hooks (those hooks
    # don't get MIND_AGENT env-injected, so the session_id path is primary).
    agents_parent = Path(project_root) / "agents"
    if agents_parent.is_dir():
        try:
            for child in agents_parent.iterdir():
                if not child.is_dir():
                    continue
                binding_p26 = child / "sessions" / session_id / "binding.yaml"
                if binding_p26.is_file():
                    name = child.name
                    if re.match(r"^[a-z][a-z0-9-]*$", name):
                        return name
        except OSError:
            pass
    # Legacy fallback: pre-Phase-2.6 .active-agent-<SID>.
    binding = Path(project_root) / f".active-agent-{session_id}"
    if not binding.exists():
        return None
    try:
        name = binding.read_text(encoding="utf-8").strip()
        if name and re.match(r"^[a-z][a-z0-9-]*$", name):
            return name
    except Exception:
        pass
    return None


def resolve_world_dir(project_root, agent):
    # Phase 2.5.D: agent dirs live under agents/ parent. Was previously
    # Path(project_root) / agent / "local-paths.conf" — resolved to a
    # non-existent path → world_dir always None → no event-stream writes.
    conf = Path(project_root) / "agents" / agent / "local-paths.conf"
    if not conf.exists():
        return None
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip().replace("\r", "")
            if line.startswith("WORLD_PATH="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def path_key(abs_path):
    return hashlib.sha256(str(abs_path).encode("utf-8")).hexdigest()[:6]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    data = stdin_json_or_approve()

    tool_name = data.get("tool_name", "") or ""
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        approve_no_mutation()

    tool_input = data.get("tool_input") or {}
    file_path = extract_file_path(tool_input)
    if not file_path:
        approve_no_mutation()
    if not is_absolute_path(file_path):
        approve_no_mutation()

    project_root = os.environ.get("PROJECT_ROOT", "") or ""
    if not project_root:
        approve_no_mutation()

    sid = data.get("session_id", "") or ""
    env_agent = os.environ.get("MIND_AGENT", "") or ""
    agent = resolve_agent(project_root, sid, env_agent)
    if not agent:
        approve_no_mutation()

    abs_path = str(Path(file_path).resolve())
    # Phase 2.5.D: agent dirs under agents/ parent.
    sidecar_dir = Path(project_root) / "agents" / agent / "session"
    sidecar_path = sidecar_dir / f"pending-evolution-{path_key(abs_path)}.json"

    if not sidecar_path.exists():
        # Pre didn't fire (untracked path, or Pre silent-failed). No-op.
        approve_no_mutation()

    # Read sidecar
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt sidecar — delete + bail
        try:
            sidecar_path.unlink()
        except Exception:
            pass
        approve_no_mutation()

    file_kind = sidecar.get("file_kind")
    if file_kind not in _PREFIX:
        try:
            sidecar_path.unlink()
        except Exception:
            pass
        approve_no_mutation()

    before_hash = sidecar.get("before_hash")
    after_hash = compute_body_hash(abs_path)

    # Detect whether a pre-edit snapshot is available. For script_edit,
    # `resolve_base_dir` returns None for core/* paths so save_history is
    # a no-op and history_snapshot is None. Without a snapshot, before_text
    # is empty and diff_stats would produce misleading "whole file added"
    # counts. Emit null/empty diff metadata in that case — change_class
    # and hashes still convey the real signal.
    snapshot_path = sidecar.get("history_snapshot")
    has_snapshot = bool(snapshot_path) and Path(snapshot_path).exists()
    is_bootstrap = before_hash is None  # file didn't exist before — empty before_text is correct

    before_text = get_body_text(snapshot_path) if has_snapshot else ""
    after_text = get_body_text(abs_path) if Path(abs_path).exists() else ""

    if has_snapshot or is_bootstrap:
        added, removed, first_change_line = diff_stats(before_text, after_text)
        section = section_at_line(before_text, first_change_line)
    else:
        added, removed, first_change_line = None, None, None
        section = "__no_snapshot__"

    change_class = classify_change(before_text, after_text, before_hash, after_hash, file_kind)

    # If body unchanged, skip event-stream write (no real edit happened)
    if change_class == "empty":
        try:
            sidecar_path.unlink()
        except Exception:
            pass
        approve_no_mutation()

    # Build stub entry per §5.2 schema
    revision_id = make_revision_id(file_kind, agent)
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Compose a short diff_excerpt — only when a real snapshot existed.
    # Without one, the "unified diff" would just be the entire after_text
    # marked as inserted, which is misleading not informative.
    diff_excerpt = ""
    if has_snapshot or is_bootstrap:
        try:
            ud_lines = list(difflib.unified_diff(
                before_text.splitlines(), after_text.splitlines(),
                fromfile="before", tofile="after", n=2, lineterm=""
            ))
            diff_excerpt = "\n".join(ud_lines[:30])[:500]
        except Exception:
            pass

    entry = {
        "revision_id": revision_id,
        "previous_revision_id": sidecar.get("previous_revision_id"),
        "ts": ts,
        "file_kind": file_kind,
        "file_path": sidecar.get("file_path_rel") or abs_path,
        "agent": agent,
        "by_session_id": sid,
        "change_class": change_class,
        "section_changed": section,
        "diff_excerpt": diff_excerpt,
        "diff_lines": {"added": added, "removed": removed},
        "before_hash": before_hash,
        "after_hash": after_hash,
        "history_snapshot": sidecar.get("history_snapshot"),
        # Phase-b fields — populated by evolution-complete.sh
        "signal_source": None,
        "signal_evidence": [],
        "reasoning": None,
        "user_notified": False,
        "user_notify_ref": None,
        "board_post_id": None,
        "verification_monitor_id": None,
        "status": "awaiting_completion",
    }

    # Kind-specific fields
    if file_kind == "skill_edit":
        entry["skill_name"] = sidecar.get("skill_name")
    elif file_kind == "rule_edit":
        entry["rule_name"] = sidecar.get("rule_name")
    elif file_kind == "script_edit":
        entry["script_path"] = sidecar.get("script_path")

    world_dir = resolve_world_dir(project_root, agent)
    if not world_dir:
        try:
            sidecar_path.unlink()
        except Exception:
            pass
        approve_no_mutation()

    try:
        write_stub_entry(world_dir, file_kind, entry)
    except Exception:
        # Event-stream write failure: leave sidecar (it's the trace)
        approve_no_mutation()

    # Clean up sidecar
    try:
        sidecar_path.unlink()
    except Exception:
        pass

    approve_no_mutation()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
