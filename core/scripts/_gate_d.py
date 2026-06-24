#!/usr/bin/env python3
# domain-leak-exempt: Gate D experiment seam — references the ratified methodology
# path, the ayoai/zds world identifiers, and the GATE_D_* flag namespace by design
# (functional experiment constants + pre-registration provenance pointer).
"""_gate_d.py — Gate D commons-pattern injection: assignment + retrieval.

Gate D experiment seam (DORMANT until omni flips GATE_D_ENABLED — DEFAULT OFF).
Binding spec: Zak-Data-Solutions-Mind agents/omni/reports/gate-d-methodology-2026-06-10.md
(RATIFIED 2026-06-10; R1-R9 binding over panel text). Built to Section 2
(assignment), Section 4 (seam), Section 4.5 (retrieval), Section 4.6 (telemetry),
Section 9.3 (artifacts), Appendix B (frozen constants).

GATE-INTEGRITY (methodology 9.5 / DOF-4): once omni blesses the diff, no agent
self-modifies the assignment or flag logic. The constants below are pre-registered
and FROZEN; changing any voids the experiment and requires a restart.

Reads goal-id / goal-text / category, computes the deterministic FNV-1a arm
assignment, and for arm B ranks a frozen cross-world commons corpus by token
overlap, returning the top-K=5 patterns (capped at 2000 tokens). Arm A is a
complete no-op (SEAM-1). Emits ONE JSON object on stdout for gate-d-inject.sh.

DEFAULT-OFF behavior: with no GATE_D_CORPUS_PATH (the corpus is frozen only at
flag-flip), arm B returns status="no_patterns". The script never raises to the
caller — on any error it emits {"arm": <hash-arm or "A">, "status": "error",
"patterns": []} so the seam fails closed and logs an E7 exclusion.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# --- Pre-registered constants (Appendix B — FROZEN, do NOT edit post-bless) ---
GATE_D_SALT = 0x47443034        # ASCII "GD04"
FNV_OFFSET_BASIS = 0x811C9DC5   # FNV-1a 32-bit offset basis
FNV_PRIME = 0x01000193          # FNV-1a 32-bit prime
TOP_K = 5                       # patterns per B-arm injection
TOKEN_CAP = 2000                # max tokens of injected pattern context
EXPERIMENT_VERSION = "gate-d-v1"

# Gate C tokenizer parity (methodology §4.5: lowercase, 3+ char tokens, stopword
# removal). The B-arm ranking depends on internal consistency — the SAME tokenizer
# tokenizes goal text and every pattern — not on byte-identity with Gate C's TS
# verdict module (a different repo whose verdict is already computed).
_STOPWORDS = {
    "the", "and", "for", "are", "was", "were", "with", "this", "that", "from",
    "have", "has", "had", "not", "but", "all", "any", "can", "his", "her",
    "its", "our", "out", "you", "your", "they", "them", "then", "than", "into",
    "per", "use", "used", "uses", "will", "would", "should", "could", "when",
    "what", "which", "who", "how", "why", "where", "been", "being", "does",
    "did", "done", "each", "more", "most", "some", "such", "only", "also",
    "over", "under", "after", "before", "between", "about",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def assign_arm(goal_id):
    """FNV-1a 32-bit salted parity assignment (methodology Section 2, EXACT).

    input = goalId + ':' + hex(GATE_D_SALT without 0x prefix); the parity bit of
    the 32-bit hash selects the arm. Deterministic (no wall-clock, no RNG), stable
    across retries (keyed on goal-id, not execution instance), auditable from the
    published salt. Mirrors the canonical TypeScript exactly: Python's
    `(h * prime) & 0xFFFFFFFF` equals JS `Math.imul(h, prime) >>> 0`.
    Returns (arm, hash_int).
    """
    h = FNV_OFFSET_BASIS
    inp = goal_id + ":" + format(GATE_D_SALT, "x")   # e.g. "g-003-42:47443034"
    for ch in inp:
        h ^= ord(ch)
        h = (h * FNV_PRIME) & 0xFFFFFFFF              # FNV prime; unsigned 32-bit
    arm = "A" if (h & 1) == 0 else "B"
    return arm, h


def tokenize(text):
    """Lowercase, alphanumeric tokens of length >= 3, stopwords removed."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower())
            if len(t) >= 3 and t not in _STOPWORDS}


def _generalized(rec):
    """The injectable pattern content lives under rec['generalized'] (launch-inventory
    schema, omni bless amendment 3 2026-06-11: cross_domain serves generalized-only per
    M4 g-325-03; the top-level original.* fields are zds-verbatim and MUST NOT inject).
    'approach' is a list of steps in the corpus — join to one string."""
    g = rec.get("generalized") or {}
    approach = g.get("approach", "")
    if isinstance(approach, list):
        approach = " ".join(str(a) for a in approach)
    return str(g.get("context", "")), str(approach), str(g.get("lesson", ""))


def _signature(rec):
    """Stable per-record signature for telemetry/earnings join: world:kind:id."""
    return "%s:%s:%s" % (rec.get("world", ""), rec.get("kind", ""), rec.get("id", ""))


def _pattern_text(rec):
    return " ".join(_generalized(rec))


def _approx_tokens(text):
    # ~4 chars/token heuristic for the 2000-token injection cap (§4.5).
    return max(1, len(text) // 4)


def _load_corpus(path):
    """Read the frozen commons corpus JSONL. Returns a list of pattern dicts.

    Empty/missing path -> [] (DORMANT default; arm B then returns no_patterns).
    Malformed lines are skipped, not fatal.
    """
    if not path or not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _emit(obj):
    sys.stdout.write(json.dumps(obj))


def _base_record(arm, h):
    return {
        "arm": arm,
        "assignment_hash": "0x%08X" % h,
        "patterns": [],
        "patterns_injected": 0,
        "pattern_signatures": [],
        "injection_tokens": 0,
        "retrieval_precision": 0.0,
        "corpus_size": 0,
        "corpus_source": "",
        "experiment_version": EXPERIMENT_VERSION,
    }


def main():
    ap = argparse.ArgumentParser(description="Gate D arm assignment + injection")
    ap.add_argument("--goal-id", required=True)
    ap.add_argument("--goal-text", default="")
    ap.add_argument("--category", default="")
    args = ap.parse_args()

    arm, h = assign_arm(args.goal_id)
    base = _base_record(arm, h)

    # Arm A: complete no-op control (SEAM-1) — no corpus read, no token cost.
    if arm == "A":
        base["status"] = "control"
        _emit(base)
        return

    # Arm B: rank the frozen corpus by token overlap; take top-K; cap tokens.
    corpus_path = os.environ.get("GATE_D_CORPUS_PATH", "")
    corpus = _load_corpus(corpus_path)
    base["corpus_size"] = len(corpus)
    base["corpus_source"] = os.path.basename(corpus_path) if corpus_path else ""

    goal_tokens = tokenize(args.goal_text + " " + args.category)
    if not corpus or not goal_tokens:
        base["status"] = "no_patterns"
        _emit(base)
        return

    scored = []
    for rec in corpus:
        ptoks = tokenize(_pattern_text(rec))
        overlap = len(goal_tokens & ptoks)
        if overlap > 0:
            scored.append((overlap, rec, ptoks))
    # Deterministic order: overlap desc, then signature asc (no wall-clock tie-break).
    scored.sort(key=lambda t: (-t[0], _signature(t[1])))

    selected, sigs, union_ptoks, used_tokens = [], [], set(), 0
    for overlap, rec, ptoks in scored:
        ntok = _approx_tokens(_pattern_text(rec))
        if used_tokens + ntok > TOKEN_CAP:
            continue                      # try the next-ranked pattern that fits
        ctx, approach, lesson = _generalized(rec)
        selected.append({
            "context": ctx,
            "approach": approach,
            "lesson": lesson,
            "signature": _signature(rec),
        })
        sigs.append(_signature(rec))
        union_ptoks |= ptoks
        used_tokens += ntok
        if len(selected) >= TOP_K:
            break

    if not selected:
        base["status"] = "no_patterns"
        _emit(base)
        return

    base["status"] = "injected"
    base["patterns"] = selected
    base["patterns_injected"] = len(selected)
    base["pattern_signatures"] = sigs
    base["injection_tokens"] = used_tokens
    base["retrieval_precision"] = round(len(goal_tokens & union_ptoks) / len(goal_tokens), 4)
    _emit(base)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — fail-closed; never raise to the seam
        # §9.3 contract: on error the seam treats the record as a control no-op
        # (arm A, excluded as E7). Emit arm A uniformly so _gate_d.py and the
        # gate-d-inject.sh wrapper agree on the error shape.
        sys.stdout.write(json.dumps({
            "arm": "A", "status": "error", "patterns": [], "error": str(exc)[:200],
        }))
