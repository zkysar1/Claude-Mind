""" — the worker /stop branch: park, the SID-scoped obligations, and
the agent-wide negative.

WHY THIS FILE EXISTS
A user /stop on a worker Body was an UNDECLARED lifecycle stage. `worker_execute.py`
carries LIFECYCLE_DISPOSITIONS precisely so an undeclared stage fails at import
rather than by surprise, and it declared 16 stages with neither the user-stop path
nor the parked state among them — so the one instrument built to catch worker
lifecycle asymmetry was blind to the two states this change is about. The branch
itself ran two of the five box/SID-scoped obligations the reducer's graceful stop
performs, and left the Body `active`, which means its learning payload stages NEVER
if nobody restarts it.

WHAT THIS SUITE PINS, AND WHY THE HALVES ARE SEPARATE
Some assertions below are FILE assertions and some are SOURCE assertions, and the
split is deliberate rather than convenient:

  * park/resume semantics and the agent-wide negative are asserted on REAL FILES in
    a tmp project root. A prose assertion cannot catch a future edit that adds a
    write — which is the whole point of the agent-wide negative, the most important
    assertion here.
  * the two ORDERING claims (worker-loop Phase -0-stop ahead of the park-due gate;
    stop-hook's parked ALLOW gate) are claims ABOUT SOURCE, so source is the only
    place they can be checked. They are asserted against the real files rather than
    restated from the goal text, because the design rests on them: if either is
    false, parking a stopped Body would resume its poll or trap its turn-end.
  * the WIRING assertions are separate from both. guard-1943: pinning a writer says
    nothing about whether anything calls it, and this change's entire defect class
    was a correct component nothing invoked.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

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


def _mk_worker_body(tmp_path: Path, agent: str = "alpha") -> Path:
    """A tmp project root holding a REDUCER sid on disk and a forked WORKER Body.

    write_manifest(role="worker") materializes the forked body-WM itself whenever a
    running-session-id names a DIFFERENT sid — that fork file is what park_body
    requires, so seeding the reducer sid is what makes this a worker at all.
    """
    state = tmp_path / "agents" / agent / "session"
    state.mkdir(parents=True, exist_ok=True)
    (state / "running-session-id").write_text(SID_REDUCER, encoding="utf-8")
    (state / "working-memory.yaml").write_bytes(b"slots:\n  scratch: seeded\n")
    bm.write_manifest(SID_WORKER, agent, role="worker", project_root=tmp_path)
    return tmp_path


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


def test_park_resume_is_worker_only():
    """The reducer has no parked state at all, so this cannot be a scoped call."""
    row = we.LIFECYCLE_DISPOSITIONS["park-resume"]
    assert row.kind == we.WORKER_ONLY
    assert row.mode is None, "worker-only rows must not carry a mode"


# ───────────────────────── (b) park semantics, on real files ─────────────────────

def test_active_worker_parks_and_resumes(tmp_path):
    pr = _mk_worker_body(tmp_path)
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == "active"

    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "parked"
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == "parked"

    assert bm.resume_body(SID_WORKER, "alpha", project_root=pr) == "resumed"
    assert bm.read_manifest(SID_WORKER, "alpha", project_root=pr)["body_state"] == "active"


def test_park_is_idempotent(tmp_path):
    """A second /stop on an already-parked Body must be a no-op, not an error — the
    branch treats every non-'parked' return as a no-op and continues."""
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
    """The most important assertion in this file.

    Seeds all five agent-wide files with known bytes, runs the one step this change
    ADDS that mutates Body state, and asserts every one is byte-identical
    afterwards. Asserted on file DIGESTS rather than on the skill's prose, because a
    prose assertion cannot catch a future edit that adds a write.

    running-session-id is seeded with its REAL value rather than a synthetic one: it
    is the field that makes this Body a worker, so preserving it is the substantive
    claim, not just an unchanged-bytes coincidence.
    """
    pr = _mk_worker_body(tmp_path)
    state = pr / "agents" / "alpha" / "session"
    seeded = {}
    for name in AGENT_WIDE_FILES:
        if name != "running-session-id":     # already holds the reducer sid
            (state / name).write_text(f"SEED-{name}\n", encoding="utf-8")
        seeded[name] = hashlib.sha256((state / name).read_bytes()).hexdigest()

    assert bm.park_body(SID_WORKER, "alpha", project_root=pr) == "parked"
    assert bm.resume_body(SID_WORKER, "alpha", project_root=pr) == "resumed"

    for name, digest in seeded.items():
        after = hashlib.sha256((state / name).read_bytes()).hexdigest()
        assert after == digest, (
            f"worker /stop mutated the AGENT-WIDE file {name!r}. On a cross-box "
            "fleet the reducer owning it may be on another machine, so this stops "
            "the wrong Body while the user believes they stopped only this box.")


def test_worker_branch_names_no_agent_wide_session_write():
    """Complementary to the file assertion above, and deliberately NOT a substitute
    for it: this one catches a newly-ADDED write that the sandbox does not exercise,
    while the file assertion catches a behavioural change in a component the branch
    already calls. Neither implies the other.

    The branch legitimately writes `sessions/<SID>/stop-requested` (SID-scoped, this
    Body, this box). The forbidden object is the AGENT-WIDE `session/stop-requested`,
    which stops the reducer wherever it runs. The two differ by one path segment,
    which is exactly why this is worth pinning — and the branch's own prose DISCUSSES
    that distinction, so only `Bash:` command lines are inspected. A prose mention is
    documentation; a command is a write.
    """
    offenders = []
    for line in _worker_branch().splitlines():
        stripped = line.strip()
        if not stripped.startswith("Bash:"):
            continue
        for name in AGENT_WIDE_FILES:
            if f"session/{name}" in stripped:
                offenders.append((name, stripped))
    assert not offenders, (
        f"worker /stop branch writes agent-wide session state: {offenders}")


# ───────── (d)+(e) the two ordering claims this design rests on, in source ────────

def test_phase_minus_0_stop_precedes_the_park_due_gate():
    """The design claim: parking a STOPPED Body cannot restart its polling, because
    worker-loop reads the session-scoped stop signal BEFORE it consults the park
    orbit. If this inverts, park_body's orbit advance would schedule a re-poll for a
    Body the user stopped — and the remedy would be a park variant that does not
    advance the orbit, NOT a reorder of Phase -0-stop, which is load-bearing for a
    different reason (g-115-9461).
    """
    text = WORKER_LOOP_SKILL.read_text(encoding="utf-8")
    stop_read = text.index("sessions/$MIND_SID/stop-requested")
    park_due = text.index("body-manifest.py park-due")
    assert stop_read < park_due, (
        "worker-loop Phase -0-stop no longer precedes the park-due gate; a parked+"
        "stopped Body would resume polling")


def test_stop_hook_allows_a_parked_body_at_turn_end():
    """Without this ALLOW gate the stop's own turn-end is BLOCKed, and the only
    escape is hand-writing `body-closing`, which DURABLY retires the Body. Stopping
    one box is not retiring that Body."""
    assert "worker-net-body-parked" in STOP_HOOK.read_text(encoding="utf-8")


# ───────────────────────── the WIRING half (guard-1943) ──────────────────────────

@pytest.mark.parametrize("call", [
    "session-summary-write.sh --sid",
    "iteration-commit.sh --goal-id worker-stop",
    "iteration-push.sh --min-commits 0 --max-age-min 0 --fetch-interval-min 0",
    "owncloud-flush.sh",
    "body-manifest.py park --sid",
])
def test_worker_branch_invokes_each_scoped_obligation(call):
    """A component that exists and is never called is the defect class this whole
    change is about: the per-Body heartbeat writer was fixed while nothing invoked
    it, and its own tests stayed green throughout (guard-1943). Assert the CALL
    SITE, not the component."""
    assert call in _worker_branch(), f"worker /stop branch no longer invokes: {call}"


def test_summary_call_as_written_is_accepted_and_writes(tmp_path, monkeypatch):
    """The call-site assertion above passed for five days while this step was INERT:
    the branch passed `--reason worker-stop`, the writer's argparse `choices` did not
    contain it, argparse exited 2 before any write, and `|| true` hid the rc
    (g-306-477, measured live 2026-09-14 and re-probed 2026-09-16). A substring
    cannot see that. So run the LITERAL flags from the skill line against the real
    parser (guard-920), and require the FILE, because main() also returns 0 when it
    writes nothing (an absent session dir is a silent no-op)."""
    import shlex

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


def test_push_call_keeps_all_three_rate_limits_zeroed():
    """The three zeroes are one semantic unit — they convert a rate-limited batch
    decision into 'push whatever is ahead, now'. Dropping any one silently restores
    the stranding this step exists to prevent, and a stopped worker has no later
    iteration to recover it."""
    branch = _worker_branch()
    idx = branch.index("iteration-push.sh")
    line = branch[idx:branch.index("\n", idx)]
    for flag in ("--min-commits 0", "--max-age-min 0", "--fetch-interval-min 0"):
        assert flag in line, f"worker /stop push lost {flag}: {line!r}"
    assert "--strict" not in line, (
        "--strict makes a transient network blip abort the stop; without it "
        "soft_exit returns 0 on every path (guard-775)")


def test_output_string_tells_the_operator_what_is_durable():
    """guard-4282: a prose comment and the string the operator READS are two
    artifacts, and this exact string has already gone stale once against the block
    directly above it. Steps 3-4 now push the git-tracked half, so an unqualified
    'local disk only' would be a false report to the operator on every worker stop.
    """
    branch = _worker_branch()
    out_idx = branch.index("9. Output:")
    out = branch[out_idx:branch.index("\n", out_idx)]
    assert "PARKED" in out, "the operator is not told the Body is parked"
    assert "committed and pushed" in out, (
        "the operator is not told the git-tracked half is now durable off-box")
    assert "sessions/<SID>/" in out, (
        "the operator is not told WHICH half remains local-only")
