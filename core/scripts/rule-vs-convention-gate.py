#!/usr/bin/env python3
"""PreToolUse[Write|Edit|MultiEdit] gate — Layer-B rule-vs-convention placement check.

Refuses writes/edits to `.claude/rules/*.md` that introduce domain-specific
content from `core/config/domain-term-blocklist.txt`. The educational deny
message points the author at `core/config/conventions/` (framework-internal
schemas/protocols) or `world/conventions/` (domain-specific behavior).

Override: any content carrying the marker `domain-leak-exempt:` is passed
through (mirrors the established marker convention in domain-leak-check.sh).
This lets a rule author opt-in to keeping a domain example for pedagogical
clarity (e.g., probe-with-canonical-code-path.md cites real wrappers).

SAFETY: fail open on ANY error (matches path-resolution-hook.py pattern).
Empty stdout + exit 0 = "approve with no mutation".

Layer: B (write-time)
Companion: Layer A = .claude/rules/domain-free-examples.md text injection
           Layer C = core/scripts/domain-leak-check.sh CI sweep
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


# Resolve PROJECT_ROOT from this script's location: core/scripts/ -> core/ -> root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BLOCKLIST_PATH = PROJECT_ROOT / "core" / "config" / "domain-term-blocklist.txt"
RULES_DIR = PROJECT_ROOT / ".claude" / "rules"


# Substring match: file_path lands under .claude/rules/ AND ends in .md
def _is_rule_file(file_path: str) -> bool:
    try:
        p = Path(file_path).resolve()
    except Exception:
        return False
    try:
        p.relative_to(RULES_DIR.resolve())
    except ValueError:
        return False
    return p.suffix == ".md"


def _load_blocklist() -> list[str]:
    try:
        terms = []
        for line in BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            terms.append(line)
        return terms
    except Exception:
        return []


def _has_exempt_marker(content: str) -> bool:
    return "domain-leak-exempt:" in content


def _scan_content(content: str, blocklist: list[str]) -> list[str]:
    hits = []
    for term in blocklist:
        # Case-sensitive whole-word match (mirrors grep -w in domain-leak-check.sh).
        # Use \b boundaries — works for words containing letters/digits/underscores.
        pattern = r"\b" + re.escape(term) + r"\b"
        try:
            if re.search(pattern, content):
                hits.append(term)
        except re.error:
            # Conservative fallback: substring check
            if term in content:
                hits.append(term)
    return hits


def _extract_proposed_content(tool_name: str, tool_input: dict) -> str:
    """Pull the proposed file content from tool_input.

    Write: tool_input["content"]
    Edit:  tool_input["new_string"]  (only the new text being inserted)
    MultiEdit: concatenated tool_input["edits"][*]["new_string"]
    """
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

        if not _is_rule_file(file_path):
            approve_no_mutation()

        proposed = _extract_proposed_content(tool_name, tool_input)
        if not proposed:
            approve_no_mutation()

        # If the new content (or, conservatively, the whole file content for
        # Write) carries the domain-leak-exempt marker, pass.
        if _has_exempt_marker(proposed):
            approve_no_mutation()

        # For Edit/MultiEdit: also check the existing file for the marker —
        # if the file is already marker-exempt, the marker survives the edit.
        try:
            existing = Path(file_path).read_text(encoding="utf-8", errors="ignore")
            if _has_exempt_marker(existing):
                approve_no_mutation()
        except Exception:
            pass

        blocklist = _load_blocklist()
        if not blocklist:
            approve_no_mutation()

        hits = _scan_content(proposed, blocklist)
        if not hits:
            approve_no_mutation()

        # Build educational deny message
        rel = file_path
        try:
            rel = str(Path(file_path).relative_to(PROJECT_ROOT))
        except ValueError:
            pass
        hit_list = ", ".join(sorted(set(hits))[:5])
        reason = (
            f"REFUSED: rule-vs-convention gate.\n\n"
            f"The write to `{rel}` introduces domain-specific term(s): {hit_list}\n\n"
            "Core rules (`.claude/rules/`) must remain domain-agnostic — they\n"
            "describe universal behavioral discipline that applies regardless\n"
            "of the agent's deployment.\n\n"
            "Where domain content goes:\n"
            "  - Framework-internal schemas / protocols → `core/config/conventions/<name>.md`\n"
            "  - Domain-specific behavior / wiring   → `world/conventions/<name>.md`\n"
            "  - Domain-scoped reasoning              → guardrail / RB entry with `applies_to: domain`\n\n"
            "If the domain reference is pedagogical (a deliberate example to\n"
            "teach the rule), add `<!-- domain-leak-exempt: <rationale> -->`\n"
            "or `# domain-leak-exempt: <rationale>` somewhere in the file —\n"
            "the gate honors that marker.\n\n"
            "Detail: `.claude/rules/domain-free-examples.md`."
        )
        emit_deny(reason)
    except Exception:
        # Bottom catch-all — never block on a gate bug
        try:
            sys.exit(0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
