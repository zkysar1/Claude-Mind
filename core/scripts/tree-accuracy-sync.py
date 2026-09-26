#!/usr/bin/env python3
# tree-accuracy-sync.py — sync hypothesis-category accuracy into tree nodes.
#
# Per-node accuracy was previously LLM-invoked via /review-hypotheses Tree
# Update Protocol Step 2 (SKILL.md), which silently degraded under context
# pressure (). This converts that discretionary step into a
# bash-gated obligation fired from iteration-close.sh learning-gate on deep
# outcomes (, mirrors the  retrieval-stub pattern).
#
# Reads pipeline.jsonl resolved + archived records, groups by category,
# maps each category to its primary leaf node via tree-find-node, updates
# node.accuracy, node.sample_size, node.confidence via tree-update --batch.
# Idempotent: records no-op when values already match.
#
# capability_level is NOT written — it derives from confidence via
# _graduate_node_level during propagate/reconcile. See core/scripts/tree.py.

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _paths  # noqa: E402
from tree import read_tree, write_tree, _load_competence_config, _graduate_node_level, _stamp_progression, _stamp_calibration  # noqa: E402
from tree_match import find_nodes  # noqa: E402
from pipeline import read_jsonl, LIVE_PATH, ARCHIVE_PATH  # noqa: E402

# Category→node bindings are cached here. find_nodes scoring shifts as
# confidence/summary values change across runs, so without a cache the same
# category would re-map to different nodes each iteration — defeating
# idempotency. Cache stabilizes the mapping; new entries accrete as new
# categories appear in the hypothesis pipeline.
BINDINGS_PATH = _paths.META_DIR / "hypothesis-category-bindings.json"


def load_pipeline_records():
    """Collect resolved + archived records with a CONFIRMED/CORRECTED outcome.
    Reads pipeline files directly via pipeline.read_jsonl — avoids bash
    subprocess calls which hang on Windows due to cp1252/UTF-8 decoding
    quirks in the subprocess reader threads."""
    records = []
    # Resolved records live in LIVE_PATH (pipeline.jsonl), filtered by stage
    for r in read_jsonl(LIVE_PATH):
        if r.get("stage") == "resolved" and r.get("outcome") in ("CONFIRMED", "CORRECTED") and r.get("category"):
            records.append(r)
    # Archived records live in ARCHIVE_PATH
    for r in read_jsonl(ARCHIVE_PATH):
        if r.get("outcome") in ("CONFIRMED", "CORRECTED") and r.get("category"):
            records.append(r)
    return records


def group_by_category(records):
    """Return {category: {'confirmed': N, 'total': N}}."""
    groups = {}
    for r in records:
        cat = r["category"]
        g = groups.setdefault(cat, {"confirmed": 0, "total": 0})
        g["total"] += 1
        if r["outcome"] == "CONFIRMED":
            g["confirmed"] += 1
    return groups


def load_bindings():
    """Load the persisted category→node map WITH provenance.

    Returns (bindings, status):
      "absent"     — no cache file yet; an empty map that is safe to persist.
      "ok"         — read and parsed.
      "unreadable" — the file EXISTS but could not be read/parsed (corrupt,
                     truncated, locked). The caller MUST NOT persist over it:
                     an unreadable-but-present cache read as {} would let a real
                     run save_bindings() a wholesale replacement, wiping every
                     pin including hand repairs (bravo fresh-eyes, rb-6775 /
                     guard-3205, g-115-5883).
    """
    if not BINDINGS_PATH.exists():
        return {}, "absent"
    try:
        return (json.loads(BINDINGS_PATH.read_text(encoding="utf-8")) or {}), "ok"
    except (json.JSONDecodeError, OSError):
        return {}, "unreadable"


def save_bindings(bindings):
    BINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BINDINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bindings, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(BINDINGS_PATH)


# ── match-quality floor for category→node bindings () ──────────────
# resolve_node_for_category took find_nodes(...)[0] with NO quality bar, so a
# SHORT node key (< MIN_KEY_TOKEN_LEN chars: `sol`, `pip`, `deb`) matched as a
# SUBSTRING inside a longer category word (`sol` in con-SOL-idation, `pip` in
# PIP-eline) became a silent binding that wrote hypothesis accuracy onto an
# unrelated node. `_is_short_key_substring_collision` refuses exactly and only
# that class — the zero-false-positive subset of the defect.
#
# It is DELIBERATELY narrower than a whole-token floor. Measured 2026-09-25
# (alpha, cc-10) over the live 164 bindings: a whole-token floor would reject 27,
# but several are legitimate (arc-agi-3 → grid-perception-decomposition shares no
# ≥4-char token yet is a real match; arc/arc-agi tokenize to nothing), and it
# still keeps the semantic collision system-behavior → npc-behavior-* (shared
# whole token `behavior`). So the SEMANTIC classification is left to the reducer
# (it needs summary judgment, not token counting); this guard refuses only the
# short-key substring class, which no legitimate binding relies on.
MIN_KEY_TOKEN_LEN = 4


def _sig_tokens(text):
    """Whole tokens of `text`, lowercased, split on non-alphanumerics."""
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def _is_short_key_substring_collision(category, node_key):
    """True when EVERY token of node_key is shorter than MIN_KEY_TOKEN_LEN AND
    none is a WHOLE token of the category — i.e. find_nodes could only have
    matched this node by a short substring inside a longer category word. A
    legitimate short-key binding (the category actually contains that whole
    short token) is NOT flagged."""
    key_toks = _sig_tokens(node_key)
    if not key_toks or any(len(t) >= MIN_KEY_TOKEN_LEN for t in key_toks):
        return False  # key carries a real (≥4-char) token — not this class
    return not (key_toks & _sig_tokens(category))


def _has_whole_token_support(category, node_key, summary):
    """Advisory (NOT a refusal): does the category share a ≥MIN_KEY_TOKEN_LEN
    whole token with the node key, or does a category token appear as a whole
    word in the node summary? Absence is REPORTED, never unbound — the census
    shows some no-support bindings are legitimate summary/semantic matches, so
    unbinding them would drop real work. The reducer judges them (g-115-5883)."""
    cat = {t for t in _sig_tokens(category) if len(t) >= MIN_KEY_TOKEN_LEN}
    if not cat:
        return True  # nothing ≥4 chars to assert against — do not flag
    if cat & _sig_tokens(node_key):
        return True
    low = (summary or "").lower()
    return any(re.search(r"\b" + re.escape(t) + r"\b", low) for t in cat)


def resolve_node_for_category(category, nodes, entity_index, bindings):
    """Return the node dict for this category.
    Pin via bindings cache so confidence updates don't re-route future
    runs to different nodes. First call does the find + persists; later
    calls hit the cache. If cached node no longer exists, re-resolve.

    A short-key substring collision (g-115-5883) is REFUSED here — cached or
    freshly derived — and the category is left UNRESOLVED so no accuracy is
    written onto an unrelated node. Refusing on both branches makes an existing
    bad pin (e.g. memory-consolidation → sol) self-heal without a cache wipe:
    the TRAP that nulling re-derives the identical collision is defused because
    the re-derivation is refused too."""
    cached = bindings.get(category)
    if cached and cached in nodes:
        if _is_short_key_substring_collision(category, cached):
            bindings.pop(category, None)  # drop the bad pin; fall through
        else:
            node = nodes[cached]
            return {
                "key": cached,
                "file": node.get("file", ""),
                "depth": node.get("depth", 0),
                "summary": node.get("summary", ""),
                "node_type": "leaf" if not node.get("children") else "interior",
            }
    results = find_nodes(category, nodes, entity_index, top=1, leaf_only=True)
    if not results:
        return None
    if _is_short_key_substring_collision(category, results[0]["key"]):
        bindings.pop(category, None)  # never persist a short-key collision
        return None
    bindings[category] = results[0]["key"]
    return results[0]


def read_node_fields(node_key, nodes):
    """Return {'accuracy', 'sample_size', 'confidence'} from in-memory tree."""
    node = nodes.get(node_key)
    if not node:
        return None
    return {
        "accuracy": node.get("accuracy"),
        "sample_size": node.get("sample_size"),
        "confidence": node.get("confidence"),
    }


def compute_confidence(confirmed, total):
    """Laplace-smoothed accuracy: (confirmed + 1) / (total + 2).

    Keeps small samples from hitting 0.0/1.0 (e.g., 1/1 → 0.67 not 1.00).
    Matches Bayesian calibration pattern used elsewhere in the framework.
    """
    return round((confirmed + 1) / (total + 2), 3)


def close_enough(old, new, tol=0.01):
    """Idempotency check. Treat None as always-different."""
    if old is None or new is None:
        return old is None and new is None
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < tol
    return old == new


def plan_updates(groups, tree, min_sample_size=1, apply_confidence_min=3, persist_bindings=True):
    """Resolve each category → node and build the update plan.
    Returns (plan, summary). Plan is a list of {node_key, mutations}
    tuples where mutations is {field: new_value}.
    persist_bindings=False (the --dry-run path) leaves the shared bindings
    cache untouched: a preview must not pin a fuzzy match it only proposed.
    """
    nodes = tree.get("nodes", {})
    entity_index = tree.get("entity_index", {})
    bindings, bindings_status = load_bindings()
    bindings_before = dict(bindings)

    summary = {
        "categories_scanned": len(groups),
        "nodes_resolved": 0,
        "nodes_updated": 0,
        "nodes_unchanged": 0,
        "nodes_unresolved": 0,
        "updates": [],
        "unresolved_categories": [],
        "new_bindings": [],
        "short_key_refused": [],
        "low_quality_bindings": [],
    }

    # One node may be the target for multiple categories — aggregate first.
    node_data = {}  # node_key → {'confirmed', 'total', 'categories':[...]}
    for category, counts in groups.items():
        if counts["total"] < min_sample_size:
            continue
        node = resolve_node_for_category(category, nodes, entity_index, bindings)
        if not node:
            summary["unresolved_categories"].append(category)
            summary["nodes_unresolved"] += 1
            prev = bindings_before.get(category)
            if prev and _is_short_key_substring_collision(category, prev):
                summary["short_key_refused"].append(
                    {"category": category, "node_key": prev})
            continue
        nk = node["key"]
        if bindings_before.get(category) != nk:
            summary["new_bindings"].append({"category": category, "node_key": nk})
        # Advisory-only (): a resolved binding with no whole-token
        # support is surfaced for review, NOT refused — the reducer judges whether
        # it is a legitimate summary/semantic match or a collision to re-point.
        if not _has_whole_token_support(category, nk, node.get("summary", "")):
            summary["low_quality_bindings"].append(
                {"category": category, "node_key": nk})
        agg = node_data.setdefault(nk, {"confirmed": 0, "total": 0, "categories": []})
        agg["confirmed"] += counts["confirmed"]
        agg["total"] += counts["total"]
        agg["categories"].append(category)

    # Persist bindings so the next run skips find_nodes for known categories.
    # Never on --dry-run (, 2026-09-24): a dry-run pinned
    # hypothesis-pipeline -> `pip`, an unrelated product-spec node, in the shared
    # cache, so the next real run would have written accuracy onto it.
    # Never over an UNREADABLE cache (): the map we hold was read as {}
    # from a present-but-corrupt file, so saving it would wholesale-replace every
    # pin, hand repairs included (bravo fresh-eyes, rb-6775 / guard-3205).
    if persist_bindings and bindings_status != "unreadable":
        save_bindings(bindings)
    elif persist_bindings and bindings_status == "unreadable":
        summary["bindings_persist_skipped"] = "cache unreadable — not overwriting"

    summary["nodes_resolved"] = len(node_data)

    plan = []
    for node_key, agg in sorted(node_data.items()):
        accuracy = round(agg["confirmed"] / agg["total"], 3) if agg["total"] > 0 else None
        sample_size = agg["total"]
        current = read_node_fields(node_key, nodes) or {}

        mutations = {}
        if not close_enough(current.get("accuracy"), accuracy):
            mutations["accuracy"] = accuracy
        if not close_enough(current.get("sample_size"), sample_size):
            mutations["sample_size"] = sample_size

        confidence_update = None
        if sample_size >= apply_confidence_min:
            confidence_update = compute_confidence(agg["confirmed"], agg["total"])
            if not close_enough(current.get("confidence"), confidence_update):
                mutations["confidence"] = confidence_update

        if mutations:
            plan.append((node_key, mutations))
            summary["nodes_updated"] += 1
            summary["updates"].append({
                "node_key": node_key,
                "categories": agg["categories"],
                "confirmed": agg["confirmed"],
                "total": agg["total"],
                "accuracy": accuracy,
                "sample_size": sample_size,
                "confidence_written": confidence_update,
                "prior": current,
            })
        else:
            summary["nodes_unchanged"] += 1

    return plan, summary


def apply_plan(plan, tree, dry_run=False):
    """Apply mutations to the in-memory tree and write once. Also regrades
    capability_level for nodes whose confidence changed (matches behavior
    of tree-update --batch + propagate)."""
    if not plan:
        return
    nodes = tree.get("nodes", {})
    competence = _load_competence_config()
    capability_changes = []

    for node_key, mutations in plan:
        node = nodes.get(node_key)
        if not node:
            continue
        confidence_changed = "confidence" in mutations
        # DELIBERATELY a separate flag, not folded into confidence_changed
        # (guard-3116: do not mirror a neighbour's guard by analogy — ask what
        # question each field answers). confidence_changed additionally gates
        # _graduate_node_level; an accuracy-only edit must stamp WITHOUT
        # regrading capability_level, which is a confidence-derived axis.
        accuracy_changed = "accuracy" in mutations
        for field, value in mutations.items():
            node[field] = value
        # Stamps come AFTER the mutation loop and in the SAME pass (guard-3358:
        # write the group first and its selector key last — a group written
        # without its selector is indistinguishable from never having been
        # written once any merge runs, which is precisely the  defect).
        if accuracy_changed:
            _stamp_calibration(node)  # : durable under own-cloud merge
        if confidence_changed:
            _stamp_progression(node)  # : durable under own-cloud merge
            old_level, new_level = _graduate_node_level(node, competence)
            if old_level is not None:
                capability_changes.append({
                    "key": node_key,
                    "old_level": old_level,
                    "new_level": new_level,
                    "confidence": node.get("confidence"),
                })

    if not dry_run:
        tree["nodes"] = nodes
        write_tree(tree)

    return capability_changes


def main():
    ap = argparse.ArgumentParser(description="Sync hypothesis-category accuracy into tree nodes.")
    ap.add_argument("--dry-run", action="store_true", help="Print planned operations without writing.")
    ap.add_argument("--min-sample", type=int, default=1, help="Minimum records per category to consider (default 1).")
    ap.add_argument("--confidence-min-sample", type=int, default=3, help="Minimum sample_size to write confidence (default 3).")
    ap.add_argument("--quiet", action="store_true", help="Suppress summary output unless updates occurred.")
    args = ap.parse_args()

    records = load_pipeline_records()
    if not records:
        if not args.quiet:
            print("[tree-accuracy-sync] no resolved/archived records with outcome+category — skipping")
        return 0

    groups = group_by_category(records)
    tree = read_tree()
    plan, summary = plan_updates(
        groups,
        tree,
        min_sample_size=args.min_sample,
        apply_confidence_min=args.confidence_min_sample,
        persist_bindings=not args.dry_run,
    )

    if plan:
        try:
            capability_changes = apply_plan(plan, tree, dry_run=args.dry_run)
            summary["capability_changes"] = capability_changes or []
            summary["batch_applied"] = not args.dry_run
        except Exception as e:
            print(f"[tree-accuracy-sync] ERROR: apply_plan failed: {e}", file=sys.stderr)
            summary["batch_exit_code"] = 1
            summary["batch_error"] = str(e)
            print(json.dumps(summary, indent=2))
            return 1

    if args.quiet and summary["nodes_updated"] == 0:
        return 0

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
