"""TF-IDF + cosine similarity for tree node retrieval ranking.

Layered ON TOP of the existing channel-based matching in tree_match.py.
Channel scoring (substring/entity_index/concept/word_prefix/sibling/parent)
decides ELIGIBILITY; cosine similarity refines the ranking among the
eligible matches by rewarding rare-token overlap and downweighting common-
token overlap. This is the audit-driven fix for the NOISY tree-leaf problem
where generic-token parents (intelligence-pipeline, platform-overview)
were ranking with specific leaves on shared common tokens.

Single source of truth: `_tree.yaml` `nodes` is canonical. The IDF index is
derived per call from the in-memory parsed nodes — no on-disk cache to
invalidate. Build cost is O(N * avg_tokens_per_summary), measured ~30ms
for 985 nodes; rebuild-per-call is acceptable until it isn't.

Tokenizer: same regex as tree_match.py P0 #2 (`re.findall(r'[a-z0-9]+', ...)`)
plus a min-length-3 filter. Aligned tokenization is the SINGLE SOURCE OF
TRUTH for what constitutes a token across the matching pipeline; do NOT
introduce a different splitter here without updating tree_match too.

Score integration: callers (tree_match._score_and_limit and
retrieve._score_weight_limit) compute cosine externally and add
`COSINE_BONUS_WEIGHT * cosine` to the base channel score. Cosine is in
[0, 1]; with COSINE_BONUS_WEIGHT=2.0 a fully-aligned query/node pair adds
~2.0 on top of channel base scores typically in [3, 7]. Tunable; treat the
weight as a hypothesis to verify against real query top-K shifts.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# DO NOT INLINE this regex — it must match tree_match.py Strategy 3/4
# tokenizer exactly. The min-length filter (>= 3) here is one char less
# strict than tree_match's word_prefix filter (>= 4) on purpose: the
# corpus-stat layer benefits from including 3-char content tokens
# ("npc", "iaus", "gba", "rps") that the prefix-match filter excludes.
# Stopwords are NOT filtered here — IDF naturally drops common terms.
_TOKEN_RE = re.compile(r'[a-z0-9]+')
_MIN_LEN = 3


def tokenize(text):
    """Lowercase + word-boundary split + min-length filter. List, not set —
    repeated tokens carry TF signal."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= _MIN_LEN]


def _doc_text(key, node):
    """Return the text per node that participates in IDF. Today: key +
    summary. Adding entities from front matter would mean reading every
    .md file at index-build time — large I/O for marginal gain since
    entities are already a separate match channel in tree_match.py."""
    summary = node.get("summary") or ""
    return key + " " + str(summary)


def build_index(nodes):
    """Build the IDF index from a {key: node_dict} mapping.

    Returns:
        {
          "idf": {term: idf_weight},
          "vectors": {key: (vec_dict, magnitude)},
          "n_docs": int,
        }

    The smoothed IDF formula `log((N+1)/(df+1)) + 1` is the same as
    sklearn's TfidfVectorizer default (smooth_idf=True, norm=None). The +1
    addition keeps every term's IDF strictly positive, which prevents a
    single-doc term from getting zero weight when the corpus is small.
    """
    docs = {}
    df = Counter()
    for key, node in nodes.items():
        toks = tokenize(_doc_text(key, node))
        bag = Counter(toks)
        docs[key] = bag
        for term in bag:
            df[term] += 1

    N = len(nodes)
    idf = {t: math.log((N + 1) / (c + 1)) + 1 for t, c in df.items()}

    vectors = {}
    for key, bag in docs.items():
        vec = {t: tf * idf.get(t, 0.0) for t, tf in bag.items()}
        mag = math.sqrt(sum(v * v for v in vec.values()))
        vectors[key] = (vec, mag)

    return {"idf": idf, "vectors": vectors, "n_docs": N}


def query_vector(query_text, idf):
    """Compute the (vec, magnitude) tuple for a query string against an
    existing IDF table. Terms in the query that are unknown to the corpus
    get IDF=0 (no contribution), which is correct: an unseen term cannot
    discriminate between corpus nodes."""
    toks = tokenize(query_text)
    bag = Counter(toks)
    vec = {t: tf * idf.get(t, 0.0) for t, tf in bag.items()}
    mag = math.sqrt(sum(v * v for v in vec.values()))
    return (vec, mag)


def cosine(q_vm, d_vm):
    """Cosine similarity between two (vec_dict, magnitude) tuples.

    Returns 0.0 when either side is empty or zero-magnitude — this is the
    correct neutral value (no signal), not an error condition. Iterating
    the smaller dict makes the dot product O(min(|q|, |d|)) instead of
    O(|q| * |d|).
    """
    q_vec, q_mag = q_vm
    d_vec, d_mag = d_vm
    if q_mag == 0.0 or d_mag == 0.0:
        return 0.0
    if len(q_vec) <= len(d_vec):
        dot = sum(v * d_vec.get(t, 0.0) for t, v in q_vec.items())
    else:
        dot = sum(v * q_vec.get(t, 0.0) for t, v in d_vec.items())
    return dot / (q_mag * d_mag)
