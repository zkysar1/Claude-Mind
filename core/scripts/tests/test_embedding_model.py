"""Unit tests for _embedding_model () — backend selection, name
resolution, normalization, and bge query-instruction handling. Hermetic: fake
backend modules are injected into sys.modules; no real model ever loads."""
import sys
import types
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import _embedding_model as em


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_l2_normalize_rows_and_idempotence():
    m = em._l2_normalize([[3.0, 4.0], [1.0, 0.0]])
    assert np.allclose((m ** 2).sum(axis=1), 1.0)
    assert np.allclose(em._l2_normalize(m), m)  # idempotent on unit rows


def test_l2_normalize_zero_vector_guard():
    v = em._l2_normalize([0.0, 0.0])
    assert not np.isnan(v).any()


def test_fastembed_resolve_exact_prefixed_and_miss():
    supported = {"BAAI/bge-small-en-v1.5", "sentence-transformers/all-MiniLM-L6-v2"}
    assert em._fastembed_resolve("BAAI/bge-small-en-v1.5", supported) == \
        "BAAI/bge-small-en-v1.5"
    assert em._fastembed_resolve("all-MiniLM-L6-v2", supported) == \
        "sentence-transformers/all-MiniLM-L6-v2"
    assert em._fastembed_resolve("no-such-model", supported) is None


def test_is_bge():
    assert em._is_bge("BAAI/bge-small-en-v1.5")
    assert not em._is_bge("all-MiniLM-L6-v2")
    assert not em._is_bge(None)


# ── backend selection ────────────────────────────────────────────────────────

class _FakeST:
    """Records what encode() was called with; returns unit vectors."""

    def __init__(self, name):
        self.name = name
        self.seen = []

    def get_sentence_embedding_dimension(self):
        return 4

    def encode(self, texts, normalize_embeddings=True, batch_size=64,
               show_progress_bar=False):
        self.seen.extend(texts)
        return np.tile(np.array([1.0, 0, 0, 0], dtype="float32"),
                       (len(texts), 1))


def _install_fake_st(monkeypatch, registry):
    mod = types.ModuleType("sentence_transformers")

    def _ctor(name):
        inst = _FakeST(name)
        registry.append(inst)
        return inst

    mod.SentenceTransformer = _ctor
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)


def _block_fastembed(monkeypatch):
    mod = types.ModuleType("fastembed")  # import ok, attribute access fails

    class _Boom:
        @staticmethod
        def list_supported_models():
            raise RuntimeError("fastembed unavailable")

    mod.TextEmbedding = _Boom
    monkeypatch.setitem(sys.modules, "fastembed", mod)


def test_falls_back_to_sentence_transformers_when_fastembed_unavailable(monkeypatch):
    registry = []
    _block_fastembed(monkeypatch)
    _install_fake_st(monkeypatch, registry)
    enc, backend = em.load_encoder("all-MiniLM-L6-v2")
    assert backend == "sentence-transformers"
    assert isinstance(enc, em._STEncoder)


def test_falls_back_when_model_not_in_fastembed_registry(monkeypatch):
    fe = types.ModuleType("fastembed")

    class _TE:
        @staticmethod
        def list_supported_models():
            return [{"model": "some/other-model"}]

    fe.TextEmbedding = _TE
    monkeypatch.setitem(sys.modules, "fastembed", fe)
    registry = []
    _install_fake_st(monkeypatch, registry)
    enc, backend = em.load_encoder("all-MiniLM-L6-v2")
    assert backend == "sentence-transformers"


# ── query instruction handling (sentence-transformers path) ─────────────────

def test_st_encoder_applies_bge_query_instruction(monkeypatch):
    registry = []
    _install_fake_st(monkeypatch, registry)
    enc = em._STEncoder(_FakeST("BAAI/bge-small-en-v1.5"),
                        "BAAI/bge-small-en-v1.5")
    enc.encode_query("find the lease bug")
    assert enc._m.seen == [em._BGE_QUERY_INSTRUCTION + "find the lease bug"]


def test_st_encoder_plain_query_for_symmetric_models():
    enc = em._STEncoder(_FakeST("all-MiniLM-L6-v2"), "all-MiniLM-L6-v2")
    enc.encode_query("find the lease bug")
    assert enc._m.seen == ["find the lease bug"]


def test_st_encoder_docs_never_get_instruction():
    enc = em._STEncoder(_FakeST("BAAI/bge-small-en-v1.5"),
                        "BAAI/bge-small-en-v1.5")
    enc.encode_docs(["doc one", "doc two"])
    assert enc._m.seen == ["doc one", "doc two"]
