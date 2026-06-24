#!/usr/bin/env python3
# domain-leak-exempt: framework encoding-parsimony infra — generic text similarity, no domain strings.
"""MDL gate — encode-time parsimony check (Phase 1c).

Part of the evaluative substrate; eval_harness.py is the keystone + in-code index of all seven.

WHY THIS EXISTS
---------------
The framework's learning gate FORCES an encoding on deep outcomes, and the fleet has accumulated
~1,623 reasoning-bank entries, ~573 guardrails, ~1,090 tree nodes. The research pass found a
Goodhart risk: encoding *volume* is optimized as a proxy for learning, but an entry that merely
restates an existing one adds no information and costs retrieval budget forever.

This module is a **principle-transfer**, not a reproduction of any one paper (full-text review,
Step 7). "Self-Revising Discovery Systems for Science" (2606.01444) is a *category-theory*
framework whose Minimum Description Length gate accepts a revision to a symbolic *world-model
law* only when it pays for its description length (it prefers expressing a phenomenon via a new
interaction *type* over an added *term*). We borrow the MDL *principle* — accept a self-revision
only if it earns its description-length cost — and apply it to a different object: the memory
store. The counter-principle becomes **keep a new entry only if it adds information rather than
paraphrasing what's already there.** `learning-routing.md` already says "Drop is a positive
choice"; this module gives that judgment a measurable basis so it isn't left to in-the-moment LLM
discretion under context pressure.

DESIGN (deliberately small)
---------------------------
Pure, hermetic, domain-free. Two questions a caller asks before encoding a candidate:
  1. Is it a near-duplicate of an existing entry?  -> `nearest()` (max token-set similarity).
  2. Does it add enough NEW information to be worth keeping?  -> `novelty()` (novel-token share).
`assess()` combines them into a keep/drop recommendation with a human-readable reason. This is
an ADVISORY signal (like the existing pre-apply consult gate) — it surfaces redundancy; the
caller (or the encoding gate) decides. It never blocks silently.

Similarity is lexical token-set Jaccard — intentionally simple and explainable (low cognitive
load). It is a *redundancy detector*, not a semantic-equivalence judge; that is the right tool
for "did I just paraphrase an existing entry," which is the bloat failure mode it targets.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Tokens shorter than this are dropped (articles, punctuation noise) so they
# don't inflate spurious overlap between unrelated entries.
_MIN_TOKEN_LEN = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Gate id for _gate_log telemetry; MUST match the entry in core/config/gates.yaml.
# Wired advisory into aspirations-state-update Step 8 c.6 (g-115-1468 / G4a).
GATE_ID = "mdl-encode-parsimony"


def tokenize(text: str) -> frozenset:
    """Lowercase alphanumeric token SET (order/count-insensitive by design —
    redundancy is about shared vocabulary, not word order)."""
    return frozenset(t for t in _TOKEN_RE.findall(str(text).lower())
                     if len(t) >= _MIN_TOKEN_LEN)


def jaccard(a: frozenset, b: frozenset) -> float:
    """|A∩B| / |A∪B|; 0 for two empty sets (no shared information)."""
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def nearest(candidate: str,
            existing: Sequence[Tuple[str, str]]) -> Tuple[Optional[str], float, str]:
    """Return (id, similarity, text) of the existing entry most similar to `candidate`.

    Returns (None, 0.0, "") when the corpus is empty OR when no entry shares any
    token with the candidate. The matched TEXT is returned alongside the id so the
    caller scores novelty against the exact entry that was matched — not a re-lookup
    that could resolve to the wrong record when ids are duplicated. Ties keep the
    earliest entry (strict `>`).
    """
    cand_tokens = tokenize(candidate)
    best_id, best_sim, best_text = None, 0.0, ""
    for ent_id, text in existing:
        sim = jaccard(cand_tokens, tokenize(text))
        if sim > best_sim:
            best_id, best_sim, best_text = ent_id, sim, text
    return best_id, best_sim, best_text


def novelty(candidate: str, nearest_text: str) -> float:
    """Share of candidate tokens NOT present in its nearest neighbour ([0,1]).

    1.0 = entirely new vocabulary; 0.0 = every token already appears in the
    neighbour (pure paraphrase / subset). Distinct from (1 - jaccard): novelty
    is asymmetric — a candidate that is a strict subset of a longer existing
    entry has novelty 0 even though jaccard < 1, and a subset adds nothing.
    """
    cand = tokenize(candidate)
    if not cand:
        return 0.0
    near = tokenize(nearest_text)
    return len(cand - near) / len(cand)


@dataclass
class Assessment:
    keep: bool
    max_similarity: float
    nearest_id: Optional[str]
    novelty: float
    reason: str

    def as_dict(self) -> dict:
        return {"keep": self.keep, "max_similarity": round(self.max_similarity, 4),
                "nearest_id": self.nearest_id, "novelty": round(self.novelty, 4),
                "reason": self.reason}


def assess(candidate: str, existing: Sequence[Tuple[str, str]],
           dup_threshold: float = 0.80, min_novelty: float = 0.20) -> Assessment:
    """Recommend keep/drop for a candidate memory entry.

    Drop recommendation when EITHER:
      - it is a near-duplicate (max similarity >= dup_threshold), OR
      - it adds too little new information (novelty vs. nearest < min_novelty).
    Otherwise keep. Thresholds are conservative defaults (favour keeping when in
    doubt — under-encoding loses signal once, but a near-duplicate is a clear
    drop). Tunable by the caller; the encoding gate can tighten them as the
    store grows.
    """
    if not (0.0 <= dup_threshold <= 1.0) or not (0.0 <= min_novelty <= 1.0):
        raise ValueError("dup_threshold and min_novelty must be in [0,1]")
    # An empty / stopword-only / punctuation-only candidate carries no information.
    # An anti-bloat gate must DROP it, not green-light it (it is the clearest noise).
    if not tokenize(candidate):
        return Assessment(False, 0.0, None, 0.0,
                          "empty or low-content candidate (no tokens >= 3 chars); drop")
    if not existing:
        return Assessment(True, 0.0, None, 1.0,
                          "no existing entries — nothing to be redundant with; keep")
    near_id, sim, near_text = nearest(candidate, existing)
    if near_id is None:
        # corpus is non-empty but the candidate shares no token with any entry —
        # maximally novel vocabulary, keep. (Distinct from the empty-corpus case
        # above so the reason never falsely claims "no existing entries".)
        return Assessment(True, 0.0, None, 1.0,
                          "no token overlap with any of the existing entries; keep (novel)")
    nov = novelty(candidate, near_text)
    if sim >= dup_threshold:
        return Assessment(False, sim, near_id, nov,
                          f"near-duplicate of {near_id} (similarity {sim:.2f} "
                          f">= {dup_threshold}) — restating an existing entry; drop")
    if nov < min_novelty:
        return Assessment(False, sim, near_id, nov,
                          f"low novelty vs {near_id} ({nov:.2f} < {min_novelty}) — "
                          f"adds little new information; drop or merge into {near_id}")
    return Assessment(True, sim, near_id, nov,
                      f"sufficiently novel (similarity {sim:.2f}, novelty {nov:.2f}); keep")


def run_assess(candidate: str, existing: Sequence[Tuple[str, str]], *,
               dup_threshold: float = 0.80, min_novelty: float = 0.20,
               caller: Optional[str] = None, node: Optional[str] = None,
               goal: Optional[str] = None) -> Assessment:
    """assess() + advisory _gate_log telemetry. Returns the Assessment unchanged.

    Logs the keep/drop recommendation to meta/gate-firings.jsonl under gate id
    `mdl-encode-parsimony` (decision pass=keep / block=drop). The telemetry
    import is LAZY so the pure assess() + the bare CLI stay dependency-light
    (no _gate_log / _paths needed for the core check or the self-contained
    `main`). Best-effort: a telemetry failure NEVER changes the recommendation
    — this is an ADVISORY parsimony signal, the curator gate stays authoritative.
    """
    a = assess(candidate, existing, dup_threshold=dup_threshold, min_novelty=min_novelty)
    try:
        import _gate_log
        _gate_log.log(GATE_ID, "pass" if a.keep else "block",
                      caller=caller or "mdl_gate.run_assess",
                      payload={"node": node, "goal": goal,
                               "dup_threshold": dup_threshold, "min_novelty": min_novelty},
                      extra=a.as_dict())
    except Exception:
        pass  # advisory telemetry is best-effort; never break the encode path
    return a


# --------------------------------------------------------------------------- #
# CLI — read a candidate string and an existing-corpus JSONL of {id, text}.
# --------------------------------------------------------------------------- #


def _load_existing(path) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        out.append((str(d.get("id", "")), str(d.get("text", ""))))
    return out


def _cli_gate(argv: Optional[List[str]] = None) -> int:
    """`gate` subcommand — instrumented parsimony check with _gate_log telemetry.

    Mirrors main() but routes through run_assess() so each invocation logs a
    pass/block firing under gate id `mdl-encode-parsimony`, and accepts the
    --node / --goal / --caller context that the telemetry payload records.
    This is the entry point the aspirations-state-update Step 8 c.6 advisory
    calls; main() stays the dependency-light bare check (no _gate_log import).
    """
    ap = argparse.ArgumentParser(
        prog="mdl_gate.py gate",
        description="Advisory MDL parsimony check with _gate_log telemetry. "
                    "exit 0 = keep, exit 1 = drop (advisory).")
    ap.add_argument("--candidate", required=True, help="candidate entry text")
    ap.add_argument("--existing", required=True, help="JSONL of {id, text}")
    ap.add_argument("--dup-threshold", type=float, default=0.80)
    ap.add_argument("--min-novelty", type=float, default=0.20)
    ap.add_argument("--node", default=None, help="target tree node key (telemetry context)")
    ap.add_argument("--goal", default=None, help="source goal id (telemetry context)")
    ap.add_argument("--caller", default=None, help="caller label (telemetry context)")
    args = ap.parse_args(argv)
    a = run_assess(args.candidate, _load_existing(args.existing),
                   dup_threshold=args.dup_threshold, min_novelty=args.min_novelty,
                   caller=args.caller, node=args.node, goal=args.goal)
    print(json.dumps(a.as_dict(), indent=2))
    return 0 if a.keep else 1  # exit 1 = "drop recommended" (shell-gateable)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MDL gate — encode-time parsimony check.")
    ap.add_argument("--candidate", required=True, help="candidate entry text")
    ap.add_argument("--existing", required=True,
                    help="JSONL of {id, text} for existing entries in the same category")
    ap.add_argument("--dup-threshold", type=float, default=0.80)
    ap.add_argument("--min-novelty", type=float, default=0.20)
    args = ap.parse_args(argv)
    a = assess(args.candidate, _load_existing(args.existing),
               dup_threshold=args.dup_threshold, min_novelty=args.min_novelty)
    print(json.dumps(a.as_dict(), indent=2))
    return 0 if a.keep else 1  # exit 1 = "drop recommended" (shell-gateable)


if __name__ == "__main__":
    # `gate` subcommand routes through run_assess (telemetry); the bare form
    # stays the dependency-light pure check via main().
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        sys.exit(_cli_gate(sys.argv[2:]))
    sys.exit(main())
