#!/usr/bin/env python3
"""Reflect bookkeeping — Tier 1a hot-path extraction.

Consolidates the pure-arithmetic / deterministic work from
reflect-on-outcome/SKILL.md Steps 2.5b, 2.7, 7.5, 7.5b, 7.6c, 7.7, 7.7f
and the batch-micro mode into a single subcommand-dispatched Python.

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #2).

Subcommands (all JSON stdout):
  encoding-score         — Step 2.7 Hippocampal Gate (pure arithmetic)
  convention-routing     — Step 2.5b classification (signal-word + recurrence)
  dual-classification    — Step 7.6c confidence+outcome → class
  entity-normalize       — Step 7.5b entity tokenization + normalization
  context-gap            — Step 7.7 missed tree_nodes/pattern_signatures cross-ref
  utilization-delta      — Step 7.7f per-item increment payload builder
  batch-micro            — Batch mode: aggregate micro_hypotheses array
  run-all                — Cascade encoding-score + dual-classification in one call

Exit codes: 0=ok, 1=advisory signal (flags non-empty), 2=input error.
Never blocks; fail-open on every non-critical path.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import AGENT_DIR, PROJECT_ROOT, CORE_ROOT, WORLD_DIR  # type: ignore
from _fileops import log_script_decision  # type: ignore
from _gate_log import log as _gate_log  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


# =============================================================================
# Shared helpers
# =============================================================================

def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _load_yaml(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or default
    except Exception:  # noqa: BLE001
        return default


def _emit(payload, exit_code=0):
    print(json.dumps(payload, ensure_ascii=False, default=str))
    sys.exit(exit_code)


# =============================================================================
# Step 2.7 — Memory Encoding Score (Hippocampal Gate)
# =============================================================================

DEFAULT_WEIGHTS = {
    "novelty": 0.30,
    "outcome_impact": 0.25,
    "surprise": 0.20,
    "goal_relevance": 0.15,
    "repetition": 0.10,
}


def _load_memory_pipeline():
    """Read core + world memory-pipeline.yaml; world wins on conflict."""
    core_path = Path(PROJECT_ROOT) / "core" / "config" / "memory-pipeline.yaml"
    core = _load_yaml(core_path, {}) or {}

    world_path = Path(WORLD_DIR) / "memory-pipeline.yaml" if WORLD_DIR else None
    world = _load_yaml(world_path, {}) if world_path else {}
    return core, world or {}


def cmd_encoding_score(args):
    """Step 2.7 arithmetic. Inputs via flags:
      --novelty [0..1]          LLM-judged
      --outcome-impact [0..1]   LLM-judged
      --surprise [0..10]        from ABC chain
      --goal-relevance [0..1]   LLM-judged (default 1.0 if tied to active goal)
      --repetition-count N      integer count from violations (clamped ×0.1)
      --domain-class STR        optional; looked up in encoding_gate multipliers
      --precision-items N       precision_manifest length → density bonus
    """
    core, world = _load_memory_pipeline()
    gate = (core.get("encoding_gate") or {})
    encode_threshold = gate.get("encode_threshold", 0.40)

    core_mul = (gate.get("category_class_multiplier") or {})
    world_gate = (world.get("encoding_gate") or {})
    world_mul = (world_gate.get("category_class_multiplier") or {}) or {}
    effective_mul = {**core_mul, **world_mul}

    novelty = max(0.0, min(1.0, args.novelty))
    outcome_impact = max(0.0, min(1.0, args.outcome_impact))
    surprise = max(0.0, min(10.0, args.surprise)) / 10.0
    goal_relevance = max(0.0, min(1.0, args.goal_relevance))
    repetition = max(0.0, min(1.0, 0.1 * args.repetition_count))

    base_score = (
        novelty * DEFAULT_WEIGHTS["novelty"]
        + outcome_impact * DEFAULT_WEIGHTS["outcome_impact"]
        + surprise * DEFAULT_WEIGHTS["surprise"]
        + goal_relevance * DEFAULT_WEIGHTS["goal_relevance"]
        + repetition * DEFAULT_WEIGHTS["repetition"]
    )

    multiplier = effective_mul.get(args.domain_class, 1.00) if args.domain_class else 1.00
    after_multiplier = base_score * multiplier

    # Precision density bonus (applied only if passing threshold path)
    precision_bonus = 0.0
    if args.precision_items >= 3:
        precision_bonus = 0.10
    elif args.precision_items >= 1:
        precision_bonus = 0.05

    final_score = min(1.0, after_multiplier + precision_bonus)
    encode = final_score >= encode_threshold

    result = {
        "subcommand": "encoding-score",
        "components": {
            "novelty": round(novelty, 4),
            "outcome_impact": round(outcome_impact, 4),
            "surprise": round(surprise, 4),
            "goal_relevance": round(goal_relevance, 4),
            "repetition": round(repetition, 4),
        },
        "base_score": round(base_score, 4),
        "domain_class": args.domain_class,
        "domain_multiplier": round(multiplier, 4),
        "precision_items": args.precision_items,
        "precision_bonus": round(precision_bonus, 4),
        "final_score": round(final_score, 4),
        "encode_threshold": round(encode_threshold, 4),
        "encode_decision": encode,
        "summary": (
            f"score={round(final_score, 3)} "
            f"(base={round(base_score, 3)} × mul={round(multiplier, 2)} "
            f"+ prec={round(precision_bonus, 2)}) "
            f"→ {'ENCODE' if encode else 'DISCARD'} "
            f"(threshold={round(encode_threshold, 2)})"
        ),
    }
    _emit(result, 0)


# =============================================================================
# Step 7.6c — Dual Classification
# =============================================================================

def cmd_dual_classification(args):
    """Confidence + outcome → process-outcome dual classification."""
    outcome = args.outcome.upper()
    conf = args.confidence
    if outcome not in ("CONFIRMED", "CORRECTED"):
        _emit({"error": f"outcome must be CONFIRMED or CORRECTED, got {outcome}"}, 2)
    if not (0.0 <= conf <= 1.0):
        _emit({"error": f"confidence must be 0..1, got {conf}"}, 2)

    if outcome == "CONFIRMED" and conf >= 0.60:
        cls = "earned_confirmed"
    elif outcome == "CONFIRMED" and conf < 0.60:
        cls = "lucky_confirmed"
    elif outcome == "CORRECTED" and conf >= 0.60:
        cls = "unlucky_corrected"
    else:
        cls = "deserved_corrected"

    process_quality = conf if outcome == "CONFIRMED" else (1.0 - conf)

    _emit({
        "subcommand": "dual-classification",
        "outcome": outcome,
        "confidence": conf,
        "dual_classification": cls,
        "process_quality": round(process_quality, 4),
        "summary": f"{cls} (conf={conf}, process_quality={round(process_quality, 3)})",
    }, 0)


# =============================================================================
# Step 2.5b — Convention Routing Check
# =============================================================================

UNIVERSAL_WORDS = ("always", "every goal", "before any", "after every", "every time")
PROCEDURAL_WORDS = (
    "run", "execute", "pull", "commit", "push", "test", "scan",
    "review", "verify", "deploy", "rebase", "build",
)
PRE_WORDS = ("before executing", "before starting", "prerequisite", "check first", "pull latest")
POST_WORDS = ("after executing", "after finishing", "commit", "push", "clean up", "record", "verify result")


def _contains_any(text, words):
    t = text.lower()
    return any(w in t for w in words)


def cmd_convention_routing(args):
    """Classify a lesson text as convention-worthy or keep-as-guardrail."""
    lesson_text = args.lesson or sys.stdin.read().strip()
    if not lesson_text:
        _emit({"error": "empty lesson text"}, 2)

    is_universal = _contains_any(lesson_text, UNIVERSAL_WORDS)
    is_procedural = _contains_any(lesson_text, PROCEDURAL_WORDS)
    maps_to_pre = _contains_any(lesson_text, PRE_WORDS)
    maps_to_post = _contains_any(lesson_text, POST_WORDS)

    if not (is_universal and is_procedural and (maps_to_pre or maps_to_post)):
        # gate_id MUST match core/config/gates.yaml id.
        # noop: signal-word filter failed → no real promotion classification performed.
        _gate_log("reflect-convention-routing", "noop",
                  caller="reflect-bookkeeping.py:cmd_convention_routing",
                  trigger_matched=None,
                  payload=lesson_text[:200],
                  extra={"is_universal": is_universal, "is_procedural": is_procedural,
                         "maps_to_pre": maps_to_pre, "maps_to_post": maps_to_post})
        _emit({
            "subcommand": "convention-routing",
            "route": "keep_as_guardrail",
            "is_universal": is_universal,
            "is_procedural": is_procedural,
            "maps_to_pre": maps_to_pre,
            "maps_to_post": maps_to_post,
            "summary": "lesson stays as guardrail (not universal/procedural enough)",
        }, 0)

    target = "pre-execution" if maps_to_pre else "post-execution"

    # Cost gate check
    conv_path = Path(WORLD_DIR) / "conventions" / f"{target}.md" if WORLD_DIR else None
    current_step_count = 0
    if conv_path and conv_path.exists():
        with open(conv_path, "r", encoding="utf-8") as f:
            current_step_count = sum(1 for ln in f if ln.startswith("## Step"))

    cfg = _load_yaml(Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml", {}) or {}
    max_steps = (
        ((cfg.get("modifiable") or {}).get("convention_learning") or {}).get(
            "max_steps_per_convention", 8)
    )

    if current_step_count >= max_steps:
        # pass: classification ran, signal words matched, but cost-gated to keep-as-guardrail.
        _gate_log("reflect-convention-routing", "pass",
                  caller="reflect-bookkeeping.py:cmd_convention_routing",
                  trigger_matched=f"target:{target}",
                  payload=lesson_text[:200],
                  extra={"reason": "step_limit_hit", "step_count": current_step_count,
                         "max_steps": max_steps})
        _emit({
            "subcommand": "convention-routing",
            "route": "keep_as_guardrail",
            "target_convention": target,
            "current_step_count": current_step_count,
            "max_steps": max_steps,
            "summary": f"at step limit ({current_step_count}/{max_steps}) — keeping as guardrail",
        }, 0)

    recurrence = args.recurrence_count
    if recurrence >= 2:
        decision = "auto_apply_promote"
    else:
        decision = "propose_pending_reinforcement"

    # block: gate's active assertion — promote to convention (FP-cost path).
    _gate_log("reflect-convention-routing", "block",
              caller="reflect-bookkeeping.py:cmd_convention_routing",
              trigger_matched=f"target:{target}",
              payload=lesson_text[:200],
              extra={"route": decision, "target_convention": target,
                     "recurrence_count": recurrence,
                     "step_count": current_step_count, "max_steps": max_steps,
                     "would_block": True})
    _emit({
        "subcommand": "convention-routing",
        "route": decision,
        "target_convention": target,
        "current_step_count": current_step_count,
        "max_steps": max_steps,
        "recurrence_count": recurrence,
        "is_universal": is_universal,
        "is_procedural": is_procedural,
        "summary": (
            f"promote to {target} convention (recurrence={recurrence})"
            if decision == "auto_apply_promote"
            else f"propose pending {target} step (recurrence={recurrence})"
        ),
    }, 0)


# =============================================================================
# Step 7.5b — Entity Extraction + Normalization
# =============================================================================

ENTITY_CHAR_RE = re.compile(r"[^\w\s-]")


def _normalize(name):
    """Entity → lowercase-kebab-case."""
    s = ENTITY_CHAR_RE.sub(" ", name)
    s = re.sub(r"\s+", "-", s.strip().lower())
    return s.strip("-")


def cmd_entity_normalize(args):
    """Read one entity per line on stdin (or --entities); emit normalized tokens."""
    if args.entities:
        raw = args.entities
    else:
        raw = sys.stdin.read()
    tokens = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        norm = _normalize(line)
        if norm:
            tokens.append({"original": line, "normalized": norm})
    _emit({
        "subcommand": "entity-normalize",
        "count": len(tokens),
        "entities": tokens,
        "summary": f"normalized {len(tokens)} entities",
    }, 0)


# =============================================================================
# Step 7.7 — Context Gap Analysis
# =============================================================================

def cmd_context_gap(args):
    """Compare consulted context against available resources in the category.

    Inputs:
      --hypothesis-category STR       e.g. "fiscal-policy"
      --consulted-nodes JSON          array of tree-node IDs that WERE read
      --consulted-signatures JSON     array of sig-NNN IDs that WERE checked
      --outcome STR                   CONFIRMED | CORRECTED

    Output:
      missed_tree_nodes, missed_pattern_signatures, context_gap_count,
      encoding_bonus (0.15 if CORRECTED and gaps), learning_signal
    """
    consulted_nodes = set(json.loads(args.consulted_nodes or "[]"))
    consulted_sigs = set(json.loads(args.consulted_signatures or "[]"))
    category = args.hypothesis_category

    # Enumerate available tree nodes in this category
    available_nodes = []
    tree_path = Path(WORLD_DIR) / "knowledge" / "tree" / "_tree.yaml" if WORLD_DIR else None
    if tree_path and tree_path.exists():
        tree = _load_yaml(tree_path, {}) or {}

        def walk(node, cat):
            if not isinstance(node, dict):
                return
            if node.get("category") == cat and node.get("id"):
                available_nodes.append(node["id"])
            for v in node.values():
                if isinstance(v, dict):
                    walk(v, cat)
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            walk(it, cat)
        walk(tree, category)

    # Enumerate available pattern signatures in this category (best-effort)
    available_sigs = []
    try:
        sig_path = Path(WORLD_DIR) / "pattern-signatures.jsonl" if WORLD_DIR else None
        if sig_path and sig_path.exists():
            with open(sig_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        sig = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if sig.get("status") != "active":
                        continue
                    conds = sig.get("conditions") or {}
                    if (isinstance(conds, dict) and conds.get("category") == category) \
                            or category in str(conds):
                        available_sigs.append(sig.get("id"))
    except Exception:  # noqa: BLE001
        pass

    missed_nodes = [n for n in available_nodes if n not in consulted_nodes]
    missed_sigs = [s for s in available_sigs if s and s not in consulted_sigs]

    outcome = (args.outcome or "").upper()
    gaps_found = bool(missed_nodes or missed_sigs)
    encoding_bonus = 0.15 if (outcome == "CORRECTED" and gaps_found) else 0.0

    if outcome == "CORRECTED" and gaps_found:
        signal = "gap_contributed_to_corrected"
    elif outcome == "CONFIRMED" and not gaps_found:
        signal = "full_context_correlated_confirmed"
    else:
        signal = "no_strong_signal"

    _emit({
        "subcommand": "context-gap",
        "hypothesis_category": category,
        "consulted_nodes_count": len(consulted_nodes),
        "consulted_signatures_count": len(consulted_sigs),
        "missed_tree_nodes": missed_nodes,
        "missed_pattern_signatures": missed_sigs,
        "context_gap_count": len(missed_nodes) + len(missed_sigs),
        "outcome": outcome,
        "learning_signal": signal,
        "encoding_bonus": encoding_bonus,
        "summary": (
            f"{len(missed_nodes)} missed node(s), {len(missed_sigs)} missed signature(s); "
            f"signal={signal}"
        ),
    }, 0)


# =============================================================================
# Step 7.7f — Utilization Delta Builder
# =============================================================================

def cmd_utilization_delta(args):
    """Build per-item increment payloads from deliberation metadata.

    Input JSON on stdin (or --deliberation):
      {
        "loaded_items": [{"id": "rb-001", "kind": "rb"}, {"id": "guard-042", "kind": "guard"}],
        "active_ids": ["rb-001"],
        "skipped_ids": ["guard-042"],
        "most_valuable_source": "rb:rb-001",
        "least_valuable_source": "guard:guard-042"
      }

    Output: array of {id, kind, increments: [times_active|times_skipped|...]}
    The LLM (or subsequent call) then invokes reasoning-bank-increment.sh /
    guardrails-increment.sh per record — keeps this script side-effect free.
    """
    raw = args.deliberation or sys.stdin.read()
    if not raw.strip():
        _emit({"error": "empty deliberation input"}, 2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _emit({"error": f"invalid JSON: {e}"}, 2)

    active_set = set(data.get("active_ids") or [])
    skipped_set = set(data.get("skipped_ids") or [])
    mv = data.get("most_valuable_source") or ""
    lv = data.get("least_valuable_source") or ""

    def parse_ref(s):
        if ":" in s:
            layer, ident = s.split(":", 1)
            return layer.strip(), ident.strip()
        return "", s.strip()

    mv_layer, mv_id = parse_ref(mv)
    lv_layer, lv_id = parse_ref(lv)

    deltas = []
    for item in data.get("loaded_items") or []:
        iid = item.get("id")
        kind = item.get("kind", "rb")
        increments = []
        if iid in active_set:
            increments.append("utilization.times_active")
        if iid in skipped_set:
            increments.append("utilization.times_skipped")
        if iid == mv_id:
            increments.append("utilization.times_helpful")
        if iid == lv_id:
            increments.append("utilization.times_noise")
        if increments:
            deltas.append({"id": iid, "kind": kind, "increments": increments})

    _emit({
        "subcommand": "utilization-delta",
        "delta_count": len(deltas),
        "deltas": deltas,
        "summary": f"{len(deltas)} items have utilization deltas to apply",
    }, 0)


# =============================================================================
# Batch Micro Mode — aggregate micro_hypotheses array
# =============================================================================

def _read_micro_hypotheses():
    """Read micro_hypotheses slot from working-memory.yaml."""
    if AGENT_DIR is None:
        return []
    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm = _resolve_wm_path()
    if not wm.exists():
        return []
    data = _load_yaml(wm, {}) or {}
    # Slots live under the top-level `slots:` map in the live WM layout — the
    # old top-level read returned None for every invocation, so batch-micro
    # always early-exited with "no micro-hypotheses to process" ().
    # Keep the top-level read as fallback for pre-slots layouts.
    slots = data.get("slots")
    if isinstance(slots, dict) and slots.get("micro_hypotheses") is not None:
        return slots.get("micro_hypotheses") or []
    return data.get("micro_hypotheses") or []


def cmd_batch_micro(args):
    """Process the entire micro_hypotheses array as one batch."""
    micros = _read_micro_hypotheses()
    if not micros:
        _emit({
            "subcommand": "batch-micro",
            "total": 0,
            "summary": "no micro-hypotheses to process",
            "flags": [],
        }, 0)

    total = len(micros)
    confirmed = sum(1 for m in micros if m.get("outcome") == "confirmed")
    corrected = sum(1 for m in micros if m.get("outcome") == "corrected")
    unresolved = sum(1 for m in micros if m.get("outcome") is None)

    resolved = confirmed + corrected
    accuracy_pct = round(100.0 * confirmed / resolved, 2) if resolved else None

    by_category = {}
    for m in micros:
        cat = m.get("category", "uncategorized")
        b = by_category.setdefault(
            cat, {"total": 0, "confirmed": 0, "corrected": 0, "accuracy_pct": None})
        b["total"] += 1
        if m.get("outcome") == "confirmed":
            b["confirmed"] += 1
        elif m.get("outcome") == "corrected":
            b["corrected"] += 1
    for cat, b in by_category.items():
        res = b["confirmed"] + b["corrected"]
        b["accuracy_pct"] = round(100.0 * b["confirmed"] / res, 2) if res else None

    overconfident_misses = sum(
        1 for m in micros
        if (m.get("confidence") or 0) >= 0.80 and m.get("outcome") == "corrected"
    )
    underconfident_hits = sum(
        1 for m in micros
        if (m.get("confidence") or 0) <= 0.40 and m.get("outcome") == "confirmed"
    )

    # Surprise + promotion
    surprises = []
    for idx, m in enumerate(micros):
        conf = m.get("confidence") or 0.0
        outcome = m.get("outcome")
        if outcome == "corrected":
            surprise = round(conf * 10)
        elif outcome == "confirmed":
            surprise = round((1.0 - conf) * 10)
        else:
            surprise = 0
        m["surprise"] = surprise

        promote_reason = None
        if surprise >= 7:
            promote_reason = "high_surprise"
        elif conf >= 0.90 and outcome == "corrected":
            promote_reason = "overconfident_violation"
        elif conf <= 0.30 and outcome == "confirmed":
            promote_reason = "underconfidence"
        if promote_reason:
            surprises.append({
                "index": idx,
                "claim": m.get("claim"),
                "confidence": conf,
                "outcome": outcome,
                "category": m.get("category"),
                "surprise": surprise,
                "promotion_reason": promote_reason,
                "encoding_score": min(1.0, 0.50 + surprise / 20.0),
            })

    # Step 6 actionable discoveries
    actionable = []
    promoted_by_cat = {}
    for s in surprises:
        promoted_by_cat[s["category"]] = promoted_by_cat.get(s["category"], 0) + 1
    for cat, n in promoted_by_cat.items():
        if n >= 3:
            actionable.append({
                "category": cat,
                "insight": f"Systematic surprises in {cat} — {n} high-surprise predictions",
                "suggested_work": f"Research {cat} domain deeper or review assumptions",
                "priority": "MEDIUM",
            })

    oc_by_cat = {}
    for m in micros:
        if (m.get("confidence") or 0) >= 0.80 and m.get("outcome") == "corrected":
            oc_by_cat[m.get("category", "uncategorized")] = (
                oc_by_cat.get(m.get("category", "uncategorized"), 0) + 1)
    for cat, n in oc_by_cat.items():
        if n >= 2:
            actionable.append({
                "category": cat,
                "insight": f"Overconfident failures in {cat} — mental model may be wrong",
                "suggested_work": f"Investigate {cat} assumptions and update knowledge",
                "priority": "HIGH",
            })

    flags = []
    if overconfident_misses > 0:
        flags.append("overconfident_misses")
    if underconfident_hits > 0:
        flags.append("underconfident_hits")
    if len(surprises) >= 3:
        flags.append("high_surprise_cluster")
    if actionable:
        flags.append("actionable_discoveries")

    # : counted-once contract. Settled micros are counted into the
    # all-time counters at THIS pass and must then LEAVE the WM slot — the
    # caller writes micro_hypotheses_writeback (pending-only) back to WM.
    # total_all_time is DERIVED (confirmed_all_time + corrected_all_time +
    # pending_now), never `+= total`: a carried pending micro re-batches every
    # pass, so the old increment counted it once per pass (observed 30 vs 9
    # resolved). The derived form counts each settled micro exactly once and
    # self-heals historical inflation on every write.
    pending_writeback = [m for m in micros if m.get("outcome") is None]
    stats_delta = {
        "confirmed_delta": confirmed,
        "corrected_delta": corrected,
        "pending_now": unresolved,
        "rule": ("confirmed_all_time += confirmed_delta; corrected_all_time += "
                 "corrected_delta; total_all_time = confirmed_all_time + "
                 "corrected_all_time + pending_now (DERIVED, never += total)"),
    }

    _emit({
        "subcommand": "batch-micro",
        "total": total,
        "confirmed": confirmed,
        "corrected": corrected,
        "unresolved": unresolved,
        "accuracy_pct": accuracy_pct,
        "by_category": by_category,
        "overconfident_misses": overconfident_misses,
        "underconfident_hits": underconfident_hits,
        "promoted_to_encoding": len(surprises),
        "surprises": surprises,
        "actionable_discoveries": actionable,
        "stats_delta": stats_delta,
        "micro_hypotheses_writeback": pending_writeback,
        "flags": flags,
        "summary": (
            f"{total} micros: {confirmed} confirmed / {corrected} corrected "
            f"({accuracy_pct}%), promoted {len(surprises)}; "
            f"writeback {len(pending_writeback)} pending (settled pruned)"
        ),
    }, 1 if flags else 0)


# =============================================================================
# run-all — cascade encoding-score → dual-classification
# =============================================================================

def cmd_run_all(args):
    """Stub: callers usually want one subcommand at a time. run-all exists for
    symmetry but just documents what the caller should invoke."""
    _emit({
        "subcommand": "run-all",
        "note": "reflect-bookkeeping subcommands are per-hypothesis; call them individually",
        "recommended_cascade": [
            "encoding-score  (Step 2.7)",
            "dual-classification  (Step 7.6c)",
            "context-gap  (Step 7.7)",
            "utilization-delta  (Step 7.7f)",
        ],
        "summary": "no-op meta-subcommand",
    }, 0)


# =============================================================================
# Dispatch
# =============================================================================

DISPATCH = {
    "encoding-score": cmd_encoding_score,
    "dual-classification": cmd_dual_classification,
    "convention-routing": cmd_convention_routing,
    "entity-normalize": cmd_entity_normalize,
    "context-gap": cmd_context_gap,
    "utilization-delta": cmd_utilization_delta,
    "batch-micro": cmd_batch_micro,
    "run-all": cmd_run_all,
}


def main():
    p = argparse.ArgumentParser(description="Reflect bookkeeping (Tier 1a)")
    p.add_argument("subcommand", choices=list(DISPATCH.keys()))

    # encoding-score inputs
    p.add_argument("--novelty", type=float, default=0.5)
    p.add_argument("--outcome-impact", type=float, default=0.5)
    p.add_argument("--surprise", type=float, default=0.0)
    p.add_argument("--goal-relevance", type=float, default=0.5)
    p.add_argument("--repetition-count", type=int, default=0)
    p.add_argument("--domain-class", type=str, default=None)
    p.add_argument("--precision-items", type=int, default=0)

    # dual-classification inputs
    p.add_argument("--outcome", type=str, default=None)
    p.add_argument("--confidence", type=float, default=0.5)

    # convention-routing inputs
    p.add_argument("--lesson", type=str, default=None)
    # --recurrence-count MUST be the LLM's semantic-similarity count over
    # active guardrails (guardrails-read.sh --active, then judge which match
    # the lesson's MEANING — not substring). The script does NOT re-derive
    # this; default=0 is a conservative floor that under-fires promotion, not
    # a valid answer. Callers that skip the semantic check degrade this
    # subcommand silently. See reflect-on-outcome Step 2.5b.
    p.add_argument("--recurrence-count", type=int, default=0)

    # entity-normalize inputs
    p.add_argument("--entities", type=str, default=None)

    # context-gap inputs
    p.add_argument("--hypothesis-category", type=str, default="uncategorized")
    p.add_argument("--consulted-nodes", type=str, default="[]")
    p.add_argument("--consulted-signatures", type=str, default="[]")

    # utilization-delta inputs
    p.add_argument("--deliberation", type=str, default=None)

    args = p.parse_args()
    log_script_decision("reflect-bookkeeping", {"subcommand": args.subcommand})
    DISPATCH[args.subcommand](args)


if __name__ == "__main__":
    main()
