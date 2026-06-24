"""Tests for mdl_gate.py (Phase 1c — encode-time parsimony)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mdl_gate as mg  # noqa: E402


def test_tokenize_drops_short_tokens_and_lowercases():
    toks = mg.tokenize("The QUICK a fox, jumped of!")
    assert "quick" in toks and "fox" in toks and "jumped" in toks
    # length filter is >= 3, so 1-2 char tokens drop; 3+ char tokens (incl "the") stay
    assert "a" not in toks and "of" not in toks
    assert "the" in toks


def test_jaccard_bounds():
    assert mg.jaccard(frozenset(), frozenset()) == 0.0
    assert mg.jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
    assert mg.jaccard(frozenset({"aaa", "bbb"}), frozenset({"aaa", "ccc"})) == pytest.approx(1 / 3)


def test_nearest_empty_corpus():
    assert mg.nearest("anything here", []) == (None, 0.0, "")


def test_nearest_picks_most_similar_and_returns_text():
    existing = [("rb-1", "windows subprocess bash path resolution issue"),
                ("rb-2", "knowledge tree node confidence rollup propagation")]
    nid, sim, text = mg.nearest("windows subprocess bash path resolution problem", existing)
    assert nid == "rb-1" and sim > 0.5 and "windows" in text


def test_nearest_returns_matched_text_not_duplicate_id_lookup():
    # Two entries share id 'rb-1'; nearest must return the text it actually matched
    # (the first, sim=1.0), not a last-wins dict() re-lookup of the wrong text.
    existing = [("rb-1", "alpha beta gamma"), ("rb-1", "totally different words here")]
    nid, sim, text = mg.nearest("alpha beta gamma", existing)
    assert nid == "rb-1" and sim == 1.0 and text == "alpha beta gamma"


def test_novelty_subset_is_zero():
    # candidate tokens all present in neighbour -> novelty 0 (pure paraphrase/subset)
    assert mg.novelty("bash path", "windows bash path resolution") == 0.0
    # entirely new vocabulary -> 1.0
    assert mg.novelty("kangaroo platypus", "windows bash path") == 1.0


def test_assess_keeps_when_no_existing():
    a = mg.assess("a brand new lesson", [])
    assert a.keep is True and a.nearest_id is None


def test_assess_drops_near_duplicate():
    existing = [("rb-1", "before trusting a probe failure verify the probe default port "
                          "matches the running service constant")]
    cand = "before trusting a probe failure verify the probe default port matches the " \
           "running service constant value"
    a = mg.assess(cand, existing, dup_threshold=0.8)
    assert a.keep is False and a.nearest_id == "rb-1" and "near-duplicate" in a.reason


def test_assess_drops_low_novelty_subset():
    existing = [("rb-1", "windows subprocess bash path resolution silent partial output fix")]
    cand = "bash path resolution output"  # subset -> low novelty, but similarity below dup
    a = mg.assess(cand, existing, dup_threshold=0.95, min_novelty=0.5)
    assert a.keep is False and "novelty" in a.reason


def test_assess_keeps_novel_entry_via_novelty_path():
    # Shares SOME tokens with rb-1 (so it goes through the novelty branch, not the
    # zero-overlap shortcut) but adds substantial new information -> keep.
    existing = [("rb-1", "windows subprocess bash path resolution silent output")]
    cand = "windows subprocess spawn leaks a child process handle on early timeout abort"
    a = mg.assess(cand, existing, dup_threshold=0.8, min_novelty=0.2)
    assert a.keep is True and a.nearest_id == "rb-1" and a.novelty >= 0.2


def test_assess_drops_empty_or_stopword_only_candidate():
    # HIGH fresh-eyes finding: an anti-bloat gate must DROP empty/garbage, not keep it.
    existing = [("rb-1", "some real existing lesson with content")]
    for junk in ("   ", "!!! ,,,", "a an of to"):
        a = mg.assess(junk, existing)
        assert a.keep is False and "low-content" in a.reason


def test_assess_disjoint_corpus_keeps_with_honest_reason():
    # HIGH fresh-eyes finding: non-empty corpus with zero overlap must NOT claim
    # "no existing entries" — it should say no overlap and keep as novel.
    existing = [("rb-1", "aaa bbb ccc"), ("rb-2", "ddd eee fff")]
    a = mg.assess("zzz www qqq", existing)
    assert a.keep is True and a.nearest_id is None and "no token overlap" in a.reason


def test_assess_rejects_bad_thresholds():
    with pytest.raises(ValueError):
        mg.assess("x", [], dup_threshold=1.5)


def test_cli(tmp_path, capsys):
    existing = tmp_path / "e.jsonl"
    existing.write_text(json.dumps({"id": "rb-1", "text": "alpha beta gamma delta epsilon"}),
                        encoding="utf-8")
    rc = mg.main(["--candidate", "alpha beta gamma delta epsilon zeta",
                  "--existing", str(existing), "--dup-threshold", "0.8"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["keep"] is False  # near-duplicate -> drop -> exit 1
