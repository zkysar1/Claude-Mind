"""test_path_resolution_virtual_prefix_cruft.py — regression test for .

Asserts the L1 path-resolution hook refuses writes to literal
PROJECT_ROOT/world/... and PROJECT_ROOT/meta/... paths — the canonical
failure mode where the LLM derives a path by string-concatenation against
the project checkout instead of resolving via local-paths.conf
(g-115-636 incident).

The pre-fix hook approved these writes because PROJECT_ROOT was a
governed root with only the agent-dir cruft check active; literal
virtual-prefix paths under PROJECT_ROOT fell through to
approve_no_mutation().

Cases:
  1. Write to PROJECT_ROOT/world/conventions/foo.md  → deny (replay of
     g-115-636 shape) with reason naming the configured WORLD_PATH.
  2. Write to PROJECT_ROOT/meta/strategy.yaml        → deny with reason
     naming the configured META_PATH.
  3. Write to PROJECT_ROOT/core/scripts/foo.py       → approve (legit
     PROJECT_ROOT subtree).
  4. Write to PROJECT_ROOT/<agent>/journal/x.md      → approve (existing
     agent subtree).
  5. Write to PROJECT_ROOT/<agent>/handoffs/foo.md   → deny (agent-dir
     cruft check still fires after the new virtual-prefix check is added).

RELATIVE-SPELLING CASES (g-369-80, 2026-08-31) — cases 6-11.

The hook originally returned approve_no_mutation() for EVERY relative
file_path, on the premise that "the permission layer (L2) handles those".
Measured false: a Write to the relative `world/knowledge/archive/<dir>/
RECEIPT.md` was approved and landed at PROJECT_ROOT/world/... as cruft,
while the ABSOLUTE spelling of the identical target denied correctly.
Cases 1-5 above all use the absolute spelling, so the entire suite passed
against a live bypass — which is the reason these cases exist.

This matters more than an edge case: every skill's pseudocode and every
convention file writes `world/...` and `meta/...` in the relative form, so
the natural spelling was the one no check could see.

  6.  relative world/conventions/foo.md              → deny (the regression)
  7.  relative meta/strategy.yaml                    → deny
  8.  relative core/scripts/foo.py                   → approve (legit)
  9.  relative agents/<agent>/journal/x.md           → approve (legit)
  10. relative path escaping via ..  into world/     → deny (`..` must
      normalize BEFORE matching, per rb-9356)
  11. relative path with PROJECT_ROOT unset          → approve (fail-open;
      an unresolvable relative path keeps the pre-fix behaviour, per the
      hook's "never emit a partial deny" posture)

Cases 8-9 are the tighten-only controls: the fix must add denials for
virtual-prefix paths WITHOUT denying ordinary relative writes.

Pattern: subprocess invocation of path-resolution-hook.py with stdin JSON
payload, parse stdout for hookSpecificOutput.permissionDecision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
HOOK_PY = CORE_SCRIPTS / "path-resolution-hook.py"


def invoke_hook(file_path: str, agent: str = "bravo", with_project_root: bool = True) -> dict:
    """Run the hook with a synthetic Write payload, return parsed response.

    with_project_root=False drops PROJECT_ROOT from the child env, exercising
    the fail-open arm of the relative-path resolution (case 11).
    """
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
            "session_id": "test-session",
        }
    )
    env = os.environ.copy()
    if with_project_root:
        env["PROJECT_ROOT"] = str(PROJECT_ROOT).replace("\\", "/")
    else:
        env.pop("PROJECT_ROOT", None)
    env["MIND_AGENT"] = agent
    result = subprocess.run(
        [sys.executable, str(HOOK_PY)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
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


def main() -> int:
    pr = str(PROJECT_ROOT).replace("\\", "/")
    cases = [
        (
            "PROJECT_ROOT/world/ literal (g-115-636 replay)",
            f"{pr}/world/conventions/foo.md",
            "deny",
            "PROJECT_ROOT/world/",
        ),
        (
            "PROJECT_ROOT/meta/ literal",
            f"{pr}/meta/strategy.yaml",
            "deny",
            "PROJECT_ROOT/meta/",
        ),
        (
            "PROJECT_ROOT/core/scripts/ legit",
            f"{pr}/core/scripts/foo.py",
            "approve",
            "",
        ),
        (
            "PROJECT_ROOT/<agent>/journal/ legit",
            f"{pr}/agents/bravo/journal/2026/05/2026-05-12.md",
            "approve",
            "",
        ),
        (
            "PROJECT_ROOT/<agent>/handoffs/ cruft (regression)",
            f"{pr}/agents/bravo/handoffs/foo.md",
            "deny",
            "agent dir",
        ),
        # --- relative spellings () ---------------------------------
        # Cases 1-5 pass against a hook that approves every relative path, so
        # these are the ones that actually pin the fix.
        (
            "RELATIVE world/ literal (g-369-80 regression)",
            "world/conventions/foo.md",
            "deny",
            "PROJECT_ROOT/world/",
        ),
        (
            "RELATIVE meta/ literal",
            "meta/strategy.yaml",
            "deny",
            "PROJECT_ROOT/meta/",
        ),
        (
            "RELATIVE core/scripts/ legit (tighten-only control)",
            "core/scripts/foo.py",
            "approve",
            "",
        ),
        (
            "RELATIVE <agent>/journal/ legit (tighten-only control)",
            "agents/bravo/journal/2026/08/2026-08-31.md",
            "approve",
            "",
        ),
        (
            "RELATIVE .. escape into world/ (normalize before match, rb-9356)",
            "agents/bravo/../../world/conventions/foo.md",
            "deny",
            "PROJECT_ROOT/world/",
        ),
    ]

    failures = []
    for desc, fpath, expect_decision, expect_reason_substr in cases:
        result = invoke_hook(fpath)
        if result["decision"] != expect_decision:
            failures.append(
                f"{desc}: expected {expect_decision}, got {result['decision']}; "
                f"reason: {result['reason'][:200]}"
            )
            continue
        if expect_reason_substr and expect_reason_substr not in result["reason"]:
            failures.append(
                f"{desc}: denied but reason missing {expect_reason_substr!r}; "
                f"reason: {result['reason'][:200]}"
            )
            continue
        print(f"PASS: {desc} — {result['decision']}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1

    # Configured-path-surfaced check: the deny reason for the world/ replay
    # must include the configured WORLD_PATH (the whole point of the
    # educational message). Read it from local-paths.conf and assert.
    conf = PROJECT_ROOT / "agents" / "bravo" / "local-paths.conf"
    if conf.exists():
        world_path = None
        for line in conf.read_text(encoding="utf-8").splitlines():
            if line.startswith("WORLD_PATH="):
                world_path = line.partition("=")[2].strip().strip('"').strip("'")
                break
        if world_path:
            result = invoke_hook(f"{pr}/world/conventions/foo.md")
            if world_path not in result["reason"]:
                print(
                    f"FAIL: configured WORLD_PATH ({world_path}) missing from "
                    f"deny reason: {result['reason'][:300]}"
                )
                return 1
            print("PASS: configured WORLD_PATH surfaces in deny reason")

    # Case 11 — fail-open arm. With PROJECT_ROOT absent a relative path cannot
    # be resolved, so the hook must fall back to its pre-fix behaviour rather
    # than deny on an unresolved guess. Asserted separately because it needs a
    # different child env, and it is the property that keeps a misconfigured
    # box writable instead of wedged.
    result = invoke_hook("world/conventions/foo.md", with_project_root=False)
    if result["decision"] != "approve":
        print(
            "FAIL: relative path with PROJECT_ROOT unset must fail OPEN, got "
            f"{result['decision']}; reason: {result['reason'][:200]}"
        )
        return 1
    print("PASS: relative path fails open when PROJECT_ROOT is unset")

    print("\nAll cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
