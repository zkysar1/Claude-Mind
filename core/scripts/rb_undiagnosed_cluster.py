#!/usr/bin/env python3
"""rb_undiagnosed_cluster.py — flag reasoning-bank clusters that share a symptom
while DISCLAIMING a mechanism (g-115-3289).

THE SIGNAL IS THE SHAPE OF THE SET, NOT ANY ONE ENTRY. Each entry in such a
cluster is individually correct, individually useful, and correctly encoded —
so no encode-time gate can catch this, and deduplication is the WRONG operation
(these are distinct incidents in different modules, not paraphrases). What the
store never says out loud is: N entries hit the same symptom, and N of them
independently admitted nobody knows why. That shape is strong evidence a cheap
root cause is sitting undiscovered while the fleet satisfices on a workaround.

OPPOSITE POLARITY TO `store_dupe_warn.py`, DELIBERATELY. That module warns at
ADD time that a new entry restates an existing one (near-verbatim duplication).
This one runs PERIODICALLY over the whole corpus and wants entries that are
*related but not duplicates*. Same primitive (`mdl_gate.tokenize/jaccard`),
opposite question. Neither subsumes the other.

TWO PREDICATES, and the second is deliberately scoped INSIDE the first:
  (a) >= `min_admissions` cluster members contain an undiagnosed-mechanism
      admission ("not diagnosed", "cause unknown", "inferred, not verified", ...).
  (b) >= 2 members assert mechanisms that share little vocabulary — candidate
      MUTUAL INCONSISTENCY. An invented mechanism is more dangerous than an
      admitted unknown, because it stops the next reader from looking.
Clusters are seeded from (a) and refined by (b) — which is not a shortcut but
the shape the source goal specifies: "Case (b) is cheap to spot once (a) has
clustered the entries: within a cluster, if two entries name different causes,
at most one can be right."

WHAT (b) DOES NOT DO — stated because a vacuous check that reads as a capability
is worse than no check (guard-1465). It does NOT prove two causal claims
contradict; token divergence is not contradiction. It flags CANDIDATES for a
human read. Two entries can name the same cause in disjoint words (the
guard-1486-vs-1485 class `store_dupe_warn` documents), and lexical methods miss
that in BOTH directions.

THE SIMILARITY BASIS IS SEMANTIC, AND THAT WAS FORCED BY MEASUREMENT. The
source goal proposed reusing the lexical nearest-neighbour helper "with the
opposite polarity, so it is small". That was built first and it does not work:
over all 3,081 admission-bearing seed pairs on the live corpus the lexical
title-jaccard MAXIMUM is 0.222, so every threshold that discriminates finds
nothing and the only one that finds anything returns a 34-member blob. The
embedding index gives the same pairs p99=0.497 / max=0.803 — real dynamic
range. Full numbers, and why calibrating from the source goal's 0.529 pair
would have shipped the vacuous version, are in `semantic_sim_provider`.

The lexical path is KEPT as an explicit fallback with its own threshold, and a
downgrade to it is announced on stderr rather than absorbed, because on this
corpus it cannot fire and a silent zero would read as "no clusters".

COST is bounded by SEEDING rather than by sampling. A naive all-pairs pass over
~9.4k active entries is ~44M comparisons; sampling to dodge that would
reintroduce the silent-miss this scan exists to remove. Instead: admission-
bearing entries are the seeds (~tens, measurable via --stats), linked pairwise,
then expanded once against the full corpus. That is O(seeds x corpus), and it is
EXACT for the stated predicate — every flaggable cluster contains at least
`min_admissions` admission-bearing members by definition, so no seed is missed.

REPORT-ONLY BY DEFAULT. `--apply` files one Investigate per cluster. The dedup
probe keys on a stable cluster id embedded in the goal DESCRIPTION, not the
title: `aspirations-query.sh --description-contains` is what the duplication
gate itself reads, and a title-only probe is NARROWER than the gate that
creates the population — the exact predicate-mismatch class that makes a sweep
report clean forever (reclaim-routed-work.md rule 7).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mdl_gate import jaccard, tokenize  # noqa: E402
# guard-580/guard-581: a bare "bash" argv[0] can resolve to the System32 WSL
# stub, and str(WindowsPath) yields backslashes that bash strips as escape
# introducers — silently producing a nonexistent script path. Never hand-build
# these argvs.
from _runtime_bash import bash_cmd  # noqa: E402

# Records in these states are not live knowledge. A resolved cluster is
# routinely CONSOLIDATED into a single root-cause entry and its members
# retired — flagging those would re-open settled work every run.
INACTIVE_STATUSES = {"retired", "superseded", "archived"}

# Phrases in which an author admits the mechanism is unknown. Kept small and
# literal on purpose: every addition widens the seed set, and a seed set that
# grows without measurement is how a targeted scan becomes a noise generator.
# Frequencies on the live corpus (2026-08-31, 9,428 records) are in --stats.
# A BARE `not established` IS DELIBERATELY ABSENT, and its removal is the single
# most load-bearing calibration in this file. Measured on the live corpus
# 2026-08-31: it matched 29 of 79 seeds — the largest contributor — and reading
# them showed it firing on ordinary methodology prose ("the invariant is not
# established by a source pin"), which is not a mechanism admission at all. An
# over-reporting instrument is as useless as an empty one, and it is worse here
# because the noise it adds is what a reader would have to disprove one entry at
# a time. The precise `mechanism not established` (4 hits) survives and carries
# the real signal.
ADMISSION_PATTERNS: Tuple[str, ...] = (
    r"not\s+diagnosed",
    r"not\s+root[-\s]caused",
    r"mechanism\s+not\s+established",
    r"cause\s+(?:is\s+)?unknown",
    r"root\s+cause\s+not\b",
    r"inferred,?\s+not\s+verified",
    r"\bquirk\b",
    r"unclear\s+(?:why|how)\b",
)

# Markers that introduce a CAUSAL claim. Used only for predicate (b), and only
# WITHIN an already-formed cluster.
CAUSE_PATTERNS: Tuple[str, ...] = (
    r"root\s+cause\s*[:\-]",
    r"caused\s+by",
    r"because\s+",
    r"the\s+mechanism\s+is",
    r"due\s+to\s+",
)

# The words that INTRODUCE a causal claim, as opposed to the words that make
# the claim. Excluded from cause-similarity — see `cause_claim_tokens`.
CAUSE_MARKER_TOKENS = frozenset({
    "root", "cause", "caused", "because", "mechanism", "due",
})

_ADMISSION_RE = re.compile("|".join(ADMISSION_PATTERNS), re.IGNORECASE)
_CAUSE_RE = re.compile("|".join(CAUSE_PATTERNS), re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Semantic cosine. Sits above the measured p99 of seed-pair similarity (0.497)
# so it stays rare, and below the genuine top tier (0.803, 0.777, 0.663, 0.630)
# so it can actually fire. See `semantic_sim_provider` for the full measurement
# and for why the lexical basis has no such workable value.
DEFAULT_THRESHOLD = 0.55
# The lexical fallback keeps its own value: on that basis nothing exceeds 0.222,
# so 0.55 there would be silently vacuous. Named separately rather than shared,
# because one threshold across two incomparable scales is how a fallback ships
# broken while the primary path looks tested.
LEXICAL_THRESHOLD = 0.20
DEFAULT_MIN_CLUSTER = 3
DEFAULT_MIN_ADMISSIONS = 2
# Two causal claims sharing less than this much vocabulary are DIVERGENT
# candidates. Sits below the cluster threshold by construction: members already
# agree on the symptom, so the question is only whether the causes differ.
DEFAULT_DIVERGENCE = 0.30
MAX_DIVERGENT_PAIRS = 10


class Record:
    """One active reasoning-bank entry, reduced to what this scan reads."""

    __slots__ = ("id", "title", "content", "category", "tokens",
                 "admissions", "cause_tokens")

    def __init__(self, rid: str, title: str, content: str, category: str):
        self.id = rid
        self.title = title
        self.content = content
        self.category = category
        self.tokens = tokenize(title)
        # Admissions may appear in EITHER field — a title routinely carries one.
        self.admissions = admission_hits(title + " " + content)
        # Causal claims are read from CONTENT ONLY, and the exclusion of `title`
        # is load-bearing rather than tidy. A title has no sentence terminator,
        # so concatenating it merges it into the first content sentence; if that
        # sentence asserts a cause, every title token is absorbed into the
        # causal claim. Those are precisely the SHARED SYMPTOM tokens that
        # clustered the entries together, so they inflate cause-similarity
        # across the board and suppress the divergence signal this field exists
        # to produce. Measured: two entries whose causal claims were "a stale
        # alpha handle" and "beta timeout entirely" scored 0.45 (not divergent)
        # with the title included and 0.10 (divergent) without it.
        self.cause_tokens = cause_claim_tokens(content)


def admission_hits(text: str) -> List[str]:
    """Every distinct undiagnosed-mechanism admission phrase present in `text`."""
    if not text:
        return []
    seen: List[str] = []
    for m in _ADMISSION_RE.finditer(text):
        phrase = m.group(0).strip().lower()
        if phrase not in seen:
            seen.append(phrase)
    return seen


def cause_claim_tokens(text: str) -> frozenset:
    """Token set of the SENTENCES that assert a cause.

    Whole-record tokens would be dominated by shared symptom vocabulary — which
    is exactly what clustered these entries together — so comparing them would
    make every cluster look internally consistent. Restricting to causal
    sentences is what lets divergence show.
    """
    if not text:
        return frozenset()
    parts: List[str] = []
    for sent in _SENTENCE_SPLIT.split(text):
        if _CAUSE_RE.search(sent):
            parts.append(sent)
    if not parts:
        return frozenset()
    # Subtract the MARKER vocabulary. "root cause", "caused by" and friends
    # appear in every causal claim by construction, so leaving them in floors
    # pairwise similarity at a non-zero value and biases the comparison toward
    # "these agree" — systematically, and in the direction that HIDES findings.
    # The effect scales inversely with claim length, so it is worst on the
    # terse claims that are hardest to read and most worth flagging.
    return frozenset(tokenize(" ".join(parts))) - CAUSE_MARKER_TOKENS


def load_active(path: Path) -> List[Record]:
    """Every ACTIVE record. Malformed lines are skipped — one bad line must not
    silence the whole scan (mirrors store_dupe_warn.load_corpus)."""
    out: List[Record] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if str(d.get("status", "")).lower() in INACTIVE_STATUSES:
            continue
        title = str(d.get("title") or "")
        if not title.strip():
            continue
        out.append(Record(str(d.get("id") or ""), title,
                          str(d.get("content") or ""),
                          str(d.get("category") or "")))
    return out


def _union(parent: Dict[str, str], a: str, b: str) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[rb] = ra


def _find(parent: Dict[str, str], a: str) -> str:
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


def lexical_sim(a: Record, b: Record) -> float:
    """Token-set jaccard over TITLES. Retained as the fallback basis, and
    MEASURED INADEQUATE for this detector's own purpose — see `semantic_sim`."""
    return jaccard(a.tokens, b.tokens)


def semantic_sim_provider(records: Sequence[Record]):
    """Cosine similarity from the shared embedding index, or None if unavailable.

    WHY THIS IS THE DEFAULT BASIS AND LEXICAL IS NOT. Measured on the live
    corpus 2026-08-31 over every pair of admission-bearing seeds:

        lexical  title-jaccard : p50=0.030 p90=0.071 p99=0.128 max=0.222
        semantic cosine        : p50=0.241 p90=0.373 p99=0.497 max=0.803

    The lexical range is CRUSHED — its maximum over all 3,081 seed pairs is
    0.222, so every threshold at or above 0.25 finds nothing and the only
    threshold that finds anything (0.20) sits BELOW the store's own median
    nearest-neighbour noise floor and returns a 34-member blob. There is no
    lexical threshold that both fires and discriminates. That is not a tuning
    problem, it is the guard-1486-vs-1485 class `store_dupe_warn.py` already
    documented: entries hitting one symptom in different modules describe it in
    almost disjoint vocabulary, which is exactly what token overlap cannot see.
    Calibrating from the 0.529 pair in the source goal would have shipped the
    vacuous version — that pair is a near-DUPLICATE, and this detector's whole
    premise is that its targets are NOT duplicates.

    Reads the index MATRIX directly rather than calling `cosine_scores`: the
    vectors for these records are already stored, so no model load and no
    re-encode is needed, and the comparison is exact.
    """
    try:
        import numpy as np
        import _embedding_retrieval as er
        emb, ids, _meta = er._load_index(er._resolve_index_dir(None))
    except Exception:  # noqa: BLE001 — absent index/numpy is a supported state
        return None
    if emb is None or not ids:
        return None
    pos = {rid: i for i, rid in enumerate(ids)}
    rows = {r.id: pos[r.id] for r in records if r.id in pos}
    if not rows:
        return None
    mat = np.asarray(emb, dtype="float32")
    norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9

    def sim(a: Record, b: Record) -> float:
        ia, ib = rows.get(a.id), rows.get(b.id)
        if ia is None or ib is None:
            # Not indexed — fall back rather than silently scoring 0, which
            # would read as "unrelated" instead of "unmeasured".
            return lexical_sim(a, b)
        va, vb = mat[ia] / norms[ia], mat[ib] / norms[ib]
        return float(va @ vb)

    return sim


def build_clusters(records: Sequence[Record],
                   threshold: float = DEFAULT_THRESHOLD,
                   min_admissions: int = DEFAULT_MIN_ADMISSIONS,
                   min_cluster: int = DEFAULT_MIN_CLUSTER,
                   sim=None) -> List[List[Record]]:
    """Symptom clusters seeded on admission-bearing entries.

    EXACTNESS ARGUMENT for the seeding: a flaggable cluster needs
    >= min_admissions admission-bearing members, so linking the seeds to each
    other cannot miss a qualifying cluster's backbone. The expansion pass then
    pulls in non-admitting members that share the symptom, which is what makes
    the reported member list complete rather than admission-only.
    """
    if sim is None:
        sim = lexical_sim
    seeds = [r for r in records if r.admissions and r.tokens]
    if len(seeds) < min_admissions:
        return []

    parent = {r.id: r.id for r in seeds}
    for i, a in enumerate(seeds):
        for b in seeds[i + 1:]:
            if sim(a, b) >= threshold:
                _union(parent, a.id, b.id)

    groups: Dict[str, List[Record]] = {}
    for r in seeds:
        groups.setdefault(_find(parent, r.id), []).append(r)

    clusters: List[List[Record]] = []
    for root, members in groups.items():
        if len(members) < min_admissions:
            continue
        member_ids = {m.id for m in members}
        expanded = list(members)
        for cand in records:
            if cand.id in member_ids or not cand.tokens:
                continue
            # ALL, not ANY. `any` is single-linkage and it CHAINS: measured on
            # the live corpus it grew a 2-seed group into a 70-member cluster
            # with an EMPTY shared-symptom set — the emptiness being the tell
            # that the members no longer share anything. Requiring similarity to
            # every seed keeps the cluster to entries that genuinely share the
            # symptom, which is what the predicate claims.
            if all(sim(cand, m) >= threshold for m in members):
                expanded.append(cand)
                member_ids.add(cand.id)
        if len(expanded) >= min_cluster:
            clusters.append(sorted(expanded, key=lambda r: r.id))
    return clusters


def divergent_causes(cluster: Sequence[Record],
                     divergence: float = DEFAULT_DIVERGENCE
                     ) -> List[Tuple[str, str, float]]:
    """Pairs within `cluster` whose CAUSAL sentences share little vocabulary.

    Candidates for mutual inconsistency, NOT a proof of it (see module docstring).
    """
    bearing = [r for r in cluster if r.cause_tokens]
    out: List[Tuple[str, str, float]] = []
    for i, a in enumerate(bearing):
        for b in bearing[i + 1:]:
            s = jaccard(a.cause_tokens, b.cause_tokens)
            if s < divergence:
                out.append((a.id, b.id, round(s, 4)))
    # Most-divergent first and CAPPED: this is quadratic in cluster size, and an
    # uncapped list buries the finding under its own evidence (measured: one
    # cluster emitted hundreds of pairs, none readable). The cap is a REPORTING
    # bound only — `len` of the full set is reported separately by the caller.
    out.sort(key=lambda t: t[2])
    return out[:MAX_DIVERGENT_PAIRS]


def cluster_key(cluster: Sequence[Record]) -> str:
    """Stable id for a cluster, order-independent and content-free.

    Derived from the sorted MEMBER IDS so the same cluster yields the same key
    across runs — that is what makes --apply idempotent. It shifts if the
    membership shifts, which is correct: a materially different cluster is a
    different finding and deserves its own Investigate.
    """
    joined = ",".join(sorted(r.id for r in cluster))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def shared_symptom(cluster: Sequence[Record], limit: int = 12) -> List[str]:
    """Tokens common to EVERY member's title — the symptom they share."""
    common: Optional[Set[str]] = None
    for r in cluster:
        common = set(r.tokens) if common is None else (common & set(r.tokens))
    return sorted(common)[:limit] if common else []


def assess(records: Sequence[Record], *,
           threshold: float = DEFAULT_THRESHOLD,
           min_cluster: int = DEFAULT_MIN_CLUSTER,
           min_admissions: int = DEFAULT_MIN_ADMISSIONS,
           divergence: float = DEFAULT_DIVERGENCE,
           sim=None) -> List[dict]:
    """Flagged clusters, richest first."""
    findings = []
    for cluster in build_clusters(records, threshold, min_admissions,
                                  min_cluster, sim):
        admitting = [r for r in cluster if r.admissions]
        if len(admitting) < min_admissions:
            continue
        divergences = divergent_causes(cluster, divergence)
        total_divergent = sum(
            1
            for i, a in enumerate([r for r in cluster if r.cause_tokens])
            for b in [r for r in cluster if r.cause_tokens][i + 1:]
            if jaccard(a.cause_tokens, b.cause_tokens) < divergence
        )
        findings.append({
            "divergent_pairs_total": total_divergent,
            "divergent_pairs_shown": len(divergences),
            "cluster_id": cluster_key(cluster),
            "members": [r.id for r in cluster],
            "size": len(cluster),
            "admitting": [
                {"id": r.id, "phrases": r.admissions, "title": r.title[:160]}
                for r in admitting
            ],
            "shared_symptom_tokens": shared_symptom(cluster),
            "divergent_cause_pairs": divergences,
            "predicates": (["a-undiagnosed-admissions"]
                           + (["b-divergent-cause-claims"] if divergences else [])),
        })
    findings.sort(key=lambda f: (-len(f["admitting"]), -f["size"]))
    return findings


# ---------------------------------------------------------------------------
# Filing (--apply). Report-only is the default; nothing below runs otherwise.
# ---------------------------------------------------------------------------

def _scripts_dir() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__)))


def already_filed(cluster_id: str) -> bool:
    """True if an Investigate for this cluster already exists.

    Probes the DESCRIPTION, not the title: the duplication gate that would
    refuse the filing reads descriptions, so a title-only probe is narrower
    than the gate and differently-worded siblings look novel.
    """
    marker = "[rb-cluster:%s]" % cluster_id
    try:
        res = subprocess.run(
            bash_cmd(_scripts_dir() / "aspirations-query.sh",
                     "--description-contains", marker),
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        # Unknown, and the fail-safe direction is "assume filed": a missed
        # Investigate costs one cycle, a duplicate pollutes the queue.
        return True
    if res.returncode != 0:
        return True
    body = (res.stdout or "").strip()
    return bool(body) and body not in ("[]", "[ ]")


def file_investigate(finding: dict, aspiration: str = "asp-115") -> Tuple[bool, str]:
    """File ONE Investigate for a cluster. Returns (filed, detail)."""
    cid = finding["cluster_id"]
    if already_filed(cid):
        return False, "already filed for cluster %s" % cid
    members = ", ".join(finding["members"])
    symptom = " ".join(finding["shared_symptom_tokens"]) or "(no common title tokens)"
    admissions = "; ".join(
        "%s: %s" % (a["id"], ", ".join(a["phrases"])) for a in finding["admitting"]
    )
    desc = (
        "[rb-cluster:%s] %d reasoning-bank entries share a symptom while %d of them "
        "independently DISCLAIM the mechanism. Each entry is individually correct — "
        "the signal is the shape of the set, not any one entry, so no encode-time "
        "gate could have caught it.\n\n"
        "MEMBERS: %s\nSHARED SYMPTOM TOKENS: %s\nADMISSIONS: %s\n"
        % (cid, finding["size"], len(finding["admitting"]), members, symptom, admissions)
    )
    if finding["divergent_cause_pairs"]:
        pairs = ", ".join("%s/%s (cause-jaccard %.2f)" % p
                          for p in finding["divergent_cause_pairs"])
        desc += (
            "\nDIVERGENT CAUSE CLAIMS (candidates, NOT proven contradictions — "
            "token divergence is not contradiction): %s\nWithin one symptom "
            "cluster, entries naming different causes cannot all be right, and an "
            "invented mechanism is more dangerous than an admitted unknown because "
            "it stops the next reader looking.\n" % pairs
        )
    desc += (
        "\nFiled by core/scripts/rb_undiagnosed_cluster.py (g-115-3289). "
        "Similarity is LEXICAL token-set jaccard over TITLES; it cannot see two "
        "entries describing one cause in disjoint vocabulary. Verify by reading "
        "the members before acting."
    )
    body = {
        "title": "Investigate: %d reasoning-bank entries share a symptom with no "
                 "established mechanism [rb-cluster:%s]" % (finding["size"], cid),
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-architecture",
        "description": desc,
        "origin_signal": "detector:rb-undiagnosed-cluster",
    }
    try:
        res = subprocess.run(
            bash_cmd(_scripts_dir() / "aspirations-add-goal.sh", aspiration),
            input=json.dumps(body), capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "filing failed: %s" % exc
    if res.returncode != 0:
        return False, "filing refused: %s" % (res.stderr or res.stdout or "").strip()[:400]
    return True, "filed for cluster %s" % cid


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flag reasoning-bank clusters that share a symptom while "
                    "disclaiming a mechanism (g-115-3289).")
    ap.add_argument("--store", help="path to reasoning-bank.jsonl "
                                    "(default: resolved WORLD_PATH)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="default depends on --basis: %.2f semantic, %.2f lexical"
                         % (DEFAULT_THRESHOLD, LEXICAL_THRESHOLD))
    ap.add_argument("--basis", choices=("semantic", "lexical"), default="semantic",
                    help="similarity basis; semantic falls back to lexical (and "
                         "SAYS SO) when the embedding index is unavailable")
    ap.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER)
    ap.add_argument("--min-admissions", type=int, default=DEFAULT_MIN_ADMISSIONS)
    ap.add_argument("--divergence", type=float, default=DEFAULT_DIVERGENCE)
    ap.add_argument("--aspiration", default="asp-115")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stats", action="store_true",
                    help="corpus + seed counts (the positive control for a zero)")
    ap.add_argument("--apply", action="store_true",
                    help="file one Investigate per cluster (default: report only)")
    args = ap.parse_args(argv)

    if args.store:
        store = Path(args.store)
    else:
        try:
            from _paths import WORLD_DIR  # type: ignore
            store = Path(WORLD_DIR) / "reasoning-bank.jsonl"
        except Exception:  # noqa: BLE001 — mirrors store_dupe_warn's resolution
            print("WORLD_DIR unresolvable; pass --store", file=sys.stderr)
            return 2

    records = load_active(store)
    if not records:
        print("no active records read from %s — this is NOT 'no clusters found'"
              % store, file=sys.stderr)
        return 2

    sim = semantic_sim_provider(records) if args.basis == "semantic" else None
    basis = "semantic" if sim is not None else "lexical"
    if args.basis == "semantic" and sim is None:
        # Never let a degraded basis pass as the requested one: the lexical path
        # cannot fire on this corpus, so a silent downgrade would turn a real
        # capability into a permanent, invisible zero.
        print("embedding index unavailable — DOWNGRADED to the lexical basis, "
              "which is measured incapable of firing on this corpus; a zero "
              "below is NOT evidence of no clusters", file=sys.stderr)
    threshold = args.threshold
    if threshold is None:
        threshold = DEFAULT_THRESHOLD if basis == "semantic" else LEXICAL_THRESHOLD

    findings = assess(records, threshold=threshold,
                      min_cluster=args.min_cluster,
                      min_admissions=args.min_admissions,
                      divergence=args.divergence, sim=sim)

    # The positive control for a zero: a bare "0 clusters" is indistinguishable
    # from a broken predicate, so the seed count and basis always accompany it.
    seeds = [r for r in records if r.admissions]
    stats = {"active_records": len(records), "admission_bearing": len(seeds),
             "threshold": threshold, "basis": basis, "clusters": len(findings)}

    if args.json:
        print(json.dumps({"stats": stats, "findings": findings}, indent=2))
    else:
        print("active=%d  admission-bearing seeds=%d  basis=%s  threshold=%.2f  "
              "clusters=%d"
              % (stats["active_records"], stats["admission_bearing"],
                 stats["basis"], stats["threshold"], stats["clusters"]))
        if args.stats:
            counts: Dict[str, int] = {}
            for r in seeds:
                for p in r.admissions:
                    counts[p] = counts.get(p, 0) + 1
            for phrase, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print("    %-30s %d" % (phrase, n))
        for f in findings:
            print("\n[%s] %d members, %d admitting  predicates=%s"
                  % (f["cluster_id"], f["size"], len(f["admitting"]),
                     ",".join(f["predicates"])))
            print("    symptom: %s" % " ".join(f["shared_symptom_tokens"]))
            for a in f["admitting"]:
                print("    %-10s %s  <- %s"
                      % (a["id"], a["title"][:100], ", ".join(a["phrases"])))
            for p in f["divergent_cause_pairs"]:
                print("    divergent causes: %s/%s (jaccard %.2f)" % p)

    if args.apply:
        for f in findings:
            filed, detail = file_investigate(f, args.aspiration)
            print("%s %s" % ("FILED  " if filed else "SKIP   ", detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
