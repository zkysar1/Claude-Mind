"""_embedding_retrieval.py — query-side embedding retrieval for the retrieve.py
hybrid path (g-306-81 part b, unlocked by the g-306-77 A/B: embed hit@3 67% vs
token 13%).

Loads the persisted index built by embedding-index-build.py, embeds a query
OFFLINE (all-MiniLM-L6-v2, HF-cached), and returns {doc_id: cosine_score}. The
part-(b2) hybrid loaders in retrieve.py will union these scores with the existing
token-overlap matches and rank by cosine (fixing the "ranks by utility not
relevance" weakness the A/B exposed).

Two hard properties for the retrieval hot path:
  1. GRACEFUL DEGRADATION — cosine_scores NEVER raises. A missing/empty/corrupt
     index, an unavailable model, any error → returns {} so the caller falls back
     to pure token-overlap (the current behavior). The flag-OFF default plus this
     means the feature can never break retrieval.
  2. LAZY + CACHED — the ~2.5MB float16 matrix + id list load ONCE per process
     (module cache, mtime-invalidated), and the model loads ONCE (14s import,
     amortized over a long-running daemon; never loaded at all when the hybrid
     flag is OFF, since cosine_scores is simply never called).
"""
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INDEX_DIR = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"
MODEL_NAME = "all-MiniLM-L6-v2"

_model = None
_index_cache = {}  # str(index_dir) -> (mtime_ns, embeddings float32, id_list)


def _get_model():
    """Lazy-load the sentence-transformer once per process (module global)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def index_available(index_dir=DEFAULT_INDEX_DIR):
    """True iff both index files exist. Cheap presence check for the hybrid flag
    gate — retrieve.py calls this before deciding whether to attempt cosine."""
    d = Path(index_dir)
    return (d / "embeddings.npy").exists() and (d / "meta.json").exists()


def _load_index(index_dir):
    """Return (embeddings float32 [N,dim], id_list) or (None, None) if absent.
    Cached per index_dir and invalidated when either file's mtime advances."""
    import json
    import numpy as np
    d = Path(index_dir)
    emb_p, meta_p = d / "embeddings.npy", d / "meta.json"
    if not (emb_p.exists() and meta_p.exists()):
        return None, None
    key = str(d)
    mtime = max(emb_p.stat().st_mtime_ns, meta_p.stat().st_mtime_ns)
    cached = _index_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    emb = np.load(emb_p).astype("float32")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    ids = [rec.get("id") for rec in meta.get("docs", [])]
    # Defensive: matrix rows must line up with the id list. If a partial write
    # ever desyncs them, truncate to the shorter — never index out of range.
    if emb.ndim != 2 or emb.shape[0] != len(ids):
        n = min(emb.shape[0] if emb.ndim == 2 else 0, len(ids))
        emb, ids = (emb[:n] if n else emb.reshape(0, -1)), ids[:n]
    _index_cache[key] = (mtime, emb, ids)
    return emb, ids


def cosine_scores(query, index_dir=DEFAULT_INDEX_DIR):
    """{doc_id: cosine_similarity} for `query` against the persisted index.

    Returns {} (graceful degrade — caller falls back to token-overlap) when the
    query is empty, the index is missing/empty, the model is unavailable, or ANY
    error occurs. NEVER raises into the retrieval hot path.
    """
    if not query or not isinstance(query, str):
        return {}
    try:
        emb, ids = _load_index(index_dir)
        if emb is None or emb.shape[0] == 0 or not ids:
            return {}
        qv = _get_model().encode([query], normalize_embeddings=True,
                                 show_progress_bar=False)[0]
        # Both sides are unit-normalized, so dot product == cosine similarity.
        scores = emb @ qv.astype("float32")
        return {ids[i]: float(scores[i]) for i in range(len(ids))}
    except Exception:
        return {}


def clear_caches():
    """Test hook — drop the model + index caches so a test can rebind them."""
    global _model
    _model = None
    _index_cache.clear()
