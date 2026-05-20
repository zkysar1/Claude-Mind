#!/usr/bin/env python3
"""PreToolUse[Edit|Write|MultiEdit] hook — settings.json structural validator.

This is the bootstrap-paradox defense per rb-931 (2026-05-14, P9 research).
The self-program-evolution backpressure system cannot guard the file that
turns SPE off: if an agent's edit removes the evolution-prepare /
evolution-record hook entries, the hooks that capture the next edit never
fire — so SPE's monitor cannot detect "hooks are not firing" because the
captures are missing entirely (silent absence, not measured regression).
Backpressure rolls back BEHAVIORAL regressions; this validator catches
STRUCTURAL removal BEFORE the write lands.

Triggered when file_path matches .claude/settings.json under any prefix
(PROJECT_ROOT-rooted or absolute Windows form). For all other writes the
hook is a fast no-op (approve_no_mutation).

Rejection conditions:
  (a) Proposed content does not parse as JSON
  (b) Top-level keys are not a subset of the known schema
  (c) Any protected hook is missing from the result
      (6 SPE evolution hooks + 3 self-protection hooks for this validator)
  (d) Any deny entry protecting Categories A/C/D/E/F is missing

Categories A/C/D/E/F per P9 audit:
  A — Git destructive ops (reset --hard, push --force, rebase, etc.)
  C — User-only mechanisms (start/stop/boot skills, AskUserQuestion)
  D — Direct data-store edits (tree.yaml, world/*.jsonl, meta/*.jsonl)
  E — Session state signals (agent-state, persona-active, stop-loop, ...)
  F — Secrets + SID bindings (.env.local, .active-agent-*)

Category B (`.claude/settings*` self-edit, 3 entries) is INTENTIONALLY
unprotected — those are the removal candidates that this validator
unlocks. Removing them after this hook is wired is what lets the agent
edit settings.json autonomously.

Fail-CLOSED on validator errors (unlike most PreToolUse hooks, which
fail-open). Justification: the file under protection is the substrate
that gates the entire safety system. A validator crash that fails open
would silently permit catastrophic edits. The cost of blocking a
legitimate edit (user retries after fixing) is far below the cost of
silently disabling SPE.

References:
  rb-931 — infrastructure self-protection paradox
  rb-930 — sidecar revision tracking for non-frontmatter formats
  g-115-732 — this implementation goal
"""

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from hook_helpers import (  # type: ignore  # noqa: E402
    approve_no_mutation,
    emit_deny,
    extract_file_path,
    stdin_json_or_approve,
)


CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"


# The 4 known top-level keys. Anything else is rejected as schema drift.
ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "permissions",
    "env",
    "skillListingBudgetFraction",
    "hooks",
})


# Protected hooks. Each tuple is (event, tool_matcher, command_substring).
# If any one is missing from the proposed hooks config, reject.
#
# Two sub-groups:
#   - 6 SPE evolution hooks (prepare/record × Write/Edit/MultiEdit) —
#     guard the post-write behavioral backpressure layer.
#   - 3 self-protection hooks (THIS validator on PreToolUse Write/Edit/
#     MultiEdit) — guard against self-uninstall. Without these, a
#     proposed edit that removes settings-structural-validator.sh from
#     settings.json would pass validation (the proposed content still
#     contains all 6 SPE hooks); the edit would land; the validator
#     would no longer fire on the NEXT edit — bootstrap paradox v2.
#
# DO NOT remove the validator self-entries below without a successor
# self-protection mechanism. See rb-931 and module docstring.
PROTECTED_HOOKS = [
    ("PreToolUse",  "Write",     "evolution-prepare.sh"),
    ("PreToolUse",  "Edit",      "evolution-prepare.sh"),
    ("PreToolUse",  "MultiEdit", "evolution-prepare.sh"),
    ("PostToolUse", "Write",     "evolution-record.sh"),
    ("PostToolUse", "Edit",      "evolution-record.sh"),
    ("PostToolUse", "MultiEdit", "evolution-record.sh"),
    ("PreToolUse",  "Write",     "settings-structural-validator.sh"),
    ("PreToolUse",  "Edit",      "settings-structural-validator.sh"),
    ("PreToolUse",  "MultiEdit", "settings-structural-validator.sh"),
]


# Protected deny-list concepts. Each entry: (category, name, regex).
# A proposed deny list must contain AT LEAST ONE pattern matching each
# regex. This is concept-level protection, not literal-string protection —
# the agent may refactor the glob form (e.g., `*/x` to `**/x`) as long
# as the protected pattern is still present.
PROTECTED_DENY = [
    # Category A — Git destructive
    ("A", "git-reset-hard",       r"Bash\(.*git\s+reset\s+--hard"),
    ("A", "git-push-force",       r"Bash\(.*git\s+push.*(--force|-f\b)"),
    ("A", "git-rebase",           r"Bash\(.*git\s+rebase"),
    ("A", "git-cherry-pick",      r"Bash\(.*git\s+cherry-pick"),
    ("A", "git-clean",            r"Bash\(.*git\s+clean"),
    # Category C — User-only mechanisms
    ("C", "ask-user-question",    r"AskUserQuestion\(\*"),
    ("C", "skill-start-edit",     r"(Edit|Write|MultiEdit)\(.*\.claude/skills/start/"),
    ("C", "skill-stop-edit",      r"(Edit|Write|MultiEdit)\(.*\.claude/skills/stop/"),
    ("C", "skill-boot-edit",      r"(Edit|Write|MultiEdit)\(.*\.claude/skills/boot/"),
    # Category D — Direct data-store edits (must use scripts to preserve history+changelog)
    ("D", "tree-yaml-direct",     r"(Edit|Write|MultiEdit)\(.*world/knowledge/tree/_tree\.yaml"),
    ("D", "world-jsonl-direct",   r"(Edit|Write|MultiEdit)\(.*world/\*\.jsonl"),
    ("D", "meta-jsonl-direct",    r"(Edit|Write|MultiEdit)\(.*meta/\*\.jsonl"),
    # Category E — Session state signals (owned by /start, /stop)
    ("E", "agent-state",          r"(Write|Edit|MultiEdit)\(.*session/agent-state"),
    ("E", "persona-active",       r"(Write|Edit|MultiEdit)\(.*session/persona-active"),
    ("E", "stop-loop",            r"(Write|Edit|MultiEdit)\(.*session/stop-loop"),
    ("E", "stop-block-count",     r"(Write|Edit|MultiEdit)\(.*session/stop-block-count"),
    # Category F — Secrets + SID bindings
    ("F", "env-local-read",       r"Read\(.*\.env\.local"),
    ("F", "active-agent-sid",     r"(Write|Edit|MultiEdit)\(.*\.active-agent-"),
]


def _norm_path(p: str) -> str:
    """Lowercase + forward-slash normalization for path comparison."""
    if not p:
        return ""
    return p.replace("\\", "/").lower()


def _is_settings_file(file_path: str) -> bool:
    """True ONLY for the project .claude/settings.json (exact == SETTINGS_PATH).

    DO NOT weaken to `endswith(".claude/settings.json")`: that also matches
    ~/.claude/settings.json (machine-global) and any */.claude/settings.json.
    Since _read_current_settings() reads ONLY the project file, a non-project
    match makes _compute_proposed_content() return None and the hook
    fail-CLOSES — denying every legitimate ~/.claude edit (g-115-792 bug).
    Identity is against SETTINGS_PATH (single source of truth); relatives
    anchor to PROJECT_ROOT (not cwd). Fail-open on unresolvable paths —
    Gate 5 + anchor-tripwire are the settings.json backstops.
    """
    if not file_path:
        return False
    try:
        cand = Path(file_path)
        if not cand.is_absolute():
            cand = PROJECT_ROOT / cand
        return _norm_path(str(cand.resolve())) == _norm_path(str(SETTINGS_PATH.resolve()))
    except Exception:
        return False


def _read_current_settings() -> str:
    """Read current settings.json contents. Returns '' on any error."""
    try:
        return SETTINGS_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


def _apply_edit(current: str, old_string: str, new_string: str,
                replace_all: bool) -> "str | None":
    """Apply a single Edit operation. Returns None if old_string missing
    or ambiguous (matches Edit tool semantics)."""
    if not isinstance(current, str) or not isinstance(old_string, str):
        return None
    if old_string == "":
        return None
    if replace_all:
        return current.replace(old_string, new_string or "")
    occurrences = current.count(old_string)
    if occurrences == 0:
        return None
    if occurrences > 1 and not replace_all:
        # Edit tool errors out — but we cannot tell whether the caller
        # would supply more context. Reject to be safe.
        return None
    return current.replace(old_string, new_string or "", 1)


def _compute_proposed_content(tool_name: str, tool_input: dict) -> "str | None":
    """Return the proposed settings.json content after applying the tool op,
    or None if we cannot compute it (caller treats as validation failure)."""
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    if tool_name == "Edit":
        current = _read_current_settings()
        if not current:
            return None
        return _apply_edit(
            current,
            tool_input.get("old_string", ""),
            tool_input.get("new_string", ""),
            bool(tool_input.get("replace_all", False)),
        )

    if tool_name == "MultiEdit":
        current = _read_current_settings()
        if not current:
            return None
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        for e in edits:
            if not isinstance(e, dict):
                return None
            current = _apply_edit(
                current,
                e.get("old_string", ""),
                e.get("new_string", ""),
                bool(e.get("replace_all", False)),
            )
            if current is None:
                return None
        return current

    return None


def _check_hooks(parsed: dict) -> "tuple[bool, str]":
    """Verify all protected hooks are present (PROTECTED_HOOKS, currently
    6 SPE evolution + 3 self-protection). Returns (ok, reason)."""
    hooks_cfg = parsed.get("hooks")
    if not isinstance(hooks_cfg, dict):
        return False, "missing or non-dict `hooks` section"
    missing = []
    for event, matcher, cmd_substr in PROTECTED_HOOKS:
        event_entries = hooks_cfg.get(event)
        if not isinstance(event_entries, list):
            missing.append(f"{event}/{matcher}/{cmd_substr}")
            continue
        found = False
        for entry in event_entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("matcher") != matcher:
                continue
            for inner in entry.get("hooks") or []:
                if not isinstance(inner, dict):
                    continue
                if cmd_substr in (inner.get("command") or ""):
                    found = True
                    break
            if found:
                break
        if not found:
            missing.append(f"{event}/{matcher}/{cmd_substr}")
    if missing:
        return False, "missing SPE evolution hook(s): " + ", ".join(missing)
    return True, ""


def _check_deny_list(parsed: dict) -> "tuple[bool, str]":
    """Verify each protected deny concept has at least one matching entry."""
    permissions = parsed.get("permissions")
    if not isinstance(permissions, dict):
        return False, "missing or non-dict `permissions` section"
    deny = permissions.get("deny")
    if not isinstance(deny, list):
        return False, "missing or non-list `permissions.deny` section"
    deny_strs = [d for d in deny if isinstance(d, str)]
    missing = []
    for cat, name, pattern in PROTECTED_DENY:
        regex = re.compile(pattern)
        if not any(regex.search(s) for s in deny_strs):
            missing.append(f"{cat}:{name}")
    if missing:
        return False, "missing protected deny entries: " + ", ".join(missing)
    return True, ""


def _check_top_level_keys(parsed: dict) -> "tuple[bool, str]":
    """Verify no unknown top-level keys present."""
    if not isinstance(parsed, dict):
        return False, "top level is not a JSON object"
    extra = set(parsed.keys()) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        return False, ("unknown top-level key(s): "
                       + ", ".join(sorted(extra))
                       + f". Allowed: {sorted(ALLOWED_TOP_LEVEL_KEYS)}")
    return True, ""


def _validate(proposed_text: str) -> "tuple[bool, str]":
    """Run all checks. Returns (ok, reason). Reason populated only on failure."""
    try:
        parsed = json.loads(proposed_text)
    except Exception as exc:
        return False, f"proposed settings.json does not parse as JSON: {exc}"
    for check in (_check_top_level_keys, _check_hooks, _check_deny_list):
        ok, reason = check(parsed)
        if not ok:
            return False, reason
    return True, ""


def main() -> int:
    # Outer try matches sibling hooks — but the EXCEPT body is the
    # interesting deviation: fail-CLOSED for this hook. See module docstring.
    try:
        payload = stdin_json_or_approve()
        tool_name = payload.get("tool_name", "")
        if tool_name not in ("Edit", "Write", "MultiEdit"):
            approve_no_mutation()
        tool_input = payload.get("tool_input") or {}
        file_path = extract_file_path(tool_input)
        if not file_path or not _is_settings_file(file_path):
            approve_no_mutation()

        proposed = _compute_proposed_content(tool_name, tool_input)
        if proposed is None:
            emit_deny(
                "settings-structural-validator: could not compute proposed "
                "post-edit content (Edit old_string missing/ambiguous, "
                "MultiEdit step failed, or unreadable settings.json). "
                "Validator fails CLOSED — investigate the edit shape and retry."
            )

        ok, reason = _validate(proposed)
        if not ok:
            emit_deny(
                "settings-structural-validator: rejected proposed edit. "
                f"{reason}. See rb-931 (bootstrap-paradox defense) and the "
                "validator module docstring for the protected surface."
            )
        approve_no_mutation()
    except SystemExit:
        # approve_no_mutation / emit_deny both raise SystemExit — let through.
        raise
    except Exception as exc:
        # FAIL-CLOSED: unexpected error during validation of a security-critical
        # file is a deny. See module docstring rationale.
        emit_deny(
            f"settings-structural-validator: validator error ({exc!r}). "
            "Fail-CLOSED on validator exceptions because the file under "
            "protection gates the safety system. Fix the validator or "
            "edit settings.json manually with user authorization."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
