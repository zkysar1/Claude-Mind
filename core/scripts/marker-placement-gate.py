#!/usr/bin/env python3
"""Marker-placement gate (Phase 5.7 of publication-cleanup-plan.md).

Refuses Edit/Write/MultiEdit on `.claude/skills/*/SKILL.md` and
`core/config/conventions/*.md` files that INTRODUCE a new
`domain-leak-exempt:` marker. Per the marker-restriction doctrine
documented in `.claude/rules/domain-free-examples.md` § "Marker
Restriction (per Phase 5)", these locations should genericize examples
rather than carry the marker.

Allowed: rule files (`.claude/rules/*.md`), script files (`.py`, `.sh`)
where domain strings are functional (regex patterns, fixtures, sentinels),
and SKILL.md / convention files that legitimately need the marker because
they DOCUMENT the marker itself (seed/SKILL.md scanner sentinels,
verify-learning/SKILL.md verify-check assertions, learning-routing.md
Layer C reference).

Invoked by `.claude/settings.json` PreToolUse[Edit|Write|MultiEdit].
Reads tool input from stdin (Claude Code hook protocol).

Hook contract: exit 0 with empty stdout = approve. Structured JSON on
stdout (via hook_helpers.emit_deny) + exit 0 = deny. Any exit code != 0
is treated by Claude Code as a hook ERROR (fail-open) — NOT as a deny.
This is why we route deny through emit_deny, mirroring sibling gates
rule-vs-convention-gate.py and path-resolution-hook.py.

Override: include a string of the form `MARKER_PLACEMENT_OVERRIDE="<reason>"`
anywhere in the proposed edit content. The override is recorded to stderr
(visible in Claude Code's tool output panel) and the edit is approved.
Future tightening MAY log overrides to a ledger like
`world/blocker-gate-overrides.jsonl`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from hook_helpers import (
        approve_no_mutation,
        emit_deny,
        extract_file_path,
        stdin_json_or_approve,
    )
except Exception:
    # Helpers unavailable — fail open.
    sys.exit(0)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# In-scope path patterns (relative to PROJECT_ROOT) — files where the
# marker should NOT appear without strong cause. The pattern matches the
# repo-relative path.
IN_SCOPE_PATTERNS = [
    r"^\.claude/skills/[^/]+/SKILL\.md$",
    r"^core/config/conventions/[^/]+\.md$",
]

# Files allowlisted by name — known-legitimate marker carriers in scope.
# Add entries here ONLY with strong cause:
#   - The file's primary purpose is to DOCUMENT the marker itself (so the
#     literal token must appear in prose / assertions).
#   - The file's content includes scanner sentinel patterns that would
#     misfire if transformed (seed/SKILL.md).
#
# This set MUST stay in sync with the ALLOWLIST array in
# core/scripts/domain-leak-check.sh. Both gates honor the same list.
ALLOWLIST = {
    ".claude/skills/seed/SKILL.md",  # scanner sentinel patterns, see fresh-eyes review 2026-05-19
    ".claude/skills/verify-learning/SKILL.md",  # documents the marker in verify-check assertions
    "core/config/conventions/learning-routing.md",  # documents the marker as Layer C in decision tree
}

MARKER_TOKEN = "domain-leak-exempt:"
OVERRIDE_TOKEN = "MARKER_PLACEMENT_OVERRIDE="


def in_scope(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/")
    if rel in ALLOWLIST:
        return False
    return any(re.match(p, rel) for p in IN_SCOPE_PATTERNS)


def has_override(content: str) -> "str | None":
    """Return override reason if present, else None."""
    m = re.search(re.escape(OVERRIDE_TOKEN) + r'"([^"]+)"', content)
    if m:
        return m.group(1)
    return None


def repo_relative(abs_path: str) -> "str | None":
    try:
        return str(Path(abs_path).resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except (ValueError, OSError):
        return None


def _extract_proposed_content(tool_name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", []) or []
        return "\n".join(
            (e.get("new_string", "") or "") for e in edits if isinstance(e, dict)
        )
    return ""


def main():
    try:
        data = stdin_json_or_approve()
        if not isinstance(data, dict):
            approve_no_mutation()

        tool_name = data.get("tool_name", "")
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            approve_no_mutation()

        tool_input = data.get("tool_input", {})
        file_path = extract_file_path(tool_input)
        if not file_path:
            approve_no_mutation()

        rel = repo_relative(file_path)
        if rel is None or not in_scope(rel):
            approve_no_mutation()

        proposed = _extract_proposed_content(tool_name, tool_input)
        if not proposed:
            approve_no_mutation()

        if MARKER_TOKEN not in proposed:
            approve_no_mutation()

        # Override path — approve, but surface the justification on stderr
        # so it's visible in tool output. Future tightening may log overrides.
        reason = has_override(proposed)
        if reason:
            sys.stderr.write(
                f"[marker-placement-gate] OVERRIDE accepted for {rel}: {reason}\n"
            )
            approve_no_mutation()

        deny_reason = (
            f"REFUSED: marker-placement gate.\n\n"
            f"The write to `{rel}` would introduce a `domain-leak-exempt:` marker.\n\n"
            "Per `.claude/rules/domain-free-examples.md` § 'Marker Restriction':\n"
            "  - `.claude/skills/*/SKILL.md` and `core/config/conventions/*.md`\n"
            "    should genericize examples (use 'agent-a', 'service-x',\n"
            "    'the framework') rather than carry the marker.\n"
            "  - The marker is reserved for executable code (scripts/tests) where\n"
            "    domain strings are FUNCTIONAL (regex patterns, sentinel arrays,\n"
            "    test fixtures). It is NOT a license to keep domain examples in\n"
            "    rule or convention prose.\n\n"
            "If this file genuinely needs the marker (rare — e.g., verification-grep\n"
            "sentinels that must not be transformed, or a file whose purpose is to\n"
            "DOCUMENT the marker), add a string of the form:\n"
            "  MARKER_PLACEMENT_OVERRIDE=\"<one-line justification>\"\n"
            "to your edit. Permanent allowlist entries (no justification needed per\n"
            "edit) live in `core/scripts/marker-placement-gate.py` ALLOWLIST set —\n"
            "keep `core/scripts/domain-leak-check.sh` ALLOWLIST array in sync."
        )
        emit_deny(deny_reason)
    except Exception:
        # Bottom catch-all — never block on a gate bug
        try:
            sys.exit(0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
