"""test_key_consumer_census.py — regression tests for  (gap-029).

`key-consumer-census.py` mechanizes "Rule 1 — census before edit" from the
knowledge-tree node system/system-constraints-loop/producer-consumer-key-drift.md:
given a field key, tabulate every WRITER against every READER so the MINORITY
spelling (the deviant) is visible instead of guessed.

WHAT THESE TESTS PIN, and why each one exists — every case below is a defect
that was live during development and caught by running the tool against a
DOCUMENTED ground truth rather than against its author's expectations:

  1. ALIAS detection + precedence. `{"reason": "why"}` is an alias map, the
     single most important line type in a census: it is where two spellings get
     reconciled. The first implementation classified that line as WRITE(reason)
     and saw `why` not at all — so the `blocker_ref` census reported `reason`
     with 8 writers and `why` with none, while the STORED record demonstrably
     used `why`. The transform was in scope and read the whole time
     (gates/blocker_ref.py). A census blind to alias maps answers the easy half
     of the question and silently drops the half that matters.

  2. ALIAS must BEAT WRITE. The same line matches the dict-literal WRITE pattern,
     so ordering is load-bearing, not incidental.

  3. Word boundaries. `reason` must not match `failure_reason` — those are the
     two rival spellings in the originating incident (g-115-3348), so a census
     that conflates them cannot answer the question it was invoked for.

  4. --scope. An unscoped census of a generic key is useless, not merely noisy:
     measured on the first ground-truth run, `blocker_id detected_at reason`
     returned 3,781 hits across 732 files and buried the ~8-participant table.
     A census is always about a STRUCTURE, never a bare word.

  5. Narration excluded by default. Any key that has ever been logged has
     thousands of changelog/journal hits; including them re-creates the
     always-drowned failure mode from a different direction.

Written pytest-collectable ON PURPOSE: 69 files in this directory are
main()-style, from which pytest collects ZERO tests, so they never run in the
mandated sweep (the g-115-2349 invisible-suite class, which left 9 silent reds).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def _load():
    path = SCRIPT_DIR / "key-consumer-census.py"
    spec = importlib.util.spec_from_file_location("_kcc", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_kcc"] = mod
    spec.loader.exec_module(mod)
    return mod


kcc = _load()


# ------------------------------------------------------------------ classify

def test_alias_map_value_is_detected():
    """`"reason": "why",` marks `why` as the ALIAS target (the canonical name)."""
    assert kcc.classify('    "reason":             "why",', "why") == "ALIAS"


def test_alias_beats_write_on_the_same_line():
    """Ordering is load-bearing: the line also matches WRITE for the LEFT key.

    If WRITE were checked first for the right-hand key the canonical target
    would degrade to MENTION and the asymmetry would stay unexplained.
    """
    line = '    "reason": "why",'
    assert kcc.classify(line, "why") == "ALIAS"
    # The left-hand (deprecated) spelling still reads as a WRITE — it is a real
    # dict key. That asymmetry is the point: one side is aliased, one is written.
    assert kcc.classify(line, "reason") == "WRITE"


@pytest.mark.parametrize("line,expected", [
    ('    "blocker_id": rec.get("id"),', "WRITE"),
    ('blocker_id: value', "WRITE"),
    ('    d["blocker_id"] = 1', "WRITE"),
    ('    x = rec.get("blocker_id")', "READ"),
    ('    if "blocker_id" in rec:', "READ"),
    ('# the blocker_id is documented in the schema', "MENTION"),
])
def test_role_classification(line, expected):
    assert kcc.classify(line, "blocker_id") == expected


def test_word_boundary_reason_vs_failure_reason(tmp_path):
    """`reason` must not match inside `failure_reason` — they are rival keys.

    This is the exact pair from g-115-3348 (create-blocker.py emitted
    failure_reason where the schema said reason), so conflating them would make
    the census unable to answer its originating question.
    """
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "a.py").write_text(
        'x = {"failure_reason": 1}\ny = {"reason": 2}\n', encoding="utf-8"
    )
    rows = kcc.census(["reason"], project_root=root)
    texts = [r["text"] for r in rows]
    assert any('"reason": 2' in t for t in texts), "canonical key missed"
    assert not any("failure_reason" in t for t in texts), (
        f"word boundary leaked into failure_reason: {texts}"
    )


# ---------------------------------------------------------------- scope/filter

def test_scope_restricts_to_files_mentioning_the_structure(tmp_path):
    """--scope is what makes a generic-key census usable at all."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "in_scope.py").write_text(
        'known_blockers = []\nrec = {"reason": 1}\n', encoding="utf-8")
    (root / "core" / "out_of_scope.py").write_text(
        'other = {"reason": 1}\n', encoding="utf-8")

    unscoped = kcc.census(["reason"], project_root=root)
    scoped = kcc.census(["reason"], project_root=root, scope="known_blockers")

    unscoped_files = {r["file"] for r in unscoped}
    scoped_files = {r["file"] for r in scoped}
    assert len(unscoped_files) == 2, f"expected both files unscoped, got {unscoped_files}"
    assert len(scoped_files) == 1, f"scope did not restrict: {scoped_files}"
    assert any("in_scope" in f for f in scoped_files)


def test_narration_excluded_by_default(tmp_path):
    """changelog.jsonl et al are append-only narration, not participants."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "changelog.jsonl").write_text(
        '{"reason": "did a thing"}\n', encoding="utf-8")
    (root / "core" / "live.py").write_text('x = {"reason": 1}\n', encoding="utf-8")

    default = kcc.census(["reason"], project_root=root)
    assert all("changelog" not in r["file"] for r in default), (
        "narration leaked into the default census — any key that has ever been "
        "logged would drown the table"
    )

    widened = kcc.census(["reason"], project_root=root, include_narration=True)
    assert any("changelog" in r["file"] for r in widened), (
        "--include-narration did not widen the scan"
    )


def test_imports_shared_iteration_rather_than_copying():
    """The root-relative classification contract must come from ONE place.

    goal-reference-scan.py's is_historical/_is_scannable take a ROOT-RELATIVE
    path for a hard-won reason (g-115-3096): matching dir names against an
    ABSOLUTE path tests every ancestor of the repo, so a checkout under a dir
    named temp/ or logs/ classifies every file as narration and the scan
    reports a vacuous clean result. Copying the loop risks re-deriving that
    subtly wrong, so the census imports it — this pins that it still does.
    """
    assert hasattr(kcc, "_refscan"), "census no longer imports goal-reference-scan"
    for fn in ("is_historical", "_is_scannable", "_iter_targets", "_rel"):
        assert hasattr(kcc._refscan, fn), f"shared helper {fn} missing"


# ------------------------------------------------- per-file site multiplicity
# . The census table was set-valued, so a file with four write sites
# rendered identically to a file with one. That is the right unit for the
# SPELLING question ("which spelling is the minority") and the wrong one for
# the SITE-ENUMERATION question ("have I found every place to edit") — and the
# second is how the table gets read in practice.
#
# Measured mechanism (hypothesis 2026-07-28_side-observation-goals-under-
# enumerate-sites, CONFIRMED by alpha via ): a goal named 9 sites, the
# census found 15, and the miss was NOT a file escaping the scan. The file was
# scanned, classified by the FIRST variant found, and never re-examined for
# others. Only per-file multiplicity can represent that.

def test_tabulate_counts_sites_not_just_roles():
    """Four WRITEs in one file must count 4, not collapse to a single role."""
    rows = [
        {"file": "a.py", "line": i, "key": "k", "role": "WRITE",
         "narration": False, "text": ""}
        for i in (10, 20, 30, 40)
    ]
    rows.append({"file": "b.py", "line": 5, "key": "k", "role": "WRITE",
                 "narration": False, "text": ""})
    table = kcc._tabulate(rows)
    assert table["a.py"]["k"]["WRITE"] == 4
    assert table["b.py"]["k"]["WRITE"] == 1
    # The pre-fix shape could not tell these two files apart at all.
    assert table["a.py"]["k"] != table["b.py"]["k"]


def test_membership_tests_still_read_role_names():
    """Counter is a dict subclass — every `"WRITE" in cell` check is unaffected.

    This is what let the change stay surgical: main() tests role MEMBERSHIP in
    five places and none of them needed touching.
    """
    rows = [{"file": "a.py", "line": 1, "key": "k", "role": "WRITE",
             "narration": False, "text": ""}]
    cell = kcc._tabulate(rows)["a.py"]["k"]
    assert "WRITE" in cell
    assert "READ" not in cell


def test_mention_only_detection_survives_the_type_change():
    """The one site that compared against a set — `== {"MENTION"}` — must still
    identify a mention-only file. A Counter never equals a set, so leaving that
    comparison unconverted would have silently zeroed the mention-only column
    (guard-1696: a fix that changes what an assertion reads must move it in the
    same change)."""
    rows = [{"file": "m.py", "line": 1, "key": "k", "role": "MENTION",
             "narration": False, "text": ""},
            {"file": "m.py", "line": 2, "key": "k", "role": "MENTION",
             "narration": False, "text": ""},
            {"file": "w.py", "line": 1, "key": "k", "role": "WRITE",
             "narration": False, "text": ""}]
    table = kcc._tabulate(rows)
    mention_only = [f for f in table if set(table[f].get("k", ())) == {"MENTION"}]
    assert mention_only == ["m.py"], mention_only


def test_fmt_roles_shows_multiplicity_only_when_it_exists():
    """`WRITE x4` when it matters, bare `WRITE` when it does not — so the common
    single-site case reads exactly as it did before the change."""
    from collections import Counter
    assert kcc._fmt_roles(Counter({"WRITE": 4})) == "WRITE x4"
    assert kcc._fmt_roles(Counter({"WRITE": 1})) == "WRITE"
    assert kcc._fmt_roles(Counter({"WRITE": 2, "READ": 1})) == "READ/WRITE x2"
    assert kcc._fmt_roles(None) == "-"
    assert kcc._fmt_roles(Counter()) == "-"
    # Tolerates the old set shape rather than raising on it.
    assert kcc._fmt_roles({"READ", "WRITE"}) == "READ/WRITE"


# ---------------------------------------------------------------------------
# main() output — . F1 (a docstring-promised warning that was never
# implemented) is exactly the class ONE main()-output assertion catches, and
# this file had zero coverage of main(), which is why it shipped.
# ---------------------------------------------------------------------------

def _run_main(kcc, capsys, argv):
    old = sys.argv
    sys.argv = ["key-consumer-census.py", *argv]
    try:
        rc = kcc.main()
    finally:
        sys.argv = old
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_main_warns_when_unscoped_result_is_too_large(tmp_path, capsys):
    """The no-scope hit-count warning census()'s docstring promises."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    for i in range(60):
        (root / "core" / f"f{i}.py").write_text('x = {"reason": 1}\n', encoding="utf-8")
    rc, out, err = _run_main(kcc, capsys, ["reason", "--project-root", str(root)])
    assert rc == 0
    assert "WARNING" in err and "--scope" in err, err
    assert "60" in err, err


def test_main_does_not_warn_when_scoped(tmp_path, capsys):
    """Positive control: the warning is conditional, not unconditional.

    Without this, the assertion above passes against a tool that warns always —
    a different defect wearing the same output.
    """
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    for i in range(60):
        (root / "core" / f"f{i}.py").write_text(
            'known_blockers = {"reason": 1}\n', encoding="utf-8")
    rc, out, err = _run_main(
        kcc, capsys,
        ["reason", "--scope", "known_blockers", "--project-root", str(root)])
    assert rc == 0
    assert "--scope" not in err, err


def test_main_does_not_warn_on_a_small_unscoped_result(tmp_path, capsys):
    """Second positive control: below both thresholds, no warning."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "a.py").write_text('x = {"reason": 1}\n', encoding="utf-8")
    rc, out, err = _run_main(kcc, capsys, ["reason", "--project-root", str(root)])
    assert rc == 0
    assert "WARNING" not in err, err


def test_census_reports_unreadable_files(tmp_path, capsys):
    """F2: an unreadable file is COUNTED and reported, never silently dropped."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "ok.py").write_text('x = {"reason": 1}\n', encoding="utf-8")
    (root / "core" / "bad.py").write_text('x = {"reason": 1}\n', encoding="utf-8")
    real = kcc.Path.read_text

    def _boom(self, *a, **k):
        if self.name == "bad.py":
            raise OSError("simulated unreadable file")
        return real(self, *a, **k)

    kcc.Path.read_text = _boom
    try:
        rows = kcc.census(["reason"], project_root=str(root))
    finally:
        kcc.Path.read_text = real
    err = capsys.readouterr().err
    assert "1 file(s) were unreadable" in err, err
    assert "lower bound" in err, err
    assert any(r["file"].endswith("ok.py") for r in rows), rows
# ------------------------------------------------------------------- main()
# main() is the tool's ONLY output surface — header, separator, per-file rows,
# totals, alias-map block, decision-rule footer. Until  nothing
# invoked it, so census() being green said nothing about whether the census
# PRINTS anything usable. These two tests give it its first coverage.

def _wide_cell_root(tmp_path):
    """A tmp project whose single file renders a cell WIDER than the legacy
    14-char pad (`READ x3/WRITE x4`, 16 chars).

    The width matters, not the exact roles: at <=14 chars the fixed pad and the
    derived pad agree, so an alignment assertion built on a narrow cell passes
    identically against the bug and against the fix. The non-vacuity guard in
    the test below pins that property rather than trusting this docstring.
    """
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "core" / "wide.py").write_text(
        'rec = {"blocker_ref": 1}\n'
        'rec2 = {"blocker_ref": 2}\n'
        'rec3 = {"blocker_ref": 3}\n'
        'rec4 = {"blocker_ref": 4}\n'
        'a = g.get("blocker_ref")\n'
        'b = g.get("blocker_ref")\n'
        'c = g["blocker_ref"]\n',
        encoding="utf-8",
    )
    return root


def test_main_keeps_columns_aligned_when_a_cell_exceeds_the_legacy_pad(
        tmp_path, monkeypatch, capsys):
    """Every data row renders to the same width as the header row.

    Pins the g-115-3611 fix at key-consumer-census.py:351-355 (derive the key
    column width from the widest rendered cell, clamped to 34). Reverting that
    to a literal `kw = 14` re-widens nothing while the cells stay 16 chars, so
    the data rows overflow past the header and this assertion fails.

    The invariant is deliberately shape-based rather than content-based: it
    predicts no cell text, so it keeps holding as role spellings evolve.
    """
    root = _wide_cell_root(tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["key-consumer-census.py", "blocker_ref", "--project-root", str(root)])
    assert kcc.main() == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    sep = [i for i, ln in enumerate(lines) if set(ln.strip()) == {"-"}]
    assert sep, f"no separator row — main() printed no table:\n{out}"
    header = lines[sep[0] - 1]
    # Data rows run from the separator to the first blank line. Do NOT filter on
    # the root path: the participant column is clamped to 62 chars, so a tmp_path
    # root is truncated out of its own row.
    data = []
    for ln in lines[sep[0] + 1:]:
        if not ln.strip():
            break
        data.append(ln)
    assert data, f"no data rows under the separator:\n{out}"

    # NON-VACUITY: the fixture must actually exercise the derivation. If the
    # widest cell ever falls back to <=14 this test would pass against the bug
    # too, so fail loudly here rather than silently going green for free.
    rows = kcc.census(["blocker_ref"], project_root=root)
    table = kcc._tabulate(rows)
    widest = max(len(kcc._fmt_roles(v.get("blocker_ref"))) for v in table.values())
    assert widest > 14, (
        f"fixture no longer exercises the width derivation: widest cell is "
        f"{widest} chars, which the legacy fixed pad of 14 already covers")

    # Compare RAW widths, never rstrip()ed ones. Both rows pad every column to
    # the same width, so they are equal as emitted — but the header's final cell
    # is a short key name padded out with spaces while the data cell fills its
    # column exactly, so rstrip() shortens the header and not the row and the
    # comparison fails on correct output. Measured while writing this test.
    for ln in data:
        assert len(ln) == len(header), (
            f"column overflow: data row is {len(ln)} chars vs header "
            f"{len(header)} — the key column stopped sizing to its widest "
            f"cell.\nheader: {header!r}\nrow:    {ln!r}")


def test_main_prints_the_operator_surface(tmp_path, monkeypatch, capsys):
    """main() emits the lines an operator actually reads.

    Coverage-first companion to the alignment test: asserts the surface exists
    (title, hit summary, separator, a row naming the file) without pinning
    wording, so it fails if main() stops printing rather than on a reword.
    """
    root = _wide_cell_root(tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["key-consumer-census.py", "blocker_ref", "--project-root", str(root)])
    assert kcc.main() == 0
    out = capsys.readouterr().out

    assert "key-consumer census" in out, f"no title line:\n{out}"
    assert "blocker_ref" in out, f"censused key never named:\n{out}"
    assert "live hits across" in out, f"no hit summary:\n{out}"
    assert any(set(ln.strip()) == {"-"} for ln in out.splitlines()), (
        f"no separator row:\n{out}")
    # NOT the basename: the participant column is clamped to 62 chars, so a
    # tmp_path root truncates its own filename away. Assert a row rendered.
    lines = out.splitlines()
    sep = [i for i, ln in enumerate(lines) if set(ln.strip()) == {"-"}]
    assert sep and sep[0] + 1 < len(lines) and lines[sep[0] + 1].strip(), (
        f"separator printed but no participant row beneath it:\n{out}")
