""": blocker-recheck must see blockers that live on GOAL RECORDS.

DEFECT (foxtrot, felt-sense lane 3, 2026-07-31; re-measured alpha/cc-04
2026-08-01). Blockers live in two places and this sweep read only one:

  (1) `known_blockers` — per-agent, per-box, EPHEMERAL working memory;
  (2) `blocker_ref` on the GOAL RECORD — shared, fleet-wide, DURABLE.

`create-blocker.py` writes BOTH, calling the WM entry "the authoritative
record" and the goal copy "a redundancy". That is inverted with respect to
durability, and the fleet measurement shows which one survives: on 2026-08-01
ALL FIVE agents read `known_blockers=null` while SIX non-terminal goals carried
a live `blocker_ref`. The ephemeral "authoritative" store had lost everything.

So the sweep reported `total_blockers: 0` — a number indistinguishable from a
genuinely clean queue (guard-1802 / rb-5650). It is the
`enumerator-all-clear-boundary` class: honest about the population it
enumerated, silent about the one the reader cared about.

WHAT THIS FILE PINS — three constraints, deliberately separated because they
fail independently:

  A. The goal-sourced population is SEEN (the widening itself).
  B. Goal-sourced entries are never WRITTEN BACK into the WM slot. Without the
     partition the sweep would invent blockers nobody created and corrupt every
     consumer that iterates that slot.
  C. Goal-sourced entries are never AUTO-CLEARED, even when the capability gate
     matches. Clearing one mutates a GOAL (usually another agent's), on a
     keyword match with no probe behind it (guard-1978).

MUTATION-PROVEN (guard-1780): each test below fails against the pre-fix code.
A verifies via `goal_blockers`/`total_blockers`, which did not exist / read 0.
B and C are guarded by the partition and the report-only branch respectively;
deleting either one alone flips its own test and leaves the others green.

Run: py -3 -m pytest core/scripts/tests/test_blocker_recheck_goal_ref_population.py -v
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
        "blocker_recheck_module_goalref",
        SCRIPT_DIR / "blocker-recheck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()


# ── Literal production shapes (guard-920) ───────────────────────────────────

def live_goal_with_blocker_ref():
    """A goal record carrying the LITERAL live `blocker_ref` of .

    Copied field-for-field from the world queue on 2026-08-01. Type
    `infrastructure` is deliberate: it is NOT in HUMAN_ONLY_BLOCKER_TYPES, so
    the record reaches the age/participants filters instead of being spared by
    the type test — a `credentials-required` fixture would pass against the
    buggy code for the wrong reason and prove nothing.

    `participants: ["agent", "user"]` is likewise deliberate. The live goal
    carries `["agent"]`, which `is_user_routed` correctly skips — a faithful
    copy would never reach the gate, so this fixture keeps every other field
    literal and widens only this one, in order to exercise the path the
    constraints are about. Stated rather than silently tidied.
    """
    return {
        "id": "g-250-124",
        "status": "blocked",
        "participants": ["agent", "user"],
        "intended_agent": "either",
        "title": "Validate: C13 CRA axis on a clean post-crash-fix NPCDemo session",
        "blocker_ref": {
            "type": "infrastructure",
            "external_id": "npcdemoexperiment-session-with-r-gt-0:2026-07-30",
            "state_hash": None,
            "created_at": "2026-07-01T12:27:00",
            "expires_at": "2026-08-04T12:27:00",
        },
    }


def live_goal_blocker_ref_without_created_at():
    """'s real shape — a live `blocker_ref` carrying NO `created_at`.

    `created_at` is OPTIONAL in the blocker_ref schema, so this is a conformant
    record, not a corrupt one. It pins that an unaged entry is COUNTED and
    attributed rather than silently vanishing: with no parsable stamp the age
    test can never pass at any threshold, which is a permanent exclusion and so
    exactly the kind of silent zero this goal exists to remove.
    """
    return {
        "id": "g-350-95",
        "status": "blocked",
        "participants": ["agent", "user"],
        "intended_agent": "either",
        "title": "Idea: normalize line-endings in Ayoai-Roblox-Integration",
        "blocker_ref": {
            "type": "coordination",
            "external_id": "roblox-clone-stale-57behind-ed0fced",
            "unblock_goal": "pq-fox-roblox-clone-stale-reconcile",
            "reason": "local clone 57 commits behind origin; reconcile needed",
            "expires_at": "2026-08-02T16:00:00",
        },
    }


def wm_blocker_that_clears():
    """A WM blocker the gate matches, so the `--apply` write-back actually fires.

    Constraint B is only observable when `_wm_set_blockers` is CALLED, and the
    production guard is `if args.apply and report["cleared"] > 0`. Without a
    clearable WM entry the write never happens and the partition test would pass
    vacuously against any code at all (guard-1665: a check whose precondition
    never fires cannot fail).
    """
    return {
        "blocker_id": "wm-clearable-fixture",
        "reason": "cannot deploy the service; no credentials loaded",
        "participants": ["user"],
        "detected_at": "2026-07-01T00:00:00",
        "resolution": None,
    }


class _Harness:
    """Stub the I/O boundaries and drive the real main().

    `_read_goal_blocker_refs` is deliberately NOT stubbed — `_rt.aspirations_read`
    underneath it is. Stubbing the function would test only that main() calls
    something, leaving the normalization (guard-961 dict guard, both envelope
    shapes, field mapping) uncovered by the very file that exists to pin it.
    """

    def __init__(self, wm_blockers, goals, gate_matches=True):
        self.wm_blockers = wm_blockers
        self.goals = goals
        self.gate_matches = gate_matches
        self.written = None
        self.goals_filed = []
        self._saved = {}
        self._saved_rt = None

    def __enter__(self):
        for name in ("_wm_read_blockers", "_wm_set_blockers", "_run_gate",
                     "_add_investigate_goal"):
            self._saved[name] = getattr(M, name)
        M._wm_read_blockers = lambda: self.wm_blockers
        M._wm_set_blockers = self._capture_write
        M._run_gate = lambda reason, intended: {
            "match_count": 10 if self.gate_matches else 0,
            "would_block": self.gate_matches,
            "matches": ([{"skill": "deploy-service", "matched_keyword": "deploy"}]
                        if self.gate_matches else []),
        }
        M._add_investigate_goal = self._capture_goal
        self._saved_rt = M._rt.aspirations_read
        M._rt.aspirations_read = self._fake_read
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(M, name, fn)
        M._rt.aspirations_read = self._saved_rt
        return False

    def _fake_read(self, source="world", active=False, **kw):
        # Returns the BARE-LIST envelope, which is what the live daemon actually
        # returned for source=world&active=1 on 2026-08-01 — not the
        # {"aspirations": [...]} shape the docstring documents. Pinning the
        # measured shape keeps the reader's dual-shape handling honest.
        if source != "world":
            return json.dumps([])
        return json.dumps([{"id": "asp-250", "goals": self.goals}])

    def _capture_write(self, blockers):
        self.written = blockers
        return True

    def _capture_goal(self, asp_id, blocker, gate_result):
        self.goals_filed.append((asp_id, blocker.get("blocker_id")))
        return "g-TEST-0001"


def _run_capturing(wm_blockers, goals, gate_matches=True):
    """Drive the PRODUCTION entry point and parse the report it prints.

    `--apply` is what Phase 0.5b.0.5 passes unconditionally; the dry-run path is
    a branch production never takes on this call (guard-920).
    """
    import io
    import contextlib
    buf = io.StringIO()
    with _Harness(wm_blockers, goals, gate_matches) as h:
        with contextlib.redirect_stdout(buf):
            M.main(["--apply", "--max-age-hours", "1"])
    h.report = json.loads(buf.getvalue())
    return h


# ── A. the widening: goal-sourced blockers are SEEN ─────────────────────────

def test_goal_blocker_ref_is_counted_when_wm_slot_is_empty():
    """The exact live condition: known_blockers empty, goals carry blocker_ref."""
    h = _run_capturing([], [live_goal_with_blocker_ref()])
    r = h.report
    assert r["goal_blockers"] == 1, (
        f"REGRESSION: goal_blockers={r['goal_blockers']} — a goal carrying a live "
        "blocker_ref was invisible to the sweep. This is the pre-fix behaviour: "
        "total_blockers reads 0 against a non-empty durable population, which is "
        "indistinguishable from a genuinely clean queue (guard-1802)."
    )
    assert r["total_blockers"] == 1
    assert r["wm_blockers"] == 0, (
        "the population must be DECLARED, not merged into one opaque count — "
        "'0 WM + 1 goal' and '1 WM + 0 goal' are different worlds to a reader"
    )


def test_malformed_blocker_ref_is_skipped_not_raised():
    """guard-961: blocker_ref must be a DICT before any field access."""
    goals = [
        {"id": "g-bad-1", "status": "blocked", "blocker_ref": "not-a-dict"},
        {"id": "g-bad-2", "status": "blocked", "blocker_ref": ["also", "wrong"]},
        live_goal_with_blocker_ref(),
    ]
    h = _run_capturing([], goals)
    assert h.report["goal_blockers"] == 1, (
        "a bare-string or list blocker_ref is a malformed record, not a blocker; "
        "it must be skipped rather than counted or raised on"
    )


def test_terminal_goals_are_excluded():
    """A blocker on finished work is moot and must not inflate the population."""
    done = dict(live_goal_with_blocker_ref(), id="g-done", status="completed")
    h = _run_capturing([], [done, live_goal_with_blocker_ref()])
    assert h.report["goal_blockers"] == 1


# ── B. the partition: goal entries never reach the WM slot ──────────────────

def test_goal_sourced_entries_are_never_written_back_to_wm():
    """The single most damaging failure mode if the widening were naive.

    `updated` accumulates BOTH populations. Writing it back unfiltered would
    inject synthetic goal-derived entries into `known_blockers`, inventing
    blockers nobody created and corrupting every consumer that iterates that
    slot (Phase 0.5b re-probe, proactive escalation, quiescence-gate C2).
    """
    h = _run_capturing([wm_blocker_that_clears()], [live_goal_with_blocker_ref()])
    assert h.report["cleared"] == 1, (
        "precondition: the WM blocker must actually clear, or the write-back "
        "never fires and this test passes vacuously (guard-1665)"
    )
    assert h.written is not None, "write-back did not fire — see precondition above"
    leaked = [b for b in h.written
              if isinstance(b, dict) and b.get("_origin") == "goal"]
    assert leaked == [], (
        f"REGRESSION: {len(leaked)} goal-derived entr(ies) leaked into the "
        f"known_blockers write-back: {json.dumps(leaked)[:300]}. The sweep would "
        "be inventing blockers that no CREATE_BLOCKER call ever made."
    )


# ── C. report-only: goal entries are never auto-cleared ─────────────────────

def test_goal_sourced_blocker_is_not_cleared_even_when_gate_matches():
    """guard-1978: this sweep consults NO probe — it decides on a keyword match.

    Extending that clear path over goal records would mutate goals (usually
    another agent's) on keyword evidence alone. `blocked-signal-resolution-check`
    owns this population's resolution question and is deliberately detective-only
    for the same reason.
    """
    goal = live_goal_with_blocker_ref()
    h = _run_capturing([], [goal])
    r = h.report
    assert r["matches_found"] == 1, (
        "precondition: the gate must MATCH, or 'not cleared' proves nothing "
        "about the report-only branch (guard-1665)"
    )
    assert r["cleared"] == 0, (
        f"REGRESSION: cleared={r['cleared']} — a goal-sourced blocker was "
        "auto-cleared on a keyword match with no probe behind it (guard-1978)."
    )
    assert r["goal_sourced_report_only"] == 1
    assert h.goals_filed == [], (
        f"REGRESSION: filed {h.goals_filed} — the report-only branch must not "
        "file a capability-was-overlooked Investigate either."
    )
    detail = r["details"][0]
    assert detail["origin"] == "goal"
    assert detail["goal_id"] == "g-250-124", (
        "a goal-sourced finding must name its goal, or a reader cannot act on it"
    )
    assert "report-only" in detail["action"]


# ── the report explains its own zero ────────────────────────────────────────

def test_skip_tally_accounts_for_every_blocker():
    """`rechecked: 0` against a non-zero population must say WHY.

    Otherwise the vacuous all-clear simply moves one field to the right: a reader
    still cannot tell "every blocker was correctly filtered" from "the filters
    are silently broken". The tally must close: skips + rechecked == total.
    """
    goals = [live_goal_with_blocker_ref(),
             live_goal_blocker_ref_without_created_at()]
    h = _run_capturing([], goals)
    r = h.report
    total = r["total_blockers"]
    accounted = sum(r["skipped"].values()) + r["rechecked"]
    assert accounted == total, (
        f"accounting does not close: skips {json.dumps(r['skipped'])} + "
        f"rechecked {r['rechecked']} = {accounted}, but total_blockers={total}. "
        "An unaccounted blocker is one the report silently dropped."
    )


def test_terminal_status_fallback_matches_the_canonical_set():
    """Surfaced by fresh-eyes review of this file's own code ().

    `_read_goal_blocker_refs` imports TERMINAL_STATUSES from `_goal_census` behind
    a `pragma: no cover` except-branch that falls back to a hardcoded literal. The
    fallback is correct today (verified equal), but nothing pinned it — so a status
    added to the canonical set would silently diverge here, and a goal in that new
    terminal state would be counted as a LIVE blocker. Divergence in this direction
    is invisible: it inflates the population rather than emptying it, so it produces
    no error and no zero to notice.

    Pins equality rather than the literal, so the canonical set stays the one source
    of truth (this test fails if either side moves independently).
    """
    from _goal_census import TERMINAL_STATUSES as canonical
    fallback = frozenset(
        {"completed", "decomposed", "expired", "skipped", "superseded"})
    assert set(canonical) == set(fallback), (
        "the hardcoded fallback in _read_goal_blocker_refs has diverged from "
        f"_goal_census.TERMINAL_STATUSES: canonical-only={sorted(set(canonical) - fallback)}, "
        f"fallback-only={sorted(fallback - set(canonical))}. Update the literal in "
        "blocker-recheck.py's import-guard to match."
    )


def test_precheck_call_site_is_not_gated_on_known_blockers():
    """THE WIRING (). Widening what the sweep SEES is inert if it never RUNS.

    The Phase 0.5b.0.5 call site was gated on `IF known_blockers is non-empty`,
    which made the sweep unreachable in precisely the state it exists for: the
    WM slot reads null on every agent while goal records carry live
    blocker_refs. Function and wiring fail independently, and a green suite
    certifies only the former (guard-1943) — so the population tests above
    would all have passed against a sweep that production never invoked.

    Deliberately a text assertion over the SKILL.md: that file IS the
    executable artifact here (the orchestrator follows its pseudocode), so
    there is no import to reach for. Scoped to the ~15 lines preceding the
    invocation rather than the whole file, so an unrelated `known_blockers`
    mention elsewhere in this 1000+ line skill cannot make it pass or fail by
    accident.
    """
    skill = (SCRIPT_DIR.parent.parent / ".claude" / "skills"
             / "aspirations-precheck" / "SKILL.md")
    assert skill.is_file(), f"precheck SKILL.md not found at {skill}"
    lines = skill.read_text(encoding="utf-8").splitlines()

    invocations = [i for i, l in enumerate(lines)
                   if "blocker-recheck.sh" in l and l.lstrip().startswith("Bash:")]
    # Positive control on the locator itself: if the invocation cannot be found,
    # every assertion below is vacuous and would pass forever (guard-1665).
    assert len(invocations) == 1, (
        f"expected exactly 1 `Bash: ...blocker-recheck.sh` call site, found "
        f"{len(invocations)} — the locator is stale, so this test proves nothing "
        "until it is re-anchored"
    )

    window = "\n".join(lines[max(0, invocations[0] - 15):invocations[0]])
    offending = [l for l in window.splitlines()
                 if not l.lstrip().startswith("#")
                 and "known_blockers" in l
                 and l.lstrip().startswith("IF")]
    assert offending == [], (
        "REGRESSION: the blocker-recheck call site is gated on known_blockers "
        f"again: {offending}. That WM slot is empty on every agent, so this "
        "guard makes the sweep unreachable exactly when the durable "
        "blocker_ref population is non-empty — the defect g-115-4328 fixed."
    )


def test_unaged_blocker_ref_is_counted_not_silently_dropped():
    """`created_at` is OPTIONAL in the blocker_ref schema ( is live proof).

    With no parsable stamp the age test can never pass at ANY threshold — a
    permanent exclusion. It must be attributed to `unaged`, distinct from
    `below_age_threshold`, because the two mean opposite things: one will never
    become eligible, the other becomes eligible on its own with time.
    """
    h = _run_capturing([], [live_goal_blocker_ref_without_created_at()])
    r = h.report
    assert r["goal_blockers"] == 1
    assert r["skipped"]["unaged"] == 1, (
        f"REGRESSION: skipped={json.dumps(r['skipped'])} — a blocker_ref with no "
        "created_at is permanently unreachable by the age filter and must be "
        "reported as such, not folded into below_age_threshold or dropped."
    )
    assert r["skipped"]["below_age_threshold"] == 0, (
        "a permanent exclusion must not be reported as a temporary wait"
    )
