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


# --- : the three cc-02 code-row shapes, pinned as controls ----------
def write_escaped_strip(d):
    """The closure_evidence row: a character-strip set whose backslash escapes
    (`\\t:`, `\\u2014:`) the regex read as drive letters. The file must carry
    the escapes as literal text (backslash + letter), exactly as in the corpus."""
    (d / "strip.py").write_text(
        "import re\n"
        "m = re.match(r'x', s)\n"
        'rest = (m.group(2) or "").lstrip("*_ \\t:\\u2014\\u2013-")\n',
        encoding="utf-8")


def write_gitbash_tuple(d):
    """The _runtime_bash row: a candidate tuple iterated by a for-loop whose
    body probes each candidate for existence. Absence of each entry is the
    DESIGNED outcome — the loop takes whichever exists."""
    (d / "resolve.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "for cand in (\n"
        f'    "{GONE}/git/bin/bash.exe",\n'
        f'    "{GONE}/git2/bin/bash.exe",\n'
        "):\n"
        "    if Path(cand).exists():\n"
        "        return cand\n",
        encoding="utf-8")


def write_bash_probe(d):
    """The minio preflight row: the literal's own line carries the bash
    existence test. The window scorer cannot see this (its marker hints lose
    to the for-loop scope hint), so the structure must."""
    (d / "probe.sh").write_text(
        "for b in minio mc; do\n"
        f'    [ -x "$STAGE/$b" ] || [ -x "{GONE}/bin/$b" ] || die "missing"\n'
        "done\n",
        encoding="utf-8")


def write_install_target(d):
    """The minio install row: the file writes the literal — an install target
    is absent before install by design. A cp SOURCE (first argument) must NOT
    count as a self-created target."""
    (d / "install.sh").write_text(
        f'run install -o root -g root -m 0755 "$STAGE/$b" "{GONE}/bin/$b"\n'
        f'cp "{GONE}/src" /tmp/keep\n',
        encoding="utf-8")


def write_vacuous_glob(d):
    """THE POSITIVE CONTROL ( constraint): a hardcoded glob over a
    directory that does not exist, silently yielding an empty list. Any change
    must keep this in active-scope — the whole point of the detector."""
    (d / "vacuous.py").write_text(
        f"import glob\n"
        f'for p in glob.glob("{GONE}/*"):\n'
        f"    handle(p)\n",
        encoding="utf-8")


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


# --- : structural re-classifications, pinned ------------------------
def test_backslash_escape_is_not_a_drive_literal(tmp_path):
    r"""(a) The escape-sequence misparse: `\t:` and `\u2014:` in a strip set
    read as `t:\` / `u2014:\` drive roots. A backslash before the letter is an
    escape, not a path — the file must produce NO finding at all."""
    write_escaped_strip(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0
    assert out["non_resolving_literals"] == 0, out["findings"]


def test_probe_guarded_tuple_is_absence_is_signal(tmp_path):
    """(b) Candidate tuple iterated by a for-loop whose body probes the
    variable for existence: absence of each entry is the DESIGNED outcome.
    Both entries must leave active-scope (they are marker-shaped, not vacuous)."""
    write_gitbash_tuple(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0
    assert len(out["findings"]) == 2, out["findings"]
    for f in out["findings"]:
        assert f["tier"] == "absence-is-signal", f
        assert "probe-guarded" in f["why"], f


def test_inline_bash_probe_is_absence_is_signal(tmp_path):
    """(b2) The literal's own line carries `[ -x L ]`: the window scorer sees
    the for-loop scope hint but the probe's marker hints lose, which is how the
    cc-02 row read active-scope. The structure must win."""
    write_bash_probe(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0
    hit = [f for f in out["findings"] if GONE in f["literal"]]
    assert len(hit) == 1, out["findings"]
    assert hit[0]["tier"] == "absence-is-signal", hit[0]
    assert "inline existence probe" in hit[0]["why"], hit[0]


def test_install_target_is_absence_is_signal_but_source_is_not(tmp_path):
    """(c) A writer line (install/cp/mkdir/...) whose LAST argument is the
    literal: self-created, absent before install by design. A cp SOURCE — the
    FIRST argument, the line reads it — must NOT be re-classified by (c)."""
    write_install_target(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0
    tgt = [f for f in out["findings"] if f["literal"].endswith("/bin/$b")]
    assert len(tgt) == 1, out["findings"]
    assert tgt[0]["tier"] == "absence-is-signal", tgt[0]
    assert "self-created target" in tgt[0]["why"], tgt[0]
    src = [f for f in out["findings"] if f["literal"].endswith("/src")]
    assert len(src) == 1, out["findings"]
    assert src[0]["tier"] != "absence-is-signal", \
        "a copy SOURCE is not a self-created target (rule c over-fire)"


def test_vacuous_glob_stays_active_scope(tmp_path):
    """THE POSITIVE CONTROL ( constraint): a hardcoded glob over a
    non-existent directory that silently yields an empty list MUST still
    classify as active-scope after the structural rules. This fixture is the
    unprobed iteration — rule (b) must not fire on it."""
    write_vacuous_glob(tmp_path)
    out, rc = run(tmp_path)
    assert rc == 0
    hit = [f for f in out["findings"] if GONE in f["literal"]]
    assert len(hit) == 1, out["findings"]
    assert hit[0]["tier"] == "active-scope", \
        f"the vacuity class must survive the new rules: {hit[0]}"


def test_structural_rules_do_not_collapse_the_tiers(tmp_path):
    """The new shapes must ADD discrimination, not collapse the tiers:
    vacuous stays active-scope while the probe-guarded shape lands
    absence-is-signal in the SAME corpus (guard-1793, the distinct-tiers
    aggregate, extended to the structural rules)."""
    write_vacuous_glob(tmp_path)
    write_gitbash_tuple(tmp_path)
    out, _ = run(tmp_path)
    tiers = {f["tier"] for f in out["findings"]}
    assert {"active-scope", "absence-is-signal"} <= tiers, sorted(tiers)


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-v", __file__]))
