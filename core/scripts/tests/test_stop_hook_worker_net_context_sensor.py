"""The worker-net BLOCK carries the context sensor line and the park, not a close ().

WHAT THIS PINS
--------------
(1) The worker-net BLOCK reason ends with the context-budget banner line --
    guard-6380's falsifier for a harness ``<total_tokens>`` marker reading 0.
    The reducer's BLOCK has carried it since 2026-09-15; the worker-net branch
    exits before that block, so a worker Body never saw it (cc-09 2026-09-25/26:
    ~145 worker-net BLOCKs answered with sleeps). The line is produced by the
    SAME scoped call the reducer uses and passed VERBATIM (guard-2676): the
    assertions compare against the copied banner script's own output, never a
    re-typed shape.
(2) The reason no longer says "write the body-closing sentinel" for "no more
    work". No eligible goal is a PARK (worker-loop Phase 1, resumable); the
    only close is an expired park, which Phase 1 takes itself.
(3) Fail-open: a failing, missing, or hostile banner never changes the BLOCK,
    and the payload stays valid JSON.

HARNESS REUSE from test_stop_hook_in_flight_integration: the same tmp
PROJECT_ROOT, daemon fixture and production shape -- no running-session-id on a
worker box (g-306-214) and MIND_SID / MIND_AGENT scrubbed (guard-1742).
``_drive`` builds its root at ``<tmp>/hookroot`` before calling ``mutate``, so
``mutate`` is where a test stages the sensor file or swaps the banner script;
``_root_of`` asserts that location after every run.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

from test_stop_hook_in_flight_integration import (  # noqa: E402
    AGENT,
    BASH,
    _blocked,
    _drive,
    _hook_log,
)

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
BANNER = "context-budget-banner.sh"

# Distinct from guard-6380's incident values (the reducer twin's fixture), so a
# match here can only have come from THIS file's sensor.
SENSOR = {
    "used_pct": 41, "pct_to_autocompact": 73.4, "zone": "normal",
    "headroom_tokens": 123456, "updated_at": "2026-09-27T17:00:00",
    "env_seen": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000",
                 "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"},
}

REASON_TAIL = "The ONLY close is an EXPIRED park, which Phase 1 takes itself."

# The one expansion that appends the sensor line. The mutation control removes
# exactly this, so it and the positive test cannot drift apart.
APPEND = '"${_WN_CTX:+ $_WN_CTX}"'


def _root_of(tmp_path) -> pathlib.Path:
    root = tmp_path / "hookroot"
    assert (root / "core" / "scripts" / "stop-hook.sh").is_file(), (
        "_drive no longer builds its root at <tmp>/hookroot; the staging below "
        "wrote to the wrong place")
    return root


def _stage(tmp_path, *, sensor=None, banner_src=None, drop_banner=False,
           edit=None):
    """A `mutate` for _drive that stages files in the root it just built."""
    def mutate(src: str) -> str:
        root = tmp_path / "hookroot"
        if sensor is not None:
            (root / "agents" / AGENT / "session" / "context-budget.json"
             ).write_text(json.dumps(sensor), encoding="utf-8")
        banner = root / "core" / "scripts" / BANNER
        if drop_banner:
            banner.unlink()
        elif banner_src is not None:
            banner.write_text(banner_src, encoding="utf-8")
        return edit(src) if edit else src
    return mutate


def _worker_block(tmp_path, **stage):
    """One worker-net turn-end in the production shape; returns (proc, root)."""
    proc, _shard, _ = _drive(tmp_path, closing=False, runner_file=False,
                             scrub_env=True, mutate=_stage(tmp_path, **stage))
    return proc, _root_of(tmp_path)


def _payload(proc) -> dict:
    """The decision line, which MUST parse -- a skipped unparseable line would
    hide exactly the escaping defect (3) exists to catch."""
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines()
             if '"decision"' in ln]
    assert len(lines) == 1, f"expected one decision line: {proc.stdout!r}"
    return json.loads(lines[0])


def _banner_line(root) -> str:
    """What the copied banner prints for this root, run the way the hook runs it."""
    env = os.environ.copy()
    env.pop("MIND_SID", None)
    env["MIND_AGENT"] = AGENT
    env["STORAGE_BACKEND"] = "local"
    out = subprocess.run([BASH, str(root / "core" / "scripts" / BANNER)],
                         capture_output=True, text=True, timeout=60, env=env)
    return out.stdout.strip()


def _assert_worker_net_block(proc, root):
    assert proc.returncode == 0, f"hook must fail-open: {proc.stderr[-2000:]}"
    assert _blocked(proc), f"not a BLOCK:\n{proc.stdout}\n{_hook_log(root)}"
    assert "gate=worker-net " in _hook_log(root), _hook_log(root)


# ---------------------------------------------------------------- (1) banner

def test_worker_net_reason_ends_with_the_banner_line_verbatim(tmp_path):
    proc, root = _worker_block(tmp_path, sensor=SENSOR)
    _assert_worker_net_block(proc, root)
    banner = _banner_line(root)
    assert banner.startswith("CTX: raw ") and "to-compact 123,456 tokens" in banner, (
        f"the fixture sensor did not reach the banner: {banner!r}")
    reason = _payload(proc)["reason"]
    assert reason.endswith(REASON_TAIL + " " + banner), reason[-500:]


def test_worker_net_reason_carries_the_degradation_line_without_a_sensor(tmp_path):
    """No context-budget.json: the banner's own 'unavailable' line rides along,
    exactly as it does on the reducer's BLOCK."""
    proc, root = _worker_block(tmp_path)
    _assert_worker_net_block(proc, root)
    banner = _banner_line(root)
    assert banner.startswith("CTX: unavailable"), banner
    assert _payload(proc)["reason"].endswith(" " + banner)


def test_worker_and_reducer_make_the_same_scoped_banner_call():
    """'The SAME scoped call' (guard-2676) as a structural fact: exactly two call
    sites, identical command text, each scoped to $HOOK_AGENT. The reducer's
    concatenation is pinned by test_the_banner_wiring_is_additive_only."""
    lines = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8").splitlines()
    calls = [i for i, ln in enumerate(lines)
             if f'bash "$CORE_ROOT/scripts/{BANNER}"' in ln]
    assert len(calls) == 2, calls
    assert len({lines[i].strip() for i in calls}) == 1, [lines[i] for i in calls]
    for i in calls:
        assert lines[i - 1].rstrip().endswith('="$(MIND_AGENT="$HOOK_AGENT" \\'), (
            lines[i - 1])


# ---------------------------------------------------------------- (2) the park

def test_worker_net_reason_names_the_park_not_a_body_closing_write(tmp_path):
    proc, root = _worker_block(tmp_path, sensor=SENSOR)
    _assert_worker_net_block(proc, root)
    reason = _payload(proc)["reason"]
    assert "body-closing" not in reason, reason
    assert "no more work" not in reason, reason
    assert "worker-loop Phase 1 PARKS this Body (resumable)" in reason, reason
    assert REASON_TAIL in reason, reason
    # The re-entry imperative is unchanged, and comes before the sensor line.
    assert "Your FIRST action MUST be: Skill('worker-loop')" in reason
    assert "NOT Skill('aspirations')" in reason
    assert reason.index("Skill('worker-loop')") < reason.index("CTX: ")


# ---------------------------------------------------------------- (3) fail-open

def test_a_failing_banner_leaves_the_block_and_adds_nothing(tmp_path):
    proc, root = _worker_block(tmp_path, sensor=SENSOR,
                               banner_src="#!/usr/bin/env bash\nexit 3\n")
    _assert_worker_net_block(proc, root)
    assert _payload(proc)["reason"].endswith(REASON_TAIL)


def test_a_missing_banner_leaves_the_block_and_adds_nothing(tmp_path):
    proc, root = _worker_block(tmp_path, sensor=SENSOR, drop_banner=True)
    _assert_worker_net_block(proc, root)
    assert _payload(proc)["reason"].endswith(REASON_TAIL)


def test_hostile_banner_output_keeps_the_payload_valid_json(tmp_path):
    """A quote, a backslash, a tab and a raw control byte: the payload parses,
    the quote and backslash survive, and the control characters are dropped."""
    hostile = ("#!/usr/bin/env bash\n"
               "printf 'CTX: raw 1%% | a \"q\" b \\\\ c\\td\\001e | zone normal\\n'\n")
    proc, root = _worker_block(tmp_path, banner_src=hostile)
    _assert_worker_net_block(proc, root)
    reason = _payload(proc)["reason"]
    assert reason.endswith(REASON_TAIL + ' CTX: raw 1% | a "q" b \\ cde | zone normal'), (
        reason[-300:])


# ---------------------------------------------------------------- control

def test_mutation_removing_the_append_turns_the_banner_test_red(tmp_path):
    """Positive control: with the one appending expansion removed, the same
    sensor produces a banner that is NOT in the reason -- so the verbatim test
    above is sensitive to exactly this line."""
    def _drop_append(src: str) -> str:
        assert src.count(APPEND) == 1, src.count(APPEND)
        return src.replace(APPEND, '""')

    proc, root = _worker_block(tmp_path, sensor=SENSOR, edit=_drop_append)
    _assert_worker_net_block(proc, root)
    banner = _banner_line(root)
    assert "to-compact 123,456 tokens" in banner, banner
    reason = _payload(proc)["reason"]
    assert banner not in reason and reason.endswith(REASON_TAIL), reason[-300:]
