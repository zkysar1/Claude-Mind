#!/usr/bin/env python3
"""Fixtures for core/scripts/hardcoded-scope-audit.py.

WHY THIS FILE EXISTS AT ALL. The script's failure mode is SILENT VACUITY — a
scan that reaches nothing, or a `_score` that classifies nothing as
active-scope, reports success either way. A one-time control run proves the
detector worked on the day it was written and says nothing about the next edit
to `_score`. These are the controls, pinned.

The detector's own verify-learning call site reproduced the very defect it
detects on its first day (scanned 1985 files instead of 2063 because
$WORLD_PATH was unset, and still reported PASS) — hence
test_unset_world_path_is_reported_as_a_skipped_root, which is the highest-value
test here.

POSITIVE **AND** NEGATIVE CONTROLS. A detector that flags everything and a
detector that flags nothing both pass a one-sided suite. Every tier below has a
fixture, and test_fixtures_drive_distinct_tiers asserts they do not collapse —
IN ADDITION to the per-case tests, never instead of them (guard-1793): an
aggregate summarises one axis and reads green through a defect on any other.

WHAT THE FIXTURE SEAM EXCLUDES (guard-1462). Fixtures enter at a synthetic
corpus passed via --root, so they cover the regex, the resolve filter, tiering
and the CANNOT_CHECK path. They do NOT cover `_default_roots`' real-corpus
resolution (only its skip-reporting, unit-tested directly), nor the
verify-learning bash check that consumes this script. That check was verified
two-way by hand in g-115-4187's closure note, not here.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pathlib
import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _verify_corpus  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "hardcoded-scope-audit.py"

# Under `opt`, so it matches the allowlisted posix roots, and non-existent, so
# it survives the resolve filter. The whole corpus below depends on both.
GONE = "/opt/nonexistent-scope-root-xyz-9f3a"


def _mod():
    """Import a hyphenated script by path — it is not a legal module name."""
    spec = importlib.util.spec_from_file_location("hsa", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(root):
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)],
                       capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout), r.returncode


def tier_of(out, needle):
    """The tier of the single finding whose context carries `needle`."""
    hits = [f for f in out["findings"] if needle in f["context"]]
    assert len(hits) == 1, f"expected exactly 1 finding for {needle!r}, got {hits}"
    return hits[0]["tier"]


# --- corpus builders: one per tier, each the minimal shape that drives it ----
def write_scope_doc(d):
    (d / "doc.md").write_text(
        f"## Repo sweep\n\nFor each repo under {GONE}/ run the build.\n", encoding="utf-8")


def write_scope_script(d):
    (d / "probe.py").write_text(
        f'ROOT = "{GONE}"\nfor entry in os.listdir(ROOT):\n    handle(entry)\n', encoding="utf-8")


def write_marker_doc(d):
    (d / "marker.md").write_text(
        f"file_check: {GONE}/box-marker\n\nThis precondition is box-gated and must not "
        "resolve anywhere else.\n", encoding="utf-8")


def write_prose_doc(d):
    (d / "prose.md").write_text(
        f"Historically the tree lived at {GONE}/old until 2026-01-01, as measured then.\n",
        encoding="utf-8")


def write_fixture_file(d):
    t = d / "tests"
    t.mkdir(exist_ok=True)
    # Deliberately the ACTIVE-SCOPE shape — the point is that the test path wins.
    (t / "negative.md").write_text(
        f"For each repo under {GONE}/ run the build.\n", encoding="utf-8")


# --- tests -------------------------------------------------------------------
def test_zero_files_is_cannot_check_not_clean(tmp_path):
    """The anti-vacuity property: an empty scan must never read as a clean corpus."""
    empty = tmp_path / "empty"
    empty.mkdir()
    out, rc = run(empty)
    assert out["verdict"] == "CANNOT_CHECK", out["verdict"]
    assert out["files_scanned"] == 0
    assert rc == 2, "CANNOT_CHECK must be distinguishable at the exit code"


def test_documented_scope_root_is_active_scope(tmp_path):
    write_scope_doc(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0
    assert tier_of(out, "For each repo") == "active-scope"


def test_script_constant_scope_root_is_active_scope(tmp_path):
    """The instance-3 shape: literal on one line, dereference on the next, so no
    prose phrase like 'for each' is anywhere near it."""
    write_scope_script(tmp_path)
    out, _ = run(tmp_path)
    assert tier_of(out, "ROOT =") == "active-scope"


def test_gate_marker_is_absence_is_signal_not_a_defect(tmp_path):
    """The known-negative. A marker that MUST NOT resolve here is the healthiest
    construct in the corpus; ranking it as the top defect is how detectors get
    switched off."""
    write_marker_doc(tmp_path)
    out, _ = run(tmp_path)
    assert tier_of(out, "file_check") == "absence-is-signal"


def test_dated_narrative_is_prose(tmp_path):
    write_prose_doc(tmp_path)
    out, _ = run(tmp_path)
    assert tier_of(out, "Historically") == "prose"


def test_test_path_beats_scope_shape(tmp_path):
    """A negative fixture names a path that must not exist — that IS its point."""
    write_fixture_file(tmp_path)
    out, _ = run(tmp_path)
    assert tier_of(out, "For each repo") == "test-fixture"


def test_resolving_literal_is_not_reported(tmp_path):
    """The base filter. Uses this suite's own directory: guaranteed to exist, and
    root-matching on both posix (/opt/...) and Windows (C:\\...) boxes."""
    here = str(Path(__file__).resolve().parent)
    (tmp_path / "live.md").write_text(f"The suite lives at {here} on this box.\n",
                                      encoding="utf-8")
    out, _ = run(tmp_path)
    assert out["files_scanned"] == 1, "the file must have been read"
    assert out["non_resolving_literals"] == 0, out["findings"]


def test_nothing_is_filed_automatically(tmp_path):
    """guard-1561: tiers are candidates, never verdicts. A detector that files is
    a different, more dangerous artifact than the one this goal authorised."""
    write_scope_doc(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0, "a finding must NOT become a non-zero exit"
    assert out["verdict"] == "SCANNED"
    assert "CANDIDATES, never verdicts" in out["note"]


def test_unset_world_path_is_reported_as_a_skipped_root(monkeypatch):
    """The defect this script's OWN call site shipped with. A caller that has not
    sourced _paths.sh silently loses the world/conventions half of the corpus;
    without this report the loss is visible only as a smaller number nobody is
    comparing against."""
    m = _mod()
    monkeypatch.delenv("WORLD_PATH", raising=False)
    _, skipped = m._default_roots()
    assert any(s["root"] == "world/conventions" for s in skipped), skipped
    assert any("WORLD_PATH" in s["reason"] for s in skipped), skipped


def test_skipped_root_downgrades_the_verdict(tmp_path, monkeypatch):
    """SCANNED_PARTIAL is the whole point of reporting the skip: a partial scan
    that still says SCANNED is indistinguishable from a complete one."""
    m = _mod()
    write_scope_doc(tmp_path)
    monkeypatch.delenv("WORLD_PATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["hsa"])
    monkeypatch.chdir(tmp_path)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main()
    out = json.loads(buf.getvalue())
    assert rc == 0
    assert out["verdict"] == "SCANNED_PARTIAL", out["verdict"]
    assert out["roots_skipped"], "a downgraded verdict must name what it skipped"


def test_fixtures_drive_distinct_tiers(tmp_path):
    """Anti-vacuity aggregate. A `_score` collapsed to one tier would leave every
    per-case test above passing in isolation only if it happened to pick that
    tier — this asserts they are genuinely discriminated, together."""
    write_scope_doc(tmp_path)
    write_marker_doc(tmp_path)
    write_prose_doc(tmp_path)
    write_fixture_file(tmp_path)
    out, _ = run(tmp_path)
    tiers = {f["tier"] for f in out["findings"]}
    assert {"active-scope", "absence-is-signal", "prose", "test-fixture"} <= tiers, \
        sorted(tiers)


def test_verify_learning_still_calls_the_detector():
    """This detector has exactly ONE call site, and nothing was watching it.

    The module docstring above already says the verify-learning check is what
    consumes this script — it just never asserted it, so the claim was prose.
    Measured 2026-08-12 (g-115-6052, alpha worker Body, hostname cc-07): merge
    0dadcff34 dropped the `hardcoded-scope-detector-wired` check from
    verify-learning/SKILL.md and NOTHING reported it. The loss was found only
    incidentally, while restoring a neighbouring check the same merge dropped in
    the same seam.

    That is this file's own thesis turned on itself. The detector exists to find
    steps whose scope silently reaches an empty set and report success anyway —
    and with no call site it became exactly that: a scan that never runs, which
    from the outside is indistinguishable from a scan that always returns clean.
    A green suite over the 13 pins below proved the SCRIPT correct the whole time
    it was unreachable (guard-1943: pinning the writer says nothing about the
    wiring).
    """
    skill = (Path(__file__).resolve().parents[3] / ".claude" / "skills"
             / "verify-learning" / "SKILL.md")
    # Corpus, not the file: the verify-learning check corpus moved to
    # core/config/verify-learning-checks.jsonl on 2026-08-18 ().
    # This canary pins a CALL SITE, and the call site moved with it.
    text = _verify_corpus.corpus_text()
    assert "hardcoded-scope-audit.py" in text, (
        "verify-learning lost its hardcoded-scope-detector-wired check -- "
        "this script's only call site is gone, so the vacuity detector has "
        "inherited the vacuity it exists to detect")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-v", __file__]))
