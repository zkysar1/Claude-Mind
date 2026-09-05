""" layer 2: the ZERO-RETRIEVAL PULSE for NON-LOOP sessions.

WHAT THIS EXISTS FOR. Layer 1 (retrieval-floor-gate) asks one question at one
moment: "did this session consult anything before writing to a knowledge store?"
That leaves the mid-task INTERIOR uncovered. A non-loop session can run dozens of
substantive tool calls, drift a long way from whatever it last checked, and never
write to a knowledge store at all -- so the floor never fires and no layer is
watching. The pulse counts consecutive tool calls with no NEW deliberate
consultation and, at the threshold, says so once and resets.

THE MUTATION TARGET -- `test_auto_prepass_does_not_reset_the_streak`.
The layer-1 author recorded the design constraint this inherits:
user-prompt-retrieval-inject.sh runs an AUTOMATIC retrieval on essentially every
substantive user message. If `retrieval-auto` reset the pulse, the counter would
reset constantly and the advisory would never fire -- measuring nothing while
reading as coverage (guard-1760). Add "retrieval-auto" to
DELIBERATE_RETRIEVAL_KINDS and that one test goes red while the rest stay green,
which a presence-only assertion would not catch.

WHY IT DELEGATES THE COUNT. cmd_retrieval_pulse calls count_deliberate_retrievals
-- the SAME predicate layer 1 uses -- rather than re-reading the manifest. The two
layers must agree about what a consultation IS, or a session could satisfy one
and trip the other on identical evidence.

THE NEGATIVE IS NARROW (guard-4407), exactly as in layer 1: the manifest is fed
by hooks bound to Read/WebFetch/WebSearch plus retrieve.sh and tree-read.sh, so a
consultation made with cat/curl/grep leaves no entry. A firing is a prompt to
check, never proof the session retrieved nothing -- which is why nothing may be
wired to refuse on this signal. `test_hook_never_blocks_it_is_advisory_only`
pins that.

Hermetic: every test builds a throwaway agent dir under the real PROJECT_ROOT and
removes it, so no live session manifest is touched. The telemetry test redirects
MIND_META to a tmpdir and opts in via GATE_LOG_ALLOW_PYTEST=1 (the sanctioned
pattern from test_layer_d_telemetry.py) so it asserts on a REAL firing record
without contaminating the production telemetry store.

Run: py -3 -m pytest core/scripts/tests/test_retrieval_pulse.py -v
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

THROWAWAY_AGENT = "_retrieval_pulse_test_throwaway_agent_"
SID = "retrieval-pulse-sid-001"
HOOK = "core/scripts/retrieval-pulse-hook.sh"
CR = "core/scripts/context-reads.py"
TELEMETRY_GLOB = "gate-firings*.jsonl"


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


def _pulse(env, threshold=3, session_id=SID, extra=None):
    """context-reads.py retrieval-pulse -> (rc, stdout). rc 0 = fired."""
    cmd = [sys.executable, CR, "retrieval-pulse", "--session-id", session_id]
    if threshold is not None:
        cmd += ["--threshold", str(threshold)]
    r = subprocess.run(cmd + (extra or []), capture_output=True, text=True,
                       env=env, timeout=60, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout.strip()


def _record(kind, value, env, session_id=SID):
    return subprocess.run(
        [sys.executable, CR, "record-prov", "--session-id", session_id,
         "--kind", kind, value],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(PROJECT_ROOT))


def _clear(env, session_id=SID):
    return subprocess.run(
        [sys.executable, CR, "clear", "--session-id", session_id],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(PROJECT_ROOT))


def _hook(env, session_id=SID, stdin=None):
    payload = stdin if stdin is not None else json.dumps(
        {"tool_name": "Bash", "session_id": session_id, "tool_input": {"command": "ls"}})
    r = subprocess.run([BASH, HOOK], input=payload, capture_output=True, text=True,
                       env=env, timeout=90, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout, r.stderr


def _tick_to_threshold(env, threshold):
    """Tick threshold-1 times; every one must stay silent."""
    for i in range(threshold - 1):
        rc, _ = _pulse(env, threshold=threshold)
        assert rc == 1, "tick %d of %d fired early" % (i + 1, threshold)


# -- the counter ------------------------------------------------------------

def test_pulse_is_silent_below_threshold():
    with _throwaway_agent() as env:
        _tick_to_threshold(env, 5)


def test_pulse_fires_exactly_at_threshold():
    with _throwaway_agent() as env:
        _tick_to_threshold(env, 3)
        rc, out = _pulse(env, threshold=3)
        assert rc == 0, "the pulse must fire on the threshold-th consecutive tick"
        assert "retrieval-pulse" in out
        assert "3" in out, "the advisory must name the count"


def test_pulse_resets_after_firing():
    """Firing must reset, or every later tick fires and the advisory becomes noise."""
    with _throwaway_agent() as env:
        _tick_to_threshold(env, 3)
        rc, _ = _pulse(env, threshold=3)
        assert rc == 0
        rc, _ = _pulse(env, threshold=3)
        assert rc == 1, "the tick right after a firing must be silent"


def test_deliberate_retrieval_resets_the_streak():
    with _throwaway_agent() as env:
        _tick_to_threshold(env, 3)          # streak now 2, one tick from firing
        _record("retrieval", "how does X work", env)
        rc, _ = _pulse(env, threshold=3)
        assert rc == 1, "a deliberate consult must reset the streak, not fire"


def test_auto_prepass_does_not_reset_the_streak():
    """THE mutation target -- see the module docstring.

    user-prompt-retrieval-inject.sh retrieves automatically on nearly every
    substantive user message. If that reset the pulse, the counter would reset
    constantly and the advisory could never fire on a real session.
    """
    with _throwaway_agent() as env:
        _tick_to_threshold(env, 3)
        _record("retrieval-auto", "injected pre-pass", env)
        rc, out = _pulse(env, threshold=3)
        assert rc == 0, "the automatic pre-pass must NOT reset the streak"
        assert "retrieval-pulse" in out


def test_other_deliberate_lanes_also_reset():
    """node / url / search / board are consultations too -- layer 1 counts them."""
    for kind in ("node", "url", "search", "board"):
        with _throwaway_agent() as env:
            _tick_to_threshold(env, 3)
            _record(kind, "some-" + kind, env)
            rc, _ = _pulse(env, threshold=3)
            assert rc == 1, "%s must reset the streak" % kind


def test_manifest_clear_resets_rather_than_fires():
    """A DECREASE means the manifest was cleared (new context window).

    The streak that preceded it describes a session that no longer exists, so
    continuing to count it would fire an advisory about someone else's work.
    """
    with _throwaway_agent() as env:
        _record("retrieval", "first", env)
        _tick_to_threshold(env, 3)
        _clear(env)
        rc, _ = _pulse(env, threshold=3)
        assert rc == 1, "a manifest clear must reset the streak, not fire"


def test_threshold_zero_disables_the_pulse():
    with _throwaway_agent() as env:
        for _ in range(10):
            rc, _ = _pulse(env, threshold=0)
            assert rc == 1, "threshold <=0 must disable the pulse entirely"


def test_env_var_sets_the_threshold_when_no_flag_is_given():
    with _throwaway_agent() as env:
        env["RETRIEVAL_PULSE_THRESHOLD"] = "2"
        rc, _ = _pulse(env, threshold=None)
        assert rc == 1
        rc, out = _pulse(env, threshold=None)
        assert rc == 0, "$RETRIEVAL_PULSE_THRESHOLD must set the threshold"
        assert "2" in out


def test_explicit_flag_beats_the_env_var():
    with _throwaway_agent() as env:
        env["RETRIEVAL_PULSE_THRESHOLD"] = "2"
        _tick_to_threshold(env, 4)
        rc, _ = _pulse(env, threshold=4)
        assert rc == 0, "--threshold must win over the env var"


def test_quiet_suppresses_the_text_but_not_the_exit_code():
    with _throwaway_agent() as env:
        _tick_to_threshold(env, 2)
        rc, out = _pulse(env, threshold=2, extra=["--quiet"])
        assert rc == 0
        assert out == "", "--quiet must print nothing"


# -- the hook ---------------------------------------------------------------

def test_hook_is_silent_in_autonomous_sessions():
    """Outcome 4: loop sessions unaffected.

    The loop retrieves for itself (execute Phase 4) and is audited at Phase 9.5b;
    firing here would spend the banner on the one path that already has teeth.
    """
    with _throwaway_agent(mode="autonomous") as env:
        env["RETRIEVAL_PULSE_THRESHOLD"] = "1"
        for _ in range(5):
            rc, out, err = _hook(env)
            assert rc == 0
            assert "retrieval-pulse" not in out, "must not fire in an autonomous session"
            assert "retrieval-pulse" not in err


def test_hook_fires_in_an_assistant_session_and_emits_posttooluse_context():
    with _throwaway_agent(mode="assistant") as env:
        env["RETRIEVAL_PULSE_THRESHOLD"] = "2"
        _hook(env)
        rc, out, err = _hook(env)
        assert rc == 0, "the hook must always exit 0"
        assert "retrieval-pulse" in err, "the advisory must reach stderr"
        payload = json.loads(out)
        hso = payload["hookSpecificOutput"]
        assert hso["hookEventName"] == "PostToolUse"
        assert "retrieval-pulse" in hso["additionalContext"]


def test_hook_never_blocks_it_is_advisory_only():
    """A PostToolUse hook cannot block, and this one must never try.

    No permissionDecision key may appear: the honest limit (guard-4407) means a
    firing is not proof the session retrieved nothing, so nothing may refuse on it.
    """
    with _throwaway_agent(mode="assistant") as env:
        env["RETRIEVAL_PULSE_THRESHOLD"] = "1"
        rc, out, _ = _hook(env)
        assert rc == 0
        assert "permissionDecision" not in out
        assert "deny" not in out


def test_hook_fails_open_on_garbage_stdin():
    with _throwaway_agent() as env:
        rc, _out, _err = _hook(env, stdin="not json at all {{{")
        assert rc == 0, "the hook must fail open on unparseable stdin"


def test_hook_is_silent_below_threshold():
    with _throwaway_agent(mode="assistant") as env:
        env["RETRIEVAL_PULSE_THRESHOLD"] = "9"
        rc, out, _err = _hook(env)
        assert rc == 0
        assert out.strip() == "", "no payload below threshold"


# -- telemetry (outcome 3) --------------------------------------------------

def test_a_firing_is_logged_to_gate_telemetry(tmp_path):
    """Outcome 3, asserted on a REAL record rather than on the call site.

    Redirects MIND_META to a tmpdir and opts in via GATE_LOG_ALLOW_PYTEST=1 --
    the sanctioned pattern (_gate_log.py docstring) -- so the production
    telemetry store is never touched. Read-only over the tmp copy.
    """
    tmp_meta = tmp_path / "meta"
    tmp_meta.mkdir()
    with _throwaway_agent(mode="assistant") as env:
        env["MIND_META"] = str(tmp_meta)
        env["GATE_LOG_ALLOW_PYTEST"] = "1"
        env["RETRIEVAL_PULSE_THRESHOLD"] = "1"
        rc, _out, err = _hook(env)
        assert rc == 0
        assert "retrieval-pulse" in err, "precondition: the hook must have fired"

        rows = []
        for f in sorted(tmp_meta.glob(TELEMETRY_GLOB)):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        assert any(r.get("gate_id") == "retrieval-pulse-hook" for r in rows), (
            "a firing must be logged to gate telemetry; got %r" % (rows,))


# -- registration -----------------------------------------------------------

def test_hook_is_registered_in_the_gate_registry():
    import yaml
    d = yaml.safe_load(
        (PROJECT_ROOT / "core" / "config" / "gates.yaml").read_text(encoding="utf-8"))
    ids = [g.get("id") for g in (d.get("gates") or [])]
    assert "retrieval-pulse-hook" in ids


def test_hook_is_wired_into_settings_posttooluse():
    d = json.loads(
        (PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = [h.get("command", "")
            for blk in d["hooks"]["PostToolUse"]
            for h in blk.get("hooks", [])]
    assert any("retrieval-pulse-hook.sh" in c for c in cmds), (
        "the pulse must be wired into PostToolUse or it never runs")


def test_pulse_state_is_routed_per_body():
    """Two Bodies of one agent must not share a streak counter.

    Same reason they must not share a dedup tracker (Phase 1D per-Body routing).
    pulse_state_path must DELEGATE to tracker_path rather than resolving the
    session dir a second time, or the two can drift apart silently.
    """
    src = (SCRIPT_DIR / "context-reads.py").read_text(encoding="utf-8")
    assert "def pulse_state_path" in src
    assert "tracker_path(session_id=session_id)" in src, (
        "pulse_state_path must delegate to tracker_path, not re-resolve the session dir")
