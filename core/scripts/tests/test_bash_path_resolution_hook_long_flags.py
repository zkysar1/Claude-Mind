"""Regression test for the bash-path-resolution-hook long-flag gap.

Origin: fresh-eyes review 2026-05-21. The original FLAG_TOKENS pattern was
`(?:-[a-zA-Z]+\\s+)*` which matched only short flags (e.g. `-p`, `-pv`).
For long flags (`mkdir --parents <path>`) the engine would skip the flag
section entirely, then greedily consume `--parents` itself as the "path
token" via PATH_CHARS (which includes `-`). The non-path string then
failed the governed-root check and the hook approved the write — silently
bypassing L1 cruft protection for any long-flag form of mkdir/touch/cp/
mv/tee.

Fix: change FLAG_TOKENS to `(?:--?[a-zA-Z][a-zA-Z0-9=._-]*\\s+)*` which
matches both short and long forms, including `--option=value` style.

Cases below cover:
  - short flag (regression sanity check: still works)
  - long flag (the actual bug — `mkdir --parents`)
  - mixed long+short flags
  - long flag with `=value` (`cp --preserve=all`)
  - touch / mv variants (parallel patterns share the same FLAG_TOKENS)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#  — this file's two "must approve" negative cases require
# agents/alpha/journal/ and agents/alpha/experience/ to EXIST as populated
# top-level dirs. That is a property of the DEPLOYMENT's agent roster, not of
# the code under test.
#
# The comment at the "two EXISTING toplevels" case below records the previous
# attempt at this same problem: the fixture used to use session/, which is
# gitignored and "exists only on boxes where alpha has RUN", and the case
# false-failed on cc-05 () where it was mis-triaged as a hook
# multi-path parsing defect. The remedy was to switch to journal/ + experience/
# "because they are both GIT-TRACKED so they exist on every clone".
#
# That remedy holds across CLONES of one deployment and NOT across DEPLOYMENTS.
# Measured by omni on ZDS-Mind (hostname cc-06, 2026-08-19): there agents/alpha
# is a stub holding only session/, so those toplevels genuinely do not exist,
# the L1 path hook CORRECTLY denies, and this test CORRECTLY fails against
# healthy code. Marked rather than re-fixed because no choice of path fixes it:
# the assumption is that a SECOND agent is populated at all.
pytestmark = pytest.mark.fleet_layout

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
HOOK_PY = CORE_SCRIPTS / "bash-path-resolution-hook.py"


def invoke_hook(command: str, agent: str = "alpha") -> dict:
    """Run the bash hook with a synthetic Bash payload, return parsed response."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "test-session",
        }
    )
    env = os.environ.copy()
    env["PROJECT_ROOT"] = str(PROJECT_ROOT).replace("\\", "/")
    env["MIND_AGENT"] = agent
    result = subprocess.run(
        [sys.executable, str(HOOK_PY)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    # Hook contract: every error path exits 0 with empty stdout (fail-open
    # discipline, guard-141). A non-zero exit means the hook itself
    # crashed — Python import error, daemon-unreachable, etc. Surface that
    # as a separate failure mode so a fully-broken hook doesn't masquerade
    # as N+1 deny-regression failures when the actual fault is "the hook
    # never ran." Per echo finding msg-20260521-202309-echo-1465.
    if result.returncode != 0:
        raise RuntimeError(
            f"hook exited rc={result.returncode}; "
            f"stderr={(result.stderr or '<empty>')[:300]}"
        )
    if not result.stdout.strip():
        return {"decision": "approve", "reason": ""}
    try:
        resp = json.loads(result.stdout)
        hs = resp.get("hookSpecificOutput", {})
        return {
            "decision": hs.get("permissionDecision", "approve"),
            "reason": hs.get("permissionDecisionReason", ""),
        }
    except json.JSONDecodeError:
        return {"decision": "approve", "reason": ""}


# Build a path that targets a new top-level entry under the bound agent dir
# so all cases hit is_new_toplevel() and SHOULD deny.
A = "agents/alpha"
NEW_SUB = "h9-regression-new-toplevel-zzz"  # never exists on disk

PROJ_ABS = str(PROJECT_ROOT).replace("\\", "/")

CASES = [
    # (label, command, expected_decision)
    ("mkdir short -p (regression sanity)",
     f"mkdir -p {A}/{NEW_SUB}/sub",                       "deny"),
    ("mkdir LONG --parents (the bug)",
     f"mkdir --parents {A}/{NEW_SUB}/sub",                "deny"),
    ("mkdir mixed --verbose -p",
     f"mkdir --verbose -p {A}/{NEW_SUB}/sub",             "deny"),
    ("touch --no-create",
     f"touch --no-create {A}/{NEW_SUB}/file.md",          "deny"),
    ("cp --recursive",
     f"cp --recursive src.txt {A}/{NEW_SUB}/sub/dst.txt", "deny"),
    ("cp --preserve=all (long flag with =value)",
     f"cp --preserve=all src.txt {A}/{NEW_SUB}/sub/dst.txt", "deny"),
    ("mv --force",
     f"mv --force src.txt {A}/{NEW_SUB}/sub/dst.txt",     "deny"),
    # H10 — end-of-flags marker: `--` was consumed as path token
    # before the fix because PATH_CHARS includes `-`.
    ("mkdir -- <path> (end-of-flags marker — H10)",
     f"mkdir -- {A}/{NEW_SUB}/sub",                       "deny"),
    # H11 — Windows absolute path: `:` was missing from PATH_CHARS
    # before the fix, so `C:/repo/...` got captured as just `C`.
    ("mkdir -p <absolute Windows path> (H11)",
     f"mkdir -p {PROJ_ABS}/{A}/{NEW_SUB}-abs/sub",        "deny"),
    # H12 — multi-arg mkdir: only first positional arg was checked
    # before the fix. `mkdir EXISTING NEW` slipped through silently and
    # the bypass-created NEW dir then passed the Write hook's
    # is_new_toplevel check on follow-on file writes (parent already
    # existed). All three flavors below must DENY.
    ("mkdir EXISTING NEW (multi-arg H12)",
     f"mkdir -p {A}/journal/2026/05/zz-ok-1 {A}/{NEW_SUB}-multi1/sub", "deny"),
    ("mkdir NEW EXISTING (multi-arg H12)",
     f"mkdir -p {A}/{NEW_SUB}-multi2/sub {A}/journal/2026/05/zz-ok-2", "deny"),
    ("touch EXISTING NEW (multi-arg H12)",
     f"touch {A}/journal/2026/05/zz-ok-3.md {A}/{NEW_SUB}-multi3/x.md", "deny"),
    ("tee NEW1 NEW2 (multi-arg H12)",
     f"tee {A}/{NEW_SUB}-multi4/log1 {A}/{NEW_SUB}-multi5/log2",       "deny"),
    # Negative case: existing top-level path under the agent dir must NOT trigger.
    ("mkdir into EXISTING toplevel (must approve)",
     f"mkdir -p {A}/journal/2026/05/test-subdir-zzz",     "approve"),
    # Negative case: TWO existing toplevels must NOT trigger (multi-arg sanity).
    # journal/ + experience/ are both GIT-TRACKED so they exist on every clone.
    # The prior fixture used session/, which is gitignored (**/session/) and
    # exists only on boxes where alpha has RUN — on any other box the hook
    # CORRECTLY denied it as a new toplevel and this case false-failed
    # (cc-05, : mis-triaged as a hook multi-path parsing defect).
    ("mkdir two EXISTING toplevels (must approve)",
     f"mkdir -p {A}/journal/zz1 {A}/experience/zz2",      "approve"),
]


def test_bash_hook_regex_edge_cases():
    """Parametrized-style check (single test fn to keep collection trivial).

    Each case asserts the hook's permission decision matches expectation.
    A bug in any case raises AssertionError with the offending command +
    reason so the failure points at the specific regex gap.
    """
    failures = []
    for label, cmd, expect in CASES:
        result = invoke_hook(cmd)
        if result["decision"] != expect:
            failures.append(
                f"{label}: expected {expect}, got {result['decision']}\n"
                f"   command: {cmd}\n"
                f"   reason:  {result['reason'][:200]}"
            )
    if failures:
        msg = "bash hook regex regression — failures:\n" + "\n".join(
            "  " + f for f in failures
        )
        raise AssertionError(msg)


def main() -> int:
    """CLI entry point — matches the sibling test_path_resolution_*.py pattern
    so the file is runnable as `py -3 <path>` for fast manual runs even when
    pytest is not available."""
    try:
        test_bash_hook_regex_edge_cases()
    except AssertionError as e:
        print("FAIL —")
        print(e)
        return 1
    print(f"OK — bash hook handles {len(CASES)} flag forms correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
