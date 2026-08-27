#!/usr/bin/env python3
"""Pins for cross-world retrieval ().

Two properties are load-bearing and everything else here is scaffolding:

  1. "peer had nothing" and "peer was unreachable" must NOT render identically.
     A cross-world retrieval whose silence is ambiguous manufactures confident
     negatives, which is worse than having no cross-world retrieval at all.
  2. The peer read must not inherit THIS world's storage backend. Proven by
     construction: the module never imports _fileops, and an unreachable peer
     reports `backend_used: None` with its OWN declared backend named.

Each pin below was verified RED by mutation before being kept.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import peer_retrieve as pr  # noqa: E402


SELF_ENV = "world-a"
PEER_REACHABLE = "world-b"
PEER_UNREACHABLE = "world-c"


def _make_world(root, name, board_rows=None, tree_docs=None):
    w = root / name
    (w / "board").mkdir(parents=True)
    (w / "knowledge" / "tree").mkdir(parents=True)
    rows = board_rows or []
    import json
    (w / "board" / "findings.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    for fname, body in (tree_docs or {}).items():
        (w / "knowledge" / "tree" / fname).write_text(body, encoding="utf-8")
    return w


@pytest.fixture
def worlds(tmp_path):
    """world-a = self (holds one inbound peer post), world-b = reachable peer, world-c = not."""
    self_w = _make_world(tmp_path, "world-a", board_rows=[
        {"id": "m-local-1", "author": "alpha", "text": "local note about widget calibration"},
        {"id": "m-inbound-1", "author": "omni@world-c",
         "text": "peer published a widget deadline finding to the shared channel"},
    ])
    peer_w = _make_world(tmp_path, "world-b", board_rows=[
        {"id": "m-peer-1", "author": "beta", "text": "widget deadline handling in world-b"},
    ], tree_docs={"widget.md": "# widget\nthe widget deadline is computed downstream"})
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "own-cloud"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local",
                         "peer_world_path": str(peer_w)},
        PEER_UNREACHABLE: {"environment_id": PEER_UNREACHABLE, "backend": "local"},
    }
    return {"root": tmp_path, "self": self_w, "peer": peer_w, "registry": registry}


def _world(result, env_id):
    return next(w for w in result["worlds"] if w["env_id"] == env_id)


def _run(worlds, query, **kw):
    return pr.retrieve(query, self_env=SELF_ENV, self_world=str(worlds["self"]),
                       registry=worlds["registry"], **kw)


# ---------------------------------------------------------------- positive control

def test_reachable_peer_returns_hits(worlds):
    """Positive control: the searcher works, so an `empty` elsewhere is a MEASURED
    empty rather than a broken search returning zero for every input."""
    res = _run(worlds, "widget deadline")
    peer = _world(res, PEER_REACHABLE)
    assert peer["status"] == pr.STATUS_HIT
    assert peer["count"] > 0
    assert peer["completeness"] == pr.COMPLETE


def test_self_world_is_searched(worlds):
    res = _run(worlds, "widget calibration")
    me = _world(res, SELF_ENV)
    assert me["status"] == pr.STATUS_HIT
    assert me["role"] == "self"


# ------------------------------------------------- THE pin: empty != unreachable

def test_empty_and_unreachable_are_distinct_statuses(worlds):
    """A peer that was READ and had nothing must not carry the same status as a
    peer that could not be read at all."""
    res = _run(worlds, "nonexistent-token-zzz")
    reachable = _world(res, PEER_REACHABLE)
    unreachable = _world(res, PEER_UNREACHABLE)
    assert reachable["status"] == pr.STATUS_EMPTY
    assert unreachable["status"] == pr.STATUS_UNREACHABLE
    assert reachable["status"] != unreachable["status"]
    assert reachable["completeness"] == pr.COMPLETE
    assert unreachable["completeness"] == pr.PARTIAL


def test_empty_and_unreachable_do_not_render_identically(worlds):
    """The JSON distinction is worthless if the human-readable output collapses it.
    This is the pin the design constraint actually asks for."""
    res = _run(worlds, "nonexistent-token-zzz")
    text = pr.render(res)
    lines = text.splitlines()

    reachable_line = next(l for l in lines if PEER_REACHABLE in l and "[" in l)
    unreachable_line = next(l for l in lines if PEER_UNREACHABLE in l and "[" in l)
    assert reachable_line != unreachable_line

    assert "UNREACHABLE" in text
    assert "read OK, no matches" in text
    # the unreachable world's reason must be visible, not buried in a log
    assert "not addressable from here" in text
    # and the reader must be told absence is not evidence of absence
    assert "NOT evidence of absence" in text


def test_partial_verdict_and_exit_code_flag_unreachability(worlds):
    res = _run(worlds, "nonexistent-token-zzz")
    assert res["verdict"] == pr.PARTIAL
    assert PEER_UNREACHABLE in res["partial_envs"]
    assert PEER_REACHABLE not in res["partial_envs"]


def test_all_reachable_yields_complete_verdict(tmp_path):
    self_w = _make_world(tmp_path, "world-a")
    peer_w = _make_world(tmp_path, "world-b")
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "own-cloud"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local",
                         "peer_world_path": str(peer_w)},
    }
    res = pr.retrieve("anything", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    assert res["verdict"] == pr.COMPLETE
    assert res["partial_envs"] == []


# --------------------------------------------- status and completeness are orthogonal

def test_hit_via_channel_still_reports_partial(worlds):
    """world-c is unreachable directly, but its agent published to OUR board. We
    return the hit AND still say the world was only partially seen. Collapsing
    these into one field is how 'found something' starts implying 'saw everything'."""
    res = _run(worlds, "widget deadline")
    w = _world(res, PEER_UNREACHABLE)
    assert w["status"] == pr.STATUS_HIT
    assert w["completeness"] == pr.PARTIAL
    assert any(r["author"] == "omni@world-c" for r in w["results"])
    text = pr.render(res)
    assert "UNREACHABLE" in text


# ------------------------------------------------------- backend non-inheritance

def test_unreachable_peer_never_reports_caller_backend(worlds, monkeypatch):
    """Caller is own-cloud; peer declares local. The peer lane must name the PEER's
    backend and must not record any backend as USED -- the read does not happen."""
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    res = _run(worlds, "nonexistent-token-zzz")
    direct = next(l for l in _world(res, PEER_UNREACHABLE)["lanes"] if l["lane"] == "direct")
    assert direct["backend_declared"] == "local"
    assert direct["backend_used"] is None
    assert "own-cloud" not in (direct["reason"] or "")
    assert "local" in direct["reason"]


def test_reachable_peer_reads_filesystem_not_a_backend(worlds, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    res = _run(worlds, "widget deadline")
    direct = next(l for l in _world(res, PEER_REACHABLE)["lanes"] if l["lane"] == "direct")
    assert direct["access"] == "filesystem"
    assert direct["backend_used"] is None


def test_fileops_is_never_imported(worlds):
    """_fileops binds STORAGE_BACKEND at import time, so importing it during a peer
    read is exactly how a peer's key shape gets transacted against the caller's
    bucket (the 2026-07-09 truncation class). This is the non-inheritance proof:
    checkable at runtime, not asserted in prose."""
    sys.modules.pop("_fileops", None)
    res = _run(worlds, "widget deadline")
    assert pr.assert_no_fileops()
    assert res["fileops_imported"] is False


def test_no_backend_declared_refuses_rather_than_guessing(tmp_path):
    self_w = _make_world(tmp_path, "world-a")
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "own-cloud"},
        "world-d": {"environment_id": "world-d"},  # no backend key at all
    }
    res = pr.retrieve("anything", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    direct = next(l for l in _world(res, "world-d")["lanes"] if l["lane"] == "direct")
    assert direct["status"] == pr.STATUS_UNREACHABLE
    assert direct["backend_used"] is None
    assert "refusing to guess" in direct["reason"]


def test_remote_backend_peer_refuses_without_a_path(tmp_path):
    """A peer on a REMOTE backend is not read through our client -- it is refused
    with its own backend named. This is the guard-955 direction stated as behavior."""
    self_w = _make_world(tmp_path, "world-a")
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"},
        "world-e": {"environment_id": "world-e", "backend": "own-cloud",
                    "bucket": "someone-elses-bucket"},
    }
    res = pr.retrieve("anything", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    direct = next(l for l in _world(res, "world-e")["lanes"] if l["lane"] == "direct")
    assert direct["status"] == pr.STATUS_UNREACHABLE
    assert direct["backend_declared"] == "own-cloud"
    assert direct["backend_used"] is None
    assert "guard-955" in direct["reason"]


# ------------------------------------------------------------------ G5 provenance

def test_peer_results_carry_origin_env(worlds):
    res = _run(worlds, "widget deadline")
    for env in (PEER_REACHABLE, PEER_UNREACHABLE):
        w = _world(res, env)
        for r in w["results"]:
            assert r["origin_env"] == env, "peer content must never lose its origin world (G5)"


def test_posture_is_read_only(worlds):
    res = _run(worlds, "widget deadline")
    assert res["posture"] == "read-only"
    assert "READ-ONLY" in pr.render(res)


# ------------------------------------------------------------------------- misc

def test_empty_query_is_a_usage_error(worlds):
    with pytest.raises(ValueError):
        _run(worlds, "   ")


def test_unresolvable_self_env_degrades_to_partial_not_to_a_clean_negative(tmp_path):
    """DELIBERATE DIVERGENCE from peer_envs()'s fail-safe -- do not "align" them.

    `peer_envs(registry, None)` returns NO peers, which is right for its own
    callers: they ROUTE work, and treating this world as a peer would push local
    work at someone else. Inheriting that posture here would be wrong in the one
    direction this module exists to prevent -- a retrieval that consulted ZERO
    peers would report `verdict: complete`, i.e. an EARNED negative, when it had
    not looked at a single peer. That is the aggregator collapse (guard-4093)
    moved up to the top level.

    So an unresolvable self_env enumerates every registered env as a peer. They
    are then almost all unreachable, the verdict is PARTIAL, and the caller is
    told it did not see everything. Same principle _peer_registry.py states in
    its own docstring: share I/O, never share policy between consumers whose
    wrong answers cost different things.
    """
    self_w = _make_world(tmp_path, "world-a")
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local"},
    }
    res = pr.retrieve("anything", self_env=None, self_world=str(self_w), registry=registry)
    assert [w["role"] for w in res["worlds"]].count("peer") == len(registry)
    # The load-bearing half: it must NOT claim it saw everything.
    assert res["verdict"] == pr.PARTIAL
    assert "NOT evidence of absence" in pr.render(res)


# ------------------------------- blind-source pins (fresh-eyes F-001 / F-002)
#
# Two further instances of THE SAME collapse, found by the post-state-update
# fresh-eyes pass on this module. Both are pinned here because both rendered a
# never-read source as an earned negative at rc=0 -- the exact outcome the module
# exists to make impossible.


def test_unreadable_registry_is_partial_not_a_clean_complete(tmp_path):
    """F-001. load_env_registry() is fail-open by contract: any yaml/dir/parse
    error yields fewer entries and a total failure yields {}. With {} no peer is
    enumerated at all, so before the fix `verdict` was COMPLETE at rc=0 --
    documented to mean "every registered world was fully read". A broken registry
    produced the most confident possible cross-world all-clear, silently."""
    self_w = _make_world(tmp_path, "world-a")
    res = pr.retrieve("anything", self_env=SELF_ENV, self_world=str(self_w), registry={})
    assert res["registry_unreadable"] is True
    assert res["verdict"] == pr.PARTIAL
    assert "<registry-unreadable>" in res["partial_envs"]
    text = pr.render(res)
    assert "UNREACHABLE" in text
    assert "NOT evidence of absence" in text


def test_registry_holding_only_self_stays_complete(tmp_path):
    """The other side of F-001, and the reason the guard tests emptiness rather
    than `len(registry) < 2`: a single-world deployment is legitimate and must
    NOT be told its own registry is broken."""
    self_w = _make_world(tmp_path, "world-a")
    registry = {SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"}}
    res = pr.retrieve("anything", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    assert res["registry_unreadable"] is False
    assert res["verdict"] == pr.COMPLETE


def test_unreadable_source_in_a_reachable_world_makes_the_lane_incomplete(tmp_path):
    """F-002. A world we CAN reach but whose files will not open was reported
    `empty` / `complete` / rc=0 -- an earned negative over content never read.
    Constructed with a directory-where-a-file-is-expected (IsADirectoryError is an
    OSError and, unlike a chmod, is not bypassed when running as root -- the first
    probe of this bug used chmod and was invalid for exactly that reason)."""
    self_w = _make_world(tmp_path, "world-a")
    peer_w = _make_world(tmp_path, "world-b")
    (peer_w / "board" / "findings.jsonl").unlink()
    (peer_w / "board" / "findings.jsonl").mkdir()
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local",
                         "peer_world_path": str(peer_w)},
    }
    res = pr.retrieve("widget", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    peer = _world(res, PEER_REACHABLE)
    direct = next(l for l in peer["lanes"] if l["lane"] == "direct")
    assert direct["complete"] is False
    assert direct["unreadable"], "the unreadable source must be named, not merely counted"
    assert peer["completeness"] == pr.PARTIAL
    assert res["verdict"] == pr.PARTIAL
    text = pr.render(res)
    assert "INCOMPLETE" in text
    assert "read OK, no matches" not in text.split("world-b")[-1].split("world-c")[0]


# ------------------- blind-search pins (fresh-eyes F-003 / F-004, 2026-08-17)
#
# Instances FIVE and SIX of the same collapse in this module, both found by the
# post-state-update fresh-eyes pass and both rendering a search that never
# happened as an earned negative at rc=0. Recorded together because the count is
# itself the finding: prose stating an invariant exerts no implementation
# pressure, only a test does.


def test_limit_below_one_is_refused_rather_than_reporting_an_earned_negative(worlds):
    """F-003. A limit below 1 stopped every lane before its first read and
    reported `empty` / `complete` / rc=0 -- which Tier 2.5 documents as "every
    registered world was fully read, so an empty here is an EARNED negative".

    Measured 2026-08-17 (bravo, hostname cc-05, uname -r 6.8.0-137-generic)
    against three reachable worlds holding matching content: `--limit 5` returned
    11 matches at rc=0, and `--limit 0` on the SAME box, worlds and query rendered
    "read OK, no matches" for every world, also at rc=0. There is no honest result
    from a search not permitted to look, so refusal is the only correct answer.
    """
    with pytest.raises(ValueError) as exc:
        _run(worlds, "widget deadline", limit=0)
    assert "must be >= 1" in str(exc.value)
    with pytest.raises(ValueError):
        _run(worlds, "widget deadline", limit=-1)
    # Positive control (guard-2421): the SAME query at a LEGAL limit still hits,
    # so the refusals above are about the limit and not a broken search.
    assert _world(_run(worlds, "widget deadline", limit=1), PEER_REACHABLE)["status"] == pr.STATUS_HIT


def test_wholly_unparseable_board_file_is_unreadable_not_empty(tmp_path):
    """F-004. A board file that OPENS but whose every line fails to parse hit the
    per-line `continue` and vanished, so the lane reported `empty` / `complete` /
    `unreadable: []` / `reason: None`. The OSError branch never fires because the
    open SUCCEEDED -- this is the F-002 collapse one layer in: a source we could
    not READ reported as a source with nothing in it."""
    self_w = _make_world(tmp_path, "world-a")
    peer_w = _make_world(tmp_path, "world-b")
    (peer_w / "board" / "findings.jsonl").write_text(
        "this is not json\n{ broken\nnor this one\n", encoding="utf-8")
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local",
                         "peer_world_path": str(peer_w)},
    }
    res = pr.retrieve("widget", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    peer = _world(res, PEER_REACHABLE)
    direct = next(l for l in peer["lanes"] if l["lane"] == "direct")
    assert direct["complete"] is False
    assert direct["unreadable"], "the corrupt file must be NAMED, not silently skipped"
    assert peer["completeness"] == pr.PARTIAL
    assert res["verdict"] == pr.PARTIAL
    assert "INCOMPLETE" in pr.render(res)


def test_one_torn_line_does_not_flag_an_otherwise_readable_file(tmp_path):
    """The other side of F-004, and why the predicate is `failed and not parsed`
    rather than any ratio: a torn tail line is normal in an append-only JSONL
    store and must NOT make a healthy world report PARTIAL. Without this pin the
    obvious over-correction (flag on ANY parse failure) passes the test above
    while making every live board read partial."""
    self_w = _make_world(tmp_path, "world-a")
    peer_w = _make_world(tmp_path, "world-b")
    (peer_w / "board" / "findings.jsonl").write_text(
        '{"id":"m1","author":"beta","text":"widget deadline handling"}\n{"id":"m2","aut\n',
        encoding="utf-8")
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local",
                         "peer_world_path": str(peer_w)},
    }
    res = pr.retrieve("widget deadline", self_env=SELF_ENV, self_world=str(self_w),
                      registry=registry)
    peer = _world(res, PEER_REACHABLE)
    assert peer["status"] == pr.STATUS_HIT
    assert peer["completeness"] == pr.COMPLETE


def test_empty_board_file_stays_complete(tmp_path):
    """A 0-row board file is parsed=0 failed=0 and must stay CLEAN -- it genuinely
    has nothing. Pins the boundary the F-004 guard must not cross."""
    self_w = _make_world(tmp_path, "world-a")
    peer_w = _make_world(tmp_path, "world-b")   # _make_world writes an empty board
    registry = {
        SELF_ENV: {"environment_id": SELF_ENV, "backend": "local"},
        PEER_REACHABLE: {"environment_id": PEER_REACHABLE, "backend": "local",
                         "peer_world_path": str(peer_w)},
    }
    res = pr.retrieve("widget", self_env=SELF_ENV, self_world=str(self_w), registry=registry)
    peer = _world(res, PEER_REACHABLE)
    assert peer["status"] == pr.STATUS_EMPTY
    assert peer["completeness"] == pr.COMPLETE
    assert res["verdict"] == pr.COMPLETE


# ------------------------------------------------- CLI integration path (sq-019)
#
# The tests above exercise retrieve() and render() directly. The INTEGRATION path
# -- argv -> main() -> exit code -- was untested, and the exit code is the
# load-bearing half of this tool's contract: retrieval-escalation.md Tier 2.5
# tells readers that rc=0 means an `empty` is an EARNED negative and rc=3 means
# absence is NOT evidence of absence. An untested exit code is an undefended
# contract, so a refactor could invert it and every engine test would still pass.


def _canned(partial):
    return {
        "query": "q", "self_env": SELF_ENV, "posture": "read-only",
        "worlds": [{"env_id": SELF_ENV, "role": "self", "status": pr.STATUS_EMPTY,
                    "completeness": pr.PARTIAL if partial else pr.COMPLETE,
                    "lanes": [{"lane": "direct",
                               "status": pr.STATUS_UNREACHABLE if partial else pr.STATUS_EMPTY,
                               "count": 0, "results": [], "reason": "reason text"}],
                    "count": 0, "results": []}],
        "partial_envs": [SELF_ENV] if partial else [],
        "verdict": pr.PARTIAL if partial else pr.COMPLETE,
        "fileops_imported": False,
    }


def test_cli_exits_3_when_any_world_is_partial(monkeypatch, capsys):
    monkeypatch.setattr(pr, "retrieve", lambda *a, **k: _canned(True))
    assert pr.main(["anything"]) == pr.EXIT_PARTIAL
    assert "UNREACHABLE" in capsys.readouterr().out


def test_cli_exits_0_when_every_world_was_fully_read(monkeypatch, capsys):
    monkeypatch.setattr(pr, "retrieve", lambda *a, **k: _canned(False))
    assert pr.main(["anything"]) == pr.EXIT_OK
    out = capsys.readouterr().out
    assert "UNREACHABLE" not in out
    assert "NOT evidence of absence" not in out


def test_cli_empty_query_is_a_usage_exit(capsys):
    with pytest.raises(SystemExit) as exc:
        pr.main([])
    assert exc.value.code == pr.EXIT_USAGE


def test_cli_limit_below_one_is_a_usage_exit_not_a_clean_zero(capsys):
    """F-003 on the path a reader actually invokes. guard-2285: retrieve()
    refusing proves nothing about the CLI. guard-1082: assert the SPECIFIC code
    -- `rc != 0` would also be satisfied by the rc=3 PARTIAL this tool emits
    routinely, so a coarse pass condition would not distinguish "refused" from
    "ran and found the fleet unreachable"."""
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit) as exc:
            pr.main(["--limit", bad, "anything"])
        assert exc.value.code == pr.EXIT_USAGE
        assert exc.value.code != pr.EXIT_PARTIAL
        assert exc.value.code != pr.EXIT_OK


def test_cli_json_mode_carries_the_three_valued_status(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(pr, "retrieve", lambda *a, **k: _canned(True))
    assert pr.main(["--json", "anything"]) == pr.EXIT_PARTIAL
    payload = _json.loads(capsys.readouterr().out)
    assert payload["verdict"] == pr.PARTIAL
    assert payload["worlds"][0]["lanes"][0]["status"] == pr.STATUS_UNREACHABLE
    assert payload["posture"] == "read-only"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------- 2026-08-21: rank + JSONL stores
# Under the pre-change code every pin in this section is RED by construction:
# the board scan filled the limit in glob order (rank test), and the two JSONL
# stores were never opened at all (store/retired/corrupt tests).

import json as _json


def _write_jsonl(world, fname, records):
    (world / fname).write_text(
        "\n".join(_json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_results_rank_by_score_not_file_order(tmp_path):
    """A dense tree match must beat a thin board row for the last slot.
    Pre-change, board rows filled the limit first regardless of quality."""
    w = _make_world(tmp_path, "rank-world", board_rows=[
        {"id": "m-thin", "author": "a", "text": "widget mentioned once"},
    ], tree_docs={"dense.md": "widget widget widget calibration of the widget"})
    results, unreadable = pr.search_world_dir(w, ["widget"], limit=1)
    assert unreadable == []
    assert len(results) == 1
    assert results[0]["store"] == "knowledge-tree"
    assert results[0]["score"] > 1


def test_phrase_adjacency_outranks_scattered_terms(tmp_path):
    w = _make_world(tmp_path, "phrase-world", tree_docs={
        "scattered.md": "deadline is set here and the widget sits elsewhere",
        "adjacent.md": "the widget deadline is computed downstream",
    })
    results, _ = pr.search_world_dir(w, ["widget", "deadline"], limit=2)
    assert [r["ref"] for r in results][0].endswith("adjacent.md")


def test_reasoning_bank_and_guardrails_are_searched(tmp_path):
    w = _make_world(tmp_path, "store-world")
    _write_jsonl(w, "reasoning-bank.jsonl", [
        {"id": "rb-1", "status": "active", "title": "widget deadline lesson",
         "content": "the widget deadline moves when the calibration slips"},
    ])
    _write_jsonl(w, "guardrails.jsonl", [
        {"id": "guard-1", "status": "active",
         "rule": "ALWAYS recheck the widget deadline before shipping"},
    ])
    results, unreadable = pr.search_world_dir(w, ["widget", "deadline"], limit=10)
    assert unreadable == []
    by_store = {r["store"]: r for r in results}
    assert by_store["reasoning-bank"]["ref"] == "rb-1"
    assert by_store["guardrails"]["ref"] == "guard-1"
    # Snippets come from the record's discriminative fields, not raw JSON.
    assert "widget deadline" in by_store["guardrails"]["snippet"].lower()


def test_retired_records_do_not_hit(tmp_path):
    w = _make_world(tmp_path, "retired-world")
    _write_jsonl(w, "reasoning-bank.jsonl", [
        {"id": "rb-old", "status": "retired", "title": "widget deadline lesson"},
        {"id": "rb-live", "status": "active", "title": "widget deadline lesson v2"},
    ])
    results, _ = pr.search_world_dir(w, ["widget", "deadline"], limit=10)
    refs = [r["ref"] for r in results]
    assert "rb-live" in refs
    assert "rb-old" not in refs


def test_wholly_corrupt_jsonl_store_is_unreadable_not_empty(tmp_path):
    """The board-file collapse (guard-4093 shape) one store over: a
    reasoning-bank file that opened but parsed nothing must flag the lane
    INCOMPLETE, never report a clean empty."""
    w = _make_world(tmp_path, "corrupt-world")
    (w / "reasoning-bank.jsonl").write_text("not json\nalso not json\n",
                                            encoding="utf-8")
    results, unreadable = pr.search_world_dir(w, ["widget"], limit=5)
    assert results == []
    assert any("reasoning-bank.jsonl" in u for u in unreadable)


def test_absent_jsonl_stores_stay_clean(tmp_path):
    """A fresh world with no reasoning bank yet is legitimately empty --
    absence of the store file must not flag unreadability."""
    w = _make_world(tmp_path, "fresh-world", tree_docs={"a.md": "widget"})
    results, unreadable = pr.search_world_dir(w, ["widget"], limit=5)
    assert unreadable == []
    assert len(results) == 1


# ── Tier classification () ─────────────────────────────────────────

def _pr():
    import importlib.util, sys as _s
    from pathlib import Path as _P
    sd = _P(__file__).resolve().parents[1]
    _s.path.insert(0, str(sd))
    spec = importlib.util.spec_from_file_location("pr_tier", sd / "peer_retrieve.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_board_is_public_everything_else_is_raw():
    m = _pr()
    assert m.store_tier("board/general") == m.TIER_PUBLIC
    assert m.store_tier("board/findings") == m.TIER_PUBLIC
    for s in ("knowledge-tree", "conventions", "reasoning-bank", "guardrails", ""):
        assert m.store_tier(s) == m.TIER_RAW, s


def test_default_tier_is_all_and_returns_rows_unchanged():
    """The non-breaking guarantee: defaulting to public would silently delete
    every tree/convention hit from the LIVE Tier 2.5 lane."""
    m = _pr()
    rows = [{"store": "board/general", "ref": "msg-1"},
            {"store": "knowledge-tree", "ref": "intelligence/agent"}]
    assert m.filter_by_tier(rows) == rows
    assert m.filter_by_tier(rows, m.TIER_ALL) == rows


def test_public_tier_keeps_only_board_rows():
    m = _pr()
    rows = [{"store": "board/general", "ref": "msg-1"},
            {"store": "knowledge-tree", "ref": "intelligence/agent"}]
    out = m.filter_by_tier(rows, m.TIER_PUBLIC)
    assert [r["store"] for r in out] == ["board/general"]


def test_a_granted_scope_admits_raw_rows_inside_that_subtree():
    m = _pr()
    rows = [{"store": "board/general", "ref": "msg-1"},
            {"store": "knowledge-tree", "ref": "intelligence/agent/memory"},
            {"store": "knowledge-tree", "ref": "performance/latency"}]
    out = m.filter_by_tier(rows, m.TIER_PUBLIC, scopes=["intelligence/agent"])
    refs = [r["ref"] for r in out]
    assert "msg-1" in refs                      # public still flows
    assert "intelligence/agent/memory" in refs  # granted raw admitted
    assert "performance/latency" not in refs    # ungranted raw withheld


def test_granted_scope_does_not_admit_a_sibling_sharing_a_prefix():
    """Same over-granting boundary as _grants.covers — asserted again HERE
    because this is a second, independent implementation of the predicate."""
    m = _pr()
    rows = [{"store": "knowledge-tree", "ref": "intelligence/agent-secrets"}]
    assert m.filter_by_tier(rows, m.TIER_PUBLIC, scopes=["intelligence/agent"]) == []


def test_tier_helpers_did_not_introduce_a_fileops_import():
    """peer_retrieve's core invariant — the guard-955/rb-2983 backend class."""
    m = _pr()
    m.assert_no_fileops()


def test_search_world_dir_filters_before_truncating(tmp_path):
    """A grant holder must not see FEWER granted nodes as the peer's public
    traffic grows. Filtering after the [:limit] slice would do exactly that."""
    m = _pr()
    rows = [{"store": "board/general", "ref": "m%d" % i, "score": 100 - i}
            for i in range(5)]
    rows.append({"store": "knowledge-tree", "ref": "intelligence/agent/x", "score": 1})
    # Low-scoring granted raw row would fall outside a limit-3 window if the
    # publics were kept; with tier=public+scope it must survive.
    out = m.filter_by_tier(rows, m.TIER_RAW, scopes=["intelligence/agent"])
    assert [r["ref"] for r in out] == ["intelligence/agent/x"]
