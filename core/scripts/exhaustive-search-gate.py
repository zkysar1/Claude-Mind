#!/usr/bin/env python3
"""Exhaustive-Search Gate — automated guard against premature negative conclusions.

Enforces the `core/config/conventions/exhaustive-search-before-negation.md`
protocol at the mechanical floor: a claim that something "isn't built,"
"doesn't exist," or "can't be done" must be backed by >= 2 tiers of search
evidence. Without sufficient evidence, the claim is blocked unless the caller
supplies `--override "<justification>"`.

Extends the capability-gate pattern (rb-229): caller asserts intent, gate
verifies against evidence. See g-115-45 / rb-232 for the broader pattern
(build agent-side gate, spawn user follow-up for enforcement wire-up).

Tier taxonomy (matches retrieval-escalation.md):
  - tree    : knowledge tree lookups (retrieve.sh, tree-find-node.sh, tree-read.sh)
  - adjacent: reasoning-bank / guardrails / pattern-signatures / experience search
  - codebase: Grep / Glob / Read on the primary workspace
  - web     : WebSearch / WebFetch (assistant+autonomous only)

Evidence sources, in priority order:
  1. Explicit flags: --tiers-used "tree,codebase" + --queries-count N
  2. Retrieval manifest at <agent>/session/retrieval-session.json, if present
  3. Default: zero tiers, zero queries (fail-closed toward blocking a claim,
     but caller can always set --override with audit justification)

Design notes:
- Fail-open everywhere for file/parse errors — a broken gate must never
  silently suppress legitimate output. Exceptions map to `gate_error` in the
  JSON result and a `would_block=false` verdict.
- Trigger-phrase detection uses a compiled regex set. If the claim doesn't
  match any trigger, the gate is a no-op (would_block=false). This keeps the
  gate cheap to call on arbitrary output.
- Whole-token matching for tier normalization prevents "treecategory" from
  registering as a tree hit.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from _gate_log import log as _gate_log
from _paths import agent_dir as _agent_dir

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

VALID_TIERS = ("tree", "adjacent", "codebase", "web")

# Phrases from exhaustive-search-before-negation.md "Trigger Phrases" section.
# Case-insensitive substring match. Keep this list conservative; false
# positives (gate fires when no claim was made) cost the caller an override
# but don't break correctness.
_TRIGGER_PATTERNS = [
    re.compile(r"\bisn't built\b", re.IGNORECASE),
    re.compile(r"\bis not built\b", re.IGNORECASE),
    re.compile(r"\bthere's no way\b", re.IGNORECASE),
    re.compile(r"\bthere is no way\b", re.IGNORECASE),
    re.compile(r"\bcan't be done\b", re.IGNORECASE),
    re.compile(r"\bcannot be done\b", re.IGNORECASE),
    re.compile(r"\bno existing (implementation|support|way)\b", re.IGNORECASE),
    re.compile(r"\bwe'd need to build\b", re.IGNORECASE),
    re.compile(r"\bwe would need to build\b", re.IGNORECASE),
    re.compile(r"\bnot possible\b", re.IGNORECASE),
    re.compile(r"\bdoesn't (exist|support|have)\b", re.IGNORECASE),
    re.compile(r"\bdoes not (exist|support|have)\b", re.IGNORECASE),
    re.compile(r"\bisn't available\b", re.IGNORECASE),
    re.compile(r"\bis not available\b", re.IGNORECASE),
    re.compile(r"\bno such (file|entry|node|record|thing)\b", re.IGNORECASE),
    re.compile(r"\bwe don't have\b", re.IGNORECASE),
]


def _read_local_paths_conf(agent_name):
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out = {}
    if not conf.is_file():
        return out
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _resolve_agent_dir():
    """Return <agent>/ path under PROJECT_ROOT, or None if not resolvable."""
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    # Defense: no path traversal via agent name.
    if any(c in agent for c in ("/", "\\", "\n", "\r", " ")) or ".." in agent:
        return None
    d = _agent_dir(agent)
    return d if d.is_dir() else None


def _load_retrieval_manifest(agent_dir):
    """Best-effort read of <agent>/session/retrieval-session.json.

    Returns dict with `tiers_used` (list) and `queries_count` (int) keys,
    or empty dict if unavailable. Never raises — fail-open is the contract.
    """
    if agent_dir is None:
        return {}
    path = agent_dir / "session" / "retrieval-session.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    tiers = data.get("tiers_used")
    if isinstance(tiers, list):
        # Normalize to known tier names; drop unknown entries rather than
        # counting them as valid evidence.
        norm = [str(t).strip().lower() for t in tiers]
        out["tiers_used"] = [t for t in norm if t in VALID_TIERS]
    qc = data.get("queries_count")
    if isinstance(qc, int):
        out["queries_count"] = qc
    elif isinstance(data.get("queries"), list):
        out["queries_count"] = len(data["queries"])
    return out


def _parse_tiers_flag(raw):
    """Split a comma-separated tiers string; drop unknown entries."""
    if not raw:
        return []
    parts = [p.strip().lower() for p in raw.split(",")]
    return [p for p in parts if p in VALID_TIERS]


def _detect_trigger(claim_text):
    """Return the first trigger pattern that matches, or None."""
    if not claim_text:
        return None
    for pat in _TRIGGER_PATTERNS:
        m = pat.search(claim_text)
        if m:
            return m.group(0)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=(
            "Exhaustive-search gate — block premature negative conclusions "
            "when insufficient search evidence exists."
        )
    )
    ap.add_argument("--claim-text", required=True,
                    help="The negation being made (scanned for trigger phrases).")
    ap.add_argument("--tiers-used", default="",
                    help="Comma-separated tiers attempted (tree,adjacent,codebase,web). "
                         "If empty, the gate reads the retrieval manifest.")
    ap.add_argument("--queries-count", type=int, default=None,
                    help="Number of distinct tier-1 queries attempted. "
                         "If unset, read from retrieval manifest.")
    ap.add_argument("--min-tiers", type=int, default=2,
                    help="Minimum tiers required to support a negation (default: 2).")
    ap.add_argument("--min-queries", type=int, default=3,
                    help="Minimum distinct tier-1 queries when tree is the only tier "
                         "(default: 3; matches convention protocol step 1).")
    ap.add_argument("--override",
                    help="Justification for bypassing the gate; echoed to stderr.")
    ap.add_argument("--output", default="json", choices=["json", "human"],
                    help="Output format.")
    args = ap.parse_args(argv)

    result = {
        "claim_text": args.claim_text,
        "trigger_matched": None,
        "tiers_used": [],
        "queries_count": 0,
        "evidence_source": "none",
        "min_tiers": args.min_tiers,
        "min_queries": args.min_queries,
        "override_applied": args.override,
        "would_block": False,
        "reason": "",
        "gate_error": None,
    }

    try:
        # 1. Trigger detection: no trigger -> no-op, caller proceeds.
        trigger = _detect_trigger(args.claim_text)
        result["trigger_matched"] = trigger
        if not trigger:
            result["reason"] = (
                "Claim does not match any negative-conclusion trigger phrase; "
                "gate is a no-op."
            )
            return _emit(result, args.output, 0, decision="noop")

        # 2. Evidence gathering — explicit flags take precedence.
        explicit_tiers = _parse_tiers_flag(args.tiers_used)
        if explicit_tiers or args.queries_count is not None:
            result["tiers_used"] = explicit_tiers
            result["queries_count"] = args.queries_count or 0
            result["evidence_source"] = "explicit"
        else:
            manifest = _load_retrieval_manifest(_resolve_agent_dir())
            result["tiers_used"] = manifest.get("tiers_used", [])
            result["queries_count"] = manifest.get("queries_count", 0)
            result["evidence_source"] = "manifest" if manifest else "none"

        # 3. Evaluate sufficiency.
        tier_count = len(set(result["tiers_used"]))
        enough_tiers = tier_count >= args.min_tiers
        enough_queries = (
            tier_count >= 1
            and result["queries_count"] >= args.min_queries
        )
        # Either breadth (>=2 tiers) OR depth (>=3 queries in >=1 tier) satisfies.
        sufficient = enough_tiers or enough_queries

        if sufficient:
            result["reason"] = (
                f"Negation claim is supported by search evidence "
                f"(tiers={tier_count}/{args.min_tiers}, "
                f"queries={result['queries_count']}/{args.min_queries})."
            )
            return _emit(result, args.output, 0, decision="pass")

        if args.override:
            result["reason"] = (
                f"Insufficient evidence (tiers={tier_count}, "
                f"queries={result['queries_count']}) but override supplied: "
                f"{args.override}"
            )
            # Audit trail on stderr — same pattern as capability-gate.py.
            print(
                f"[exhaustive-search-gate] override applied: {args.override}",
                file=sys.stderr,
            )
            return _emit(result, args.output, 0, decision="override")

        result["would_block"] = True
        result["reason"] = (
            f"Negation claim '{trigger}' matched but search evidence is "
            f"insufficient: tiers={tier_count} (min {args.min_tiers}), "
            f"queries={result['queries_count']} (min {args.min_queries}). "
            f"Per exhaustive-search-before-negation.md: run >=3 tree queries "
            f"with distinct terms and check adjacent stores (reasoning bank, "
            f"guardrails, pattern signatures, experience) or escalate to "
            f"codebase/web. If this match is a false positive, re-call with "
            f'--override "<justification>".'
        )
        return _emit(result, args.output, 1, decision="block")

    except Exception as e:
        # Fail-open on any unexpected error. A broken gate suppresses its
        # verdict rather than blocking legitimate output.
        result["gate_error"] = f"{type(e).__name__}: {e}"
        result["reason"] = (
            "Gate encountered an unexpected error; failing open. "
            "Caller may proceed; investigate the error offline."
        )
        return _emit(result, args.output, 0, decision="fail_open")


def _emit(result, output_format, exit_code, decision=None):
    if decision is not None:
        # gate_id MUST match core/config/gates.yaml id.
        _gate_log(
            "exhaustive-search-gate",
            decision,
            trigger_matched=result.get("trigger_matched"),
            payload=result.get("claim_text"),
            override_reason=result.get("override_applied"),
            gate_error=result.get("gate_error"),
            extra={
                "would_block": result.get("would_block"),
                "tiers_used": result.get("tiers_used"),
                "queries_count": result.get("queries_count"),
                "evidence_source": result.get("evidence_source"),
                "min_tiers": result.get("min_tiers"),
                "min_queries": result.get("min_queries"),
            },
        )
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"Claim: {result['claim_text'][:120]}")
        print(f"Trigger matched: {result['trigger_matched']}")
        print(f"Tiers used: {result['tiers_used']} ({result['evidence_source']})")
        print(f"Queries count: {result['queries_count']}")
        print(f"Min tiers: {result['min_tiers']}, Min queries: {result['min_queries']}")
        print(f"Override: {result['override_applied']}")
        print(f"Would block: {result['would_block']}")
        print(f"Reason: {result['reason']}")
        if result["gate_error"]:
            print(f"Gate error: {result['gate_error']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
