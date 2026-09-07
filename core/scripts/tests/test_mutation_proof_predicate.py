"""Pins for the hand-rolled mutation-proof predicate ().

The PAIR (backup AND restore) is the fingerprint; either half alone is ordinary
work. These tests pin that asymmetry, because loosening it to either-half is the
obvious "improvement" and would make the gate fire on every `git restore`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _mutation_proof_predicate import advisory_text, detect  # noqa: E402


# ── affirmative: the four recorded occurrence shapes ──────────────────────────

def test_cp_bak_gradle_restore_fires():
    f = detect("cp src/App.java src/App.java.bak && sed -i s/a/b/ src/App.java "
               "&& ./gradlew test; cp src/App.java.bak src/App.java")
    assert f is not None
    assert f["form"] == "backup+testrun+restore"


def test_cp_to_tmp_pytest_restore_fires():
    f = detect("cp core/scripts/x.py /tmp/x.py && python3 -m pytest core/scripts/tests "
               "&& cp /tmp/x.py core/scripts/x.py")
    assert f is not None


def test_sabotage_marker_without_a_test_run_still_fires():
    """Occurrence 4 planted a MUTATION-PROOF marker and verified by grep."""
    f = detect("cp v.java v.java.orig && echo '// MUTATION-PROOF' >> v.java "
               "&& cp v.java.orig v.java")
    assert f is not None
    assert f["form"] == "backup+sabotage+restore"


def test_git_stash_pair_fires():
    f = detect("git stash && pytest -q tests/test_x.py && git stash pop")
    assert detect("git stash push") is None
    assert f is not None


# ── negative: either half alone is ordinary work ──────────────────────────────

def test_bare_restore_is_silent():
    """git-restore-uncommitted-gate.sh owns this risk; do not double-warn."""
    assert detect("git restore -- src/App.java") is None
    assert detect("git checkout -- src/App.java && pytest -q") is None


def test_bare_backup_is_silent():
    assert detect("cp important.conf important.conf.bak") is None


def test_pair_without_a_corroborator_is_silent():
    """A save-and-revert with no test run and no sabotage marker is not a proof."""
    assert detect("cp a.txt a.txt.bak && vim a.txt && cp a.txt.bak a.txt") is None


def test_ordinary_commands_silent():
    for cmd in ("git status && pytest -q",
                "./gradlew test --no-daemon",
                "ls -la && grep -rn foo core/"):
        assert detect(cmd) is None, cmd


# ── the helper-in-use suppression ─────────────────────────────────────────────

def test_already_using_the_helper_is_silent():
    assert detect("bash core/scripts/mutation-proof-test.sh --file x.py "
                  "&& cp x.py x.py.bak && cp x.py.bak x.py && pytest") is None


def test_partition_helper_also_suppresses():
    assert detect("bash core/scripts/mutation-partition-proof.sh && git stash "
                  "&& pytest && git stash pop") is None


# ── contract ──────────────────────────────────────────────────────────────────

def test_none_inputs_are_silent_not_crashing():
    for bad in (None, "", 0, [], {}):
        assert detect(bad) is None


def test_advisory_names_the_helper_and_stays_advisory():
    f = detect("cp a.py a.py.bak && pytest -q && cp a.py.bak a.py")
    text = advisory_text(f)
    assert "core/scripts/mutation-proof-test.sh" in text
    assert "ADVISORY ONLY" in text
    assert "g-115-3494" in text


# ── fresh-eyes-code regression pins ( self-review, 2026-09-06) ──────
# All three were probe-confirmed defects in the shipped predicate. Each pin
# names the false direction, because that is what a future "simplification"
# would restore.

def test_absolute_restore_destination_fires():
    """F1: the destination class forbade a leading '/', so every ABSOLUTE
    restore path was silently uncovered -- a false NEGATIVE on the commonest
    real-world form. The relative case passed, which is why the original pin
    missed it."""
    f = detect("cp /opt/repo/core/x.py /tmp/x.py && pytest -q "
               "&& cp /tmp/x.py /opt/repo/core/x.py")
    assert f is not None


def test_tmp_to_tmp_copy_is_not_a_restore():
    """Control for F1's fix: widening the destination must not make a
    tmp-to-tmp copy read as putting a file back."""
    assert detect("cp /tmp/a.py /tmp/b.py && pytest -q") is None


def test_heredoc_body_describing_the_shape_is_silent():
    """F2: a heredoc body is DATA, not a command. Documentation about the
    hand-rolled shape (including this predicate's own file, authored via
    heredoc) fired the advisory until _strip_heredoc_bodies was carried over
    from the sibling predicate."""
    doc = ("cat > notes.md <<'EOF'\n"
           "The shape is: cp app.java app.java.bak && ./gradlew test\n"
           "then cp app.java.bak app.java to restore.\n"
           "EOF")
    assert detect(doc) is None


def test_heredoc_stripping_does_not_hide_a_real_command():
    """Control for F2: stripping heredoc BODIES must leave the surrounding
    command matchable."""
    cmd = ("cat > note.txt <<'EOF'\nplain text\nEOF\n"
           "cp a.py a.py.bak && pytest -q && cp a.py.bak a.py")
    assert detect(cmd) is not None


def test_advisory_text_does_not_raise_on_a_foreign_finding():
    """F3: advisory_text indexed required keys directly, so any finding it did
    not build raised KeyError. Unreachable from detect(), but the gate calls it
    outside a try -- fail-open must not depend on that."""
    assert isinstance(advisory_text({"form": "unrecognised"}), str)
