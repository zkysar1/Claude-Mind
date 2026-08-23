"""`restore_status: ok` must stop meaning "the file is clean" ().

THE DEFECT. `restore()` did `cp -p "$BACKUP" "$TARGET"` and then `cmp -s` the
two. `cmp` proves target == BACKUP. It cannot prove target is CLEAN, because a
backup taken over a file that ALREADY contained the sabotage string matches
itself perfectly. echo reported `restore_status=ok` while the sabotage was still
live in a world/ script.

WHAT WAS MEASURED before the fix, and why the reported shape was not the worst
one. A two-site fixture with one site pre-sabotaged produced
`verdict: PASS`, reason "restore left no sabotage behind", sabotage LIVE, and
the backup DELETED — strictly worse than the FAIL echo saw, because a PASS ends
the reader's inquiry and the deleted backup removes the evidence. The two
mechanisms echo's goal proposed (own-cloud read-through clobber; sync racing
the restore) were both FALSIFIED by measurement: `ensure_local()` preserved a
divergent local file (`no_clobber` held), and 38 frequency cycles across a
git-tracked arm and a synced world/ arm produced 0 dirty targets.

The three fixes this file pins:
  1. ENTRY refusal -- a target already containing the sabotage string is
     refused before the backup is taken, because every verdict downstream
     would be about the wrong baseline.
  2. `residue_check` is EMITTED, with `unavailable` distinct from `clean`.
     `--sabotage-sed` injects text the script cannot know, so its clean-check
     DID NOT RUN; rendering that as a pass is the rb-245 absent-vs-zero class.
  3. The backup SURVIVES every exit path except the one state where it has no
     evidentiary value (PASS). It used to be deleted inside `restore()`, i.e.
     precisely on the paths where a human would need it.

AND A FOURTH, from g-115-7145 (2026-08-21):
  4. That surviving backup is written OUTSIDE the target's working tree. Fix 3
     is correct and unchanged, but it was landed on a backup named as a SIBLING
     of the target — so retention meant stray copies of production source
     accumulating in product repos (measured: five files after a 6-mutation
     partition run, seventeen after an 8-mutation Java run), and the next
     canonical step for product work stages with `git add -A`. Adjacency was
     never the point; recoverability was, and the two are separable.

     The goal that surfaced it also claimed `residue_check` was falsely
     asserting those files had been cleaned. IT WAS NOT: residue_check answers
     "did sabotage TEXT survive in the TARGET", it was correct on every run
     cited, and the two readings are about different objects. The real
     reporting gap was that NO field described the backup's on-disk state, so
     an N-mutation caller could not see N files piling up. `backup_retained` /
     `backup_path` close that, measured with a stat at emit time.

EVERY TEST BELOW WAS PROVEN RED BY MUTATION before being committed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _bash_helpers import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "mutation-proof-test.sh"

BODY = "alpha = 1\nbeta = 2\n"
CHECK = "grep -q '^alpha = 1$' target.txt\n"

SUB = ["--sabotage-old", "alpha = 1", "--sabotage-new", "alpha = 99"]
SED = ["--sabotage-sed", "0,/^alpha = 1$/s//alpha = 99/"]


@pytest.fixture
def bed(tmp_path):
    (tmp_path / "target.txt").write_text(BODY, encoding="utf-8")
    (tmp_path / "check.sh").write_text(CHECK, encoding="utf-8")
    # A PRIVATE TMPDIR (). The backup no longer lives beside the target,
    # so every assertion about where it went needs a store this test owns —
    # globbing the shared /tmp would race any concurrent run on this box and would
    # make "no backup was taken" unprovable rather than merely awkward.
    (tmp_path / "tmp").mkdir()
    return tmp_path


def run(bed, sabotage, test_cmd="bash check.sh"):
    env = dict(os.environ, TMPDIR=str(bed / "tmp"))
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--target", "target.txt", "--workdir", str(bed),
         "--test-cmd", test_cmd, *sabotage],
        capture_output=True, text=True, timeout=180, env=env,
    )
    return proc


def verdict(proc):
    line = proc.stdout[proc.stdout.find("{"):].strip()
    assert line, f"no JSON verdict; rc={proc.returncode} stderr={proc.stderr[-400:]}"
    return json.loads(line)


def adjacent_backups(bed):
    """Backups sitting IN the target's working tree — must ALWAYS be empty.

    This is the g-115-7145 defect surface: `product-pr-flow.sh` stages with
    `git add -A`, so anything matching this glob in a product repo gets
    committed into a PR and merged.
    """
    return sorted(bed.glob("target.txt.mutation-backup.*"))


def stored_backups(bed):
    """Backups in the out-of-tree store. NON-empty is the retention guarantee.

    Read this as the POSITIVE control for every emptiness assertion above it
    (guard-4166): a mutation-proof-test.sh that had stopped backing up at all
    would satisfy `adjacent_backups(bed) == []` perfectly.
    """
    return sorted((bed / "tmp").glob("mutation-proof-backup-*/*.mutation-backup.*"))


# --- 1. the entry refusal ------------------------------------------------

def test_preexisting_sabotage_string_is_refused_before_the_backup(bed):
    """THE REGRESSION THIS FILE EXISTS FOR.

    The target already contains what the sabotage would inject. A backup taken
    now captures residue, so `cmp` would certify it and every field downstream
    would describe the wrong baseline. Refuse at entry -- rc=2, no verdict.
    """
    (bed / "target.txt").write_text("alpha = 99\nbeta = 2\n", encoding="utf-8")
    proc = run(bed, SUB)
    assert proc.returncode == 2
    assert proc.stdout.strip() == "", "a refusal must emit no verdict at all"
    assert "already contains" in proc.stderr
    assert "alpha = 99" in proc.stderr, "the offending string must be named"
    # "before the backup was taken" is now checked in the store, not beside the
    # target. Since  the adjacency glob can NEVER match, so asserting
    # on it here would pass against a run that happily backed up (guard-2903).
    assert stored_backups(bed) == [], "refused before the backup was taken"
    assert adjacent_backups(bed) == []


def test_the_refusal_survives_the_string_appearing_only_in_a_comment(bed):
    """Substring, not line-equality: residue anywhere is still residue.

    A pre-check anchored to whole lines would miss a partial-line occurrence and
    hand back the same false `ok`. `grep -qF` is deliberate -- fixed-string, so
    a sabotage payload containing regex metacharacters is matched literally.
    """
    (bed / "target.txt").write_text(
        "alpha = 1\n# note: alpha = 99 was the old value\n", encoding="utf-8")
    proc = run(bed, SUB)
    assert proc.returncode == 2
    assert "already contains" in proc.stderr


# --- 2. residue_check is emitted, and absent != clean --------------------

def test_clean_run_reports_residue_check_clean(bed):
    proc = run(bed, SUB)
    v = verdict(proc)
    assert v["verdict"] == "PASS"
    assert v["restore_status"] == "ok"
    assert v["residue_check"] == "clean"


def test_sed_sabotage_reports_unavailable_and_never_clean(bed):
    """`--sabotage-sed` injects text the script cannot know.

    So the clean-check DID NOT RUN. `unavailable` must not collapse into
    `clean`: a check that never ran, rendered as a pass, is exactly the
    absent-vs-zero masquerade (rb-245) -- and it is the mode under which the
    ONLY remaining unguarded path runs.
    """
    proc = run(bed, SED)
    v = verdict(proc)
    assert v["verdict"] == "PASS"
    assert v["residue_check"] == "unavailable"
    assert v["residue_check"] != "clean"


def test_residue_check_is_present_on_a_failing_verdict_too(bed):
    """A field that only appears on PASS cannot be read as a safety signal."""
    proc = run(bed, ["--sabotage-old", "NO_SUCH_TOKEN_QQQ", "--sabotage-new", "x"])
    v = verdict(proc)
    assert v["verdict"] == "FAIL"
    assert "residue_check" in v


# --- 3. the backup survives every path that is not a clean PASS ----------

def test_backup_is_removed_on_pass(bed):
    """PASS is the one state where the backup has no evidentiary value."""
    proc = run(bed, SUB)
    v = verdict(proc)
    assert v["verdict"] == "PASS"
    assert stored_backups(bed) == []
    assert adjacent_backups(bed) == []
    # The whole store directory goes, not just the file inside it — otherwise
    # every passing run leaves an empty dir and the accumulation defect survives
    # in a quieter form ().
    assert list((bed / "tmp").iterdir()) == []
    assert v["backup_retained"] == "false"
    assert not Path(v["backup_path"]).exists()


# GREEN on call 1, RED on every call after: green -> red under sabotage ->
# red after restore. That drives Step 5's post-restore-RED path, which is the
# exact moment a human needs the backup and the exact moment the old code had
# already deleted it inside restore().
GREEN_ONCE = (
    "n=$(cat n.txt 2>/dev/null || echo 0); echo $((n+1)) > n.txt; "
    "[ \"$n\" -eq 0 ]\n"
)


def test_backup_is_retained_when_the_post_restore_test_is_red(bed):
    (bed / "once.sh").write_text(GREEN_ONCE, encoding="utf-8")
    proc = run(bed, SUB, test_cmd="bash once.sh")
    v = verdict(proc)
    assert v["verdict"] == "FAIL"
    assert "post-restore RED" in v["reason"]
    assert len(stored_backups(bed)) == 1, (
        "the backup must survive the one path where a human needs it")
    # : survive, but NOT inside the repo the target belongs to.
    assert adjacent_backups(bed) == []
    assert v["backup_retained"] == "true"
    assert Path(v["backup_path"]).exists()
    assert Path(v["backup_path"]).read_bytes() == (bed / "target.txt").read_bytes()


def test_the_post_restore_red_reason_does_not_blame_the_test(bed):
    """It used to assert the file matched its backup and call the test flaky.

    That is a safety tool whose failure mode is MISDIRECTION -- it points the
    reader at their own test and away from an unrestored file. The reason must
    now separate what IS established from what is NOT, and name the two probes
    a reader should actually run.
    """
    (bed / "once.sh").write_text(GREEN_ONCE, encoding="utf-8")
    v = verdict(run(bed, SUB, test_cmd="bash once.sh"))
    reason = v["reason"]
    assert "NOT established" in reason
    assert "CLEAN" in reason
    # backend-cat.sh reads the AUTHORITATIVE store; world-cat.sh reads the local
    # mirror, so on the own-cloud world/ target where this defect was reported
    # the mirror is the one thing that cannot settle the question.
    assert "backend-cat.sh" in reason
    assert "NOT world-cat.sh" in reason


def test_the_pass_reason_still_claims_no_sabotage_left_behind(bed):
    """The claim is now EARNED rather than removed, so it must still be made.

    Weakening it to silence would lose the signal a PASS is supposed to carry.
    What makes it honest is that it is now reachable only past the entry
    refusal and the restore-time re-check.
    """
    v = verdict(run(bed, SUB))
    assert v["verdict"] == "PASS"
    assert "restore left no sabotage behind" in v["reason"]


# --- 4. the retained backup is never inside the target's working tree ----

@pytest.mark.parametrize("test_cmd", ["bash check.sh", "bash once.sh"])
def test_no_run_ever_writes_a_backup_into_the_targets_working_tree(bed, test_cmd):
    """BOTH buckets, because the retention split is exactly where this failed.

    The clean-PASS path removes the backup, so it would look clean under any
    implementation and proves nothing on its own; the post-restore-RED path is
    the one that RETAINS, and is where the five/seventeen stray files were
    measured. Pinning only the passing case is how an absence-shaped fix gets
    certified by a test that cannot fail (guard-4374, guard-4166).
    """
    (bed / "once.sh").write_text(GREEN_ONCE, encoding="utf-8")
    v = verdict(run(bed, SUB, test_cmd=test_cmd))
    assert adjacent_backups(bed) == [], (
        "a backup beside the target is what `git add -A` commits into a PR")
    path = Path(v["backup_path"])
    # The invariant is "in the TMPDIR store, never beside the target". It is NOT
    # "outside `bed`": this fixture deliberately points TMPDIR *inside* tmp_path
    # so the test can own the store, which is exactly the case an outside-of-bed
    # assertion would fail on while the production shape (TMPDIR=/tmp, target in
    # a repo) is fine. Pin the two properties that actually travel.
    assert path.parent != (bed / "target.txt").parent, "backup is a sibling of the target"
    assert path.parent.name.startswith("mutation-proof-backup-")
    assert (bed / "tmp") in path.parents, "backup escaped the TMPDIR it was given"
    # POSITIVE CONTROL. Everything above is satisfied by a script that never
    # backs up at all — including, silently, by one whose backup step regressed
    # to a no-op. The retaining arm must show a real file with the target's bytes.
    if v["verdict"] != "PASS":
        assert path.exists() and path.read_bytes() == (bed / "target.txt").read_bytes()


def test_backup_retained_is_measured_not_inferred_from_the_verdict(bed):
    """The field must read the filesystem at emit time.

    A goal filed against this tool read `residue_check: clean` as a claim that
    the backup FILES had been cleaned up. It never was — residue_check is about
    sabotage TEXT surviving in the TARGET, and it was correct on both runs cited.
    The real gap was that nothing in the JSON described the backup's on-disk
    state at all, so an N-mutation caller had no way to see N files piling up.
    """
    (bed / "once.sh").write_text(GREEN_ONCE, encoding="utf-8")
    red = verdict(run(bed, SUB, test_cmd="bash once.sh"))
    green = verdict(run(bed, SUB))
    # residue_check reports the same value across both — it is answering a
    # different question, and reading it as a backup-cleanup signal is the
    # misreading this test exists to make impossible to repeat.
    assert red["residue_check"] == green["residue_check"] == "clean"
    assert red["backup_retained"] == "true"
    assert green["backup_retained"] == "false"
    assert Path(red["backup_path"]).exists()
    assert not Path(green["backup_path"]).exists()


# --- the guarantee the fix must not have weakened ------------------------

def test_restore_still_leaves_the_target_byte_identical(bed):
    before = (bed / "target.txt").read_bytes()
    run(bed, SUB)
    assert (bed / "target.txt").read_bytes() == before
