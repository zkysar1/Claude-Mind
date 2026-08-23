#!/usr/bin/env python3
"""Guardrail-vs-guardrail pair audit — contradictions AND near-duplicates in ONE pass.

Sibling of ``guardrail-protocol-conflict-check.py``, which does guardrail-vs-PROTOCOL
(a guardrail rule contradicting a SKILL.md protocol line). This one does
guardrail-vs-GUARDRAIL, which nothing covered. (g-115-7055 deliverables 2 + 3.)

WHY ONE PASS AND NOT TWO DETECTORS
----------------------------------
The originating goal scoped these as separate deliverables — pairwise contradiction
detection (2) and a stock near-duplicate consolidation slate (3) — and proposed
reusing ``store_dupe_warn.py`` similarity for the first. Similarity is the right
BUCKETING primitive but the wrong DETECTOR, because on this corpus similarity and
contradiction are nearly anti-correlated at the decisive moment:

    guard-A: "ALWAYS use the canonical wrapper before probing"
    guard-B: "NEVER use the canonical wrapper before probing"

Those contradict, and they are among the most token-similar pairs in the entire
store — they differ by ONE token. A similarity ranking therefore surfaces
contradictions and near-duplicates interleaved and cannot separate them; a
threshold alone would emit both classes as one undifferentiated slate.

The discriminator is POLARITY, applied INSIDE a high-similarity bucket:

    same subject + SAME polarity      -> NEAR-DUPLICATE  (consolidation: merge/supersede)
    same subject + OPPOSITE polarity  -> CONTRADICTION   (scope-split per guard-3814)

So both deliverables fall out of one scan. That is strictly less code than two
detectors and avoids a second full pass over a 4k-record store.

POLARITY IS STRIPPED BEFORE SIMILARITY. If the polarity tokens stayed in the bag,
the pair above would score LOWER than an unrelated pair that happens to share
filler — exactly backwards. Subject similarity is computed on content tokens with
polarity words removed, so "always X" and "never X" score 1.0 on subject and are
then split by polarity. This is the single most important implementation detail in
the file.

POLARITY IS READ FROM THE FIRST CLAUSE, NOT THE FIRST TOKEN. guard-1421 measured
the operative imperative sitting past the head of the rule ~85% of the time, so a
first-token read misclassifies most of the corpus.

PROPOSAL ONLY. There is no ``--apply`` and none should be added. guard-3814: a
contradiction resolves by finding the PRECONDITION under which each side is right
(scope-split) — never by utilization counts and never by averaging two rules
together. learning-philosophy rule 5: retirement is a judgement the loop must make,
not delegate.

Usage:
    py -3 core/scripts/guardrail-pair-audit.py --output json
    py -3 core/scripts/guardrail-pair-audit.py --class contradiction --top 20
"""

import argparse
import json
import re
import sys
from itertools import combinations
from pathlib import Path

_SELF = Path(__file__).resolve().parent
if str(_SELF) not in sys.path:
    sys.path.insert(0, str(_SELF))

INACTIVE_STATUSES = {"retired", "superseded", "archived"}

# Negation/prohibition markers. Presence of ANY in the first clause => NEGATIVE.
NEG_MARKERS = {
    "never", "not", "no", "avoid", "avoids", "forbid", "forbids", "forbidden",
    "refuse", "refuses", "prohibit", "prohibits", "cannot", "cant", "dont",
    "doesnt", "shouldnt", "mustnt", "wont", "without", "stop", "skip",
    "exclude", "omit", "suppress", "disallow", "disallowed",
}
# Affirmative imperative markers — used only to confirm a clause IS directive.
POS_MARKERS = {
    "always", "must", "require", "requires", "required", "ensure", "ensures",
    "should", "shall", "do", "use", "run", "check", "verify", "confirm",
    "prefer", "treat", "read", "call", "pass", "add", "keep", "state", "record",
}
# Stripped before subject-similarity so polarity never drives the score.
POLARITY_TOKENS = NEG_MARKERS | POS_MARKERS

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "it",
    "its", "this", "that", "these", "those", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "and", "or", "but", "if", "then", "than",
    "when", "while", "you", "your", "we", "our", "they", "their", "has", "have",
    "had", "will", "would", "can", "could", "may", "might", "there", "here",
    "what", "which", "who", "how", "why", "so", "because", "into", "about",
    "any", "all", "one", "two", "each", "every", "other", "same", "such",
}

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
# First clause = up to the first strong break. Falls back to the whole rule.
CLAUSE_BREAK_RE = re.compile(r"[.;:]|\s+--\s+|\s+—\s+")


def _world_path() -> Path:
    import os
    for var in ("WORLD_PATH", "MIND_WORLD", "WORLD_DIR"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return Path(_SELF).parent.parent / "world"


def tokens(text: str) -> set:
    return {t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS}


def subject_tokens(text: str) -> set:
    """Content tokens with polarity removed — the SUBJECT of the rule.

    Removing polarity is what lets 'always X' and 'never X' score as the same
    subject so the polarity split can then separate them. Leaving them in would
    penalise exactly the pairs this audit exists to find.
    """
    return tokens(text) - POLARITY_TOKENS


def first_clause(rule: str) -> str:
    parts = CLAUSE_BREAK_RE.split(rule or "", maxsplit=1)
    head = (parts[0] if parts else rule or "").strip()
    return head or (rule or "")


def polarity(rule: str) -> str:
    """'neg' | 'pos' | 'unknown' — read from the FIRST CLAUSE (guard-1421).

    'unknown' is a real third value, not a default: a rule whose first clause
    carries no directive marker has no polarity to compare, and pairing on it
    would manufacture a verdict from absence. Those pairs are reported as
    class='unclassified' rather than silently bucketed either way.
    """
    tk = tokens(first_clause(rule))
    if tk & NEG_MARKERS:
        return "neg"
    if tk & POS_MARKERS:
        return "pos"
    return "unknown"


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


class StoreUnreadable(RuntimeError):
    """The guardrail store could not be read. NOT the same as an empty store."""


def load_active_guardrails(world: Path) -> list:
    """Load active guardrails, or RAISE. Never returns [] for a missing store.

    This raises rather than returning an empty list because the two are
    byte-identical to a caller and the empty one is a lie (guard-1715: an empty
    population and an unexamined one look the same). Measured while building
    this file: a bare ``py -3 core/scripts/guardrail-pair-audit.py`` resolved
    WORLD_PATH to the nonexistent fallback ``PROJECT_ROOT/world`` and printed a
    confident ``active=0 findings=0`` over a live 11.9MB / 4,382-line store —
    a clean bill of health for a scan that read nothing. WORLD_PATH comes from
    the per-agent local-paths.conf that only ``_paths.sh`` reads, so callers
    MUST go through ``guardrail-pair-audit.sh`` (guard-3864, the same trap the
    stranded-claim sweep documents).
    """
    path = world / "guardrails.jsonl"
    out = []
    if not path.exists():
        raise StoreUnreadable(
            f"guardrails.jsonl not found at {path}. WORLD_PATH resolved to "
            f"{world}, which is almost certainly the fallback rather than the "
            f"configured world. Invoke core/scripts/guardrail-pair-audit.sh "
            f"(it sources _paths.sh); a bare `py -3 ...` has no world mapping "
            f"and would otherwise report a scan of zero records as clean."
        )
    with open(str(path), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (rec.get("status") or "active") in INACTIVE_STATUSES:
                continue
            rule = rec.get("rule") or rec.get("title") or ""
            if not rule:
                continue
            out.append({
                "id": rec.get("id") or "",
                "rule": rule,
                "category": rec.get("category") or "",
                "subject": subject_tokens(rule),
                "polarity": polarity(rule),
            })
    return out


def bucket(records: list, min_token_len: int = 4) -> dict:
    """Bucket by (category, discriminative token).

    Full pairwise on the live corpus is ~9.3M unordered pairs — note that is
    n*(n-1)/2, NOT n^2; a naive n^2 figure double-counts every pair. Bucketing
    keeps comparisons to pairs that already share a category AND a reasonably
    specific token, which is where same-subject pairs necessarily live.
    """
    buckets = {}
    for i, rec in enumerate(records):
        for tok in rec["subject"]:
            if len(tok) < min_token_len:
                continue
            buckets.setdefault((rec["category"], tok), []).append(i)
    return buckets


def audit(records: list, threshold: float, max_bucket: int) -> dict:
    buckets = bucket(records)
    seen_pairs = set()
    findings = []
    oversized = 0
    compared = 0

    for key, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        if len(idxs) > max_bucket:
            # A hub token (e.g. a category-wide word) degenerates toward the
            # full pairwise cost this bucketing exists to avoid. Skipping is
            # COUNTED and reported, never silent — an unreported skip makes the
            # slate read as complete when it is not (guard-3830).
            oversized += 1
            continue
        for i, j in combinations(sorted(idxs), 2):
            if (i, j) in seen_pairs:
                continue
            seen_pairs.add((i, j))
            compared += 1
            a, b = records[i], records[j]
            sim = jaccard(a["subject"], b["subject"])
            if sim < threshold:
                continue
            pa, pb = a["polarity"], b["polarity"]
            if pa == "unknown" or pb == "unknown":
                cls = "unclassified"
            elif pa != pb:
                cls = "contradiction"
            else:
                cls = "near-duplicate"
            findings.append({
                "class": cls,
                "similarity": round(sim, 3),
                "a_id": a["id"], "a_polarity": pa, "a_rule": a["rule"][:240],
                "b_id": b["id"], "b_polarity": pb, "b_rule": b["rule"][:240],
                "category": a["category"],
                "shared_token": key[1],
            })

    findings.sort(key=lambda f: (-f["similarity"], f["a_id"], f["b_id"]))
    by_class = {}
    for f in findings:
        by_class[f["class"]] = by_class.get(f["class"], 0) + 1
    return {
        "active_guardrails": len(records),
        "buckets": len(buckets),
        "oversized_buckets_skipped": oversized,
        "pairs_compared": compared,
        "findings_total": len(findings),
        "by_class": by_class,
        "findings": findings,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", choices=["human", "json"], default="human")
    ap.add_argument("--world", default="", help="override WORLD_PATH (tests)")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="subject-similarity floor (default 0.6)")
    ap.add_argument("--max-bucket", type=int, default=60,
                    help="skip hub buckets larger than this; skips are counted")
    ap.add_argument("--class", dest="cls", default="",
                    choices=["", "contradiction", "near-duplicate", "unclassified"],
                    help="restrict the printed slate to one class")
    ap.add_argument("--top", type=int, default=15, help="slate cap for human output")
    args = ap.parse_args(argv)

    world = Path(args.world) if args.world else _world_path()
    try:
        records = load_active_guardrails(world)
    except StoreUnreadable as e:
        # Fail LOUD and non-zero. A silent empty result here would read as
        # "no conflicting guardrail pairs exist", which is the strongest
        # possible false all-clear this script can emit.
        print(f"[guardrail-pair-audit] STORE UNREADABLE: {e}", file=sys.stderr)
        return 2
    result = audit(records, args.threshold, args.max_bucket)

    rows = result["findings"]
    if args.cls:
        rows = [r for r in rows if r["class"] == args.cls]

    if args.output == "json":
        out = dict(result)
        out["findings"] = rows
        print(json.dumps(out))
        return 0

    print(f"[guardrail-pair-audit] active={result['active_guardrails']} "
          f"buckets={result['buckets']} compared={result['pairs_compared']} "
          f"findings={result['findings_total']} by_class={result['by_class']}")
    if result["oversized_buckets_skipped"]:
        print(f"  NOTE {result['oversized_buckets_skipped']} hub bucket(s) skipped "
              f"(> --max-bucket {args.max_bucket}) — the slate is bounded, "
              f"not exhaustive")
    shown = rows[:args.top]
    print(f"  showing {len(shown)} of {len(rows)} (--top {args.top}) — the cap "
          f"bounds the SLATE, never the scan")
    for r in shown:
        print(f"\n  [{r['class']}] sim={r['similarity']} "
              f"cat={r['category']} tok={r['shared_token']}")
        print(f"    {r['a_id']} ({r['a_polarity']}): {r['a_rule'][:150]}")
        print(f"    {r['b_id']} ({r['b_polarity']}): {r['b_rule'][:150]}")
    if not rows:
        print("  no pairs above threshold")
    print("\n  PROPOSAL ONLY. guard-3814: resolve a contradiction by finding the "
          "PRECONDITION that makes each side right (scope-split) — never by "
          "utilization counts, never by averaging.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
