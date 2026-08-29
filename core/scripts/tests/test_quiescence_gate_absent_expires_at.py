""": a blocker_ref with NO expires_at must DISQUALIFY quiescence.

THE DEFECT (quiescence-gate.py C2/C3, pre-fix):

    exp = ref.get("expires_at")
    if exp:
        ... treat as expired when in the past ...

An ABSENT expires_at fell through the `if exp:` and was silently treated as
"not yet expired", so the ref passed C3. Consequences, both silent:

  1. goal-schemas.md promises "On expiry the blocker auto-converts to an
     Unblock goal via aspirations-precheck Phase 0.5b re-probe, disqualifying
     quiescence." With no expires_at there is no expiry, so that conversion
     can never fire.
  2. The queue looks legitimately gated INDEFINITELY and qualifies for long
     quiescent sleep, with no TTL that can ever break it — precisely the
     narrative-laundering the blocker_ref requirement exists to prevent,
     reachable by omitting a field the schema calls automatic.

WHY ABSENCE MUST FAIL CLOSED (guard-487): the quiescence gate is a
SUPPRESSION gate — it approves sleep, suppressing loop work. A suppression
gate must fail CLOSED when its input cannot establish the fact it needs.
Contrast guard-142, which puts BLOCKING gates on the fail-OPEN side; the
quiescence gate is not one. The pre-fix code already treated an UNPARSEABLE
expires_at as expired, so "cannot confirm liveness -> disqualify" was already
this block's posture. Absence was the one case that slipped through, not a
deliberate exemption — which is why this is a bug fix and not a policy change.

HOW THE BAD REFS GET WRITTEN (measured 2026-07-27): auto-population lives in
gates/blocker_ref.validate(), reached ONLY via the --blocker-ref flag paired
with a defer_reason / status=blocked write. A DIRECT
`update-goal <id> blocker_ref '<json>'` field write lands verbatim at
aspirations.py:1905 with no validation and no TTL. 7 of 11 live blocked goals
carried a ref with no expires_at, and only 1 of 11 matched validate()'s exact
output shape. This gate is the READ-side backstop.

Run: py -3 -m pytest core/scripts/tests/test_quiescence_gate_absent_expires_at.py -v
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import quiescence-gate.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "quiescence_gate_under_test", SCRIPT_DIR / "quiescence-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Args:
    """Minimal stand-in for the argparse Namespace cmd_check dereferences."""

    def __init__(self, **kw):
        self.all_blocked = True
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, name):  # any unset flag reads as falsy, not AttributeError
        return None


def _entry(goal_id, ref):
    return {"goal_id": goal_id, "blocker_ref": ref}


def _run_check(monkeypatch, tmp_path, entries):
    """Drive the REAL cmd_check to its denial output.

    Patches only the I/O boundary (working memory, the blocked-goal source,
    and the log append). The C2/C3 classification under test is the genuine
    production code path, not a re-implementation — guard-920: a regression
    test must exercise the literal production shape.
    """
    qg = load_module()
    monkeypatch.setattr(qg, "_wm_read_loop_state", lambda: {})
    monkeypatch.setattr(qg, "_collect_blocked_entries", lambda: entries)
    monkeypatch.setattr(qg, "_known_blockers", lambda: [])
    monkeypatch.setattr(qg, "_append_log", lambda *a, **k: None)
    monkeypatch.setattr(qg, "_total_goal_count", lambda: 10)
    monkeypatch.setattr(qg, "_read_prolonged_pinged", lambda: set())
    monkeypatch.setattr(qg, "_write_prolonged_pinged", lambda *a, **k: None)
    monkeypatch.setattr(qg, "_write_cycle_cache", lambda *a, **k: None)
    monkeypatch.setattr(qg, "AGENT_DIR", tmp_path)

    cfg = qg._load_config()
    with pytest.raises(SystemExit) as exc:
        qg.cmd_check(_Args(), cfg)
    out = json.loads(_capsys_text())
    return exc.value.code, out


# capsys is function-scoped; stash the fixture's reader here so _run_check can
# stay a plain helper rather than threading capsys through every call.
_CAPSYS = {}


def _capsys_text():
    return _CAPSYS["read"]().out.strip().splitlines()[-1]


@pytest.fixture(autouse=True)
def _bind_capsys(capsys):
    _CAPSYS["read"] = capsys.readouterr
    yield
    _CAPSYS.clear()


def _c3(out):
    """Return the C3 denial reason dict, or None when C3 did not fire."""
    for r in out.get("reasons") or []:
        if r.get("condition") == "C3_blocker_ref_future_expiry":
            return r
    return None


CANONICAL_KEYS = {"type", "external_id", "state_hash", "created_at", "expires_at"}


def test_absent_expires_at_disqualifies(monkeypatch, tmp_path):
    """THE REGRESSION. A ref with no expires_at must fire C3.

    Pre-fix this ref passed C3 silently and the queue qualified for sleep.
    """
    ref = {"type": "user_action", "external_id": "pq-example-01",
           "state_hash": None}
    code, out = _run_check(monkeypatch, tmp_path,
                           [_entry("g-000-01", ref)])
    r = _c3(out)
    assert r is not None, (
        "absent expires_at did NOT disqualify — the g-115-3505 regression is "
        f"back. reasons={out.get('reasons')}"
    )
    assert r["missing_expires_at_count"] == 1
    assert r["past_expiry_count"] == 0
    assert r["sample"][0]["missing_expires_at"] is True
    assert r["sample"][0]["expired_at"] is None
    assert code != 0, "a disqualified queue must not be approved for sleep"


@pytest.mark.parametrize("empty", [None, "", 0])
def test_falsy_expires_at_also_disqualifies(monkeypatch, tmp_path, empty):
    """null / "" / 0 are absence too — they carry no liveness evidence either.

    The pre-fix `if exp:` treated every one of these as unexpired.
    """
    ref = {"type": "infrastructure", "external_id": "svc-1",
           "expires_at": empty}
    _, out = _run_check(monkeypatch, tmp_path, [_entry("g-000-02", ref)])
    r = _c3(out)
    assert r is not None and r["missing_expires_at_count"] == 1


def test_future_expires_at_still_passes_c3(monkeypatch, tmp_path):
    """NEGATIVE CONTROL. A genuinely live TTL must NOT be disqualified.

    Without this, a fix that simply always-fires C3 would pass the test above
    while breaking every legitimate quiescent sleep.
    """
    future = (datetime.now() + timedelta(hours=48)).isoformat(timespec="seconds")
    ref = {"type": "user_action", "external_id": "pq-live-01",
           "expires_at": future}
    _, out = _run_check(monkeypatch, tmp_path, [_entry("g-000-03", ref)])
    assert _c3(out) is None, (
        "a future expires_at was disqualified — the fix over-fires and would "
        "deny every legitimate quiescent sleep"
    )


def test_past_expires_at_still_disqualifies(monkeypatch, tmp_path):
    """The pre-existing behavior must survive the change."""
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    ref = {"type": "user_action", "external_id": "pq-stale-01",
           "expires_at": past}
    _, out = _run_check(monkeypatch, tmp_path, [_entry("g-000-04", ref)])
    r = _c3(out)
    assert r is not None
    assert r["past_expiry_count"] == 1
    assert r["missing_expires_at_count"] == 0


def test_unparseable_expires_at_still_disqualifies(monkeypatch, tmp_path):
    """Unparseable was already disqualifying — and must not be recounted as
    missing OR as past. THREE buckets, three follow-up actions.

    UPDATED DELIBERATELY (g-115-3537), not deleted. This test previously
    asserted past_expiry_count == 1 for an unparseable value, which CODIFIED
    the conflation the goal exists to remove: n_past was computed as
    len(expired_ref) - n_missing, so every parse_error landed in "expires_at
    in the past" — a claim about a value that was never compared to anything.
    The assertion is now inverted: past MUST be 0 and parse_error MUST be 1.
    """
    ref = {"type": "user_action", "external_id": "pq-junk-01",
           "expires_at": "not-a-timestamp"}
    _, out = _run_check(monkeypatch, tmp_path, [_entry("g-000-05", ref)])
    r = _c3(out)
    assert r is not None
    assert r["missing_expires_at_count"] == 0
    assert r["past_expiry_count"] == 0, (
        "an UNPARSEABLE expires_at was never compared, so nothing is known "
        f"about whether it is past — it must not be counted there: {r!r}")
    assert r["parse_error_count"] == 1, r
    assert r["sample"][0].get("parse_error") is True
    assert "UNPARSEABLE" in r["detail"], (
        f"the detail line must name the shape, not describe it as past: {r!r}")


def test_tz_aware_expires_at_is_compared_not_swallowed(monkeypatch, tmp_path):
    """guard-4372: a tz-aware stamp must PARSE, not degrade into parse_error.

    This is the case the goal was filed for. `datetime.fromisoformat` accepts
    "+00:00" fine and then raises TypeError on the comparison to a naive now(),
    which the fail-open `except` swallowed — so a tz-aware FUTURE expiry (a
    perfectly valid, unexpired ref) was disqualified and mislabelled "in the
    past". parse_naive_iso strips the offset at the parse boundary, so the
    comparison happens for real.

    A tz-aware value arrives via the unvalidated direct-field write path
    (aspirations.py) that produced 7 of 11 live blocked refs, so this is the
    reachable shape, not a hypothetical.
    """
    future = ((datetime.now() + timedelta(hours=48))
              .isoformat(timespec="seconds") + "+00:00")
    ref = {"type": "user_action", "external_id": "pq-tz-01", "expires_at": future}
    _, out = _run_check(monkeypatch, tmp_path, [_entry("g-000-06", ref)])
    r = _c3(out)
    assert r is None, (
        "a tz-aware FUTURE expiry is unexpired and must not disqualify at all; "
        f"C3 fired, so the comparison was still being swallowed: {r!r}")


def test_tz_aware_PAST_expiry_is_counted_as_past_not_parse_error(monkeypatch, tmp_path):
    """The other half: tz-aware must not become a free pass either.

    Without this, a fix that routed every tz-aware value into parse_error would
    pass the test above while silently disabling the expiry check — the same
    swallowed-comparison defect wearing a different bucket.
    """
    past = ((datetime.now() - timedelta(hours=2))
            .isoformat(timespec="seconds") + "+00:00")
    ref = {"type": "user_action", "external_id": "pq-tz-02", "expires_at": past}
    _, out = _run_check(monkeypatch, tmp_path, [_entry("g-000-07", ref)])
    r = _c3(out)
    assert r is not None, f"a tz-aware PAST expiry must still disqualify: {out!r}"
    assert r["past_expiry_count"] == 1, r
    assert r["parse_error_count"] == 0, (
        f"it parsed and compared cleanly — this is not a parse error: {r!r}")


def test_parse_error_count_DISCRIMINATES_from_past(monkeypatch, tmp_path):
    """guard-5163: prove the new field differs between the two conflated cases.

    A discriminator added in good faith that happens to take the same value on
    both branches is decoration — the ambiguity survives behind a field that
    now reads as a fix. So: build a fixture for EACH case, assert the SHARED
    old counter cannot tell them apart, and assert the NEW field does.
    """
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    _, out_past = _run_check(
        monkeypatch, tmp_path,
        [_entry("g-000-08", {"type": "user_action", "external_id": "pq-p",
                             "expires_at": past})])
    _, out_junk = _run_check(
        monkeypatch, tmp_path,
        [_entry("g-000-09", {"type": "user_action", "external_id": "pq-j",
                             "expires_at": "not-a-timestamp"})])
    rp, rj = _c3(out_past), _c3(out_junk)
    assert rp is not None and rj is not None
    # Both disqualify identically on the OLD observable — that is the ambiguity.
    assert len(rp["sample"]) == len(rj["sample"]) == 1
    # The NEW field is what separates them.
    assert (rp["past_expiry_count"], rp["parse_error_count"]) == (1, 0), rp
    assert (rj["past_expiry_count"], rj["parse_error_count"]) == (0, 1), rj


def test_missing_and_past_are_reported_separately(monkeypatch, tmp_path):
    """The detail line must not describe an absent TTL as "in the past".

    The reader's next action differs: a past TTL means re-probe the blocker;
    an absent one means the ref was written through the unvalidated field
    path and needs normalizing.
    """
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    entries = [
        _entry("g-000-06", {"type": "user_action", "external_id": "a"}),
        _entry("g-000-07", {"type": "user_action", "external_id": "b",
                            "expires_at": past}),
    ]
    _, out = _run_check(monkeypatch, tmp_path, entries)
    r = _c3(out)
    assert r is not None
    assert r["missing_expires_at_count"] == 1
    assert r["past_expiry_count"] == 1
    assert "NO expires_at" in r["detail"]
    assert "in the past" in r["detail"]


def test_non_canonical_vocabulary_refs_are_caught(monkeypatch, tmp_path):
    """The live variant shapes measured 2026-07-27 must all disqualify.

    These are the refs that prove the write path bypasses validate(): the
    validator returns EXACTLY CANONICAL_KEYS, so a ref carrying `ref` /
    `why` / `blocker_type` / `blocking_goal` cannot have been through it.
    All lack expires_at, so all must now fire C3 rather than silently
    gating the queue forever.
    """
    variants = [
        {"type": "user_action", "ref": "pq-fox-ppe-plugin-toggle",
         "why": "user-gated plugin toggle"},
        {"blocker_type": "credentials-required", "blocking_goal": "g-000-99",
         "denied_action": "s3:DeleteObjectVersion", "principal": "fleet-agent"},
        {"type": "infrastructure", "external_id": "svc-x", "why": "down"},
    ]
    for i, ref in enumerate(variants):
        assert not (set(ref) >= CANONICAL_KEYS), (
            "fixture drifted: this shape would have passed validate()"
        )
        _, out = _run_check(monkeypatch, tmp_path,
                            [_entry(f"g-000-1{i}", ref)])
        r = _c3(out)
        assert r is not None and r["missing_expires_at_count"] == 1, (
            f"variant {i} ({sorted(ref)}) did not disqualify"
        )


def test_validate_output_always_carries_expires_at():
    """Cross-check the WRITE side: anything through validate() is safe here.

    Pins the invariant this gate's fix depends on — if validate() ever stops
    auto-populating expires_at, every ref it produces would start tripping C3
    and quiescence would be denied fleet-wide. That failure should surface as
    this named test, not as a mysterious sleep outage.
    """
    from gates.blocker_ref import BLOCKER_REF_TYPES, validate

    for btype in BLOCKER_REF_TYPES:
        ok, ref = validate({"type": btype, "external_id": "x"})
        assert ok, ref
        assert ref.get("expires_at"), f"{btype} produced no expires_at"
        assert set(ref) == CANONICAL_KEYS, (
            f"{btype} output shape drifted: {sorted(ref)}"
        )
