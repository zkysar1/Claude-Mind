""": the conservation identity is ASSERTED AS A SUM, not as a bare count.

The identity

    filed + filing_failed + addressing_refused + skipped_already_converted
      + audit_stale + overflow == scanned

is the acceptance criterion of recurring goal g-115-754 (interval 5.33h) and was
claimed exact in FOUR places -- insight-trigger-sweep.py :105-110, the companion
test docstring ("outcome 1 must still hold EXACTLY"), and g-115-754's close notes
every tick. It was not exact, and NO test summed the terms: every `scanned`
assertion across the five sweep test files is a bare count. Four prose defenses,
zero enforcement (guard-1082 -- a claim with no positive control behind it).

Two independent defects, measured 2026-08-09 (alpha, cc-04, Linux 6.8.0-136-generic):

  A. OVER-COUNT. `affects_missing` was reported inline with the disposition
     buckets, but :936-945 appends to it and then FALLS THROUGH to file_goal at
     :946 -- so the trigger is counted in BOTH affects_missing and filed.
     It is an ANNOTATION on the filed set, never a disposition. scanned=1, sum=2.

  B. UNDER-COUNT. `filed_count` counts only rc == 0 (:952). An attempted filing
     that FAILED was in `filed` but excluded from `filed_count`, landing in no
     bucket at all. scanned=1, sum=0.

  C. THE CASE THAT MATTERS -- they CANCEL. One of each in the same run gives
     scanned=2, sum=2: the identity holds, and ZERO goals were actually created.
     A bare `scanned` assertion cannot see any of this, which is why the defect
     survived every green run for as long as it existed.

guard-3092: conservation is NECESSARY BUT NOT SUFFICIENT. test_case_C below
therefore asserts BOTH `holds` and the actual goals-created count -- asserting
`holds` alone would have passed against the unfixed code.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_sweep_conservation.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

SWEEP_PATH = CORE_SCRIPTS / "insight-trigger-sweep.py"
_spec = importlib.util.spec_from_file_location("its_conservation", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_conservation"] = its
_spec.loader.exec_module(its)

# The six identity terms. Named here rather than inline so a future term added
# to the summary without being added here fails LOUDLY at test_terms_match_source
# below, instead of silently dropping out of the sum (guard-2529 -- a filter
# before counting must report what it excluded).
IDENTITY_TERMS = [
    "filed", "filing_failed", "addressing_refused",
    "skipped_already_converted", "audit_stale", "overflow",
]


def _findings_line(msg_id, *, author, target, action, severity,
                   affects_goal, timestamp):
    tags = [
        f"requires_action_by:{target}",
        f"action_type:{action}",
        f"severity:{severity}",
    ]
    if affects_goal:
        tags.append(f"affects:{affects_goal}")
    return json.dumps({
        "id": msg_id,
        "author": author,
        "channel": "findings",
        "type": "finding",
        "text": f"conservation trigger {msg_id}",
        "tags": tags,
        "timestamp": timestamp,
    }) + "\n"


def _aspiration_record(asp_id, goals):
    return json.dumps({"id": asp_id, "status": "active", "goals": goals}) + "\n"


def _trigger_timestamp(hours_ago=2.0):
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


@pytest.fixture
def sandbox(monkeypatch, tmp_path: Path):
    """Same sandbox shape as test_insight_trigger_sweep_reprobe.py.

    `rc_for` lets a test choose file_goal's return code per msg_id, which is
    what case B needs -- the reprobe fixture hardcodes rc=0 and so cannot
    express a failed filing at all. That gap is why defect B had no coverage.
    """
    world = tmp_path / "world"
    world.mkdir()
    findings = world / "board" / "findings.jsonl"
    findings.parent.mkdir(parents=True)
    asp_jsonl = world / "aspirations.jsonl"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    test_agent = agents_dir / "zeta-test"
    test_agent.mkdir()
    (test_agent / "local-paths.conf").write_text(
        f'WORLD_PATH="{world}"\nMETA_PATH="{tmp_path / "meta"}"\n', encoding="utf-8",
    )
    (test_agent / "aspirations.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr(its, "WORLD_ASPS", asp_jsonl)
    monkeypatch.setattr(its, "BOARD_DIR", findings.parent)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)
    # Hermeticity (): never read the REAL registry/roster.
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", tmp_path / "no-environments")
    monkeypatch.setattr(its, "_self_env", lambda: "test-env")
    monkeypatch.setattr(its, "_local_roster", lambda: set())

    rc_for = {}
    filed_calls = []

    def fake_file_goal(trigger, *, dry_run=False):
        filed_calls.append({"trigger": trigger, "dry_run": dry_run})
        return {"would_file": dry_run, "rc": rc_for.get(trigger["msg_id"], 0),
                "stdout": "", "stderr": ""}

    note_calls = []

    def fake_note(trigger, target_status):
        note_calls.append({"trigger": trigger, "target_status": target_status})
        return {"posted": True, "msg_id": f"fake-note-{trigger['msg_id']}"}

    monkeypatch.setattr(its, "file_goal", fake_file_goal)
    monkeypatch.setattr(its, "_emit_audit_stale_note", fake_note)

    return {"world": world, "findings": findings, "asp_jsonl": asp_jsonl,
            "filed_calls": filed_calls, "note_calls": note_calls, "rc_for": rc_for}


def _run(argv):
    saved = sys.argv
    sys.argv = ["insight-trigger-sweep.py"] + argv
    try:
        return its.main()
    finally:
        sys.argv = saved


def _sum_terms(summary):
    """Sum the identity terms FROM THE SUMMARY, independently of the
    `conservation` block the script computes. If the script's own sum and this
    one ever disagree, the script is grading its own homework."""
    return sum(summary[t] for t in IDENTITY_TERMS)


def test_terms_match_source(sandbox, capsys):
    """The script's declared term list must equal this test's, and its computed
    sum must equal an independently-summed one. Guards against a seventh bucket
    being added to the summary without joining the identity."""
    sandbox["findings"].write_text("", encoding="utf-8")
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")
    _run(["--dry-run", "--json"])
    summary = json.loads(capsys.readouterr().out)

    assert summary["conservation"]["terms"] == IDENTITY_TERMS
    assert summary["conservation"]["sum"] == _sum_terms(summary)
    assert summary["conservation"]["scanned"] == summary["scanned"]
    # fresh-eyes F-1: `sum` and `holds` were computed from two separately-written
    # copies of the same six-term expression, so an editor changing one and not
    # the other produced a `holds` disagreeing with the `sum` printed beside it.
    # Now hoisted to one local; this pins the relationship so a re-split fails.
    assert summary["conservation"]["holds"] == (
        summary["conservation"]["sum"] == summary["conservation"]["scanned"]
    )


def test_split_is_exhaustive_and_valued(sandbox, capsys):
    """fresh-eyes F-2: pin the filed/filing_failed split by VALUE, because
    `conservation.holds` structurally cannot.

    What this test does NOT do, stated plainly because the first draft claimed
    it did: it does not detect whether `filing_failed` is counted independently
    or derived as `len(filed) - filed_count`. Both forms were run against this
    file and both are 5/5 green — `rc == 0` and `rc != 0` are exact complements
    over one list, so the two counts sum to `attempted` by construction either
    way. `attempted == filed + filing_failed` is therefore a TAUTOLOGY, kept
    below only as a shape check, never as evidence.

    What it does do: assert each term's VALUE on a run containing one success
    and one failure. Sabotaging the split to miscount failures turns this red
    with `assert 1 == 2` (measured), so the per-term assertions are the live
    guarantee. This is guard-3092 in its sharpest form — the identity is
    necessary and not sufficient, and here it is not even *capable*, so the
    value assertions are doing all the work.
    """
    ts = _trigger_timestamp()
    sandbox["findings"].write_text(
        _findings_line("msg-split-OK", author="charlie", target="zeta",
                       action="extend-filter", severity="constrains",
                       affects_goal=None, timestamp=ts)
        + _findings_line("msg-split-FAIL", author="charlie", target="zeta",
                         action="extend-filter", severity="constrains",
                         affects_goal=None, timestamp=ts),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")
    sandbox["rc_for"]["msg-split-FAIL"] = 1

    _run(["--json"])
    summary = json.loads(capsys.readouterr().out)

    # The load-bearing assertions: per-term VALUES on a mixed run.
    assert summary["filed"] == 1, "exactly one filing returned rc == 0"
    assert summary["filing_failed"] == 1, "exactly one filing returned rc != 0"
    # Shape check only — holds by construction (see docstring). Present so a
    # future third disposition (neither counted) would surface here.
    assert summary["attempted"] == summary["filed"] + summary["filing_failed"]
    assert summary["conservation"]["holds"] is True


def test_case_A_affects_missing_does_not_double_count(sandbox, capsys):
    """A: affects target resolves to no goal. Pre-fix: scanned=1, sum=2."""
    sandbox["findings"].write_text(
        _findings_line("msg-cons-A", author="charlie", target="zeta",
                       action="extend-filter", severity="constrains",
                       affects_goal="g-999-999", timestamp=_trigger_timestamp()),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")  # target absent

    _run(["--dry-run", "--json"])
    summary = json.loads(capsys.readouterr().out)

    assert summary["scanned"] == 1
    assert summary["affects_missing_annotation"] == 1, "the annotation must still fire"
    assert summary["filed"] == 1, "affects-missing still files (file-as-is-with-warning)"
    assert _sum_terms(summary) == 1, (
        f"OVER-COUNT: sum={_sum_terms(summary)} scanned=1 — affects_missing is an "
        f"annotation on the filed set, not a seventh disposition"
    )
    assert summary["conservation"]["holds"] is True


def test_case_B_failed_filing_lands_in_a_bucket(sandbox, capsys):
    """B: filing returns rc != 0. Pre-fix: scanned=1, sum=0 (no bucket at all)."""
    sandbox["findings"].write_text(
        _findings_line("msg-cons-B", author="charlie", target="zeta",
                       action="extend-filter", severity="constrains",
                       affects_goal=None, timestamp=_trigger_timestamp()),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")
    sandbox["rc_for"]["msg-cons-B"] = 1  # the filing FAILS

    # NOT --dry-run: filed_count == len(filed) on the dry-run branch (:950),
    # so the defect is unreachable there. The production shape is the apply
    # path (guard-920 — replicate the literal production arg shape).
    _run(["--json"])
    summary = json.loads(capsys.readouterr().out)

    assert summary["scanned"] == 1
    assert summary["attempted"] == 1, "the filing was attempted"
    assert summary["filed"] == 0, "rc != 0 must not count as filed"
    assert summary["filing_failed"] == 1, "a failed filing needs its own bucket"
    assert _sum_terms(summary) == 1, (
        f"UNDER-COUNT: sum={_sum_terms(summary)} scanned=1 — an attempted-and-failed "
        f"filing fell into no bucket"
    )
    assert summary["conservation"]["holds"] is True


def test_case_C_errors_do_not_cancel(sandbox, capsys):
    """C: one of each in the same run. Pre-fix the +1 and -1 CANCEL —
    scanned=2, sum=2, identity 'holds', and zero goals were created.

    guard-3092: this is why `holds` alone is not a sufficient assertion. The
    goals-created check below is what discriminates the fixed implementation
    from the broken one; without it this test passes against BOTH."""
    ts = _trigger_timestamp()
    sandbox["findings"].write_text(
        _findings_line("msg-cons-C1", author="charlie", target="zeta",
                       action="extend-filter", severity="constrains",
                       affects_goal="g-999-999", timestamp=ts)
        + _findings_line("msg-cons-C2", author="charlie", target="zeta",
                         action="extend-filter", severity="constrains",
                         affects_goal=None, timestamp=ts),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")
    sandbox["rc_for"]["msg-cons-C2"] = 1  # second filing FAILS

    _run(["--json"])
    summary = json.loads(capsys.readouterr().out)

    assert summary["scanned"] == 2
    assert _sum_terms(summary) == 2
    assert summary["conservation"]["holds"] is True
    # The discriminating assertions — these are what the pre-fix code fails.
    assert summary["affects_missing_annotation"] == 1
    assert summary["filing_failed"] == 1
    assert summary["filed"] == 1, (
        "exactly one goal was actually created (C1 filed OK, C2 failed) — a run "
        "whose terms sum correctly can still have created fewer goals than it "
        "scanned, so `holds` must never be read alone (guard-3092)"
    )
