#!/usr/bin/env python3
"""learning-routing-audit.py — audit cross-references and doc pointers in learning stores.

Resolves the "cross-reference audit" follow-up scoped-out in
core/config/conventions/learning-routing.md "Follow-ups (out of scope for this pass)".

Three drift surfaces are audited:
  1. Dangling cross-references — an ID referenced by one store that doesn't
     exist in the referenced store (rb.preventive_guardrail → retired guardrail,
     experience.hypothesis_id → hypothesis not in pipeline, etc.).
  2. Doc pointer integrity — every `core/config/conventions/*.md` referenced
     in learning-routing.md points at a file that exists.
  3. Store-catalog parity — the "Ten Stores" header word count matches the
     actual row count in the table below it.

Invoked ad-hoc; wired into /verify-learning as a post-test check.

Exit codes:
  0  clean — no drift detected
  1  drift detected — report printed to stdout
  2  input error (missing required stores, unreadable files)
"""
import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import (  # type: ignore
    PROJECT_ROOT, WORLD_DIR, CORE_ROOT, CONFIG_DIR, agents_root,
    enumerate_agent_confs,
)

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


# Audit-script reads of JSONL files directly. The "access stores via scripts"
# rule binds the LLM's tool calls, not framework-internal Python scripts.
# Going through bash wrappers from Python hangs on Windows MinGW subprocess.

LEARNING_ROUTING_MD = CONFIG_DIR / "conventions" / "learning-routing.md"
TREE_YAML = WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"
RB_JSONL = WORLD_DIR / "reasoning-bank.jsonl"
GUARDRAILS_JSONL = WORLD_DIR / "guardrails.jsonl"
PIPELINE_JSONL = WORLD_DIR / "pipeline.jsonl"
SIGNATURES_JSONL = WORLD_DIR / "pattern-signatures.jsonl"

# Set by load_all_experiences(); None until it runs.
_AGENT_CORPUS_OWNED = None


def _canonical_world_dirs():
    """Worlds that the agents_root() experience corpus belongs to."""
    cands = set()
    for p in (PROJECT_ROOT / ".mind-data" / "world", PROJECT_ROOT / "world"):
        try:
            cands.add(p.resolve())
        except OSError:
            pass
    for conf in enumerate_agent_confs():
        try:
            text = conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("WORLD_PATH="):
                value = line.split("=", 1)[1].strip()
                if value:
                    try:
                        cands.add(Path(value).resolve())
                    except OSError:
                        pass
    return cands


def world_owns_agent_corpus():
    """Whether the world under audit owns the per-agent experience corpus.

    The experience records under agents_root() belong to THIS project's own
    world. When WORLD_DIR has been redirected — `MIND_WORLD` pointing at a
    tmp fixture world, which is what every hermetic test does — those records
    are FOREIGN to the world being audited: nothing in the fixture can resolve
    their hypothesis_id or tree_nodes_related, so every ref reads dangling.

    That is not a reporting nicety. `learning-routing-repair.py --apply` NULLS
    whatever the audit calls dangling, writing to the files it resolves — which
    are the REAL agent files, because agents_root() is PROJECT_ROOT-based and
    no world override moves it. Measured 2026-08-10 (cc-05): an EMPTY tmp world
    produced 2,739 dangling refs across 12 real agent files, every one of them
    valid. tree.py::_post_remove_sweep_dangling runs that repair with --apply on
    every tree-node removal, so three ordinary tree tests were one fast machine
    away from mass-nulling six agents' experience stores. What actually stopped
    it was the 30s subprocess timeout expiring during the READ phase, before any
    write — accidental protection that any performance work would have removed.
    """
    try:
        return WORLD_DIR.resolve() in _canonical_world_dirs()
    except OSError:
        return False


def _read_jsonl(path, active_only=False):
    """Load a JSONL file into a list of records. Skips blank lines and malformed records."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if active_only and rec.get("status") not in (None, "active", "discovered"):
            continue
        records.append(rec)
    return records


def load_reasoning_bank():
    return _read_jsonl(RB_JSONL, active_only=True)


def load_guardrails():
    # active_only=True is intentional: an RB entry linked to a RETIRED guardrail
    # is stale knowledge (the rule no longer applies) and must surface as dangling.
    # Do not widen this filter to include retired — that would mask real drift.
    return _read_jsonl(GUARDRAILS_JSONL, active_only=True)


def load_pipeline():
    return _read_jsonl(PIPELINE_JSONL)


def load_pattern_signatures():
    return _read_jsonl(SIGNATURES_JSONL)


def load_all_experiences():
    """Read every agent's experience.jsonl AND experience-archive.jsonl.

    Cross-refs in shared-world stores can point at experiences from any agent.
    Single-agent loading would produce false-positive 'dangling' verdicts on
    valid cross-agent links.

    TWO defects fixed 2026-08-10 (g-115-5646), both of which manufactured
    exactly the false positives this function exists to prevent:

    1. DEPTH-1 GLOB. This read ``PROJECT_ROOT.glob("*/experience.jsonl")``,
       which matches NOTHING post-relocation (agent dirs live at
       ``PROJECT_ROOT/agents/<name>``), so it returned ZERO records and every
       experience_ref in the corpus read as dangling. Route through
       ``agents_root()`` — the reference pattern named in CLAUDE.md
       "cross-agent glob consumers" and required by guard-1318. This file was
       invisible to all three audit greps there (no constant, no literal
       ``agents/*``, no ``.parent``), which is why it survived.
    2. ARCHIVE-BLIND. Live-only reads missed the 36.2% of the corpus that has
       aged into experience-archive.jsonl.

    Measured impact before the fix: the audit reported 319 dangling
    cross-references, of which 305 (95.6%) existed in the true corpus. That
    number is not cosmetic — ``learning-routing-repair.py --apply`` NULLS every
    field the audit calls dangling, and tree.py::_post_remove_sweep_dangling
    invokes it automatically on every tree-node removal. 17,466 experience_ref
    fields were nulled over 13 days; 16,541 of them (94.7%) pointed at records
    that existed. Old values survive in
    world/.history/learning-routing-repair-YYYY-MM-DD.jsonl.
    """
    global _AGENT_CORPUS_OWNED
    _AGENT_CORPUS_OWNED = world_owns_agent_corpus()
    if not _AGENT_CORPUS_OWNED:
        # Foreign world (a tmp fixture): these records are not its records.
        # Returning them would make every ref dangle. See world_owns_agent_corpus.
        return []
    records = []
    root = agents_root()
    for name in ("experience.jsonl", "experience-archive.jsonl"):
        for exp_path in sorted(root.glob("*/" + name)):
            records.extend(_read_jsonl(exp_path))
    return records


def load_tree_node_keys():
    """Tree yaml shape: data['nodes'] is {node_key: node_dict}."""
    if not TREE_YAML.exists():
        return set()
    with TREE_YAML.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return set()
    nodes = data.get("nodes")
    if isinstance(nodes, dict):
        return set(nodes.keys())
    return set()


def build_id_sets(stores):
    return {
        "rb": {r.get("id") for r in stores["reasoning_bank"] if r.get("id")},
        "guard": {r.get("id") for r in stores["guardrails"] if r.get("id")},
        "pipeline": {r.get("id") for r in stores["pipeline"] if r.get("id")},
        "sig": {r.get("id") for r in stores["pattern_signatures"] if r.get("id")},
        "exp": {r.get("id") for r in stores["experience"] if r.get("id")},
    }


# ID-shape regexes. Values that match the pattern but don't resolve are TRUE
# drift. Values that don't match any pattern are field-misuse (prose in an ID
# field) — a separate finding class, not drift.
_ID_PATTERNS = {
    "guard": re.compile(r"^guard-\d+$"),
    "rb": re.compile(r"^rb-\d+$"),
    "exp": re.compile(r"^exp-[A-Za-z0-9_\-\.]+$"),
    "sig": re.compile(r"^sig-\d+$"),
    # pipeline hypothesis IDs are date-prefixed slugs: 2026-04-19_slug-text
    "pipeline": re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Za-z0-9_\-]+$"),
}


def _classify_ref(value, expected_kind):
    """Return ('id', canonical_id) or ('prose', raw). Splits comma-lists into 'id-list'."""
    if not isinstance(value, str):
        return [("prose", str(value))]
    value = value.strip()
    if not value:
        return []
    # Comma-separated ID lists are a documented emission shape (e.g., rb-386 → 'guard-340,guard-341')
    if "," in value:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if len(parts) > 1 and all(_ID_PATTERNS[expected_kind].match(p) for p in parts):
            return [("id", p) for p in parts]
    if _ID_PATTERNS[expected_kind].match(value):
        return [("id", value)]
    return [("prose", value)]


def audit_cross_refs(stores, ids, tree_keys, experience_evaluable=None):
    """Walk each documented linking field; classify IDs vs prose; flag dangling IDs.

    AN ABSENT STORE IS UNMEASURABLE, NEVER EMPTY. When the experience corpus is
    not loadable — a foreign world (world_owns_agent_corpus() False) or a corpus
    that read as zero records — refs INTO it are not falsifiable and refs FROM it
    do not exist. Both directions are SKIPPED rather than flagged.

    This is the deeper half of g-115-5646, and it would have prevented that
    incident on its own. The proximate cause there was a depth-1 glob returning
    zero experience records; the reason zero records became 17,466 nulled fields
    over 13 days is that an empty id-set silently licensed mass invalidation.
    Fixing only the glob leaves that license in place for the next way the corpus
    can come back empty — an unmounted external path, a sync miss, a renamed
    file. Treat "I could not read it" as unknown, and say so out loud
    (guard-1760: report what you declined to look at, not only what you scanned).
    """
    dangling = []
    guardrail_candidates_unfiled = []
    if experience_evaluable is None:
        experience_evaluable = bool(stores.get("experience"))

    def _check(src_store, rec, field, raw, expected_kind, tgt_store, valid_set):
        for kind, val in _classify_ref(raw, expected_kind):
            if kind == "id":
                if val not in valid_set:
                    dangling.append({
                        "store": src_store, "record_id": rec.get("id"),
                        "field": field, "ref": val, "target_store": tgt_store,
                    })
            else:
                guardrail_candidates_unfiled.append({
                    "store": src_store, "record_id": rec.get("id"),
                    "field": field, "expected": expected_kind,
                    "value_preview": (val[:80] + "...") if len(val) > 80 else val,
                })

    # Reasoning bank → guardrail, experience, pipeline
    for r in stores["reasoning_bank"]:
        if r.get("preventive_guardrail"):
            _check("reasoning_bank", r, "preventive_guardrail", r["preventive_guardrail"],
                   "guard", "guardrails", ids["guard"])
        if r.get("experience_ref") and experience_evaluable:
            _check("reasoning_bank", r, "experience_ref", r["experience_ref"],
                   "exp", "experience", ids["exp"])
        if r.get("source_hypothesis"):
            _check("reasoning_bank", r, "source_hypothesis", r["source_hypothesis"],
                   "pipeline", "pipeline", ids["pipeline"])

    # Guardrails → experience, pattern_signatures
    for r in stores["guardrails"]:
        if r.get("experience_ref") and experience_evaluable:
            _check("guardrails", r, "experience_ref", r["experience_ref"],
                   "exp", "experience", ids["exp"])
        for p in r.get("related_patterns") or []:
            _check("guardrails", r, "related_patterns", p,
                   "sig", "pattern_signatures", ids["sig"])

    # Pipeline → experience
    for r in stores["pipeline"]:
        if r.get("experience_ref") and experience_evaluable:
            _check("pipeline", r, "experience_ref", r["experience_ref"],
                   "exp", "experience", ids["exp"])

    # Experience → pipeline, tree. Tree keys are flat kebab-case slugs, not dotted.
    for r in stores["experience"]:
        if r.get("hypothesis_id"):
            _check("experience", r, "hypothesis_id", r["hypothesis_id"],
                   "pipeline", "pipeline", ids["pipeline"])
        for tk in r.get("tree_nodes_related") or []:
            if not isinstance(tk, str) or not tk.strip():
                continue
            tk = tk.strip()
            # PATH-SHAPED REFS RESOLVE BY LEAF SLUG (, 2026-08-10).
            # tree_keys are flat kebab-case slugs, but writers routinely record
            # this field as a PATH ("system/daemon-only-architecture") because
            # that is how tree nodes are named everywhere else. A strict `tk not
            # in tree_keys` therefore flags a valid ref whose leaf exists.
            # Measured on the live corpus the moment the depth-1 glob fix made
            # this branch reachable at all: 573 flagged rows, of which 456 (198
            # unique refs) had their leaf slug present in _tree.yaml. That is
            # not a reporting nit — learning-routing-repair.py --apply NULLS
            # every flagged ref and tree.py::_post_remove_sweep_dangling invokes
            # it automatically on each tree-node removal, so those 456 valid
            # references would have been stripped out of experience records.
            # Deliberately conservative: exact match first, then leaf-slug. A
            # trailing ".md" is NOT stripped — a ref carrying a file extension
            # is genuinely malformed and should keep surfacing.
            if tk not in tree_keys and tk.rstrip("/").split("/")[-1] not in tree_keys:
                dangling.append({
                    "store": "experience", "record_id": r.get("id"),
                    "field": "tree_nodes_related", "ref": tk,
                    "target_store": "knowledge_tree",
                })

    return dangling, guardrail_candidates_unfiled


_DOC_REF_RE = re.compile(r"`(core/config/conventions/[\w\-/.]+\.md)`")
_STORE_COUNT_RE = re.compile(r"^##\s*The\s+(\w+)\s+Stores\b", re.IGNORECASE)

_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
}


def audit_doc_pointers():
    findings = []
    if not LEARNING_ROUTING_MD.exists():
        findings.append({"kind": "missing_source_doc", "path": str(LEARNING_ROUTING_MD)})
        return findings
    text = LEARNING_ROUTING_MD.read_text(encoding="utf-8")
    seen = set()
    for m in _DOC_REF_RE.finditer(text):
        rel = m.group(1)
        if rel in seen:
            continue
        seen.add(rel)
        abs_path = PROJECT_ROOT / rel
        if not abs_path.exists():
            findings.append({"kind": "missing_conventions_file", "path": rel})
    return findings


def audit_store_catalog():
    """Header word (e.g., 'Ten Stores') must match markdown-table row count."""
    findings = []
    if not LEARNING_ROUTING_MD.exists():
        return findings
    lines = LEARNING_ROUTING_MD.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        m = _STORE_COUNT_RE.match(line)
        if not m:
            continue
        word = m.group(1).lower()
        if word not in _WORD_TO_NUM:
            continue
        header_count = _WORD_TO_NUM[word]
        table_start = None
        for j in range(i + 1, min(i + 30, len(lines) - 1)):
            if lines[j].lstrip().startswith("|") and "---" in lines[j + 1]:
                table_start = j + 2
                break
        if table_start is None:
            continue
        row_count = 0
        for k in range(table_start, len(lines)):
            if lines[k].lstrip().startswith("|"):
                row_count += 1
            else:
                break
        if header_count != row_count:
            findings.append({
                "kind": "store_count_mismatch",
                "header_line": i + 1,
                "header_word": word,
                "header_count": header_count,
                "actual_row_count": row_count,
            })
    return findings


def format_report(dangling, prose, doc_findings, catalog_findings, stats):
    out = []
    out.append("=" * 60)
    out.append("LEARNING ROUTING AUDIT")
    out.append("=" * 60)
    out.append(
        f"Stores loaded — rb:{stats['rb']} guard:{stats['guard']} "
        f"pipeline:{stats['pipeline']} sig:{stats['sig']} exp:{stats['exp']} "
        f"tree:{stats['tree']}"
    )
    # Never let a skipped axis read as a clean one (guard-1760). exp:0 has two
    # causes and they demand opposite responses: a foreign world means the
    # corpus is NOT THIS WORLD'S and every experience axis was deliberately not
    # evaluated; an owned-but-empty corpus means the read itself came back
    # empty, which is a defect to chase, not a clean bill.
    if stats.get("exp", 0) == 0:
        out.append(
            "  experience axis SKIPPED — %s. Refs into/out of the experience "
            "store were NOT evaluated; a 0 here is 'unmeasured', not 'clean'."
            % ("foreign world: the agents corpus does not belong to %s" % WORLD_DIR
               if not world_owns_agent_corpus()
               else "corpus owned by this world but read back 0 records")
        )
    drift_total = len(dangling) + len(doc_findings) + len(catalog_findings)
    if drift_total == 0 and not prose:
        out.append("")
        out.append("CLEAN — no drift detected.")
        return "\n".join(out)

    if dangling:
        out.append("")
        out.append(f"Dangling cross-references (ID shape, missing target): {len(dangling)}")
        for f in dangling:
            out.append(
                f"  {f['store']}/{f['record_id']}.{f['field']} = "
                f"{f['ref']!r} -> missing in {f['target_store']}"
            )
    if doc_findings:
        out.append("")
        out.append(f"Broken doc pointers: {len(doc_findings)}")
        for f in doc_findings:
            out.append(f"  {f['kind']}: {f['path']}")
    if catalog_findings:
        out.append("")
        out.append(f"Store-catalog mismatches: {len(catalog_findings)}")
        for f in catalog_findings:
            out.append(
                f"  {f['kind']}: header '{f['header_word']}' = "
                f"{f['header_count']}, actual rows = {f['actual_row_count']} "
                f"(line {f['header_line']})"
            )
    if prose:
        out.append("")
        out.append(
            f"Candidate guardrails (prose in preventive_guardrail, unfiled): {len(prose)}"
        )
        out.append(
            "  Backlog — rule text awaiting a filing pass into guardrails.jsonl."
        )
        out.append(
            "  Mining pattern: for each, dedup against existing guardrails, file a"
        )
        out.append(
            "  new guard-NNN if novel, then update the RB record to point at it."
        )
        for f in prose[:10]:
            out.append(
                f"  {f['store']}/{f['record_id']}.{f['field']}: "
                f"{f['value_preview']}"
            )
        if len(prose) > 10:
            out.append(f"  ... ({len(prose) - 10} more)")
    out.append("")
    out.append(
        f"DRIFT TOTAL: {drift_total} (exit 1 if >0) | "
        f"guardrail-candidate-backlog: {len(prose)} (non-gating)"
    )
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = ap.parse_args()

    stores = {
        "reasoning_bank": load_reasoning_bank(),
        "guardrails": load_guardrails(),
        "pipeline": load_pipeline(),
        "pattern_signatures": load_pattern_signatures(),
        "experience": load_all_experiences(),
    }
    tree_keys = load_tree_node_keys()
    ids = build_id_sets(stores)

    if not stores["reasoning_bank"] and not stores["guardrails"]:
        print("ERROR: no records loaded from reasoning-bank or guardrails", file=sys.stderr)
        sys.exit(2)

    dangling, prose = audit_cross_refs(stores, ids, tree_keys)
    doc_findings = audit_doc_pointers()
    catalog_findings = audit_store_catalog()

    stats = {
        "rb": len(ids["rb"]),
        "guard": len(ids["guard"]),
        "pipeline": len(ids["pipeline"]),
        "sig": len(ids["sig"]),
        "exp": len(ids["exp"]),
        "tree": len(tree_keys),
    }

    drift_total = len(dangling) + len(doc_findings) + len(catalog_findings)

    if args.json:
        print(json.dumps({
            "stats": stats,
            "dangling": dangling,
            "guardrail_candidates_unfiled": prose,
            "doc_pointers": doc_findings,
            "catalog": catalog_findings,
            "drift_total": drift_total,
            "prose_count": len(prose),
        }, indent=2))
    else:
        print(format_report(dangling, prose, doc_findings, catalog_findings, stats))

    sys.exit(0 if drift_total == 0 else 1)


if __name__ == "__main__":
    main()
