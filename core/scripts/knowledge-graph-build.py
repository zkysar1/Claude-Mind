#!/usr/bin/env python3
# domain-leak-exempt: framework knowledge-graph extractor; the entity-ID regexes
#   (rb-NNN, guard-NNN, g-NNN-NN, etc.) are framework record identifiers --
#   functional pattern data, not illustrative domain examples.
"""knowledge-graph-build.py -- Build the Mind knowledge-graph triple-store substrate.

Reads the Gap-5 bi-temporal records -- reasoning-bank.jsonl, guardrails.jsonl, and
the knowledge-tree node front matter / bodies -- and emits entity->relation->entity
plus entity->source-node provenance triples into meta/knowledge-graph.jsonl.

BRD Gap 1b+1c, child 1/4 of g-306-15. Evidence: HippoRAG (2405.14831) OpenIE->graph
step. The downstream children build Personalized PageRank over this graph
(g-306-43), blend it into retrieve.sh (g-306-44), and validate multi-hop retrieval
(g-306-45).

MIND-SCOPED -- READ THIS BEFORE EXTENDING.
  This is the Mind FRAMEWORK's OWN knowledge graph over its reasoning/guardrail/tree
  stores. It is NOT, and MUST NOT be conflated with, the NPC behavior pipeline's
  all-MiniLM-L6-v2 sentence embeddings (a separate product-side artifact). The two
  never share storage, vocabulary, or code. This graph indexes the symbolic record
  identifiers (rb-/guard-/g-/sig-/asp-/node-keys) and their cross-references -- a
  purely lexical/structural graph, no neural embedding, deterministic and inspectable
  (the framework's design value: "structured, inspectable, evolvable reasoning").

WRITE MODEL (guard-832 analysis -- why this is compliant):
  knowledge-graph.jsonl is a SINGLE-WRITER DERIVED store: only this extractor writes
  it. guard-832 guards against own-cloud multi-writer-append RACES (a bulk local
  rewrite losing a concurrent peer's appended record). That race cannot occur here
  -- there is no peer writer to lose. The write uses _fileops.locked_write_jsonl
  (lock -> history snapshot -> atomic tempfile+os.replace -> changelog -> unlock),
  which its own docstring sanctions for "single-owner / full-rewrite content where
  last-writer-wins is intended". Semantics are ADDITIVE: existing triples are read
  and unioned with the freshly-extracted set (dedup by triple key) before the atomic
  write, so no triple is ever lost and re-runs are idempotent (deterministic
  extraction -> stable union -> byte-stable output for unchanged sources).

Usage:
  knowledge-graph-build.py                 # dry-run: report triple counts, no write
  knowledge-graph-build.py --apply         # build + atomic write meta/knowledge-graph.jsonl
  knowledge-graph-build.py --output json   # machine-readable summary to stdout
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _fileops import read_jsonl_with_recovery, locked_write_jsonl  # noqa: E402
from _paths import WORLD_DIR, META_DIR  # noqa: E402

_TREE_SUBDIR = ("knowledge", "tree")
_OUTPUT_NAME = "knowledge-graph.jsonl"

# Record-identifier reference regex. Matches the framework ID formats in CLAUDE.md:
#   rb-NNN guard-NNN sig-NNN asp-NNN bel-NNN trans-NNN sq-NNN pt-NNN sa-NNN
#   g-NNN-NN (goals, 2-4 digit second segment) and the older g-NNN form.
# Word-boundary anchored; case-sensitive (IDs are lowercase in the stores).
_REF_RE = re.compile(r"\b(?:rb|guard|sig|asp|bel|trans|sq|pt|sa|g)-\d+(?:-\d+)?\b")

# Front-matter text fields scanned for cross-references, per store.
_RB_TEXT_FIELDS = ("title", "content", "when_to_use", "failure_lesson", "description")
_GUARD_TEXT_FIELDS = ("rule", "trigger_condition", "when_to_use")


def _agent_name() -> str:
    return os.environ.get("AYOAI_AGENT", "system") or "system"


def _output_path() -> Path:
    return Path(META_DIR) / _OUTPUT_NAME


def _iso(value):
    """Coerce a temporal field to an ISO string (or None if falsy).

    The JSON stores (reasoning-bank, guardrails) carry valid_from/created as
    strings, but yaml.safe_load parses a tree-node front-matter
    `valid_from: 2026-06-03T00:00:00` into a datetime/date OBJECT. Left as-is
    that object reaches json.dumps inside locked_write_jsonl and raises
    'Object of type datetime is not JSON serializable'. Normalizing here keeps
    triples JSON-serializable AND byte-stable across re-runs (idempotency)."""
    if not value:
        return None
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def _stringify(value) -> str:
    """Flatten a record field (which may be a dict/list) to searchable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_stringify(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_stringify(v) for v in value.values())
    return str(value)


def _extract_refs(text: str):
    """Yield unique record-identifier references found in text."""
    seen = set()
    for m in _REF_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            yield m


def _parse_front_matter(md_path: Path):
    """Return (front_matter_dict, body_text). Front matter is the YAML block
    delimited by '---' on line 0 and the next '---'. Missing/malformed -> ({}, full)."""
    try:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, raw
    close = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            close = i
            break
    if close is None:
        return {}, raw
    fm_block = "".join(lines[1:close])
    body = "".join(lines[close + 1:])
    try:
        import yaml  # lazy: only needed for tree nodes
        fm = yaml.safe_load(fm_block) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    return fm, body


def _node_key(md_path: Path, fm: dict, tree_root: Path) -> str:
    """Stable node key: front-matter 'key' if present, else the path relative to
    the tree root with the .md suffix dropped (POSIX-separated)."""
    k = fm.get("key")
    if isinstance(k, str) and k.strip():
        return k.strip()
    try:
        rel = md_path.relative_to(tree_root).with_suffix("")
        return rel.as_posix()
    except ValueError:
        return md_path.stem


def _category_from_path(md_path: Path, tree_root: Path) -> "str | None":
    """First path segment under the tree root is the L1 category."""
    try:
        rel = md_path.relative_to(tree_root)
        parts = rel.parts
        if len(parts) >= 2:
            return parts[0]
    except ValueError:
        pass
    return None


def build_triples():
    """Deterministic extraction. Returns a list of triple dicts in stable order."""
    triples = []
    seen = set()

    def add(s, p, o, src, store, vf, vt):
        if not s or not o or s == o:
            return
        key = (s, p, o, src, store)
        if key in seen:
            return
        seen.add(key)
        triples.append({
            "s": s, "p": p, "o": o,
            "src": src, "store": store,
            "valid_from": _iso(vf),
            "valid_to": _iso(vt),
        })

    # --- reasoning-bank + guardrails (homogeneous structured stores) ---
    for store, fname, text_fields in (
        ("reasoning-bank", "reasoning-bank.jsonl", _RB_TEXT_FIELDS),
        ("guardrails", "guardrails.jsonl", _GUARD_TEXT_FIELDS),
    ):
        path = Path(WORLD_DIR) / fname
        if not path.exists():
            continue
        for rec in read_jsonl_with_recovery(path):
            if not isinstance(rec, dict):
                continue
            rid = rec.get("id")
            if not rid:
                continue
            vf = rec.get("valid_from") or rec.get("created")
            vt = rec.get("valid_to")
            cat = rec.get("category")
            if cat:
                add(rid, "has_category", "cat:" + str(cat), rid, store, vf, vt)
            for t in (rec.get("tags") or []):
                add(rid, "has_tag", "tag:" + str(t), rid, store, vf, vt)
            sg = rec.get("source_goal") or rec.get("origin_goal_id")
            if sg:
                add(rid, "derived_from", str(sg), rid, store, vf, vt)
            text = " ".join(_stringify(rec.get(f)) for f in text_fields)
            for ref in _extract_refs(text):
                add(rid, "references", ref, rid, store, vf, vt)

    # --- knowledge-tree nodes (heterogeneous front matter; reference-rich bodies) ---
    tree_root = Path(WORLD_DIR).joinpath(*_TREE_SUBDIR)
    if tree_root.exists():
        for md in sorted(tree_root.rglob("*.md")):
            fm, body = _parse_front_matter(md)
            node_key = _node_key(md, fm, tree_root)
            subject = "node:" + node_key
            vf = fm.get("valid_from") or fm.get("created") or fm.get("last_updated")
            cat = fm.get("category") or _category_from_path(md, tree_root)
            if cat:
                add(subject, "has_category", "cat:" + str(cat), node_key, "tree", vf, None)
            for t in (fm.get("tags") or []):
                add(subject, "has_tag", "tag:" + str(t), node_key, "tree", vf, None)
            for ref in _extract_refs(body):
                add(subject, "references", ref, node_key, "tree", vf, None)

    # Stable deterministic ordering for idempotent output.
    triples.sort(key=lambda t: (t["store"], t["src"], t["p"], t["o"]))
    return triples


def _triple_key(t: dict):
    return (t.get("s"), t.get("p"), t.get("o"), t.get("src"), t.get("store"))


def _read_existing(path: Path):
    if not path.exists():
        return []
    return [r for r in read_jsonl_with_recovery(path) if isinstance(r, dict)]


def run(apply: bool):
    out_path = _output_path()
    fresh = build_triples()
    existing = _read_existing(out_path)

    existing_keys = {_triple_key(t) for t in existing}
    new_triples = [t for t in fresh if _triple_key(t) not in existing_keys]

    # Additive union: existing preserved, new added; stable order.
    union = existing + new_triples
    union.sort(key=lambda t: (str(t.get("store")), str(t.get("src")), str(t.get("p")), str(t.get("o"))))

    # Distinct entity/predicate stats for the summary.
    entities = set()
    predicates = {}
    stores = {}
    for t in fresh:
        entities.add(t["s"])
        entities.add(t["o"])
        predicates[t["p"]] = predicates.get(t["p"], 0) + 1
        stores[t["store"]] = stores.get(t["store"], 0) + 1

    summary = {
        "output": str(out_path),
        "dry_run": not apply,
        "extracted_triples": len(fresh),
        "existing_triples": len(existing),
        "new_triples": len(new_triples),
        "total_after": len(union),
        "distinct_entities": len(entities),
        "by_predicate": predicates,
        "by_store": stores,
        "applied": False,
    }

    if apply and new_triples:
        locked_write_jsonl(out_path, union)
        summary["applied"] = True
    elif apply and not new_triples:
        # Idempotent no-op: nothing new to add. Still mark applied=True if the
        # file already matches (the union equals existing); create if absent+empty.
        if not out_path.exists():
            locked_write_jsonl(out_path, union)  # creates empty/seed file
            summary["applied"] = True
        else:
            summary["applied"] = False  # already up to date

    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Mind knowledge-graph triple-store.")
    ap.add_argument("--apply", action="store_true", help="Write meta/knowledge-graph.jsonl (default: dry-run).")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    summary = run(apply=args.apply)

    if args.output == "json":
        print(json.dumps(summary))
    else:
        print(f"knowledge-graph-build ({'APPLY' if args.apply else 'DRY-RUN'})")
        print(f"  output: {summary['output']}")
        print(f"  extracted={summary['extracted_triples']} existing={summary['existing_triples']} "
              f"new={summary['new_triples']} total_after={summary['total_after']}")
        print(f"  distinct_entities={summary['distinct_entities']}")
        print(f"  by_predicate={summary['by_predicate']}")
        print(f"  by_store={summary['by_store']}")
        print(f"  applied={summary['applied']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
