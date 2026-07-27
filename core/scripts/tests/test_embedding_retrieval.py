"""Fast unit tests for _embedding_retrieval ( part b1).

No real model load: a synthetic index + a stub model exercise the query-side
helper's logic (graceful degradation, cosine math, mtime-gated caching,
desync-truncation) in milliseconds. End-to-end validation against the real
all-MiniLM-L6-v2 model is done interactively during development, not in the
hot suite.
"""
import json

import pytest

# : importorskip -> clean module SKIP on a numpy-less box instead of a
# collection ERROR that aborts the whole `pytest core/scripts/tests` run. Also
# guards `import _embedding_retrieval` below (it imports numpy transitively).
np = pytest.importorskip("numpy")

import _embedding_retrieval as er


def _write(d, arr, ids):
    """Write a synthetic index (embeddings.npy float16 + meta.json). `arr` rows
    need NOT equal len(ids) — the desync test relies on a deliberate mismatch."""
    d.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(arr, dtype="float16")
    np.save(d / "embeddings.npy", arr)  # path already ends .npy -> no double suffix
    dim = int(arr.shape[1]) if arr.ndim == 2 and arr.shape[0] else 384
    meta = {"model": er.MODEL_NAME, "dim": dim, "count": len(ids),
            "docs": [{"id": i, "type": "guardrail", "hash": "h"} for i in ids]}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


class _StubModel:
    """Stands in for an encoder — encode() returns a fixed unit vector.
    Exposes only the legacy .encode surface, deliberately: cosine_scores
    must keep working with encoders lacking encode_query (g-306-82 shim)."""

    def __init__(self, vec):
        self._v = np.asarray(vec, dtype="float32")

    def encode(self, queries, normalize_embeddings=True, show_progress_bar=False):
        return self._v.reshape(1, -1)


def test_missing_index_returns_empty(tmp_path):
    er.clear_caches()
    assert er.cosine_scores("anything", index_dir=tmp_path / "nope") == {}
    assert er.index_available(index_dir=tmp_path / "nope") is False


def test_empty_or_bad_query_returns_empty(tmp_path):
    er.clear_caches()
    _write(tmp_path, [[1, 0, 0, 0]], ["guard-1"])
    assert er.cosine_scores("", index_dir=tmp_path) == {}
    assert er.cosine_scores(None, index_dir=tmp_path) == {}
    assert er.cosine_scores(123, index_dir=tmp_path) == {}


def test_cosine_math_with_stub(tmp_path, monkeypatch):
    er.clear_caches()
    # A parallel to the query (score 1), B orthogonal (0), C at cos=0.6.
    _write(tmp_path, [[1, 0, 0, 0], [0, 1, 0, 0], [0.6, 0.8, 0, 0]], ["A", "B", "C"])
    monkeypatch.setattr(er, "_get_model", lambda *a, **k: _StubModel([1, 0, 0, 0]))
    s = er.cosine_scores("q", index_dir=tmp_path)
    assert set(s) == {"A", "B", "C"}
    assert s["A"] == pytest.approx(1.0, abs=1e-2)
    assert s["B"] == pytest.approx(0.0, abs=1e-2)
    assert s["C"] == pytest.approx(0.6, abs=1e-2)
    # The most-similar doc is A (the ranking property the hybrid relies on).
    assert max(s, key=s.get) == "A"


def test_index_caching_populates_and_reuses(tmp_path, monkeypatch):
    er.clear_caches()
    _write(tmp_path, [[1, 0, 0, 0]], ["A"])
    monkeypatch.setattr(er, "_get_model", lambda *a, **k: _StubModel([1, 0, 0, 0]))
    assert str(tmp_path) not in er._index_cache
    er.cosine_scores("q", index_dir=tmp_path)
    assert str(tmp_path) in er._index_cache  # cached after first load
    emb1, ids1, _m1 = er._load_index(tmp_path)
    emb2, ids2, _m2 = er._load_index(tmp_path)
    assert emb1 is emb2 and ids1 is ids2  # same objects -> served from cache


def test_index_cache_invalidates_on_mtime(tmp_path, monkeypatch):
    er.clear_caches()
    _write(tmp_path, [[1, 0, 0, 0]], ["A"])
    emb1, _, _m = er._load_index(tmp_path)
    # Rewrite with a different corpus + bump mtime; cache must refresh.
    import os
    st = (tmp_path / "embeddings.npy").stat()
    _write(tmp_path, [[0, 1, 0, 0], [1, 0, 0, 0]], ["B", "C"])
    os.utime(tmp_path / "meta.json", (st.st_atime + 10, st.st_mtime + 10))
    os.utime(tmp_path / "embeddings.npy", (st.st_atime + 10, st.st_mtime + 10))
    emb2, ids2, _m2 = er._load_index(tmp_path)
    assert ids2 == ["B", "C"]
    assert emb2.shape[0] == 2


def test_desync_truncates_to_shorter(tmp_path, monkeypatch):
    er.clear_caches()
    # 2 embedding rows but 3 ids -> the extra id is dropped, no IndexError.
    _write(tmp_path, [[1, 0], [0, 1]], ["A", "B", "C"])
    monkeypatch.setattr(er, "_get_model", lambda *a, **k: _StubModel([1, 0]))
    s = er.cosine_scores("q", index_dir=tmp_path)
    assert set(s) == {"A", "B"}


def test_model_error_degrades_to_empty(tmp_path, monkeypatch):
    er.clear_caches()
    _write(tmp_path, [[1, 0, 0, 0]], ["A"])

    def _boom(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(er, "_get_model", _boom)
    # Must NOT raise into the retrieval hot path — degrades to {}.
    assert er.cosine_scores("q", index_dir=tmp_path) == {}
