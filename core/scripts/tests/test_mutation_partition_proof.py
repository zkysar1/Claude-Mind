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


def test_rows_carry_the_restore_and_residue_fields(bed):
    """A per-mutant row must forward BOTH restore fields, not just the tally.

    g-115-6356: `restore_status` answers "target == BACKUP", which is NOT
    "target is clean" -- a backup taken over pre-existing residue matches
    itself. `residue_check` is the field that separates the two, and a matrix
    that reports only `restore_status` reproduces the original false `ok` at
    N-mutant scale, where a human is even less likely to re-check by hand.

    `unavailable` is a THIRD value, distinct from clean and from RESIDUE: a
    `sabotage_sed` mutant injects text the prover cannot know, so its
    clean-check did not run. Both mutants below are sed-form, so both must say
    so rather than defaulting to something that reads as a pass.
    """
    # Both mutants must be KILLED, or the run fails for an unrelated reason and
    # this test would be asserting on a survivor report instead of a clean one.
    (bed / "check_both.sh").write_text(
        "grep -q '^GUARDED_TOKEN lives here$' target.txt && "
        "grep -q '^UNRELATED_TOKEN lives here$' target.txt\n", encoding="utf-8")
    rc, r = run_plan(bed, base(bed, test_cmd="bash check_both.sh", mutations=[
        dict(name="m1", case="c1", **SINGLE_SITE),
        dict(name="m2", case="c2",
             sabotage_sed="0,/^UNRELATED_TOKEN lives here$/s//CHANGED/"),
    ]))
    assert rc == 0 and r["verdict"] == "PASS", r
    for row in r["rows"]:
        assert row["restore_status"] == "ok", row
        assert row["residue_check"] == "unavailable", (
            "a sed mutant's residue check cannot run; rendering that as clean "
            f"is the absent-vs-zero masquerade (rb-245): {row}")


def _both_tokens_check(bed):
    """A predicate both mutations below can kill, so neither is a survivor."""
    (bed / "check_both.sh").write_text(
        "grep -q '^GUARDED_TOKEN lives here$' target.txt && "
        "grep -q '^UNRELATED_TOKEN lives here$' target.txt\n", encoding="utf-8")


# The two mutations are the same pair test_target_is_byte_identical_after_every_mutant
# uses. That test drives a test_cmd which does NOT read stdin -- which is precisely
# why the defect below survived this file.
_STDIN_PAIR = [
    dict(name="m1", case="c1", **SINGLE_SITE),
    dict(name="m2", case="c2", sabotage_old="UNRELATED_TOKEN", sabotage_new="CHANGED"),
]


def test_stdin_reading_test_cmd_does_not_truncate_the_matrix(bed):
    """THE STDIN-LEAK REGRESSION ().

    run_test in mutation-proof-test.sh redirected stdout and stderr but NOT stdin.
    This script drives its loop with `while read ... done <<< "$PLAN_TSV"`, so
    inside the loop body fd 0 IS the remaining plan rows. A test_cmd that reads
    stdin consumed them, the loop's next `read` found nothing, and the run ended
    after the FIRST mutation.

    ASSERT THE COUNTS, NOT TERMINATION AND NOT THE VERDICT -- both are vacuous
    here, and that is the whole point of this test. Measured against the pre-fix
    script on this exact plan: it did not hang, it exited 0 in under a second and
    reported verdict PASS / mutations 1 / cases 1 / cases_unproven [] against a
    plan declaring TWO of each. A termination assertion passes that. A verdict
    assertion passes that. Only the count discriminates.

    The severity is the inverse of what a hang would be: this tool exists to
    detect a k/N tally that conceals unproven cases, and the truncation made its
    own tally read 1/1. A silent false PASS from the vacuity detector is worse
    than a loud stall.
    """
    _both_tokens_check(bed)
    rc, r = run_plan(bed, base(
        bed, test_cmd="cat >/dev/null; bash check_both.sh", mutations=_STDIN_PAIR))
    assert r["mutations"] == 2, (
        f"matrix truncated: {r['mutations']} of 2 mutations ran -- the test_cmd "
        f"consumed the caller's plan rows from stdin")
    assert len(r["rows"]) == 2
    assert sorted(row["case"] for row in r["rows"]) == ["c1", "c2"]
    assert r["killed"] == 2 and r["survivors"] == []
    assert r["cases"] == 2 and r["cases_unproven"] == []
    assert rc == 0


def test_single_mutation_plan_with_stdin_reading_test_cmd_still_passes(bed):
    """Plan-size control: the fix must not trade one plan size for another.

    A 1-mutation plan was green BEFORE the fix (the single `read` consumes the
    whole here-string, so stdin is already at EOF). It must stay green after.
    Paired with the test above this isolates the variable to stdin rather than
    to plan size -- either test alone is consistent with the wrong explanation.
    """
    _both_tokens_check(bed)
    rc, r = run_plan(bed, base(
        bed, test_cmd="cat >/dev/null; bash check_both.sh",
        mutations=[_STDIN_PAIR[0]]))
    assert r["mutations"] == 1 and r["killed"] == 1
    assert r["cases_unproven"] == [] and rc == 0


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


# --- multi-line sabotage is refused, not silently flattened (rb-6196, ) ---

def _run_raw(bed, plan):
    """Refusals exit 2 with stderr only, so run_plan's JSON assertion cannot see them."""
    plan_path = bed / "plan_raw.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return subprocess.run(
        [BASH, str(SCRIPT), "--plan", str(plan_path), "--quiet"],
        capture_output=True, text=True, timeout=300,
    )


@pytest.mark.parametrize("field", ["sabotage_old", "sabotage_new", "sabotage_sed"])
def test_multiline_sabotage_field_is_refused(bed, field):
    """The plan is flattened into a newline-separated TSV, so a newline cannot survive.

    Before this refusal it failed LATER as "sabotage-old string not found in
    target" -- honest about the outcome, misleading about the cause. Measured
    (zeta, cc-02): a 7-mutation plan returned 4 survivors split perfectly on line
    count. That message reads as "your anchor is stale", so the next move is
    re-deriving an anchor that was never wrong -- or worse, "fixing" correct tests.
    """
    mut = {"name": "m1", "case": "c1", "test_cmd": "bash check.sh", field: "line one\nline two"}
    if field != "sabotage_sed":
        mut.setdefault("sabotage_old", "GUARDED_TOKEN")
    proc = _run_raw(bed, base(bed, mutations=[mut]))
    assert proc.returncode == 2, f"not refused: rc={proc.returncode} out={proc.stdout[:300]}"
    assert "contains a newline" in proc.stderr
    # The refusal must POINT AT THE FIX, not just say no -- sabotage_sed keeps the
    # expression single-line while sed does the multi-line work.
    assert "sabotage_sed" in proc.stderr


def test_single_line_sabotage_is_still_accepted(bed):
    """POSITIVE CONTROL -- a refusal that rejected every plan would satisfy the test above."""
    rc, r = run_plan(bed, base(bed, mutations=[
        {"name": "m1", "case": "c1", "test_cmd": "bash check.sh", **SINGLE_SITE},
    ]))
    assert rc == 0, f"a valid single-line plan was refused: {r}"
    assert r["mutations"] == 1   # field is "mutations"; read from the report, not guessed
