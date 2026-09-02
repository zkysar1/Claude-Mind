"""strategic_focus: one-sided-key preservation + a set_at that actually moves.

g-115-5294. TWO defects, deliberately pinned in ONE file because they are NOT
independent: preserving one-sided keys while set_at stays frozen means a
preserved key is chosen by JSON string comparison rather than recency, so a test
suite that covered only one half would certify a fix that still loses edits.

  DEFECT 1  coordination_merge._merge_strategic_focus did `out = dict(win)`,
            taking the winner's keys alone, so a key carried only by the LOSER
            was silently dropped.
  DEFECT 2  team-state.py cmd_update never bumped strategic_focus.set_at, while
            _merge_strategic_focus orders on it. Live team-state carried set_at
            2026-07-04T13:45:00 against a `primary` amended 2026-08-03, so a
            cross-box merge saw EQUAL timestamps and fell through to
            _order_by_ts's _canon content tiebreak. The amendment survived by
            being the LONGER string. guard-1153 permits LWW only on a timestamp
            written by the same mutation that writes the field.

TESTED AS A PROPERTY, NOT AS THE KNOWN INSTANCE (guard-3080). The bad instance
was "an amendment lost to a stale stamp"; the properties are "a one-sided key
survives", "the winner's value wins including an explicit None", "a write bumps
the stamp", and "recency beats string length". A test keyed to the specific
2026-08-03 amendment would pass against a fix that only special-cased it.

WHY THE PRE-EXISTING SUITE DID NOT CATCH DEFECT 1:
test_coordination_merge.py::test_ts_acknowledged_by_and_completions_union
asserts ONLY the acknowledged_by union, which the broken `dict(win)` code
satisfies — acknowledged_by was the one key it unioned. Both sides of that
test's strategic_focus carry identical key SETS, so no key is ever one-sided and
the defect is unreachable from it. Every merge-side test below was confirmed RED
against `out = dict(win)`.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))

import coordination_merge as cm  # noqa: E402

TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"

_BASE = {
    "last_updated": None, "last_updated_by": None,
    "strategic_focus": {"primary": None, "set_at": None, "acknowledged_by": []},
    "active_blockers": [], "recent_completions": [], "agent_status": {},
    "critical_blockers": [], "inbox_alert_backlog": None,
}


def _ts(**kw) -> bytes:
    base = dict(_BASE)
    base.update(kw)
    return yaml.dump(base, default_flow_style=False, sort_keys=False).encode()


def _focus(a: bytes, b: bytes) -> dict:
    return yaml.safe_load(cm.merge_team_state(a, b).decode())["strategic_focus"]


# --- DEFECT 1: the merge preserves one-sided keys ---------------------------

def test_loser_only_key_survives():
    """THE defining property. RED against `out = dict(win)`."""
    a = _ts(strategic_focus={"primary": "newer", "set_at": "2026-08-03T10:00:00",
                             "acknowledged_by": []})
    b = _ts(strategic_focus={"primary": "older", "set_at": "2026-07-04T13:45:00",
                             "rationale": "only-on-the-loser",
                             "acknowledged_by": []})
    m = _focus(a, b)
    assert m["primary"] == "newer", "the newer set_at must still win the shared key"
    assert m.get("rationale") == "only-on-the-loser", \
        "a key carried only by the LOSER must survive the merge"


def test_winner_value_wins_on_a_shared_key():
    """Preservation must not invert into loser-wins. Guards the other direction:
    a naive `dict(lose); update(...)` with the operands swapped would pass the
    test above and fail this one."""
    a = _ts(strategic_focus={"primary": "newer", "set_at": "2026-08-03T10:00:00",
                             "rationale": "from-winner", "acknowledged_by": []})
    b = _ts(strategic_focus={"primary": "older", "set_at": "2026-07-04T13:45:00",
                             "rationale": "from-loser", "acknowledged_by": []})
    assert _focus(a, b)["rationale"] == "from-winner"


def test_winner_explicit_none_is_not_backfilled_from_the_loser():
    """"Clearing" writes a None VALUE to a key that remains present. If the merge
    treated None as absent and backfilled it, a cleared field would resurrect —
    which is precisely the guard-1816 hazard this handler was audited against."""
    a = _ts(strategic_focus={"primary": None, "set_at": "2026-08-03T10:00:00",
                             "acknowledged_by": []})
    b = _ts(strategic_focus={"primary": "stale-value",
                             "set_at": "2026-07-04T13:45:00",
                             "acknowledged_by": []})
    assert _focus(a, b)["primary"] is None, \
        "a deliberately cleared field must not be resurrected by the loser"


def test_acknowledged_by_union_is_not_regressed():
    a = _ts(strategic_focus={"primary": "X", "set_at": "2026-07-02T09:00:00",
                             "acknowledged_by": ["echo"]})
    b = _ts(strategic_focus={"primary": "X", "set_at": "2026-07-02T09:00:00",
                             "rationale": "one-sided", "acknowledged_by": ["zeta"]})
    m = _focus(a, b)
    assert m["acknowledged_by"] == ["echo", "zeta"]
    assert m.get("rationale") == "one-sided"


# --- commutativity + convergence (verification outcome 3) ------------------

def test_commutative_with_a_one_sided_key():
    a = _ts(last_updated="2026-08-03T10:00:00",
            strategic_focus={"primary": "newer", "set_at": "2026-08-03T10:00:00",
                             "acknowledged_by": ["echo"]})
    b = _ts(last_updated="2026-07-04T13:45:00",
            strategic_focus={"primary": "older", "set_at": "2026-07-04T13:45:00",
                             "rationale": "loser-only", "acknowledged_by": ["zeta"]})
    assert cm.merge_team_state(a, b) == cm.merge_team_state(b, a)


def test_multiround_convergence():
    """Re-merging a merged result against either operand is a fixed point, so
    repeated cross-box syncs cannot oscillate."""
    a = _ts(last_updated="2026-08-03T10:00:00",
            strategic_focus={"primary": "newer", "set_at": "2026-08-03T10:00:00",
                             "acknowledged_by": ["echo"]})
    b = _ts(last_updated="2026-07-04T13:45:00",
            strategic_focus={"primary": "older", "set_at": "2026-07-04T13:45:00",
                             "rationale": "loser-only", "acknowledged_by": ["zeta"]})
    ab = cm.merge_team_state(a, b)
    assert cm.merge_team_state(ab, a) == ab
    assert cm.merge_team_state(ab, b) == ab
    assert cm.merge_team_state(ab, ab) == ab


# --- DEFECT 2: the writer bumps set_at -------------------------------------

def _run(world: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(world)
    # guard-955: pinned explicitly rather than relying on the conftest autouse
    # fixture. On an own-cloud box a subprocess inheriting the backend derives
    # its S3 key from customer_prefix+env_id+filename — NOT from MIND_WORLD —
    # so a tmp-world write would land on the production key.
    env["STORAGE_BACKEND"] = "local"
    return subprocess.run([sys.executable, str(TEAM_STATE_PY), *args],
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.fixture()
def world(tmp_path: Path) -> Path:
    w = tmp_path / "world"
    w.mkdir()
    r = _run(w, "init")
    assert r.returncode == 0, r.stderr
    return w


def _read_focus(world: Path) -> dict:
    return (yaml.safe_load((world / "team-state.yaml").read_text(encoding="utf-8"))
            or {}).get("strategic_focus") or {}


def test_writing_primary_bumps_set_at(world: Path):
    """THE defining property for defect 2. RED before the fix."""
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", "2026-07-04T13:45:00")
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", "strategic_focus.primary",
             "--value", "amended directive")
    assert r.returncode == 0, r.stderr
    f = _read_focus(world)
    assert f["primary"] == "amended directive"
    assert f["set_at"] != "2026-07-04T13:45:00", \
        "amending primary must bump set_at, or the merge orders on a frozen stamp"
    assert f["set_at"] > "2026-07-04T13:45:00"


def test_explicit_set_at_write_is_respected(world: Path):
    """The escape hatch: a migration restating a historical stamp must not be
    clobbered by an auto-bump."""
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", "2026-07-04T13:45:00")
    assert r.returncode == 0, r.stderr
    assert _read_focus(world)["set_at"] == "2026-07-04T13:45:00"


def test_unrelated_field_does_not_bump_set_at(world: Path):
    """Negative control. Without it, a bump applied unconditionally on every
    core-file write would pass every other test in this section."""
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", "2026-07-04T13:45:00")
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", "inbox_alert_backlog", "--value", "7")
    assert r.returncode == 0, r.stderr
    assert _read_focus(world)["set_at"] == "2026-07-04T13:45:00", \
        "a write outside strategic_focus must leave its stamp alone"


# --- the two defects are coupled ------------------------------------------

def test_amendment_made_through_the_writer_wins_the_merge(world: Path):
    """THE COUPLING TEST, and it must go through the WRITER to be worth anything.

    An earlier draft of this test supplied both set_at values directly in YAML.
    It passed — and it passed under BOTH mutations, because hand-written stamps
    exercise only _order_by_ts, which already preferred recency before this fix.
    Its docstring claimed to be the coupling test while guarding nothing
    (guard-1793: a test whose subject is not on the path under test is vacuous no
    matter how green it reads).

    So: amend `primary` via the real CLI, take the team-state it produced, and
    merge it against a stale document whose primary is deliberately the LONGER
    string. Under a frozen stamp the two stamps compare EQUAL and the _canon
    tiebreak hands the win to the longer stale side. Only a stamp the writer
    actually moved lets recency decide. RED under M2."""
    stale_stamp = "2026-07-04T13:45:00"
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", stale_stamp)
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", "strategic_focus.primary",
             "--value", "short amendment")
    assert r.returncode == 0, r.stderr

    amended = (world / "team-state.yaml").read_bytes()
    amended_focus = _read_focus(world)

    # The stale side must carry the SAME KEY SET as the writer's output, with
    # only `primary` longer. A second draft of this test hand-built a 3-key stale
    # side against the writer's richer document, so _canon — which compares the
    # WHOLE sub-document, not just primary — favoured the amendment on KEY COUNT.
    # It went green under M2 for a reason unrelated to what it asserts. Mirroring
    # the key set is what makes the tiebreak turn on primary length alone.
    # _canon orders LEXICOGRAPHICALLY, not by length — a fact worth stating
    # because 's description says the amendment "wins by being longer",
    # and length is not what decides it. A first draft used a long primary
    # starting with "a", which sorts BELOW "short amendment" and so failed to set
    # up the adverse case at all; the assertion below caught that. "zzz" is
    # chosen to sort ABOVE the amendment.
    stale_focus = dict(amended_focus)
    stale_focus["primary"] = (
        "zzz stale directive that wins the canonical-JSON tiebreak by sorting "
        "above the amendment")
    stale_focus["set_at"] = stale_stamp
    stale = _ts(strategic_focus=stale_focus)

    # Guard the guard: confirm the tiebreak really would favour the stale side,
    # so a future edit cannot quietly reduce this to a no-op again.
    assert cm._canon(stale_focus) > cm._canon(amended_focus), \
        "fixture no longer sets up the adverse tiebreak this test depends on"

    assert _focus(amended, stale)["primary"] == "short amendment"
    assert _focus(stale, amended)["primary"] == "short amendment"


def test_equal_stamps_still_tiebreak_deterministically():
    """The frozen-stamp path is not removed, only escaped: two genuinely
    simultaneous writes must still converge to the same winner on both boxes."""
    a = _ts(strategic_focus={"primary": "aaa", "set_at": "2026-08-03T09:00:00",
                             "acknowledged_by": []})
    b = _ts(strategic_focus={"primary": "zzz", "set_at": "2026-08-03T09:00:00",
                             "acknowledged_by": []})
    assert _focus(a, b) == _focus(b, a)


# --- : the stamp must NOT ride a non-content write ---------------
#
# The negative control above (`test_unrelated_field_does_not_bump_set_at`) uses
# `inbox_alert_backlog`, a field OUTSIDE strategic_focus. Nothing covered a write
# INSIDE strategic_focus that is not the directive's content — which is where the
# whole fleet writes, because acknowledging is the normal response to a directive.

def test_acking_a_directive_does_not_bump_set_at(world: Path):
    """THE defining property for . RED before the fix.

    `acknowledged_by` is appended to by every agent a directive binds, so under
    the old unconditional bump the directive's age was falsified by agents doing
    exactly the right thing."""
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", "2026-07-04T13:45:00")
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", "strategic_focus.acknowledged_by",
             "--value", '"alpha"', "--operation", "append")
    assert r.returncode == 0, r.stderr
    f = _read_focus(world)
    assert f["acknowledged_by"] == ["alpha"], "the ack itself must still land"
    assert f["set_at"] == "2026-07-04T13:45:00", \
        "an ack must not re-stamp the directive's own provenance"


@pytest.mark.parametrize("sub,val", [("primary", "amended"),
                                     ("rationale", "because"),
                                     ("set_by", "zachary")])
def test_directive_content_writes_STILL_bump_set_at(world: Path, sub: str, val: str):
    """Anti-vacuity twin (guard-1220). A fix that simply stopped stamping
    entirely would pass the test above and silently reintroduce g-115-5294's
    frozen-stamp defect, so the allowlist must be shown to still ALLOW."""
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", "2026-07-04T13:45:00")
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", f"strategic_focus.{sub}", "--value", val)
    assert r.returncode == 0, r.stderr
    assert _read_focus(world)["set_at"] > "2026-07-04T13:45:00", \
        f"writing {sub} is directive content and MUST still bump the stamp"


def test_an_ack_cannot_outrank_the_owner_directive_in_the_merge(world: Path):
    """The expensive consequence, pinned end-to-end: directive LOSS.

    Built through the REAL writer, not hand-written stamps — a hand-written
    fixture exercises only _order_by_ts, which already preferred recency, so it
    would go green under the broken writer (the guard-1793 lesson this file
    already learned once).

    Setup: this box holds a STALE directive and merely ACKS it. A peer holds a
    NEWER owner directive. Under the old unconditional bump the ack stamped
    `now`, which outranks the peer's stamp, so the stale `primary` won the
    scalars and the owner's directive was destroyed by an acknowledgement."""
    r = _run(world, "update", "--field", "strategic_focus.primary",
             "--value", "stale directive")
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", "strategic_focus.set_at",
             "--value", "2026-07-04T13:45:00")
    assert r.returncode == 0, r.stderr
    r = _run(world, "update", "--field", "strategic_focus.acknowledged_by",
             "--value", '"alpha"', "--operation", "append")
    assert r.returncode == 0, r.stderr

    acker = (world / "team-state.yaml").read_bytes()
    acker_focus = _read_focus(world)
    # Guard the guard: if the ack DID bump, this fixture is no longer adverse.
    assert acker_focus["set_at"] == "2026-07-04T13:45:00"

    owner_focus = dict(acker_focus)
    owner_focus["primary"] = "NEW owner directive"
    owner_focus["set_at"] = "2026-08-01T00:00:00"   # newer than stale, older than now
    owner_focus["acknowledged_by"] = ["bravo"]
    owner = _ts(strategic_focus=owner_focus)

    m = _focus(acker, owner)
    assert m["primary"] == "NEW owner directive", \
        "an acknowledgement must never outrank the owner's directive"
    assert m["set_at"] == "2026-08-01T00:00:00"
    assert sorted(m["acknowledged_by"]) == ["alpha", "bravo"], \
        "and every acknowledgement must still survive the union"


# --- whole-sub-document scope of the ack exemption (restored) --------------
# These three were dropped by evil merge b9ef9676 ("take the peer Body's
# allowlist over mine for "), which resolved this file by taking
# parent-2 wholesale: merge blob == ^2 exactly (17279 B), discarding ^1's
# extra 5838 B. Every other function it dropped came back under a new name;
# these did not, and `subdocument` scored 0 hits at HEAD. Restored verbatim
# from b9ef9676^1 by the  evil-merge audit. The merge also dropped
# the `STALE` constant these referenced; inlined to the literal here because
# that is what the other 26 uses in this file do (no competing convention).
def test_whole_subdocument_write_of_only_acks_still_bumps(world: Path):
    """THE SCOPE BOUNDARY, and it is the opposite of what it looks like.

    `--field strategic_focus` takes a whole dict, so a payload carrying nothing
    but `acknowledged_by` LOOKS like the same non-amendment said another way. It
    is not: `_set_nested` REPLACES the sub-document, so that write DELETES
    primary, rationale, set_by and set_at. This test was originally written to
    assert the stamp was preserved and failed with a bare `KeyError: 'set_at'`,
    which is what established the behaviour — a directive WIPE is maximally
    content-changing, and exempting it would hand a brand-new sub-document a
    stale inherited stamp. The exemption is therefore dot-path-only."""
    assert _run(world, "update", "--field", "strategic_focus.set_at",
                "--value", "2026-07-04T13:45:00").returncode == 0
    import json as _json
    assert _run(world, "update", "--field", "strategic_focus",
                "--value", _json.dumps({"acknowledged_by": ["alpha"]})
                ).returncode == 0
    f = _read_focus(world)
    assert "primary" not in f, \
        "fixture assumption: a whole-map write replaces rather than merges"
    assert f["set_at"] > "2026-07-04T13:45:00", "a directive wipe must bump the stamp"


def test_whole_subdocument_write_carrying_content_still_bumps(world: Path):
    """POSITIVE CONTROL for the dict form (guard-4166): the same write shape
    with one content key present must still bump."""
    assert _run(world, "update", "--field", "strategic_focus.set_at",
                "--value", "2026-07-04T13:45:00").returncode == 0
    import json as _json
    assert _run(world, "update", "--field", "strategic_focus",
                "--value", _json.dumps({"acknowledged_by": ["alpha"],
                                        "primary": "new directive"})).returncode == 0
    assert _read_focus(world)["set_at"] > "2026-07-04T13:45:00"


def test_empty_whole_subdocument_write_still_bumps(world: Path):
    """`--value {}` REPLACES the sub-document with nothing — the most
    destructive write available on this field. Pinned alongside the ack-only
    whole-map case above so the dot-path-only scope of the exemption is covered
    from both ends of the payload spectrum (empty and ack-only)."""
    assert _run(world, "update", "--field", "strategic_focus.set_at",
                "--value", "2026-07-04T13:45:00").returncode == 0
    assert _run(world, "update", "--field", "strategic_focus",
                "--value", "{}").returncode == 0
    assert _read_focus(world)["set_at"] > "2026-07-04T13:45:00"


# --- the DAEMON arm of the same predicate (, carried) -------------
#
# Everything above drives `core/scripts/team-state.py`. That is the MIRROR, not
# the live path: `team-state-update.sh` is daemon-only (`rt_call POST
# /v1/team-state/update`), so `mind_api/src/world/team_state_write.py` is what
# actually runs in the fleet. The  fix correctly edited BOTH copies —
# but with the CLI arm alone under test, a revert of the daemon copy would ship
# green. That is not hypothetical for this exact code block: guard-2323 is
# recorded against it because the  bump ORIGINALLY landed CLI-side
# only and was inert in a daemon-only deployment for as long as nobody looked.
#
# `test_daemon_cli_mirror_parity.py` does not close this: it asserts EMPTY_STATE
# field-sets, not writer behaviour, so the predicate can diverge under a green
# parity suite.

def _daemon_update(world: Path, field: str, value: str, operation: str = "set"):
    """Drive the daemon writer in-process against a tmp world (the `_FakeCtx`
    shape used by mind_api/tests/test_runtime_team_state_write.py)."""
    sys.path.insert(0, str(CORE_SCRIPTS.parents[0]))
    from mind_api.src.world import team_state_write

    class _P:
        def __init__(self, w):
            self.world = self.meta = self.agent = w

    class _C:
        def __init__(self, w):
            self.paths = _P(w)
            self.query = {"field": field, "value": value, "operation": operation}
            self.body = b""
            self.headers = {"x-mind-agent": "alpha"}

    return team_state_write.update(_C(world))


def _daemon_focus(world: Path) -> dict:
    return (yaml.safe_load((world / "team-state.yaml").read_text(encoding="utf-8"))
            or {}).get("strategic_focus") or {}


def test_daemon_writer_does_not_bump_set_at_on_an_ack(tmp_path: Path):
    """THE defining property, on the path that actually runs."""
    w = tmp_path / "dae"
    w.mkdir()
    _daemon_update(w, "strategic_focus.set_at", "2026-07-04T13:45:00")
    _daemon_update(w, "strategic_focus.acknowledged_by", '"alpha"', "append")
    f = _daemon_focus(w)
    assert f["acknowledged_by"] == ["alpha"], "the ack itself must still land"
    assert f["set_at"] == "2026-07-04T13:45:00", \
        "daemon copy must not re-stamp the directive's provenance on an ack"


def test_daemon_writer_still_bumps_on_directive_content(tmp_path: Path):
    """Anti-vacuity twin for the daemon arm — the positive control that must NOT
    flip (guard-4166). Without it, a daemon copy that stopped stamping entirely
    would satisfy the test above while reintroducing g-115-5294."""
    w = tmp_path / "dae2"
    w.mkdir()
    _daemon_update(w, "strategic_focus.set_at", "2026-07-04T13:45:00")
    _daemon_update(w, "strategic_focus.primary", "amended directive")
    assert _daemon_focus(w)["set_at"] > "2026-07-04T13:45:00", \
        "daemon copy must still bump on directive content"


@pytest.mark.parametrize("field,value,operation,should_bump", [
    ("strategic_focus.acknowledged_by", '"alpha"', "append", False),
    ("strategic_focus.primary", "amended", "set", True),
    ("strategic_focus.rationale", "because", "set", True),
    ("strategic_focus.set_by", "zachary", "set", True),
    ("strategic_focus", '{"primary": "whole map"}', "set", True),
])
def test_daemon_and_cli_agree_on_the_bump_predicate(
        tmp_path: Path, field: str, value: str, operation: str, should_bump: bool):
    """The DRIFT class, pinned directly (guard-742 / guard-547 / guard-2323).

    Two hand-maintained copies of one predicate diverge silently, and nothing
    fails when they do — which is why this is asserted rather than trusted. Both
    arms are driven with the SAME field/operation matrix and must return the
    same verdict; `should_bump` is carried so the matrix cannot degrade into
    "they agree because neither ever bumps".
    """
    stale = "2026-07-04T13:45:00"
    cli_w = tmp_path / "cli"
    cli_w.mkdir()
    assert _run(cli_w, "init").returncode == 0
    assert _run(cli_w, "update", "--field", "strategic_focus.set_at",
                "--value", stale).returncode == 0
    args = ["update", "--field", field, "--value", value]
    if operation != "set":
        args += ["--operation", operation]
    assert _run(cli_w, *args).returncode == 0
    cli_bumped = _read_focus(cli_w).get("set_at", "") > stale

    dae_w = tmp_path / "dae"
    dae_w.mkdir()
    _daemon_update(dae_w, "strategic_focus.set_at", stale)
    _daemon_update(dae_w, field, value, operation)
    dae_bumped = _daemon_focus(dae_w).get("set_at", "") > stale

    assert cli_bumped == dae_bumped, (
        f"CLI/daemon predicate DRIFT on {field!r} ({operation}): "
        f"cli bumped={cli_bumped}, daemon bumped={dae_bumped}")
    assert cli_bumped is should_bump, (
        f"{field!r} ({operation}) should_bump={should_bump} but both copies "
        f"returned {cli_bumped} — the matrix has gone vacuous or the "
        f"predicate changed meaning")
