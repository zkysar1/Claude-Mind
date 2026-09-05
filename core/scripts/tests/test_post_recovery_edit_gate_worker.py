"""The post-recovery edit gate's two narrowings —  (worker), 
(graceful stop).

Both exist because the gate's (state=IDLE, mode=autonomous) tuple was documented
as "unambiguously" a crashed loop and is not: TWO distinct populations live there
by design. The worker case is below. The graceful-stop case is the deferred stop
sequence, which sets IDLE at D1 and the target mode only at D7 — so D4
consolidation and everything around it runs at exactly that tuple, and
aspirations-graceful-stop/SKILL.md says so itself ("retains autonomous
capabilities through D7"). Its predicate is `session/stop-checkpoint.json`,
written at GS-0 and cleared only at D7.1, so its presence brackets the stop
sequence and nothing else.

The two narrowings share `test_crashed_reducer_is_still_refused` as their common
load-bearing negative: that stage has neither a forked WM nor a checkpoint, so it
proves BOTH exemptions stayed narrow.

--- the worker case (g-306-234) ---

WHAT BROKE: the gate refuses framework edits on the (state=IDLE, mode=autonomous)
tuple, and its docstring called that tuple "unambiguously" a crashed or
auto-recovered loop. The Mind/Body split made the word false. A WORKER never
flips agent-state, so (IDLE, autonomous) is its normal, healthy, work-in-flight
condition — and every framework edit a worker made was refused, leaving the
documented override as the only way through. Ledger evidence before the fix: 3 of
the last 3 override records were a worker, one of them editing
`.claude/skills/worker-loop/SKILL.md` itself. With two worker bodies live
(alpha/cc-07, foxtrot/cc-08) the override becomes routine, and a gate everyone
always overrides has stopped being a gate.

THE RISK THIS FILE GUARDS: the fix NARROWS the gate, and a narrowing can go too
far. `test_crashed_reducer_is_still_refused` is the load-bearing case — the
canonical incident (charlie session d600a945, an orphan edit against a
confabulated goal id) must still be refused. It is asserted here as its own test
rather than folded into the positive one, so a future edit cannot satisfy the
worker case by disabling the gate outright.

Predicate under test: a Body with a forked `sessions/<SID>/working-memory.yaml`
is a WORKER; a reducer stays on the agent-wide WM and never has one.

Hook contract: exit 0 + EMPTY stdout = approve. exit 0 + JSON on stdout = deny.
A non-zero exit is a hook ERROR (fail-open), not a deny.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GATE_REL = Path("core") / "scripts" / "post-recovery-edit-gate.py"
IN_SCOPE_FILE = "core/scripts/some_framework_script.py"


def _stage(tmp_path, *, state="IDLE", mode="autonomous", worker=False,
           stopping=False, stray_checkpoint=False, binding_mode=None,
           omit_binding_mode=False,
           agent="alpha", sid="11111111-2222-3333-4444-555555555555"):
    """Relocated PROJECT_ROOT. The gate derives PROJECT_ROOT from its own
    location (parent.parent.parent), so copying core/scripts into a staged tree
    is what redirects it -- COPY, never symlink (guard-2534)."""
    root = tmp_path / "repo"
    shutil.copytree(
        REPO / "core" / "scripts",
        root / "core" / "scripts",
        ignore=shutil.ignore_patterns("tests", "__pycache__", ".python-shim", "*.pyc"),
    )
    (root / "core" / "config").mkdir(parents=True, exist_ok=True)

    adir = root / "agents" / agent
    (adir / "session").mkdir(parents=True)
    (adir / "session" / "agent-state").write_text(state, encoding="utf-8")
    (adir / "session" / "agent-mode").write_text(mode, encoding="utf-8")
    (adir / "local-paths.conf").write_text(
        f"WORLD_PATH={(root / 'world').as_posix()}\n", encoding="utf-8")
    (root / "world").mkdir(exist_ok=True)

    # Session binding (Phase 2.6 layout) so the gate can resolve agent from SID.
    sess = adir / "sessions" / sid
    sess.mkdir(parents=True)
    # `binding_mode` lets the PER-SESSION mode diverge from the AGENT-WIDE
    # agent-mode file. They agreed unconditionally until 2026-09-04, which is
    # exactly why this file could pass while the gate read the wrong one — see
    # test_session_binding_mode_wins_over_agent_wide. `omit_binding_mode` stages
    # the legacy shape (no mode key), where agent-wide is still the only signal.
    _bmode = mode if binding_mode is None else binding_mode
    _mode_line = "" if omit_binding_mode else f"mode: {_bmode}\n"
    # `session_id` is REQUIRED, not decorative. Without it
    # `_try_phase26_binding_with_reason` returns (None, "session-id-mismatch")
    # and resolve_binding silently falls back to the legacy
    # `.active-agent-<SID>` file — which carries an agent name but NO mode.
    # This key was missing until 2026-09-04, so every test in this file had been
    # exercising the LEGACY path only; the Phase 2.6 binding it appeared to
    # stage was never once parsed. The tests still passed because the agent
    # resolved fine either way and mode came from the agent-wide file, which is
    # exactly the read this file now has to be able to distinguish.
    (sess / "binding.yaml").write_text(
        f"session_id: {sid}\nagent: {agent}\n{_mode_line}"
        f"started_at: 2026-08-06T00:00:00\nstarted_by: test\n", encoding="utf-8")
    # Legacy fallback too, so the resolver finds it either way.
    (root / f".active-agent-{sid}").write_text(agent, encoding="utf-8")

    # THE PREDICATE: a forked per-session WM means WORKER.
    if worker:
        (sess / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")

    # THE OTHER PREDICATE: stop-checkpoint.json in the AGENT-WIDE session dir
    # means a graceful stop is in progress. `agent_state_dir()` resolves to
    # agents/<name>/session (singular), which is what stop_checkpoint.py writes.
    if stopping:
        (adir / "session" / "stop-checkpoint.json").write_text(
            '{"target_mode": "assistant"}\n', encoding="utf-8")
    # Same filename in the PER-SESSION dir — the two-tier layout's other half.
    # Must NOT exempt: nothing writes it there, so a match would mean the gate
    # is globbing rather than reading the one path that brackets the stop.
    if stray_checkpoint:
        (sess / "stop-checkpoint.json").write_text(
            '{"target_mode": "assistant"}\n', encoding="utf-8")

    return root, sid


def _run(root, sid, *, rel_path=IN_SCOPE_FILE, new_string="x = 1"):
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(root / rel_path),
            "old_string": "x = 0",
            "new_string": new_string,
        },
        "session_id": sid,
    }
    r = subprocess.run(
        [sys.executable, str(root / GATE_REL)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=120,
        cwd=str(root),
    )
    return r


def _is_deny(r):
    """Deny == exit 0 with a JSON decision on stdout. Non-zero is fail-open."""
    if r.returncode != 0:
        return False
    out = (r.stdout or "").strip()
    if not out:
        return False
    try:
        return isinstance(json.loads(out), dict)
    except ValueError:
        return False


def test_worker_body_is_approved(tmp_path):
    """THE DEFECT: a worker is (IDLE, autonomous) by design and was refused."""
    root, sid = _stage(tmp_path, worker=True)
    r = _run(root, sid)
    assert not _is_deny(r), (
        "the gate REFUSED a worker Body's framework edit. A worker never flips "
        "agent-state, so (IDLE, autonomous) is its normal working condition — "
        f"gating it makes the override routine. stdout={r.stdout[:400]}"
    )


def test_graceful_stop_is_approved(tmp_path):
    """THE SECOND DEFECT (): D1 sets IDLE, D7 sets mode, so every step
    between them — D4 consolidation above all — is (IDLE, autonomous) BY DESIGN.

    Refusing there left the override as the only route, and the override token
    must appear IN the edit content, so taking it deposits permanent cruft in a
    framework file. That cost is why alpha abandoned the edit instead of
    overriding on 2026-08-05 — which is also why the override ledger shows zero
    graceful-stop records while the false-positive was real. A ledger of
    ACCEPTED overrides cannot count REFUSALS.
    """
    root, sid = _stage(tmp_path, worker=False, stopping=True)
    r = _run(root, sid)
    assert not _is_deny(r), (
        "the gate REFUSED a framework edit during a graceful stop. D1..D7 runs "
        "at (IDLE, autonomous) by design and the skill declares it retains "
        f"autonomous capability there. stdout={r.stdout[:400]}"
    )


def test_crashed_reducer_is_still_refused(tmp_path):
    """LOAD-BEARING NEGATIVE for BOTH narrowings: neither may disable the gate.

    A crashed/auto-recovered REDUCER has no forked per-session WM AND no
    stop-checkpoint (nothing requested a stop — it just died). This is the
    charlie d600a945 shape the gate exists for.
    """
    root, sid = _stage(tmp_path, worker=False, stopping=False)
    r = _run(root, sid)
    assert _is_deny(r), (
        "a crashed REDUCER's framework edit was approved — an exemption "
        "widened into a hole and the gate no longer catches the orphan-edit "
        f"incident it was built for. rc={r.returncode} stdout={r.stdout[:400]}"
    )


def test_stray_per_session_checkpoint_does_not_exempt(tmp_path):
    """PATH PIN. The checkpoint lives in the AGENT-WIDE session/ dir; the
    per-session sessions/<SID>/ dir is a different tier that nothing writes it
    to. A gate that matched the filename anywhere would exempt on a file no
    stop ever creates — the exemption must read the one path that brackets the
    stop sequence, not the basename.
    """
    root, sid = _stage(tmp_path, worker=False, stray_checkpoint=True)
    r = _run(root, sid)
    assert _is_deny(r), (
        "a stop-checkpoint.json in the PER-SESSION dir exempted the edit — the "
        "predicate is matching a basename rather than the agent-wide "
        f"session/stop-checkpoint.json. rc={r.returncode} stdout={r.stdout[:400]}"
    )


def test_assistant_mode_still_exempt(tmp_path):
    """Pre-existing exemption must survive the change."""
    root, sid = _stage(tmp_path, mode="assistant", worker=False)
    assert not _is_deny(_run(root, sid))


def test_running_reducer_still_exempt(tmp_path):
    """The loop owns its own edits."""
    root, sid = _stage(tmp_path, state="RUNNING", worker=False)
    assert not _is_deny(_run(root, sid))


def test_out_of_scope_path_untouched(tmp_path):
    """Only framework paths are gated; a worker-or-not distinction must not leak
    into files the gate never governed."""
    root, sid = _stage(tmp_path, worker=False)
    r = _run(root, sid, rel_path="agents/alpha/notes.md")
    assert not _is_deny(r)


# --------------------------------------------------------------------------
# THE THIRD NARROWING (2026-09-04): mode is read PER SESSION, not agent-wide.
#
# The two narrowings above fixed WHICH TUPLES are exempt. Both still read the
# tuple's mode from `session/agent-mode`, which is AGENT-WIDE — so on a box
# running more than one Body it reports whichever Body started LAST, not the
# session doing the asking. `_stage` wrote both sources from one `mode`
# argument, so they could never disagree here and the whole file passed while
# the gate consulted the wrong file in production.
# --------------------------------------------------------------------------

def test_session_binding_mode_wins_over_agent_wide(tmp_path):
    """THE DEFECT: an assistant session refused because a WORKER wrote the
    agent-wide file after it.

    MEASURED on DESKTOP-O91DLK2 — a session bound `mode: assistant` at
    2026-09-02T19:02:14 was refused because a worker Body started the next day
    left `autonomous` in agents/alpha/session/agent-mode. The gate's own
    `(IDLE, assistant) — user-directed work` exemption was never reached, so
    the override became the only route: the same "how a gate stops being a
    gate" failure the worker narrowing was written to end.

    Note this session is NOT a worker (no forked WM) and is NOT stopping, so
    neither existing exemption can rescue it — only reading the binding does.
    """
    root, sid = _stage(tmp_path, mode="autonomous", binding_mode="assistant",
                       worker=False, stopping=False)
    r = _run(root, sid)
    assert not _is_deny(r), (
        "the gate REFUSED a session whose own binding.yaml says mode: assistant, "
        "because the AGENT-WIDE agent-mode file said autonomous. Mode must be "
        "resolved per session (CLAUDE.md § Session Binding), not from a file any "
        f"co-resident Body can overwrite. stdout={r.stdout[:400]}"
    )


def test_binding_autonomous_refused_even_when_agent_wide_says_assistant(tmp_path):
    """LOAD-BEARING NEGATIVE for the third narrowing — the binding must win in
    BOTH directions.

    Without this, the change above is satisfiable by simply making the gate more
    permissive (e.g. approving whenever a binding exists, or dropping the mode
    check). Here the AGENT-WIDE file says `assistant` — the permissive reading —
    while this session's binding says `autonomous` with no forked WM and no
    checkpoint: the crashed-reducer shape. It must still be refused.
    """
    root, sid = _stage(tmp_path, mode="assistant", binding_mode="autonomous",
                       worker=False, stopping=False)
    r = _run(root, sid)
    assert _is_deny(r), (
        "a crashed REDUCER (binding mode: autonomous, no forked WM, no "
        "checkpoint) was APPROVED because the agent-wide file happened to say "
        "assistant. The per-session binding is authoritative in both "
        f"directions. rc={r.returncode} stdout={r.stdout[:400]}"
    )


def test_missing_binding_mode_falls_back_to_agent_wide(tmp_path):
    """BACKWARD COMPATIBILITY. `resolve_binding` also serves the legacy
    `.active-agent-<SID>` form, where `mode` is None. There the agent-wide file
    is still the only signal available and the pre-2026-09-04 behaviour must be
    preserved exactly — a binding with no mode key must not read as "no mode,
    therefore approve".
    """
    root, sid = _stage(tmp_path, mode="autonomous", omit_binding_mode=True,
                       worker=False, stopping=False)
    r = _run(root, sid)
    assert _is_deny(r), (
        "a binding carrying no mode key caused the gate to skip the agent-wide "
        "fallback and approve a crashed-reducer edit. Absent per-session mode "
        f"means fall back, not fail open. rc={r.returncode} stdout={r.stdout[:400]}"
    )


def test_worker_exemption_survives_binding_mode_read(tmp_path):
    """The worker narrowing must not regress: a worker's binding says
    `autonomous` (it was started autonomous), so the binding read alone puts it
    back in scope and only the forked-WM predicate rescues it."""
    root, sid = _stage(tmp_path, mode="autonomous", binding_mode="autonomous",
                       worker=True)
    assert not _is_deny(_run(root, sid))
