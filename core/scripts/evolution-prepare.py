#!/usr/bin/env python3
"""PreToolUse[Write|Edit|MultiEdit] hook — Phase a of D1 (3-phase write path).

Captures before-state for any Edit/Write touching one of the 5 evolution-tracked
file kinds:
  - agent_self  : <agent>/self.md
  - program     : world/program.md
  - skill_edit  : .claude/skills/**/SKILL.md
  - rule_edit   : .claude/rules/*.md
  - script_edit : core/scripts/**/*.{sh,py}, mind_api/src/**/*.py,
                  core/config/**/*.{md,yaml,yml,json,jsonl}

What this hook does:
  1. Read stdin JSON (tool_input.file_path, session_id).
  2. Classify file_kind by path-glob; non-tracked paths → fast no-op approve.
  3. Resolve the editing agent's session dir (from MIND_AGENT or session_id binding).
  4. Compute before_hash (body-only per §5.4a) — None if file doesn't exist yet (bootstrap).
  5. Call save_history() to snapshot the pre-edit file (skipped if file is new).
  6. Read previous_revision_id from file's existing front matter (None if no FM / not present).
  7. Write sidecar JSON to <agent>/session/pending-evolution-<6hex>.json.

Sidecar schema (read by evolution-record.py at PostToolUse):
  {
    "file_kind":             "agent_self" | "program" | "skill_edit" | "rule_edit" | "script_edit",
    "file_path":             "<absolute path>",
    "file_path_rel":         "<repo-relative path for display>",
    "before_hash":           "sha256:<12hex>" | null,
    "history_snapshot":      "<abs path to .history/ snapshot>" | null,
    "previous_revision_id":  "<prior revision_id from front matter>" | null,
    "by_agent":              "<agent name>",
    "by_session_id":         "<SID>",
    "ts_pre":                "<ISO timestamp>",
    "skill_name":            "<dir name>" (skill_edit only),
    "rule_name":             "<basename without .md>" (rule_edit only),
    "script_path":           "<repo-relative path, e.g. core/scripts/foo.sh>" (script_edit only)
  }

SAFETY: fail-open. Any error → approve_no_mutation (exit 0, empty stdout).
No deny path — Pre hooks for evolution-prepare never block edits.

Per world/conventions/self-program-evolution.md
"""

import sys
import os
import json
import hashlib
import re
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
# Path classification
# ---------------------------------------------------------------------------

# Hard-stop paths under .claude/ that are NOT evolution-tracked.
# Both .claude/.history/ (snapshot store) and .claude/agents/ (auto-injected
# project teammates) live under .claude/ but must never produce evolution
# records — .history/ writes would self-reference, agents/ are non-versioned
# config like settings.json. The skill/rule globs below already require
# specific path shapes, but the .history/ guard is defense in depth in case
# a future code path writes to a snapshot location directly.
_CLAUDE_EXCLUDED_TOPLEVEL = {".history", "agents"}


# Path segments that disqualify a core/ file from script_edit tracking.
# These are auto-generated, vendored, or snapshot stores — including them
# would produce noise (pyc files churn on every Python run) or self-
# reference loops (.history/ is the snapshot destination).
_CORE_EXCLUDED_SEGMENTS = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".python-shim",
    ".history",
})


# Extensions tracked per core/ subtree. The split is intentional: scripts/
# and runtime/ are code; config/ is data. Other subtrees (logs/, evolution/)
# are runtime output, not source — skip entirely.
_CORE_TRACKED_EXTS = {
    "scripts": (".sh", ".py"),
    "runtime": (".py",),
    "config":  (".md", ".yaml", ".yml", ".json", ".jsonl"),
}


def _is_tracked_core_path(parts):
    """True when a PROJECT_ROOT-relative path under core/ should be tracked.

    parts: tuple of path components; caller ensures parts[0] == "core".
    """
    if len(parts) < 2:
        return False
    if any(seg in _CORE_EXCLUDED_SEGMENTS for seg in parts):
        return False
    exts = _CORE_TRACKED_EXTS.get(parts[1])
    if not exts:
        return False
    return parts[-1].endswith(exts)


def classify_path(abs_path, project_root, world_dir):
    """Return (file_kind, key) for a tracked path, or (None, None) for untracked.

    file_kind = "agent_self" | "program" | "skill_edit" | "rule_edit" | "script_edit"
    key       = identifying string (skill name, rule name, agent name, "program",
                or repo-relative path for script_edit)
    """
    p = Path(abs_path).resolve()
    pr = Path(project_root).resolve()

    # Program: world/program.md
    if world_dir:
        wd = Path(world_dir).resolve()
        if p == (wd / "program.md").resolve():
            return ("program", "program")

    # Agent self.md: PROJECT_ROOT/<agent>/self.md
    try:
        if p.is_relative_to(pr):
            rel = p.relative_to(pr)
            parts = rel.parts
            # Skill: .claude/skills/<name>/SKILL.md
            if (
                len(parts) >= 4
                and parts[0] == ".claude"
                and parts[1] == "skills"
                and parts[-1] == "SKILL.md"
            ):
                return ("skill_edit", parts[2])
            # Rule: .claude/rules/<name>.md
            if (
                len(parts) == 3
                and parts[0] == ".claude"
                and parts[1] == "rules"
                and parts[2].endswith(".md")
            ):
                return ("rule_edit", parts[2][:-3])  # strip ".md"
            # Reject other .claude/ paths even if they end in SKILL.md somewhere
            if len(parts) >= 2 and parts[0] == ".claude":
                if parts[1] in _CLAUDE_EXCLUDED_TOPLEVEL:
                    return (None, None)
                # Not a recognized skill/rule path
                return (None, None)
            # Script: core/scripts/**/*.{sh,py}, mind_api/src/**/*.py,
            #         core/config/**/*.{md,yaml,yml,json,jsonl}
            if len(parts) >= 2 and parts[0] == "core":
                if _is_tracked_core_path(parts):
                    return ("script_edit", "/".join(parts))
                return (None, None)
            # Agent self.md: agents/<agent>/self.md (Phase 2.5.D: under agents/ parent).
            # Was previously: <agent>/self.md (len(parts)==2) — silently dead since 2.5.D.
            if (
                len(parts) == 3
                and parts[0] == "agents"
                and parts[2] == "self.md"
            ):
                if (pr / "agents" / parts[1] / "local-paths.conf").exists():
                    return ("agent_self", parts[1])
    except (ValueError, OSError):
        pass

    return (None, None)


# ---------------------------------------------------------------------------
# Body hash (§5.4a)
# ---------------------------------------------------------------------------

def compute_body_hash(file_path):
    """Body-only sha256 per §5.4a. Returns None if file doesn't exist."""
    if not Path(file_path).exists():
        return None
    raw = Path(file_path).read_text(encoding="utf-8")
    if raw.startswith("---\n"):
        end_idx = raw.find("\n---\n", 4)
        if end_idx >= 0:
            body = raw[end_idx + 5:]
        else:
            body = raw  # malformed FM
    else:
        body = raw  # no FM (rule files)
    lines = [ln.rstrip() for ln in body.replace("\r\n", "\n").split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    normalized = "\n".join(lines) + "\n"
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Front matter peek (read previous_revision_id)
# ---------------------------------------------------------------------------

_FM_FIELD_RE = re.compile(r'^(\w[\w_]*)\s*:\s*"?([^"\n]*?)"?\s*$')


def read_revision_id_from_fm(file_path):
    """Return current revision_id from file's front matter, or None."""
    p = Path(file_path)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        if not raw.startswith("---\n"):
            return None
        end_idx = raw.find("\n---\n", 4)
        if end_idx < 0:
            return None
        fm_block = raw[4:end_idx]
        for line in fm_block.split("\n"):
            m = _FM_FIELD_RE.match(line)
            if m and m.group(1) == "revision_id":
                v = m.group(2).strip()
                return v if v and v != "null" else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Agent binding
# ---------------------------------------------------------------------------

def resolve_agent(project_root, session_id, env_agent):
    """Resolve the editing agent's name. Priority: MIND_AGENT env > .active-agent-<SID>.

    Returns agent name string, or None if unresolvable.
    """
    if env_agent:
        return env_agent
    if not session_id:
        return None
    # Defense: no path traversal via SID
    if any(c in session_id for c in ("/", "\\", "\n", "\r", " ")) or ".." in session_id:
        return None
    # Phase 2.6 binding (preferred): agents/<name>/sessions/<SID>/binding.yaml.
    # Without this, Write/Edit/MultiEdit PreToolUse hooks (which don't get
    # MIND_AGENT env-injected by bash-agent-inject) fail to resolve agent
    # → no sidecar → evolution pipeline silently dead for new sessions.
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


# ---------------------------------------------------------------------------
# Sidecar write
# ---------------------------------------------------------------------------

def path_key(abs_path):
    """6-hex SHA256 of the absolute path (collision-free for ~4 distinct files)."""
    return hashlib.sha256(str(abs_path).encode("utf-8")).hexdigest()[:6]


def write_sidecar(sidecar_path, payload):
    """Atomic write: tmp + os.replace."""
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp-prepare")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(sidecar_path))


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

    # Resolve world_dir (best effort — read agent's local-paths.conf)
    sid = data.get("session_id", "") or ""
    env_agent = os.environ.get("MIND_AGENT", "") or ""
    agent = resolve_agent(project_root, sid, env_agent)
    if not agent:
        approve_no_mutation()  # No agent binding → no sidecar location

    world_dir = None
    # Phase 2.5.D: agent dirs under agents/ parent. Was previously
    # Path(project_root) / agent / "local-paths.conf" — missed the migration.
    conf = Path(project_root) / "agents" / agent / "local-paths.conf"
    if conf.exists():
        try:
            for line in conf.read_text(encoding="utf-8").splitlines():
                line = line.strip().replace("\r", "")
                if line.startswith("WORLD_PATH="):
                    world_dir = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass

    file_kind, key = classify_path(file_path, project_root, world_dir)
    if not file_kind:
        approve_no_mutation()  # Untracked path

    # --- This is a tracked edit. Capture before-state. ---
    abs_path = str(Path(file_path).resolve())
    rel_path = abs_path
    try:
        rel_path = str(Path(abs_path).relative_to(Path(project_root).resolve())).replace("\\", "/")
    except Exception:
        # Path may be outside PROJECT_ROOT (program.md under WORLD_PATH).
        # Schema consistency with evolution-git-sweep.py program special-case:
        # use the canonical "world/program.md" form so audits across retroactive
        # + runtime entries see the same file_path shape (Phase 3 audit fix).
        if file_kind == "program":
            rel_path = "world/program.md"

    before_hash = compute_body_hash(abs_path)
    previous_rev = read_revision_id_from_fm(abs_path)

    # save_history — only if file exists.
    # IMPORTANT: don't predict the snapshot path from our own datetime.now() —
    # save_history computes its OWN timestamp internally with 1-second granularity,
    # and a second-boundary crossing between our timestamp and save_history's
    # produces a mismatched path and a silent `history_snapshot=None` even
    # though the snapshot did land. Instead, scan the snapshot directory
    # AFTER the call and pick the most-recently-modified file matching the
    # agent suffix.
    history_snapshot = None
    if Path(abs_path).exists():
        try:
            from _fileops import save_history, resolve_base_dir
            base = resolve_base_dir(abs_path)
            if base:
                save_history(abs_path, base, agent, summary=f"pre-edit snapshot (file_kind={file_kind})")
                # CAS-store migration (fix-ballooning-history, 2026-05-22):
                # save_history now writes the CAS-delta store, NOT the legacy
                # .history/<rel>/<ts>.gz tree. Recover the snapshot_id from the
                # store (list_snapshots is newest-first) instead of globbing the
                # now-unwritten legacy tree. The old glob silently null'd
                # history_snapshot on EVERY tracked edit post-cutover, degrading
                # section_changed to __no_snapshot__ (rollback stayed safe via
                # the CAS store + git; only the recorded id + section label broke).
                import _history_store
                snaps = _history_store.list_snapshots(abs_path, base)
                if snaps:
                    history_snapshot = snaps[0]["snapshot_id"]
        except Exception:
            # Snapshot failure is non-fatal — Post will record without snapshot reference
            history_snapshot = None

    # --- Build sidecar payload ---
    payload = {
        "file_kind": file_kind,
        "file_path": abs_path,
        "file_path_rel": rel_path,
        "before_hash": before_hash,
        "history_snapshot": history_snapshot,
        "previous_revision_id": previous_rev,
        "by_agent": agent,
        "by_session_id": sid,
        "ts_pre": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if file_kind == "skill_edit":
        payload["skill_name"] = key
    elif file_kind == "rule_edit":
        payload["rule_name"] = key
    elif file_kind == "agent_self":
        payload["agent_self"] = key
    elif file_kind == "script_edit":
        payload["script_path"] = key  # repo-relative, e.g. "core/scripts/foo.sh"
        # NOTE: history_snapshot is None for script_edit because
        # _fileops.resolve_base_dir covers WORLD/META/AGENT/.claude but NOT
        # core/. evolution-record.py detects the missing snapshot and emits
        # null diff_lines + empty diff_excerpt + section_changed="__no_snapshot__"
        # rather than the misleading "whole file added" you'd get from a
        # diff against empty before_text. The change_class + before_hash +
        # after_hash signal is unaffected.
        #
        # If a real consumer of script-diff metadata appears, the fix is to
        # extend resolve_base_dir to include core/ — not to add a parallel
        # snapshot path (single source of truth).
    # program: no extra key needed

    # Phase 2.5.D: agent dirs under agents/ parent. Was previously
    # Path(project_root) / agent / "session" — created orphan cruft dirs
    # at PROJECT_ROOT/<agent>/session/ because mkdir(parents=True) succeeds.
    sidecar_dir = Path(project_root) / "agents" / agent / "session"
    sidecar_name = f"pending-evolution-{path_key(abs_path)}.json"
    sidecar_path = sidecar_dir / sidecar_name

    try:
        write_sidecar(sidecar_path, payload)
    except Exception:
        # Sidecar write failure → no Post processing, but never block the edit
        pass

    approve_no_mutation()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Catch-all: never block on hook failure
        sys.exit(0)
