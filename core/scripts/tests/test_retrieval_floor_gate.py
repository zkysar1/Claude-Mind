""" layer 1: the retrieval floor for NON-LOOP sessions.

WHAT THIS EXISTS FOR. User directive 2026-08-31: assistant mode under-retrieves,
and "not only user questions should trigger fetching". The autonomous loop has
per-decision retrieval enforcement (execute Phase 4 + the Phase 9.5b audit);
assistant / reader / observer sessions had none. This is the mechanical floor:
when a NON-LOOP session writes to a knowledge / ground-truth store having
consulted nothing, it says so.

THE LOAD-BEARING DISTINCTION THIS PINS — `retrieval` vs `retrieval-auto`.
retrieve.sh records every successful consultation, and it has two callers with
opposite meanings: an agent deliberately consulting the stores, and
user-prompt-retrieval-inject.sh's AUTOMATIC per-prompt pre-pass, which the model
never asked for and may never read. That hook fires on essentially every
substantive user message, so counting its retrievals as evidence would make this
floor UNREACHABLE — it would pass for every session in which a human typed a
sentence, while measuring nothing. A gate that cannot fail is worse than no gate,
because it reads as coverage (guard-1760).

`test_auto_prepass_alone_does_not_satisfy_the_floor` is the guard on that. It is
the mutation target: drop "retrieval-auto" from the exclusion (i.e. add it to
DELIBERATE_RETRIEVAL_KINDS) and it goes red, while every other test here stays
green — which a presence-only assertion would not have caught.

THE NEGATIVE IS NARROW (guard-4407) and two tests pin that it stays advisory:
the hook binds to the Edit/MultiEdit/Write TOOLS and the manifest it reads is fed
by hooks bound to Read/WebFetch/WebSearch, so under a Bash-preference session
BOTH halves go blind together and silently. The gate may therefore never refuse.

Hermetic: every test builds a throwaway agent dir under the real PROJECT_ROOT and
removes it, so no live session manifest is touched. The gate is invoked with the
production PreToolUse stdin JSON — canonical code path AND canonical invocation
shape (probe-with-canonical-code-path.md), not a hand-rolled equivalent.

Run: py -3 -m pytest core/scripts/tests/test_retrieval_floor_gate.py -v
"""
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

THROWAWAY_AGENT = "_retrieval_floor_gate_test_throwaway_agent_"
SID = "retrieval-floor-sid-001"
GATE = "core/scripts/retrieval-floor-gate.sh"

KNOWLEDGE_PATH = str(PROJECT_ROOT / "world" / "knowledge" / "tree" / "system" / "example-node.md")
CONVENTION_PATH = str(PROJECT_ROOT / "core" / "config" / "conventions" / "session-state.md")
OUT_OF_SCOPE_PATH = str(PROJECT_ROOT / "core" / "scripts" / "some-script.sh")
ANCHOR_PATH = str(PROJECT_ROOT / ".claude" / "settings.local.json")


@contextmanager
def _throwaway_agent(mode="assistant"):
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline="")
        (session_dir / "agent-mode").write_text(mode, encoding="utf-8", newline="")
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _record(kind, value, env, session_id=SID):
    return subprocess.run(
        [sys.executable, "core/scripts/context-reads.py", "record-prov",
         "--session-id", session_id, "--kind", kind, value],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(PROJECT_ROOT))


def _floor(env, session_id=SID):
    """context-reads.py retrieval-floor -> (rc, printed count)."""
    r = subprocess.run(
        [sys.executable, "core/scripts/context-reads.py", "retrieval-floor",
         "--session-id", session_id],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout.strip()


def _gate(file_path, env, session_id=SID, tool="Edit"):
    payload = {"tool_name": tool, "session_id": session_id,
               "tool_input": {"file_path": file_path}}
    r = subprocess.run([BASH, GATE], input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=90, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout, r.stderr


def _fired(stdout, stderr):
    return "retrieval-floor-gate" in stderr or "retrieval-floor-gate" in stdout


# ── the floor query ────────────────────────────────────────────────────────

def test_fresh_session_has_no_consultations():
    with _throwaway_agent() as env:
        rc, n = _floor(env)
        assert rc == 1, "a session that consulted nothing must exit 1"
        assert n == "0"


def test_a_deliberate_retrieval_satisfies_the_floor():
    with _throwaway_agent() as env:
        _record("retrieval", "how does X work", env)
        rc, n = _floor(env)
        assert rc == 0, "a deliberate retrieve.sh consult must satisfy the floor"
        assert n == "1"


def test_auto_prepass_alone_does_not_satisfy_the_floor():
    """THE mutation target — see the module docstring.

    The UserPromptSubmit hook retrieves automatically on nearly every substantive
    user message. If that counted, the floor could never fail on any session
    where a human typed a sentence, and would report coverage it never measured.
    """
    with _throwaway_agent() as env:
        _record("retrieval-auto", "injected pre-pass for the user's message", env)
        rc, n = _floor(env)
        assert rc == 1, (
            "the AUTOMATIC pre-pass must NOT count as the agent having consulted "
            "anything — counting it makes the floor unreachable")
        assert n == "0", f"auto entries must not be counted, got {n}"


def test_other_retrieval_lanes_do_satisfy_the_floor():
    # A fetched source or a tree-node read IS the agent checking something.
    for kind in ("url", "search", "node", "board"):
        with _throwaway_agent() as env:
            _record(kind, f"value-for-{kind}", env)
            rc, _n = _floor(env)
            assert rc == 0, f"kind {kind!r} must satisfy the floor"


# ── the gate ───────────────────────────────────────────────────────────────

def test_gate_fires_on_a_knowledge_write_with_no_consultation():
    with _throwaway_agent() as env:
        rc, out, err = _gate(KNOWLEDGE_PATH, env)
        assert rc == 0, "ADVISORY: the gate must always exit 0"
        assert _fired(out, err), "gate must warn on a knowledge write with zero consultations"
        payload = json.loads(out)
        h = payload["hookSpecificOutput"]
        assert h["permissionDecision"] == "allow", "must never deny — advisory only"
        assert h["hookEventName"] == "PreToolUse"
        # The structured payload is the ONLY channel that reaches the model
        # (guard-1680); stderr alone communicates nothing to it.
        assert h["permissionDecisionReason"].strip()
        assert h["additionalContext"].strip()


def test_gate_is_silent_after_a_deliberate_retrieval():
    with _throwaway_agent() as env:
        _record("retrieval", "consulted the tree first", env)
        rc, out, err = _gate(KNOWLEDGE_PATH, env)
        assert rc == 0
        assert not _fired(out, err), "must stay silent once the session has consulted something"


def test_gate_fires_for_convention_files_too():
    with _throwaway_agent() as env:
        rc, out, err = _gate(CONVENTION_PATH, env)
        assert rc == 0
        assert _fired(out, err), "conventions assert ground truth and are in scope"


def test_gate_is_silent_outside_the_knowledge_scope():
    """Scope is deliberately narrower than pre-edit-context-gate's. A script edit
    is not a knowledge claim, and warning there spends the banner's credibility
    on the wrong writes."""
    with _throwaway_agent() as env:
        rc, out, err = _gate(OUT_OF_SCOPE_PATH, env)
        assert rc == 0
        assert not _fired(out, err)


def test_gate_is_silent_in_autonomous_sessions():
    """outcome (4): loop sessions unaffected by default. They retrieve at Phase 4
    and are audited at Phase 9.5b; firing here would duplicate that."""
    with _throwaway_agent(mode="autonomous") as env:
        rc, out, err = _gate(KNOWLEDGE_PATH, env)
        assert rc == 0
        assert not _fired(out, err), "the autonomous loop has its own retrieval discipline"


def test_gate_never_emits_an_allow_for_the_constitutional_anchor():
    """The payload carries permissionDecision:allow, which short-circuits the
    permission system. The anchor is hard-denied at every tier; this gate must
    never hand out an allow that could weaken that."""
    with _throwaway_agent() as env:
        rc, out, err = _gate(ANCHOR_PATH, env)
        assert rc == 0
        assert out.strip() == "", "no payload may be emitted for the anchor"


def test_gate_fails_open_on_garbage_stdin():
    with _throwaway_agent() as env:
        r = subprocess.run([BASH, GATE], input="not json at all", capture_output=True,
                           text=True, env=env, timeout=90, cwd=str(PROJECT_ROOT))
        assert r.returncode == 0, "fail-open: a malformed payload must never block a write"


# ── producer wiring (structural: a consumer says nothing about the writer) ──

def test_retrieve_sh_records_the_consultation():
    """guard-1943: pinning the query says nothing about the wiring. The floor is
    inert unless retrieve.sh actually records, and retrieve.sh is the framework's
    unified retrieval entry point."""
    src = (SCRIPT_DIR / "retrieve.sh").read_text(encoding="utf-8")
    assert "record-prov" in src, "retrieve.sh must record consultations into the manifest"
    assert "MIND_RETRIEVAL_AUTO" in src, (
        "retrieve.sh must distinguish the automatic pre-pass from a deliberate consult")


def test_inject_hook_marks_its_prepass_as_automatic():
    """The pair is the mechanism: retrieve.sh reads the flag, the inject hook sets
    it. Either half alone silently collapses the distinction the floor rests on."""
    src = (SCRIPT_DIR / "user-prompt-retrieval-inject.sh").read_text(encoding="utf-8")
    assert "MIND_RETRIEVAL_AUTO" in src, (
        "user-prompt-retrieval-inject.sh must mark its automatic retrieval, or its "
        "pre-pass counts as a deliberate consult and the floor becomes unreachable")


def test_gate_is_registered_in_the_gate_registry():
    import yaml
    d = yaml.safe_load((PROJECT_ROOT / "core" / "config" / "gates.yaml").read_text(encoding="utf-8"))
    ids = [g["id"] for g in d["gates"]]
    assert "retrieval-floor-gate" in ids, "gate-log.sh requires the id to exist in gates.yaml"


def test_gate_is_wired_into_settings_for_every_write_shaped_tool():
    """guard-3448: a gate is only as broad as its entry points. Edit alone would
    leave Write and MultiEdit uncovered."""
    d = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    wired = {e.get("matcher")
             for e in d["hooks"]["PreToolUse"]
             for h in e.get("hooks", [])
             if "retrieval-floor-gate" in h.get("command", "")}
    assert {"Edit", "MultiEdit", "Write"} <= wired, f"missing matchers, wired at {wired}"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
