"""test_agent_dir_allowlist.py — regression guard for the agent-dir write-surface
allowlist hard gate (file-model normalization Phase 4).

The L1 path-resolution hook (`core/scripts/path-resolution-hook.py`) gates writes
under the bound agent's directory to an ALLOWLIST of permitted first-segment
entries. This replaced the prior new-top-level-only check. The motivation
(user directive, 2026-06): agents were scattering working docs into ad-hoc
`reports/` (and would invent other slush dirs); the knowledge tree is the
durable home for long-term content, and `temp/` is the single staging SSOT for
working docs that drain to the tree. The gate is an allowlist (deny everything
not explicitly permitted), NOT a `reports/` blacklist — so any future invented
slush dir is denied by construction.

Contract asserted here (the durable hard gate):
  - `reports/...`           → DENY   (the entry deliberately excluded)
  - any unregistered dir    → DENY   (e.g. handoffs/, new slush dirs)
  - any unregistered file   → DENY   (e.g. scratch.md)
  - `temp/...`              → APPROVE (the new working-doc SSOT)
  - allowlisted dirs        → APPROVE (journal/, experience/, session/, ...)
  - allowlisted top files   → APPROVE (self.md, COMPLETION-REPORT.md, ...)

The allowlist SSOT lives in `path-resolution-hook.py`
(`_AGENT_DIR_ALLOWLIST_DIRS` / `_AGENT_DIR_ALLOWLIST_FILES`) and is mirrored in
`core/config/conventions/temp-store.md`. When that list changes, update both —
and this test's expectations.

Pattern mirrors test_path_resolution_virtual_prefix_cruft.py: subprocess
invocation of the real hook with a synthetic Write payload, parse stdout for
hookSpecificOutput.permissionDecision. Uses the first agent on THIS box that
has a local-paths.conf (the hook resolves external roots from the bound
agent's conf and FAILS OPEN when it is absent — hardcoding "bravo" held on the
dev box but silently approved everything on satellite boxes where bravo's
per-machine gitignored conf does not exist; g-115-1940). Disk existence of the
target is irrelevant — the allowlist check is pure first-segment membership,
so no on-disk fixture is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
HOOK_PY = CORE_SCRIPTS / "path-resolution-hook.py"


def _pick_agent_with_conf():
    """First agent dir carrying a local-paths.conf on this box, or None.

    The hook exits fail-open (approve) at its conf gate when the bound agent
    has no local-paths.conf, so the deny contract is only testable against an
    agent that has one HERE. local-paths.conf is per-machine and gitignored —
    which agent qualifies varies by box (g-115-1940).
    """
    agents_root = PROJECT_ROOT / "agents"
    if agents_root.is_dir():
        for p in sorted(agents_root.iterdir()):
            if (p / "local-paths.conf").is_file():
                return p.name
    return None


AGENT = _pick_agent_with_conf()
pytestmark = pytest.mark.skipif(
    AGENT is None,
    reason="no agent with local-paths.conf on this box — the L1 hook fails "
           "open without one, so the allowlist deny contract is untestable here",
)


def invoke_hook(rel_path: str) -> dict:
    """Run the hook with a Write payload for agents/<AGENT>/<rel_path>.

    Returns {"decision": "approve"|"deny", "reason": str}. An empty stdout
    means the hook approved with no mutation (its silent-approve path).
    """
    pr = str(PROJECT_ROOT).replace("\\", "/")
    file_path = f"{pr}/agents/{AGENT}/{rel_path}"
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
            "session_id": "test-session",
        }
    )
    env = os.environ.copy()
    env["PROJECT_ROOT"] = pr
    env["MIND_AGENT"] = AGENT
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


# (description, relative-path-under-agent-dir, expected-decision)
CASES = [
    # --- DENY: reports/ is the deliberately-excluded entry (user motivation) ---
    ("reports/ denied (the abolished slush dir)", "reports/fresh-eyes-x.md", "deny"),
    ("reports/ nested denied", "reports/phase-costs/cost.json", "deny"),
    # --- DENY: any other unregistered dir or file (allowlist, not blacklist) ---
    ("unregistered dir handoffs/ denied", "handoffs/foo.md", "deny"),
    ("unregistered dir scratch-dir/ denied", "scratch-dir/x.md", "deny"),
    ("unregistered top-level file denied", "scratch.md", "deny"),
    ("unregistered top-level file 2 denied", "notes-random.txt", "deny"),
    # --- APPROVE: temp/ is the new working-doc SSOT ---
    ("temp/ allowed (new SSOT)", "temp/working-doc.md", "approve"),
    ("temp/drained allowed", "temp/drained/2026-05-old.md", "approve"),
    # --- APPROVE: allowlisted directories ---
    ("journal/ allowed", "journal/2026/05/2026-05-12.md", "approve"),
    ("experience/ allowed", "experience/exp-x.md", "approve"),
    ("session/ allowed", "session/working-memory.yaml", "approve"),
    # --- APPROVE: allowlisted top-level files ---
    ("self.md allowed", "self.md", "approve"),
    ("COMPLETION-REPORT.md allowed", "COMPLETION-REPORT.md", "approve"),
    ("BACKLOG.md allowed", "BACKLOG.md", "approve"),
    ("local-paths.conf allowed", "local-paths.conf", "approve"),
    ("aspirations.jsonl allowed", "aspirations.jsonl", "approve"),
]


@pytest.mark.parametrize(
    "desc,rel_path,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_agent_dir_allowlist(desc: str, rel_path: str, expected: str) -> None:
    result = invoke_hook(rel_path)
    assert result["decision"] == expected, (
        f"{desc}: expected {expected}, got {result['decision']}; "
        f"reason: {result['reason'][:200]}"
    )
    # A deny must carry a non-empty educational reason — an empty-reason deny
    # is a hook bug (the user never learns where to write instead).
    if expected == "deny":
        assert result["reason"].strip(), f"{desc}: deny with empty reason"


def test_reports_deny_redirects_to_temp() -> None:
    """The reports/ deny message must steer the writer to the temp/ SSOT and
    the knowledge tree — that redirect is the whole point of the gate."""
    result = invoke_hook("reports/some-analysis.md")
    assert result["decision"] == "deny"
    reason = result["reason"].lower()
    assert "temp" in reason, f"deny reason should mention temp/: {result['reason'][:300]}"
