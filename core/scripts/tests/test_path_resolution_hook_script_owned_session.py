"""Script-owned session files are refused to Write/Edit by the L1 hook ().

Measured 2026-08-30 (coach, zc-03): after `session-state-set` correctly refused
RUNNING for a missing liveness carrier, the reducer used the **Write tool** to
hand-author `agents/coach/session/body-heartbeat-<sid>.json` — right name, wrong
shape (session_id/timestamp/phase keys, a 2025 timestamp). Every later reader
trusted it.

Bash was already fenced (bash-store-write-guard refuses cp/mv/redirect into
governed stores). The Write/Edit lane was not: `session` is on the agent-dir
allowlist and anything under a bound `sessions/<sid>/` is approved as sanctioned
scratch, so both surfaces admitted the hand-write.

BOTH DIRECTIONS ARE PINNED HERE ON PURPOSE (rb-8987): a fence that only proves
it REFUSES has not been shown to still ADMIT the work it must not block.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
HOOK_PY = CORE_SCRIPTS / "path-resolution-hook.py"
PR = str(PROJECT_ROOT).replace("\\", "/")
AGENT = "zeta"


def invoke_hook(file_path: str, tool_name: str = "Write", agent: str = AGENT) -> dict:
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "session_id": "test-session",
    })
    env = os.environ.copy()
    env["PROJECT_ROOT"] = PR
    env["MIND_AGENT"] = agent
    result = subprocess.run(
        [sys.executable, str(HOOK_PY)], input=payload,
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    if not result.stdout.strip():
        return {"decision": "approve", "reason": ""}
    try:
        hs = json.loads(result.stdout).get("hookSpecificOutput", {})
    except json.JSONDecodeError:
        return {"decision": "approve", "reason": ""}
    return {
        "decision": hs.get("permissionDecision", "approve"),
        "reason": hs.get("permissionDecisionReason", ""),
    }


def _live_sid():
    """An EXISTING sessions/<sid> dir — the hook requires one to reach the branch."""
    root = PROJECT_ROOT / "agents" / AGENT / "sessions"
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if child.is_dir():
            return child.name
    return None


# --------------------------- REFUSES ---------------------------------------

@pytest.mark.parametrize("basename", [
    "body-heartbeat-abc123.json",   # the carrier from the incident (prefix rule)
    "agent-state",
    "agent-mode",
    "running-session-id",
    "runner-token",
    "stop-requested",
    "stop-loop",
])
def test_script_owned_session_file_is_refused(basename):
    r = invoke_hook(f"{PR}/agents/{AGENT}/session/{basename}")
    assert r["decision"] == "deny", (
        f"{basename} is script-owned but Write was approved. reason={r['reason'][:200]}"
    )


def test_deny_names_the_owning_script():
    """A wall that does not name the fix sends the reader looking for a way around it."""
    r = invoke_hook(f"{PR}/agents/{AGENT}/session/agent-state")
    assert r["decision"] == "deny"
    assert "session-state-set.sh" in r["reason"], (
        "deny must name the sanctioned writer, not just refuse. "
        f"reason={r['reason'][:300]}"
    )


def test_edit_is_refused_too():
    """Edit mutates an existing bad file just as effectively as Write creates one."""
    r = invoke_hook(f"{PR}/agents/{AGENT}/session/agent-mode", tool_name="Edit")
    assert r["decision"] == "deny", f"Edit approved. reason={r['reason'][:200]}"


def test_binding_yaml_under_a_bound_sid_is_refused():
    sid = _live_sid()
    if sid is None:
        pytest.skip("no existing sessions/<sid> dir on this box")
    r = invoke_hook(f"{PR}/agents/{AGENT}/sessions/{sid}/binding.yaml")
    assert r["decision"] == "deny", (
        f"binding.yaml under a bound SID was approved. reason={r['reason'][:200]}"
    )


# --------------------------- STILL ADMITS ----------------------------------

def test_sanctioned_session_scratch_still_lands():
    """The L1-sanctioned scratch home must be untouched (path-resolution.md)."""
    sid = _live_sid()
    if sid is None:
        pytest.skip("no existing sessions/<sid> dir on this box")
    r = invoke_hook(f"{PR}/agents/{AGENT}/sessions/{sid}/scratch/notes.md")
    assert r["decision"] != "deny", (
        f"scratch write was refused — the fence over-reached. reason={r['reason'][:200]}"
    )


def test_nested_path_that_merely_ends_in_a_owned_basename_still_lands():
    """The rule is DIRECT children only; a deeper path is scratch, not the artifact."""
    sid = _live_sid()
    if sid is None:
        pytest.skip("no existing sessions/<sid> dir on this box")
    r = invoke_hook(f"{PR}/agents/{AGENT}/sessions/{sid}/scratch/agent-state")
    assert r["decision"] != "deny", (
        f"a nested scratch file was refused. reason={r['reason'][:200]}"
    )


def test_ordinary_agent_writes_still_land():
    r = invoke_hook(f"{PR}/agents/{AGENT}/journal/2026/08/2026-08-30.md")
    assert r["decision"] != "deny", f"journal write refused. reason={r['reason'][:200]}"


def test_unlisted_session_file_is_not_swept_up():
    """Scope discipline: only the enumerated set is fenced, not all of session/."""
    r = invoke_hook(f"{PR}/agents/{AGENT}/session/handoff.yaml")
    assert r["decision"] != "deny", (
        f"handoff.yaml is not on the script-owned list but was refused. "
        f"reason={r['reason'][:200]}"
    )


# --------------------------- SSOT SYNC (guard-3408) ------------------------

def test_every_fenced_basename_is_documented_in_the_convention():
    """The list is a producer/consumer sync point; the convention is its SSOT.

    A producer that renames one of these escapes the fence silently, so the
    names must be findable in one documented place rather than only in code.
    """
    # Do NOT import the hook: path-resolution-hook.py calls main() unguarded at
    # module level (no `if __name__ == "__main__"`), so importing it RUNS the
    # hook and raises SystemExit. Read the literals with ast instead — that is
    # also the honest thing to assert against, since the deny list is data.
    import ast
    tree = ast.parse(HOOK_PY.read_text(encoding="utf-8"))
    fenced = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_SCRIPT_OWNED_SESSION_FILES" in names:
            call = node.value
            arg = call.args[0] if isinstance(call, ast.Call) and call.args else call
            fenced |= set(ast.literal_eval(arg))
        elif "_SCRIPT_OWNED_SESSION_PREFIXES" in names:
            fenced |= set(ast.literal_eval(node.value))
    assert fenced, "could not read the fenced-basename literals from the hook"

    conv = PROJECT_ROOT / "core" / "config" / "conventions" / "temp-store.md"
    text = conv.read_text(encoding="utf-8", errors="replace") if conv.is_file() else ""
    if not text.strip():
        pytest.skip("temp-store.md unreadable on this box")

    missing = [b for b in sorted(fenced) if b not in text]
    assert not missing, (
        "script-owned basenames absent from temp-store.md (SSOT drift): "
        f"{missing}"
    )


def test_carrier_deny_names_its_owner_too():
    """The prefix-matched carrier is the incident file — it must name its writer.

    Regression pin: the owner map is keyed by exact basename, so a carrier
    (`body-heartbeat-<sid>.json`) could never hit a key and fell back to the
    generic "the framework script that owns it". A basename-only unit test on
    `agent-state` passed the whole time; only a live production-shape probe
    surfaced it. That asymmetry — unit-green while the real case degrades — is
    the reason this case is pinned separately rather than folded into the
    parametrize above.
    """
    r = invoke_hook(f"{PR}/agents/{AGENT}/session/body-heartbeat-abc123.json")
    assert r["decision"] == "deny"
    assert "the framework script that owns it" not in r["reason"], (
        "carrier deny fell back to the generic owner string. "
        f"reason={r['reason'][:300]}"
    )
    assert "heartbeat" in r["reason"].lower(), (
        f"carrier deny does not name its writer. reason={r['reason'][:300]}"
    )


# --------------------------- BYPASS PINS (fresh-eyes, 2026-08-30) ----------
# All four of these probed APPROVE against the first shipped version of the
# predicate. They are pinned separately from the parametrize above because they
# are not variations of the SAME question — each is a distinct way for a path to
# denote a fenced file without spelling it the obvious way.

@pytest.mark.parametrize("spelling,why", [
    ("session/./agent-state",              "dot segment survives a raw split"),
    ("sessions/../session/agent-state",    "parent traversal survives a raw split"),
    ("session/Agent-State",                "case-insensitive FS: same file on win/mac"),
    ("session/agent-state ",               "windows strips trailing space: same file"),
    ("session/Body-Heartbeat-XY.json",     "prefix rule must fold case too"),
])
def test_alternate_spellings_of_a_fenced_file_are_refused(spelling, why):
    r = invoke_hook(f"{PR}/agents/{AGENT}/{spelling}")
    assert r["decision"] == "deny", (
        f"bypass reopened ({why}): {spelling!r} was approved. "
        f"reason={r['reason'][:200]}"
    )
