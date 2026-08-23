""": the `encoding_capture` -> `encoding_queue` drain lane.

`encoding_capture` was the LAST of the four worker->reducer capture slots with
no consumer at all: workers appended to it, `body-merge.merge_wm` carried it up
to the reducer, and nothing ever read it. This file pins the lane that drains it.

Two halves, and the SECOND is the reason this file exists:

  1. FUNCTION tests — `enc_observation` defensive reads and `_lane_encoding`'s
     argv/stdin/rc contract.
  2. CALL-SITE tests — that `retrospect` actually DISPATCHES the lane and that
     `main` actually LOADS the slot and passes it down. The sibling
     `hyp_capture` guard (g-306-200) shipped INERT at its only call site while
     all 11 of its unit tests passed: the caller read the slot in one directive
     and consumed it in the next, so the value arrived empty on every
     invocation. A green function suite certifies the FUNCTION and says nothing
     about the WIRING (guard-1943). The call-site half is what would have caught
     it, and did not exist.

Daemon-safe: `_run` is stubbed everywhere, so no wrapper is executed and no live
store is read or written. No `daemon_integration` marker needed.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_encoding_capture_lane.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import worker_retrospective as wr  # noqa: E402

ROOT = Path("/nonexistent")
ITEM = {"goal_id": "g-306-1", "source": "world", "title": "t",
        "aspiration_id": "asp-306"}


def _capture_run(monkeypatch, rc: int = 0, err: str = ""):
    """Stub `_run`, recording every (argv, stdin) it is handed."""
    seen: list[tuple[list, str | None]] = []

    def _fake_run(argv, timeout=90, stdin=None):
        seen.append((list(argv), stdin))
        return rc, "", err

    monkeypatch.setattr(wr, "_run", _fake_run)
    return seen


# ───────────────────────── half 1: function contract ─────────────────────────

@pytest.mark.parametrize("entry", [
    None, {}, [], "a string", 42,
    {"evidence": "file:1"},              # evidence without a fact
    {"fact": ""}, {"fact": "   "},       # blank fact
])
def test_enc_observation_returns_empty_without_a_usable_fact(entry):
    """`fact` is the only load-bearing key, and the slot has no enforced schema.

    Workers append to `encoding_capture` freely (guard-4044 — no writer-side
    validation), so the reader must be defensive rather than assume a shape.
    """
    assert wr.enc_observation(entry) == ""


def test_enc_observation_carries_fact_evidence_and_supersedes():
    got = wr.enc_observation({"fact": "X is Y", "evidence": "auth.py:23",
                              "supersedes": "the old belief"})
    assert got == "X is Y | Evidence: auth.py:23 | SUPERSEDES: the old belief"


def test_enc_observation_omits_absent_optional_fields():
    assert wr.enc_observation({"fact": "X is Y"}) == "X is Y"


def test_lane_appends_to_encoding_queue_via_the_daemon_wrapper(monkeypatch):
    """The write MUST go through `wm-append.sh`, never `wm.py` directly.

    `wm-append.sh` is daemon-only since the 2026-05-29 cutover and
    `wm_write.py::append_slot` is the live write path (guard-742). A direct CLI
    append resolves its backend differently from every other writer on the box —
    the split-brain class `no-python-cli-fallback.md` exists to prevent.
    """
    seen = _capture_run(monkeypatch)
    rc, _out, _err = wr._lane_encoding(
        ITEM, "zeta", "2026-08-21T17:00:00", ROOT, [{"fact": "X is Y"}])

    assert rc == 0
    assert len(seen) == 1
    argv, stdin = seen[0]
    assert argv[1].endswith("core/scripts/wm-append.sh"), argv
    assert "wm.py" not in " ".join(argv)
    assert argv[2] == wr.ENC_QUEUE_SLOT == "encoding_queue"
    assert stdin is not None, "the item to append is the wrapper's stdin body"


def test_lane_payload_carries_every_field_consolidation_reads(monkeypatch):
    seen = _capture_run(monkeypatch)
    wr._lane_encoding(ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
                      [{"fact": "X is Y", "suggested_node": "system/x"}])

    payload = json.loads(seen[0][1])
    assert payload == {
        "source_goal": "g-306-1",
        "observation": "X is Y",
        "target_article": "system/x",
        "replay_priority": wr.ENC_REPLAY_PRIORITY,
        "captured_by": "zeta",
        "captured_at": "2026-08-21T17:00:00",
    }


@pytest.mark.parametrize("suggested", [None, "", "   ", "null", "NULL"])
def test_target_article_normalises_absent_placeholders_to_none(monkeypatch,
                                                               suggested):
    """`target_article` is a NON-BINDING hint; consolidation decides placement.

    A literal "null" string reaching the queue would read as a real node key.
    """
    seen = _capture_run(monkeypatch)
    wr._lane_encoding(ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
                      [{"fact": "X is Y", "suggested_node": suggested}])
    assert json.loads(seen[0][1])["target_article"] is None


def test_lane_queues_every_usable_entry_and_skips_the_malformed_ones(monkeypatch):
    seen = _capture_run(monkeypatch)
    rc, out, _err = wr._lane_encoding(
        ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
        [{"fact": "one"}, {"evidence": "no fact here"}, {"fact": "two"}])

    assert rc == 0
    assert len(seen) == 2, "the fact-less entry must not produce an append"
    assert "2" in out


def test_one_fact_captured_twice_queues_once(monkeypatch):
    """Criterion 2: a hint must not produce a duplicate node.

    `encoding_queue` is deliberately NOT in `wm.ARRAY_SLOTS`, so it does NOT get
    body-merge's `_dedup_append` content-hash dedup — nothing downstream would
    collapse these. The lane has to.
    """
    seen = _capture_run(monkeypatch)
    rc, out, _err = wr._lane_encoding(
        ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
        [{"fact": "X is Y"}, {"fact": "X is Y"}])

    assert rc == 0
    assert len(seen) == 1, "the same observation must not queue twice"
    assert "1" in out


def test_dedup_keys_on_the_observation_not_the_whole_payload(monkeypatch):
    """`suggested_node` is a NON-BINDING hint, so it cannot split one fact in two.

    Two captures of the same fact that merely disagree about where it should
    live are still ONE node.
    """
    seen = _capture_run(monkeypatch)
    wr._lane_encoding(ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
                      [{"fact": "X is Y", "suggested_node": "system/a"},
                       {"fact": "X is Y", "suggested_node": "system/b"}])

    assert len(seen) == 1
    # First writer wins, so the surviving hint is deterministic rather than
    # whichever entry happened to be last.
    assert json.loads(seen[0][1])["target_article"] == "system/a"


def test_distinct_facts_are_not_collapsed(monkeypatch):
    """POSITIVE CONTROL for the two tests above.

    Without this, a lane that queued NOTHING would satisfy both dedup
    assertions — 'no duplicates' is trivially true of an empty queue
    (guard-2421: positive-control a zero before believing it).
    """
    seen = _capture_run(monkeypatch)
    rc, _out, _err = wr._lane_encoding(
        ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
        [{"fact": "X is Y"}, {"fact": "P is Q"}])

    assert rc == 0
    assert len(seen) == 2, "distinct facts are distinct nodes — do not collapse"
    obs = sorted(json.loads(s)["observation"] for _a, s in seen)
    assert obs == ["P is Q", "X is Y"]


def test_dedup_does_not_reach_across_goals(monkeypatch):
    """Dedup is per-batch, and a batch is one goal. Two goals may share a fact.

    Collapsing across goals would silently drop the second goal's provenance.
    """
    seen = _capture_run(monkeypatch)
    wr._lane_encoding(ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
                      [{"fact": "X is Y"}])
    other = dict(ITEM, goal_id="g-306-2")
    wr._lane_encoding(other, "zeta", "2026-08-21T17:00:00", ROOT,
                      [{"fact": "X is Y"}])

    assert len(seen) == 2
    assert sorted(json.loads(s)["source_goal"] for _a, s in seen) == [
        "g-306-1", "g-306-2"]


def test_an_all_malformed_batch_fails_the_lane_rather_than_reporting_success(
        monkeypatch):
    """rc=0 here would stamp the retrospective marker on unencoded work.

    The marker suppresses the retry PERMANENTLY, so a lane that ran and queued
    nothing must not count toward `wrote`. Recoverable beats permanent.
    """
    seen = _capture_run(monkeypatch)
    rc, _out, err = wr._lane_encoding(
        ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
        [{"evidence": "no fact"}, {"not": "a fact either"}])

    assert seen == [], "nothing usable, so nothing should have been appended"
    assert rc != 0
    assert "fact" in err


def test_a_failing_append_propagates_its_rc_and_stops_the_batch(monkeypatch):
    seen = _capture_run(monkeypatch, rc=1, err="daemon unreachable")
    rc, _out, err = wr._lane_encoding(
        ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
        [{"fact": "one"}, {"fact": "two"}])

    assert rc == 1
    assert "daemon unreachable" in err
    assert len(seen) == 1, "a failed append must not silently continue the batch"


# ─────────────────────── half 2: the call-site wiring ────────────────────────
#
# Everything above passes against a lane nobody calls. These do not.

def test_encoding_is_a_declared_run_lane():
    """`RUN_LANES` is what the driver reports as the lanes it ran."""
    assert "encoding" in wr.RUN_LANES


def _stub_lanes(monkeypatch, dispatched: list):
    """Stub every lane so only dispatch is under test; record encoding calls."""
    def _ok(*a, **k):
        return 0, "", ""

    for name in ("_lane_team_state", "_lane_journal", "_lane_findings",
                 "_lane_experience", "_lane_impk"):
        monkeypatch.setattr(wr, name, _ok)
    monkeypatch.setattr(wr, "_write_marker", _ok)

    def _enc(item, agent, now_iso, root, entries):
        dispatched.append((item["goal_id"], list(entries)))
        return 0, "queued", ""

    monkeypatch.setattr(wr, "_lane_encoding", _enc)


def test_retrospect_dispatches_the_lane_when_a_capture_arrived(monkeypatch):
    dispatched: list = []
    _stub_lanes(monkeypatch, dispatched)

    out = wr.retrospect(ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
                        enc_captures={"g-306-1": [{"fact": "X is Y"}]})

    assert dispatched == [("g-306-1", [{"fact": "X is Y"}])]
    assert out["lanes"]["encoding"]["ok"] is True
    assert out["lanes"]["encoding"]["entries"] == 1


def test_retrospect_skips_the_lane_with_a_named_reason_when_no_capture(monkeypatch):
    dispatched: list = []
    _stub_lanes(monkeypatch, dispatched)

    out = wr.retrospect(ITEM, "zeta", "2026-08-21T17:00:00", ROOT)

    assert dispatched == []
    lane = out["lanes"]["encoding"]
    assert lane["ok"] is False and lane["rc"] is None
    assert lane["skipped"] == wr.SKIP_NO_ENCODING


def test_a_capture_for_another_goal_does_not_leak_into_this_one(monkeypatch):
    dispatched: list = []
    _stub_lanes(monkeypatch, dispatched)

    out = wr.retrospect(ITEM, "zeta", "2026-08-21T17:00:00", ROOT,
                        enc_captures={"g-306-999": [{"fact": "someone else's"}]})

    assert dispatched == []
    assert out["lanes"]["encoding"]["skipped"] == wr.SKIP_NO_ENCODING


def _stub_driver(monkeypatch, enc_slot: dict, seen: dict):
    """Stub main()'s surroundings so only the enc wiring is under test."""
    monkeypatch.setattr(wr, "body_role", lambda agent: "reducer")
    monkeypatch.setattr(wr, "load_records",
                        lambda ids, root: {i: {"goal_id": i, "source": "world",
                                               "title": "t",
                                               "aspiration_id": "asp-306"}
                                           for i in ids})
    monkeypatch.setattr(wr, "load_exp_captures", lambda root: {})
    monkeypatch.setattr(wr, "load_enc_captures", lambda root: enc_slot)

    def _spy(item, agent, now_iso, root, captures=None, enc_captures=None):
        seen["enc_captures"] = enc_captures
        return {"goal_id": item["goal_id"], "lanes": {}, "marked": True}

    monkeypatch.setattr(wr, "retrospect", _spy)


def test_apply_loads_the_slot_and_hands_it_to_retrospect(monkeypatch, capsys):
    """THE hyp_capture test: prove the value REACHES the consumer.

    A lane wired to a loader that is never called — or whose result is never
    passed down — behaves exactly like an empty slot, on every invocation,
    silently. Only an end-to-end driver call can tell the two apart.
    """
    slot = {"g-306-1": [{"fact": "X is Y"}]}
    seen: dict = {}
    _stub_driver(monkeypatch, slot, seen)

    rc = wr.main(["--agent", "zeta", "--goal-ids", "g-306-1", "--apply"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert seen["enc_captures"] == slot, \
        "main() must pass the loaded encoding_capture slot into retrospect"
    assert out["encoding_capture_goals"] == ["g-306-1"]
    assert "encoding" in out["run_lanes"]


def test_dry_run_reports_whether_the_lane_would_fire(monkeypatch, capsys):
    """A dry run that cannot show the lane firing makes it unverifiable."""
    seen: dict = {}
    _stub_driver(monkeypatch, {"g-306-1": [{"fact": "X is Y"}]}, seen)

    rc = wr.main(["--agent", "zeta", "--goal-ids", "g-306-1"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["would_encode_encoding"] == ["g-306-1"]
    assert out["encoding_capture_goals"] == ["g-306-1"]


def test_dry_run_reports_no_fire_when_the_slot_is_empty(monkeypatch, capsys):
    """Positive control for the test above — an empty slot must read empty."""
    seen: dict = {}
    _stub_driver(monkeypatch, {}, seen)

    wr.main(["--agent", "zeta", "--goal-ids", "g-306-1"])
    out = json.loads(capsys.readouterr().out)

    assert out["would_encode_encoding"] == []
    assert out["encoding_capture_goals"] == []


def test_load_enc_captures_reads_the_encoding_capture_slot(monkeypatch):
    """The loader must name `encoding_capture`, not its exp_capture sibling."""
    seen = _capture_run(monkeypatch)
    wr.load_enc_captures(ROOT)

    argv = seen[0][0]
    assert argv[1].endswith("core/scripts/wm-read.sh"), argv
    assert argv[2] == wr.ENC_SLOT == "encoding_capture"
