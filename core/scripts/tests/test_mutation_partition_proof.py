"""mutation-partition-proof.sh: the N-mutation matrix must fail loudly ().

Hermetic: every case builds its own target file and predicates under tmp_path, so
nothing here touches the repo.

The highest-value test in this file is test_omitted_control_does_not_shift_fields.
The plan is flattened to one delimited line per mutation and read back in bash; the
first implementation used TAB, which is an IFS *whitespace* character, so a run of
them collapses to a single delimiter and an EMPTY field silently vanishes -- shifting
every later field left. An omitted control_cmd is exactly that case. It was invisible
in a 6-mutation dogfood on real files because every entry there happened to HAVE a
control_cmd, and it surfaced only when an adversarial plan omitted one.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _bash_helpers import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "mutation-partition-proof.sh"


@pytest.fixture
def bed(tmp_path):
    """A target with a guarded token plus an unrelated one, and two predicates."""
    (tmp_path / "target.txt").write_text(
        "GUARDED_TOKEN lives here\n"
        "an unrelated line mentioning GUARDED_TOKEN again\n"
        "UNRELATED_TOKEN lives here\n",
        encoding="utf-8",
    )
    # Anchored: only the first line satisfies it.
    (tmp_path / "check.sh").write_text(
        "grep -q '^GUARDED_TOKEN lives here$' target.txt\n", encoding="utf-8")
    # Unanchored: the token anywhere satisfies it -> must stay blind under a
    # single-site mutation, because line 2 still carries the token.
    (tmp_path / "naive.sh").write_text(
        "grep -q 'GUARDED_TOKEN' target.txt\n", encoding="utf-8")
    return tmp_path


def run_plan(bed, plan):
    plan_path = bed / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--plan", str(plan_path), "--quiet"],
        capture_output=True, text=True, timeout=300,
    )
    out = proc.stdout[proc.stdout.find("{"):].strip()
    assert out, f"no JSON report; rc={proc.returncode} stderr={proc.stderr[-400:]}"
    return proc.returncode, json.loads(out)


def base(bed, **extra):
    plan = {
        "workdir": str(bed), "target": "target.txt",
        "test_cmd": "bash check.sh",
        "mutations": [],
    }
    plan.update(extra)
    return plan


SINGLE_SITE = {"sabotage_sed": "0,/^GUARDED_TOKEN lives here$/s//MUTATED line/"}


def test_killed_mutation_with_blind_control_passes(bed):
    rc, r = run_plan(bed, base(bed, mutations=[
        dict(name="m1", case="c1", control_cmd="bash naive.sh", **SINGLE_SITE)]))
    assert rc == 0
    assert r["verdict"] == "PASS"
    assert r["killed"] == 1 and r["survivors"] == []
    assert r["cases_unproven"] == []
    assert r["rows"][0]["control"]["blind"] is True


def test_omitted_control_does_not_shift_fields(bed):
    """THE DELIMITER REGRESSION.

    With no control_cmd the row carries an empty field. Under a whitespace
    delimiter that field collapses and FORM/SAB1/SAB2 shift left, so the sabotage
    args become garbage: the prover reports 'string not found', the mutation reads
    as a SURVIVOR, and the absent control reads as CONTAMINATED. Both are false.
    """
    rc, r = run_plan(bed, base(bed, mutations=[
        dict(name="m1", case="c1", **SINGLE_SITE)]))
    row = r["rows"][0]
    assert row["control"] is None                       # absent, not contaminated
    assert r["controls_contaminated"] == []
    assert r["mutations_without_control"] == ["m1"]
    assert row["sabotage_sites"] == 1                   # the sabotage really applied
    assert row["killed"] is True                        # ...and was really caught
    assert rc == 0


def test_survivor_is_named_and_fails_the_run(bed):
    """A mutation the predicate cannot see must fail the run, not be tallied away."""
    rc, r = run_plan(bed, base(bed, mutations=[
        dict(name="blind-spot", case="unrelated",
             sabotage_old="UNRELATED_TOKEN", sabotage_new="CHANGED_TOKEN")]))
    assert rc == 1
    assert r["verdict"] == "FAIL"
    assert r["survivors"] == ["blind-spot"]
    assert r["cases_unproven"] == ["unrelated"]


def test_broad_mutation_is_flagged_even_though_killed(bed):
    """A >1-site mutation still kills, but must not read as proof of anchoring."""
    rc, r = run_plan(bed, base(bed, mutations=[
        dict(name="broad", case="c1",
             sabotage_old="GUARDED_TOKEN", sabotage_new="CHANGED_TOKEN")]))
    row = r["rows"][0]
    assert row["killed"] is True
    assert row["sabotage_sites"] == 2
    assert row["broad"] is True
    assert r["broad_mutations"] == ["broad"]


def test_contaminated_control_is_reported(bed):
    """A control that goes red is not unanchored -- the anchoring claim is unproven."""
    rc, r = run_plan(bed, base(bed, mutations=[
        dict(name="m1", case="c1", control_cmd="bash check.sh", **SINGLE_SITE)]))
    assert r["rows"][0]["control"]["blind"] is False
    assert r["controls_contaminated"] == ["m1"]


def test_unproven_case_surfaces_behind_a_perfect_tally(bed):
    """The  failure: k/N reads clean while a case is untouched.

    Two mutations both target case c1 and both die; case c2 has a mutation nothing
    catches. A per-mutation tally alone would read 2/3 and hide that c2 is unproven.
    """
    rc, r = run_plan(bed, base(bed, mutations=[
        dict(name="m1", case="c1", **SINGLE_SITE),
        dict(name="m2", case="c1",
             sabotage_old="GUARDED_TOKEN lives here", sabotage_new="gone"),
        dict(name="m3", case="c2",
             sabotage_old="UNRELATED_TOKEN", sabotage_new="CHANGED"),
    ]))
    assert rc == 1
    assert r["cases"] == 2
    assert r["cases_unproven"] == ["c2"]
    assert "m3" in r["survivors"]


def test_target_is_byte_identical_after_every_mutant(bed):
    before = (bed / "target.txt").read_bytes()
    run_plan(bed, base(bed, mutations=[
        dict(name="m1", case="c1", **SINGLE_SITE),
        dict(name="m2", case="c2",
             sabotage_old="UNRELATED_TOKEN", sabotage_new="CHANGED"),
    ]))
    assert (bed / "target.txt").read_bytes() == before


def test_ambiguous_sabotage_form_is_refused(bed):
    plan_path = bed / "plan.json"
    plan_path.write_text(json.dumps(base(bed, mutations=[
        dict(name="m1", sabotage_old="a", sabotage_new="b",
             sabotage_sed="s/a/b/")])), encoding="utf-8")
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--plan", str(plan_path)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2
    assert "exactly one" in proc.stderr
