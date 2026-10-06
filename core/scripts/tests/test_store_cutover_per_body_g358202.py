"""Pins for the per-Body lane of store-cutover-check.py ( U23).

The roster lanes prove an AGENT, and an agent runs a Body on many boxes, so a
SAFE read says nothing about a box that has not pulled the reader (rb-8276). The
lane asks the question per Body: every live Body's heartbeat carrier publishes
`main_base`, and each one must pass the same predicate the roster lanes use.

The decision core (`evaluate_bodies`) is pure and is pinned case by case. The
git-facing `_prove_main_base` is pinned for what it delegates and when it
refuses, and `cmd_check` is pinned for the WIRING, because a pure core that
nothing calls reads as green while the verdict never changes (guard-1943).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "store_cutover_check_per_body", SCRIPTS / "store-cutover-check.py")
scc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scc)

NOW = datetime(2026, 10, 4, 18, 0, 0)
BASE_A = "a" * 40
BASE_B = "b" * 40
COMPLETE = {"complete": True, "read_via": "authoritative", "reason": None}
CLOSED = frozenset({"closed-pending-merge", "merged", "closed-stale", "closed-graceful"})


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _row(sid, *, agent="alpha", host="box-1", minutes_ago=5, base=BASE_A,
         state="active", **doc_extra):
    doc = {"sid": sid, "agent": agent, "host": host, "ts": _ts(minutes_ago),
           "body_state": state, "main_base": base}
    if base is None:
        del doc["main_base"]
    doc.update(doc_extra)
    return {"agent": agent, "sid": sid, "doc": doc}


class _Prover:
    """Records every base it is asked about; answers from `verdicts`."""

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.calls = []

    def __call__(self, base):
        self.calls.append(base)
        return self.verdicts.get(base, {"proven": True, "commit": base[:9]})


def _eval(rows, *, meta=COMPLETE, prover=None, closed=CLOSED):
    prover = prover or _Prover()
    return scc.evaluate_bodies(rows, meta, NOW, prover, closed), prover


# --- the verdict -------------------------------------------------------------
def test_every_live_body_proven_is_all_proven_and_proves_each_base_once():
    rows = [_row("s1" * 4, host="box-1", base=BASE_A),
            _row("s2" * 4, host="box-1", base=BASE_A),
            _row("s3" * 4, host="box-2", base=BASE_B)]
    out, prover = _eval(rows)
    assert out["all_proven"] is True and out["reason"] is None
    assert (out["bodies_live"], out["bodies_proven"]) == (3, 3)
    assert out["unproven"] == []
    # Two Bodies on one checkout share a base: one git proof, not two.
    assert sorted(prover.calls) == [BASE_A, BASE_B]
    assert out["distinct_bases"] == 2


def test_a_body_with_no_main_base_blocks_the_fleet_and_is_named():
    rows = [_row("s1" * 4), _row("deadbeef-0000", agent="zeta", host="box-9", base=None)]
    out, _ = _eval(rows)
    assert out["all_proven"] is False
    assert out["reason"] == "per_body_reader_unproven"
    assert out["bodies_proven"] == 1
    (who,) = out["unproven"]
    assert (who["agent"], who["host"], who["sid"]) == ("zeta", "box-9", "deadbeef")
    assert who["reason"] == "no_main_base"


def test_a_malformed_main_base_is_no_main_base_not_a_git_argument():
    for bad in ("", "HEAD", "origin/main", "abc", "g" * 40, "A" * 40, "a" * 41,
                BASE_A + "\n--upload-pack=x"):
        out, prover = _eval([_row("s1" * 4, base=bad)])
        assert out["all_proven"] is False, bad
        assert out["unproven"][0]["reason"] == "no_main_base", bad
        assert prover.calls == [], f"{bad!r} reached the prover"


def test_a_body_whose_base_fails_the_predicate_carries_the_predicates_reason():
    prover = _Prover({BASE_B: {"proven": False, "reason": "seam_not_ancestor",
                               "commit": BASE_B[:9]}})
    rows = [_row("s1" * 4, base=BASE_A), _row("s2" * 4, host="box-2", base=BASE_B)]
    out, _ = _eval(rows, prover=prover)
    assert out["all_proven"] is False and out["bodies_proven"] == 1
    (who,) = out["unproven"]
    assert who["reason"] == "seam_not_ancestor"
    assert who["main_base"] == BASE_B[:9]
    assert who["host"] == "box-2"


# --- who counts as live ------------------------------------------------------
def test_a_stale_carrier_is_not_a_live_body():
    """A dead session's carrier outlives it by days (measured: 123 of 142 stale);
    counting it would block the flip on residue."""
    rows = [_row("s1" * 4), _row("old0" * 2, minutes_ago=7 * 60, base=None)]
    out, _ = _eval(rows)
    assert out["all_proven"] is True
    assert (out["bodies_live"], out["stale_skipped"]) == (1, 1)


def test_the_window_edge_is_the_gates_own_liveness_constant():
    inside = _row("in00" * 2, minutes_ago=scc.BODY_LIVENESS_MAX_AGE_HOURS * 60 - 1, base=None)
    outside = _row("out0" * 2, minutes_ago=scc.BODY_LIVENESS_MAX_AGE_HOURS * 60 + 1, base=None)
    out, _ = _eval([_row("s1" * 4), inside, outside])
    assert out["bodies_live"] == 2 and out["stale_skipped"] == 1
    assert [u["sid"] for u in out["unproven"]] == ["in00in00"]


def test_a_closed_body_is_not_a_live_body_but_an_empty_closed_set_counts_it():
    rows = [_row("s1" * 4), _row("cl00" * 2, state="merged", base=None)]
    out, _ = _eval(rows)
    assert out["all_proven"] is True and out["closed_skipped"] == 1
    # The enumeration-failure path hands back an EMPTY closed set. A failure must
    # only ever make MORE Bodies count as live, never fewer.
    out, _ = _eval(rows, closed=frozenset())
    assert out["all_proven"] is False and out["closed_skipped"] == 0


def test_a_parked_or_blank_state_body_is_live():
    for state in ("parked", "active", ""):
        out, _ = _eval([_row("s1" * 4, state=state, base=None)])
        assert out["bodies_live"] == 1, state
        assert out["reason"] == "per_body_reader_unproven", state


def test_unknown_freshness_is_live_and_unproven_never_skipped():
    for label, row, reason in (
        ("empty doc", {"agent": "alpha", "sid": "e0e0e0e0", "doc": {}}, "carrier_unreadable"),
        ("no doc", {"agent": "alpha", "sid": "e1e1e1e1", "doc": None}, "carrier_unreadable"),
        ("no ts", _row("e2e2e2e2", ts=""), "unparseable_ts"),
        ("bad ts", _row("e3e3e3e3", ts="yesterday"), "unparseable_ts"),
    ):
        out, _ = _eval([_row("s1" * 4), row])
        assert out["all_proven"] is False, label
        assert out["unproven"][0]["reason"] == reason, label
        assert out["bodies_live"] == 2, label


def test_a_future_timestamp_is_clock_skew_not_staleness():
    out, _ = _eval([_row("fu00" * 2, minutes_ago=-30)])
    assert out["all_proven"] is True and out["bodies_live"] == 1


# --- an enumeration that could not answer is not an empty fleet --------------
def test_an_incomplete_or_mirror_read_refuses_even_when_every_row_proves():
    rows = [_row("s1" * 4)]
    ok, _ = _eval(rows)
    assert ok["all_proven"] is True                   # positive control
    for meta in ({"complete": False, "read_via": "none", "reason": "boom"},
                 {"complete": True, "read_via": "local-mirror", "reason": None},
                 {"complete": False, "read_via": "authoritative"}, {}):
        out, _ = _eval(rows, meta=meta)
        assert out["all_proven"] is False, meta
        assert out["reason"] == "carrier_enumeration_incomplete", meta


def test_no_live_body_is_not_a_clean_pass():
    for rows in ([], [_row("old0" * 2, minutes_ago=9 * 60)],
                 [_row("cl00" * 2, state="closed-graceful")]):
        out, _ = _eval(rows)
        assert out["all_proven"] is False
        assert out["reason"] == "no_live_bodies"


def test_enumeration_failure_returns_an_incomplete_read_and_an_empty_closed_set(monkeypatch):
    import worker_stall as ws

    def boom(_root):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(ws, "enumerate_carriers", boom)
    rows, meta, closed = scc._enumerate_body_carriers()
    assert rows == [] and closed == frozenset()
    assert meta["complete"] is False
    assert "store unreachable" in meta["reason"]
    out = scc.evaluate_bodies(rows, meta, NOW, _Prover(), closed)
    assert out["reason"] == "carrier_enumeration_incomplete"


# --- the git-facing half ---------------------------------------------------------
def test_main_base_that_cannot_be_read_is_unproven_and_never_reaches_the_predicate(monkeypatch):
    monkeypatch.setattr(scc, "_git", lambda *a, **k: _Proc(128, "", "bad object"))
    called = []
    monkeypatch.setattr(scc, "_prove_commit", lambda *a, **k: called.append(a) or {"proven": True})
    out = scc._prove_main_base(BASE_A, "5ea" * 13 + "5", ["c.py"], NOW)
    assert out["proven"] is False and out["reason"] == "main_base_unreadable"
    assert called == []


def test_main_base_is_judged_by_the_roster_predicate_at_its_own_commit(monkeypatch):
    when = "2026-10-04T12:00:00+00:00"
    seen_git = []

    def fake_git(*args, **kw):
        seen_git.append(args)
        return _Proc(0, when + "\n")

    monkeypatch.setattr(scc, "_git", fake_git)
    captured = {}

    def fake_prove(commit, ciso, seam, consumers, now, seam_symbols=None):
        captured.update(commit=commit, ciso=ciso, seam=seam, consumers=consumers,
                        now=now, seam_symbols=seam_symbols)
        return {"proven": False, "reason": "seam_not_ancestor", "commit": commit[:9]}

    monkeypatch.setattr(scc, "_prove_commit", fake_prove)
    out = scc._prove_main_base(BASE_A, "seam", ["c.py"], NOW, ["sym"])
    assert out["reason"] == "seam_not_ancestor"       # the predicate's verdict, unrelabelled
    assert captured == {"commit": BASE_A, "ciso": when, "seam": "seam",
                        "consumers": ["c.py"], "now": NOW, "seam_symbols": ["sym"]}
    assert seen_git[0][:3] == ("log", "-1", "--format=%cI")


# --- the wiring --------------------------------------------------------------
def _wire(monkeypatch, *, carriers, local_ok=True, base_proven=True):
    monkeypatch.setattr(scc, "_fetch_origin_main", lambda: True)
    monkeypatch.setattr(scc, "_fetch_worker_refs", lambda: True)
    monkeypatch.setattr(scc, "_head_commit", lambda: "deadbee")
    monkeypatch.setattr(scc, "_read_team_state", lambda: (
        {"agent_status": {"alpha": {"last_active": "2026-10-04T17:00:00"}}}, None))
    monkeypatch.setattr(scc, "derive_proof", lambda *a, **k: {
        "proven": True, "commit": "abc", "committed_at": "2026-10-04T00:00:00", "age_days": 0.1})
    monkeypatch.setattr(scc, "_local_report", lambda *a, **k: {
        "seam_present": local_ok, "reason": None if local_ok else "seam_not_ancestor_of_HEAD"})
    monkeypatch.setattr(scc, "_symbol_report", lambda *a, **k: {"symbol_present": True})
    monkeypatch.setattr(scc, "_enumerate_body_carriers", lambda: (carriers, COMPLETE, CLOSED))
    monkeypatch.setattr(scc, "_prove_main_base", lambda base, *a, **k: (
        {"proven": True, "commit": base[:9]} if base_proven
        else {"proven": False, "reason": "seam_not_ancestor", "commit": base[:9]}))


def _run(capsys, cfg):
    rc = scc.cmd_check(cfg)
    return rc, json.loads(capsys.readouterr().out)


def _fresh(sid, **kw):
    """A carrier that is live as of the REAL clock, which cmd_check reads."""
    row = _row(sid, **kw)
    row["doc"]["ts"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return row


def test_cmd_check_is_unsafe_when_one_live_body_has_not_published_a_base(monkeypatch, capsys):
    """Fails if the call site is removed even though every pure test still passes."""
    _wire(monkeypatch, carriers=[_fresh("s1" * 4), _fresh("s2" * 4, base=None)])
    rc, out = _run(capsys, dict(scc.STORES["composite"]))
    assert rc == 2 and out["verdict"] == "UNSAFE"
    assert out["reason"] == "per_body_reader_unproven"
    assert out["per_body"]["unproven"][0]["reason"] == "no_main_base"
    assert out["per_body"]["bodies_proven"] == 1


def test_cmd_check_is_safe_when_every_live_body_proves(monkeypatch, capsys):
    """The positive control: the same wiring, every Body published a proving base."""
    _wire(monkeypatch, carriers=[_fresh("s1" * 4), _fresh("s2" * 4, base=BASE_B)])
    rc, out = _run(capsys, dict(scc.STORES["composite"]))
    assert rc == 0 and out["verdict"] == "SAFE"
    assert out["per_body"]["all_proven"] is True


def test_cmd_check_a_proven_agent_does_not_launder_an_unproven_body(monkeypatch, capsys):
    """The defect itself: every AGENT proves (derive_proof is stubbed proven) and
    the local box carries the seam, yet one Body's checkout predates it."""
    _wire(monkeypatch, carriers=[_fresh("s1" * 4)], base_proven=False)
    rc, out = _run(capsys, dict(scc.STORES["composite"]))
    assert out["attested"] and not out["unattested"]            # the roster half is clean
    assert out["local_box"]["seam_present"] is True             # and so is this box
    assert rc == 2 and out["reason"] == "per_body_reader_unproven"
    assert out["per_body"]["unproven"][0]["reason"] == "seam_not_ancestor"


def test_a_store_that_does_not_opt_in_keeps_its_verdict_and_its_shape(monkeypatch, capsys):
    _wire(monkeypatch, carriers=[_fresh("s2" * 4, base=None)])
    cfg = dict(scc.STORES["composite"])
    cfg.pop("per_body_carriers")
    rc, out = _run(capsys, cfg)
    assert rc == 0 and out["verdict"] == "SAFE"
    assert "per_body" not in out


def test_the_lane_never_masks_a_broader_failure(monkeypatch, capsys):
    _wire(monkeypatch, carriers=[_fresh("s2" * 4, base=None)], local_ok=False)
    rc, out = _run(capsys, dict(scc.STORES["composite"]))
    assert rc == 2 and out["verdict"] == "UNSAFE"
    assert out["reason"] == "local_box_not_reader_capable"       # not relabelled
    assert out["per_body"]["all_proven"] is False                # but still reported


def test_only_the_composite_entry_opts_in():
    """Opt-in per store: the other cutovers keep their verdicts byte for byte."""
    assert scc.STORES["composite"]["per_body_carriers"] is True
    for name, cfg in scc.STORES.items():
        if name != "composite":
            assert not cfg.get("per_body_carriers"), name
