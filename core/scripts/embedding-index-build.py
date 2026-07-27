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
import _vendor_path  # noqa: E402,F401  : per-box vendored encoder stack
import retrieve as R  # noqa: E402  canonical store readers (read_jsonl / paths)

MODEL_NAME = "all-MiniLM-L6-v2"  # fallback when config + --model are absent
DEFAULT_OUT = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"


def resolve_model_name(cli_override=None):
    """ precedence: --model CLI > tree.yaml retrieval:
    embedding_model_name > MODEL_NAME fallback. The BUILDER is the only
    config consumer — the query side (_embedding_retrieval) reads the model
    name from the built index's meta.json, so index and query can never
    disagree about the model."""
    if cli_override:
        return cli_override
    try:
        cfg = R._load_retrieval_config()
        name = (cfg.get("embedding_model_name") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return MODEL_NAME


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


def tree_doc_id(node):
    """Stable index id for a tree node: 'tree:' + tree-root-relative path
    (no .md). Derived from the node's `file` field — NOT the basename key,
    which is retrieve.load_tree_nodes' namespace but is neither unique nor
    stable across moves. Same derivation as retrieve._graph_node_key_
    candidates' path form (the g-306-45 lesson: a basename-keyed join
    matched ZERO real records and the PPR blend shipped silently inert).
    Returns None when the file field doesn't contain the tree marker."""
    f = str((node or {}).get("file") or "").replace("\\", "/")
    marker = "knowledge/tree/"
    i = f.find(marker)
    if i < 0:
        return None
    rel = f[i + len(marker):]
    if rel.endswith(".md"):
        rel = rel[:-3]
    return ("tree:" + rel) if rel else None


TREE_BODY_CHAR_CAP = 400


def tree_body_paragraph(node, cap=TREE_BODY_CHAR_CAP):
    """First prose paragraph of a tree node's .md body, for the embed surface.

    g-306-87: the g-306-83 tree-lane A/B missed 5/12 node-paraphrase queries
    because the embedded surface was only humanized-key + summary, and a
    one-line summary carries too little signal to match a paraphrase. The
    node's opening paragraph is the cheapest available depth.

    The node's `file` carries the VIRTUAL `world/` prefix, which is NOT
    PROJECT_ROOT-relative under the .mind-data layout — it MUST go through
    _paths.resolve_file_path. (g-115-3099: `PROJECT_ROOT / file_path` silently
    yields a nonexistent path, so every body read returns empty and the caller
    degrades to exactly the thin surface this function exists to widen.)

    Fail-open: any unreadable or malformed node yields "" so a 1251-node build
    never dies on one bad file."""
    f = (node or {}).get("file")
    if not isinstance(f, str) or not f:
        return ""
    try:
        from _paths import resolve_file_path
        text = Path(resolve_file_path(f)).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    # Strip YAML front matter when present (--- ... ---).
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
    # First paragraph that is prose, not a heading / list / table / code fence.
    for block in text.split("\n\n"):
        para = " ".join(block.split()).strip()
        if not para or para[0] in "#|->`*" or para.startswith("---"):
            continue
        return para[:cap]
    return ""


def tree_doc_text(key, node):
    """The embedded surface for a tree node: humanized key + summary + the
    first body paragraph (g-306-87).

    Body reads were deliberately absent before g-306-87 ("bodies are the LLM's
    post-triage Read, not the match surface"). The g-306-83 A/B refuted that
    for MATCHING specifically: 5/12 node-paraphrase queries missed both arms on
    the key+summary surface alone. Bodies remain the post-triage Read; only
    their first paragraph joins the match surface, capped so one long node
    cannot dominate the embedding."""
    parts = [str(key or "").replace("-", " ")]
    s = (node or {}).get("summary")
    if isinstance(s, str) and s:
        parts.append(s)
    body = tree_body_paragraph(node)
    if body:
        parts.append(body)
    return " ".join(p for p in parts if p).strip()


def load_corpus(limit=None):
    """Active guardrails + reasoning-bank + tree nodes as [{id, type, text,
    hash}], skipping empty-text records. Deterministic order (guardrails,
    rb, then tree in _tree.yaml order).

    Tree docs (g-306-83) use 'tree:<relpath>' ids — namespace-disjoint from
    rb-*/guard-* so the supplementary consumers' id joins ignore them and
    the tree channel's join ignores supplementary rows.

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
    try:
        tree = R.read_yaml(R.TREE_PATH) if R.TREE_PATH else {}
    except Exception:
        tree = {}
    for key, node in (tree.get("nodes") or {}).items():
        did = tree_doc_id(node)
        t = tree_doc_text(key, node)
        if did and t:
            docs.append({"id": did, "type": "tree", "text": t, "hash": content_hash(t)})
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


def _load_encoder(model_name):
    """: backend-agnostic encoder (fastembed ONNX preferred,
    sentence-transformers fallback). See _embedding_model.load_encoder."""
    from _embedding_model import load_encoder
    return load_encoder(model_name)


def _embed(encoder, texts):
    import numpy as np
    if not texts:
        return np.zeros((0, 384), dtype="float16")
    return np.asarray(encoder.encode_docs(texts)).astype("float16")


def _atomic_write_index(out, embeddings, docs, model_name, backend=None):
    """Write embeddings.npy + meta.json atomically (tmp + rename).

    meta["model"] is the SSOT the query side (_embedding_retrieval) loads
    its encoder from — index and query can never disagree about the model."""
    import numpy as np
    out.mkdir(parents=True, exist_ok=True)
    emb_tmp = out / "embeddings.npy.tmp"
    meta_tmp = out / "meta.json.tmp"
    np.save(emb_tmp, embeddings)
    # np.save appends .npy to a path without it; normalize the tmp name it wrote.
    written = emb_tmp if emb_tmp.exists() else Path(str(emb_tmp) + ".npy")
    meta = {
        "model": model_name,
        "backend": backend,
        "dim": int(embeddings.shape[1]) if embeddings.shape[0] else 384,
        "count": len(docs),
        "docs": [{"id": d["id"], "type": d["type"], "hash": d["hash"]} for d in docs],
    }
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(written, out / "embeddings.npy")
    os.replace(meta_tmp, out / "meta.json")
    return meta


def cmd_build(out, limit=None, model_override=None):
    import numpy as np
    t0 = time.time()
    model_name = resolve_model_name(model_override)
    docs = load_corpus(limit=limit)
    encoder, backend = _load_encoder(model_name)
    emb = _embed(encoder, [d["text"] for d in docs])
    meta = _atomic_write_index(out, emb, docs, model_name, backend)
    print(json.dumps({"op": "build", "model": model_name, "backend": backend,
                      "count": len(docs), "dim": meta["dim"],
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
    # MODEL PINNING (): an incremental update MUST embed with the
    # model the existing index was built with — mixing two models' vectors
    # in one matrix silently corrupts every cosine. Config/--model changes
    # take effect only via an explicit --build (full re-embed).
    index_model = (old_meta.get("model") or "").strip() or MODEL_NAME
    configured = resolve_model_name(None)
    if configured != index_model:
        print(json.dumps({"op": "update", "note": "model_drift",
                          "index_model": index_model,
                          "configured_model": configured,
                          "action": "updating with index_model; run --build "
                                    "to switch models"}), file=sys.stderr)
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
    backend = old_meta.get("backend")
    if to_embed_texts:
        encoder, backend = _load_encoder(index_model)
        new_emb = _embed(encoder, to_embed_texts)
        for slot, vec in zip(to_embed_slots, new_emb):
            rows[slot] = vec
    if rows:
        emb = np.vstack([r.reshape(1, -1) for r in rows]).astype("float16")
    else:
        emb = np.zeros((0, old_emb.shape[1] if old_emb.ndim == 2 else 384), dtype="float16")
    cur_ids = {d["id"] for d in cur}
    removed = sum(1 for i in old_by_id if i not in cur_ids)
    meta = _atomic_write_index(out, emb, cur, index_model, backend)
    print(json.dumps({"op": "update", "model": index_model, "count": len(cur),
                      "reused": reused, "reembedded": to_embed,
                      "removed": removed,
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
    ap.add_argument("--model", type=str, default=None,
                    help="Model override for --build (g-306-82; default: "
                         "tree.yaml retrieval: embedding_model_name, then "
                         f"{MODEL_NAME}). Ignored by --update, which pins "
                         "the existing index's model.")
    args = ap.parse_args()
    out = Path(args.out)
    if args.build:
        cmd_build(out, limit=args.limit, model_override=args.model)
    elif args.update:
        cmd_update(out)
    else:
        cmd_stats(out)


if __name__ == "__main__":
    main()
