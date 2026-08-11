"""The skill-dedup gate must not block `worker-loop` re-entry — .

WHAT BROKE: `context-reads-skill-gate.sh` exempts loop-orchestrator skills from
its exit-2 block, but the list was written when `aspirations` was the only loop
(`aspirations|aspirations-*`). The Mind/Body split then added a SECOND loop
orchestrator — `worker-loop`, whose Phase 5 re-enters via `Skill(worker-loop)`
every work unit, exactly as the reducer re-enters via `Skill(aspirations)`. It
was never added to the list, so every worker re-entry was blocked.

WHY IT STAYED INVISIBLE: g-304-20's blocked `Skill(aspirations)` KILLED the
loop, which is loud. A blocked `Skill(worker-loop)` does not — the model keeps
following the copy of the skill already in its context, so the worker iterates
along looking perfectly healthy while the SKILL.md on disk is never re-read
again. Every edit to worker-loop/SKILL.md was therefore unreachable by every
RUNNING worker; only a restart could deliver one.

MEASURED CONSEQUENCE (cc-07, 2026-08-06): a Phase -0.3 `iteration-push --no-push`
step committed at 03:09 had not fired once by 04:14, across four re-entries
visible in the execution diary (03:54, 04:04, 04:10, 04:14). That worker sat 22
commits behind with no path to self-heal.

THE RISK THIS FILE GUARDS: the fix WIDENS an exemption, and a widened exemption
can swallow the gate. `test_non_orchestrator_skill_is_still_blocked` is the
load-bearing negative — dedup must still work for ordinary skills, or the gate
has been deleted rather than corrected. It is a separate test on purpose, so a
future change cannot satisfy the worker case by exempting everything.

Verification shape mandated by guard-802: invoke the REAL hook twice with real
hook-stdin JSON and assert the second call does not return exit 2.

Hook contract: exit 0 = allow, exit 2 = block.

Daemon-safe (no subprocess daemon, no mind_api/state — pure gate decision).

Run:
  STORAGE_BACKEND=local python -m pytest \
    core/scripts/tests/test_skill_gate_worker_loop_exempt.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent                  # repo root
GATE_SH = CORE_SCRIPTS / "context-reads-skill-gate.sh"

sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import bash_cmd  # noqa: E402  guard-580: never bare "bash"

THROWAWAY = "_skill_gate_worker_exempt_throwaway_"
SID = "cccc3333-cccc-4ccc-8ccc-cccccccccccc"


@contextmanager
def _agent():
    """Throwaway agent under the real PROJECT_ROOT/agents/, always torn down.

    Mirrors test_context_reads_body_routing.py's pattern: the gate sources
    _paths.sh and derives PROJECT_ROOT from its own location, so a relocated
    tree would not exercise the production resolution path.
    """
    adir = PROJECT_ROOT / "agents" / THROWAWAY
    (adir / "session").mkdir(parents=True, exist_ok=True)
    # newline="" disables CRLF translation (guard-1688) — _paths.sh sources this.
    (adir / "local-paths.conf").write_text(
        "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
    )
    # Phase 2.6 binding so the gate's session-id -> agent resolver finds us
    # whether or not MIND_AGENT is already set in the environment.
    sess = adir / "sessions" / SID
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "binding.yaml").write_text(
        f"agent: {THROWAWAY}\nmode: autonomous\n"
        f"started_at: 2026-08-06T00:00:00\nstarted_by: test\n",
        encoding="utf-8",
    )
    legacy = PROJECT_ROOT / f".active-agent-{SID}"
    legacy.write_text(THROWAWAY, encoding="utf-8")
    try:
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY
        env["STORAGE_BACKEND"] = "local"   # guard-955
        yield env
    finally:
        shutil.rmtree(adir, ignore_errors=True)
        legacy.unlink(missing_ok=True)


def _invoke(env, skill):
    """Fire the hook exactly as Claude Code does: JSON on stdin."""
    payload = {"tool_name": "Skill",
               "tool_input": {"skill": skill},
               "session_id": SID}
    return subprocess.run(
        bash_cmd(str(GATE_SH)),
        input=json.dumps(payload), capture_output=True, text=True,
        timeout=180, cwd=str(PROJECT_ROOT), env=env,
    )


def _skill_exists(name):
    return (PROJECT_ROOT / ".claude" / "skills" / name / "SKILL.md").is_file()


def test_worker_loop_survives_re_entry():
    """THE DEFECT: `Skill(worker-loop)` is the Body's per-unit loop re-entry."""
    assert _skill_exists("worker-loop"), "worker-loop skill missing from repo"
    with _agent() as env:
        first = _invoke(env, "worker-loop")
        second = _invoke(env, "worker-loop")
    assert first.returncode != 2, f"first invocation blocked: {first.stderr[:300]}"
    assert second.returncode != 2, (
        "the gate BLOCKED a worker-loop re-entry. A worker re-enters via "
        "Skill(worker-loop) every work unit; blocking it means the running "
        "worker never re-reads worker-loop/SKILL.md, so framework edits to the "
        f"worker loop cannot reach it. stderr={second.stderr[:300]}"
    )


def test_non_orchestrator_skill_is_still_blocked():
    """LOAD-BEARING NEGATIVE: widening the exemption must not delete the gate.

    An ordinary skill must still be deduped on re-invocation, or the fix has
    turned a targeted exemption into a blanket allow.
    """
    if not _skill_exists("respond"):
        pytest.skip("no non-orchestrator skill available to use as control")
    with _agent() as env:
        first = _invoke(env, "respond")
        second = _invoke(env, "respond")
    assert first.returncode == 0, f"first invocation not allowed: {first.stderr[:300]}"
    assert second.returncode == 2, (
        "an ordinary skill was NOT deduped on re-invocation — the exemption "
        "has widened into a hole and the gate no longer dedups anything. "
        f"rc={second.returncode} stderr={second.stderr[:300]}"
    )


def test_aspirations_exemption_unregressed():
    """The original  exemption must survive the edit."""
    assert _skill_exists("aspirations"), "aspirations skill missing from repo"
    with _agent() as env:
        first = _invoke(env, "aspirations")
        second = _invoke(env, "aspirations")
    assert first.returncode != 2
    assert second.returncode != 2, (
        "the reducer's own loop re-entry regressed — this is the g-304-20 "
        f"loop-death shape. stderr={second.stderr[:300]}"
    )
