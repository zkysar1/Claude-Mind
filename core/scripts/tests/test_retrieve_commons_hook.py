"""test_retrieve_commons_hook.py — behavioral pins for the commons-retrieval
hook that g-335-667 moved INTO core/scripts/retrieve.sh.

WHY BEHAVIORAL AND NOT STRUCTURAL. The defect these pins exist for was twice
invisible to a code read. The `commons-retrieval` Pattern B slot shipped
2026-07-26 with the core Step 4a step reading only "Follow each Step in the
convention" — prose. g-335-666 replaced that with a literal `Bash:` line in
core/config/execute-protocol-digest.md, and the fire rate stayed PARTIAL:
cc-02 logged 2 producer invocations while another box logged 0 across three
Phase-4 executions. Both forms read as correct on the page; prose and a
`Bash:` line in a loaded digest are the SAME enforcement class, because both
need the model to elect to run them. So a test that greps the digest, or
asserts the wiring shape, would have passed for BOTH broken versions. These
tests run the real wrapper against a stub producer and assert the producer
was actually invoked (guard-1740: assert the OUTPUT STORE, not the exit code).

Pins, in the order they matter:
  1. fires  — goal-scoped retrieval invokes the producer with the args the
              convention specifies (--goal-id/--category/--title/--draw-top).
  2. title  — --goal-title reaches the producer. Its query token set is
              tokens(category)|tokens(title), so a dropped title silently
              NARROWS every commons match rather than failing loudly.
  3. stdout — the producer's verdict must not pollute the wrapper's stdout,
              which callers parse as JSON.
  4. read-only — observer sessions (reader/assistant auto-inject --read-only)
              must not write the ledger/manifest.
  5. no-goal — non-goal-scoped retrievals must not draw.
  6. absent producer — must emit a diagnostic, NOT go silent. "No producer on
              this box" and "ran and drew nothing" are different facts, and
              the digest's old `|| true` rendered them identically (guard-2352);
              under own-cloud a `test -f` miss can also mean the file was never
              materialized into this box's read-through cache (guard-980).

Pure stdlib + the shared DaemonFixture — never touches the live world.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent   # tests -> scripts -> core -> repo root
RETRIEVE = PROJECT_ROOT / "core" / "scripts" / "retrieve.sh"
assert RETRIEVE.is_file(), "wrapper under test not found at %s" % RETRIEVE

CATEGORY = "test-commons-hook-cat"
GOAL_ID = "g-999-01"
GOAL_TITLE = "distinctive commons hook title token"

# The stub records argv and deliberately writes to STDOUT, so pin 3 has
# something to catch. A real producer prints its verdict the same way.
STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$COMMONS_HOOK_ARGV_LOG"
echo "commons verdict: ok listed=1 drawn=0"
exit 0
"""


def _seed_world(tmp: Path, with_producer: bool) -> Path:
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)
    (world / "scripts").mkdir(parents=True, exist_ok=True)
    if with_producer:
        hook = world / "scripts" / "commons-retrieve.sh"
        hook.write_text(STUB, encoding="utf-8")
        hook.chmod(0o755)
    return world


def _run(df, world: Path, argv_log: Path, extra: list) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["RT_DIR"] = str(df.runtime_dir)
    env["MIND_WORLD"] = str(world)
    env["STORAGE_BACKEND"] = "local"   # guard-955
    env["COMMONS_HOOK_ARGV_LOG"] = str(argv_log)
    # Pin the SESSION MODE the wrapper reads, not just the world. retrieve.sh
    # auto-injects --read-only when the bound agent's session/agent-mode is
    # reader/assistant (rt_session_mode), and the commons hook never draws
    # under --read-only (pin 4). Without this pin the wrapper read the LIVE
    # agents/alpha/session/agent-mode of whatever box ran the suite: pins 1-3
    # and 6 were green on an autonomous box and red on an assistant/reader one
    # for the same tree (4x red on cc-09 assistant, 2026-08-16 — the class
    # guard-2015 names: env-dependence reproduces cross-platform, so it was
    # filed as a portability red). MIND_AGENT_DIR is _paths.sh's test-only
    # agent-dir override (); rt_session_mode honors it via $AGENT_DIR.
    fixture_agent_dir = df.project_root / "agents" / df.agent
    (fixture_agent_dir / "session").mkdir(parents=True, exist_ok=True)
    (fixture_agent_dir / "session" / "agent-mode").write_text("autonomous\n",
                                                              encoding="utf-8")
    env["MIND_AGENT_DIR"] = str(fixture_agent_dir)
    return subprocess.run(
        [BASH, str(RETRIEVE), "--category", CATEGORY, "--supplementary-only"] + extra,
        capture_output=True, text=True, timeout=180, env=env, cwd=str(PROJECT_ROOT),
    )


def _case(extra: list, with_producer: bool = True):
    """Run the wrapper once; return (proc, argv_log_lines)."""
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = _seed_world(tmp, with_producer)
        argv_log = tmp / "argv.log"
        with DaemonFixture(world) as df:
            proc = _run(df, world, argv_log, extra)
        lines = (argv_log.read_text(encoding="utf-8").splitlines()
                 if argv_log.exists() else [])
        return proc, lines


def test_hook_fires_for_goal_scoped_retrieval():
    """Pin 1 — the producer is actually INVOKED. This is the whole point: the
    two prior versions of this wiring both looked right and did not fire."""
    proc, lines = _case(["--goal", GOAL_ID, "--goal-title", GOAL_TITLE])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert len(lines) == 1, (
        "producer invoked %d times, expected exactly 1 "
        "(0 = the g-335-666 regression; >1 = a restored digest line double-firing). "
        "stderr=%s" % (len(lines), proc.stderr[-1500:])
    )
    argv = lines[0]
    assert "--goal-id %s" % GOAL_ID in argv, argv
    assert "--category %s" % CATEGORY in argv, argv
    assert "--draw-top 2" in argv, argv


def test_goal_title_reaches_the_producer():
    """Pin 2 — a dropped title does not fail, it silently narrows matching."""
    _, lines = _case(["--goal", GOAL_ID, "--goal-title", GOAL_TITLE])
    assert len(lines) == 1
    assert "--title %s" % GOAL_TITLE in lines[0], lines[0]


def test_producer_output_does_not_pollute_stdout():
    """Pin 3 — callers parse the wrapper's stdout as JSON. The stub prints its
    verdict on stdout; the wrapper must redirect it to stderr."""
    proc, lines = _case(["--goal", GOAL_ID, "--goal-title", GOAL_TITLE])
    assert len(lines) == 1, "producer did not fire; pin 3 would be vacuous"
    assert "commons verdict" not in proc.stdout, (
        "producer stdout leaked into the wrapper's stdout: %r" % proc.stdout[-500:])
    assert "commons verdict" in proc.stderr, (
        "verdict must stay VISIBLE on stderr (Step 4a reports it), got: %r"
        % proc.stderr[-500:])
    json.loads(proc.stdout)   # must still parse


def test_read_only_does_not_draw():
    """Pin 4 — observer safety. reader/assistant mode auto-injects --read-only,
    and a draw writes both the ledger and the manifest."""
    proc, lines = _case(["--goal", GOAL_ID, "--goal-title", GOAL_TITLE, "--read-only"])
    # rc MUST be asserted first: an empty argv log is also what a wrapper that
    # never ran produces, so without this the pin passes vacuously. It did
    # exactly that on first run here (rc=127 from a bad path constant).
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert lines == [], "producer ran under --read-only: %r" % lines


def test_no_goal_does_not_draw():
    """Pin 5 — the commons draw is goal-scoped enrichment."""
    proc, lines = _case([])
    assert proc.returncode == 0, proc.stderr[-2000:]   # see note in pin 4
    assert lines == [], "producer ran without --goal: %r" % lines


def test_absent_producer_emits_diagnostic_not_silence():
    """Pin 6 — the failure this whole goal is about must not be silent. A world
    with no commons producer is a legitimate state (fresh world, or one that
    shares nothing), but it must be DISTINGUISHABLE from a producer that ran
    and drew nothing."""
    proc, lines = _case(["--goal", GOAL_ID, "--goal-title", GOAL_TITLE],
                        with_producer=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert lines == []
    assert "commons-retrieval" in proc.stderr and "no producer" in proc.stderr, (
        "absent producer went silent — that is the g-335-666 failure shape. "
        "stderr=%r" % proc.stderr[-800:])


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
