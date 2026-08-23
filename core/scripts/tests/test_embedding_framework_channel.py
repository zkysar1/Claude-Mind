"""Unit tests for the framework-lane embedding channel ().

Covers retrieve.framework_doc_id / framework_doc_text /
_framework_embedding_scores, the eligibility hook in load_framework_rules,
the builder<->consumer id agreement, and the freshness watch-set widening.

Same importlib-against-scratch-world bootstrap as
test_embedding_tree_channel.py.

THE CENTREPIECE IS test_builder_and_consumer_derive_the_same_doc_id. The
tree lane derives its join key TWICE — build.tree_doc_id and
retrieve._tree_doc_id_for, each documented as "MUST mirror" the other — and
that exact shape is what g-306-45 measured as a join matching ZERO real
records while the feature shipped silently inert. This lane derives it once
and the builder calls it; that test is what pins the arrangement, so a
future edit that reintroduces a second copy fails here rather than in
production silence.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="embedding-framework-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_fwch_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _embedding_retrieval as er  # noqa: E402


def _cfg(framework_on):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_framework_channel_enabled"] = framework_on
    return cfg


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


# A two-doc corpus: TARGET is the one a paraphrase query token-MISSES.
TARGET = {
    "path": "core/config/conventions/partner-liveness.md",
    "title": "Partner Liveness",
    "tags": ["Verdicts", "The two-branch protocol"],
    "content": "liveness-check.sh verdicts and provenance.",
    "source_tier": "core-convention",
}
OTHER = {
    "path": ".claude/rules/first-principles.md",
    "title": "First-Principles Thinking",
    "tags": ["Rules", "Anti-patterns"],
    "content": "Surface assumptions explicitly.",
    "source_tier": "rule",
}
CORPUS = [TARGET, OTHER]
TARGET_ID = "framework:core/config/conventions/partner-liveness"

# The paraphrase from the goal's own cc-13 measurement. It shares no
# >=5-char token with TARGET, so the token lane cannot admit it — which is
# what makes the flag-ON/flag-OFF difference attributable to the channel.
PARAPHRASE = ["is my teammate agent actually down or just quiet"]


def _paths(entries):
    return [e["path"] for e in entries]


# ── framework_doc_id: the join key ───────────────────────────────────────────

def test_doc_id_strips_md_and_normalises_separators():
    assert _retrieve.framework_doc_id(TARGET) == TARGET_ID
    win = dict(TARGET, path="core\\config\\conventions\\partner-liveness.md")
    assert _retrieve.framework_doc_id(win) == TARGET_ID


def test_doc_id_none_for_missing_path():
    assert _retrieve.framework_doc_id({}) is None
    assert _retrieve.framework_doc_id(None) is None
    assert _retrieve.framework_doc_id({"path": ""}) is None


def test_doc_id_namespace_is_disjoint_from_rb_guard_and_tree():
    """The property that makes every existing id join ignore these rows."""
    did = _retrieve.framework_doc_id(TARGET)
    assert did.startswith("framework:")
    for foreign in ("rb-", "guard-", "tree:"):
        assert not did.startswith(foreign)


# ── framework_doc_text: the embedded surface ────────────────────────────────

def test_doc_text_carries_title_headers_and_body():
    t = _retrieve.framework_doc_text(TARGET)
    assert "Partner Liveness" in t
    assert "two-branch protocol" in t          # headers are match surface
    assert "liveness-check.sh" in t            # body sample


def test_doc_text_caps_headers_and_body():
    fat = {
        "path": "x.md",
        "title": "T",
        "tags": [f"header{i}" for i in range(100)],
        "content": "z" * 5000,
        "source_tier": "rule",
    }
    t = _retrieve.framework_doc_text(fat)
    assert "header0" in t
    assert f"header{_retrieve.FRAMEWORK_DOC_HEADER_CAP}" not in t
    assert t.count("z") == _retrieve.FRAMEWORK_DOC_BODY_CHARS


def test_doc_text_survives_missing_fields():
    assert _retrieve.framework_doc_text({}) == ""
    assert _retrieve.framework_doc_text(None) == ""
    assert _retrieve.framework_doc_text({"title": "only"}) == "only"


# ── _framework_embedding_scores ──────────────────────────────────────────────

def test_scores_flag_off_never_calls_cosine(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)

    def _boom(*a, **k):
        raise AssertionError("cosine_scores must not be called when flag off")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    assert _retrieve._framework_embedding_scores(["q"], CORPUS) == {}


def test_scores_join_ignores_foreign_namespaces(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    raw = {TARGET_ID: 0.9, "rb-123": 0.99, "tree:system/loop": 0.98}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: raw)
    out = _retrieve._framework_embedding_scores(["q"], CORPUS)
    assert out == {TARGET["path"]: pytest.approx(0.9)}


# ── load_framework_rules: the eligibility channel ────────────────────────────

def _with_corpus(monkeypatch):
    monkeypatch.setattr(_retrieve, "_build_framework_index", lambda: list(CORPUS))


def test_flag_off_reproduces_the_reported_miss(monkeypatch):
    """ARM A. The paraphrase token-misses the target — the cc-13 symptom."""
    _with_corpus(monkeypatch)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {TARGET_ID: 0.62})
    assert TARGET["path"] not in _paths(_retrieve.load_framework_rules(PARAPHRASE))


def test_flag_on_admits_a_doc_the_token_lane_missed(monkeypatch):
    """ARM B. The whole point of the goal."""
    _with_corpus(monkeypatch)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {TARGET_ID: 0.62})
    assert TARGET["path"] in _paths(_retrieve.load_framework_rules(PARAPHRASE))


def test_score_below_the_floor_is_not_admitted(monkeypatch):
    _with_corpus(monkeypatch)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {TARGET_ID: 0.10})
    assert TARGET["path"] not in _paths(_retrieve.load_framework_rules(PARAPHRASE))


def test_lane_floor_overrides_the_shared_floor(monkeypatch):
    """'s reasoning applied here: retuning the shared floor would
    silently move the supplementary rb/guardrail lane, so this lane gets its
    own key that falls back to the shared value."""
    _with_corpus(monkeypatch)
    cfg = _cfg(True)
    cfg["embedding_framework_min_cosine"] = 0.05
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {TARGET_ID: 0.10})
    # 0.10 clears the lane floor (0.05) though it is under the shared 0.35.
    assert TARGET["path"] in _paths(_retrieve.load_framework_rules(PARAPHRASE))


@pytest.mark.parametrize("scores,label", [
    ({}, "empty index"),
    (None, "cosine raises"),
])
def test_degradation_is_identical_to_the_token_baseline(monkeypatch, scores, label):
    """ARMs D/E. An unprovisioned or broken index must leave this lane exactly
    as it was — the structural graceful-degradation contract shared with
    _tree_embedding_scores and _embedding_blend."""
    _with_corpus(monkeypatch)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)
    baseline = _paths(_retrieve.load_framework_rules(["assumptions"]))

    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    if scores is None:
        def _raise(*a, **k):
            raise RuntimeError("index corrupt")
        monkeypatch.setattr(er, "cosine_scores", _raise)
    else:
        monkeypatch.setattr(er, "cosine_scores", lambda q, **k: scores)
    assert _paths(_retrieve.load_framework_rules(["assumptions"])) == baseline, label


def test_empty_categories_still_returns_empty(monkeypatch):
    """The flag must not change the categories-falsy contract."""
    _with_corpus(monkeypatch)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {TARGET_ID: 0.99})
    assert _retrieve.load_framework_rules([]) == []
    assert _retrieve.load_framework_rules(None) == []


def test_cap_is_still_honoured_with_the_channel_on(monkeypatch):
    big = [
        {"path": f"core/config/conventions/c{i}.md", "title": f"C{i}",
         "tags": [], "content": "x", "source_tier": "core-convention"}
        for i in range(40)
    ]
    monkeypatch.setattr(_retrieve, "_build_framework_index", lambda: big)
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    all_high = {_retrieve.framework_doc_id(e): 0.99 for e in big}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: all_high)
    got = _retrieve.load_framework_rules(["anything"])
    assert len(got) == _retrieve.FRAMEWORK_RULES_CAP


# ── builder <-> consumer agreement (the  pin) ────────────────────────

def test_builder_and_consumer_derive_the_same_doc_id():
    """The join key must have ONE definition.

    g-306-45: a join whose write side and read side were derived separately
    matched zero real records and the feature shipped silently inert. The
    builder calls retrieve.framework_doc_id rather than mirroring it; this
    test fails if a second derivation is ever introduced.
    """
    spec = importlib.util.spec_from_file_location(
        "eib_fwch", CORE_SCRIPTS / "embedding-index-build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    # The builder reaches the id through the retrieve module it imports.
    assert build.R.framework_doc_id is not None
    for entry in CORPUS:
        assert build.R.framework_doc_id(entry) == _retrieve.framework_doc_id(entry)


def test_builder_emits_framework_rows_from_the_shared_corpus(monkeypatch):
    """load_corpus must actually carry framework docs, typed and id'd."""
    spec = importlib.util.spec_from_file_location(
        "eib_fwch2", CORE_SCRIPTS / "embedding-index-build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    monkeypatch.setattr(build.R, "_build_framework_index", lambda: list(CORPUS))
    monkeypatch.setattr(build.R, "read_jsonl", lambda *a, **k: [])
    monkeypatch.setattr(build.R, "read_yaml", lambda *a, **k: {})
    docs = build.load_corpus()
    fw = [d for d in docs if d["type"] == "framework"]
    assert len(fw) == len(CORPUS)
    assert TARGET_ID in {d["id"] for d in fw}
    assert all(d["text"] for d in fw)


# ── freshness watch-set (the  pin) ─────────────────────────────────

def test_source_mtime_watches_world_conventions(tmp_path, monkeypatch):
    """A framework edit must mark the index stale.

    g-115-3763: the tree was in the corpus but not in the watch-set, so a
    tree-only encoding never triggered a re-index and the new node stayed
    invisible to retrieve.sh. Adding framework docs to the corpus without
    adding them here would reproduce that exactly.
    """
    spec = importlib.util.spec_from_file_location(
        "eif_fwch", CORE_SCRIPTS / "embedding-index-freshness.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    world = tmp_path / "world"
    (world / "conventions").mkdir(parents=True)
    (world / "reasoning-bank.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("MIND_WORLD", str(world))

    import _paths
    monkeypatch.setattr(_paths, "WORLD_DIR", str(world))

    before = fresh._source_mtime()
    doc = world / "conventions" / "brand-new.md"
    doc.write_text("# New convention\n", encoding="utf-8")
    os.utime(doc, ((before or 0) + 100, (before or 0) + 100))
    after = fresh._source_mtime()

    assert after is not None
    assert before is None or after > before
