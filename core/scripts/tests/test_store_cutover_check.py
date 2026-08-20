"""Pins for store-cutover-check.py ( item 3).

The decision core (evaluate_roster) is pure — every fleet verdict class is
pinned here, plus the fail-closed branches that make an error unmistakable
for permission. The git-facing derive_proof is exercised only for its
error-shape contract (proven=False with a reason), not against a live repo:
the live discriminations were validated by hand on real fleet data at build
time (alpha/echo derived-proven, bravo/zeta/foxtrot correctly unproven —
2026-08-17), and a tmp-repo harness would re-test git, not this script.
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
    "store_cutover_check", SCRIPTS / "store-cutover-check.py")
scc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scc)

NOW = datetime(2026, 8, 17, 12, 0, 0)
FIELD = "utilization_seam"


def _row(**kw):
    base = {"last_active": "2026-08-17T11:00:00"}
    base.update(kw)
    return base


def fresh_stamp(days_old=1):
    return {"attested_at": (NOW - timedelta(days=days_old)).isoformat(),
            "commit": "abc123def"}


def test_derived_proof_attests_without_stamp():
    roster = {"alpha": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc123def",
                        "committed_at": "2026-08-17T10:00:00", "age_days": 0.1}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "SAFE"
    assert r["attested"][0]["basis"] == "derived"


def test_stamp_rescues_unproven_box():
    roster = {"bravo": _row(**{FIELD: fresh_stamp()})}
    proofs = {"bravo": {"proven": False, "reason": "seam_not_ancestor"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "SAFE"
    assert r["attested"][0]["basis"] == "stamp"


def test_no_proof_no_stamp_is_unsafe():
    roster = {"zeta": _row()}
    proofs = {"zeta": {"proven": False, "reason": "no_iteration_commit"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "UNSAFE"
    assert r["reason"] == "unattested_or_stale_boxes"
    assert r["unattested"][0]["derivation"] == "no_iteration_commit"


def test_one_unproven_box_blocks_the_fleet():
    roster = {"alpha": _row(), "zeta": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc", "age_days": 0.1},
              "zeta": {"proven": False, "reason": "seam_not_ancestor"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "UNSAFE"
    assert len(r["attested"]) == 1 and len(r["unattested"]) == 1


def test_stale_stamp_is_unsafe():
    roster = {"echo": _row(**{FIELD: fresh_stamp(days_old=45)})}
    proofs = {"echo": {"proven": False, "reason": "iteration_commit_stale"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "UNSAFE"
    assert r["stale"][0]["age_days"] > scc.ATTESTATION_MAX_AGE_DAYS


def test_unparseable_stamp_is_unsafe():
    roster = {"echo": _row(**{FIELD: {"attested_at": "not-a-date"}})}
    proofs = {"echo": {"proven": False, "reason": "x"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "UNSAFE"
    assert r["unattested"][0]["reason"] == "unparseable attested_at"


def test_empty_roster_is_unsafe_not_vacuous_safe():
    r = scc.evaluate_roster({}, {}, FIELD, NOW)
    assert r["verdict"] == "UNSAFE"
    assert r["reason"] == "empty_roster"


def test_retired_agent_skipped_not_blocking():
    roster = {"alpha": _row(),
              "ghost": _row(retired_at="2026-07-01T00:00:00")}
    proofs = {"alpha": {"proven": True, "commit": "abc", "age_days": 0.1}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "SAFE"
    assert r["retired_skipped"] == ["ghost"]


def test_unreadable_shard_is_unsafe():
    roster = {"alpha": "corrupt-string-not-dict"}
    r = scc.evaluate_roster(roster, {}, FIELD, NOW)
    assert r["verdict"] == "UNSAFE"
    assert r["unattested"][0]["reason"] == "unreadable shard"


def test_derived_outranks_stamp_when_both_present():
    roster = {"alpha": _row(**{FIELD: fresh_stamp()})}
    proofs = {"alpha": {"proven": True, "commit": "def456", "age_days": 0.2}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["attested"][0]["basis"] == "derived"


def test_registry_carries_the_utilization_cutover():
    cfg = scc.STORES["utilization"]
    assert cfg["seam_commit"].startswith("0c0bb0073")
    assert cfg["field"] == "utilization_seam"
    assert "core/scripts/retrieve.py" in cfg["consumers"]
    assert "mind_api/src/world/reasoning_bank.py" in cfg["consumers"]
    assert not any("tests/" in c for c in cfg["consumers"])


def test_registry_carries_the_gzip_cutover():
    """: the transport-gzip seam. Consumers are the READER files (the
    backend, the sync layer, the codec, and every raw get_object reader routed
    through it) — the writer flag names env-ids, so the flag field is reporting
    only, exactly like the utilization entry."""
    cfg = scc.STORES["gzip"]
    assert cfg["seam_commit"].startswith("ad2ae3207")
    assert cfg["field"] == "owncloud_gzip_seam"
    assert cfg["flag"] == "OWNCLOUD_GZIP_STORES"
    for c in ("core/scripts/_owncloud_codec.py", "core/scripts/owncloud_backend.py",
              "core/scripts/owncloud_sync.py",
              "mind_api/src/endpoints/aspirations_write.py"):
        assert c in cfg["consumers"], c
    assert not any("tests/" in c for c in cfg["consumers"])
    # Every consumer path exists in the tree — a renamed reader would silently
    # make the byte-identity diff vacuous.
    from _paths import PROJECT_ROOT
    for c in cfg["consumers"]:
        assert (PROJECT_ROOT / c).is_file(), c


def test_local_box_veto_blocks_an_otherwise_safe_fleet():
    """The defect  closed on 2026-08-18: every roster proof is
    AGENT-keyed, so a Body on a box that is behind reads SAFE off a sibling
    Body's commit. Measured on cc-07 (27 behind, alpha 'proven' by a cc-04
    commit). The local box now vetoes."""
    roster = {"alpha": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc", "age_days": 0.1}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW,
                            local={"seam_present": False,
                                   "reason": "seam_not_ancestor_of_HEAD"})
    assert r["verdict"] == "UNSAFE"
    assert r["reason"] == "local_box_not_reader_capable"
    assert r["local_box"]["reason"] == "seam_not_ancestor_of_HEAD"


def test_local_box_present_keeps_safe_and_is_surfaced():
    roster = {"alpha": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc", "age_days": 0.1}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW,
                            local={"seam_present": True, "hostname": "cc-07"})
    assert r["verdict"] == "SAFE"
    assert r["local_box"]["hostname"] == "cc-07"


def test_local_omitted_preserves_the_roster_only_shape():
    """Backward compatibility: local=None is the pre-wiring contract, and the
    key must be ABSENT rather than null so a caller cannot mistake 'not
    checked' for 'checked and empty'."""
    roster = {"alpha": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc", "age_days": 0.1}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "SAFE"
    assert "local_box" not in r


def test_local_veto_does_not_mask_a_peer_failure():
    roster = {"alpha": _row(), "zeta": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc", "age_days": 0.1},
              "zeta": {"proven": False, "reason": "seam_not_ancestor"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW,
                            local={"seam_present": False})
    assert r["verdict"] == "UNSAFE"
    assert r["reason"] == "unattested_or_stale_boxes"


def test_local_veto_does_not_mask_empty_roster():
    r = scc.evaluate_roster({}, {}, FIELD, NOW, local={"seam_present": False})
    assert r["verdict"] == "UNSAFE"
    assert r["reason"] == "empty_roster"


def test_cmd_check_actually_wires_the_local_report(monkeypatch, capsys):
    """The defect was a WIRING gap, not a missing capability: _local_report()
    was correct and complete from day one and cmd_check never called it
    (guard-1943 — pinning the writer says nothing about the wiring). This test
    fails if the call site is removed even though every pure test above still
    passes."""
    calls = []

    def fake_local(seam, consumers, symbol=None):
        calls.append((seam, tuple(consumers), symbol))
        return {"seam_present": False, "reason": "seam_not_ancestor_of_HEAD"}

    monkeypatch.setattr(scc, "_local_report", fake_local)
    monkeypatch.setattr(scc, "_fetch_origin_main", lambda: True)
    monkeypatch.setattr(scc, "_read_team_state",
                        lambda: ({"agent_status": {"alpha": _row()}}, None))
    monkeypatch.setattr(scc, "derive_proof",
                        lambda a, s, c, **kw: {"proven": True, "commit": "abc",
                                               "committed_at": "2026-08-18T00:00:00",
                                               "age_days": 0.1})
    monkeypatch.setattr(scc, "_fetch_worker_refs", lambda: True)
    monkeypatch.setattr(scc, "_head_commit", lambda: "deadbee")

    rc = scc.cmd_check(scc.STORES["utilization"])
    out = json.loads(capsys.readouterr().out)

    assert calls, "_local_report was never called — the veto is unwired"
    assert rc == 2, "a non-reader-capable local box must exit UNSAFE"
    assert out["verdict"] == "UNSAFE"
    assert out["reason"] == "local_box_not_reader_capable"
    assert out["local_box"]["head"] == "deadbee"
    assert out["local_box"]["hostname"]


def test_derive_proof_error_shape_on_bogus_agent():
    # An agent dir that has never existed on origin/main must come back
    # proven=False with a reason, never an exception (fail-closed contract).
    out = scc.derive_proof("no-such-agent-zzz",
                           scc.STORES["utilization"]["seam_commit"],
                           ["core/scripts/retrieve.py"])
    assert out["proven"] is False
    assert "reason" in out


# --- seam_symbol: the content predicate absorbed from gate-firings ---------
#
# These pins came over WITH the gate-firings cutover when  folded its
# 279-line implementation into this engine (). They are kept verbatim
# in intent because the two marked MUTATION PROOFs are the reason the predicate
# is shaped the way it is — both describe states in which a cheaper check
# reports the hazard as resolved, which is the false all-clear this whole gate
# exists to prevent. Deleting the old file without carrying these forward would
# have retired the evidence and kept the code.

GF = "gate_firings"
SYM = "firings_paths"


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def _tmp_consumers(tmp_path, monkeypatch, body_for):
    """Lay the store's consumers down under a tmp PROJECT_ROOT.

    Also stubs _git so ancestry and the origin/main byte-diff both pass — the
    point of these tests is the symbol predicate, so the two git gates before
    it must not be what decides the outcome.
    """
    cfg = scc.STORES[GF]
    for rel in cfg["consumers"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body_for(rel), encoding="utf-8")
    monkeypatch.setattr(scc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scc, "_git", lambda *a, **k: _Proc(0, ""))
    return cfg


def test_symbol_check_requires_a_CALL_not_merely_an_import(tmp_path, monkeypatch):
    """MUTATION PROOF: fails if the check greps for the bare symbol.

    An `import firings_paths` that nothing calls leaves the consumer reading
    the legacy filename -- pre-seam behavior -- while a symbol grep still
    succeeds. That is the precise state this gate exists to detect, so a naive
    `SYM in text` implementation reports the hazard as resolved.
    """
    cfg = _tmp_consumers(
        tmp_path, monkeypatch,
        lambda rel: f"from _gate_log import {SYM}\n"
                    "rows = read(META / 'gate-firings.jsonl')\n")
    r = scc._local_report(cfg["seam_commit"], cfg["consumers"], SYM)
    assert r["seam_present"] is False
    assert r["reason"] == f"consumers_do_not_call_{SYM}"
    assert set(r["missing"]) == set(cfg["consumers"])


def test_a_comment_mentioning_the_symbol_does_not_count_as_a_call(tmp_path, monkeypatch):
    """MUTATION PROOF: fails if comments are not stripped before the call check.

    Not hypothetical: two of the three real consumers carry a comment
    containing `firings_paths(` to explain the seam above the call. Revert the
    call and leave the comment -- a plausible bad refactor -- and an
    uncommented check reports the seam present. Same referent trap as
    guard-1685: the token survives its own removal.
    """
    cfg = _tmp_consumers(
        tmp_path, monkeypatch,
        lambda rel: f"# resolved via {SYM}() rather than a hardcoded filename\n"
                    "rows = read(META / 'gate-firings.jsonl')\n")
    r = scc._local_report(cfg["seam_commit"], cfg["consumers"], SYM)
    assert r["seam_present"] is False
    assert set(r["missing"]) == set(cfg["consumers"])


def test_symbol_check_passes_when_consumers_call_it(tmp_path, monkeypatch):
    cfg = _tmp_consumers(
        tmp_path, monkeypatch,
        lambda rel: f"from _gate_log import {SYM}\n"
                    f"rows = [r for p in {SYM}(META) for r in parse(p)]\n")
    r = scc._local_report(cfg["seam_commit"], cfg["consumers"], SYM)
    assert r["seam_present"] is True


def test_missing_consumer_file_is_unreadable_not_silently_ok(tmp_path, monkeypatch):
    cfg = scc.STORES[GF]
    monkeypatch.setattr(scc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scc, "_git", lambda *a, **k: _Proc(0, ""))
    r = scc._local_report(cfg["seam_commit"], cfg["consumers"], SYM)
    assert r["seam_present"] is False
    assert {u["consumer"] for u in r["unreadable"]} == set(cfg["consumers"])


def test_a_store_without_seam_symbol_is_unaffected(tmp_path, monkeypatch):
    """The predicate is OPT-IN. utilization/gzip declare no seam_symbol, so the
    absorbed check must not start gating them — a silent tightening of two
    unrelated cutovers would be exactly the drift this migration must avoid."""
    monkeypatch.setattr(scc, "PROJECT_ROOT", tmp_path)   # no consumer files
    monkeypatch.setattr(scc, "_git", lambda *a, **k: _Proc(0, ""))
    cfg = scc.STORES["utilization"]
    assert "seam_symbol" not in cfg
    r = scc._local_report(cfg["seam_commit"], cfg["consumers"], cfg.get("seam_symbol"))
    assert r["seam_present"] is True


def test_registry_carries_the_gate_firings_cutover():
    cfg = scc.STORES[GF]
    assert cfg["field"] == "gate_firings_seam"
    assert cfg["flag"] == "GATE_FIRINGS_SEGMENTED"
    assert cfg["seam_symbol"] == SYM
    assert len(cfg["seam_commit"]) == 40
    assert [Path(c).name for c in cfg["consumers"]] == [
        "gate-stats.py", "gate-retirement-eval.py", "override-ledger-consume.py"]


def test_the_real_consumers_still_call_the_seam_on_this_tree():
    """Regression floor against the LIVE tree, carried over unchanged.

    The three consumers genuinely call the seam AND carry prose about it, so
    this fails if comment-stripping ever eats the call itself. This is the one
    pin here that reads real files rather than fixtures, deliberately.
    """
    cfg = scc.STORES[GF]
    r = scc._local_report(cfg["seam_commit"], cfg["consumers"], SYM)
    assert r["seam_present"] is True, r


def test_origin_main_losing_the_symbol_vetoes_an_otherwise_SAFE_fleet(monkeypatch, capsys):
    """The hole byte-identity CANNOT see, and the reason _symbol_report exists.

    derive_proof proves an agent matches origin/main; _local_report proves this
    box matches origin/main. Both are RELATIVE. If main itself reverted the
    call, every box matches a broken main and the whole fleet reports proven —
    and ancestry cannot notice, because the seam commit stays an ancestor of a
    commit that reverts it. Without the fleet-level veto this run is SAFE.
    """
    monkeypatch.setattr(scc, "_fetch_origin_main", lambda: True)
    monkeypatch.setattr(scc, "_read_team_state",
                        lambda: ({"agent_status": {"alpha": _row()}}, None))
    monkeypatch.setattr(scc, "derive_proof",
                        lambda a, s, c, **kw: {"proven": True, "commit": "abc",
                                               "committed_at": "2026-08-18T00:00:00",
                                               "age_days": 0.1})
    monkeypatch.setattr(scc, "_fetch_worker_refs", lambda: True)
    monkeypatch.setattr(scc, "_local_report",
                        lambda s, c, sym=None: {"seam_present": True})
    monkeypatch.setattr(scc, "_head_commit", lambda: "deadbee")
    monkeypatch.setattr(scc, "_symbol_report",
                        lambda sym, cons, ref: {"symbol_present": False,
                                                "symbol": sym, "ref": ref,
                                                "ok": [], "missing": list(cons),
                                                "unreadable": []})
    rc = scc.cmd_check(scc.STORES[GF])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["verdict"] == "UNSAFE"
    assert out["reason"] == "origin_main_does_not_call_the_seam_symbol"
    assert out["seam_symbol"]["missing"]


def test_unattested_entry_surfaces_which_consumer_diverged():
    """The verdict must say WHICH consumer diverged, not just that one did.

    derive_proof computes diff_files and the examined commit; the roster verdict
    dropped both and reported only the reason. Measured on the utilization
    cutover (2026-08-18): three agents came back `consumers_diverge_from_main`
    and identifying that all three diverged on the SAME single file required a
    hand-run `git diff <agent-commit> origin/main -- <17 consumers>` per agent.
    The evidence was already in the proof dict.
    """
    roster = {"bravo": _row()}
    proofs = {"bravo": {"proven": False, "reason": "consumers_diverge_from_main",
                        "commit": "5a9f656c4",
                        "diff_files": ["core/scripts/_utilization_store.py"]}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    entry = r["unattested"][0]
    assert entry["derivation"] == "consumers_diverge_from_main"
    assert entry["derivation_diff_files"] == ["core/scripts/_utilization_store.py"]
    assert entry["derivation_commit"] == "5a9f656c4"


def test_stale_stamp_entry_keeps_both_ages_distinct():
    """The stamp's age and the examined commit's age are different quantities.

    They would silently collide on one `age_days` key; the derivation copy is
    prefixed so a reader cannot mistake 'your stamp is 40 days old' for 'the
    commit I looked at is 40 days old'.
    """
    roster = {"zeta": _row(**{FIELD: fresh_stamp(days_old=99)})}
    proofs = {"zeta": {"proven": False, "reason": "iteration_commit_stale",
                       "commit": "deadbeef1", "age_days": 42.0}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    entry = r["stale"][0]
    assert entry["age_days"] == 99.0
    assert entry["derivation_age_days"] == 42.0


def test_derivation_detail_is_absent_when_the_proof_carries_none():
    """No invented keys: a proof with only a reason yields only a reason."""
    roster = {"echo": _row()}
    proofs = {"echo": {"proven": False, "reason": "no_iteration_commit"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    entry = r["unattested"][0]
    assert entry["derivation"] == "no_iteration_commit"
    assert not [k for k in entry if k.startswith("derivation_")]


def _proof_git(monkeypatch, *, commit="feedfacecafe", ancestor_rc=0,
               diff_out="", days_old=1):
    """Drive derive_proof's three git calls independently of a real repo."""
    when = (datetime.now() - timedelta(days=days_old)).isoformat()

    def fake_git(*args, **kw):
        if args[0] == "log":
            return _Proc(0, f"{commit}|{when}\n")
        if args[0] == "merge-base":
            return _Proc(ancestor_rc, "")
        if args[0] == "diff":
            return _Proc(0, diff_out)
        return _Proc(0, "")

    monkeypatch.setattr(scc, "_git", fake_git)


def test_derived_proof_needs_ancestry_AND_consumer_identity(monkeypatch):
    """The third fixture pin outcome (a) names: CONSUMER-MISMATCH.

    Ancestry alone is not proof. A box can carry the seam commit and still be
    running different consumer bytes — an uncommitted revert, a cherry-pick, a
    half-applied merge. Ancestry says "you HAVE the commit", never "you are
    RUNNING what it landed", so proof requires both and this pin fails if the
    identity half is ever dropped for the cheaper ancestry check.
    """
    cfg = scc.STORES[GF]
    _proof_git(monkeypatch, diff_out="core/scripts/gate-stats.py\n")
    out = scc.derive_proof("alpha", cfg["seam_commit"], cfg["consumers"])
    assert out["proven"] is False
    assert out["reason"] == "consumers_diverge_from_main"
    assert out["diff_files"] == ["core/scripts/gate-stats.py"]


def test_derived_proof_refuses_when_the_seam_is_not_an_ancestor(monkeypatch):
    cfg = scc.STORES[GF]
    _proof_git(monkeypatch, ancestor_rc=1)
    out = scc.derive_proof("alpha", cfg["seam_commit"], cfg["consumers"])
    assert out["proven"] is False
    assert out["reason"] == "seam_not_ancestor"


def test_derived_proof_attests_with_ancestry_and_identity(monkeypatch):
    """The happy path outcome (a) calls 'attests with ZERO agent action'."""
    cfg = scc.STORES[GF]
    _proof_git(monkeypatch)
    out = scc.derive_proof("alpha", cfg["seam_commit"], cfg["consumers"])
    assert out["proven"] is True
    assert out["commit"] == "feedfacec"


def test_derived_proof_expires_with_the_commit_it_cites(monkeypatch):
    """A proof is only as fresh as the commit it cites (ATTESTATION_MAX_AGE_DAYS).

    Without this a box that pulled the seam once and went dormant a year ago
    still reads proven, which is the same permanent-permission defect the stale
    STAMP check exists to prevent — the derived path must not reintroduce it.
    """
    cfg = scc.STORES[GF]
    _proof_git(monkeypatch, days_old=scc.ATTESTATION_MAX_AGE_DAYS + 1)
    out = scc.derive_proof("alpha", cfg["seam_commit"], cfg["consumers"])
    assert out["proven"] is False
    assert out["reason"] == "iteration_commit_stale"


def test_unreadable_roster_is_unsafe(monkeypatch, capsys):
    """Fail-CLOSED. A read that errored has not shown the fleet carries the seam.

    Carried over from the gate-firings file: this branch lives in cmd_check
    (not in the pure evaluate_roster core), so none of the pure tests above
    reach it. It was the second of the two 'natural implementation returns SAFE
    having checked nothing' traps that file was written around.
    """
    monkeypatch.setattr(scc, "_fetch_origin_main", lambda: True)
    monkeypatch.setattr(scc, "_read_team_state",
                        lambda: ({}, "daemon unreachable"))
    rc = scc.cmd_check(scc.STORES[GF])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["verdict"] == "UNSAFE"
    assert out["reason"] == "roster_unreadable"
    assert "daemon unreachable" in out["detail"]


def test_attest_refuses_when_the_local_seam_is_absent(monkeypatch, capsys):
    """Attesting is a claim about THIS box. It must not be assertable falsely.

    The assertion that matters is `wrote == []`: a refusal that still wrote the
    stamp would record the very precondition the gate exists to verify.
    """
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setattr(scc, "_local_report",
                        lambda s, c, sym=None: {"seam_present": False,
                                                "reason": "seam_not_ancestor_of_HEAD"})
    wrote = []
    monkeypatch.setattr(scc.subprocess, "run",
                        lambda *a, **k: wrote.append(a) or _Proc(0, ""))
    rc = scc.cmd_attest(scc.STORES[GF])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["verdict"] == "refused"
    assert wrote == [], "attest wrote to team-state despite a missing seam"


def test_attest_without_agent_binding_errors_rather_than_guessing(monkeypatch, capsys):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    rc = scc.cmd_attest(scc.STORES[GF])
    out = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert out["verdict"] == "error"


def test_symbol_report_reads_the_named_ref_and_fails_closed_on_unreadable(monkeypatch):
    """An unreadable blob is UNSAFE, never 'nothing to object to'."""
    def fake_git(*args, **kw):
        if args[0] == "show" and args[1].endswith("gate-stats.py"):
            return _Proc(128, "", "fatal: path does not exist")
        return _Proc(0, f"rows = {SYM}(META)\n")
    monkeypatch.setattr(scc, "_git", fake_git)
    cfg = scc.STORES[GF]
    r = scc._symbol_report(SYM, cfg["consumers"], "origin/main")
    assert r["symbol_present"] is False
    assert r["ref"] == "origin/main"
    assert [u["consumer"] for u in r["unreadable"]] == [
        "core/scripts/gate-stats.py"]


# ── worker-carrier proof lane () ───────────────────────────────────
# A Body is a checkout too. The agent_namespace lane proves whichever Body
# committed agents/<agent>/ last and says nothing about the others, so an agent
# whose other Bodies run elsewhere falls to the hand-stamp this file exists to
# delete. These pin the second lane, its liveness join, and — the half most
# likely to rot — that the verdict NAMES which candidate proved the agent.

SEAM = "seamseamseamseamseamseamseamseamseamseam"
REF_SID = "cd5fd3b9-5b97-439a-9914-196c1c8f5c00"


def _bodies(sid=REF_SID, hours_ago=0.5, **extra):
    """An in_flight_bodies map with one row, aged `hours_ago`."""
    return {sid: {"claimed_at": (datetime.now()
                                 - timedelta(hours=hours_ago)).isoformat(),
                  "goal_id": "g-x", "phase": "4", **extra}}


def _lane_git(monkeypatch, *, agent_commit=None, ref_commit=None,
              ancestors=("*",), diff_for=None, days_old=1):
    """Drive BOTH proof lanes independently of a real repo.

    agent_commit=None  -> the agents/<agent>/ lane finds no commit (rc=1), which
                          is exactly the dormant-Body case the carrier lane
                          exists to rescue.
    ancestors          -> commits the seam is an ancestor of ('*' = all).
    diff_for           -> {commit: "path\n"} making that commit's consumers
                          diverge from origin/main.
    """
    when = (datetime.now() - timedelta(days=days_old)).isoformat()
    diff_for = diff_for or {}

    def fake_git(*args, **kw):
        if args[0] == "log":
            if "origin/main" in args:                    # agent_namespace lane
                return (_Proc(0, f"{agent_commit}|{when}\n") if agent_commit
                        else _Proc(1, ""))
            return (_Proc(0, f"{ref_commit}|{when}\n") if ref_commit
                    else _Proc(1, ""))                   # carrier lane
        if args[0] == "merge-base":
            hit = "*" in ancestors or args[3] in ancestors
            return _Proc(0 if hit else 1, "")
        if args[0] == "diff":
            return _Proc(0, diff_for.get(args[2], ""))
        return _Proc(0, "")

    monkeypatch.setattr(scc, "_git", fake_git)


def test_live_carrier_ref_proves_what_the_namespace_lane_cannot(monkeypatch):
    """Fixture pin 1: no recent agents/<agent>/ commit + a LIVE carrier ref
    carrying the seam -> proven, with the proving ref NAMED."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit=None, ref_commit="cafebabe1234")
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"], bodies=_bodies())
    assert out["proven"] is True
    assert out["lane"] == "worker_carrier_ref"
    assert out["ref"] == f"refs/workers/alpha/{REF_SID}"
    assert out["commit"] == "cafebabe1"


def test_a_stale_body_row_carrier_ref_proves_nothing(monkeypatch):
    """Fixture pin 2: the SAME ref, reachable and carrying the seam, proves
    nothing once its Body row is stale — the guard-3660 liveness join is the
    substantive half, so this must fail for the LIVENESS reason and not because
    the content went bad. Content is held identical to the passing test above."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit=None, ref_commit="cafebabe1234")
    stale = _bodies(hours_ago=scc.BODY_LIVENESS_MAX_AGE_HOURS + 1)
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"], bodies=stale)
    assert out["proven"] is False
    assert out["reason"] == "no_iteration_commit"      # fell back, unrelabelled
    assert "carrier_candidates" not in out             # never even considered


def test_the_agent_namespace_lane_still_wins_when_it_can_prove(monkeypatch):
    """No regression on the landed lane: when it proves, it proves, and it
    names itself — the carrier lane must not silently take over."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit="aaaa1111bbbb", ref_commit="cafebabe1234")
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"], bodies=_bodies())
    assert out["proven"] is True
    assert out["lane"] == "agent_namespace"
    assert out["commit"] == "aaaa1111b"
    assert "ref" not in out


def test_omitting_bodies_keeps_the_single_lane_behaviour(monkeypatch):
    """The default is byte-identical to before: no bodies -> no carrier lane,
    so every pre-existing caller and test is unaffected."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit=None, ref_commit="cafebabe1234")
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"])
    assert out["proven"] is False
    assert out["reason"] == "no_iteration_commit"
    assert "carrier_candidates" not in out


def test_a_live_ref_that_lacks_the_seam_is_reported_not_swallowed(monkeypatch):
    """A live Body whose ref does NOT carry the seam must not prove, and the
    attempt must be visible — a proof lane that fails silently is unauditable."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit=None, ref_commit="cafebabe1234",
              ancestors=())                                # seam ancestor of nothing
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"], bodies=_bodies())
    assert out["proven"] is False
    assert out["reason"] == "no_iteration_commit"
    assert out["carrier_candidates"][0]["reason"] == "seam_not_ancestor"
    assert out["carrier_candidates"][0]["ref"].endswith(REF_SID)


def test_a_live_ref_whose_consumers_diverge_names_the_file(monkeypatch):
    """Ancestry is not identity on the carrier lane either (the guard the
    agent lane already carries) — and the diverging file is surfaced."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit=None, ref_commit="cafebabe1234",
              diff_for={"cafebabe1234": "core/scripts/gate-stats.py\n"})
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"], bodies=_bodies())
    assert out["proven"] is False
    c = out["carrier_candidates"][0]
    assert c["reason"] == "consumers_diverge_from_main"
    assert c["diff_files"] == ["core/scripts/gate-stats.py"]


def test_an_unreadable_carrier_ref_is_reported_not_proven(monkeypatch):
    """A live body row whose ref is not fetched locally must not prove. This is
    the failure _fetch_worker_refs exists to prevent, and it must be legible."""
    cfg = scc.STORES[GF]
    _lane_git(monkeypatch, agent_commit=None, ref_commit=None)
    out = scc.derive_proof("alpha", SEAM, cfg["consumers"], bodies=_bodies())
    assert out["proven"] is False
    assert out["carrier_candidates"][0]["reason"] == "carrier_ref_unreadable"


def test_live_body_sids_drops_every_uncertain_row():
    """The liveness join fails CLOSED on anything it cannot read, and accepts a
    future stamp (clock skew between boxes is not staleness)."""
    now = datetime.now()
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=scc.BODY_LIVENESS_MAX_AGE_HOURS + 1)).isoformat()
    future = (now + timedelta(minutes=5)).isoformat()
    bodies = {"live": {"claimed_at": fresh}, "old": {"claimed_at": old},
              "skewed": {"claimed_at": future}, "junk": {"claimed_at": "nope"},
              "missing": {}, "notadict": "x"}
    assert scc._live_body_sids(bodies, now) == ["live", "skewed"]
    assert scc._live_body_sids(None, now) == []
    assert scc._live_body_sids({}, now) == []


def test_the_verdict_names_the_lane_that_proved_each_agent():
    """evaluate_roster must carry lane/ref through — without them a derived
    attestation cannot be audited once more than one lane exists."""
    roster = {"alpha": _row()}
    proofs = {"alpha": {"proven": True, "commit": "abc123def",
                        "committed_at": "2026-08-19T00:00:00", "age_days": 0.1,
                        "lane": "worker_carrier_ref",
                        "ref": f"refs/workers/alpha/{REF_SID}"}}
    r = scc.evaluate_roster(roster, proofs, FIELD, NOW)
    assert r["verdict"] == "SAFE"
    entry = r["attested"][0]
    assert entry["lane"] == "worker_carrier_ref"
    assert entry["ref"].endswith(REF_SID)


def test_cmd_check_fetches_worker_refs_and_passes_the_body_rows(monkeypatch, capsys):
    """The wiring pin. derive_proof gaining a bodies= parameter is inert unless
    cmd_check FETCHES the refs and actually passes each agent's rows — the
    'built but never called' failure this file has hit before."""
    seen = {}
    monkeypatch.setattr(scc, "_fetch_origin_main", lambda: True)
    monkeypatch.setattr(scc, "_fetch_worker_refs",
                        lambda: seen.setdefault("fetched", True))
    row = _row()
    row["in_flight_bodies"] = _bodies()
    monkeypatch.setattr(scc, "_read_team_state",
                        lambda: ({"agent_status": {"alpha": row}}, None))

    def spy(agent, seam, consumers, bodies=None):
        seen["bodies"] = bodies
        return {"proven": True, "commit": "abc", "lane": "agent_namespace"}

    monkeypatch.setattr(scc, "derive_proof", spy)
    monkeypatch.setattr(scc, "_local_report",
                        lambda s, c, sym=None: {"seam_present": True})
    monkeypatch.setattr(scc, "_head_commit", lambda: "deadbee")
    monkeypatch.setattr(scc, "_symbol_report",
                        lambda sym, cons, ref: {"symbol_present": True})
    scc.cmd_check(dict(scc.STORES[GF]))
    out = json.loads(capsys.readouterr().out)
    assert seen.get("fetched") is True
    assert REF_SID in (seen.get("bodies") or {})
    assert out["worker_refs_fetch_ok"] is True
