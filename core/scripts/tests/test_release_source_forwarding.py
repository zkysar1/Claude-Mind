"""aspirations-release.sh must be able to SAY `agent` — .

THE DEFECT THIS PINS. The wrapper built `QUERY="id=${GOAL_ID}&source=world"` with
`world` as a LITERAL, and its arg loop appended every unrecognized flag to a
`PASSTHROUGH` array that no line of the script ever read — the g-115-4733
silent-swallow shape. So `--source agent` was accepted, silently discarded, and
the request went to the WORLD queue anyway, with exit 0.

WHY IT MATTERED ONLY NOW. That was harmless while `claim()` refused the agent
queue outright (400 `agent_queue_goal`). It no longer does: g-306-238 landed
`&source=agent` on the claim endpoint, LIVE-verified 2026-08-07 from cc-02 by two
zero-mutation probes — `id=<absent>&source=agent` answers "not found in **agent**
queue" (a world-resolving daemon says "world queue"), and `source=bogus` answers
400 `invalid_source`, a branch that exists only in the post-g-306-238 module.
A claim protocol with no matching RELEASE strands a claim on every recurring
cadence goal (g-001-01..g-001-10 all live in the agent queue), so the wrapper had
to gain `--source` BEFORE the loop digest could drop its `IF source==world`
release guard.

THE READING LESSON. g-306-249's own description asserted "release() ALREADY
supports source=agent (it resolves paths from its own source param), so this is a
digest change only, not an endpoint change." That is true of the DAEMON endpoint
and false of the WRAPPER in front of it — the inverse of guard-2374 (a flag the
client accepts and the endpoint rejects). The measured half of a goal's premise
does not vouch for the inferred half (rb-5669 / guard-1719). Two independent
corroborations that the wrapper was the gap:
`.claude/skills/aspirations-execute/SKILL.md` already documented
`aspirations-release.sh <goal-id> (when --source agent)` as a call shape, and the
`PASSTHROUGH` array it would have travelled in has no reader.

HERMETIC BY CONSTRUCTION: no daemon, no network, no world writes. Cases 1-3 run
the REAL wrapper in its production arg shape — the refusal and the --source
validation both fire BEFORE `_runtime.sh` is sourced, so they need no transport.
Case 4 exercises the query-construction lines extracted from the wrapper source,
the same extract-and-exercise-in-isolation technique
`test_runner_claim_release_surface.py` uses on `runner-claim.sh`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

_WRAPPER = Path(__file__).resolve().parents[1] / "aspirations-release.sh"


def _run_wrapper(*args: str):
    # .as_posix(), never str(Path) — bash silently strips a WindowsPath's
    # backslashes (guard-581).
    p = subprocess.run(
        [BASH, _WRAPPER.as_posix(), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return p.returncode, (p.stdout + p.stderr)


# --------------------------------------------------------------------------
# 1. The silent swallow is gone. rc==2 SPECIFICALLY, per the _argv_strict.sh
#    header: the daemon transport path also exits non-zero, so a test asserting
#    `rc != 0` stays green with the guard reverted.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad_flag", ["--bogus", "--value-file", "--sourse", "-x"])
def test_unknown_flag_is_refused_with_exit_2(bad_flag):
    rc, out = _run_wrapper("g-306-1", bad_flag, "some-value")
    assert rc == 2, f"expected rc=2 for {bad_flag!r}, got {rc}: {out}"
    assert "unknown option" in out, out


def test_unknown_flag_refusal_names_the_accepted_flags():
    """A refusal is only better than a silent swallow if the caller can act on it."""
    rc, out = _run_wrapper("g-306-1", "--bogus")
    assert rc == 2, out
    assert "--source" in out, out


# --------------------------------------------------------------------------
# 2. --help must NOT be caught by the refusal. Turning `--help` into an exit-2
#    error would be a regression the refusal introduced, not a defect it fixed.
# --------------------------------------------------------------------------
def test_help_exits_zero_and_names_source():
    rc, out = _run_wrapper("--help")
    assert rc == 0, f"--help must exit 0, got {rc}: {out}"
    assert "--source" in out, out


# --------------------------------------------------------------------------
# 3. An out-of-vocabulary --source is refused HERE rather than shipped to the
#    daemon. The endpoint would answer 400 invalid_source either way; refusing
#    locally keeps the failure at the layer that can name the typo.
# --------------------------------------------------------------------------
def test_invalid_source_value_is_refused():
    rc, out = _run_wrapper("g-306-1", "--source", "wrold")
    assert rc == 1, f"expected rc=1 for an invalid source, got {rc}: {out}"
    assert "world or agent" in out, out


# --------------------------------------------------------------------------
# 4. THE QUERY ITSELF. This is the property the whole change exists for, and the
#    one a source-text-only assertion would miss: `world` must be the DEFAULT
#    (backward compatibility is the entire safety argument for landing this
#    ahead of the loop-digest change) and `agent` must actually reach the query.
# --------------------------------------------------------------------------
def _build_query(goal_id: str, source_val: str, sid: str = "") -> str:
    """Evaluate the wrapper's own QUERY-construction lines in isolation."""
    src = _WRAPPER.read_text(encoding="utf-8")
    m = re.search(r'^(QUERY="id=.*?)^rc=0', src, re.S | re.M)
    assert m, "could not locate the QUERY construction block in aspirations-release.sh"
    block = m.group(1)
    assert "&source=" in block, block
    script = (
        "rt_url_encode() { printf '%s' \"$1\"; }\n"
        f'GOAL_ID="{goal_id}"\n'
        f'SOURCE_VAL="{source_val}"\n'
        f'MIND_SID="{sid}"\n'
        f"{block}\n"
        'printf "%s" "$QUERY"\n'
    )
    p = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_default_source_is_world():
    """Byte-identical to the pre-change wrapper for every existing caller."""
    assert _build_query("g-115-1", "world") == "id=g-115-1&source=world"


def test_agent_source_reaches_the_query():
    """The capability the loop digest's Phase 5.3 release guard depends on."""
    assert _build_query("g-001-08", "agent") == "id=g-001-08&source=agent"


def test_sid_is_still_forwarded_alongside_source():
    """: the non-holder release warning is structurally dead without it."""
    q = _build_query("g-001-08", "agent", sid="abc-123")
    assert "source=agent" in q and "sid=abc-123" in q, q


def test_wrapper_no_longer_hardcodes_the_world_literal():
    """Direct regression pin on the original defect's exact shape."""
    src = _WRAPPER.read_text(encoding="utf-8")
    live = [
        ln for ln in src.splitlines()
        if 'QUERY="id=' in ln and not ln.lstrip().startswith("#")
    ]
    assert live, "QUERY construction line vanished"
    for ln in live:
        assert "source=world" not in ln, f"hardcoded world literal is back: {ln}"


# --------------------------------------------------------------------------
# 5. THE POSITIONAL HALF — .
#
#    Section 1 pins that an unknown FLAG is refused. Nothing pinned the twin:
#    an unexpected POSITIONAL was still discarded first-wins-rest-dropped, so
#    the queue name passed bare vanished and the release went to the DEFAULT
#    queue. Measured on cc-07 before the fix, same id, one token apart:
#        <id> agent          -> "not found in world queue"   <- swallowed
#        <id> --source agent -> "not found in agent queue"   <- meant
#    Same intent, opposite target, and exit 0 on a real id either way.
#
#    Note the direction: --source is what made this reachable (). A
#    flag's existence teaches callers the wrapper HAS that dimension, so adding
#    one adds a bare-positional failure mode to the arm that ignores positionals.
#    The suite that shipped with the flag had neither case below.
# --------------------------------------------------------------------------
def test_extra_positional_is_refused_with_exit_2():
    """The exact mis-invocation --source made plausible."""
    rc, out = _run_wrapper("g-306-1", "agent")
    assert rc == 2, f"expected rc=2 for a bare second positional, got {rc}: {out}"
    assert "extra argument" in out, out


def test_extra_positional_refusal_points_at_the_flag():
    """`agent` bare is almost always a --source that lost its flag; say so."""
    rc, out = _run_wrapper("g-306-1", "agent")
    assert rc == 2, out
    assert "--source" in out, out


def test_one_positional_still_passes_the_ceiling():
    """The load-bearing negative: the ceiling must not fire at N=1.

    Without this, a ceiling of 0 — or any off-by-one — passes the two tests
    above while breaking every real invocation. Uses an out-of-vocabulary
    --source so the run still terminates BEFORE _runtime.sh (rc=1 from the
    world|agent check), keeping the case hermetic: reaching that check at all
    proves the single positional survived.
    """
    rc, out = _run_wrapper("g-306-1", "--source", "wrold")
    assert rc == 1, f"one positional must not be refused, got rc={rc}: {out}"
    assert "world or agent" in out, out


def test_ceiling_fires_before_the_source_validation():
    """Ordering matters: the argv defect is reported, not a downstream symptom."""
    rc, out = _run_wrapper("g-306-1", "extra", "--source", "wrold")
    assert rc == 2, f"expected the ceiling (rc=2), got {rc}: {out}"
    assert "extra argument" in out, out


def test_source_with_no_value_is_refused_loudly():
    """The silent-death case — and `rc == 2` is only half the assertion.

    WAS `SOURCE_VAL="${2:-}"; shift 2`. With --source as the final argument
    there is no $2, `shift 2` is out of range and returns 1, and `set -e` kills
    the script THERE — before the world|agent check below it can print anything.
    Measured pre-fix: rc=1, ZERO BYTES on both streams.

    That is the worst shape a wrapper failure can take, because rc=1-and-silent
    is exactly what the daemon transport path produces: the caller cannot tell a
    typo from an outage. So the output assertion is not decoration — a fix that
    returned rc=2 silently would still leave the caller guessing, and a fix that
    printed but kept rc=1 would still be indistinguishable from transport.
    """
    rc, out = _run_wrapper("g-306-1", "--source")
    assert rc == 2, f"expected rc=2 for a valueless --source, got {rc}: {out}"
    assert out.strip(), "refusal must not be silent — rc=1-and-silent was the defect"
    assert "requires a value" in out, out


def test_source_with_no_value_and_no_goal_id():
    """The same arity defect with nothing else on the line."""
    rc, out = _run_wrapper("--source")
    assert rc == 2, f"expected rc=2, got {rc}: {out}"
    assert "requires a value" in out, out


def test_catch_all_arm_no_longer_swallows():
    """Direct pin on the defect's exact shape, anchored to NON-COMMENT lines.

    The unanchored form fails against this very wrapper: the comment explaining
    the defect necessarily quotes it. Same guard-1099 trap
    `test_passthrough_array_is_gone` documents below — a check counting the
    prose that quotes a deleted construct as evidence it is still live.
    """
    src = _WRAPPER.read_text(encoding="utf-8")
    live = [
        ln for ln in src.splitlines()
        if "argv_strict_refuse_extra_positional" in ln
        and not ln.lstrip().startswith("#")
    ]
    assert live, "the positional ceiling call is gone from aspirations-release.sh"


def test_passthrough_array_is_gone():
    """It had no reader — carrying it is what made the swallow silent.

    Anchored to NON-COMMENT lines. The unanchored form fails against this very
    wrapper, because the comment explaining the defect necessarily NAMES it —
    the guard-1099 shape, where a check counts the prose quoting a deleted
    construct as evidence the construct is still live. Caught by this test
    failing on its own first run.
    """
    src = _WRAPPER.read_text(encoding="utf-8")
    live = [
        ln for ln in src.splitlines()
        if "PASSTHROUGH" in ln and not ln.lstrip().startswith("#")
    ]
    assert not live, f"the unread PASSTHROUGH array is back: {live}"
