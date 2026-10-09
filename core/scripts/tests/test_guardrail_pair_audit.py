"""Tests for guardrail-pair-audit.py's adjudication memory ().

The seam is ported from guardrail-protocol-conflict-check.py: an in-record
`reconciled:` escape hatch (pair form: it must name the partner id) and a
`--known` flag. An adjudicated or known pair is still REPORTED and counted in
by_class; it only leaves `novel`. An undecided pair must come out exactly as it
did before the seam existed.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "guardrail_pair_audit",
    Path(__file__).resolve().parents[1] / "guardrail-pair-audit.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

# Two near-duplicate pairs with disjoint subjects: A/B (sim 5/6) and C/D (6/7).
A = {"id": "guard-1", "category": "ops", "status": "active",
     "rule": "Always verify the widget cache before restarting the widget service"}
B = {"id": "guard-2", "category": "ops", "status": "active",
     "rule": "Always verify the widget cache before restarting the widget service quickly"}
C = {"id": "guard-3", "category": "ops", "status": "active",
     "rule": "Never delete the gadget ledger during migration windows"}
D = {"id": "guard-4", "category": "ops", "status": "active",
     "rule": "Never delete the gadget ledger during migration windows ever"}

MARKER = "Keep both.\nreconciled: guard-2 keep both; prefer guard-2 when citing"


def _world(tmp_path, records, name="world"):
    world = tmp_path / name
    world.mkdir()
    with open(world / "guardrails.jsonl", "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return world


def _run_json(capsys, world, *extra):
    assert mod.main(["--world", str(world), "--output", "json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def _pair(result, a_id, b_id):
    rows = [f for f in result["findings"] if {f["a_id"], f["b_id"]} == {a_id, b_id}]
    assert len(rows) == 1, result["findings"]
    return rows[0]


def test_marker_in_action_hint_names_the_partner():
    rec = dict(A, action_hint=MARKER)
    assert mod.reconciled_partners(rec) == {"guard-2": "keep both; prefer guard-2 when citing"}


def test_marker_must_start_a_line_and_carry_a_concrete_id():
    # Prose that mentions or DESCRIBES the marker is not a marker (guard-2096).
    for hint in (
        "This pair was reconciled: guard-2 earlier",      # not at line start
        "write `reconciled: guard-NNN <reason>` here",    # documentation
        "reconciled: guard-NNN <reason>",                 # placeholder, no concrete id
        "reconciled: the two rules agree",                # names no partner
    ):
        assert mod.reconciled_partners(dict(A, action_hint=hint)) == {}, hint
    # Indented, upper-case, em-dash separator: still one marker.
    got = mod.reconciled_partners(dict(A, action_hint="x\n  RECONCILED: GUARD-7 — scope split by host"))
    assert got == {"guard-7": "scope split by host"}
    # The rule field is read too, as in the donor.
    assert mod.reconciled_partners(dict(A, rule=A["rule"] + "\nreconciled: guard-2 same rule")) == {
        "guard-2": "same rule"}


def test_adjudicated_pair_is_reported_distinctly_and_undecided_pair_is_unchanged(tmp_path, capsys):
    before = _run_json(capsys, _world(tmp_path, [A, B, C, D], "before"))
    after = _run_json(capsys, _world(tmp_path, [dict(A, action_hint=MARKER), B, C, D], "after"))

    # Still reported: nothing is suppressed, counts and classes are unchanged.
    assert after["findings_total"] == before["findings_total"] == 2
    assert after["by_class"] == before["by_class"] == {"near-duplicate": 2}
    assert (before["novel"], before["adjudicated"]) == (2, 0)
    assert (after["novel"], after["adjudicated"]) == (1, 1)

    ab = _pair(after, "guard-1", "guard-2")
    assert ab["adjudicated"] == {"guard-1": "keep both; prefer guard-2 when citing"}
    assert ab["known"] is True
    assert ab["class"] == "near-duplicate"

    # The undecided pair comes out byte-for-byte as it did without the marker.
    assert _pair(after, "guard-3", "guard-4") == _pair(before, "guard-3", "guard-4")
    assert _pair(after, "guard-3", "guard-4")["known"] is False


def test_marker_on_either_side_adjudicates(tmp_path, capsys):
    hint = "reconciled: guard-1 kept for its citations"
    res = _run_json(capsys, _world(tmp_path, [A, dict(B, action_hint=hint), C, D]))
    assert _pair(res, "guard-1", "guard-2")["adjudicated"] == {"guard-2": "kept for its citations"}
    assert res["novel"] == 1


def test_marker_naming_another_partner_does_not_quiet_this_pair(tmp_path, capsys):
    res = _run_json(capsys, _world(tmp_path, [dict(A, action_hint="reconciled: guard-9 other pair"), B, C, D]))
    ab = _pair(res, "guard-1", "guard-2")
    assert ab["adjudicated"] == {} and ab["known"] is False
    assert res["novel"] == 2


def test_known_flag_needs_both_ids(tmp_path, capsys):
    world = _world(tmp_path, [A, B, C, D])
    both = _run_json(capsys, world, "--known", "GUARD-1, guard-2")
    assert _pair(both, "guard-1", "guard-2")["known"] is True
    assert _pair(both, "guard-1", "guard-2")["adjudicated"] == {}
    assert both["novel"] == 1
    assert both["known_guardrails"] == ["guard-1", "guard-2"]

    one = _run_json(capsys, world, "--known", "guard-1,guard-3")
    assert one["novel"] == 2


def test_human_output_tags_the_adjudicated_pair(tmp_path, capsys):
    world = _world(tmp_path, [dict(A, action_hint=MARKER), B, C, D])
    assert mod.main(["--world", str(world)]) == 0
    out = capsys.readouterr().out
    assert "findings=2 by_class={'near-duplicate': 2} novel=1 adjudicated=1" in out
    assert "[near-duplicate] [adjudicated: guard-1]" in out
    assert "reconciled (guard-1): keep both; prefer guard-2 when citing" in out


def test_there_is_still_no_apply_path():
    with pytest.raises(SystemExit):
        mod.main(["--apply"])


# ---------------------------------------------------------------------------
# First-clause scoring (): a near-duplicate whose identity-bearing
# FIRST clause is identical used to hide as the members' elaborations diverge
# in length (whole-rule Jaccard 0.140 vs floor 0.6, first-clause Jaccard 1.000
# — measured by echo on guard-7131/guard-7149, recorded in 's
# progress_note). A pair is now reported when EITHER score clears the floor.
# ---------------------------------------------------------------------------

# Fixture pair modelled on guard-7131/guard-7149: verbatim-identical first
# clause, diverging elaborations. Whole-rule subject Jaccard is ~0.11
# (6 shared head tokens / 53 union) — the old gate would discard it.
E = {"id": "guard-7131", "category": "ops", "status": "active",
     "rule": ("Never probe a live daemon before confirming its identity. "
              "The measured failure was the probe returning exit 49 from a "
              "store stub while the daemon had been healthy all along, and "
              "the retry storm that followed consumed the entire billing "
              "window, so the identity check must come first and every probe "
              "must be bounded by a cost ceiling.")}
F = {"id": "guard-7149", "category": "ops", "status": "active",
     "rule": ("Never probe a live daemon before confirming its identity "
              "-- the rule was added after the incident where the operator "
              "box and the vessel box were mistaken for each other and the "
              "probe hit the wrong machine entirely; keep the allowlist "
              "explicit and re-verify the target after every restart.")}


def test_head_identical_pair_fails_old_scorer_passes_new(tmp_path, capsys, monkeypatch):
    world = _world(tmp_path, [E, F])

    # NEW scorer: the pair is reported via the first-clause score even though
    # the whole-rule score is far under the floor.
    new = _run_json(capsys, world)
    row = _pair(new, "guard-7131", "guard-7149")
    assert row["class"] == "near-duplicate"
    assert row["first_clause_similarity"] == 1.0
    assert row["similarity"] < 0.6  # the old gate (whole-rule only) discards it

    # OLD scorer, emulated: first-clause scoring removed (empty head subject
    # for every rule -> fc score 0.0) reduces the gate to whole-rule Jaccard,
    # and the pair disappears.
    monkeypatch.setattr(mod, "first_clause_subject_tokens", lambda rule: set())
    old = _run_json(capsys, world)
    assert old["findings_total"] == 0, old["findings"]


def test_first_clause_score_is_polarity_stripped(tmp_path, capsys):
    # Identical heads except the polarity word: the head score is about
    # identity, never polarity, and the polarity split still separates them.
    g = {"id": "guard-7001", "category": "ops", "status": "active",
         "rule": "Never verify the relay ledger before pruning the relay queue"}
    h = {"id": "guard-7002", "category": "ops", "status": "active",
         "rule": "Always verify the relay ledger before pruning the relay queue"}
    res = _run_json(capsys, _world(tmp_path, [g, h]))
    row = _pair(res, "guard-7001", "guard-7002")
    assert row["first_clause_similarity"] == 1.0
    assert row["class"] == "contradiction"


def test_skipped_pairs_reported_beside_skipped_buckets(tmp_path, capsys):
    # Five members of one category sharing a template: every shared
    # discriminative token (len>=4, non-stopword, non-polarity) becomes its
    # own oversized bucket at --max-bucket 2. The template below shares seven
    # (widget, surface, variant, quirk, before, touching, state), so the
    # skip is 7 buckets / 70 pairs — 5*4/2 per bucket. The PAIR count must
    # grow quadratically and be printed beside the bucket count.
    recs = [
        {"id": f"guard-{7100 + n}", "category": "ops", "status": "active",
         "rule": (f"Always inspect the widget surface variant {n} for its own "
                  f"quirk {n} before touching surface variant {n} state")}
        for n in range(5)
    ]
    world = _world(tmp_path, recs)
    res = _run_json(capsys, world, "--max-bucket", "2")
    assert res["oversized_buckets_skipped"] == 7
    assert res["pairs_skipped"] == 7 * (5 * 4 // 2) == 70
    assert res["pairs_compared"] == 0
    assert res["findings_total"] == 0

    assert mod.main(["--world", str(world), "--max-bucket", "2"]) == 0
    out = capsys.readouterr().out
    assert "skipped_pairs=70" in out
    assert "70 pair(s) live in those buckets" in out
