"""test_narrative_clobber_audit.py —  regression suite.

WHAT THIS PINS. `narrative-clobber-audit.py` asks whether a narrative write
destroyed the note it replaced. Its predicate is CONTAINMENT (does the pre-write
text still appear in the post-write text), which is right and must not be
weakened — a length delta misses the same-size clobber the audit's own docstring
records at +11 chars. But containment is NECESSARY, NOT SUFFICIENT: two
SANCTIONED writes also fail it, and before this goal both were reported as data
loss.

WHY THE OVER-REPORT WAS NOT MERELY NOISE, which is the part worth carrying.
`if clob:` is the ONLY branch that prints the recovery footer, and that footer
sits one line away from `history.py restore` / `history-restore.sh` — destructive
CLI siblings of a safe module function with the identical name, which have caught
three agents (guard-5651, guard-4165; one truncated a live 18MB store by 7.5MB).
So every false positive walked an agent up to the trap. Measured 2026-09-01 on
two boxes independently: cc-07 6 of 6 flagged rows sanctioned, cc-02 4 of 5.
Real data loss on cc-07: ZERO.

THE ASYMMETRY THAT SHAPES EVERY TEST BELOW. This code is an EXEMPTER, not a
detector. A detector that over-matches is noisy and self-announcing; an exempter
that over-matches is a SILENT FALSE MISS that disables the protection (guard-4015).
So the tests that matter most are the ones asserting the exemption does NOT fire:
`test_marker_quoted_in_prose_does_not_exempt` and
`test_recurring_but_hand_written_pre_stays_clobbered`. If those two ever go green
by accident the audit still reports "clean" while blind.

THE TIGHTENING, and why it is deliberately narrower than the filing asked for.
g-115-8514's scope addition proposed skipping rows where `recurring` is true and
the post note carries the closure-evidence marker. That is too broad. The
mechanism's own rule (guard-5679) is that a note WITHOUT the auto stamp was
hand-written and must never be superseded — and closure-evidence-write.sh's
recurring branch has no never-clobber test to enforce it, so it CAN silently
destroy a hand-written note (guard-5049, tracked as g-115-7733). Keying on
`recurring` alone would have hidden that live defect, which is the one TRUE
positive in this class. The exemption therefore requires the auto stamp on the
DESTROYED note.

THE BASH TWIN. `_has_auto_marker` mirrors closure-evidence-write.sh::_ce_marker_ach,
which shipped this same anchoring fix for this same reason (g-115-7853). Two
consumers of one marker convention is exactly the shape guard-4015's corollary
warns about, so `test_marker_constants_match_the_bash_twin` is a drift tripwire:
if either side re-spells the token, this fails rather than one side silently
exempting things the other does not.
"""
import gc
import importlib.util
import json
import pathlib
import re
import sys
import types
import weakref

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load():
    """Import the audit by path — its filename has hyphens, so no plain import."""
    spec = importlib.util.spec_from_file_location(
        "narrative_clobber_audit", _SCRIPTS / "narrative-clobber-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nca = _load()

# A properly-shaped stamp, as closure-evidence-write.sh actually writes it.
AUTO_STAMP = ("[closure-evidence:auto] written 2026-09-01T02:02:48 by "
              "closure-evidence-write.sh (achievedCount=407)")


def _auto_note(body="prior occurrence body"):
    return "%s\n\n%s" % (body, AUTO_STAMP)


# ---------------------------------------------------------------- the marker

def test_properly_shaped_stamp_is_recognised():
    """Positive control. Without this, every negative below is vacuous."""
    assert nca._has_auto_marker(_auto_note()) is True


def test_marker_quoted_in_prose_does_not_exempt():
    """guard-4015: an exemption that matches its own documentation is a silent
    false miss. This is not hypothetical — the closure narrative written for
    g-115-8514 itself quotes the marker while explaining the mechanism, and a
    bare `in` test would have exempted it."""
    note = ("I am explaining the mechanism: closure-evidence-write.sh stamps "
            "notes with [closure-evidence:auto] written <ts> by "
            "closure-evidence-write.sh (achievedCount=N) so the next occurrence "
            "can recognise a machine-written note.")
    assert nca._has_auto_marker(note) is False


def test_token_starting_a_line_without_the_full_shape_does_not_exempt():
    """Mirrors the bash twin's own adversarial fixture: starting a line with the
    token is not enough, the line must carry the written shape."""
    assert nca._has_auto_marker(
        "[closure-evidence:auto] - I am pasting the marker at the start of a line."
    ) is False


def test_empty_and_none_are_not_exempt():
    assert nca._has_auto_marker("") is False
    assert nca._has_auto_marker(None) is False


DEFER_STAMP = ("[closure-evidence:deferred] written 2026-08-30T15:43:37 by "
               "closure-evidence-write.sh (achievedCount=361) - the note above "
               "predates the provenance marker or was hand-written, so THIS "
               "occurrence preserved it rather than superseding it. The NEXT "
               "occurrence (achievedCount > 361) MAY supersede it.")


def test_deferred_stamp_is_a_provenance_stamp_too():
    """FOUND ONLY BY WIDENING THE WINDOW. At the default --examine 20 this token
    is invisible; at 68 the single CLOBBERED row on cc-07 was a deferred->
    superseded transition (g-115-105, 7832 -> 4815). The prior occurrence
    DECLINED to supersede and said in the note itself that the next one may."""
    assert nca._provenance_stamp("body\n\n" + DEFER_STAMP) == "deferred"
    assert nca._provenance_stamp(_auto_note()) == "auto"
    assert nca._provenance_stamp("an ordinary hand-written note") is None


def test_deferred_supersede_names_the_hand_written_exposure():
    """Sanctioned, but it is the ONE place the framework knowingly destroys
    possibly-hand-written text — the row must say so rather than reading like
    any other supersede."""
    v, why = nca._classify("prior body\n\n" + DEFER_STAMP,
                           _auto_note("occurrence N"),
                           {"recurring": True})
    assert v == "superseded"
    assert "hand-written" in why and "g-115-7733" in why


def test_deferred_token_quoted_in_prose_does_not_exempt():
    note = ("Explaining: a note may carry [closure-evidence:deferred] written "
            "<ts> by closure-evidence-write.sh (achievedCount=N) when the "
            "occurrence declines to supersede.")
    assert nca._provenance_stamp(note) is None


def test_deferred_token_without_the_shape_does_not_exempt():
    assert nca._provenance_stamp(
        "[closure-evidence:deferred] pasted at a line start, no shape") is None


def test_marker_constants_match_the_bash_twin():
    """Drift tripwire across the two consumers of one marker convention.

    Asserts the LITERAL agrees with closure-evidence-write.sh. This reads the
    sibling's source deliberately and narrowly — it is a constant-agreement
    check, not a test that builds its population by grepping a name
    (guard-5611). The sibling's own suite does the same in reverse.
    """
    src = (_SCRIPTS / "closure-evidence-write.sh").read_text(encoding="utf-8")
    assert 'CE_AUTO_MARK="%s"' % nca.CE_AUTO_MARK in src, (
        "the audit and closure-evidence-write.sh disagree on the auto marker; "
        "one side will exempt notes the other does not")
    assert 'CE_DEFER_MARK="%s"' % nca.CE_DEFER_MARK in src, (
        "the audit and closure-evidence-write.sh disagree on the deferred marker")
    assert nca.CE_AUTO_SHAPE in src, (
        "the audit's shape anchor no longer appears in the bash matcher")


# -------------------------------------------------------------- the verdicts

def test_containment_still_wins_when_text_is_preserved():
    v, _ = nca._classify("old body", "old body\n\nplus an append", {})
    assert v == "preserved"


def test_absent_pre_is_new_not_loss():
    v, _ = nca._classify("", "a first note", {})
    assert v == "new"


def test_genuine_clobber_is_still_reported():
    """The whole point of the instrument. A hand-written note replaced by
    unrelated text on a one-shot goal is data loss and must stay loud."""
    v, _ = nca._classify("a hand-written finding", "something else entirely",
                         {"recurring": False})
    assert v == "CLOBBERED"


def test_same_size_replacement_is_still_a_clobber():
    """The class the containment predicate exists for: a length check reads +11
    chars and sees nothing wrong."""
    pre = "A" * 200
    post = "B" * 211
    v, _ = nca._classify(pre, post, {"recurring": False})
    assert v == "CLOBBERED"


def test_recurring_auto_to_auto_is_superseded_not_loss():
    v, why = nca._classify(_auto_note("occurrence N-1"),
                           _auto_note("occurrence N"),
                           {"recurring": True})
    assert v == "superseded"
    assert "auto-note" in why


def test_recurring_but_hand_written_pre_stays_clobbered():
    """THE LOAD-BEARING NEGATIVE (guard-5049 / ). closure-evidence-
    write.sh's recurring branch has no never-clobber test, so it CAN destroy a
    hand-written note. That is the one true positive in this class and the
    exemption must not swallow it."""
    v, _ = nca._classify("a hand-written artifact with no provenance stamp",
                         _auto_note("occurrence N"),
                         {"recurring": True})
    assert v == "CLOBBERED"


def test_non_recurring_with_auto_stamp_stays_clobbered():
    """The exemption is a conjunction; neither half alone may license it."""
    v, _ = nca._classify(_auto_note("prior"), "unrelated replacement",
                         {"recurring": False})
    assert v == "CLOBBERED"


def test_missing_goal_record_does_not_exempt():
    """Fail toward the loud side when the record cannot be resolved."""
    v, _ = nca._classify(_auto_note("prior"), "unrelated replacement", None)
    assert v == "CLOBBERED"


# --------------------------------------------------------------- the restore

def test_restore_is_recognised_when_older_text_is_reinserted():
    """A repair puts recovered history back into the MIDDLE of the field, so the
    immediately-prior text stops being contiguous and containment reads the
    remedy as the disease."""
    v, why = nca._classify("remnant tail only", "recovered head\nMIDDLE INSERT\nremnant",
                           {"recurring": False},
                           restore_lookup=lambda: "2026-08-31T22-42-55")
    assert v == "restored"
    assert "2026-08-31T22-42-55" in why


def test_restore_lookup_is_not_consulted_for_preserved_rows():
    """Laziness is load-bearing: each lookback reconstructs the whole store from
    its blob chain, so it must run only for rows that would otherwise report
    loss."""
    calls = []

    def lookup():
        calls.append(1)
        return None

    nca._classify("old body", "old body plus more", {}, restore_lookup=lookup)
    assert calls == [], "restore lookback ran on a row that never failed containment"


def test_restore_lookup_absent_falls_back_to_clobbered():
    v, _ = nca._classify("a hand-written finding", "something else", {}, None)
    assert v == "CLOBBERED"


# ------------------------------------------------------------ the instrument

def test_reclassified_rows_are_not_dropped_from_the_report():
    """An exemption you cannot audit is how a silent false miss survives review.
    The reclassified verdicts must remain printable rows, not deletions."""
    src = (_SCRIPTS / "narrative-clobber-audit.py").read_text(encoding="utf-8")
    # every row appended is printed; assert no filter drops the new verdicts
    assert re.search(r'for r in rows:', src), "the table no longer iterates all rows"
    assert 'superseded=%d restored=%d' in src, (
        "the reclassified counts are no longer reported beside CLOBBERED")


def test_exit_code_and_footer_key_on_clobbered_only():
    """The footer carries the destructive-sibling warning and must fire for real
    loss only — that is the entire point of the reclassification."""
    src = (_SCRIPTS / "narrative-clobber-audit.py").read_text(encoding="utf-8")
    assert 'clob = [r for r in rows if r["verdict"] == "CLOBBERED"]' in src
    assert "return 1 if clob else 0" in src


# ------------------------------------------------------------------ memory

class _Snapshot:
    """A restored snapshot the test can COUNT. A str cannot be weak-referenced, so
    the fake restore hands main() this instead: load() passes non-bytes through
    unchanged, and the audit only ever calls .splitlines() on what it loaded."""
    live = weakref.WeakSet()

    def __init__(self, text):
        self.text = text
        _Snapshot.live.add(self)

    def splitlines(self):
        return self.text.splitlines()


# Oldest -> newest writes: (goal, the progress_note the write leaves behind).
_WRITES = [
    ("g-1-1", "alpha"),                 # new
    ("g-1-2", "one"),                   # new
    ("g-1-1", "alpha beta"),            # preserved
    ("g-1-2", "two"),                   # CLOBBERED
    ("g-1-1", "gamma"),                 # CLOBBERED
    ("g-1-1", "alpha beta, restored"),  # restored: re-inserts an older value
    ("g-1-2", "two three"),             # preserved
    ("g-1-1", "zeta"),                  # newest: its post-state is the live file, never examined
]


def _run_audit(monkeypatch, capsys, cache_size):
    """main() over a synthetic .history. Returns (report, how many snapshots
    were already resident each time a new one was restored)."""
    state = {"g-1-1": "", "g-1-2": ""}
    pre = []  # (snapshot name, manifest summary, PRE-write content), oldest first
    for n, (gid, note) in enumerate(_WRITES):
        goals = [{"id": g, "progress_note": v} for g, v in state.items()]
        pre.append(("2026-09-22T00-00-%02d_alpha" % n,
                    "update-goal %s progress_note" % gid,
                    json.dumps({"id": "asp-1", "goals": goals})))
        state[gid] = note
    snaps = [types.SimpleNamespace(name=name) for name, _, _ in reversed(pre)]
    summaries = {name: summary for name, summary, _ in pre}
    contents = {name: content for name, _, content in pre}
    resident = []

    def restore(target, name, base):
        resident.append(len(_Snapshot.live))
        return _Snapshot(contents[name])

    monkeypatch.setattr(nca, "SNAPSHOT_CACHE", cache_size)
    monkeypatch.setattr(nca._history_store, "restore", restore)
    monkeypatch.setattr(nca, "_summary", lambda p: summaries[p.name])
    monkeypatch.setattr(nca.H, "resolve_target", lambda f: "target")
    monkeypatch.setattr(nca.H, "resolve_base_dir", lambda t: "base")
    monkeypatch.setattr(nca.H, "_find_history_snapshots", lambda t: snaps)
    monkeypatch.setattr(nca.H, "parse_snapshot_name", lambda n: (n[:19], "alpha"))
    monkeypatch.setattr(sys, "argv", ["narrative-clobber-audit.py", "--examine", "100", "--json"])
    gc.collect()
    assert not _Snapshot.live, "an earlier run left restored snapshots alive"
    nca.main()
    return json.loads(capsys.readouterr().out), resident


def test_resident_snapshots_are_bounded_and_verdicts_unchanged(monkeypatch, capsys):
    """The cache used to keep one whole-store copy for every snapshot it ever
    loaded, and one copy of the goal store is 29 MB on disk: `--examine 400`
    reached 4.9 GB and then 7.4 GB on cc-13 (2026-09-22) and was OOM-killed both
    times. Bounded, at most SNAPSHOT_CACHE copies may be resident, and the report
    must match the unbounded cache's exactly — including the restored row, whose
    lookback loads a THIRD snapshot in the middle of a row."""
    bound = nca.SNAPSHOT_CACHE  # read before _run_audit monkeypatches it
    bounded, seen = _run_audit(monkeypatch, capsys, bound)
    unbounded, seen_all = _run_audit(monkeypatch, capsys, 10 ** 9)
    assert [r["verdict"] for r in bounded["rows"]] == [
        "preserved", "restored", "CLOBBERED", "CLOBBERED", "preserved", "new", "new"]
    assert bounded == unbounded, "bounding the cache changed the report"
    assert max(seen) + 1 <= bound, seen
    # Positive control: unbounded, the counter DOES see copies pile up, so the
    # bound asserted above is a measurement and not a vacuous pass.
    assert max(seen_all) + 1 > bound, seen_all


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ------------------------------------------------ the rotation exemption ( lane)
#
# THE THIRD SANCTIONED WRITE. goal-field-append.py bounds an oversize note by
# moving its OLDEST blocks to an archive sink and PREPENDING a notice, so the
# pre-write text stops being a substring and the row read as total loss.
# guard-6715 records this same false positive for the hand-executed FOLD and
# patches the READER; these pin the DETECTOR for the AUTOMATIC rotation, which
# recurs on every note crossing GOAL_NOTE_ROTATE_BYTES, fleet-wide.
#
# Same asymmetry as the marker tests above: this is an EXEMPTER, so the tests
# that matter most are the ones asserting it does NOT fire.

import _paths as _paths_mod


def _sink(tmp_path, gid, field):
    d = tmp_path / "audit-reports" / "goal-note-archive"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("%s.%s.md" % (gid, field))


def _rotated_pair(sink_path):
    """(pre, post) for a rotation: pre fails containment, post opens with the notice."""
    pre = "OLDEST BLOCK\n\n[appended:a]\n\nNEWEST BLOCK\n\n[appended:b]"
    post = ("%s 2026-09-22T12:49:10: the 1 oldest block(s) (99 bytes) were moved to "
            "%s to keep this field readable. Nothing was deleted — read them there."
            "\n\nNEWEST BLOCK\n\n[appended:b]" % (nca.ROTATE_NOTICE_HEAD, sink_path))
    return pre, post


def test_rotation_notice_constant_matches_the_producer():
    """Drift tripwire, mirroring test_marker_constants_match_the_bash_twin.

    Two consumers of one literal: if goal-field-append.py re-spells the notice,
    this fails loudly rather than the audit silently re-flagging every rotation.
    """
    src = (_SCRIPTS / "goal-field-append.py").read_text(encoding="utf-8")
    assert 'ROTATE_NOTICE_HEAD = "%s"' % nca.ROTATE_NOTICE_HEAD in src, (
        "the audit and goal-field-append.py disagree on the rotation notice head; "
        "every rotation would be re-reported as a clobber")


def test_verified_rotation_is_reclassified(tmp_path, monkeypatch):
    """Positive control. Without this, every negative below is vacuous."""
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    s = _sink(tmp_path, "g-1-1", "progress_note")
    s.write_text("archived blocks", encoding="utf-8")
    pre, post = _rotated_pair(s)
    v, why = nca._classify(pre, post, {}, gid="g-1-1", field="progress_note")
    assert v == "rotated", why
    assert "archive verified" in why


def test_rotation_with_missing_archive_stays_clobbered(tmp_path, monkeypatch):
    """THE one that must never go green by accident.

    A notice ASSERTING an archive is a claim, not evidence (guard-6715,
    archive-before-delete.md step 4). No file on disk => real potential loss.
    """
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    s = _sink(tmp_path, "g-1-1", "progress_note")      # path derived, file NOT written
    pre, post = _rotated_pair(s)
    v, why = nca._classify(pre, post, {}, gid="g-1-1", field="progress_note")
    assert v == "CLOBBERED"
    assert "MISSING archive" in why


def test_rotation_header_quoted_in_prose_does_not_exempt(tmp_path, monkeypatch):
    """guard-4015 anchoring: a note EXPLAINING rotation must not exempt itself.

    This is not hypothetical — the goal note that motivated this branch quotes
    the header verbatim while describing the mechanism.
    """
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    s = _sink(tmp_path, "g-1-1", "progress_note")
    s.write_text("archived blocks", encoding="utf-8")
    post = ("I am explaining the mechanism: goal-field-append.py prepends "
            "%s <ts>: ... were moved to %s ... when a note gets too large."
            % (nca.ROTATE_NOTICE_HEAD, s))
    v, _ = nca._classify("OLDEST BLOCK\n\n[appended:a]", post, {},
                         gid="g-1-1", field="progress_note")
    assert v == "CLOBBERED"


def test_rotation_naming_another_goals_archive_does_not_exempt(tmp_path, monkeypatch):
    """Conjunct 3: a header copied from another goal's note must not borrow
    that goal's archive to exempt THIS row."""
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    other = _sink(tmp_path, "g-9-9", "progress_note")
    other.write_text("someone else's archive", encoding="utf-8")
    pre, post = _rotated_pair(other)
    v, why = nca._classify(pre, post, {}, gid="g-1-1", field="progress_note")
    assert v == "CLOBBERED"
    assert "does not name this goal" in why


def test_rotation_without_gid_or_field_stays_clobbered(tmp_path, monkeypatch):
    """The default-argument path: an unidentified row cannot be verified, so it
    must fail to the loud side rather than exempt on the header alone."""
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    s = _sink(tmp_path, "g-1-1", "progress_note")
    s.write_text("archived blocks", encoding="utf-8")
    pre, post = _rotated_pair(s)
    v, why = nca._classify(pre, post, {})          # no gid/field
    assert v == "CLOBBERED"
    assert "cannot verify" in why


# -- the persistence trap: THE notice survives every later write (alpha cc-04 06:30) --
#
# rotate_oversize PREPENDS its notice, so a field that rotated once carries the
# marker forever. "Does post carry the marker?" answers "has this field EVER
# rotated?", never "was THIS write the rotation". Keying on presence alone is a
# FALSE GREEN — it clears a GENUINE clobber of a previously-rotated field, the
# one direction an exempter must never fail in. The discriminator is the STAMP
# DELTA: a rotation writes a NEW notice.

OLD_STAMP = "2026-09-01T00:00:00"
NEW_STAMP = "2026-09-22T12:49:10"


def _notice(stamp, sink_path):
    return ("%s %s: the 9 oldest block(s) (99999 bytes) were moved to %s to keep "
            "this field readable. Nothing was deleted — read them there."
            % (nca.ROTATE_NOTICE_HEAD, stamp, sink_path))


def test_genuine_clobber_of_a_previously_rotated_field_is_not_exempted(tmp_path, monkeypatch):
    """THE false-green regression. Reproduced adversarially before the fix.

    Field rotated long ago (notice persists at position 0, archive on disk),
    then suffers a real read-and-concatenate clobber from a stale copy. Every
    presence-based conjunct passes; only the stamp delta catches it.
    """
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    s = _sink(tmp_path, "g-1-1", "progress_note")
    s.write_text("blocks archived by the PAST rotation", encoding="utf-8")
    head = _notice(OLD_STAMP, s)
    pre = head + "\n\nblock A\n\n[appended:a]\n\nblock B\n\n[appended:b]"
    post = head + "\n\nTOTALLY DIFFERENT TEXT that destroyed A and B"
    v, why = nca._classify(pre, post, {}, gid="g-1-1", field="progress_note")
    assert v == "CLOBBERED", "exempted a real clobber: %s" % why
    assert "INHERITED" in why


def test_a_second_rotation_advances_the_stamp_and_is_still_exempted(tmp_path, monkeypatch):
    """Guard against over-correcting: a field CAN legitimately rotate twice, and
    the later rotation must still be reclassified."""
    monkeypatch.setattr(_paths_mod, "WORLD_DIR", str(tmp_path))
    s = _sink(tmp_path, "g-1-1", "progress_note")
    s.write_text("append-only sink, two rotations deep", encoding="utf-8")
    pre = _notice(OLD_STAMP, s) + "\n\nblock A\n\n[appended:a]\n\nblock B\n\n[appended:b]"
    post = _notice(NEW_STAMP, s) + "\n\nblock B\n\n[appended:b]"
    v, why = nca._classify(pre, post, {}, gid="g-1-1", field="progress_note")
    assert v == "rotated", why
    assert NEW_STAMP in why
