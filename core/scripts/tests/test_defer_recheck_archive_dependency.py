""": defer-recheck must resolve dependency ids against the ARCHIVE,
not the live queues alone.

THE DEFECT. `main()` built its dependency index from `_read_goals("world") +
_read_goals("agent")` — both live-only reads. When an aspiration COMPLETES it
moves to aspirations-archive.jsonl and every one of its goals disappears from
those reads. So a dependency that completed and was then archived resolved to
nothing, and the sweep emitted "dep(s) not found" — the SAME message it emits
for an id that never existed. The dependent goal stayed deferred forever.

MEASURED INSTANCE (from the goal, re-confirmed live before this test was
written): g-005-17 carried `blocked_on_dependency: g-316-08 ... Auto-clears when
g-316-08 completes`. g-316-08 completed 2026-06-22T01:01:52 inside asp-316,
which was later archived. The goal sat frozen 37 DAYS while this sweep ran every
iteration and reported nothing. Live probe at fix time: g-316-08 absent from the
live index, present in the archive index with status='completed'.

WHY IT SURVIVED SO LONG — the incentive is inverted. Archiving a completed
aspiration is CORRECT housekeeping, and performing it is precisely what makes
every defer citing one of its goals permanently unclearable. The defer's own
promise ("auto-clears when X completes") is then what stops a human re-checking
it. Same family as guard-1802: a predicate structurally blind to part of the
population it covers, where a clean run and an empty run look identical.

WHY THESE FIVE CASES AND NOT FEWER. Case 1 alone would pass against a mutant
that resolves EVERY unknown id as completed and clears everything, so case 2
(nonexistent id must NOT clear) is the discriminator that gives case 1 meaning.
Case 3 pins the live-wins precedence, without which a stale archive snapshot
could shadow a re-opened live goal and clear a defer that is genuinely still
blocked — a WRONG-clear, the one failure direction that is worse than the bug
being fixed. Cases 4 and 5 are a PAIR and neither works alone: 4 pins that a
genuinely unreachable archive degrades LOUDLY (a silent degradation reinstates
the original invisibility), while 5 pins that a merely EMPTY archive does NOT
report as degraded. The first cut collapsed both into one return value, so the
sweep disowned its own correct verdicts on any world with nothing archived yet.

CASE 5 WAS FOUND BY REVIEWING THIS CHANGE, NOT BY WRITING IT — and its first
version was VACUOUS: it stubbed the empty archive as json.dumps([]), which
decodes to [] and never reaches the `data is None` branch the bug lived on.
The mutation-prover caught that (sabotage applied, test stayed green). Both the
bug and the vacuous test are the guard-1906 shape — a fixture that never
reaches the code under test — which is why the stub now distinguishes an empty
BODY ("") from an empty LIST ("[]") explicitly.

REFUSAL ASSERTIONS NAME THE FIELD (rb-5778). A test asserting only "did not
clear" survives a mutant that never clears anything and proves nothing, so the
negative cases assert the specific reason/anomaly fields, not just the absence
of a clear.

Run: py -3 -m pytest core/scripts/tests/test_defer_recheck_archive_dependency.py -v
"""
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import defer-recheck.py (hyphen in the name blocks a plain import)."""
    spec = importlib.util.spec_from_file_location(
        "defer_recheck_archive_module", SCRIPT_DIR / "defer-recheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_module()

# Old enough to clear the --max-age-hours floor in every case below.
OLD = "2026-01-01T00:00:00"


def _goal(gid, **kw):
    g = {"id": gid, "status": "pending", "title": gid, "created": OLD}
    g.update(kw)
    return g


def _deferred(gid, dep_id):
    """A pending goal deferred on `dep_id` via the structured DEP pattern.

    `defer_reason_set_at` is the field main() actually ages against (the other
    accepted fallbacks are `started` / `created_at`). Writing a plausible-but-
    wrong name here made all four cases report eligible=0 and details=[] on the
    first run — a fixture that never reaches the code under test, which is the
    guard-920 shape: the fixture must carry the literal production field name,
    not the one that reads correctly.
    """
    return _goal(gid, defer_reason=f"blocked_on_dependency: {dep_id} — "
                                   f"Auto-clears when {dep_id} completes",
                 defer_reason_set_at=OLD)


def _envelope(asps):
    return json.dumps({"aspirations": asps})


class _Stub:
    """Stubs _rt for main(): live vs archive selected by the `archive` kwarg.

    Mirrors the real signature (`aspirations_read(source=..., active=...,
    archive=...)`) rather than a convenience shape, so a future signature
    change breaks this test instead of silently exercising a branch production
    never takes (probe-with-canonical-code-path.md).
    """

    live = {}       # source -> list[aspiration]
    archive = {}    # source -> list[aspiration]
    archive_raises = ()   # sources whose archive read raises RtError
    # Sources whose archive read returns an EMPTY BODY (""), which is a
    # DIFFERENT state from an empty list "[]" and reaches a different branch:
    # tolerant_decode_aggregate returns None for "" and [] for "[]". Case 5
    # needs the None path, and stubbing it as json.dumps([]) made that test
    # vacuous — the mutation-prover caught it (guard-1906 shape: the fixture
    # never reached the code under test).
    archive_empty_body = ()

    class RtError(Exception):
        def __init__(self, body=""):
            self.body = body
            super().__init__(body)

    @classmethod
    def aspirations_read(cls, source="world", active=False,
                         active_compact=False, asp_id=None, limit=None,
                         archive=False):
        if archive:
            if source in cls.archive_raises:
                raise cls.RtError("simulated archive read failure")
            if source in cls.archive_empty_body:
                return ""      # genuinely-empty body -> decoder returns None
            # The endpoint returns a BARE list for ?archive=1 — assert the
            # production shape here, not the envelope shape.
            return json.dumps(cls.archive.get(source, []))
        return _envelope(cls.live.get(source, []))

    @staticmethod
    def tolerant_decode_aggregate(source, raw):
        import importlib
        return importlib.import_module("_rt").tolerant_decode_aggregate(source, raw)

    @staticmethod
    def tolerant_decode_list(source, raw):
        import importlib
        return importlib.import_module("_rt").tolerant_decode_list(source, raw)


def run_main(live, archive, archive_raises=(), archive_empty_body=()):
    """Drive main() in dry-run with metrics disabled; return parsed JSON."""
    stub = type("_S", (_Stub,), {"live": live, "archive": archive,
                                 "archive_raises": archive_raises,
                                 "archive_empty_body": archive_empty_body})
    orig_rt, orig_argv = M._rt, sys.argv
    M._rt = stub
    # --metrics-log "" disables the metrics file so the test writes nothing.
    sys.argv = ["defer-recheck.py", "--max-age-hours", "0",
                "--metrics-log", "", "--output", "json"]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            M.main()
    finally:
        M._rt, sys.argv = orig_rt, orig_argv
    return json.loads(out.getvalue()), err.getvalue()


def _detail(res, goal_id):
    for d in res["details"]:
        if d["goal_id"] == goal_id:
            return d
    raise AssertionError(f"{goal_id} absent from details: {res['details']!r}")


# ── Case 1: THE FIX — a dep that is completed-and-archived CLEARS ──────────
def test_archived_completed_dependency_clears_the_defer():
    res, _ = run_main(
        live={"world": [{"id": "asp-005", "status": "active",
                         "goals": [_deferred("g-005-17", "g-316-08")]}],
              "agent": []},
        archive={"world": [{"id": "asp-316", "status": "completed",
                            "goals": [_goal("g-316-08", status="completed")]}],
                 "agent": []},
    )
    assert "g-005-17" in res["would_clear"], (
        "a defer whose only dependency completed inside an ARCHIVED aspiration "
        "must clear — this is the 37-day freeze of the measured instance; "
        f"got details={res['details']!r}")
    d = _detail(res, "g-005-17")
    assert d["dep_origins"] == {"g-316-08": "archive"}, (
        "the clear must record that the dep resolved from the ARCHIVE; without "
        "that the archive path is invisible in the record, which is the "
        f"condition that hid the bug. got {d.get('dep_origins')!r}")
    assert res["archive_indexed"] == 1, (
        f"expected 1 archive-sourced id in the index, got {res['archive_indexed']}")


# ── Case 2: DISCRIMINATOR — a genuinely nonexistent dep does NOT clear ─────
def test_nonexistent_dependency_does_not_clear_and_is_flagged_anomaly():
    res, _ = run_main(
        live={"world": [{"id": "asp-005", "status": "active",
                         "goals": [_deferred("g-005-99", "g-999-99")]}],
              "agent": []},
        archive={"world": [], "agent": []},
    )
    assert res["would_clear"] == [], (
        "an id present in NEITHER store must not clear — without this case, "
        "case 1 would also pass against a mutant that clears everything")
    d = _detail(res, "g-005-99")
    # Name the FIELD, not just "it didn't clear" (rb-5778).
    assert d["action"] == "skipped", f"expected skipped, got {d['action']!r}"
    assert d.get("anomaly") is True, (
        "an id in neither store is an anomaly to REPORT, never a silent skip "
        f"(guard-1555); got {d!r}")
    assert d.get("searched") == ["live", "archive"], (
        f"the reason must record which stores were searched; got {d.get('searched')!r}")
    assert "archive" in d["reason"], (
        "the not-found message must state that the archive was searched too — "
        "collapsing 'never existed' and 'archived' into one message is what "
        f"made the defect invisible for 37 days; got {d['reason']!r}")


# ── Case 3: PRECEDENCE — a live record must not be shadowed by the archive ──
def test_live_record_wins_over_stale_archive_copy():
    """A re-opened goal exists in BOTH stores. Clearing on the archive's stale
    'completed' would be a WRONG-clear — worse than the bug being fixed."""
    res, _ = run_main(
        live={"world": [{"id": "asp-005", "status": "active",
                         "goals": [_deferred("g-005-18", "g-316-09"),
                                   _goal("g-316-09", status="in-progress")]}],
              "agent": []},
        archive={"world": [{"id": "asp-316", "status": "completed",
                            "goals": [_goal("g-316-09", status="completed")]}],
                 "agent": []},
    )
    assert res["would_clear"] == [], (
        "the LIVE in-progress record must win over the archive's stale "
        "'completed' snapshot — clearing here would unblock a goal that is "
        f"genuinely still waiting. got details={res['details']!r}")
    d = _detail(res, "g-005-18")
    assert "not completed" in d["reason"], (
        f"expected the incomplete-dep reason, got {d['reason']!r}")


# ── Case 4: DEGRADATION — an archive read failure is loud, not silent ──────
def test_archive_read_failure_degrades_loudly_to_live_only():
    res, err = run_main(
        live={"world": [{"id": "asp-005", "status": "active",
                         "goals": [_deferred("g-005-17", "g-316-08")]}],
              "agent": []},
        archive={"world": [], "agent": []},
        archive_raises=("world", "agent"),
    )
    # Degrades to the pre-fix behavior (missed-clear), NOT to a wrong-clear,
    # and NOT to taking the whole sweep down as _read_goals does on RtError.
    assert res["would_clear"] == [], "a failed archive read must not clear"
    assert res["archive_read_failed"] == ["world", "agent"], (
        "the degradation must be reported in the result — a silent fallback to "
        "live-only reinstates exactly the invisibility this fix removes; "
        f"got {res['archive_read_failed']!r}")
    assert "archive read failed" in err, (
        f"expected a stderr diagnostic on archive read failure; got {err!r}")
    d = _detail(res, "g-005-17")
    assert d.get("archive_degraded") is True, (
        "a not-found reported during a degraded run must say so, else it reads "
        f"as a confirmed anomaly; got {d!r}")


# ── Case 5: an EMPTY archive is a valid state, not a read failure ─────────
def test_empty_archive_is_not_reported_as_a_read_failure():
    """Found by fresh-eyes review of this same change, not by writing it.

    `tolerant_decode_aggregate` returns None for a genuinely-empty body and
    documents that as "valid state, not a source error"; every sibling maps it
    to []. The first cut of `_read_archived_goals` returned None instead, so a
    source with nothing archived yet (a fresh world) was marked
    archive_read_failed — and SILENTLY, because unlike the RtError branch that
    path prints no diagnostic. The sweep would have disowned its own correct
    not-found verdicts via archive_degraded on a world where nothing was wrong.

    This case is the inverse of case 4 and they must BOTH hold: empty => fine,
    unreachable => degraded. One test cannot pin both, because the whole defect
    was that the two collapsed into the same return value.
    """
    res, err = run_main(
        live={"world": [{"id": "asp-005", "status": "active",
                         "goals": [_deferred("g-005-20", "g-777-77")]}],
              "agent": []},
        archive={"world": [], "agent": []},
        # EMPTY BODY, not "[]" — this is the branch the bug lived on. Stubbing
        # it as an empty list decodes to [] and never reaches `data is None`,
        # which is what made the first version of this test vacuous.
        archive_empty_body=("world", "agent"),
    )
    assert res["archive_read_failed"] == [], (
        "an EMPTY archive is a valid state — reporting it as a read failure "
        "makes the sweep disown its own correct verdicts on any world with "
        f"nothing archived yet; got {res['archive_read_failed']!r}")
    d = _detail(res, "g-005-20")
    assert d.get("archive_degraded") is False, (
        f"not-found on an empty (but readable) archive is CONFIRMED, not degraded; got {d!r}")
    assert d.get("anomaly") is True, (
        "the id exists in neither store, so it is still a genuine anomaly")
    assert "archive read failed" not in err, (
        f"no failure diagnostic should be emitted for an empty archive; got {err!r}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001 — standalone runner
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
