""" — the claim wrapper must refuse an unknown flag AND write no roster row.

THE DEFECT THIS PINS (g-115-3686, already fixed in aspirations-claim.sh:107-130)
The `-*` arm of the argv loop used to do `PASSTHROUGH+=("$1"); shift` — shifting
the FLAG ONLY. The orphaned VALUE then fell through to the positional branch and
became `agent_name`. So a single typo (`--deviation-code X` for `--deviation X`)
silently minted a PHANTOM FLEET AGENT named X: a team-state `agent_status` row in
a fleet that has no such agent, carrying an `in_flight` that release could never
clear, because no process anywhere is that agent.

WHY THIS FILE EXISTS AT ALL
The fix shipped with no regression test. `test_unknown_flag_refusal.py` covers 30
wrappers and `aspirations-claim.sh` is NOT among them — it appears there only in a
comment ("aspirations-claim.sh already refusing (g-115-3686)"), i.e. the g-115-5438
sweep EXCLUDED it from CASES precisely because it was already correct. So the
behaviour every other wrapper's parser has pinned is, for this one, unpinned.

WHY THE EXIT CODE IS NOT ENOUGH (the half that would otherwise be missed)
The harm is the ROSTER WRITE, not the exit status. A future refactor that returns
non-zero for some other reason while still binding the stray value would keep an
rc-only test green. Every case below therefore asserts BOTH halves: the refusal
fired, AND the stray token reached no store.

WHY THE ROSTER HALF ASSERTS ABSENCE RATHER THAN BYTE-EQUALITY
The harm is a row being ADDED, so the scratch world is left EMPTY and the assertion
is that no roster file and no agent shard were ever created. That is both the
sharper predicate and the honest one: seeding a roster fixture would mean this test
hand-writing a governed store shape, which guard-996 forbids and which no framework
script would produce for a throwaway tmp dir.

WHY rc==1 IS SAFE TO PIN HERE
Sibling `test_unknown_flag_refusal.py` pins rc==2 because its wrappers' daemon path
can also exit 1, so `!= 0` stays green on revert. This wrapper's own refusals exit 1
by file convention and the daemon path can ALSO exit 1 — the rc alone cannot
discriminate. So each case pins rc AND the refusal message AND the absence of
daemon-path markers. On revert the stray value proceeds to the daemon path: stderr
then carries transport text and never the refusal text, so the message assertions go
RED regardless of which rc the transport happens to return.

HERMETIC IN BOTH DIRECTIONS — THE LIVE FLEET ROSTER IS NEVER TOUCHED
RT_PORT_FILE points at a nonexistent path and RT_NO_AUTOSPAWN=1 is set (honored at
both spawn sites, _runtime.sh:727,766), so even a REVERTED wrapper never contacts a
live daemon nor spawns one: rt_base_url resolves empty, rt_call returns 3, autospawn
is suppressed. MIND_WORLD/MIND_META point at a per-test tmp world, and
STORAGE_BACKEND=local is pinned (guard-955/rb-2983: on an own-cloud box
OwnCloudBackend._s3_key derives the key from the env id, NOT from the MIND_WORLD
override, so a tmp write would collide on the PRODUCTION key). The goal id is bogus
(g-99999-99999, sibling-file convention) as a further net.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from _runtime_bash import bash_cmd  # noqa: E402

BOGUS_GOAL = "g-99999-99999"
CLAIM = SCRIPTS / "aspirations-claim.sh"

# Markers that only appear once the wrapper got PAST the argv loop. The refusal
# text contains none of these.
#
# `scorer-sovereignty` is in this set BY MEASUREMENT, not by guesswork: the
# sovereignty gate runs immediately after the argv loop and BEFORE any transport,
# so on a bogus goal id it refuses first and the daemon is never reached. An
# earlier draft of this file listed only the transport markers and its positive
# control went red on 3 of 5 accepted flags for exactly that reason — the flags
# had parsed fine. Reaching the gate is proof the parser completed and handed off,
# which is the only thing the control needs to establish.
PAST_PARSER_MARKERS = (
    "daemon",
    "rt_call",
    "rt_curl",
    "http://",
    "unreachable",
    "scorer-sovereignty",
)

# The five flags the parser accepts, each taking a VALUE (`${2-}`, shift 2 —
# verified against aspirations-claim.sh:43-106). Used as the positive control.
ACCEPTED_FLAGS = [
    ("--cross-lane", "some-lane"),
    ("--override-lane-pin", "justification text"),
    ("--source", "world"),
    ("--deviation", "force-override"),
    ("--verdict-file", "/nonexistent/verdict.json"),
]

# The roster surfaces a phantom-agent write would land on, relative to the world
# root: the composed file and the per-agent shard dir (the  shard).
ROSTER_FILE = "team-state.yaml"
ROSTER_SHARD_GLOB = "team-state/agents/*.yaml"


def _world(tmp_path):
    """An EMPTY scratch world. Nothing seeds a roster — see the module docstring."""
    world = tmp_path / "world"
    world.mkdir(parents=True)
    (tmp_path / "meta").mkdir()
    return world


def _env(tmp_path, world):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["RT_NO_AUTOSPAWN"] = "1"
    env["RT_PORT_FILE"] = str(tmp_path / "no-such-port-file")
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(tmp_path / "meta")
    return env


def _run_claim(argv, tmp_path, world):
    return subprocess.run(
        bash_cmd(CLAIM, *argv),
        capture_output=True,
        text=True,
        input="",
        env=_env(tmp_path, world),
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


def _assert_no_roster_row(world, stray):
    """The roster half: the stray token reached no store, and no roster was created.

    Checked over the WHOLE scratch world rather than one path, because the roster is
    sharded — asserting on the composed file alone would miss a shard write. These
    are existence and substring checks over a throwaway tmp tree, never a parse of a
    live store.
    """
    assert not (world / ROSTER_FILE).exists(), (
        f"a refused claim created {ROSTER_FILE}"
    )
    shards = sorted(world.glob(ROSTER_SHARD_GLOB))
    assert shards == [], f"a refused claim wrote roster shard(s): {shards}"
    for path in world.rglob("*"):
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        assert stray not in body, (
            f"stray token {stray!r} reached {path} — this is the phantom-agent "
            f"write g-115-3686 fixed; the parser bound it as a positional"
        )


def _assert_refused_before_daemon(r, tmp_path):
    lowered = (r.stdout + r.stderr).lower()
    for marker in PAST_PARSER_MARKERS:
        assert marker not in lowered, (
            f"found past-parser marker {marker!r} — the wrapper got PAST the "
            f"refusal.\nstderr={r.stderr!r}"
        )
    assert not (tmp_path / "no-such-port-file").exists(), (
        "the wrapper created a port file — autospawn was not suppressed"
    )


def test_unknown_flag_is_refused_and_named(tmp_path):
    """rc=1, the offending flag echoed, and nothing reached the daemon."""
    world = _world(tmp_path)
    r = _run_claim([BOGUS_GOAL, "--nonexistent-flag", "alpha"], tmp_path, world)
    assert r.returncode == 1, (
        f"expected parse-time refusal rc=1, got {r.returncode}.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "unrecognized flag" in r.stderr
    assert "--nonexistent-flag" in r.stderr, (
        "the refusal must echo the flag so the caller sees which argument is wrong"
    )
    assert r.stdout == ""
    _assert_refused_before_daemon(r, tmp_path)


def test_typo_flag_value_never_becomes_a_roster_row(tmp_path):
    """THE INCIDENT SHAPE: `--deviation-code X` must not mint a phantom agent X.

    Pre-fix, the `-*` arm shifted the flag only; `force-override` fell through to
    the agent-name positional and was written to the fleet roster as a real agent.
    Both halves are asserted: the refusal fired, and no store carries the token.
    """
    world = _world(tmp_path)
    r = _run_claim(
        [BOGUS_GOAL, "--deviation-code", "force-override"], tmp_path, world
    )
    assert r.returncode == 1, (
        f"expected refusal rc=1, got {r.returncode}.\nstderr={r.stderr!r}"
    )
    assert "unrecognized flag" in r.stderr
    assert "--deviation-code" in r.stderr
    _assert_refused_before_daemon(r, tmp_path)
    _assert_no_roster_row(world, "force-override")


def test_typo_flag_with_trailing_agent_writes_no_row(tmp_path):
    """The variant where a REAL agent name follows the typo'd flag.

    Pre-fix this bound `force-override` as the agent and dropped `alpha`. The
    refusal must fire before either binding, and neither token may reach a store.
    """
    world = _world(tmp_path)
    r = _run_claim(
        [BOGUS_GOAL, "--deviation-code", "force-override", "alpha"], tmp_path, world
    )
    assert r.returncode == 1
    assert "unrecognized flag" in r.stderr
    _assert_refused_before_daemon(r, tmp_path)
    _assert_no_roster_row(world, "force-override")


@pytest.mark.parametrize(
    "flag,value", ACCEPTED_FLAGS, ids=[f[0] for f in ACCEPTED_FLAGS]
)
def test_accepted_flag_still_parses(flag, value, tmp_path):
    """POSITIVE CONTROL: every accepted flag must still parse and hand off.

    Without this, a parser that refused EVERYTHING would pass the refusal tests
    above. rc is deliberately not pinned (it belongs to the transport); the pin is
    that no refusal fired and the failure that does occur is the daemon path's,
    which proves the parser accepted the flag and handed off.
    """
    world = _world(tmp_path)
    r = _run_claim([BOGUS_GOAL, "alpha", flag, value], tmp_path, world)
    assert "unrecognized flag" not in r.stderr, (
        f"accepted flag {flag} was refused as unknown.\nstderr={r.stderr!r}"
    )
    assert "unexpected extra positional" not in r.stderr, (
        f"the VALUE of {flag} leaked to the positional branch — this is the "
        f"g-115-3686 shape on an ACCEPTED flag.\nstderr={r.stderr!r}"
    )
    lowered = (r.stdout + r.stderr).lower()
    assert any(m in lowered for m in PAST_PARSER_MARKERS), (
        f"expected a past-parser failure (the sovereignty gate or the transport), "
        f"proving the parser accepted {flag} and handed off. got:\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
