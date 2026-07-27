#!/usr/bin/env python3
"""Utilization feedback engine — single-command replacement for Phase 4.26.

Reads <agent>/session/retrieval-session.json (auto-written by retrieve.py) and
applies utilization feedback to all retrieved tree nodes and supplementary items.

Usage:
    utilization-feedback.sh --goal <goal-id> --helpful "node1,node2,rb-001"
    utilization-feedback.sh --goal <goal-id> --all-helpful
    utilization-feedback.sh --goal <goal-id> --all-noise     # legacy (poisons times_noise)
    utilization-feedback.sh --goal <goal-id> --all-unknown   # preferred backstop

The --helpful flag marks named items as helpful and everything else as noise.
The --all-helpful and --all-noise flags apply uniformly. The hook fallback
(utilization-gate.sh) uses --all-unknown — a no-op on the helpful/noise/inferred
counters that simply marks utilization_pending=false. The older --all-noise
backstop ran the times_noise counter on every retrieved item, which over many
iterations pushed nodes toward distill/prune candidacy purely from the
backstop misfiring (tree.py:540-588 has_feedback gate consumes times_noise).
--all-unknown leaves the retrieval-prioritization floor clamp (utility_weight
floor 0.5x) doing the conservative work alone, without lying about negative
signal that pushes toward retirement.

Idempotent: if utilization_pending is already false, exits with no changes.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import PROJECT_ROOT, WORLD_DIR, AGENT_DIR, CORE_ROOT

# --- producer-side token helpers (single source of truth, ) --------
# This module CONSUMES what retrieve.py's _distinctive_tokens produces. The
# tokenizer and the structural test MUST be the same on both sides: a local
# copy would keep parsing without error while silently matching nothing the day
# either side changed — the verbatim-twin drift class. Imported lazily and
# cached so an import problem in retrieve.py cannot break this module's other
# subcommands at load time.
_PRODUCER = None


def _producer():
    global _PRODUCER
    if _PRODUCER is None:
        d = str(Path(__file__).resolve().parent)
        if d not in sys.path:
            sys.path.insert(0, d)
        import retrieve as _r
        _PRODUCER = _r
    return _PRODUCER


def _prod_token_re():
    """The producer's identifier-preserving tokenizer (`retrieve._TOKEN_RE`)."""
    return _producer()._TOKEN_RE


def _prod_is_structural():
    """The producer's rb-1729 shape test (`retrieve._is_structural_token`)."""
    return _producer()._is_structural_token
import _rt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TREE_PATH = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
SESSION_PATH = AGENT_DIR / "session" / "retrieval-session.json" if AGENT_DIR else None


def now_str():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Tree node batch update (single atomic read-modify-write)
# ---------------------------------------------------------------------------

def _recompute_utility_ratio(node):
    # DO NOT INLINE. Must match tree.py _recompute_utility_ratio exactly.
    # Inferred hits count half — manual feedback keeps authoritative weight.
    rc = node.get("retrieval_count", 0)
    th = node.get("times_helpful", 0)
    tih = node.get("times_inferred_helpful", 0)
    node["utility_ratio"] = round((th + 0.5 * tih) / max(rc, 1), 4)
    # C.1 parallel field — see utilization-stats.py module docstring for the
    # canonical formula. Tree nodes don't currently track times_active / cited
    # (those are rb/guardrail-specific signals), so those terms degrade to 0.
    ta = node.get("times_active", 0)
    tc = node.get("times_cited", 0)
    node["utility_ratio_v2"] = round(
        (th + 0.5 * tih + 0.25 * ta + 1.0 * tc) / max(rc + 1, 1), 4
    )


def _mark_legacy_backstop_noise(node):
    """Phase 1d migration: flag nodes whose high times_noise came from the
    pre-refactor --all-noise backstop loop. The flag is descriptive — cleared
    on the first real helpful/inferred_helpful event. No formula dampening
    applied here; it's preserved for future use (e.g., retirement gates that
    want to forgive historical backstop poisoning)."""
    tn = node.get("times_noise", 0)
    th = node.get("times_helpful", 0)
    tih = node.get("times_inferred_helpful", 0)
    if tn > 10 and th == 0 and tih == 0:
        node["legacy_backstop_noise"] = True


def _clear_legacy_backstop_noise(node):
    """Clear the flag when any real helpful signal arrives."""
    if "legacy_backstop_noise" in node:
        del node["legacy_backstop_noise"]


def update_tree_nodes(helpful_keys, noise_keys, inferred_helpful_keys=None):
    """Increment times_helpful/times_noise/times_inferred_helpful and recompute
    utility_ratio atomically. `inferred_helpful_keys` carries --infer hits, which
    increment the half-weight counter instead of the authoritative one.

    guard-366: the read->mutate->write runs through _fileops.locked_modify_yaml,
    which holds the lock across the ENTIRE cycle. This (a) closes the lost-update
    race where two agents read the same _tree.yaml baseline and the second writer
    clobbers the first, and (b) replaces the prior bare tempfile + os.replace
    write that raised PermissionError (WinError 5) under OneDrive / partner-agent
    contention (observed g-115-18 close 2026-06-29, which forced the
    --all-unknown backstop). locked_modify_yaml's CSafeDumper output matches the
    on-disk _tree.yaml format the rest of tree.py's mechanical ops write, so this
    also retires the width=200 reformat churn the old write_yaml caused on every
    successful write, and routes the mutation through the history + changelog
    path it previously bypassed.
    """
    from _fileops import locked_modify_yaml

    inferred_helpful_keys = inferred_helpful_keys or []
    if not TREE_PATH.exists():
        return 0, 0, 0
    # Nothing to apply -> skip the locked write entirely. locked_modify_yaml
    # always writes once entered, so the original "only write when a counter
    # moved" guard is preserved here by not entering on an empty key set.
    if not (helpful_keys or inferred_helpful_keys or noise_keys):
        return 0, 0, 0

    counts = {"h": 0, "n": 0, "ih": 0}

    def _apply(tree):
        nodes = tree.get("nodes", {})
        for key in helpful_keys:
            if key in nodes:
                node = nodes[key]
                node["times_helpful"] = node.get("times_helpful", 0) + 1
                _clear_legacy_backstop_noise(node)
                _recompute_utility_ratio(node)
                counts["h"] += 1
        for key in inferred_helpful_keys:
            if key in nodes:
                node = nodes[key]
                node["times_inferred_helpful"] = node.get("times_inferred_helpful", 0) + 1
                _clear_legacy_backstop_noise(node)
                _recompute_utility_ratio(node)
                counts["ih"] += 1
        for key in noise_keys:
            if key in nodes:
                node = nodes[key]
                node["times_noise"] = node.get("times_noise", 0) + 1
                _mark_legacy_backstop_noise(node)
                _recompute_utility_ratio(node)
                counts["n"] += 1
        return tree

    locked_modify_yaml(TREE_PATH, _apply)
    return counts["h"], counts["n"], counts["ih"]


# ---------------------------------------------------------------------------
# Supplementary item feedback (reasoning bank + guardrails)
# ---------------------------------------------------------------------------

def increment_supplementary(item_id, item_type, field):
    """Increment a utilization counter via the daemon store endpoint.

    DAEMON-ONLY (2026-05-15, H2 Wave 2): reasoning-bank.py's rb/guard
    `increment` CLI subcommand was removed; this calls POST
    /v1/store/increment through _rt.py (the canonical Python->daemon
    client — a Python child cannot reach the daemon-aware .sh wrapper on
    Windows, rb-225/rb-247). Fail-soft: a daemon error logs to stderr
    (visible signal — same reason the old path treated non-zero rc as
    visible: silent swallow is how `times_inferred_helpful` dropped on the
    floor for two sessions) but does not break the feedback flow.
    """
    if item_type == "reasoning_bank":
        store = "reasoning-bank"
    elif item_type == "guardrail":
        store = "guardrails"
    else:
        return  # pattern_signatures don't have utilization increment paths

    try:
        _rt.store_increment(store, item_id, f"utilization.{field}")
    except _rt.RtError as e:
        print(f"[utilization-feedback] Increment failed for {item_id} "
              f"({store} utilization.{field}): {e}", file=sys.stderr)
    except Exception as e:
        print(f"[utilization-feedback] Warning: increment failed for {item_id}: {e}",
              file=sys.stderr)


def set_supplementary_field(item_id, item_type, field, value):
    """Set a top-level field via the daemon store endpoint.

    Used by C.3 to flip `auto_flagged_for_review = true` once
    times_inferred_unknown crosses unknown_threshold. DAEMON-ONLY
    (2026-05-15, H2 Wave 2): reasoning-bank.py's rb/guard `update-field`
    CLI subcommand was removed; this calls POST /v1/store/set-field
    through _rt.py. Same fail-soft pattern as increment_supplementary — a
    write failure logs to stderr but doesn't break the feedback flow.
    """
    if item_type == "reasoning_bank":
        store = "reasoning-bank"
    elif item_type == "guardrail":
        store = "guardrails"
    else:
        return
    try:
        _rt.store_set_field(store, item_id, field, str(value))
    except _rt.RtError as e:
        print(f"[utilization-feedback] update-field failed for {item_id} "
              f"({store} {field}={value}): {e}", file=sys.stderr)
    except Exception as e:
        print(f"[utilization-feedback] Warning: update-field failed for {item_id}: {e}",
              file=sys.stderr)


def _load_unknown_threshold():
    """Read curation.unknown_threshold from core/config/memory-pipeline.yaml.
    Default 5 if missing or unreadable. C.3 config knob."""
    cfg_path = CORE_ROOT / "config" / "memory-pipeline.yaml"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        threshold = (cfg.get("curation") or {}).get("unknown_threshold")
        if isinstance(threshold, int) and threshold > 0:
            return threshold
    except (OSError, yaml.YAMLError):
        pass
    return 5


def _current_inferred_unknown(item_id, item_type):
    """Read the live times_inferred_unknown counter for an item. Used to decide
    whether the auto-flag threshold has been crossed AFTER the increment.
    Returns 0 on any error (fail-soft — auto-flag is a soft assist, not gating).
    """
    if WORLD_DIR is None:
        return 0
    if item_type == "reasoning_bank":
        path = WORLD_DIR / "reasoning-bank.jsonl"
    elif item_type == "guardrail":
        path = WORLD_DIR / "guardrails.jsonl"
    else:
        return 0
    if not Path(path).exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") != item_id:
                    continue
                util = rec.get("utilization") or {}
                v = util.get("times_inferred_unknown", 0)
                return int(v) if isinstance(v, int) else 0
    except OSError:
        return 0
    return 0


# ---------------------------------------------------------------------------
# --infer mode: heuristic helpful/noise classification (Phase 1 curation plan)
# ---------------------------------------------------------------------------

def _fetch_diary_text(goal_id):
    """Read execution diary entries for a goal, directly from the JSONL file.

    Reads <agent>/session/execution-diary.jsonl (same path execution-diary.py uses)
    and concatenates `content` fields from entries whose goal_id matches. Reading
    the file directly avoids a subprocess → bash → python3 chain that's fragile
    across WSL/MSYS/Windows-py environments. Returns empty string on any failure.
    """
    if not goal_id or not AGENT_DIR:
        return ""
    diary_path = AGENT_DIR / "session" / "execution-diary.jsonl"
    if not diary_path.exists():
        return ""
    parts = []
    try:
        with open(diary_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("goal_id") != goal_id:
                    continue
                txt = rec.get("content", "")
                if txt:
                    parts.append(str(txt))
    except OSError:
        return ""
    return "\n".join(parts)


def _fetch_goal_text(goal_id):
    """Read the goal's title + description + verification criteria from
    aspirations stores (world + agent).

    The goal definition is much denser in domain-content prose than the
    execution diary, which largely logs structural phase transitions like
    "phase_start phase-2-select" (~26 chars). Diagnostic 2026-04-23 found
    that classifying retrieved items against diary-only tokens produced
    zero-overlap → "noise" for ~every item, which is why the post-g-242-06
    `--infer` path accumulated times_noise but no times_inferred_helpful.
    Including the goal's own prose gives the classifier real domain tokens
    to match retrieval distinctive_tokens against.

    Returns concatenated prose; empty string on any failure. Fail-open.
    """
    if not goal_id:
        return ""
    parts = []
    sources = []
    if WORLD_DIR:
        sources.append(WORLD_DIR / "aspirations.jsonl")
    if AGENT_DIR:
        sources.append(AGENT_DIR / "aspirations.jsonl")
    for src in sources:
        try:
            if not src.exists():
                continue
            with open(src, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in asp.get("goals", []) or []:
                        if not isinstance(g, dict):
                            continue
                        if g.get("id") != goal_id:
                            continue
                        for key in ("title", "description"):
                            val = g.get(key)
                            if isinstance(val, str) and val:
                                parts.append(val)
                        # verification.outcomes + verification.checks per
                        # goal-schemas.md — the only two canonical sub-fields.
                        v = g.get("verification")
                        if isinstance(v, dict):
                            for vk in ("outcomes", "checks"):
                                vv = v.get(vk)
                                if isinstance(vv, list):
                                    parts.extend(str(x) for x in vv if x)
                                elif isinstance(vv, str) and vv:
                                    parts.append(vv)
        except OSError:
            continue
    return " ".join(p for p in parts if p)


def _read_jsonl_simple(path):
    """Lightweight JSONL reader for the active-delta backstop.

    Avoids importing reasoning-bank.py here (would be a circular hop through
    subprocess). Returns [] on missing/unreadable; we silently fall through —
    the active-delta backstop is fail-open by design.
    """
    p = Path(path)
    if not p.exists():
        return []
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _live_times_active_index():
    """Build {id: current times_active} for rb + guardrails. Used by the
    --infer fallback-active backstop to detect "trigger fired during goal
    execution" as inferred-helpful even when token overlap classified the
    item as unknown.

    Reads live world stores once per call. Fail-open on missing files.
    """
    idx = {}
    if WORLD_DIR is None:
        return idx
    for fname in ("reasoning-bank.jsonl", "guardrails.jsonl"):
        for rec in _read_jsonl_simple(WORLD_DIR / fname):
            iid = rec.get("id")
            if not iid:
                continue
            util = rec.get("utilization") if isinstance(rec, dict) else None
            if isinstance(util, dict):
                v = util.get("times_active", 0)
                if isinstance(v, int) and not isinstance(v, bool):
                    idx[iid] = v
    return idx


def infer_feedback(session, confidence="conservative"):
    """Partition retrieved items into {helpful, noise, unknown} by token-overlap
    heuristic against goal definition + execution diary, then run the C.2
    fallback-active backstop on the unknown set.

    Backstop logic: for each item still in `unknown` after token classification,
    compare its CURRENT times_active to the times_active_at_retrieve baseline
    captured by retrieve.py. A positive delta means guardrail-check.py matched
    its trigger condition during this goal's execution — strong evidence the
    item was relevant. Such items are reclassified as inferred-helpful.

    Returns (helpful_ids, noise_ids, unknown_ids, stats_dict), or None if the
    session is too old (schema_version < 2) to carry distinctive_tokens.
    """
    # schema_version 3 () is the first version whose distinctive_tokens
    # keep identifier shape AND rank structural tokens ahead of prose. v2 sessions
    # are refused, NOT run through the old classifier: v2's output was MEASURED
    # (, 6 real manifests / 485 items) at helpful/population 0.922, with
    # an unrelated cake recipe scoring 0.627 against the same manifests — 68% of
    # its "helpful" verdicts were reproducible by topically-unrelated text. It was
    # never a measurement, so continuing to write counters from it during the
    # transition would knowingly inject bad data into times_inferred_helpful
    # (which carries half weight in utility_ratio). Sessions are per-goal and
    # short-lived, so the transition window is a few iterations.
    if session.get("schema_version", 1) < 3:
        return None

    goal_id = session.get("goal_id", "")
    goal_text = _fetch_goal_text(goal_id)
    diary_text = _fetch_diary_text(goal_id)

    # Combined token source: goal prose (title/description/verification) +
    # agent-authored diary. Goal prose carries the domain terms that make
    # retrieval distinctive_tokens meaningful; diary fills in when the
    # execution produced prose (debug logs, findings, etc.). Before 2026-04-23
    # this was diary-only, which failed because diary entries are mostly
    # phase-marker structural text.
    combined_text = (goal_text + "\n" + diary_text).strip()
    combined_lc = combined_text.lower()
    # Tokenize the SAME way the producer does, via the producer's own helpers —
    # a local copy of the regex would silently stop matching the day either side
    # changed (the verbatim-twin drift class this codebase keeps re-learning).
    combined_tokens = {
        t.strip("-_") for t in _prod_token_re().findall(combined_lc)}
    combined_tokens.discard("")

    min_distinctive = 2 if confidence == "conservative" else 1

    helpful = set()
    noise = set()
    unknown = set()

    is_structural = _prod_is_structural()

    def classify(entry_id, tokens):
        # Key-or-id appearance is a strong signal (agent referenced it).
        if entry_id and entry_id.lower() in combined_lc:
            return "helpful"
        # STRUCTURAL overlap only (rb-1729: token SHAPE is the discriminator,
        # not generic prose vocab). Counting any shared token made this
        # near-certain for a multi-KB goal description — a goal in category
        # `framework-architecture` marked every `*-architecture` node helpful
        # because its prose contained the word "architecture". Requiring a
        # [-_0-9]-bearing identifier drops that to zero false positives across
        # the 6-manifest corpus while every manifest still clears helpful>0.
        structural = [t for t in tokens
                      if t in combined_tokens and is_structural(t)]
        if len(structural) >= min_distinctive:
            return "helpful"
        if len(structural) == 0:
            return "noise"
        # `unknown` keeps its original meaning — SOME structural evidence but
        # below threshold (reachable only when confidence=conservative sets
        # min_distinctive=2). Do NOT route prose-only overlap here: at the
        # default min_distinctive=1 that would populate a bucket which was
        # previously unreachable, and every unknown supplementary item
        # increments times_inferred_unknown, which at unknown_threshold=5 sets
        # auto_flagged_for_review=true and force-feeds the item into the
        # curation candidate list REGARDLESS of evidence. Since virtually every
        # item shares some prose token with a multi-KB goal, that would flag
        # most of the rb/guardrail corpus within ~5 deep closes. Routing to
        # `noise` costs nothing: the C.2 backstop below discards from `noise`
        # AND `unknown`, so a genuinely-used item is still rescued by its
        # times_active delta. (Caught by the  fresh-eyes pass.)
        return "unknown"

    for entry in session.get("tree_nodes_detail", []) or []:
        key = entry.get("key")
        if not key:
            continue
        label = classify(key, entry.get("distinctive_tokens", []))
        {"helpful": helpful, "noise": noise, "unknown": unknown}[label].add(key)

    for entry in session.get("supplementary_detail", []) or []:
        iid = entry.get("id")
        if not iid:
            continue
        label = classify(iid, entry.get("distinctive_tokens", []))
        {"helpful": helpful, "noise": noise, "unknown": unknown}[label].add(iid)

    # C.2 fallback-active backstop --------------------------------------------
    # For supplementary items (rb / guardrails), check if their times_active
    # counter has incremented since retrieve time. A positive delta means
    # guardrail-check.py matched the trigger condition during this goal's
    # execution — deterministic evidence of relevance, stronger than token
    # overlap. Promote to helpful regardless of where token classification
    # placed them: noise (0 token matches), unknown (between 0 and threshold),
    # or already helpful (no-op). The trigger-fire signal overrides token
    # heuristics.
    active_promotions = 0
    supp_detail = session.get("supplementary_detail", []) or []
    if supp_detail:
        live_idx = _live_times_active_index()
        for entry in supp_detail:
            iid = entry.get("id")
            if not iid:
                continue
            baseline = entry.get("times_active_at_retrieve")
            if not isinstance(baseline, int):
                continue
            current = live_idx.get(iid)
            if not isinstance(current, int):
                continue
            if current > baseline and iid not in helpful:
                # Pull from whichever bucket currently holds it
                noise.discard(iid)
                unknown.discard(iid)
                helpful.add(iid)
                active_promotions += 1

    stats = {
        "confidence": confidence,
        "goal_text_chars": len(goal_text),
        "diary_chars": len(diary_text),
        "combined_token_count": len(combined_tokens),
        "helpful": len(helpful),
        "noise": len(noise),
        "unknown": len(unknown),
        "active_promotions": active_promotions,
    }
    return helpful, noise, unknown, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Apply utilization feedback from retrieval-session.json"
    )
    parser.add_argument("--goal", required=True, help="Goal ID (must match session file)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--helpful", type=str, default=None,
                       help="Comma-separated IDs of helpful items (others marked noise)")
    group.add_argument("--all-helpful", action="store_true",
                       help="Mark all items as helpful")
    group.add_argument("--all-noise", action="store_true",
                       help="Mark all items as noise (LEGACY; poisons times_noise — "
                            "use --all-unknown for the backstop case)")
    group.add_argument("--all-unknown", action="store_true",
                       help="Mark every retrieved item as unclassified — no counter "
                            "changes, just records utilization_method=all_unknown so "
                            "the retrieval-session is no longer pending. The preferred "
                            "backstop: phase-4-26-gate still blocks goal completion "
                            "(same as --all-noise) but no times_noise pollution.")
    group.add_argument("--infer", action="store_true",
                       help="Heuristic inference: match distinctive_tokens against "
                            "execution diary + guardrail triggers to classify each item "
                            "as helpful / noise / unknown. Requires schema_version >= 2 "
                            "retrieval-session.json (exits 4 otherwise so a backstop can fall back).")
    parser.add_argument("--confidence", choices=("conservative", "balanced"),
                        default="conservative",
                        help="--infer distinctive-token threshold: conservative (>=2, default) "
                             "or balanced (>=1). Conservative avoids signal poisoning.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the decision without writing any counters.")
    args = parser.parse_args()

    if not SESSION_PATH or not SESSION_PATH.exists():
        print(json.dumps({"status": "no_session_file", "message": "No retrieval-session.json found"}))
        sys.exit(0)

    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        session = json.load(f)

    # Validate goal ID matches (prevents stale feedback)
    if session.get("goal_id") != args.goal:
        print(json.dumps({
            "status": "goal_mismatch",
            "session_goal": session.get("goal_id"),
            "requested_goal": args.goal,
        }))
        sys.exit(0)

    # Idempotency guard — with ONE sanctioned supersede path ().
    #
    # iteration-close.sh do_state_update runs `--infer` immediately BEFORE
    # phase-4-26-gate.sh (it is the PRODUCER for the flag that gate consumes),
    # so by the time the gate refuses — "method=infer with helpful=0 ... run
    # utilization-feedback.sh manually with explicit --helpful items" — the
    # session is ALREADY closed and that instructed recovery returned
    # already_processed. The recovery was unreachable by construction, leaving
    # --no-retrieval-applicable as the only exit, which is FALSE whenever
    # retrieval genuinely helped. The gate's own escape hatch therefore
    # corrupted the signal the gate exists to protect. Observed 4x in 2 days
    # (, , , ).
    #
    # An EXPLICIT-POSITIVE verdict superseding a NON-POSITIVE one is a
    # CORRECTION, not a double-count. The three non-positive methods each
    # leave times_helpful untouched: `--infer` increments
    # times_inferred_helpful, `--all-noise` increments times_noise, and
    # `--all-unknown` increments nothing at all. So the explicit pass adds
    # signal on a counter the prior pass never wrote. Explicit-positive over
    # explicit-positive (manual / all_helpful) stays refused — THAT would be a
    # genuine double-count of times_helpful.
    #
    # all_noise/all_unknown were added to this set on 2026-07-26 ()
    # after the original infer-only fix proved too narrow: the same unreachable
    # recovery reappeared one iteration later with method=all_noise. The gate's
    # instruction ("run utilization-feedback.sh manually with explicit --helpful
    # items") is identical in both cases, so restricting the supersede to
    # `infer` left the instruction still-unfollowable — and left
    # --no-retrieval-applicable, a FALSE assertion whenever retrieval ran, as
    # the only exit. The defect is the mismatch between what the gate tells you
    # to do and what this function permits; it is not specific to `infer`.
    SUPERSEDABLE_METHODS = ("infer", "all_noise", "all_unknown")
    prior_method = session.get("utilization_method")
    is_explicit = bool(args.helpful) or args.all_helpful
    superseding_infer = False
    if not session.get("utilization_pending", False):
        if prior_method in SUPERSEDABLE_METHODS and is_explicit:
            superseding_infer = True   # fall through and record the correction
        else:
            print(json.dumps({
                "status": "already_processed",
                "completed_at": session.get("utilization_completed_at"),
                "method": prior_method,
                # Name the reachable correction so a caller that hits this
                # is not left guessing, the way the gate's instruction did.
                "hint": ("an explicit --helpful/--all-helpful call may supersede a "
                         "non-positive verdict (method in "
                         f"{list(SUPERSEDABLE_METHODS)}); nothing may supersede an "
                         "explicit-positive one"),
            }))
            sys.exit(0)

    # Determine which items are helpful vs noise (vs inferred_helpful vs unknown)
    tree_nodes = session.get("tree_nodes_loaded", [])
    supp_items = session.get("supplementary_items", [])
    all_tree_set = set(tree_nodes)
    all_supp_set = {s["id"] for s in supp_items}

    # Partitions fed to update_tree_nodes / increment_supplementary
    tree_helpful = []              # authoritative helpful (times_helpful++)
    tree_inferred_helpful = []     # --infer hits (times_inferred_helpful++)
    tree_noise = []                # marked noise (times_noise++)
    supp_helpful = []
    supp_inferred_helpful = []
    supp_noise = []
    unknown_ids = []               # skipped entirely — no counter change

    utilization_method = None
    inference_stats = None

    if args.infer:
        infer_result = infer_feedback(session, confidence=args.confidence)
        if infer_result is None:
            # Schema too old to carry distinctive_tokens; surface this so the
            # hook/caller can fall back to --all-noise.
            print(json.dumps({
                "status": "inference_disabled",
                "reason": "session schema_version < 2 (retrieval did not persist distinctive_tokens)",
                "goal_id": args.goal,
            }, indent=2))
            sys.exit(4)
        helpful_set, noise_set, unknown_set, inference_stats = infer_result
        utilization_method = "infer"
        tree_inferred_helpful = [k for k in tree_nodes if k in helpful_set]
        tree_noise = [k for k in tree_nodes if k in noise_set]
        supp_inferred_helpful = [s for s in supp_items if s["id"] in helpful_set]
        supp_noise = [s for s in supp_items if s["id"] in noise_set]
        unknown_ids = sorted(unknown_set)

    elif args.all_helpful:
        utilization_method = "all_helpful"
        tree_helpful = list(tree_nodes)
        supp_helpful = list(supp_items)
    elif args.all_noise:
        utilization_method = "all_noise"
        tree_noise = list(tree_nodes)
        supp_noise = list(supp_items)
    elif args.all_unknown:
        # No-op on counters. Items keep their existing times_helpful /
        # times_inferred_helpful / times_noise. retrieval_count was already
        # bumped at retrieve time. unknown_ids is populated for audit symmetry
        # with --infer's unknown bucket.
        utilization_method = "all_unknown"
        unknown_ids = sorted(list(all_tree_set | all_supp_set))
    else:  # --helpful "key1,key2"
        utilization_method = "manual"
        helpful_ids = {k.strip() for k in args.helpful.split(",") if k.strip()}
        tree_helpful = [k for k in tree_nodes if k in helpful_ids]
        tree_noise = [k for k in tree_nodes if k not in helpful_ids]
        supp_helpful = [s for s in supp_items if s["id"] in helpful_ids]
        supp_noise = [s for s in supp_items if s["id"] not in helpful_ids]

    if superseding_infer:
        # The superseded pass ALREADY applied its noise verdict to these same
        # items (infer per-item, all_noise to every item; all_unknown applied
        # none), and the explicit paths above mark everything not-named as
        # noise — so re-applying here would double-count times_noise. A
        # supersede contributes ONLY the corrective helpful increments.
        #
        # KNOWN RESIDUE (): an item the superseded pass marked noise
        # that this explicit pass marks HELPFUL keeps that earlier times_noise++.
        # The correction can add times_helpful but cannot retract the earlier
        # noise increment, so such an entry reads as both. Deliberately not
        # decrementing here — a counter rollback is a separate, riskier change
        # than making honest positive signal recordable at all. The residue is
        # widest under all_noise, which marked EVERY item noise ().
        tree_noise = []
        supp_noise = []

    if args.dry_run:
        print(json.dumps({
            "status": "dry_run",
            "superseding_infer": superseding_infer,
            "utilization_method": utilization_method,
            "inference_stats": inference_stats,
            "tree_helpful": tree_helpful,
            "tree_inferred_helpful": tree_inferred_helpful,
            "tree_noise": tree_noise,
            "supp_helpful": [s["id"] for s in supp_helpful],
            "supp_inferred_helpful": [s["id"] for s in supp_inferred_helpful],
            "supp_noise": [s["id"] for s in supp_noise],
            "unknown": unknown_ids,
        }, indent=2))
        sys.exit(0)

    # Apply tree node feedback (atomic batch)
    h_count, n_count, ih_count = update_tree_nodes(
        tree_helpful, tree_noise, inferred_helpful_keys=tree_inferred_helpful
    )

    # Apply supplementary item feedback
    supp_h = 0
    supp_ih = 0
    supp_n = 0
    for item in supp_helpful:
        increment_supplementary(item["id"], item["type"], "times_helpful")
        supp_h += 1
    for item in supp_inferred_helpful:
        increment_supplementary(item["id"], item["type"], "times_inferred_helpful")
        supp_ih += 1
    for item in supp_noise:
        increment_supplementary(item["id"], item["type"], "times_noise")
        supp_n += 1

    # C.3: bump times_inferred_unknown for supplementary items still in the
    # unknown bucket (or all of them on --all-unknown). After bump, check if
    # the live count crossed unknown_threshold — if yes, set
    # auto_flagged_for_review=true on the record so B.1's candidate filter
    # picks it up regardless of evidence.
    supp_iu = 0
    auto_flagged = 0
    if utilization_method in ("infer", "all_unknown"):
        threshold = _load_unknown_threshold()
        # Build {id: type} from supp_items so we can route per-id.
        supp_type_by_id = {s["id"]: s["type"] for s in supp_items}
        for iid in unknown_ids:
            item_type = supp_type_by_id.get(iid)
            if item_type not in ("reasoning_bank", "guardrail"):
                continue
            increment_supplementary(iid, item_type, "times_inferred_unknown")
            supp_iu += 1
            # Live read AFTER increment so we see the post-bump count. This
            # double-checks the increment landed on disk before we flag —
            # if the subprocess silently failed, current == previous, the
            # threshold check fails, and we don't flag spuriously.
            current = _current_inferred_unknown(iid, item_type)
            if current >= threshold:
                # Pass the literal lowercase "true" — parse_value() in
                # reasoning-bank.py only treats "true"/"false" (case-sensitive)
                # as bools. str(True) is "True" with a capital T which would
                # fall through and store the string "True" instead of the
                # bool. Don't change "true" here.
                set_supplementary_field(iid, item_type,
                                         "auto_flagged_for_review", "true")
                auto_flagged += 1

    # M-6: Update per-module health metrics.
    # Groups tree nodes by their top-level category (first path segment) and
    # supplementary items by store type, then records each invocation outcome
    # into world/module-health.yaml.  Fail-soft: module_health import or
    # write errors are logged but never break the feedback flow.
    _m6_updates = 0
    try:
        from module_health import load_module_health, save_module_health, record_invocation as mh_record

        mh = load_module_health(WORLD_DIR)

        # Tree nodes -> top-level category.  Key shape: "execution/sub/leaf"
        # -> category "execution".  Root-level keys (no slash) use the key
        # itself as the category.
        def _tree_cat(key):
            return key.split("/")[0] if "/" in key else key

        # Map item outcome from the per-item partitions above.
        for key in tree_helpful:
            mh_record(mh, _tree_cat(key), "helpful")
            _m6_updates += 1
        for key in tree_inferred_helpful:
            mh_record(mh, _tree_cat(key), "helpful")
            _m6_updates += 1
        for key in tree_noise:
            mh_record(mh, _tree_cat(key), "noise")
            _m6_updates += 1

        # Supplementary items -> store type.  Type field values are
        # "reasoning_bank", "guardrail", "pattern_signature".  Normalize
        # "guardrail" -> "guardrails" and "pattern_signature" ->
        # "pattern_signatures" to match SUPPLEMENTARY_MODULES.
        _TYPE_NORMALIZE = {
            "reasoning_bank": "reasoning_bank",
            "guardrail": "guardrails",
            "guardrails": "guardrails",
            "pattern_signature": "pattern_signatures",
            "pattern_signatures": "pattern_signatures",
        }
        for item in supp_helpful:
            store = _TYPE_NORMALIZE.get(item.get("type", ""), item.get("type", ""))
            mh_record(mh, store, "helpful")
            _m6_updates += 1
        for item in supp_inferred_helpful:
            store = _TYPE_NORMALIZE.get(item.get("type", ""), item.get("type", ""))
            mh_record(mh, store, "helpful")
            _m6_updates += 1
        for item in supp_noise:
            store = _TYPE_NORMALIZE.get(item.get("type", ""), item.get("type", ""))
            mh_record(mh, store, "noise")
            _m6_updates += 1

        if _m6_updates > 0:
            save_module_health(WORLD_DIR, mh)
    except Exception as e:
        print(f"[utilization-feedback] M-6 module health update failed "
              f"(non-fatal): {e}", file=sys.stderr)

    # Mark as processed (atomic write) — persist method + inference stats for audit
    session["utilization_pending"] = False
    session["utilization_completed_at"] = now_str()
    session["utilization_method"] = utilization_method
    if superseding_infer:
        # Keep the correction auditable: without this the record would just
        # read "manual" and the earlier inferred verdict (whose noise
        # increments are still on the entries) would vanish from the history.
        session["superseded_method"] = "infer"
        session["superseded_at"] = session["utilization_completed_at"]
    if inference_stats is not None:
        session["inference_stats"] = inference_stats
    tmp = Path(str(SESSION_PATH) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(SESSION_PATH))

    result = {
        "status": "completed",
        "goal_id": args.goal,
        "utilization_method": utilization_method,
        "tree_nodes": {"helpful": h_count, "inferred_helpful": ih_count, "noise": n_count},
        "supplementary": {"helpful": supp_h, "inferred_helpful": supp_ih,
                          "noise": supp_n, "inferred_unknown": supp_iu,
                          "auto_flagged": auto_flagged},
        "unknown": len(unknown_ids),
        "module_health_updates": _m6_updates,
    }
    if inference_stats is not None:
        result["inference_stats"] = inference_stats
    print(json.dumps(result, indent=2))
    print(f"[utilization-feedback] {args.goal}: method={utilization_method} "
          f"tree(h={h_count} ih={ih_count} n={n_count}) "
          f"supp(h={supp_h} ih={supp_ih} n={supp_n} iu={supp_iu} "
          f"flagged={auto_flagged}) unknown={len(unknown_ids)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
