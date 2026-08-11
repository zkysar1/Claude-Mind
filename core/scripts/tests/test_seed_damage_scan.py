"""Tests for seed-damage-scan.py ().

The three constraints under test are not stylistic — each one alone is the
difference between 4,829 reported sites and 2 (rb-6267, measured on the
dev -> staging hop 2026-08-01). So each gets a POSITIVE test proving
it detects, and a NEGATIVE test proving it rejects the specific confound it
exists to survive. A scanner that only had the positives would pass while
reporting the version gap as damage, which is exactly the defect.

The fourth group is the one guard-1587 asks for: this tool's honest answer on a
healthy hop is 0, and a mistyped path also yields 0. Those must not be the same
output, so the empty-population branches are pinned individually.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "seed-damage-scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("seed_damage_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_damage_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


sds = _load()

# W1 is the real manifest rule: strips domain identifiers from COMMENTS in
# core/**/*.py. Using a real word keeps the fixtures honest — a synthetic
# vocabulary would not prove the manifest is being read.
W1_WORD = "Lambda"


def _pair(tmp_path, src_lines, dst_lines, rel="core/a.py"):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for root, lines in ((src, src_lines), (dst, dst_lines)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return src, dst


def _run(src, dst, manifest=None):
    return sds.run(Path(src), Path(dst), Path(manifest or sds.DEFAULT_MANIFEST))


# ── constraint 1: scope to the transform's own vocabulary ────────────────────

def test_damage_in_a_comment_is_detected(tmp_path):
    """POSITIVE: a vocabulary word purely deleted from a comment IS damage."""
    src, dst = _pair(tmp_path,
                     [f"x = 1  # uses {W1_WORD} to dispatch"],
                     ["x = 1  # uses to dispatch"])
    r = _run(src, dst)
    assert r["damage_count"] == 1, r
    assert r["damage_sites"][0]["word"] == W1_WORD
    assert r["damage_sites"][0]["rule"] == "W1"


def test_out_of_scope_path_is_not_scanned(tmp_path):
    """NEGATIVE: W1's applies_to is core/**/*.py — a docs/ file is out of scope.

    Without the applies_to filter the scan would flag every file in the repo
    that happens to contain a vocabulary word, which is how the naive scan
    reached 1,033 files.
    """
    src, dst = _pair(tmp_path,
                     [f"notes  # {W1_WORD} mentioned"],
                     ["notes  #  mentioned"], rel="docs/n.py")
    r = _run(src, dst)
    assert r["damage_count"] == 0
    assert r["files_in_transform_scope"] == 0


def test_a_word_outside_the_vocabulary_is_not_damage(tmp_path):
    """NEGATIVE: deleting a non-vocabulary word is ordinary divergence."""
    src, dst = _pair(tmp_path,
                     ["x = 1  # uses Kubernetes to dispatch"],
                     ["x = 1  # uses to dispatch"])
    assert _run(src, dst)["damage_count"] == 0


# ── constraint 2: the transform's own context predicate, imported ────────────

def test_word_in_code_not_comment_is_not_damage(tmp_path):
    """NEGATIVE: W1 is when_in_context=comment; the transform never touches code.

    This is the constraint a hand-rolled splitter gets wrong, which is why the
    module imports _check_context rather than re-deriving the boundary.
    """
    src, dst = _pair(tmp_path, [f'z = "{W1_WORD}"'], ['z = ""'])
    assert _run(src, dst)["damage_count"] == 0


def test_context_predicate_is_imported_from_the_transform():
    """The predicates must BE the transform's, not copies of them.

    Identity, not behaviour: a copy that currently agrees would drift silently
    the first time the transform's boundary moved (rb-6267 constraint 2).
    """
    import _seed_transforms as st
    assert sds._check_context is st._check_context
    assert sds._applies_to is st._applies_to


# ── constraint 3: pure deletion only ─────────────────────────────────────────

def test_substitution_is_not_damage(tmp_path):
    """NEGATIVE: the word is gone but content was ADDED — ordinary evolution.

    This is the confound that produced nearly all of the 4,829 false sites: a
    derived copy is legitimately BEHIND, so an upstream line that gained words
    makes the older downstream line look like a deletion.
    """
    src, dst = _pair(tmp_path,
                     [f"y = 2  # uses {W1_WORD} here"],
                     ["y = 2  # uses a managed runtime here"])
    assert _run(src, dst)["damage_count"] == 0


def test_is_pure_deletion_rejects_any_added_token():
    assert sds.is_pure_deletion("a b c", "a c") is True
    assert sds.is_pure_deletion("a b c", "a b c") is True
    assert sds.is_pure_deletion("a b c", "a d") is False
    assert sds.is_pure_deletion("a b c", "a b c d") is False


def test_whole_line_removal_is_a_version_gap_not_damage(tmp_path):
    """A deleted LINE is the frontier moving, not a word-strip.

    Only 'replace' opcodes are inspected. Counting 'delete' opcodes would make
    every file the dest is behind on report damage proportional to the gap.
    """
    src, dst = _pair(tmp_path,
                     ["keep = 0", f"gone = 1  # {W1_WORD} here", "keep2 = 2"],
                     ["keep = 0", "keep2 = 2"])
    assert _run(src, dst)["damage_count"] == 0


# ── guard-1587: a zero must never be reported without its denominator ────────

def test_clean_pair_reports_the_population_the_zero_came_from(tmp_path):
    src, dst = _pair(tmp_path, ["x = 1  # ordinary"], ["x = 1  # ordinary"])
    r = _run(src, dst)
    assert r["damage_count"] == 0
    assert sds.empty_population_reason(r) is None
    assert r["files_in_transform_scope"] >= 1
    assert r["vocabulary_size"] > 0


def test_no_shared_files_refuses_a_verdict(tmp_path):
    """A wrong --target yields a clean zero; it must exit 3, not 0."""
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "a.py").write_text("x = 1\n", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()
    r = _run(src, dst)
    assert r["files_in_both_trees"] == 0
    assert "BOTH trees" in (sds.empty_population_reason(r) or "")


def test_empty_vocabulary_refuses_a_verdict(tmp_path):
    """A manifest with no deletion rules cannot report damage — say so."""
    manifest = tmp_path / "m.yaml"
    manifest.write_text("transformations: []\n", encoding="utf-8")
    src, dst = _pair(tmp_path, ["x = 1"], ["x = 1"])
    r = _run(src, dst, manifest=manifest)
    assert r["rules_scanned"] == []
    assert "vocabulary is empty" in (sds.empty_population_reason(r) or "")


def test_vocabulary_is_read_from_the_manifest_not_hardcoded(tmp_path):
    """Swap the manifest and the detected word changes with it (constraint 1)."""
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "transformations:\n"
        "  - id: T9\n"
        "    type: word_list_strip\n"
        "    when_in_context: comment\n"
        "    applies_to: ['core/**/*.py']\n"
        "    words: ['Widgetron']\n", encoding="utf-8")
    src, dst = _pair(tmp_path,
                     ["x = 1  # uses Widgetron to dispatch"],
                     ["x = 1  # uses to dispatch"])
    r = _run(src, dst, manifest=manifest)
    assert r["damage_count"] == 1
    assert r["damage_sites"][0]["rule"] == "T9"
    # ...and the REAL manifest's word is inert under this one.
    src2, dst2 = _pair(tmp_path / "b",
                       [f"x = 1  # uses {W1_WORD} to dispatch"],
                       ["x = 1  # uses to dispatch"])
    assert _run(src2, dst2, manifest=manifest)["damage_count"] == 0


# ── exit codes, through the real CLI ─────────────────────────────────────────

@pytest.mark.parametrize("case,expected_rc", [("damage", 2), ("clean", 0), ("nopop", 3)])
def test_cli_exit_codes(tmp_path, case, expected_rc):
    """The rc IS the interface — a caller that only reads stdout cannot tell
    'clean' from 'could not look', which is the whole point of rc=3."""
    if case == "damage":
        src, dst = _pair(tmp_path, [f"x = 1  # {W1_WORD} here"], ["x = 1  #  here"])
    elif case == "clean":
        src, dst = _pair(tmp_path, ["x = 1  # ordinary"], ["x = 1  # ordinary"])
    else:
        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir(); dst.mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(src), "--target", str(dst), "--json"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == expected_rc, proc.stdout + proc.stderr
    if expected_rc != 3:
        payload = json.loads(proc.stdout)
        assert payload["verdict"] == ("damage-found" if expected_rc == 2 else "clean")
        # The denominator travels with every verdict, not just the clean one.
        for key in ("vocabulary_size", "files_in_transform_scope",
                    "line_pairs_compared", "source", "target"):
            assert key in payload
