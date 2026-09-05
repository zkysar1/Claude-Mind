"""EXECUTION coverage for context-budget zone derivation when the token counter is dead.

WHAT THIS PINS, AND WHY IT NEEDED ITS OWN FILE
----------------------------------------------
`context-budget-status.py` computes headroom, pct_to_autocompact and zone from
``current_usage.input_tokens`` ALONE. ``used_percentage`` was recorded in the
output and consumed by nothing. Before this fix the file had NO test at all,
which is precisely why the following could ship and run for a whole session.

Measured 2026-09-03 (foxtrot, hostname LAPTOP-3IOFCNEO, uname -r
6.18.33.2-microsoft-standard-WSL2), from the live
``agents/foxtrot/session/context-budget.json``::

    "used_pct": 36,          <- LIVE
    "window_size": 1000000,  <- LIVE
    "input_tokens": 2,       <- DEAD (36% of 1M is ~360000, not 2)
    "headroom_tokens": 479998,
    "pct_to_autocompact": 0.0,
    "zone": "fresh"

Two halves of one record, mutually falsifying, and the three numbers the loop
ACTS on all came off the dead half. Every soft-degradation path keyed on the
zone (aspirations Phase 8.8 evolution skip, aspirations-select batch sizing,
aspirations-execute episode-chain capping, the abbreviation policy's
``zone == tight`` precondition) read ``fresh`` up to hard exhaustion, throttled
nothing, and the session then livelocked against the stop hook -- which
correctly refuses a text-only turn-end but cannot be satisfied by a session
with no budget left to act with.

WHAT IS A NON-GOAL, PINNED AS HARD AS THE FEATURE
-------------------------------------------------
* The zone is NOT re-anchored on ``used_pct``. ``classify_zone``'s own comment
  forbids that and is still binding: raw ``used_pct`` makes ``tight``
  unreachable whenever autocompact fires below 85% of the raw window. The
  repair is to the INPUT, not to the anchor -- so a small
  ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` must still be able to reach ``tight``
  well below 85% raw usage (``test_small_effective_window_still_reaches_tight``).
* A GENUINE early-session zero must stay zero (guard-2018: an absent counter
  field can BE the zero). It survives because ``used_pct`` is then also ~0.
* The derivation only ever RAISES the figure. Overstating usage degrades
  earlier than strictly needed; understating is the failure above.

THE MUTATION TWIN IS THE LOAD-BEARING TEST
------------------------------------------
``test_dead_counter_without_the_fix_reads_fresh`` strips the derivation block
from a COPY of the script and asserts the copy reproduces the original
production reading (``fresh``). Without it, the positive tests would still pass
against a hardcoded ``zone: "normal"`` and would be asserting nothing
(rb-5146: source text proves wiring exists, never that it runs).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "context-budget-status.py"

# The exact live payload shape observed on 2026-09-03. window_size is the REAL
# window the harness reported; input_tokens is the dead counter.
LIVE_DEAD_COUNTER_PAYLOAD = {
    "context_window": {
        "used_percentage": 36,
        "remaining_percentage": 64,
        "context_window_size": 1000000,
        "current_usage": {"input_tokens": 2},
    }
}


def _run(payload, script=None, env_overrides=None):
    """Invoke the status script with a statusLine-shaped payload on stdin.

    STORAGE_BACKEND is pinned to local (guard-955) and MIND_SID / MIND_AGENT
    are scrubbed (guard-1742) so the production-shaped env of the hook does not
    leak this test's identity into a live agent's session dir.
    """
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env.pop("MIND_SID", None)
    env.pop("MIND_AGENT", None)
    # Reproduce the env the banner reported at measurement time: env 600000/80.
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "600000"
    env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "80"
    # The script imports siblings from core/scripts (_stdio, _paths). The real
    # script gets those from its own directory; the mutation twin below runs
    # from a tmp dir and would fail collection without this.
    scripts_dir = str(PROJECT_ROOT / "core" / "scripts")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{scripts_dir}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else scripts_dir
    )
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

    proc = subprocess.run(
        [sys.executable, str(script or SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    return proc


def _parse_status_line(stdout):
    """Parse `CTX: 36%/75% [normal]` into (used_pct, pct_to_autocompact, zone)."""
    match = re.search(r"CTX:\s*(\d+)%/(\d+)%\s*\[(\w+)\]", stdout)
    assert match, f"status line not found in stdout: {stdout!r}"
    return int(match.group(1)), int(match.group(2)), match.group(3)


def test_dead_counter_derives_usage_from_used_pct():
    """The measured defect: 36% used must NOT report 0% to autocompact."""
    proc = _run(LIVE_DEAD_COUNTER_PAYLOAD)
    assert proc.returncode == 0, proc.stderr
    used_pct, pct_to_autocompact, zone = _parse_status_line(proc.stdout)

    assert used_pct == 36
    # 36% of 1,000,000 = 360,000 against a 480,000 limit (600000 * 80%) = 75%.
    assert pct_to_autocompact == 75, (
        "pct_to_autocompact must come from the derived figure, not the dead "
        f"counter (got {pct_to_autocompact})"
    )
    assert zone == "normal", f"expected normal, got {zone}"


def test_genuine_early_session_zero_stays_fresh():
    """guard-2018 control: an absent counter CAN be the zero. Do not inflate it."""
    payload = {
        "context_window": {
            "used_percentage": 0,
            "remaining_percentage": 100,
            "context_window_size": 1000000,
            "current_usage": {"input_tokens": 0},
        }
    }
    proc = _run(payload)
    assert proc.returncode == 0, proc.stderr
    _, pct_to_autocompact, zone = _parse_status_line(proc.stdout)
    assert pct_to_autocompact == 0
    assert zone == "fresh"


def test_near_exhaustion_with_dead_counter_reaches_tight():
    """The case that actually killed a session: 92% used previously read fresh."""
    payload = {
        "context_window": {
            "used_percentage": 92,
            "remaining_percentage": 8,
            "context_window_size": 1000000,
            "current_usage": {"input_tokens": 2},
        }
    }
    proc = _run(payload)
    assert proc.returncode == 0, proc.stderr
    _, _, zone = _parse_status_line(proc.stdout)
    assert zone == "tight", (
        "a session at 92% of a 1M window must reach the tight zone so the "
        f"degradation paths engage; got {zone}"
    )


def test_a_live_counter_is_never_lowered_by_the_derivation():
    """The derivation only RAISES. A healthy counter above the derived figure wins."""
    payload = {
        "context_window": {
            "used_percentage": 10,
            "remaining_percentage": 90,
            "context_window_size": 1000000,
            # 400k reported directly, far above the 100k the percentage implies.
            "current_usage": {"input_tokens": 400000},
        }
    }
    proc = _run(payload)
    assert proc.returncode == 0, proc.stderr
    _, pct_to_autocompact, _ = _parse_status_line(proc.stdout)
    # 400000/480000 = 83%, not the 20% the percentage alone would give.
    assert pct_to_autocompact == 83, (
        f"max() must keep the higher live counter (got {pct_to_autocompact})"
    )


def test_small_effective_window_still_reaches_tight():
    """NON-GOAL guard: the zone is still anchored on distance-to-autocompact.

    classify_zone's comment forbids re-anchoring on raw used_pct because that
    makes `tight` unreachable when autocompact fires low. Here raw usage is only
    45% but the effective window is 200k of a 1M window, so autocompact is
    imminent and the zone MUST be tight. A used_pct-anchored zone would say
    `fresh`.
    """
    payload = {
        "context_window": {
            "used_percentage": 45,
            "remaining_percentage": 55,
            "context_window_size": 1000000,
            "current_usage": {"input_tokens": 2},
        }
    }
    proc = _run(payload, env_overrides={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"})
    assert proc.returncode == 0, proc.stderr
    used_pct, _, zone = _parse_status_line(proc.stdout)
    assert used_pct == 45, "raw usage is well under any used_pct tight threshold"
    assert zone == "tight", (
        "distance-to-autocompact must still drive the zone; a used_pct anchor "
        f"would have said fresh here. got {zone}"
    )


def test_dead_counter_without_the_fix_reads_fresh(tmp_path):
    """MUTATION TWIN: strip the derivation and reproduce the production reading.

    This is what proves the block above is load-bearing rather than decorative.
    The mutant must reproduce the exact defect measured in production: zone
    `fresh` and 0% to autocompact on a payload that is 36% used.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    needle = "        input_tokens = max(input_tokens, derived_input_tokens)"
    assert needle in source, (
        "the derivation's final line moved; update this mutation twin so it "
        "keeps testing something"
    )
    # Neutralise the raise without touching line numbers around it.
    mutant_source = source.replace(
        needle, "        input_tokens = input_tokens  # mutated: derivation removed"
    )
    assert mutant_source != source

    mutant = tmp_path / "context-budget-status-mutant.py"
    mutant.write_text(mutant_source, encoding="utf-8")

    proc = _run(LIVE_DEAD_COUNTER_PAYLOAD, script=mutant)
    assert proc.returncode == 0, proc.stderr
    used_pct, pct_to_autocompact, zone = _parse_status_line(proc.stdout)

    assert used_pct == 36
    assert pct_to_autocompact == 0, (
        "the mutant must reproduce the measured defect (0% to autocompact at "
        f"36% used); got {pct_to_autocompact}"
    )
    assert zone == "fresh", (
        f"the mutant must reproduce the measured `fresh` reading; got {zone}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
