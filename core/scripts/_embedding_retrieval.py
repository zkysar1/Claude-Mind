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


# --- Degradation visibility (, 2026-08-20) -------------------------
# The five `return {}` paths below were INDISTINGUISHABLE to every caller, which
# is the whole defect this instrument exists to remove: `embedding_blend_enabled`
# is a git-tracked SHARED flag while the index is a per-box GITIGNORED artifact,
# so a box with the flag on and no index serves the token baseline forever while
# the config reads "enabled". Measured 25 days on cc-04 ( filing) and
# independently on cc-08 the same way (index dir, ~/.ayoai-vendor/py and
# ~/.ayoai-emb all absent). It has bitten the TEST suite too, in this same file:
# see the  note above, where a test passed for 17 days only because
# cosine_scores was silently returning {}.
#
# WARN ONCE PER REASON PER PROCESS, never per call. Retrieval is a hot path —
# a per-call warning would flood every command on an unprovisioned box and get
# muted, which reproduces the silence it is meant to break.
#
# NEVER RAISES. This module's contract (docstring property 1) is that the
# retrieval path cannot be broken by this file, so the recorder is itself
# wrapped: a failure to REPORT a degradation must never become a degradation.
_last_degradation = None   # None == the last call SERVED embeddings
_warned_reasons = set()


def _degrade(reason, detail="", warn=True):
    """Record why the blend degraded, warn once per reason, return {}."""
    global _last_degradation
    try:
        _last_degradation = {"reason": reason, "detail": str(detail)[:200]}
        if warn and reason not in _warned_reasons:
            _warned_reasons.add(reason)
            import sys
            sys.stderr.write(
                f"[embedding-blend] DEGRADED to token-overlap: {reason}"
                f"{(' — ' + str(detail)[:200]) if detail else ''}\n"
                "[embedding-blend]   embedding_blend_enabled is ON but this box "
                "cannot serve embeddings; retrieval is running the token baseline.\n"
                "[embedding-blend]   Provision: core/scripts/embedding-index-build.py --build "
                "(see g-115-3115 for the vendor stack), or turn the flag off.\n"
            )
    except Exception:  # pragma: no cover - reporting must never break retrieval
        pass
    return {}


def last_degradation():
    """The last cosine_scores outcome: None if it SERVED, else {reason, detail}.

    Lets a caller or trace emit blend-served-vs-degraded without re-deriving it.
    Read it immediately after a cosine_scores call — it is per-process, not
    per-request, and a later call overwrites it.
    """
    return _last_degradation


def cosine_scores(query, index_dir=None):
    """{doc_id: cosine_similarity} for `query` against the persisted index.

    Returns {} (graceful degrade — caller falls back to token-overlap) when the
    query is empty, the index is missing/empty, the model is unavailable, or ANY
    error occurs. NEVER raises into the retrieval hot path.

    Each of those paths now records a DISTINCT reason via _degrade(); read it
    with last_degradation(). An empty query is recorded but NOT warned — it is a
    normal no-op, not a degraded box, and warning on it would train readers to
    ignore the warning that matters.
    """
    global _last_degradation
    if not query or not isinstance(query, str):
        return _degrade("empty-query", warn=False)
    try:
        d = _resolve_index_dir(index_dir)
        emb, ids, model_name = _load_index(d)
        if emb is None or emb.shape[0] == 0 or not ids:
            # Absent vs present-but-empty are different operator actions:
            # provision the index, versus rebuild an index that built to zero.
            return _degrade(
                "index-empty" if index_available(d) else "index-absent", str(d))
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
        _last_degradation = None   # SERVED — the positive half of the signal
        return {ids[i]: float(scores[i]) for i in range(len(ids))}
    except Exception as e:
        # Covers the encoder path (_get_model raising when the vendored stack or
        # the model files are absent) and anything numpy raises. The class name
        # is carried because "which import failed" is the operator's next step.
        return _degrade("encoder-or-runtime-error", f"{type(e).__name__}: {e}")


def clear_caches():
    """Test hook — drop the model + index caches so a test can rebind them."""
    global _last_degradation
    _encoders.clear()
    _index_cache.clear()
    # Reset the degradation state too. Without this a test that provokes a
    # degradation leaks BOTH the recorded reason and the warn-once suppression
    # into every later test in the same process — and the suppression is the
    # dangerous half, since it would silence the warning a later test asserts on.
    _last_degradation = None
    _warned_reasons.clear()
