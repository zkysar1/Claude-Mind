""" — the per-agent-store merge-registration decisions, made executable.

WHY THIS EXISTS. g-115-5457 proposed registering FIVE per-agent JSONL stores as
"a small, low-risk change". Reading every writer disqualified four of them. The
refusals are the more valuable half of that work and they are the half that
rots: a future reader running `merge_handler_for` sees `None`, reads it as
"untriaged", and registers it — which is an active regression, not a fix. The
convention names that exact failure ("absent from _HANDLERS is not untriaged").
A comment can be skipped; a red test cannot.

So this file pins BOTH directions:
  - the one store certified append-only IS registered, and its handler conserves
  - the four disqualified stores are STILL unregistered

If a future change registers one of the four, the test that goes red is the
signal to re-derive the disqualification (guard-1816 step 4), not to edit the
expectation. Each refusal below names its disqualifying writer so the re-derivation
starts from evidence rather than from scratch.
"""
import json
import sys
from pathlib import Path

import pytest

# parents[1] IS core/scripts — one hop, no re-descent. The first version of this
# line did parents[2] ("core") and re-appended "core/scripts", yielding
# core/core/scripts. Under pytest that is INERT (conftest.py already puts
# core/scripts on sys.path, so the import succeeded anyway) and it only surfaced
# when run-invisible-suites.sh ran this file DIRECTLY -- ModuleNotFoundError.
# A path bug masked by a sibling fixture is invisible in the runner you use most.
SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import coordination_merge as cm  # noqa: E402


# (path, why-it-is-refused) — the reason travels with the assertion on purpose.
DISQUALIFIED = [
    ("agents/alpha/session/execution-diary.jsonl",
     "execution-diary.py cmd_trim removes entries older than N hours and is WIRED "
     "at iteration-close.sh (trim --hours 8) every iteration; a line-union would "
     "resurrect every trimmed entry"),
    ("agents/alpha/insights.jsonl",
     "insights-read.sh --mark-processed sets processed=True on every entry and "
     "rewrites the file; a line-union keeps the processed AND unprocessed copy of "
     "each entry as two lines"),
    ("agents/alpha/experience.jsonl",
     "archive_sweep phase 2 filters live by archived_ids (a removal); refusal "
     "already recorded in governed-store-write-classes.md, cured via locked_rmw"),
    ("agents/alpha/experience-archive.jsonl",
     "experience_write.set_field targets the archive path when the id lives there "
     "and rewrites the file; same two-lines-per-record corruption as insights"),
]


def test_all_five_stores_have_the_disposition_this_goal_decided():
    """The whole  verdict in one assertion: 1 registered, 4 refused.

    DELIBERATELY TOP-LEVEL, and that is not a style choice. run-invisible-suites.sh
    enumerates its population as `grep -qE '^def test_' || echo "$f"` -- a file whose
    tests all live inside CLASSES has zero top-level matches, so it is classified
    main()-style and executed directly instead of under pytest. This file was, and
    it failed there (rc=1) while passing cleanly under pytest. So a class-only test
    file silently leaves the pytest half and joins a population where it does not
    belong. This function keeps the file where it belongs; it earns its place by
    asserting the goal's actual deliverable rather than existing to satisfy a grep.
    """
    expected = {
        "agents/alpha/session/desync-warnings.jsonl": cm.merge_append_only_jsonl,
        "agents/alpha/session/execution-diary.jsonl": None,
        "agents/alpha/insights.jsonl": None,
        "agents/alpha/experience.jsonl": None,
        "agents/alpha/experience-archive.jsonl": None,
    }
    actual = {p: cm.merge_handler_for(p) for p in expected}
    assert actual == expected, (
        "the five-store disposition changed. One store was certified append-only "
        "across ALL its writers and registered; four were refused on writer evidence "
        "(a removal or a mutation -- either one makes a line-union corrupt). "
        "Re-derive against today's writers before editing this expectation.")


class TestTheOneCertifiedStoreIsRegistered:

    def test_desync_warnings_resolves_to_the_append_only_handler(self):
        h = cm.merge_handler_for("agents/alpha/session/desync-warnings.jsonl")
        assert h is cm.merge_append_only_jsonl, (
            "desync-warnings.jsonl is no longer registered — it was certified "
            "append-only across all three writers (session_desync_check.py open('a'), "
            "recovery-gate.sh '>>', aspirations-graceful-stop '>>'), and two of those "
            "bypass the backend, which is what strands the If-Match fence. "
            "Unregistering restores the permanent-wedge shape.")

    def test_registration_is_basename_keyed_so_it_covers_every_agent(self):
        """The store is per-agent; the registry is basename-keyed. Every agent's
        copy must resolve, or the cure reaches one agent and silently misses the
        rest."""
        for agent in ("alpha", "bravo", "zeta", "some-future-agent"):
            p = f"agents/{agent}/session/desync-warnings.jsonl"
            assert cm.merge_handler_for(p) is cm.merge_append_only_jsonl, p


class TestTheFourRefusalsHold:

    @pytest.mark.parametrize("path,reason", DISQUALIFIED,
                             ids=[p.split("/")[-1] for p, _ in DISQUALIFIED])
    def test_disqualified_store_is_still_unregistered(self, path, reason):
        assert cm.merge_handler_for(path) is None, (
            f"{path} has been registered, but it was REFUSED with evidence: {reason}. "
            "Re-derive the disqualification against today's writers before changing "
            "this expectation — do not edit the assertion to match the registry.")


class TestTheHandlerActuallyConservesForThisStore:
    """Registration is only a cure if the handler conserves (the 
    lesson: class (a) says a reconciler runs, never that it conserves)."""

    def _recs(self, blob):
        return [json.loads(l) for l in blob.split(b"\n") if l.strip()]

    def _mk(self, *stamps):
        return b"".join(
            json.dumps({"id": "orphan_file", "severity": "info",
                        "file": f"f{s}.json", "logged_at": s}).encode() + b"\n"
            for s in stamps)

    def test_union_loses_nothing_and_collapses_the_shared_baseline(self):
        a = self._mk("2026-01-01T00:00:01", "2026-01-01T00:00:02", "2026-01-01T00:00:03")
        b = self._mk("2026-01-01T00:00:02", "2026-01-01T00:00:03", "2026-01-01T00:00:04")
        merged = self._recs(cm.merge_append_only_jsonl(a, b))
        stamps = [r["logged_at"] for r in merged]
        assert stamps == ["2026-01-01T00:00:0%d" % i for i in (1, 2, 3, 4)], stamps

    def test_merge_is_commutative(self):
        """guard-907: the handler runs on both boxes and they must agree."""
        a = self._mk("2026-01-01T00:00:01", "2026-01-01T00:00:03")
        b = self._mk("2026-01-01T00:00:02")
        assert cm.merge_append_only_jsonl(a, b) == cm.merge_append_only_jsonl(b, a)


class TestTheOrderingFieldAndItsNarrowness:

    def test_logged_at_resolves_so_the_merged_log_stays_chronological(self):
        """Without this the merge is still correct and commutative but sorts by
        canonical JSON, and jsonl_hygiene's 'keep newest' cap/rotate depends on
        chronological order — so a cap wired later would trim the wrong records."""
        assert cm._log_ts({"logged_at": "2026-01-01T00:00:00"}) == "2026-01-01T00:00:00"

    def test_logged_at_is_LAST_so_adding_it_could_not_reorder_any_existing_store(self):
        """The lookup is first-present in tuple order. Appending can only affect
        records that previously resolved to "" — that narrowness is the whole
        safety argument for the change, so it is pinned rather than assumed."""
        assert cm._LOG_TS_FIELDS[-1] == "logged_at"
        earlier = cm._LOG_TS_FIELDS[:-1]
        for field in earlier:
            rec = {field: "2026-01-01T00:00:00", "logged_at": "2099-12-31T23:59:59"}
            assert cm._log_ts(rec) == "2026-01-01T00:00:00", (
                f"a record carrying both {field} and logged_at now resolves to "
                "logged_at — the change stopped being narrowing and may have "
                "reordered an already-registered log")
