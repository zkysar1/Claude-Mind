#!/usr/bin/env python3
"""Pins ARCHIVE-BEFORE-DELETE on the generalize-down path ().

WHY THIS FILE EXISTS, and why it is a separate file from
``test_body_merge_array_limits.py`` rather than more cases inside it. That file
pins that the merge ENFORCES the caps (g-306-309); its own docstring frames the
harm it fixed as unbounded growth, *"NOT data loss ... Nothing was being
destroyed on this path precisely because nothing evicted"*. That sentence was
true when it was written and stopped being true in the same change: once the
merge evicts, it destroys. ``wm.enforce_slot_limit`` carries a ``may_evict``
seam for exactly this — its docstring says *"the append path uses it to ARCHIVE
a capture victim before it is destroyed; the merge path passes nothing"* — and
that seam was left unpassed here. So the two files pin opposite halves of one
contract and must not be merged: enforcement without archiving is the defect.

MEASURED, on the box this fix was written on (alpha, hostname cc-04, uname -r
6.8.0-139-generic, own-cloud, 2026-09-15): the reducer held ``spark_capture``
3152 against cap 50 and ``encoding_capture`` 3301 uncapped-by-design, with 10
cross-box staged Body units still to drain. Draining them through the
pre-fix merge would have popped ~3100 capture entries with no copy anywhere.

WHY THE ARCHIVE IS A WAL AND NOT A DIRECT SINK APPEND — the design property
``test_store_io_stays_outside_the_wm_lock`` below exists to protect. ``merge_wm``
runs inside ``wm.wm_lock_for(reducer_wm_path)``, which breaks at
``stale_seconds=10``; ONE ``wm.archive_evicted_capture`` append to the durable
sink measured **0.968s** against an 18,185,861-byte archive on this box, so ten
victims would break the lock and a real drain holds it for ~50 minutes. The
victim therefore goes to a machine-local fsync'd journal under ``sessions/``
(no store round trip) inside the lock, and the batch moves to the durable sink
in ONE ``locked_modify_jsonl`` cycle after the lock is released.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import wm  # noqa: E402


def _load_body_merge():
    spec = importlib.util.spec_from_file_location(
        "body_merge_capture_archive_uut", SCRIPTS / "body-merge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def bmg():
    return _load_body_merge()


@pytest.fixture(scope="module")
def limits():
    lim = wm.get_pruning_config(wm.read_config()).get("array_limits", {}) or {}
    assert lim, "CONTROL FAILED: array_limits is empty — config did not load"
    return lim


def entry(i: int) -> dict:
    return {"goal_id": f"g-{i:04d}",
            "_item_ts": f"2026-09-15T{i // 60:02d}:{i % 60:02d}:00"}


# ── the emitter seam ────────────────────────────────────────────────────────

def test_default_none_leaves_enforcement_exactly_as_it_was(bmg, limits):
    """The opt-in property. Every existing caller and every test in the sibling
    file calls merge_wm with two or three positional args; if the default
    changed behaviour, this change would be a silent regression of g-306-309
    rather than an addition to it."""
    cap = limits["spark_capture"]
    reducer = {"slots": {"spark_capture": [entry(i) for i in range(cap + 7)]}}

    out = bmg.merge_wm(reducer, {"slots": {}})

    assert len(out["slots"]["spark_capture"]) == cap
    assert out["capture_evictions"]["spark_capture"] == 7


def test_every_victim_reaches_the_emitter_while_still_in_the_slot(bmg, limits):
    """ARCHIVE *BEFORE* DELETE, not archive-and-delete-together. The emitter is
    asked about a victim that is still present in the list, so a False can
    actually save it — the whole point of the contract. Asserting only the call
    count would pass against an implementation that popped first and archived
    after, which is the defect wearing the fix's uniform."""
    cap = limits["exp_capture"]
    reducer = {"slots": {"exp_capture": [entry(i) for i in range(cap + 4)]}}
    slot_ref = reducer["slots"]["exp_capture"]
    seen = []

    def emitter(slot_name, victim):
        assert slot_name == "exp_capture"
        assert any(x is victim for x in slot_ref), (
            "the victim was already popped when the emitter was asked — "
            "this is archive-AFTER-delete, which archives nothing recoverable"
        )
        seen.append(victim)
        return True

    out = bmg.merge_wm(reducer, {"slots": {}}, archive_victim=emitter)

    assert len(seen) == 4
    assert len(out["slots"]["exp_capture"]) == cap
    assert out["capture_evictions"]["exp_capture"] == 4
    # The archived victims are exactly the ones that left the slot.
    survivors = {id(x) for x in out["slots"]["exp_capture"]}
    assert all(id(v) not in survivors for v in seen)


def test_a_refused_archive_keeps_the_entry_and_the_lane_over_cap(bmg, limits):
    """THE LOAD-BEARING FAIL-SAFE. wm.archive_evicted_capture's docstring makes
    the return value a contract: False means the caller MUST KEEP the entry.
    An over-cap lane costs memory and is visible in the next prune report; a
    destroyed capture is unrecoverable — the WM store is git-untracked and the
    own-cloud recovery config is unreadable from the fleet identity."""
    cap = limits["hyp_capture"]
    n = cap + 5
    reducer = {"slots": {"hyp_capture": [entry(i) for i in range(n)]}}

    out = bmg.merge_wm(reducer, {"slots": {}},
                       archive_victim=lambda s, v: False)

    assert len(out["slots"]["hyp_capture"]) == n, "a refused archive destroyed data"
    assert "capture_evictions" not in out, (
        "nothing was evicted, so no tally may be stamped — an always-present "
        "counter reads as 'measured zero' rather than 'never fired'"
    )


def test_eviction_stops_at_the_first_refusal(bmg, limits):
    """enforce_slot_limit BREAKS rather than CONTINUES on a False, because the
    loop re-tests the same over-limit condition and a continue would ask about
    the same victim forever. Two archived then a refusal must leave exactly two
    evicted, not 'skip that one and keep going'."""
    cap = limits["spark_capture"]
    reducer = {"slots": {"spark_capture": [entry(i) for i in range(cap + 6)]}}
    calls = {"n": 0}

    def emitter(slot_name, victim):
        calls["n"] += 1
        return calls["n"] <= 2

    out = bmg.merge_wm(reducer, {"slots": {}}, archive_victim=emitter)

    assert out["capture_evictions"]["spark_capture"] == 2
    assert len(out["slots"]["spark_capture"]) == cap + 4
    assert calls["n"] == 3, "the loop did not stop at the refusal"


def test_non_capture_slots_never_reach_the_emitter(bmg, limits):
    """Scope pin. The emitter archives to the CAPTURE eviction sink, whose row
    schema and consumers are capture-specific; routing a sensory_buffer or
    micro_hypotheses victim through it would pollute that store. Those lanes
    still get capped — this narrows WHO is archived, never WHETHER caps apply."""
    other = [s for s in limits
             if s not in wm.CAPTURE_SLOTS and s != "encoding_queue"]
    assert other, "CONTROL FAILED: no non-capture capped slot to test against"
    sk = other[0]
    cap = limits[sk]
    reducer = {"slots": {sk: [entry(i) for i in range(cap + 3)]}}
    calls = []

    out = bmg.merge_wm(reducer, {"slots": {}},
                       archive_victim=lambda s, v: calls.append(s) or True)

    assert calls == [], f"{sk} is not a capture slot but was routed to the emitter"
    assert len(out["slots"][sk]) == cap, "the cap stopped being enforced"


def test_the_emitter_is_told_which_slot_each_victim_came_from(bmg, limits):
    """The closure is bound per slot with a default argument, not by closing
    over the loop variable — the classic late-binding bug would report every
    victim as belonging to the LAST capped slot, and the archive row's `slot`
    field is what a restore reads to know where to put the entry back."""
    reducer = {"slots": {
        "spark_capture": [entry(i) for i in range(limits["spark_capture"] + 2)],
        "exp_capture": [entry(500 + i) for i in range(limits["exp_capture"] + 3)],
    }}
    got = {}

    def emitter(slot_name, victim):
        got.setdefault(slot_name, 0)
        got[slot_name] += 1
        return True

    bmg.merge_wm(reducer, {"slots": {}}, archive_victim=emitter)

    assert got == {"spark_capture": 2, "exp_capture": 3}


# ── the write-ahead journal and its drain ───────────────────────────────────

def test_journal_lives_under_sessions_not_beside_the_agent_wide_wm(bmg, tmp_path):
    """wm.py puts the agent-wide WM out of scope for its own local archive: a
    file there sits in the synced tree, whose post-PUT rewrite of the local copy
    can erase a row appended mid-push. sessions/ is machine-local."""
    state_dir = tmp_path / "agents" / "alpha" / "session"
    state_dir.mkdir(parents=True)

    journal = bmg._capture_eviction_journal(state_dir)

    assert journal.parent.name == "sessions"
    assert journal.parent.parent == state_dir.parent
    assert journal.parent != state_dir


def test_drain_moves_every_row_to_the_sink_and_unlinks_the_journal(bmg, tmp_path,
                                                                   monkeypatch):
    agent_dir = tmp_path / "agents" / "alpha"
    (agent_dir / "sessions").mkdir(parents=True)
    journal = agent_dir / "sessions" / bmg._CAPTURE_EVICTION_JOURNAL
    rows = [{"slot": "spark_capture", "entry": {"goal_id": f"g-{i}"}} for i in range(3)]
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    written = {}

    def fake_locked_modify_jsonl(path, modifier_fn, **kw):
        out = modifier_fn([{"slot": "spark_capture", "entry": {"goal_id": "pre"}}])
        written["path"] = path
        written["items"] = out
        return out

    import _fileops
    monkeypatch.setattr(_fileops, "locked_modify_jsonl", fake_locked_modify_jsonl)

    result = bmg._drain_capture_eviction_journal(journal, agent_dir)

    assert result == {"journalled": 3, "archived": 3, "drain_error": None}
    assert written["path"] == agent_dir / wm.CAPTURE_EVICTION_ARCHIVE
    assert len(written["items"]) == 4, "the drain replaced the sink instead of appending"
    assert written["items"][0]["entry"]["goal_id"] == "pre"
    assert not journal.exists()


def test_drain_keeps_the_journal_when_the_sink_write_fails(bmg, tmp_path,
                                                           monkeypatch):
    """Same failure direction as the emitter: an archive that did not land must
    never be followed by a delete. Keeping the journal costs a duplicate row on
    the re-drain; unlinking it costs the only copy."""
    agent_dir = tmp_path / "agents" / "alpha"
    (agent_dir / "sessions").mkdir(parents=True)
    journal = agent_dir / "sessions" / bmg._CAPTURE_EVICTION_JOURNAL
    journal.write_text(json.dumps({"slot": "spark_capture", "entry": {}}) + "\n",
                       encoding="utf-8")

    import _fileops

    def boom(path, modifier_fn, **kw):
        raise OSError("sink unreachable")

    monkeypatch.setattr(_fileops, "locked_modify_jsonl", boom)

    result = bmg._drain_capture_eviction_journal(journal, agent_dir)

    assert result["archived"] == 0
    assert "sink unreachable" in result["drain_error"]
    assert journal.exists(), "a failed drain deleted the only copy"


def test_drain_keeps_an_unparseable_row_rather_than_stepping_over_it(bmg, tmp_path,
                                                                     monkeypatch):
    agent_dir = tmp_path / "agents" / "alpha"
    (agent_dir / "sessions").mkdir(parents=True)
    journal = agent_dir / "sessions" / bmg._CAPTURE_EVICTION_JOURNAL
    journal.write_text(
        json.dumps({"slot": "spark_capture", "entry": {"goal_id": "ok"}}) + "\n"
        + "{not json\n", encoding="utf-8")

    import _fileops
    monkeypatch.setattr(_fileops, "locked_modify_jsonl",
                        lambda path, fn, **kw: fn([]))

    result = bmg._drain_capture_eviction_journal(journal, agent_dir)

    assert result["journalled"] == 1
    assert result["drain_error"] == "unparseable_row_kept"
    assert journal.exists(), "the torn row was discarded with the file"


def test_drain_is_a_noop_with_no_journal(bmg, tmp_path):
    agent_dir = tmp_path / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    journal = agent_dir / "sessions" / bmg._CAPTURE_EVICTION_JOURNAL

    assert bmg._drain_capture_eviction_journal(journal, agent_dir) == {
        "journalled": 0, "archived": 0, "drain_error": None}


# ── the design property the WAL exists for ──────────────────────────────────

def test_store_io_stays_outside_the_wm_lock():
    """STRUCTURAL PIN, and the reason the WAL exists at all. Both call sites
    state that store I/O stays outside wm_lock_for, which breaks at
    stale_seconds=10 (guard-1965). A future edit that "simplifies" the drain by
    calling it inside the `with` block would pass every behavioural test above
    and convert the lock into a stale-break generator under any real drain.

    Checked by INDENTATION because that is what actually encodes the nesting:
    the drain call must sit at the same depth as the `with` statement, never
    deeper."""
    src = (SCRIPTS / "body-merge.py").read_text(encoding="utf-8").splitlines()
    drains = [(i, ln) for i, ln in enumerate(src)
              if "_drain_capture_eviction_journal(" in ln and "def " not in ln]
    assert len(drains) == 2, (
        f"expected exactly 2 drain call sites, found {len(drains)} — "
        "a new merge_wm caller was added without its drain, or one was lost"
    )
    for i, ln in drains:
        depth = len(ln) - len(ln.lstrip())
        opener = None
        for j in range(i - 1, max(i - 40, -1), -1):
            if "with wm.wm_lock_for(" in src[j]:
                opener = src[j]
                break
        assert opener is not None, f"line {i + 1}: no wm_lock_for block above the drain"
        open_depth = len(opener) - len(opener.lstrip())
        assert depth <= open_depth, (
            f"line {i + 1}: the drain is INSIDE the wm_lock_for block "
            f"(indent {depth} > {open_depth}) — that puts a store round trip "
            "inside a lock that breaks at stale_seconds=10"
        )


def test_both_merge_wm_call_sites_pass_the_emitter():
    """Coverage pin with a positive control. Adding the parameter is worthless
    if a caller forgets it — and a caller that forgets it fails silently, by
    destroying data exactly as before."""
    import re
    src = (SCRIPTS / "body-merge.py").read_text(encoding="utf-8")
    # An ASSIGNMENT from merge_wm, which is what a real call site is. Matching
    # the bare token instead sweeps up the module docstring's own prose
    # reference to `merge_wm(reducer, body, baseline)` — measured, it did.
    calls = re.findall(r"^\s*\w+ = merge_wm\(", src, re.MULTILINE)
    assert len(calls) == 2, f"expected 2 merge_wm call sites, found {len(calls)}: {calls}"
    assert src.count("archive_victim=_archive_victim") == 2, (
        "a merge_wm call site does not pass archive_victim — that site still "
        "destroys capture entries with no copy anywhere"
    )
