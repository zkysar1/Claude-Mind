""", revised 2026-10-07 — the worker /stop branch: release, relay, push to
the worker ref, then CLOSE; the SID-scoped obligations; and the agent-wide negative.

WHY THIS FILE EXISTS
A user /stop on a worker Body was an UNDECLARED lifecycle stage. `worker_execute.py`
carries LIFECYCLE_DISPOSITIONS precisely so an undeclared stage fails at import
rather than by surprise, and it declared 16 stages with neither the user-stop path
nor the parked state among them — so the one instrument built to catch worker
lifecycle asymmetry was blind to the two states this change is about.

The 2026-10-07 revision replaced the stop's PARK with a CLOSE. A stopped SID never
resumes (`/start` refuses its fork file), and a stopped Body never reaches park
expiry (Phase -0-stop stands down first), so the park staged its learning late or
never. The stop also released no claims, pushed the shared branch from a worker, and
ran its telemetry close after the park, where parked-body-gate.py denied it. Why each
step sits where it does: core/config/rationale/worker-stop-close.md.

WHAT THIS SUITE PINS, AND WHY THE HALVES ARE SEPARATE
Some assertions below are FILE assertions and some are SOURCE assertions, and the
split is deliberate rather than convenient:

  * the agent-wide negative is asserted on REAL FILES in a tmp project root, for both
    Body-state mutations a worker can make (park, and the genuine close this branch
    now ends on). A prose assertion cannot catch a future edit that adds a write —
    which is the whole point of the agent-wide negative, the most important
    assertion here.
  * the ORDERING claims (worker-loop Phase -0-stop ahead of the park-due gate; the
    stop-hook's close branch ahead of its stop-requested valve; the branch's own step
    order) are claims ABOUT SOURCE, so source is the only place they can be checked.
  * the WIRING assertions are separate from both. guard-1943: pinning a writer says
    nothing about whether anything calls it, and this change's entire defect class
    was a correct component nothing invoked.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _bash_helpers import BASH  # noqa: E402

STOP_SKILL = PROJECT_ROOT / ".claude" / "skills" / "stop" / "SKILL.md"
WORKER_LOOP_SKILL = PROJECT_ROOT / ".claude" / "skills" / "worker-loop" / "SKILL.md"
STOP_HOOK = CORE_SCRIPTS / "stop-hook.sh"

SID_REDUCER = "11111111-1111-4111-8111-111111111111"
SID_WORKER = "22222222-2222-4222-8222-222222222222"

# The five AGENT-WIDE files a worker /stop must never touch. They are agent-wide,
# so on a cross-box fleet the reducer that owns them may be on ANOTHER MACHINE: a
# worker writing any of these stops the wrong Body while the user believes they
# stopped only the box in front of them.
AGENT_WIDE_FILES = (
    "agent-state",
    "agent-mode",
    "stop-loop",
    "stop-requested",
    "running-session-id",
)


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


we = _load("worker_execute", "worker_execute.py")
bm = _load("body_manifest", "body-manifest.py")


def _worker_branch() -> str:
    """The `IF output is "worker"` branch of /stop, sliced from its own delimiters.

    Sliced rather than read whole so an assertion about the WORKER branch cannot be
    satisfied by text living in the reducer branch further down the same file.
    """
    text = STOP_SKILL.read_text(encoding="utf-8")
    start = text.index('IF output is "worker":')
    end = text.index("DONE. Do NOT continue to Step 1", start)
    return text[start:end]


def _command_lines() -> list:
    """The branch's executable lines: `Bash:` commands and the `Skill:` call.
    Prose DISCUSSES files it does not write, so only these lines are inspected."""
    return [ln.strip() for ln in _worker_branch().splitlines()
            if ln.strip().startswith(("Bash:", "Skill:"))]


def _mk_worker_body(tmp_path: Path, agent: str = "alpha") -> Path:
    """A tmp project root holding a REDUCER sid on disk and a forked WORKER Body.

    write_manifest(role="worker") materializes the forked body-WM itself whenever a
    running-session-id names a DIFFERENT sid — that fork file is what park_body and
    close_body_on_genuine require, so seeding the reducer sid is what makes this a
    worker at all. remote_body defaults to False, so the close stages locally and
    pushes nothing.
    """
    state = tmp_path / "agents" / agent / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "running-session-id").write_text(SID_REDUCER, encoding="utf-8")
    (state / "working-memory.yaml").write_bytes(b"slots:\n  scratch: seeded\n")
    bm.write_manifest(SID_WORKER, agent, role="worker", project_root=tmp_path)
    return tmp_path


def _seed_agent_wide(pr: Path) -> dict:
    state = pr / "agents" / "alpha" / "session"
    seeded = {}
    for name in AGENT_WIDE_FILES:
        if name != "running-session-id":     # already holds the reducer sid
            (state / name).write_text(f"SEED-{name}\n", encoding="utf-8")
        seeded[name] = hashlib.sha256((state / name).read_bytes()).hexdigest()
    return seeded


def _assert_agent_wide_unchanged(pr: Path, seeded: dict) -> None:
    state = pr / "agents" / "alpha" / "session"
    for name, digest in seeded.items():
        after = hashlib.sha256((state / name).read_bytes()).hexdigest()
        assert after == digest, (
            f"worker /stop mutated the AGENT-WIDE file {name!r}. On a cross-box "
            "fleet the reducer owning it may be on another machine, so this stops "
            "the wrong Body while the user believes they stopped only this box.")


# ───────────────────────── (a) the lifecycle declaration ─────────────────────────

def test_lifecycle_contract_still_complete_with_the_two_new_stages():
    """The whole point of declaring them: gaps() must still be empty afterwards."""
    assert we.lifecycle_gaps() == []


@pytest.mark.parametrize("stage", ["user-stop", "park-resume"])
def test_new_stage_is_canonical_and_declared(stage):
    assert stage in we.CANONICAL_LIFECYCLE_STAGES, (
        f"{stage!r} missing from CANONICAL_LIFECYCLE_STAGES — a disposition row "
        "alone is not a declaration; lifecycle_gaps() checks BOTH directions")
    assert stage in we.LIFECYCLE_DISPOSITIONS


@pytest.mark.parametrize("stage", ["user-stop", "park-resume"])
def test_new_stage_kind_is_legal(stage):
    assert we.LIFECYCLE_DISPOSITIONS[stage].kind in we.DISPOSITION_KINDS


def test_user_stop_is_a_scoped_call_carrying_its_mode():
    """scoped-call is the claim that the worker CALLS the reducer's components in a
    narrowed mode. The mode field is where that narrowing is stated, and
    LifecycleDisposition refuses a scoped-call without one — so this asserts the
    disposition is the honest one, not merely a legal one."""
    row = we.LIFECYCLE_DISPOSITIONS["user-stop"]
    assert row.kind == we.SCOPED_CALL
    assert row.mode, "a scoped-call must state how it is narrowed"
    # The narrowing that matters: the agent-wide half is excluded BY NAME.
    assert "NEVER D1/D2/D3/D6/D7" in row.mode
    # And the row describes the stop that runs, not the park it replaced.
    assert "body-closing" in row.mode and "then park" not in row.mode


def test_park_resume_is_worker_only():
    """The reducer has no parked state at all, so this cannot be a scoped call."""
    row = we.LIFECYCLE_DISPOSITIONS["park-resume"]
    assert row.kind == we.WORKER_ONLY
    assert row.mode is None, "worker-only rows must not carry a mode"


# ──────────── (b) park semantics, on real files (the worker-loop park) ────────────

def test_active_worker_parks_and_resumes(tmp_path):
    pr = _mk_worker_body(tmp_path)
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == "active"

    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "parked"
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == "parked"

    assert bm.resume_body(SID_WORKER, "alpha", project_root=pr) == "resumed"
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == "active"


def test_park_is_idempotent(tmp_path):
    """A second park of an already-parked Body must be a no-op, not an error."""
    pr = _mk_worker_body(tmp_path)
    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "parked"
    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "already-parked"


@pytest.mark.parametrize("closed", ["closed-pending-merge", "merged", "closed-stale"])
def test_a_closed_body_is_never_parked(tmp_path, closed):
    """A close never becomes a park. This is the direction that would LOSE work: a
    Body already staged for merge, re-opened as parked, would diverge AFTER its
    snapshot was taken."""
    pr = _mk_worker_body(tmp_path)
    bm.set_state(SID_WORKER, "alpha", closed, project_root=pr)
    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "not-active"
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == closed


# ────────────── (c) THE AGENT-WIDE NEGATIVE — asserted on the FILES ──────────────

def test_park_does_not_touch_any_agent_wide_file(tmp_path):
    """Seeds all five agent-wide files with known bytes, parks and resumes, and
    asserts every one is byte-identical afterwards. Asserted on file DIGESTS rather
    than on prose, because a prose assertion cannot catch a future edit that adds a
    write. running-session-id keeps its REAL value: it is the field that makes this
    Body a worker, so preserving it is the substantive claim."""
    pr = _mk_worker_body(tmp_path)
    seeded = _seed_agent_wide(pr)
    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "parked"
    assert bm.resume_body(SID_WORKER, "alpha", project_root=pr) == "resumed"
    _assert_agent_wide_unchanged(pr, seeded)


@pytest.mark.parametrize("start_state", ["active", "parked"])
def test_stop_close_does_not_touch_any_agent_wide_file(tmp_path, start_state):
    """The most important assertion in this file, for the step the branch now ENDS on.

    The stop writes `body-closing`; the stop-hook hands it to close_body_on_genuine.
    Run that close on a seeded tmp root and require (1) the Body really closed and the
    sentinel was consumed, and (2) all five agent-wide files are byte-identical.
    `parked` is a start state because a Body parked by the worker loop can still be
    stopped by the user."""
    pr = _mk_worker_body(tmp_path)
    if start_state == "parked":
        assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "parked"
    seeded = _seed_agent_wide(pr)
    session_dir = pr / "agents" / "alpha" / "sessions" / SID_WORKER
    (session_dir / "body-closing").touch()

    assert bm.close_body_on_genuine(SID_WORKER, "alpha", project_root=pr) == "marked"
    assert (bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"]
            == "closed-pending-merge")
    assert not (session_dir / "body-closing").exists(), "the sentinel must be consumed"
    _assert_agent_wide_unchanged(pr, seeded)


def test_worker_branch_names_no_agent_wide_session_write():
    """Complementary to the file assertions above, and deliberately NOT a substitute
    for them: this one catches a newly-ADDED write that the sandbox does not exercise.

    The branch legitimately writes `sessions/<SID>/stop-requested` (SID-scoped, this
    Body, this box). The forbidden object is the AGENT-WIDE `session/stop-requested`,
    which stops the reducer wherever it runs. The two differ by one path segment,
    which is exactly why this is worth pinning."""
    offenders = [(name, line) for line in _command_lines()
                 for name in AGENT_WIDE_FILES if f"session/{name}" in line]
    assert not offenders, (
        f"worker /stop branch writes agent-wide session state: {offenders}")


# ─────────────── (d) the ordering claims this design rests on, in source ───────────

def test_phase_minus_0_stop_precedes_the_park_due_gate():
    """A wakeup armed before the stop (a park re-poll or the deadman net) must stand
    down, not run a unit. worker-loop reads the session-scoped stop signal BEFORE it
    consults the park orbit, and that is what makes it stand down (g-115-9461)."""
    text = WORKER_LOOP_SKILL.read_text(encoding="utf-8")
    stop_read = text.index("sessions/$MIND_SID/stop-requested")
    park_due = text.index("body-manifest.py park-due")
    assert stop_read < park_due, (
        "worker-loop Phase -0-stop no longer precedes the park-due gate; a stopped "
        "Body's armed wakeup would resume polling")


def test_stop_hook_runs_the_close_before_its_stop_requested_valve():
    """The stop arms BOTH sessions/<SID>/stop-requested and body-closing. The hook
    must take the genuine-close branch when the sentinel is present: if the
    stop-requested valve were checked first, the turn would end WITHOUT staging the
    WM, and the Body's learning would wait for the 24h stale-binding sweep again."""
    text = STOP_HOOK.read_text(encoding="utf-8")
    sentinel = text.index('_CLOSE_SENTINEL="$HOOK_AGENT_DIR/sessions/$HOOK_SID/body-closing"')
    close = text.index("close-body-on-genuine", sentinel)
    valve = text.index("ALLOW gate=worker-net-stop-requested-session")
    assert sentinel < close < valve


def test_body_closing_is_the_last_command_and_release_precedes_the_relay():
    """The order the rationale argues for, pinned on the branch's command lines:
    stop signal first, release before the learning relay, the relay before the
    commit, telemetry and the binding cleanup before the close (they used to run
    after a park, where parked-body-gate.py denied them), and body-closing LAST
    (the WM snapshot is taken when the turn ends, so nothing may write after it)."""
    lines = _command_lines()

    def pos(needle):
        hits = [i for i, ln in enumerate(lines) if needle in ln]
        assert hits, f"worker /stop branch no longer runs: {needle}"
        return hits[0]

    order = [pos("/stop-requested"), pos("body-claims-release.py"),
             pos("encode-session"), pos("session-summary-write.sh"),
             pos("iteration-commit.sh"), pos("iteration-push.sh"),
             pos("owncloud-flush.sh"), pos("write_close("),
             pos(".active-agent-"), pos("/body-closing")]
    assert order == sorted(order), f"worker /stop step order changed: {order}"
    assert pos("/body-closing") == len(lines) - 1, (
        "body-closing must be the LAST command: a write after it diverges after "
        "the WM was staged")


# ───────────────────────── the WIRING half (guard-1943) ──────────────────────────

@pytest.mark.parametrize("call", [
    "body-claims-release.py --agent",
    "encode-session` with args `--relay`",
    "session-summary-write.sh --sid",
    "iteration-commit.sh --goal-id worker-stop",
    "iteration-push.sh --push-worker-ref",
    "owncloud-flush.sh",
    "write_close(",
    "session-binding-write.sh --sid",
    'sessions/$MIND_SID/body-closing"',
])
def test_worker_branch_invokes_each_scoped_obligation(call):
    """A component that exists and is never called is the defect class this whole
    change is about: the per-Body heartbeat writer was fixed while nothing invoked
    it, and its own tests stayed green throughout (guard-1943). Assert the CALL
    SITE, not the component."""
    assert call in _worker_branch(), f"worker /stop branch no longer invokes: {call}"


def test_worker_branch_no_longer_parks():
    """The park was replaced, not supplemented. A park after the close would be
    refused anyway (closed is not active), but a park BEFORE it would re-arm the
    parked-body gate and deny every later step, which is the defect that left
    telemetry records open."""
    assert not [ln for ln in _command_lines() if "body-manifest.py park" in ln]


def test_release_call_as_written_is_accepted(monkeypatch):
    """guard-920: run the skill's LITERAL flags through the real parser. The summary
    step sat inert for five days behind a flag argparse rejected under `|| true`;
    a substring check cannot see that. release_all is stubbed so no daemon is hit."""
    branch = _worker_branch()
    idx = branch.index("body-claims-release.py")
    line = branch[branch.rindex("`", 0, idx) + 1:branch.index("\n", idx)]
    argv = shlex.split(line.split("||")[0])[3:]       # drop `py -3 <script>`
    argv = [{"$MIND_SID": SID_WORKER, "<agent-name>": "alpha"}.get(a, a) for a in argv]

    bcr = _load("body_claims_release_wiring", "body-claims-release.py")
    seen = {}
    monkeypatch.setattr(bcr, "release_all", lambda agent, sid, dry_run=False:
                        seen.update(agent=agent, sid=sid) or {"verdict": "nothing-held"})
    assert bcr.main(argv) == 0, f"body-claims-release rejects the skill's own call: {argv}"
    assert seen == {"agent": "alpha", "sid": SID_WORKER}


def test_summary_call_as_written_is_accepted_and_writes(tmp_path, monkeypatch):
    """The call-site assertion above passed for five days while this step was INERT:
    the branch passed `--reason worker-stop`, the writer's argparse `choices` did not
    contain it, argparse exited 2 before any write, and `|| true` hid the rc
    (g-306-477, measured live 2026-09-14 and re-probed 2026-09-16). A substring
    cannot see that. So run the LITERAL flags from the skill line against the real
    parser (guard-920), and require the FILE, because main() also returns 0 when it
    writes nothing (an absent session dir is a silent no-op)."""
    branch = _worker_branch()
    idx = branch.index("session-summary-write.sh")
    line = branch[idx:branch.index("\n", idx)]
    argv = shlex.split(line.split("||")[0].rstrip(" `"))[1:]
    argv = [{"$MIND_SID": SID_WORKER, "<agent-name>": "alpha"}.get(a, a) for a in argv]

    ssw = _load("session_summary_write", "session-summary-write.py")
    monkeypatch.setattr(ssw, "_project_root", lambda: tmp_path)
    session_dir = tmp_path / "agents" / "alpha" / "sessions" / SID_WORKER
    session_dir.mkdir(parents=True)
    try:
        rc = ssw.main(argv)
    except SystemExit as exc:  # argparse rejects a flag value by exiting 2
        rc = exc.code
    assert rc == 0, f"session-summary-write rejects the skill's own call: {argv}"
    summary = session_dir / "session-summary.yaml"
    assert summary.is_file(), f"the skill's call wrote no summary: {argv}"
    reason = argv[argv.index("--reason") + 1]
    assert f"ended_reason: {reason}\n" in summary.read_text(encoding="utf-8")


def test_push_goes_to_the_worker_ref_never_the_shared_branch():
    """Merging into main is reducer-only (worker-loop Phase 3.8). The old stop pushed
    main with the three rate limits zeroed, which made the stop the one place a
    worker wrote the shared branch. --push-worker-ref pushes HEAD to
    refs/workers/<agent>/<sid> before the rate limiter is consulted."""
    pushes = [ln for ln in _command_lines() if "iteration-push.sh" in ln]
    assert len(pushes) == 1, f"expected exactly one push call: {pushes}"
    assert "--push-worker-ref" in pushes[0]
    assert "--strict" not in pushes[0], (
        "--strict makes a transient network blip abort the stop; without it "
        "soft_exit returns 0 on every path (guard-775)")


def test_output_string_tells_the_operator_what_happened():
    """guard-4282: a prose comment and the string the operator READS are two
    artifacts, and this exact string has gone stale twice against the steps above
    it. It must say the Body CLOSED, where the commits went, the mode the session
    landed in, and that it never runs worker units again — the string before the
    close promised a resume `/start` refuses."""
    branch = _worker_branch()
    out_idx = branch.index("10. Output:")
    out = branch[out_idx:branch.index("\n", out_idx)]
    assert "CLOSED" in out and "PARKED" not in out
    assert "worker ref" in out, "the operator is not told where the commits went"
    assert "now in <target_mode> mode" in out, "the operator is not told where it landed"
    assert "never runs worker units again" in out, (
        "the operator is not told this SID is finished as a worker")
    assert "<step-2 verdict" in out, "the operator is not told what was released"


# ──────────── the landing (2026-10-07): the session ends in target_mode ────────────

def _step0_command() -> str:
    """Step 0's Bash command, from between its backticks."""
    branch = _worker_branch()
    start = branch.index("Bash: `", branch.index("0. Skip what an earlier stop")) + len("Bash: `")
    return branch[start:branch.index("`\n", start)]


def _run_step0(pr: Path) -> str:
    env = {**os.environ, "MIND_SID": SID_WORKER}
    r = subprocess.run([BASH, "-c", _step0_command().replace("<agent-name>", "alpha")],
                       cwd=pr, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _set_body_state(session_dir: Path, state: str) -> None:
    manifest = session_dir / "body-manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(re.sub(r"(?m)^body_state:.*$", f"body_state: {state}", text),
                        encoding="utf-8")


def test_landing_shares_the_last_call_and_cannot_block_the_close():
    """The landing is the FIRST half of the last call (rationale § Why the session
    lands): it precedes body-closing, and `;`, never `&&`, joins them, so a failed
    binding write still closes the Body."""
    last = _command_lines()[-1]
    assert "session-binding-write.sh" in last and "/body-closing" in last, last
    land = last.index("session-binding-write.sh")
    between = last[land:last.index("touch ", land)]
    assert "&&" not in between, "a failed landing would skip the close"
    assert "|| echo" in between and between.rstrip().endswith(";"), between


@pytest.mark.parametrize("target_mode", ["assistant", "reader"])
def test_landing_call_as_written_lands_the_session(tmp_path, monkeypatch, target_mode):
    """guard-920: the skill's LITERAL flags through the real writer, then the real
    predicate every consumer reads. Neither half alone proves the session lands.
    The agent-wide files are seeded and re-hashed: the landing is this session's
    own binding, never agent-mode."""
    pr = _mk_worker_body(tmp_path)
    (pr / "agents" / "alpha" / "local-paths.conf").write_text("", encoding="utf-8")
    seeded = _seed_agent_wide(pr)
    last = _command_lines()[-1]
    call = last[last.index("session-binding-write.sh"):].split("||")[0]
    argv = shlex.split(call.replace(">/dev/null", ""))[1:]       # drop the script
    argv = [{"$MIND_SID": SID_WORKER, "<agent-name>": "alpha",
             "<target_mode>": target_mode}.get(a, a) for a in argv]

    sbw = _load("session_binding_write_wiring", "session-binding-write.py")
    monkeypatch.setattr(sbw, "_project_root", lambda: pr)
    assert sbw.main(argv) == 0, f"the binding writer rejects the skill's own call: {argv}"
    from _session_binding import landed_mode_in
    assert landed_mode_in(pr / "agents" / "alpha" / "sessions" / SID_WORKER) == target_mode
    _assert_agent_wide_unchanged(pr, seeded)


def test_step0_skips_to_the_landing_only_for_a_stopped_body(tmp_path):
    """Step 0 routes a repeat /stop. Run as written through real bash: an open
    Body (active or parked) takes the whole stop; a Body in ANY closed state, from
    the real body-manifest set, skips to the landing; a landed session re-lands.
    Its `landed` check is a third copy of the landed rule, so it is compared with
    landed_mode_in on the same bindings."""
    from _session_binding import landed_mode_in
    pr = _mk_worker_body(tmp_path)
    session_dir = pr / "agents" / "alpha" / "sessions" / SID_WORKER
    assert _run_step0(pr) == "open"                  # no binding yet
    (session_dir / "binding.yaml").write_text("mode: autonomous\n", encoding="utf-8")
    assert _run_step0(pr) == "open"
    _set_body_state(session_dir, "parked")
    assert _run_step0(pr) == "open", "a parked Body is alive, not closed"
    for state in bm.CLOSED_STATES:
        _set_body_state(session_dir, state)
        assert _run_step0(pr) == "closed", state
    for raw in ("mode: assistant\n", "mode: reader\n", "mode: 'assistant'\n",
                "mode: assistants\n", "mode: autonomous\n"):
        (session_dir / "binding.yaml").write_text(raw, encoding="utf-8")
        assert (_run_step0(pr) == "landed") == (landed_mode_in(session_dir) is not None), raw
