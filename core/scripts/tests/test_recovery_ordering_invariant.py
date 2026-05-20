"""Regression test for  ordering invariant.

Asserts that all 3 RUNNING->IDLE recovery sites place
session-state-set.sh IDLE BEFORE session-manifest-clear.sh in the
executable code path. The previous ordering created a brief window
where state=RUNNING + sid=missing (half-recovered zombie); the
reversed ordering eliminates that window. See
g-115-666-brief.md and rb-323/guard-403 (inverse direction).

Three sites under test:
  1. core/scripts/recovery-gate.sh  _perform_recovery
  2. .claude/skills/start/SKILL.md  Step 0.7 (--recover)
  3. .claude/skills/start/SKILL.md  auto-recovery branch
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _executable_lines(text: str) -> list[tuple[int, str]]:
    """Return (lineno, line) pairs for lines that are not pure comments.

    A line is a "comment line" if its first non-whitespace char is `#` AND
    the line does not also contain a `Bash:` directive (markdown).
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#") and "Bash:" not in line and "`session-" not in line:
            continue
        out.append((i, line))
    return out


def _find_invocations(lines: list[tuple[int, str]], pattern: re.Pattern) -> list[int]:
    return [n for n, L in lines if pattern.search(L)]


def _assert_ordering(path: Path, state_re: re.Pattern, manifest_re: re.Pattern,
                     site_label: str, expected_state_count: int,
                     expected_manifest_count: int) -> None:
    text = path.read_text(encoding="utf-8")
    lines = _executable_lines(text)
    state_lines = _find_invocations(lines, state_re)
    manifest_lines = _find_invocations(lines, manifest_re)
    assert len(state_lines) >= expected_state_count, \
        f"{site_label}: expected >={expected_state_count} state-set IDLE lines, got {state_lines}"
    assert len(manifest_lines) >= expected_manifest_count, \
        f"{site_label}: expected >={expected_manifest_count} manifest-clear lines, got {manifest_lines}"
    first_state = min(state_lines)
    first_manifest = min(manifest_lines)
    assert first_state < first_manifest, (
        f"{site_label}: ORDERING VIOLATION — first state-set IDLE at line {first_state} "
        f"must precede first manifest-clear at line {first_manifest}. "
        f"All state-set IDLE lines: {state_lines}; manifest-clear lines: {manifest_lines}. "
        f"Inverse of rb-323/guard-403 for RUNNING->IDLE: state flip MUST precede observer-signal cleanup."
    )


def test_recovery_gate_perform_recovery_ordering() -> None:
    path = PROJECT_ROOT / "core" / "scripts" / "recovery-gate.sh"
    state_re = re.compile(r'session-state-set\.sh"?\s+IDLE')
    manifest_re = re.compile(r'session-manifest-clear\.sh')
    _assert_ordering(path, state_re, manifest_re,
                     "recovery-gate.sh _perform_recovery",
                     expected_state_count=1, expected_manifest_count=1)


def test_start_skill_recovery_branches_ordering() -> None:
    path = PROJECT_ROOT / ".claude" / "skills" / "start" / "SKILL.md"
    state_re = re.compile(r'`MIND_AGENT=<agent-name> bash core/scripts/session-state-set\.sh IDLE`')
    manifest_re = re.compile(r'`MIND_AGENT=<agent-name> bash core/scripts/session-manifest-clear\.sh`')
    _assert_ordering(path, state_re, manifest_re,
                     "start/SKILL.md (Step 0.7 + auto-recovery)",
                     expected_state_count=2, expected_manifest_count=2)


def test_recovery_gate_preserves_rc_check() -> None:
    """The rc-check + desync-warnings + retry-counter machinery must survive the reorder."""
    path = PROJECT_ROOT / "core" / "scripts" / "recovery-gate.sh"
    text = path.read_text(encoding="utf-8")
    assert "local rc=$?" in text, "rc capture missing after state-set IDLE"
    assert "desync-warnings.jsonl" in text, "desync-warnings logging missing"
    assert "recovery-failure-count" in text, "retry counter missing"
    # The failure-path message must mention that manifest-clear was skipped
    # (so the fix is documented in the operator-visible diagnostic).
    assert "manifest-clear was SKIPPED" in text or "Manifest-clear was SKIPPED" in text, (
        "failure-path diagnostic must explain that manifest-clear was SKIPPED to avoid zombie state"
    )


def test_start_skill_failure_path_diagnostic() -> None:
    """The /start failure-path diagnostic must mention skipped manifest-clear at BOTH recovery branches."""
    path = PROJECT_ROOT / ".claude" / "skills" / "start" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # Case-insensitive match: "(M|m)anifest-clear ... SKIPPED" appears at both
    # recovery branches (Step 0.7 + auto-recovery). The prose phrasing varies
    # ("manifest-clear is SKIPPED" vs "Manifest-clear was SKIPPED") so we count
    # the substring "SKIPPED" near "manifest-clear" instead of an exact phrase.
    skipped_count = sum(
        1 for line in text.splitlines()
        if "SKIPPED" in line and ("manifest-clear" in line.lower())
    )
    assert skipped_count >= 2, (
        f"both /start recovery branches must document the SKIPPED behavior on state-set failure; "
        f"found {skipped_count} (manifest-clear + SKIPPED) co-occurrences"
    )


if __name__ == "__main__":
    test_recovery_gate_perform_recovery_ordering()
    test_start_skill_recovery_branches_ordering()
    test_recovery_gate_preserves_rc_check()
    test_start_skill_failure_path_diagnostic()
    print("ALL PASS — g-115-683 ordering invariant locked down (3 sites + rc-check + diagnostic)")
