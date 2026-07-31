"""Tests for the gate-firings cutover ordering gate ().

The thing under test decides whether it is safe to set GATE_FIRINGS_SEGMENTED
fleet-wide. Its failure direction is asymmetric: a wrong SAFE lets a box write
date segments that a pre-seam peer cannot see, so that peer reads hours of data
as a 30-day window and reports a still-firing gate as retirable -- a confident
false all-clear. A wrong UNSAFE merely delays a cutover. Every test below exists
to pin the SAFE verdict shut; none of them are about making SAFE easier to get.

The two that carry the most weight are `test_empty_roster_is_unsafe_not_vacuously_safe`
and `test_unreadable_roster_is_unsafe`, because both are cases where the natural
implementation returns SAFE while having checked nothing at all.
"""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "gate_firings_cutover_check", SCRIPTS / "gate-firings-cutover-check.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def _row(attested_days_ago=0, commit="abc1234", retired=False, seam=True):
    row = {"last_active": _iso(datetime.now())}
    if retired:
        row["retired_at"] = _iso(datetime.now())
    if seam:
        row["gate_firings_seam"] = {
            "attested_at": _iso(datetime.now() - timedelta(days=attested_days_ago)),
            "commit": commit,
            "consumers": list(mod.CONSUMERS),
        }
    return row


def _run(monkeypatch, roster, err=None, capsys=None):
    monkeypatch.setattr(mod, "_read_team_state", lambda: (roster, err))
    monkeypatch.setattr(mod, "_local_seam_report",
                        lambda: {"seam_present": True, "ok": list(mod.CONSUMERS),
                                 "missing": [], "unreadable": []})
    rc = mod.cmd_check(None)
    import json
    return rc, json.loads(capsys.readouterr().out)


# --- the two vacuous-SAFE traps ------------------------------------------

def test_empty_roster_is_unsafe_not_vacuously_safe(monkeypatch, capsys):
    """MUTATION PROOF: fails if the verdict is computed as `not blockers`.

    With no agents, "every live agent has attested" is TRUE and any
    straightforward all()-style predicate returns SAFE having verified nothing
    (guard-1665: a detector's empty result is not evidence until it is shown
    capable of matching). An empty roster means the roster read is broken or
    the fleet is unknown -- neither is permission to cut over.
    """
    rc, out = _run(monkeypatch, {"agent_status": {}}, capsys=capsys)
    assert rc == 2
    assert out["verdict"] == "UNSAFE"
    assert out["reason"] == "empty_roster"


def test_unreadable_roster_is_unsafe(monkeypatch, capsys):
    """Fail-CLOSED. A read that errored has not shown the fleet carries the seam."""
    rc, out = _run(monkeypatch, {}, err="daemon unreachable", capsys=capsys)
    assert rc == 2
    assert out["verdict"] == "UNSAFE"
    assert out["reason"] == "roster_unreadable"
    assert "daemon unreachable" in out["detail"]


# --- the ordering predicate itself ---------------------------------------

def test_all_attested_is_safe(monkeypatch, capsys):
    roster = {"agent_status": {"alpha": _row(), "bravo": _row()}}
    rc, out = _run(monkeypatch, roster, capsys=capsys)
    assert rc == 0
    assert out["verdict"] == "SAFE"
    assert {a["agent"] for a in out["attested"]} == {"alpha", "bravo"}


def test_one_unattested_box_blocks_the_whole_fleet(monkeypatch, capsys):
    """A single pre-seam peer is sufficient to produce the false all-clear."""
    roster = {"agent_status": {"alpha": _row(), "bravo": _row(seam=False)}}
    rc, out = _run(monkeypatch, roster, capsys=capsys)
    assert rc == 2
    assert out["verdict"] == "UNSAFE"
    assert [a["agent"] for a in out["unattested"]] == ["bravo"]


def test_stale_attestation_does_not_count_as_attested(monkeypatch, capsys):
    """An attestation is evidence about the tree deployed AT THAT MOMENT.

    A box can be rolled back, so an old attestation can outlive the code it
    attested to. Without the age check a one-time attestation would grant
    permanent permission.
    """
    roster = {"agent_status": {
        "alpha": _row(),
        "bravo": _row(attested_days_ago=mod.ATTESTATION_MAX_AGE_DAYS + 1),
    }}
    rc, out = _run(monkeypatch, roster, capsys=capsys)
    assert rc == 2
    assert [s["agent"] for s in out["stale"]] == ["bravo"]
    assert [a["agent"] for a in out["attested"]] == ["alpha"]


def test_unparseable_timestamp_is_unattested_not_attested(monkeypatch, capsys):
    roster = {"agent_status": {"alpha": _row()}}
    roster["agent_status"]["alpha"]["gate_firings_seam"]["attested_at"] = "not-a-date"
    rc, out = _run(monkeypatch, roster, capsys=capsys)
    assert rc == 2
    assert out["unattested"][0]["reason"] == "unparseable attested_at"


def test_retired_agent_does_not_block_forever(monkeypatch, capsys):
    """A decommissioned box cannot attest and must not wedge the cutover.

    The complement of the tests above: fail-closed must not mean unfalsifiable.
    """
    roster = {"agent_status": {"alpha": _row(), "ghost": _row(retired=True, seam=False)}}
    rc, out = _run(monkeypatch, roster, capsys=capsys)
    assert rc == 0
    assert out["verdict"] == "SAFE"
    assert out["retired_skipped"] == ["ghost"]


def test_unreadable_shard_counts_against_safety(monkeypatch, capsys):
    roster = {"agent_status": {"alpha": _row(), "bravo": "corrupt-not-a-dict"}}
    rc, out = _run(monkeypatch, roster, capsys=capsys)
    assert rc == 2
    assert out["unattested"][0]["agent"] == "bravo"


# --- local seam detection -------------------------------------------------

def test_seam_check_requires_a_CALL_not_merely_an_import(tmp_path, monkeypatch):
    """MUTATION PROOF: fails if the check greps for the bare symbol.

    An `import firings_paths` that nothing calls leaves the consumer reading the
    legacy filename -- pre-seam behavior -- while a symbol grep still succeeds.
    That is the precise state this gate exists to detect, so a naive
    `SEAM in text` implementation reports the hazard as resolved.
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in mod.CONSUMERS:
        (scripts / name).write_text(
            f"from _gate_log import {mod.SEAM}\n"
            "rows = read(META / 'gate-firings.jsonl')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)

    report = mod._local_seam_report()
    assert report["seam_present"] is False
    assert set(report["missing"]) == set(mod.CONSUMERS)


def test_seam_check_passes_when_consumers_call_it(tmp_path, monkeypatch):
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in mod.CONSUMERS:
        (scripts / name).write_text(
            f"from _gate_log import {mod.SEAM}\n"
            f"rows = [r for p in {mod.SEAM}(META) for r in parse(p)]\n",
            encoding="utf-8")
    monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)

    report = mod._local_seam_report()
    assert report["seam_present"] is True
    assert set(report["ok"]) == set(mod.CONSUMERS)


def test_missing_consumer_file_is_unreadable_not_silently_ok(tmp_path, monkeypatch):
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)

    report = mod._local_seam_report()
    assert report["seam_present"] is False
    assert {u["consumer"] for u in report["unreadable"]} == set(mod.CONSUMERS)


def test_attest_refuses_when_the_local_seam_is_absent(monkeypatch, capsys):
    """Attesting is a claim about THIS box. It must not be assertable falsely."""
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setattr(mod, "_local_seam_report",
                        lambda: {"seam_present": False, "ok": [],
                                 "missing": list(mod.CONSUMERS), "unreadable": []})
    called = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: called.append(a) or pytest.fail(
                            "attest wrote to team-state despite a missing seam"))
    rc = mod.cmd_attest(None)
    import json
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["verdict"] == "refused"
    assert called == []


def test_attest_without_agent_binding_errors_rather_than_guessing(monkeypatch, capsys):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    rc = mod.cmd_attest(None)
    import json
    out = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert out["verdict"] == "error"


def test_a_comment_mentioning_the_seam_does_not_count_as_a_call(tmp_path, monkeypatch):
    """MUTATION PROOF: fails if comments are not stripped before the call check.

    Found by adversarially probing the check rather than by reading it. Two of
    the three real consumers already carry a comment containing `firings_paths(`
    to explain the seam above the call. Revert the call and leave the comment --
    a plausible bad refactor -- and an uncommented check reports the seam
    present, which is the false all-clear this whole gate exists to prevent.
    Same referent trap as guard-1685: the token survives its own removal.
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in mod.CONSUMERS:
        (scripts / name).write_text(
            f"# resolved via {mod.SEAM}() rather than a hardcoded filename\n"
            "rows = read(META / 'gate-firings.jsonl')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)

    report = mod._local_seam_report()
    assert report["seam_present"] is False
    assert set(report["missing"]) == set(mod.CONSUMERS)


def test_the_real_consumers_still_pass_after_comment_stripping():
    """Regression floor: the fix must not break the live tree.

    The three consumers genuinely call the seam AND carry prose about it, so
    this fails if comment-stripping ever eats the call itself.
    """
    report = mod._local_seam_report()
    assert report["seam_present"] is True, report
