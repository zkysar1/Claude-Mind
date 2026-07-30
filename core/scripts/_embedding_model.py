"""_embedding_model.py — shared encoder loading for the retrieval embedding
stack (g-306-82; used by embedding-index-build.py and _embedding_retrieval.py).

Backend AUTO-selection, per the g-306-81 spike (agents/alpha decision record
2026-07-10): fastembed (int8 ONNX — no torch, ~42MB wheel bundle, faster
short-query embeds) is preferred when it is importable AND its registry
supports the requested model; otherwise sentence-transformers (torch — the
stack delta's part a/b1 shipped on, still the fleet default until fastembed
is provisioned). Boxes without fastembed keep working unchanged.

The adapter API hides backend differences:
    enc, backend = load_encoder(model_name)
    enc.encode_docs(texts)  -> float32 (N, dim), L2-normalized rows
    enc.encode_query(text)  -> float32 (dim,),  L2-normalized

encode_query exists because retrieval-tuned models (bge family) expect a
query-side instruction prefix that documents must NOT get. fastembed's
query_embed() applies the model-appropriate prefix itself; for
sentence-transformers we apply the published bge instruction manually.
Symmetric-embedding models (MiniLM) pass through unprefixed on both paths.

Offline posture: HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE are set by the
CALLERS (builder + query helper) before importing this module; nothing here
reaches the network when model files are cached. EMBED_CACHE_DIR (env)
overrides the fastembed cache location — REQUIRED to be a SHORT path on
Windows (MAX_PATH: HF's models--org--name/snapshots/<40-hex>/ layout under a
deep base path exceeds 260 chars; the g-306-81 spike hit WinError 206).
"""
import os

# : put the per-box vendored encoder stack (pip --target under PEP 668)
# on sys.path before any backend import. Defensive for the same reason as in
# _embedding_retrieval.py — a module-level ImportError here would break the
# builder AND the query path; absent vendor dir just means the pre-vendor
# behavior (backends unavailable, callers degrade).
try:
    import _vendor_path  # noqa: F401
except Exception:  # pragma: no cover - sibling dir not importable
    pass

# The published query instruction for the bge-en v1/v1.5 family. Applied
# ONLY on the sentence-transformers path (fastembed's query_embed handles
# it internally) and ONLY for bge-prefixed model names.
_BGE_QUERY_INSTRUCTION = ("Represent this sentence for searching relevant "
                          "passages: ")


def _l2_normalize(arr):
    """Row-normalize; idempotent on already-unit vectors. Guards zero rows."""
    import numpy as np
    arr = np.asarray(arr, dtype="float32")
    if arr.ndim == 1:
        n = float((arr ** 2).sum()) ** 0.5
        return arr / n if n > 0 else arr
    norms = (arr ** 2).sum(axis=1, keepdims=True) ** 0.5
    norms[norms == 0] = 1.0
    return arr / norms


def _is_bge(model_name):
    return "bge-" in (model_name or "").lower()


class _FastembedEncoder:
    def __init__(self, model):
        self._m = model

    def encode_docs(self, texts):
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype="float32")
        return _l2_normalize(np.asarray(list(self._m.embed(texts, batch_size=8))))

    def encode_query(self, text):
        import numpy as np
        # query_embed applies the model's own query-side preprocessing
        # (instruction prefix for bge; plain passthrough for MiniLM-class).
        return _l2_normalize(np.asarray(list(self._m.query_embed(text))[0]))


class _STEncoder:
    def __init__(self, model, model_name):
        self._m = model
        self._name = model_name

    def encode_docs(self, texts):
        import numpy as np
        if not texts:
            return np.zeros((0, self._m.get_sentence_embedding_dimension()),
                            dtype="float32")
        return _l2_normalize(self._m.encode(
            texts, normalize_embeddings=True, batch_size=64,
            show_progress_bar=False))

    def encode_query(self, text):
        q = (_BGE_QUERY_INSTRUCTION + text) if _is_bge(self._name) else text
        return _l2_normalize(self._m.encode(
            [q], normalize_embeddings=True, show_progress_bar=False)[0])


def _fastembed_resolve(model_name, supported):
    """Map a configured model name onto fastembed's registry naming.
    Tries exact, org-prefixed, and basename matches — the registry uses
    HF ids ('sentence-transformers/all-MiniLM-L6-v2', 'BAAI/bge-small-en-v1.5')
    while configs and st accept short names."""
    if model_name in supported:
        return model_name
    for cand in supported:
        if cand.endswith("/" + model_name):
            return cand
    return None


def load_encoder(model_name):
    """Return (encoder, backend_name) for `model_name`.

    fastembed first (when importable AND the registry supports the model),
    sentence-transformers otherwise. Raises only when BOTH backends fail —
    callers that must never raise (query hot path) wrap this themselves.
    """
    try:
        from fastembed import TextEmbedding
        supported = {m.get("model") for m in TextEmbedding.list_supported_models()}
        resolved = _fastembed_resolve(model_name, supported)
        if resolved:
            # Cache default: a stable SHORT home-dir path, not fastembed's
            # OS-temp default (cleaned by temp sweeps → silent re-download
            # need → offline boxes degrade to token-only). EMBED_CACHE_DIR
            # env overrides. Short matters on Windows (MAX_PATH, WinError
            # 206 —  spike).
            cache = (os.environ.get("EMBED_CACHE_DIR") or "").strip()
            if not cache:
                from pathlib import Path
                cache = str(Path.home() / ".ayoai-emb")
            return _FastembedEncoder(
                TextEmbedding(model_name=resolved, cache_dir=cache)), "fastembed"
    except Exception as exc:
        #  (b): NEVER swallow this silently. A `pass` here is how the
        # semantic retrieval channel stayed dead on a box for 17 days (2026-07-10
        # to 2026-07-27) while every query still returned HTTP 200 — the fallback
        # degraded results to token-overlap only, and nothing said so. The failure
        # is soft by design (callers must keep working), but soft must not mean
        # SILENT: emit one diagnostic naming the model and the reason so a dead
        # channel is visible in daemon logs and to health probes.
        #
        # This is also where the latency goes. When fastembed cannot load a model
        # the call falls through to SentenceTransformer() below, which under the
        # offline posture searches the HF cache layout before failing — measured
        # ~28s cold on a box where sentence-transformers IS installed. On a box
        # without it the fallback raises immediately (0.13s measured here), so the
        # cost is paid only on ST-provisioned boxes.
        import sys
        print("[embedding-model] fastembed backend unavailable for "
              "%r (%s: %s) — falling back to sentence-transformers; "
              "if that also fails, retrieval degrades to token-overlap only"
              % (model_name, type(exc).__name__, str(exc)[:160]),
              file=sys.stderr)
    from sentence_transformers import SentenceTransformer
    return _STEncoder(SentenceTransformer(model_name), model_name), \
        "sentence-transformers"
