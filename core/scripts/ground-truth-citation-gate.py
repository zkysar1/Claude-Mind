#!/usr/bin/env python3
"""PreToolUse[Write|Edit|MultiEdit] citation lint for ground-truth writes ().

Write-path half of the 2026-08-31 no-publish-from-memory directive. The
close-time half is g-357-42; the two share incident vocabulary and nothing else.

WHAT IT INSPECTS: only the ADDED text of the write -- `content` for Write,
`new_string` for Edit, every edit's `new_string` for MultiEdit. Never the file on
disk. A gate that scanned the whole file would flag inherited prose the caller
did not write and could not fix, which is the shape that gets a gate switched off.

SCOPE: `world/knowledge/tree/**`, plus any file whose front matter carries
`ground_truth: true` (the domain's own opt-in). Everything else exits silently.

ADVISORY BY DEFAULT, ESCALATABLE. `permissionDecision: "allow"` -- the write
proceeds and this can never wedge the loop. Set
GROUND_TRUTH_CITATION_GATE=refuse to turn the same finding into a deny once the
false-positive rate has been measured in the field. The escalation path is
deliberately an env flag rather than a code edit so the decision is reversible
per box (see the convention).

CHANNEL POLICY, stated because copying an exemplar's redirections silently is
guard-2410's exact failure: this script writes on TWO channels and needs both.
stdout carries the structured hookSpecificOutput -- the ONLY channel that reaches
the model (guard-1680). stderr carries the same text for the human terminal,
which the structured payload does NOT reach. The wrapper must suppress NEITHER.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from ground_truth_citation import analyze  # noqa: E402

FRONT_MATTER_FLAG = "ground_truth: true"


def _world_tree_root():
    try:
        from _paths import WORLD_DIR            # type: ignore
        return Path(WORLD_DIR) / "knowledge" / "tree"
    except Exception:
        return None


def _in_scope(file_path: str, added: str) -> bool:
    if not file_path:
        return False
    p = str(file_path).replace("\\", "/")
    if "/knowledge/tree/" in p:
        return True
    root = _world_tree_root()
    if root is not None:
        try:
            Path(file_path).resolve().relative_to(root.resolve())
            return True
        except Exception:
            pass
    # Domain opt-in: the marker may be in the text being written (a new node) or
    # already on disk (an edit to an existing one).
    if FRONT_MATTER_FLAG in (added or ""):
        return True
    try:
        head = Path(file_path).read_text(encoding="utf-8", errors="replace")[:2048]
        return FRONT_MATTER_FLAG in head
    except Exception:
        return False


def _added_text(tool_name: str, ti: dict) -> str:
    if tool_name == "Write":
        return ti.get("content") or ""
    if tool_name == "Edit":
        return ti.get("new_string") or ""
    if tool_name == "MultiEdit":
        return "\n\n".join((e or {}).get("new_string") or ""
                           for e in (ti.get("edits") or []))
    return ""


def _retrieved_predicate(session_id):
    """(kind, value) -> was it actually retrieved this session?

    Loads the g-357-43 provenance manifest ONCE and matches locally, rather than
    shelling out to provenance-check.sh per token. Returns None -- NOT a
    permissive lambda -- when the manifest cannot be read, so analyze() SKIPS the
    decorative check instead of silently passing every citation (guard-1760: a
    check that could not run must not report a pass).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_ctx_reads_for_gate", SCRIPTS / "context-reads.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                       # type: ignore
        # CANCEL THE SELF-DESTRUCT WATCHDOG (guard-2138) — sibling of the same
        # fix in q4_provenance_sample.py. context-reads.py arms
        # `threading.Timer(10, lambda: os._exit(0))` at module scope; left
        # armed it kills any process living >10s with NO traceback and exit
        # status 0. Harmless while this gate only ever ran as a short-lived
        # CLI; fatal the moment it is imported into a long-running one
        # (). Cancel DEFENSIVELY rather than asserting: an assert here
        # would land inside the `except Exception: return None` below, which
        # swallows it into a silent SKIP (the guard-1760 alarm direction), and
        # tests legitimately point loaders like this at a stub with no timer.
        # The rename guarantee lives where it can fire —
        # _context_reads_helper.load_context_reads(), against the real module.
        _t = getattr(mod, "_timer", None)
        if _t is not None:
            _t.cancel()
        entries = mod.read_provenance(session_id=session_id) or []
    except Exception:
        return None
    values = []
    for e in entries:
        if isinstance(e, dict):
            v = e.get("value") or e.get("url") or e.get("path") or ""
        else:
            v = str(e)
        if v:
            values.append(str(v))
    if not values:
        # An EMPTY manifest is not the same as an unreadable one, but treating it
        # as "nothing was retrieved" would flag every citation in a session whose
        # hooks never fired. Under-flag by contract: skip the decorative check.
        return None

    def _retrieved(kind, value):
        v = str(value).rstrip("/.,);")
        return any(v in got or got in v for got in values)
    return _retrieved


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_name = payload.get("tool_name") or ""
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0
    ti = payload.get("tool_input") or {}
    file_path = ti.get("file_path") or ""
    added = _added_text(tool_name, ti)
    if not added.strip():
        return 0
    if not _in_scope(file_path, added):
        return 0

    findings = analyze(added, retrieved=_retrieved_predicate(payload.get("session_id")))
    if not findings:
        return 0

    lines = [
        "[ground-truth-citation-gate] ADVISORY (g-357-45): "
        f"{len(findings)} entity-fact cluster(s) in this write lack usable provenance.",
        f"  file: {file_path}",
    ]
    for f in findings[:6]:
        lines.append(f"  L{f.start_line}-{f.end_line} {f.kind}: {f.detail}")
        lines.append(f"      > {f.sample}")
    if len(findings) > 6:
        lines.append(f"  ... and {len(findings) - 6} more cluster(s).")
    lines.append(
        "  FIX: add a source token the session actually retrieved (URL, tree-node "
        "key, board msg-id, or goal-id), or tag the claim "
        "'[UNVERIFIED -- model prior]'. A bare publication name is NOT a source "
        "(coach g-012-02). A citation this session never fetched is DECORATIVE "
        "and is flagged at the same severity as none.")
    msg = "\n".join(lines)

    # Human terminal. NOT redundant with the payload below: a non-blocking hook's
    # stderr does not reach the model, and the structured payload does not reach
    # the terminal (guard-1680). Two readers, two channels.
    print(msg, file=sys.stderr)

    decision = "deny" if os.environ.get(
        "GROUND_TRUTH_CITATION_GATE", "").strip().lower() == "refuse" else "allow"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": msg,
            "additionalContext": msg,
        },
        "systemMessage": msg,
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)      # fail-open by contract
