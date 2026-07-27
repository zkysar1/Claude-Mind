"""test_bash_inject_always_on.py — regression for .

Pre-fix, bash-agent-inject.py skipped the MIND_AGENT export entirely when
`MIND_AGENT=` appeared ANYWHERE in the command — so a single per-statement
override (`MIND_AGENT=other cmd`, the documented one-off cross-agent probe
form, prescribed by skill pseudocode e.g. the verify-wake call) left every
OTHER statement of a compound unbound. Census on the cc-02 2026-07-18
session: 28/28 real injection failures were this shape.

Post-fix policy: the hook ALWAYS prepends `export MIND_AGENT=<bound>; `
when the SID binding resolves; caller overrides compose through shell
scoping (head re-export wins for the compound; per-statement assignment
shadows one statement). Pinned here by driving main() end-to-end with a
monkeypatched binding resolver and asserting on the emitted updatedInput.

Three contracts:
  1. A command CONTAINING a per-statement `MIND_AGENT=` still gets the
     hook's export clause prepended (the fix).
  2. A plain command gets the export clause (unchanged baseline).
  3. When the binding does NOT resolve and the caller wrote an explicit
     override, no export is fabricated from nothing (fail-open, no crash).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "bash_agent_inject", CORE_SCRIPTS / "bash-agent-inject.py")
bai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bai)


def _run_hook(monkeypatch, command: str, resolved_agent: str | None):
    """Drive main() with a synthetic hook payload; return parsed stdout JSON."""
    payload = {
        "session_id": "test-sid-always-on",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if resolved_agent is not None:
        fake = SimpleNamespace(agent=resolved_agent)
        monkeypatch.setattr(
            bai, "resolve_binding_with_diagnostics",
            lambda sid, root: (fake, None))
    else:
        monkeypatch.setattr(
            bai, "resolve_binding_with_diagnostics",
            lambda sid, root: (None, "binding-yaml-missing"))
        # No memo failsafe either — simulate a never-resolved SID.
        monkeypatch.setattr(bai, "_last_resolved_agent", lambda sid, root: "")
    # Memo writes + miss logs are irrelevant side effects here — silence them.
    monkeypatch.setattr(bai, "_mark_binding_resolved",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bai, "_log_binding_miss_once",
                        lambda *a, **k: None, raising=False)

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        bai.main()
    except SystemExit:
        pass
    text = out.getvalue().strip()
    return json.loads(text) if text else {}


def test_per_statement_override_no_longer_disables_injection(monkeypatch):
    """The  shape: override on stmt-1, sibling stmt-2 must be bound."""
    cmd = ("MIND_AGENT=alpha py -3 core/scripts/quiescence-gate.py verify-wake; "
           "bash core/scripts/execution-diary.sh phase-start phase-0-precheck")
    result = _run_hook(monkeypatch, cmd, resolved_agent="zeta")
    updated = (result.get("hookSpecificOutput") or {}).get("updatedInput") or {}
    new_cmd = updated.get("command", "")
    assert "export MIND_AGENT=zeta; " in new_cmd, (
        "hook must inject the bound agent export even when the caller wrote a "
        f"per-statement MIND_AGENT= override; got: {new_cmd[:200]!r}")
    assert new_cmd.index("export MIND_AGENT=zeta; ") < new_cmd.index(cmd[:20]), (
        "export clause must be PREPENDED so shell scoping lets the "
        "per-statement override shadow exactly one statement")


def test_plain_command_still_injected(monkeypatch):
    """Baseline unchanged: no override present → export prepended."""
    result = _run_hook(monkeypatch, "bash core/scripts/wm-read.sh loop_state",
                       resolved_agent="zeta")
    updated = (result.get("hookSpecificOutput") or {}).get("updatedInput") or {}
    assert "export MIND_AGENT=zeta; " in updated.get("command", "")


def test_unresolved_binding_with_override_stays_fail_open(monkeypatch):
    """No binding + explicit caller override → no fabricated export, no crash."""
    cmd = "MIND_AGENT=alpha bash core/scripts/wm-read.sh loop_state"
    result = _run_hook(monkeypatch, cmd, resolved_agent=None)
    # The hook may approve without mutation or mutate with PATH/SID only —
    # either way it must NOT invent an MIND_AGENT export it cannot resolve.
    updated = (result.get("hookSpecificOutput") or {}).get("updatedInput") or {}
    new_cmd = updated.get("command", "")
    assert "export MIND_AGENT=" not in new_cmd, (
        f"no binding resolved — hook must not fabricate an export; got: {new_cmd[:200]!r}")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
