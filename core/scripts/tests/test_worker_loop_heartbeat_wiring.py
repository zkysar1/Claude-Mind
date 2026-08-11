"""The worker loop must CALL the shared heartbeat writer — .

This file pins a CALL SITE, not a function, and that distinction is the whole
point. `test_body_heartbeat_writer.py` already proves heartbeat-tick.sh writes
both per-Body heartbeats correctly, and g-306-208 fixed its ordering so the body
write precedes the agent-state=IDLE refusal (a worker box is IDLE by design).
Both of those stayed GREEN while the capability was completely inert in
production, because `.claude/skills/worker-loop/SKILL.md` never invoked the
script at all.

Measured 2026-08-05 on cc-07 (live alpha worker, 17 min into an active unit):
neither `sessions/<SID>/body-heartbeat` nor the syncable
`session/body-heartbeat-<SID>.json` carrier existed. With no liveness signal,
`stranded-claim-sweep.py` (DEFAULT_FOREIGN_SID_GRACE_MINUTES = 120) pops any
foreign-SID claim held past the grace — so every worker unit running longer than
two hours was killed mid-execution. This is also the retro-cause of the
g-315-518 claim pop on 2026-08-04, which was closed against the writer.

guard-1943 names the shape: pinning the reader/writer says nothing about the
wiring. So the assertions here deliberately read the SKILL text rather than
exercising the script — a test that ran the writer would have passed throughout
the defect, which is exactly the failure being prevented.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKER_LOOP = REPO / ".claude" / "skills" / "worker-loop" / "SKILL.md"
REDUCER_LOOP = REPO / ".claude" / "skills" / "aspirations" / "SKILL.md"

# A Bash invocation of the shared writer, tolerant of `bash `/`py -3 ` prefixes
# and of path spelling, but NOT of a worker-local reimplementation.
_INVOCATION = re.compile(r"^\s*Bash:\s*bash\s+core/scripts/heartbeat-tick\.sh\s*$", re.M)


def _text(p):
    return p.read_text(encoding="utf-8")


def test_worker_loop_invokes_the_shared_heartbeat_writer():
    """The defect: zero invocations, so both per-Body heartbeats were never written."""
    body = _text(WORKER_LOOP)
    hits = _INVOCATION.findall(body)
    assert hits, (
        "worker-loop/SKILL.md contains NO `Bash: bash core/scripts/heartbeat-tick.sh` "
        "invocation. A cross-box worker then has no liveness signal at all and its "
        "claims are popped after stranded-claim-sweep's 120-minute foreign-SID grace. "
        "The writer's own tests cannot catch this — they exercise the script directly."
    )


def test_the_tick_runs_before_the_claim():
    """A heartbeat written only AFTER claiming leaves the claim unprotected in the
    window that matters, and a Body between units would report stale."""
    body = _text(WORKER_LOOP)
    m = _INVOCATION.search(body)
    assert m, "no heartbeat-tick invocation at all (see the previous test)"
    # Anchor inside the executable loop block. The prose sections above it
    # DISCUSS claiming, so an unanchored find() matches explanatory text and the
    # ordering assertion becomes meaningless (it compared against a header
    # mention on first write of this test).
    loop_at = body.find("## The loop")
    assert loop_at != -1, "could not locate the '## The loop' block"
    claim_at = body.find("aspirations-claim", loop_at)
    if claim_at == -1:
        claim_at = body.find("Phase 2", loop_at)
    assert claim_at != -1, "could not locate the claim step inside the loop block"
    assert m.start() < claim_at, (
        "heartbeat-tick.sh is invoked AFTER the claim step; it must run at the top "
        "of the cycle so a Body that is alive but between units still reports fresh"
    )


def test_no_worker_local_reimplementation_of_the_heartbeat():
    """No-transcription contract (guard-2676 / ): worker capabilities are
    scoped CALLS into the shared component, never transcriptions of its steps —
    a copy silently drifts the next time the component evolves."""
    body = _text(WORKER_LOOP)
    forbidden = (
        # hand-rolled equivalents of what heartbeat-tick.sh does internally
        "touch \"agents/$MIND_AGENT/sessions/$MIND_SID/body-heartbeat\"",
        "body-heartbeat-$MIND_SID.json",
    )
    for frag in forbidden:
        assert frag not in body, (
            f"worker-loop appears to reimplement the heartbeat inline ({frag!r}). "
            "Call heartbeat-tick.sh instead — one implementation, two entry scopes."
        )


_PULL = re.compile(r"^\s*Bash:\s*bash\s+core/scripts/iteration-push\.sh\s+--no-push\s*$", re.M)


def test_worker_loop_pulls_latest_framework():
    """: a worker never pulled, so it ran whatever code it had at launch.

    iteration-push.sh is the framework's ONLY fetch+merge, and its only caller is
    iteration-close.sh — which this loop skips by design. Measured 2026-08-06:
    cc-07 sat 112 / 30 / 51 commits behind on successive checks while the reducer
    never exceeded 2, so every fix shipped during a worker's life missed it.
    """
    body = _text(WORKER_LOOP)
    assert _PULL.search(body), (
        "worker-loop/SKILL.md has no `Bash: bash core/scripts/iteration-push.sh "
        "--no-push` call. Without it a worker NEVER pulls — not once in its "
        "lifetime — so no framework fix can reach a running worker."
    )


def test_the_pull_runs_before_the_claim():
    """A merge landing mid-unit could swap code under an executing goal. The pull
    belongs between units, at the top of the cycle."""
    body = _text(WORKER_LOOP)
    m = _PULL.search(body)
    assert m, "no pull invocation at all (see the previous test)"
    loop_at = body.find("## The loop")
    assert loop_at != -1, "could not locate the '## The loop' block"
    claim_at = body.find("aspirations-claim", loop_at)
    if claim_at == -1:
        claim_at = body.find("Phase 2", loop_at)
    assert claim_at != -1, "could not locate the claim step inside the loop block"
    assert m.start() < claim_at, (
        "the pull is invoked AFTER the claim step — a merge could then land "
        "under a goal that is already executing"
    )


def test_worker_pull_is_pull_only():
    """The worker must not push: the reducer owns the shared tree, and two Bodies
    of one agent pushing the same store files is contention the --no-push mode
    exists to avoid. Pins the FLAG, which is the entire difference from the
    reducer's call site."""
    body = _text(WORKER_LOOP)
    bare = re.search(r"^\s*Bash:\s*bash\s+core/scripts/iteration-push\.sh\s*$", body, re.M)
    assert bare is None, (
        "worker-loop invokes iteration-push.sh WITHOUT --no-push, so a worker "
        "would push the shared tree. That is the reducer's job."
    )


def test_reducer_loop_still_calls_it_too():
    """Both loops must reach the same writer; if the reducer's call site ever
    disappears this test says so before the fleet finds out the hard way."""
    assert "core/scripts/heartbeat-tick.sh" in _text(REDUCER_LOOP), (
        "the reducer loop lost its heartbeat-tick.sh invocation — the agent-wide "
        "runner-heartbeat and the DDB claim renewal both hang off that call"
    )
