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
    table = kcc._tabulate(rows, ["k"])
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
    cell = kcc._tabulate(rows, ["k"])["a.py"]["k"]
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
    table = kcc._tabulate(rows, ["k"])
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
