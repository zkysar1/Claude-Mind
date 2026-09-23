"""requires_capability is gated on the UPDATE path, not only at the ADD sites ().

THE DEFECT. g-115-8826 built `gates/capability_vocab.py` for this exact class and
wired it at the two ADD sites only. Its docstring says why, and the reasoning is
correct: `update_goal` validates its in-lock candidate through `_validate_goal`,
so a check THERE would wedge status changes on any legacy carrier that arrived by
merge from another box — and those carriers are precisely the rows a reader must
still be able to EDIT to unstick them. The unstated consequence was that the
UPDATE path ran no vocabulary check at all, so a value written onto an EXISTING
goal fenced it fleet-wide. The gate was not failing; it was never on this road.

WHY THAT IS EXPENSIVE. An off-contract token is not a low score. It is REMOVAL
from the ranked candidate pool on every box in the fleet, permanently, with no
error printed anywhere: `goal_is_locally_executable` is a plain subset test, and
no runner can ever declare an off-contract token.

MEASURED (alpha, cc-04, 2026-09-21, one variable at a time). Census over 3035
non-terminal goals: 32 carried the field; five distinct VALID tokens were in live
use, which is the positive control that the reader was not returning empty for
everything; exactly TWO carried an unknown token. Both were HIGH-priority
multi-paragraph PROSE, and both were written by UPDATE days after the goal was
filed — g-369-222 (885 chars, in a directive-boosted lane) and g-115-10161 (1317
chars). Flipping ONLY that field to null moved `goal_is_locally_executable` from
False to True on each.

THE PARAMETRISED VALUES BELOW ARE THE REAL ONES, not invented shapes: every one
of them was writable on this path before this gate existed.
"""
import pathlib
import sys
import tempfile

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_SRC.parent))

_CORE = pathlib.Path(__file__).resolve().parents[2] / "core" / "scripts"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from src.endpoints.aspirations_write import _run_update_goal_gates  # noqa: E402

import _runner_capabilities as rc  # noqa: E402


# NEVER `pathlib.Path(".")` HERE. The gate under test calls _gate_log.log, which
# resolves its telemetry destination as `dest = (meta_dir if meta_dir is not None
# else META_DIR)` and writes `gate-firings.spool.jsonl` (or a date segment) there —
# so a relative "." meta aims the write at the process cwd, i.e. THE REPO ROOT.
#
# WHY THAT MATTERS EVEN THOUGH THIS FILE HAS NEVER TRIGGERED IT, stated precisely
# because the tempting version of this comment is a causal claim I could not
# support. _gate_log suppresses every write when PYTEST_CURRENT_TEST is set unless
# GATE_LOG_ALLOW_PYTEST is also set (_gate_log.py:275), and this file sets neither —
# VERIFIED 2026-09-21 by running the 16 tests with the stub pointed at a fresh tmp
# dir and finding that dir EMPTY afterwards. So under pytest this stub is inert.
#
# What IS measured is the cost when something on this code path does write to ".":
# a 437-byte one-record `gate-firings.spool.jsonl` sat at the repo root on cc-04,
# `check-repo-root-entries` correctly REFUSED the commit that would have added a new
# top-level entry, the refusal left the index staged, and `iteration-push` defers on
# a staged index — stranding the box's push lane for 3 consecutive iterations
# (behind=34, ahead=3) until someone read
# `agents/<agent>/session/commit-refused.json`. That record was NOT written by this
# file (its `agent` and goal-id fields match nothing in the test tree, and pytest
# suppression rules the whole tree out); its true writer is filed separately.
# The point stands regardless of who wrote that one: a stub that aims a production
# writer at the repo root is one `GATE_LOG_ALLOW_PYTEST=1` away from doing it, and
# the blast radius is the whole box's ability to push.
_TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="cap-vocab-gate-test-"))


class _StubPaths:
    project_root = _TMP_ROOT
    world = _TMP_ROOT
    meta = _TMP_ROOT
    agent_name = "test-agent"


class _StubCtx:
    """Minimal ctx. The vocabulary gate fires before any live fleet state is
    read, so a stub keeps the control from depending on this box's queue."""
    paths = _StubPaths()
    headers: dict = {}


def _gate(value, field="requires_capability", goal_id="g-000-01"):
    return _run_update_goal_gates(_StubCtx(), goal_id, field, value)


def _body(resp):
    """Response.json() serialises to BYTES, not a dict — reading `.body` as a
    mapping silently yields {} and every content assertion then passes vacuously
    against a gate that never fired. Decode explicitly."""
    if resp is None:
        return {}
    raw = getattr(resp, "body", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            import json
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


# --- the block direction --------------------------------------------------

@pytest.mark.parametrize("bad", [
    # The two live carriers, truncated to their opening clause. Both are prose.
    "live env-server locus: outcomes 1 and 2 both need a running DEV "
    "per-character env-server, which this box does not host.",
    "cc-04 host locus. Outcome 4's residual artifacts are 9 "
    "conflict-persistent files on this box's own-cloud mirror.",
    # The gate module's own three prior instances, from its docstring.
    "win-32-typo",
    "live runner claim for agent dir foxtrot",
    "vinheim-operator-api-key",
    # A mixed list — one good token does not launder the bad one.
    ["aws", "bogus-token"],
])
def test_off_contract_value_is_refused_on_the_update_path(bad):
    """REACHABLE RED. Each of these was writable here before this gate existed."""
    resp, ref, cap = _gate(bad)
    assert resp is not None, f"{bad!r} was admitted — the gate did not fire"
    assert resp.status == 400
    assert _body(resp).get("error") == "unknown_capability_token"


def test_refusal_is_asserted_BY_MESSAGE_CONTENT_not_by_the_id_we_passed():
    """guard-7202: verify a write by a field that is uniquely YOURS, never by the
    id you just supplied. `goal_id` and `field` are our own arguments echoed back,
    so neither can distinguish a real refusal from a stub. The DETAIL is the only
    thing the gate itself composes — assert on that."""
    resp, _ref, _cap = _gate("live env-server locus: needs a running DEV box")
    detail = _body(resp).get("detail") or ""

    # (a) it names the offending value
    assert "live env-server locus" in detail

    # (b) it names every valid token — an author cannot fix a token without
    #     being told what IS valid
    for tok in rc.KNOWN_CAPABILITIES:
        assert tok in detail, f"valid token {tok!r} missing from the refusal"

    # (c) it names the sanctioned homes for a LOCUS constraint. This is the half
    #     that makes the gate route instead of merely refuse: both measured
    #     authors were recording a real locus, and a refusal that only lists the
    #     seven tokens sends the next author to the next ungated field.
    assert "STRUCTURED PRECONDITION" in detail
    assert "prefix:snake_token" in detail        # guard-4310's declarative head
    assert "SELECTABLE ROUTING" in detail
    assert "intended_agent" in detail


# --- the pass direction (the half that would do fleet-wide damage) ---------

@pytest.mark.parametrize("good", [
    "aws",                 # bare string — the most natural single-token form
    ["aws"],
    ["git-push", "aws"],
    ["studio-session"],    # NEVER_AUTO_PROVIDED but LEGITIMATE — must not refuse
])
def test_valid_token_still_writes(good):
    resp, _ref, _cap = _gate(good)
    assert _body(resp).get("error") != "unknown_capability_token", (
        f"{good!r} is a valid capability and must not be refused")


@pytest.mark.parametrize("clearing", [None, "", []])
def test_clearing_stays_open(clearing):
    """The unstick path. Both live carriers were legitimately retired by clearing
    this field; refusing a clear would wedge the exact repair this gate protects."""
    resp, _ref, _cap = _gate(clearing)
    assert _body(resp).get("error") != "unknown_capability_token"


def test_the_gate_is_FIELD_scoped_not_RECORD_scoped():
    """THE LOAD-BEARING CONTROL, and the reason this check is here rather than in
    `_validate_goal`. A legacy carrier — a goal that ALREADY holds an off-contract
    token, arrived by merge from another box — must stay editable, or the gate
    wedges the rows it exists to free. The check evaluates the INCOMING VALUE as a
    synthetic one-field goal, never the stored record, so a status write on such a
    row never reaches this branch at all."""
    for f, v in (("status", "in-progress"), ("status", "pending"),
                 ("priority", "HIGH"), ("progress_note", "unsticking this row")):
        resp, _ref, _cap = _gate(v, field=f)
        assert _body(resp).get("error") != "unknown_capability_token", (
            f"a {f} write was refused by the capability-vocab gate — the check is "
            f"record-scoped, which wedges every legacy carrier")


def test_gate_fails_open_when_the_vocabulary_cannot_be_resolved(monkeypatch):
    """rb-1028. A gate that cannot read its own vocabulary must never refuse every
    capability-tagged write fleet-wide — that direction is where it would do real
    damage. Forced here by making the evaluator itself raise."""
    import src.endpoints.aspirations_write as aw

    def boom(*a, **kw):
        raise RuntimeError("simulated vocabulary failure")

    monkeypatch.setattr(aw, "_capability_vocab_eval", boom)
    resp, _ref, _cap = _gate("definitely-not-a-capability")
    assert _body(resp).get("error") != "unknown_capability_token"
