"""_embedding_retrieval.py — query-side embedding retrieval for the retrieve.py
hybrid path (g-306-81 part b, unlocked by the g-306-77 A/B: embed hit@3 67% vs
token 13%).

Loads the persisted index built by embedding-index-build.py, embeds a query
OFFLINE, and returns {doc_id: cosine_score}. The hybrid loaders in retrieve.py
(_embedding_blend / _universal_relevance_split) union these scores with the
existing token-overlap matches and rank by cosine.

Model identity (g-306-82): the encoder is loaded for the model NAMED IN THE
INDEX's meta.json — the single source of truth written at build time — so the
query side can never embed with a different model than the one that produced
the matrix (which would silently corrupt every cosine). MODEL_NAME below is
only the fallback for legacy indexes whose meta lacks a model field. Backend
selection (fastembed ONNX preferred, sentence-transformers fallback) lives in
_embedding_model.load_encoder.

Two hard properties for the retrieval hot path:
  1. GRACEFUL DEGRADATION — cosine_scores NEVER raises. A missing/empty/corrupt
     index, an unavailable model, any error → returns {} so the caller falls back
     to pure token-overlap (the current behavior). The flag-OFF default plus this
     means the feature can never break retrieval.
  2. LAZY + CACHED — the ~2.5MB float16 matrix + id list load ONCE per process
     (module cache, mtime-invalidated), and each encoder loads ONCE per model
     name (never loaded at all when the hybrid flag is OFF, since cosine_scores
     is simply never called).
"""
import os
from pathlib import Path

# : put the per-box vendored encoder stack on sys.path. Defensive by
# design — this module's contract is that the retrieval path never raises, and a
# module-level ImportError would fire BEFORE cosine_scores' own guard exists and
# break every importer. On failure we simply keep the pre-vendor behavior: numpy
# stays unavailable, cosine_scores returns {}, callers fall back to token-overlap.
try:
    import _vendor_path  # noqa: F401
except Exception:  # pragma: no cover - sibling dir not importable
    pass

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INDEX_DIR = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"
MODEL_NAME = "all-MiniLM-L6-v2"  # fallback for meta.json without a model field

# Test seam (). A caller that does NOT pass index_dir gets the real
# per-box index — correct in production, a HERMETICITY HOLE under test: a test
# can redirect GUARD_PATH/RB_PATH to a tmp store, but retrieve.py's
# `_embedding_blend` calls `cosine_scores(query)` with no index_dir, so the
# widen pass scores tmp-seeded records against PRODUCTION embeddings and pulls
# in any real ID above embedding_min_cosine.
#
# Measured 2026-07-27: test_load_guardrails_filters_by_category seeds a tmp
# guard-001/002/003 and asserts {guard-002}; the real index scores the
# UNRELATED real "guard-001" at 0.588 vs "framework-architecture" (threshold
# 0.35), so guard-001 was widened in and the assert failed. That test had
# passed for 17 days only because the index named a model that could not load
# — cosine_scores returned {} and the blend silently no-opped. The moment the
# channel was repaired the latent breach surfaced. conftest.py points this at
# a nonexistent dir so every test is hermetic by default; a test wanting the
# blend passes index_dir= explicitly or monkeypatches cosine_scores.
#
# Resolved at CALL time, never bound as a def-time default: conftest sets the
# env at import, and a def-time default would capture the value before that,
# making the seam silently inert.
_INDEX_DIR_ENV = "MIND_EMBEDDING_INDEX_DIR"


def _resolve_index_dir(index_dir):
    """Explicit arg wins; else the env seam; else the real per-box index."""
    if index_dir is not None:
        return Path(index_dir)
    env = os.environ.get(_INDEX_DIR_ENV)
    return Path(env) if env else DEFAULT_INDEX_DIR

_encoders = {}  # model_name -> encoder adapter (see _embedding_model)
_index_cache = {}  # str(index_dir) -> (mtime_ns, embeddings float32, id_list, model_name)


def _get_model(model_name=MODEL_NAME):
    """Lazy-load the encoder for `model_name` once per process. The name
    normally comes from the loaded index's meta.json (g-306-82 SSOT)."""
    enc = _encoders.get(model_name)
    if enc is None:
        from _embedding_model import load_encoder
        enc, _backend = load_encoder(model_name)
        _encoders[model_name] = enc
    return enc


def index_available(index_dir=None):
    """True iff both index files exist. Cheap presence check for the hybrid flag
    gate — retrieve.py calls this before deciding whether to attempt cosine."""
    d = _resolve_index_dir(index_dir)
    return (d / "embeddings.npy").exists() and (d / "meta.json").exists()


def _load_index(index_dir):
    """Return (embeddings float32 [N,dim], id_list, model_name) or
    (None, None, None) if absent. Cached per index_dir and invalidated when
    either file's mtime advances."""
    import json
    import numpy as np
    d = Path(index_dir)
    emb_p, meta_p = d / "embeddings.npy", d / "meta.json"
    if not (emb_p.exists() and meta_p.exists()):
        return None, None, None
    key = str(d)
    mtime = max(emb_p.stat().st_mtime_ns, meta_p.stat().st_mtime_ns)
    cached = _index_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1], cached[2], cached[3]
    emb = np.load(emb_p).astype("float32")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    ids = [rec.get("id") for rec in meta.get("docs", [])]
    model_name = (meta.get("model") or "").strip() or MODEL_NAME
    # Defensive: matrix rows must line up with the id list. If a partial write
    # ever desyncs them, truncate to the shorter — never index out of range.
    if emb.ndim != 2 or emb.shape[0] != len(ids):
        n = min(emb.shape[0] if emb.ndim == 2 else 0, len(ids))
        emb, ids = (emb[:n] if n else emb.reshape(0, -1)), ids[:n]
    _index_cache[key] = (mtime, emb, ids, model_name)
    return emb, ids, model_name


def cosine_scores(query, index_dir=None):
    """{doc_id: cosine_similarity} for `query` against the persisted index.

    Returns {} (graceful degrade — caller falls back to token-overlap) when the
    query is empty, the index is missing/empty, the model is unavailable, or ANY
    error occurs. NEVER raises into the retrieval hot path.
    """
    if not query or not isinstance(query, str):
        return {}
    try:
        emb, ids, model_name = _load_index(_resolve_index_dir(index_dir))
        if emb is None or emb.shape[0] == 0 or not ids:
            return {}
        enc = _get_model(model_name)
        # encode_query applies model-appropriate query-side preprocessing
        # (bge instruction prefix; plain for symmetric models). Test stubs
        # and legacy encoders exposing only .encode keep working.
        if hasattr(enc, "encode_query"):
            qv = enc.encode_query(query)
        else:
            qv = enc.encode([query], normalize_embeddings=True,
                            show_progress_bar=False)[0]
        # Both sides are unit-normalized, so dot product == cosine similarity.
        scores = emb @ qv.astype("float32")
        return {ids[i]: float(scores[i]) for i in range(len(ids))}
    except Exception:
        return {}


def clear_caches():
    """Test hook — drop the model + index caches so a test can rebind them."""
    _encoders.clear()
    _index_cache.clear()
