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

EVERY TEST BELOW WAS PROVEN RED BY MUTATION before being committed.
"""

import json
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
    return tmp_path


def run(bed, sabotage, test_cmd="bash check.sh"):
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--target", "target.txt", "--workdir", str(bed),
         "--test-cmd", test_cmd, *sabotage],
        capture_output=True, text=True, timeout=180,
    )
    return proc


def verdict(proc):
    line = proc.stdout[proc.stdout.find("{"):].strip()
    assert line, f"no JSON verdict; rc={proc.returncode} stderr={proc.stderr[-400:]}"
    return json.loads(line)


def backups(bed):
    return sorted(bed.glob("target.txt.mutation-backup.*"))


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
    assert backups(bed) == [], "refused before the backup was taken"


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
    assert verdict(proc)["verdict"] == "PASS"
    assert backups(bed) == []


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
    assert len(backups(bed)) == 1, (
        "the backup must survive the one path where a human needs it")


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


# --- the guarantee the fix must not have weakened ------------------------

def test_restore_still_leaves_the_target_byte_identical(bed):
    before = (bed / "target.txt").read_bytes()
    run(bed, SUB)
    assert (bed / "target.txt").read_bytes() == before
