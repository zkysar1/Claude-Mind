""" — `update-goal` must refuse a non-dict blocker_ref at the WRITE.

THE DEFECT. `add-goal` routes its blocker_ref through `gates.blocker_ref.validate`,
which refuses a non-dict outright. The generic field-update path did not: it wrote
whatever the wrapper's `parse_value` mirror encoded, so
`aspirations-update-goal.sh <g> blocker_ref "<anything>"` stored a bare string.

WHY A SCALAR IS UNREADABLE RATHER THAN MERELY UGLY. The read-side guards test
`isinstance(br, dict) and br.get("type")`, so a scalar is SKIPPED, not flagged —
it never surfaces as malformed. And no `expires_at` can be stored on a string, so
the TTL that would force a re-probe never arms. The value gates work silently and
indefinitely, which is the whole harm.

WHY THIS IS A WRITE-PATH TEST AND NOT A BACKFILL. The population is tiny and
SELF-CLEARING — a blocker_ref disappears when its goal unblocks or completes — so
every sweep reports "exactly ONE bare string" and looks stable while naming a
DIFFERENT record each time: g-335-228 (2026-07-29), g-335-902 (2026-08-07),
g-326-105 (2026-08-11, a multi-sentence prose narrative, a third distinct
malformed shape). A count that stays arithmetically true while its referent is
replaced is invisible to exactly the check a careful reader would run ("is it
still 1?"). Backfilling the named record closes the tracker and leaves the writer
live, so the residue regenerates — which is what happened twice.

WHY THE TEST TARGETS THE DAEMON AND NOT `core/scripts/aspirations.py`. This
framework is daemon-only (`.claude/rules/no-python-cli-fallback.md`):
`aspirations-update-goal.sh` reaches `rt_call POST /v1/aspirations/update-goal`,
so the CLI `cmd_update_goal` is not on the production path. The CLI twin carries
the same refusal (guard-2323), but a test that exercised only the CLI copy would
pass while the defect stayed fully open (guard-742).
"""
import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_SRC.parent))

from src.endpoints.aspirations_write import _run_update_goal_gates  # noqa: E402


class _StubPaths:
    project_root = pathlib.Path(".")
    world = pathlib.Path(".")
    agent_name = "test-agent"


class _StubCtx:
    """Minimal ctx. The shape gate fires before any ctx attribute is read, so a
    stub is sufficient for the red case AND keeps the control from depending on
    live fleet state."""
    paths = _StubPaths()
    headers: dict = {}


# Every scalar shape actually observed on disk, plus the empty-ish neighbours of
# each. `""` is NOT here: it is a legitimate CLEAR (see the control below).
@pytest.mark.parametrize("bad", [
    "g-335-935",                                  # 's bare goal-id
    "pq-fox-vinheim-chardef-authoring",           # 's bare pq name
    "BLOCKED ON AN OBSERVABLE, NOT ON A DECISION. The remaining work in this "
    "goal is the section that ...",               # 's prose narrative
    0,
    1,
    42,
    True,
    3.5,
    ["g-335-935"],                                # a list is not a dict either
])
def test_non_dict_blocker_ref_is_refused(bad):
    """REACHABLE RED. Each of these was writable before this gate existed."""
    resp, ref, cap = _run_update_goal_gates(
        _StubCtx(), "g-000-01", "blocker_ref", bad)
    assert resp is not None, f"{bad!r} was admitted — the gate did not fire"
    assert resp.status == 400
    body = resp.body if isinstance(resp.body, dict) else {}
    payload = body if body else {}
    assert "blocker_ref_shape" in str(getattr(resp, "body", "")) or \
           payload.get("error") == "blocker_ref_shape"
    assert ref is None and cap is None


# POSITIVE CONTROLS (guard-2421). A gate that refuses EVERYTHING passes the red
# case above while breaking every legitimate write, and the red case alone cannot
# tell the two apart. These are the writes that MUST still go through.
@pytest.mark.parametrize("ok", [
    {"type": "partner-response", "external_id": "msg-20260808-124457-zeta-6480"},
    {"type": "credentials-required", "external_id": "aws-exec:g-335-1103"},
    {},        # empty dict — an explicit no-op, not a malformed value
    None,      # `null` — the CLEAR path, how every prior instance was retired
    "",        # empty string — the other CLEAR spelling the wrapper can emit
])
def test_dict_and_clear_shapes_pass_the_shape_gate(ok):
    """The clear path is load-bearing: refusing it would break normal unblock."""
    resp, _ref, _cap = _run_update_goal_gates(
        _StubCtx(), "g-000-01", "blocker_ref", ok)
    assert resp is None, (
        f"{ok!r} was refused — the shape gate is over-broad and would break "
        f"the unblock path")


def test_gate_is_scoped_to_the_blocker_ref_field():
    """A scalar on ANY OTHER field is none of this gate's business — scoping it
    to one field is what keeps the refusal from becoming a general type policy
    nobody asked for (implementation-discipline.md rule 1)."""
    resp, _ref, _cap = _run_update_goal_gates(
        _StubCtx(), "g-000-01", "origin_signal", "a bare string is fine here")
    assert resp is None
