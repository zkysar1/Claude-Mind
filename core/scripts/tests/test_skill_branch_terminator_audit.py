"""test_skill_branch_terminator_audit.py — regression test for .

Verifies that skill-branch-terminator-audit.py's STATEMENT_START regex and
classify_terminator function correctly handle SKILL.md step labels with
sub-numbered prefixes like `7.5. Bash:` and `1.2.3. Skill(...)`.

Source: g-240-88 investigation — encode-session/SKILL.md Lane 7 uses sub-
numbered step labels (7.1, 7.2, ..., 7.5). The pre-fix STATEMENT_START
regex matched `\\d+\\.\\s` (e.g., `7. `) but missed `7.5. ` because the
character after the first dot is a digit, not whitespace. The audit then
walked past line 402 (the actual `Bash:` terminator) and surfaced line
399's bullet as WARN — a false positive on a benign Phase Final summary.

The fix has two coupled parts:
  1. STATEMENT_START regex updated to `\\d+(?:\\.\\d+)*\\.\\s` so step
     labels with arbitrary nesting depth are recognized as statement
     starts (no longer skipped as continuation lines).
  2. classify_terminator strips an optional STEP_PREFIX before applying
     FAIL_TEXT_OUTPUT / SAFE_TOOL_CALL / SAFE_TYPED_RETURN — so
     `7.5. Bash: foo.sh` classifies as SAFE (not WARN), and
     `7.5. Output: msg` classifies as FAIL (not WARN).

Cases covered:
  STATEMENT_START.match — what counts as a statement start (used by
    _is_procedural_fence and _find_terminator_before to walk the file).
  classify_terminator    — final SAFE / WARN / FAIL verdict on a line.

Regression cases ensure existing FAIL/SAFE behavior on plain (non-step-
prefixed) lines did not change.

Run: py -3 core/scripts/tests/test_skill_branch_terminator_audit.py
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _load_audit_module():
    """Load skill-branch-terminator-audit.py by spec because the filename has hyphens."""
    spec = importlib.util.spec_from_file_location(
        "audit_mod",
        CORE_SCRIPTS / "skill-branch-terminator-audit.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load skill-branch-terminator-audit.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_mod"] = mod  # required for @dataclass to resolve cls.__module__
    spec.loader.exec_module(mod)
    return mod


def test_statement_start_simple_step_prefix():
    """`7. Bash: ...` matches as a statement start (pre-existing behavior)."""
    mod = _load_audit_module()
    assert mod.STATEMENT_START.match("7. Bash: foo.sh") is not None


def test_statement_start_sub_numbered_prefix():
    """`7.5. Bash: ...` matches as a statement start (the fix)."""
    mod = _load_audit_module()
    assert mod.STATEMENT_START.match("7.5. Bash: foo.sh") is not None


def test_statement_start_triple_numbered_prefix():
    """`1.2.3. Skill(...)` matches — arbitrary nesting depth supported."""
    mod = _load_audit_module()
    assert mod.STATEMENT_START.match("1.2.3. Skill(foo)") is not None


def test_statement_start_indented_sub_numbered():
    """Leading whitespace is allowed before the step prefix."""
    mod = _load_audit_module()
    assert mod.STATEMENT_START.match("   7.5. Bash:") is not None


def test_statement_start_rejects_dotted_decimal_without_trailing_dot():
    """`7.5 Bash:` (no trailing dot) is NOT a step prefix — must not match."""
    mod = _load_audit_module()
    assert mod.STATEMENT_START.match("7.5 Bash:") is None


def test_statement_start_plain_prose_unchanged():
    """Plain prose still does not match — regression on existing behavior."""
    mod = _load_audit_module()
    assert mod.STATEMENT_START.match("plain text without leading marker") is None


def test_classify_step_prefixed_bash_is_safe():
    """`7.5. Bash: ...` classifies as SAFE — the actual fix to the
    encode-session line 402 false-positive WARN."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("7.5. Bash: spark-questions-increment.sh sq-012")
    assert sev == "SAFE", f"expected SAFE for step-prefixed Bash:, got {sev}"


def test_classify_step_prefixed_skill_is_safe():
    """`7. Skill(foo)` classifies as SAFE — same step-prefix fix applies
    to Skill( as to Bash:."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("7. Skill(aspirations)")
    assert sev == "SAFE", f"expected SAFE for step-prefixed Skill(, got {sev}"


def test_classify_step_prefixed_typed_return_is_safe():
    """`7. RETURN(values)` classifies as SAFE — typed sub-skill return."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("7. RETURN(goal=None)")
    assert sev == "SAFE", f"expected SAFE for step-prefixed RETURN(, got {sev}"


def test_classify_step_prefixed_output_is_fail():
    """`7.5. Output: msg` classifies as FAIL — text-emission semantics
    survive the step prefix. Without this, a step-prefixed Output: would
    silently kill the loop and the audit would miss it."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("7.5. Output: msg")
    assert sev == "FAIL", f"expected FAIL for step-prefixed Output:, got {sev}"


def test_classify_plain_bash_unchanged():
    """`Bash: foo` (no step prefix) still classifies as SAFE — regression
    on existing behavior."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("Bash: foo.sh")
    assert sev == "SAFE", f"plain Bash should still classify as SAFE, got {sev}"


def test_classify_plain_output_unchanged():
    """`Output: msg` (no step prefix) still classifies as FAIL — regression
    on existing FAIL detection."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("Output: foo")
    assert sev == "FAIL", f"plain Output: should still classify as FAIL, got {sev}"


def test_classify_unknown_shape_unchanged():
    """Unrecognized terminator shape still WARNs — regression on existing
    fail-open behavior."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("plain prose with no marker")
    assert sev == "WARN", f"unknown shape should classify as WARN, got {sev}"


def test_classify_step_prefix_does_not_promote_unknown_to_safe():
    """A step prefix on an unknown shape must NOT silently promote to SAFE.
    `7. plain prose` is still WARN — the prefix is stripped, the inner
    content is what classifies."""
    mod = _load_audit_module()
    sev, _ = mod.classify_terminator("7. plain prose with no marker")
    assert sev == "WARN", f"step-prefixed unknown should stay WARN, got {sev}"


def main():
    tests = [
        ("statement_start_simple_step_prefix", test_statement_start_simple_step_prefix),
        ("statement_start_sub_numbered_prefix", test_statement_start_sub_numbered_prefix),
        ("statement_start_triple_numbered_prefix", test_statement_start_triple_numbered_prefix),
        ("statement_start_indented_sub_numbered", test_statement_start_indented_sub_numbered),
        ("statement_start_rejects_dotted_decimal_without_trailing_dot",
         test_statement_start_rejects_dotted_decimal_without_trailing_dot),
        ("statement_start_plain_prose_unchanged", test_statement_start_plain_prose_unchanged),
        ("classify_step_prefixed_bash_is_safe", test_classify_step_prefixed_bash_is_safe),
        ("classify_step_prefixed_skill_is_safe", test_classify_step_prefixed_skill_is_safe),
        ("classify_step_prefixed_typed_return_is_safe",
         test_classify_step_prefixed_typed_return_is_safe),
        ("classify_step_prefixed_output_is_fail", test_classify_step_prefixed_output_is_fail),
        ("classify_plain_bash_unchanged", test_classify_plain_bash_unchanged),
        ("classify_plain_output_unchanged", test_classify_plain_output_unchanged),
        ("classify_unknown_shape_unchanged", test_classify_unknown_shape_unchanged),
        ("classify_step_prefix_does_not_promote_unknown_to_safe",
         test_classify_step_prefix_does_not_promote_unknown_to_safe),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            traceback.print_exc()
            failed.append(name)

    if failed:
        print(f"\n{len(failed)}/{len(tests)} test(s) failed: {failed}")
        return 1
    print(f"\n{len(tests)}/{len(tests)} test(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
