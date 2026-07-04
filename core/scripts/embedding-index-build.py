#!/usr/bin/env python3
"""embedding-index-build.py — build/update a persisted embedding index for the
Mind framework's supplementary-store retrieval corpus (guardrails + reasoning-bank).

Part (a) of g-306-81 (integration), unlocked by the g-306-77 A/B: offline
all-MiniLM-L6-v2 embedding-cosine beat retrieve.py's token-overlap text-fallback
~5x on hit@3 (67% vs 13%). This script produces the PERSISTED index that the
part-(b) retrieve.py hybrid path will load and cosine-rank against — so the ~14-min
full CPU build (3.8 docs/s over ~3200 docs) happens ONCE, and `--update` re-embeds
only changed/new docs on write (never per query).

Scope note: guardrails + reasoning-bank are the SUPPLEMENTARY stores whose
text-fallback (_entry_matches_text: binary >=2-shared-token predicate, then ranked
by utility not relevance) is exactly what the A/B showed embedding fixes. Tree nodes
use retrieve.py's SEPARATE richer Substring/Entity-index/Word-prefix/Concept cascade
— embedding them is an additive follow-on, not part of this text-fallback fix.

Offline/local only: all-MiniLM-L6-v2 must be HF-cached; HF_HUB_OFFLINE is forced.
No external embedding API (own-cloud posture).

Layout (default mind_api/state/retrieval-embedding-index/ — gitignored, local,
daemon-adjacent; the daemon serves retrieval so the index is its cache):
  embeddings.npy   float16 (N, dim), row i <-> meta["docs"][i]
  meta.json        {model, dim, built_at, count, source_hashes, docs:[{id,type,hash}]}

Usage:
  py -3 core/scripts/embedding-index-build.py --build            # full rebuild
  py -3 core/scripts/embedding-index-build.py --update           # incremental
  py -3 core/scripts/embedding-index-build.py --stats            # report only
  py -3 core/scripts/embedding-index-build.py --build --limit N  # test subset
  py -3 core/scripts/embedding-index-build.py --build --out DIR   # custom location
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import retrieve as R  # noqa: E402  canonical store readers (read_jsonl / paths)

MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_OUT = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"


def match_text(e):
    """The doc text surface embedded — exactly the fields retrieve.py's
    supplementary matcher (_entry_matches_text) tokenizes: title/content/rule/
    summary + tags + when_to_use.conditions. Keeping this identical to the matched
    surface is what made the A/B a clean controlled comparison; the persisted index
    must embed the SAME surface so part-(b) query-time cosine ranks over it fairly."""
    parts = []
    for f in ("title", "content", "rule", "summary"):
        v = e.get(f)
        if isinstance(v, str) and v:
            parts.append(v)
    tags = e.get("tags")
    if isinstance(tags, list):
        parts.extend(t for t in tags if isinstance(t, str))
    wtu = e.get("when_to_use")
    if isinstance(wtu, dict):
        c = wtu.get("conditions")
        if isinstance(c, list):
            parts.extend(s for s in c if isinstance(s, str))
        elif isinstance(c, str):
            parts.append(c)
    elif isinstance(wtu, str):
        parts.append(wtu)
    return " ".join(parts).strip()


def content_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_corpus(limit=None):
    """Active guardrails + reasoning-bank as [{id, type, text, hash}], skipping
    empty-text records. Deterministic order (guardrails then rb, file order).

    When limit is None, honors EMBED_INDEX_LIMIT (test knob) so build/update/stats
    all see the SAME subset in a test run — otherwise stats/update would load the
    full corpus and a --limit-built index would look permanently stale."""
    if limit is None:
        env = os.environ.get("EMBED_INDEX_LIMIT")
        if env and env.isdigit():
            limit = int(env)
    docs = []
    for e in R.read_jsonl(R.GUARD_PATH):
        if (e.get("status") or "active") != "active":
            continue
        t = match_text(e)
        if t and e.get("id"):
            docs.append({"id": e["id"], "type": "guardrail", "text": t, "hash": content_hash(t)})
    for e in R.read_jsonl(R.RB_PATH):
        if (e.get("status") or "active") != "active":
            continue
        t = match_text(e)
        if t and e.get("id"):
            docs.append({"id": e["id"], "type": "rb", "text": t, "hash": content_hash(t)})
    # De-dup by id (defensive — a store should not carry two active same-id rows;
    # keep first). Preserves order.
    seen = set()
    uniq = []
    for d in docs:
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        uniq.append(d)
    if limit:
        uniq = uniq[:limit]
    return uniq


def _load_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_NAME)


def _embed(model, texts, batch_size=64):
    import numpy as np
    if not texts:
        return np.zeros((0, model.get_sentence_embedding_dimension()), dtype="float16")
    arr = model.encode(texts, normalize_embeddings=True, batch_size=batch_size,
                       show_progress_bar=False)
    return arr.astype("float16")


def _atomic_write_index(out, embeddings, docs):
    """Write embeddings.npy + meta.json atomically (tmp + rename)."""
    import numpy as np
    out.mkdir(parents=True, exist_ok=True)
    emb_tmp = out / "embeddings.npy.tmp"
    meta_tmp = out / "meta.json.tmp"
    np.save(emb_tmp, embeddings)
    # np.save appends .npy to a path without it; normalize the tmp name it wrote.
    written = emb_tmp if emb_tmp.exists() else Path(str(emb_tmp) + ".npy")
    meta = {
        "model": MODEL_NAME,
        "dim": int(embeddings.shape[1]) if embeddings.shape[0] else 384,
        "count": len(docs),
        "docs": [{"id": d["id"], "type": d["type"], "hash": d["hash"]} for d in docs],
    }
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(written, out / "embeddings.npy")
    os.replace(meta_tmp, out / "meta.json")
    return meta


def cmd_build(out, limit=None):
    import numpy as np
    t0 = time.time()
    docs = load_corpus(limit=limit)
    model = _load_model()
    emb = _embed(model, [d["text"] for d in docs])
    meta = _atomic_write_index(out, emb, docs)
    print(json.dumps({"op": "build", "count": len(docs), "dim": meta["dim"],
                      "seconds": round(time.time() - t0, 1),
                      "bytes": int(emb.nbytes), "out": str(out)}))


def cmd_update(out):
    """Incremental: keep unchanged doc embeddings, re-embed only new/changed docs
    (by content hash), drop removed. Reassemble in current-corpus order."""
    import numpy as np
    t0 = time.time()
    meta_path = out / "meta.json"
    emb_path = out / "embeddings.npy"
    if not (meta_path.exists() and emb_path.exists()):
        return cmd_build(out)  # no prior index — full build
    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    old_emb = np.load(emb_path)
    old_by_id = {d["id"]: (i, d["hash"]) for i, d in enumerate(old_meta.get("docs", []))}
    cur = load_corpus()
    reused = to_embed = 0
    rows = []
    to_embed_texts, to_embed_slots = [], []
    for d in cur:
        prev = old_by_id.get(d["id"])
        if prev is not None and prev[1] == d["hash"] and prev[0] < old_emb.shape[0]:
            rows.append(old_emb[prev[0]])  # unchanged — reuse
            reused += 1
        else:
            rows.append(None)  # placeholder — fill after embedding
            to_embed_texts.append(d["text"])
            to_embed_slots.append(len(rows) - 1)
            to_embed += 1
    if to_embed_texts:
        model = _load_model()
        new_emb = _embed(model, to_embed_texts)
        for slot, vec in zip(to_embed_slots, new_emb):
            rows[slot] = vec
    if rows:
        emb = np.vstack([r.reshape(1, -1) for r in rows]).astype("float16")
    else:
        emb = np.zeros((0, old_emb.shape[1] if old_emb.ndim == 2 else 384), dtype="float16")
    cur_ids = {d["id"] for d in cur}
    removed = sum(1 for i in old_by_id if i not in cur_ids)
    meta = _atomic_write_index(out, emb, cur)
    print(json.dumps({"op": "update", "count": len(cur), "reused": reused,
                      "reembedded": to_embed, "removed": removed,
                      "seconds": round(time.time() - t0, 1), "out": str(out)}))


def cmd_stats(out):
    meta_path = out / "meta.json"
    emb_path = out / "embeddings.npy"
    if not meta_path.exists():
        print(json.dumps({"op": "stats", "exists": False, "out": str(out)}))
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # Staleness: compare stored per-doc hashes against the live corpus.
    cur = load_corpus()
    cur_hashes = {d["id"]: d["hash"] for d in cur}
    old_hashes = {d["id"]: d["hash"] for d in meta.get("docs", [])}
    added = [i for i in cur_hashes if i not in old_hashes]
    removed = [i for i in old_hashes if i not in cur_hashes]
    changed = [i for i in cur_hashes if i in old_hashes and cur_hashes[i] != old_hashes[i]]
    print(json.dumps({"op": "stats", "exists": True, "model": meta.get("model"),
                      "dim": meta.get("dim"), "indexed": meta.get("count"),
                      "live_corpus": len(cur), "added": len(added),
                      "removed": len(removed), "changed": len(changed),
                      "stale": bool(added or removed or changed),
                      "bytes": emb_path.stat().st_size if emb_path.exists() else 0,
                      "out": str(out)}))


def main():
    ap = argparse.ArgumentParser(description="Build/update the retrieval embedding index.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--build", action="store_true", help="Full rebuild")
    g.add_argument("--update", action="store_true", help="Incremental update by content hash")
    g.add_argument("--stats", action="store_true", help="Report index state + staleness")
    ap.add_argument("--out", type=str, default=str(DEFAULT_OUT), help="Index directory")
    ap.add_argument("--limit", type=int, default=None, help="Cap corpus size (testing)")
    args = ap.parse_args()
    out = Path(args.out)
    if args.build:
        cmd_build(out, limit=args.limit)
    elif args.update:
        cmd_update(out)
    else:
        cmd_stats(out)


if __name__ == "__main__":
    main()
