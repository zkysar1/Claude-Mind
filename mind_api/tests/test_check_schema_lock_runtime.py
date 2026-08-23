"""test_check_schema_lock_runtime.py — , the RUNTIME half.

The goal's first outcome asks that no shell command execute while the world
`aspirations.jsonl` lock is held on the verification-edit path, "demonstrated by
a timing probe, not by inspection."

`core/scripts/tests/test_check_schema_lock_scope.py` pins the same property by
INSPECTION — it parses `update_goal` and asserts the two `_check_schema_eval`
call sites are not lexically nested inside a `with file_locks.locked(...)`.
That guard is good and was itself mutation-proven, but its own docstring
concedes the limit: it "cannot prove absence of shell-out under lock, only that
the call sites sit ahead of the critical section." A static guard says where the
CALL is written. It cannot say what actually executes while the lock is open —
by any path, including ones nobody thought to grep for.

This file closes that gap by RUNNING the production `update_goal` and observing
the two events directly.

WHY LOCK DEPTH RATHER THAN ELAPSED TIME, since the outcome says "timing probe".
A timing probe infers the property from a duration: hold the lock, run a slow
command, see whether a concurrent writer stalls. That is weaker than it sounds
in both directions — it passes by luck whenever the command happens to be fast,
and it fails spuriously whenever the box is loaded. Recording the lock depth at
the moment `subprocess.run` is entered measures the property ITSELF rather than
a symptom of it, is exact, and is hermetic. Stated plainly because it is a
deliberate departure from the outcome's literal wording, not an oversight.

THE POSITIVE CONTROL IS THE LOAD-BEARING HALF (guard-4166). The assertion here
is that something STOPS HAPPENING — no subprocess under the lock — and "no
subprocess under the lock" is exactly what a completely inert test also
produces. The sibling inspection file deliberately uses schema-INVALID checks so
that nothing ever shells out; reusing that fixture here would make this test
pass forever while proving nothing. So the check below is schema-VALID and
allowlisted, and the test asserts `subprocess.run` was reached AT LEAST ONCE
before asserting where. Both halves were verified against the pre-fix source:

    pre-fix  e85ba0c31 : subprocess calls 1, depth at call 1   <- defect visible
    fixed    origin/main: subprocess calls 1, depth at call 0

The control does not flip between those runs — only the depth does. That
asymmetry is the evidence that this test discriminates.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_MIND_API = pathlib.Path(__file__).resolve().parents[1]
_REPO = _MIND_API.parent
for _p in (str(_MIND_API), str(_REPO / "core" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.endpoints import aspirations_write as AW  # noqa: E402
import predicate  # noqa: E402

GOAL_ID = "g-999-01"

# Schema-VALID and allowlist-passing, so evaluation really reaches
# `subprocess.run`. `bash core/scripts/` is one of predicate's
# ALLOWED_COMMAND_PREFIXES; the script need not exist, because the allowlist
# check is a string-prefix test and the call itself is stubbed below. Nothing
# executes in this test — the stub records the call and returns success.
SHELLING_CHECK = {
    "type": "command_succeeds",
    "command": "bash core/scripts/does-not-need-to-exist.sh",
    "timeout_seconds": 5,
}


class _Recorder:
    """Counts `subprocess.run` entries and the lock depth at each one."""

    def __init__(self):
        self.depth = 0
        self.calls = []
        self.lock_entries = 0   # cumulative; depth alone cannot prove the wrapper ran

    def wrap_locked(self, real_locked):
        recorder = self

        class _Tracked:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                recorder.depth += 1
                recorder.lock_entries += 1
                return self._inner.__enter__()

            def __exit__(self, *exc):
                recorder.depth -= 1
                return self._inner.__exit__(*exc)

        def _locked(*a, **k):
            return _Tracked(real_locked(*a, **k))

        return _locked

    def fake_run(self, *a, **k):
        self.calls.append(self.depth)

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()


class _StubPaths:
    agent_name = "test-agent"

    def __init__(self, world, meta, project_root):
        self.world = world
        self.meta = meta
        self.project_root = project_root
        self.agent = world.parent / "agent"


class _StubCtx:
    def __init__(self, paths, value):
        self.query = {"id": GOAL_ID, "field": "verification", "source": "world"}
        self.body = json.dumps(value).encode()
        self.headers = {}
        self.paths = paths


@pytest.fixture
def world(tmp_path):
    """A throwaway world carrying one goal for `update_goal` to edit."""
    w = tmp_path / "world"
    w.mkdir()
    (tmp_path / "meta").mkdir()
    asp = {
        "id": "asp-999", "title": "probe", "status": "active",
        "goals": [{
            "id": GOAL_ID, "title": "probe goal", "status": "pending",
            "priority": "HIGH", "participants": ["agent"],
            "verification": {"outcomes": ["x"], "checks": []},
        }],
    }
    (w / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
    return w


@pytest.fixture
def recorder(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(AW.file_locks, "locked", r.wrap_locked(AW.file_locks.locked))
    monkeypatch.setattr(predicate.subprocess, "run", r.fake_run)
    return r


def _edit_verification(world, recorder):
    paths = _StubPaths(world, world.parent / "meta", _REPO)
    value = {"outcomes": ["x"], "checks": [SHELLING_CHECK]}
    return AW.update_goal(_StubCtx(paths, value))


# ── the positive control ──────────────────────────────────────────────────

def test_the_edit_really_shells_out(world, recorder):
    """POSITIVE CONTROL — without this the depth assertion is unfalsifiable.

    If the fixture ever stops reaching `subprocess.run` (a tightened allowlist,
    a schema change that makes the check invalid, an early return added ahead of
    evaluation), then "nothing ran under the lock" becomes trivially true and
    the test below would pass against ANY source, including the pre-fix code it
    exists to reject.
    """
    resp = _edit_verification(world, recorder)

    assert getattr(resp, "status", None) == 200, (
        f"the verification edit itself must succeed; got {getattr(resp, 'body', b'')!r}")
    assert recorder.calls, (
        "check evaluation never reached subprocess.run — the depth assertion in "
        "the companion test is now vacuous. Fix the fixture before trusting it.")


# ── the property the goal actually asks for ───────────────────────────────

def test_no_subprocess_runs_while_the_aspirations_lock_is_held(world, recorder):
    """RUNTIME, not inspection: observe the lock depth as the shell-out happens.

    Measured against the pre-fix source (e85ba0c31) this assertion FAILS with
    depth 1 while the positive control above still passes — the discrimination
    that makes it a real guard rather than a restatement.
    """
    _edit_verification(world, recorder)

    under_lock = [d for d in recorder.calls if d > 0]
    assert not under_lock, (
        f"{len(under_lock)} subprocess call(s) executed while the world "
        f"aspirations.jsonl lock was held (depths {under_lock}). predicate.py "
        "shells out at five sites — one a network call to GitHub — and holding "
        "the lock across that blocks every agent's goal write fleet-wide for up "
        "to 240s. See g-115-5357."
    )


def test_the_lock_instrument_is_actually_wired(world, recorder):
    """ANTI-VACUITY for the depth instrument itself.

    The depth assertion above reads "no call had depth > 0". That is ALSO what a
    completely unwired instrument reports, because an unwired counter never
    leaves 0 — so the guard would pass on the pre-fix source for the wrong
    reason. Counting cumulative ENTRIES is what distinguishes "the lock was
    taken and we watched it" from "we watched nothing":

      * `lock_entries > 0` — `update_goal` really did take the lock through the
        wrapped `locked`, so the depth readings describe the real critical
        section.
      * `depth == 0` at the end — enter/exit are balanced, so a reading of 0
        during the run means genuinely outside, not a leaked decrement.

    The first draft of this test asserted only the second condition and claimed
    in its own docstring to prove the first. It did not, and would have passed
    with the wrapper deleted.
    """
    _edit_verification(world, recorder)

    assert recorder.lock_entries > 0, (
        "update_goal never entered the wrapped lock — the depth readings in the "
        "companion test describe nothing and cannot fail")
    assert recorder.depth == 0, "lock tracking is unbalanced — the instrument is wrong"
