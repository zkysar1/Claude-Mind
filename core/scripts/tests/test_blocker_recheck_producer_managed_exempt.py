""": producer-managed blockers are NEVER auto-cleared by the
capability-gate recheck.

INCIDENT (foxtrot, cc-04, 2026-07-30). `blocker-recheck.sh --apply` runs every
iteration at aspirations-precheck Phase 0.5b.0.5. Against the live
`streak-roblox-studio` blocker it reported match_count=10,
top_match=access-roblox-studio, matched_keyword='connect' and — at 14:23:26 —
actually cleared it, filing a false Investigate (g-115-4083, "capability
'access-roblox-studio' missed at blocker creation"). At that moment the
canonical probe reported doctor_verdict=relay-dead with 28 consecutive
failures. Owning the access-roblox-studio skill does not repair a black-holed
localhost relay.

WHY IT REACHED THE KEYWORD PATH — both pre-existing structural filters miss,
and that is exactly why a third filter was needed rather than a new entry in
either one:
  - HUMAN_ONLY_BLOCKER_TYPES is keyed on b["type"]; streak blockers have NO
    `type` field at all, so the test sees None.
  - is_user_routed treats an ABSENT `participants` list as user-routed (a
    deliberate allowance for legacy blockers); streak blockers have none.

THE FIX is a structural exemption, not a probe consultation. The producer
already owns these blockers' whole lifecycle — infra-health.py::
_sync_known_blockers re-derives every streak-* entry from scratch on each sync
and simply does not re-add one whose component recovered ("Recovery is
automatic"). So this script clearing them is never NEEDED, and is destructive
when the condition is still live.

The deeper reason is a CATEGORY ERROR. The recheck asks "was an
agent-provisionable capability OVERLOOKED at blocker creation time?" — a
question about a decision CREATE_BLOCKER Step 2.5 made. A producer-emitted
blocker never went through CREATE_BLOCKER, so there is no creation-time
decision to have been wrong. That is why g-115-4083 was unanswerable: it
presupposed a step that never ran.

ASYMMETRY that makes fail-closed correct here: a wrong CLEAR writes
`resolution` onto known_blockers, and BOTH the Phase 0.5b re-probe loop AND
the proactive-escalation path iterate known_blockers — so a live outage goes
invisible to each. A wrong NON-clear costs nothing: the producer drops the
entry on the next sync after recovery.

Run: py -3 -m pytest core/scripts/tests/test_blocker_recheck_producer_managed_exempt.py -v
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import blocker-recheck.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "blocker_recheck_module_pm",
        SCRIPT_DIR / "blocker-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


def real_streak_blocker():
    """The LITERAL production record shape.

    Copied field-for-field from infra-health.py::_sync_known_blockers (the sole
    construction site). Deliberately NOT a contract-ideal blocker: it carries no
    `type` and no `participants`, because their ABSENCE is what defeats both
    existing filters. A tidied-up fixture that added either field would pass
    against the buggy code and prove nothing (guard-920 — replicate the literal
    production shape, not the one the contract describes).

    The `reason` narrative is the real one, and it is what the capability gate
    keyword-matches on ('connect' inside 'CONNECT TIMEOUT' / 'no plugin
    connected').
    """
    return {
        "blocker_id": "streak-roblox-studio",
        "reason": (
            "Streak alert: 28 consecutive probe failures. "
            "Last failure: no plugin connected (CONNECT TIMEOUT)"
        ),
        "affected_categories": ["roblox-studio"],
        "affected_skills": [],
        "detected_session": 1201,
        # Well past any --max-age-hours the loop uses, so the age filter cannot
        # be what spares this blocker. If the exemption regresses, the record
        # reaches the gate.
        "detected_at": "2026-07-01T00:00:00",
        "resolution": None,
        "source": "infra-health.streak-alert",
    }


class _Harness:
    """Stub the three I/O boundaries and drive the real main()."""

    def __init__(self, blockers):
        self.blockers = blockers
        self.written = None
        self.goals_filed = []
        self._saved = {}

    def __enter__(self):
        for name in ("_wm_read_blockers", "_wm_set_blockers", "_run_gate",
                     "_add_investigate_goal"):
            self._saved[name] = getattr(M, name)
        M._wm_read_blockers = lambda: self.blockers
        M._wm_set_blockers = self._capture_write
        # The MEASURED gate verdict from the live incident. The gate genuinely
        # matches — the fix must not depend on the gate returning no match.
        M._run_gate = lambda reason, intended: {
            "match_count": 10,
            "would_block": True,
            "matches": [{"skill": "access-roblox-studio", "matched_keyword": "connect"}],
        }
        M._add_investigate_goal = self._capture_goal
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(M, name, fn)
        return False

    def _capture_write(self, blockers):
        self.written = blockers
        return True

    def _capture_goal(self, asp_id, blocker, gate_result):
        self.goals_filed.append((asp_id, blocker.get("blocker_id")))
        return "g-TEST-0001"


def run_apply(blockers):
    """Drive the PRODUCTION entry point with the production arg shape.

    `--apply` is what Phase 0.5b.0.5 passes unconditionally; testing only the
    dry-run path would exercise a branch production never takes on this call.
    Note guard-1813: this script's gate is NOT side-effect-free on a match — it
    files a real goal and writes a real resolution — which is precisely why
    `_add_investigate_goal` is stubbed here rather than probed live.
    """
    with _Harness(blockers) as h:
        M.main(["--apply", "--max-age-hours", "1"])
    return h


# ── The constraint: a producer-managed blocker survives --apply untouched ────

def test_streak_blocker_is_not_cleared_by_apply():
    b = real_streak_blocker()
    h = run_apply([b])

    assert b.get("resolution") is None, (
        "REGRESSION: the live streak-roblox-studio blocker was auto-cleared by "
        f"--apply. resolution={json.dumps(b.get('resolution'))}. A live outage "
        "is now invisible to BOTH the Phase 0.5b re-probe loop and the "
        "proactive-escalation path, which each iterate known_blockers."
    )


def test_no_false_investigate_goal_is_filed():
    """ is the artifact this prevents.

    Same branch as the clear (the loop files the goal, then clears), so this
    rides the same mutation rather than being an independently-guarded
    constraint — stated explicitly so no reader counts it as separate coverage.
    """
    h = run_apply([real_streak_blocker()])
    assert h.goals_filed == [], (
        f"REGRESSION: filed {h.goals_filed} — a false 'capability was "
        "overlooked at blocker creation' Investigate against a blocker that "
        "never went through CREATE_BLOCKER's capability gate at all."
    )


def test_exemption_is_counted_not_silent():
    """A sweep that skips work silently is indistinguishable from a clean run."""
    with _Harness([real_streak_blocker()]) as h:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.main(["--apply", "--max-age-hours", "1"])
        out = buf.getvalue()
    assert "producer_managed_exempt" in out, (
        "the exemption counter is absent from the report — a silent skip reads "
        f"as 'nothing matched'. stdout={out[:400]}"
    )
    payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert payload.get("producer_managed_exempt") == 1, (
        f"expected producer_managed_exempt==1, got {payload.get('producer_managed_exempt')}"
    )
    assert payload.get("cleared") == 0, f"expected cleared==0, got {payload.get('cleared')}"


# ── MUTATION PROOF (guard-1475 / guard-1780) ────────────────────────────────

def test_mutation_removing_the_exemption_makes_the_suite_fail():
    """Prove the tests above discriminate, rather than passing vacuously.

    Disabling `_is_producer_managed` restores the exact pre-fix behavior. If the
    blocker is STILL not cleared under the mutation, these tests are not testing
    the fix and would keep passing after a regression.

    One mutation is sufficient here because all three assertions above ride the
    SAME branch — the exemption `continue`. That is not a shortcut around
    guard-1861's "N constraints need N mutations": the constraint count here is
    genuinely one. The isinstance-guard cases below are a separate constraint
    with a separate failure mode, and are covered separately.
    """
    saved = M._is_producer_managed
    try:
        M._is_producer_managed = lambda b: False
        b = real_streak_blocker()
        h = run_apply([b])
        assert b.get("resolution") is not None, (
            "MUTATION DID NOT REDDEN: with the exemption disabled the blocker "
            "was still not cleared, so something OTHER than the fix is sparing "
            "it and these tests do not prove the fix works."
        )
        assert h.goals_filed, "mutation did not reach the goal-filing path either"
    finally:
        M._is_producer_managed = saved


# ── Separate constraint: the predicate must not raise on hostile shapes ─────

def test_predicate_is_isinstance_guarded():
    """A bare string or non-dict must return False, never raise.

    Distinct failure mode from the exemption itself: a naive `.get()` on a
    string raises AttributeError, and a bare try/except around it reads the
    record as absent — converting a fail-closed skip into either a crash or a
    false all-clear. Same read-side discipline `blocker_ref` handling requires.
    """
    for hostile in ("streak-roblox-studio", None, 42, ["streak-x"], object()):
        assert M._is_producer_managed(hostile) is False, (
            f"expected False for non-dict {hostile!r}"
        )


def test_non_producer_blockers_are_still_rechecked():
    """The exemption must be narrow — it must not disable the sweep at large.

    A blocker with no `source` and no streak- id is ordinary work and MUST
    still reach the gate, or the fix has silently retired the whole mechanism.
    """
    ordinary = {
        "blocker_id": "manual-thing",
        "failure_reason": "cannot connect to the thing",
        "detected_at": "2026-07-01T00:00:00",
        "resolution": None,
        "participants": ["user"],
    }
    h = run_apply([ordinary])
    assert ordinary.get("resolution") is not None, (
        "the exemption is too broad — an ordinary user-routed blocker was not "
        "rechecked, meaning the sweep has been effectively disabled."
    )


def test_source_prefix_and_id_prefix_each_suffice_independently():
    """Either signal alone identifies a producer-managed blocker."""
    src_only = {"blocker_id": "other-id", "source": "infra-health.streak-alert"}
    id_only = {"blocker_id": "streak-something", "source": None}
    assert M._is_producer_managed(src_only) is True
    assert M._is_producer_managed(id_only) is True
    assert M._is_producer_managed({"blocker_id": "x", "source": "create-blocker"}) is False
